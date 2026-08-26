#!/usr/bin/env python3
# pipeline.py — End-to-end REO deal pipeline runner.
#
# Runs the full spine: source → enrich → value → underwrite → price → memo → review.
# Usage:
#   python3 pipeline.py --checks-only               # module self-checks only (zero network)
#   python3 pipeline.py --all-checks                # self-checks first, then a data mode
#   python3 pipeline.py --fixture                   # offline fixture run -> data/fixtures/
#   python3 pipeline.py --live --county collin      # OPT-IN live scrape (network required)
#   python3 pipeline.py --bulk-dcad                 # DCAD bulk cross-reference
#   python3 pipeline.py --geocode-backfill          # OPT-IN Census geocode backfill (network)
#   python3 pipeline.py --geocode-backfill --dry-run  # plan only — zero network
#   python3 pipeline.py --geocode-backfill --retry-failed  # override failure ledger
#   python3 pipeline.py --resume-valued data/cpr_X.valued.json   # finish one HITL-valued parcel
#   python3 pipeline.py --live --county collin --min-leads 5     # exit 4 below lead floor
#
# Live scraping is strictly opt-in via --live. With no data mode selected the
# runner prints a hint and exits 0 without touching the network.
#
# Exit codes: 0 ok · 1 usage/data-missing · 2 gate-fail · 3 lookup-error ·
# 4 below --min-leads floor — cataloged in docs/EXIT_CODES.md (see --help).
from __future__ import annotations
import argparse
import hashlib
import re
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING

# Ensure src is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.addr import is_plausible_street_address, to_census_one_line  # shared outbound-query normalizer
from src.io_utils import write_json, write_text  # atomic crash-safe writers (shared contract)
from src import policy  # B4: single source of truth for decision-policy constants

if TYPE_CHECKING:
    from collections.abc import Callable

    from src.cpr import CPR, ValuationObject
    from src.compliance import GateResult
    from src.valuation import GateValResult

CHECK_TIMEOUT_SECONDS = 120
CHECK_WORKERS = 4

MODULES = [
    "src.legal_rules", "src.cpr", "src.source_registry",
    "src.adapters.base", "src.adapters.dallas_foreclosure",
    "src.adapters.dallas_cad", "src.adapters.dallas_cad_bulk",
    "src.adapters.collin_clerk", "src.adapters.grayson_tax",
    "src.adapters.route_optimizer", "src.adapters.geojson_export",
    "src.adapters.notice_ocr",
    "src.enrichment", "src.valuation", "src.underwriting",
    "src.pricing", "src.deal_memo", "src.compliance", "src.reviewer",
    "src.synthesis", "src.negotiation",
]

MODE_HINT = """\
  No data mode selected — nothing scraped, nothing written.

    pipeline.py --checks-only            run module self-checks and exit (zero network)
    pipeline.py --all-checks             self-checks first, then one mode below
    pipeline.py --fixture                offline fixture run (writes data/fixtures/)
    pipeline.py --live --county collin   OPT-IN live portal scrape (network)
    pipeline.py --bulk-dcad              DCAD bulk cross-reference (Dallas CPRs)
    pipeline.py --geocode-backfill [--dry-run]
                                         OPT-IN Census geocode backfill into
                                         current_deals.json (network; --dry-run plans only)
    pipeline.py --resume-valued <file>   finish a HITL-valued CPR (memo/offer into
                                         data/, production naming)

  Live scraping is opt-in: pass --live explicitly.
  Exit codes are cataloged in docs/EXIT_CODES.md."""


# --- Exit-code catalog (docs/EXIT_CODES.md) ---------------------------------
# Stable process exit codes shared by every mode. docs/EXIT_CODES.md is the
# human-readable catalog referenced from --help; this mapping is its
# executable source of truth so code and docs can never silently drift apart.
EXIT_OK = 0
EXIT_USAGE = 1            # bad usage / failed self-checks / uncaught error
EXIT_DATA_MISSING = 1     # dataset or input file absent/unreadable
EXIT_GATE_FAIL = 2        # GATE-LEGAL-LIEN fail/hitl (not lookup-caused)
EXIT_LOOKUP_ERROR = 3     # gate block caused by an errored lien lookup
EXIT_MIN_LEADS = 4        # live scrape returned fewer leads than --min-leads

EXIT_CODES = {
    "ok": EXIT_OK,
    "usage": EXIT_USAGE,
    "data_missing": EXIT_DATA_MISSING,
    "gate_fail": EXIT_GATE_FAIL,
    "lookup_error": EXIT_LOOKUP_ERROR,
    "min_leads": EXIT_MIN_LEADS,
}


def exit_code_for(outcome: str) -> int:
    """Map a named run outcome to its process exit code (docs/EXIT_CODES.md)."""
    try:
        return EXIT_CODES[outcome]
    except KeyError:
        raise ValueError(
            f"unknown exit-code outcome {outcome!r}; known: {sorted(EXIT_CODES)}"
        ) from None


def _utc_now() -> datetime:
    """Timezone-aware clock; injectable in tests (never naive utcnow)."""
    return datetime.now(timezone.utc)


# --- RL4: operator run summaries ----------------------------------------------

RUN_SUMMARY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data", "run_summary.json")
SUMMARY_COUNT_KEYS = ("leads", "parsed", "mismatch", "no_data_found",
                      "errors", "enriched", "written_back")


class RunSummary:
    """Structured per-invocation summary written to data/run_summary.json.

    Every data mode (fixture/live/bulk-dcad/geocode/resume) ends by writing
    one of these through io_utils.write_json, so an operator can tell "the
    scrape came back empty" apart from "the scrape errored halfway". Counts
    are grouped per source name ('fixture', 'collin_foreclosure',
    'dcad_bulk', 'census_geocode', 'resume', ...) over a FIXED key set so
    consumers never key-miss:

        leads          items discovered by the source
        parsed         items successfully turned into CPRs/queries
        mismatch       items skipped on identity/address mismatch
        no_data_found  sources queried that yielded nothing usable
        errors         caught failures (run continued past them)
        enriched       CPRs enriched/completed this run
        written_back   dataset records updated by a write-back
    """

    def __init__(self, mode: str, flags: dict | None = None,
                 path: str = RUN_SUMMARY_PATH,
                 now_fn: Callable[[], datetime] = _utc_now):
        self.mode = mode
        self.flags = dict(flags or {})
        self.path = path
        self.now_fn = now_fn
        self.started_at = now_fn()
        self._counts: dict[str, dict[str, int]] = {}

    def source(self, name: str) -> dict[str, int]:
        """Zeroed count row for *name*, auto-created."""
        return self._counts.setdefault(name, {k: 0 for k in SUMMARY_COUNT_KEYS})

    def bump(self, source: str, key: str, n: int = 1) -> None:
        """Increment one count; unknown keys raise instead of drifting."""
        if key not in SUMMARY_COUNT_KEYS:
            raise ValueError(f"unknown summary count key {key!r}; "
                             f"known: {SUMMARY_COUNT_KEYS}")
        self.source(source)[key] += n

    def as_dict(self) -> dict:
        duration = (self.now_fn() - self.started_at).total_seconds()
        return {
            "mode": self.mode,
            "started_at": self.started_at.isoformat(),
            "duration_s": round(duration, 3),
            "counts": {name: dict(counts)
                       for name, counts in self._counts.items()},
            "flags": self.flags,
        }

    def write(self) -> str:
        """Atomically persist the summary; returns its path."""
        payload = self.as_dict()
        write_json(self.path, payload)
        print(f"\nRun summary [{payload['mode']}]: {self.path} "
              f"(duration {payload['duration_s']}s)")
        return self.path


def _iso_week_label(now_fn: Callable[[], datetime] = _utc_now) -> str:
    """ISO-8601 week tag for digest filenames, e.g. '2026-W35'."""
    iso = now_fn().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def write_weekly_digest(cprs: list[CPR], output_dir: str,
                        filename_prefix: str = "",
                        dead_reasons: dict | None = None,
                        now_fn: Callable[[], datetime] = _utc_now) -> str:
    """F9: render src.synthesis.weekly_digest and write it atomically.

    weekly_digest(cprs, dead_reasons=None) is standalone-friendly — it needs
    only the run's CPR objects — so no wrapper state is required. Fixture
    runs pass filename_prefix='fixture_' and the fixtures output_dir, keeping
    production-named data/digest_<ISO-week>.md reserved for live runs.
    """
    from src.synthesis import weekly_digest

    name = f"{filename_prefix}digest_{_iso_week_label(now_fn)}.md"
    return write_text(os.path.join(output_dir, name),
                      weekly_digest(cprs, dead_reasons))


def run_all_checks() -> bool:
    """Run all module self-checks concurrently. Returns True if all pass."""
    def _check(mod: str) -> tuple[str, int, str]:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", mod],
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT_SECONDS,
            )
            return mod, proc.returncode, proc.stderr or ""
        except subprocess.TimeoutExpired:
            return mod, 124, f"self-check timed out after {CHECK_TIMEOUT_SECONDS}s"
        except OSError as exc:
            return mod, 127, repr(exc)

    results = []
    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        futures = [pool.submit(_check, mod) for mod in MODULES]
        for fut in futures:  # collect in module order for deterministic output
            results.append(fut.result())

    all_pass = True
    for mod, ret, stderr in results:
        status = "✓" if ret == 0 else "✗"
        print(f"  {status} {mod}")
        if ret != 0:
            all_pass = False
            lines = [ln for ln in stderr.splitlines() if ln.strip()]
            if lines:
                tail = lines[-15:]
                print(f"    last {len(tail)} stderr line(s):")
                for ln in tail:
                    print(f"      | {ln}")
    return all_pass


# --- Shared enrichment helpers (used by BOTH fixture and live paths) ---

def _clean_owner_name(owner_raw: str) -> str:
    """Clean an owner name for Kofile party search.

    Kofile matches exact strings — long names with '&' return nothing, so take
    the first two words (first + last name), uppercased, suffixes dropped.
    """
    parts = owner_raw.replace("&", " ").split()
    return " ".join(parts[:2]).upper()


def _clerk_lien_search(cpr: CPR) -> None:
    """Collin County clerk lien lookup, merged into the CPR encumbrance stack.

    Loud-failure contract (audit S7/E1): success sets cpr.lien_lookup_status='ok'
    (zero instruments is still a successful lookup); ANY exception sets 'error',
    stores repr on cpr.lien_lookup_error and prints CLERK-LOOKUP-FAILED to stderr.
    A failed search must never be indistinguishable from clean title.
    """
    from src.adapters.collin_clerk import CollinClerkAdapter, build_lien_stack

    owner_raw = cpr.owner_name.value if cpr.owner_name else ""
    owner_search = _clean_owner_name(owner_raw)
    print(f"  Searching by owner: {owner_search} (from '{owner_raw}')")
    try:
        clerk = CollinClerkAdapter()
        instruments = clerk.fetch(party_name=owner_search)
        print(f"  Instruments found: {len(instruments)}")
        clerk_liens = build_lien_stack(instruments) if instruments else []
        if clerk_liens:
            print(f"  Lien stack: {len(clerk_liens)} encumbrances")
            for l in clerk_liens:
                s_fc = "SURVIVES FC" if l.survives_foreclosure else ""
                print(f"    #{l.priority} {l.lien_type:15s} | {str(l.recording_date):35s} | {s_fc}")
            for l in clerk_liens:
                cpr.add_lien(l, "collin_clerk")
            print(f"  → {len(clerk_liens)} liens merged into CPR (total: {len(cpr.liens)})")
        cpr.lien_lookup_status = "ok"
    except Exception as exc:
        cpr.lien_lookup_status = "error"
        cpr.lien_lookup_error = repr(exc)
        print(f"CLERK-LOOKUP-FAILED: {exc!r}", file=sys.stderr)


def _dcad_xref(cpr: CPR) -> CPR:
    """Dallas CAD cross-reference with loud failure semantics."""
    from src.enrichment import cross_reference_dcad
    try:
        cpr = cross_reference_dcad(cpr)
        cpr.dcad_xref_status = "ok"
    except Exception as exc:
        cpr.dcad_xref_status = "error"
        cpr.dcad_xref_error = repr(exc)
        print(f"DCAD-XREF-FAILED: {exc!r}", file=sys.stderr)
        return cpr
    if cpr.improvement_sqft and cpr.improvement_sqft.value:
        beds = cpr.beds.value if cpr.beds else "?"
        print(f"  DCAD: {cpr.improvement_sqft.value:,.0f} sqft, {beds} bd")
    else:
        print("  DCAD: no results (address may not match)")
    return cpr


def _effective_lien_gate(cpr: CPR) -> tuple[GateResult, bool]:
    """GATE-LEGAL-LIEN with lookup-error semantics (audit S7).

    Returns (gate_result, lookup_caused). An errored lien lookup forces verdict
    'hitl' regardless of the raw gate outcome: a search that never ran cannot
    evidence clean title.
    """
    from src.compliance import GateResult, gate_legal_lien

    gate = gate_legal_lien(cpr)
    status = getattr(cpr, "lien_lookup_status", "not_run")
    if status != "error":
        return gate, False
    reason = (
        f"Lien lookup errored ({getattr(cpr, 'lien_lookup_error', '')}) — "
        "failed search is indistinguishable from clean title; attorney/title HITL required"
    )
    details = dict(gate.details or {})
    details["lien_lookup_status"] = "error"
    return GateResult(gate.gate_name, "hitl", reason, details), True


def _gate_exit_code(lookup_caused: bool) -> int:
    """Exit 3 when the gate block was caused by a lookup error, else exit 2."""
    return exit_code_for("lookup_error" if lookup_caused else "gate_fail")


def _print_gate_block_banner(verdict: str, reason: str, lookup_caused: bool) -> None:
    print("\n" + "!" * 70)
    print(f"!! GATE-LEGAL-LIEN {verdict.upper()} — pricing/bidding BLOCKED")
    print(f"!! Reason: {reason}")
    if lookup_caused:
        print("!! Cause: lien LOOKUP ERROR (not clean title — see CLERK-LOOKUP-FAILED above)")
    print("!" * 70)


DEFAULT_REHAB_ESTIMATE = 50_000  # quick-quote rehab (2004 build, ~4,300 sqft)


def process_valued_cpr(cpr: CPR, rehab: float = DEFAULT_REHAB_ESTIMATE,
                       output_dir: str | None = None,
                       artifact_prefix: str = "",
                       summary: "RunSummary | None" = None,
                       source_label: str = "fixture",
                       gate_val_result: GateValResult | None = None) -> dict | None:
    """Steps 5–8 of the spine for a VALUED CPR — the shared pipeline tail.

    F1: extracted verbatim from run_fixture_pipeline so BOTH the offline
    fixture run and --resume-valued execute identical underwrite → price →
    review → memo → negotiation logic over a CPR that already carries a
    GATE-VAL-passed valuation (cpr.valuation.arv). Artifacts land in
    *output_dir* named f"{artifact_prefix}<kind>_<parcel_id>.<ext>" — the
    fixture passes prefix "fixture_" and data/fixtures/, resume passes ""
    and data/ (production naming).

    deal_type routing reads the CPR's own deal_type when set (resumed valued
    CPRs carry one); empty falls back to DT-AUCTION exactly as before.
    *gate_val_result* is the in-memory GateValResult for this run when one
    exists (fixture path). A serialized *.valued.json cannot carry the
    gate's confidence/floor, so resume passes None and the reviewer's
    fabricated-confidence check degrades to its honest not-checkable state
    instead of inventing numbers.

    Returns a result dict {cpr, arv, rehab, underwrite, pricing, review,
    memo/memo_path/cpr_path/offer_path}, or None when no usable ARV is
    attached — callers decide their own exit semantics for that case.
    """
    from src.cpr import pv_value
    from src.underwriting import flip_pnl, mao
    from src.pricing import price_deal
    from src.deal_memo import render_memo
    from src.reviewer import run_review

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data")

    valuation = cpr.valuation
    arv = pv_value(valuation.arv) if valuation and valuation.arv else None
    if not arv:
        print("  No ARV attached to this CPR — underwriting requires a "
              "passed GATE-VAL first")
        return None

    # Step 5: Underwrite
    print("\n[5/7] UNDERWRITE — Fix-and-flip model")
    # B4: policy wiring — closing costs and MAO factor come from src.policy so
    # this model can never drift from the Excel Assumptions tab again (the old
    # inline arv*0.08 disagreed with the workbook's 7.5%).
    uw = flip_pnl(
        arv=arv, purchase=mao(arv, rehab, policy.MAO_FACTOR_DEFAULT), rehab=rehab,
        holding=5_000, financing=10_000, closing_buy=5_000,
        closing_sell=arv * policy.CLOSING_SELL_PCT,
    )
    print(f"  Model: {uw.model}")
    print(f"  MAO ({policy.MAO_FACTOR_DEFAULT:.0%} rule): ${mao(arv, rehab, policy.MAO_FACTOR_DEFAULT):,.0f}")
    print(f"  Purchase: ${uw.purchase_price:,.0f} | Rehab: ${uw.rehab:,.0f} (w/contingency: ${uw.line_items['rehab_with_contingency']:,.0f})")
    print(f"  Net profit: ${uw.net_profit:,.0f} | ROI: {uw.roi:.1%}")

    # Step 6: Pricing
    print("\n[6/7] PRICING — MAO, bid ceiling, offer ladder, sensitivity")
    deal_type = str(pv_value(getattr(cpr, "deal_type", "")) or "DT-AUCTION")
    pricing = price_deal(
        cpr, arv=arv, rehab=rehab, deal_type=deal_type,
        factor=policy.MAO_FACTOR_DEFAULT, capital_at_risk=uw.cash_invested,
    )
    print(f"  MAO: ${pricing.mao:,.0f}")
    print(f"  Bid ceiling: ${pricing.bid_ceiling:,.0f}")
    print(f"  Blast tier: {pricing.blast_tier.name}")
    if pricing.offer_ladder:
        ladder = pricing.offer_ladder
        print(f"  Offer ladder: open ${ladder['opening']:,.0f} → target ${ladder['target']:,.0f} → walk ${ladder['walk_away']:,.0f}")
    else:
        print("  Offer ladder: none — bid ceiling non-positive (dead deal, C2)")
    print(f"  Optimistic-only flag: {pricing.optimistic_only_flag}")

    # Step 7: Reviewer — fake-signal harness
    print("\n[7/7] REVIEWER — AGENT-REV fake-signal harness")
    review = run_review(cpr, gate_val_result, pricing)
    print(f"  Overall: {review.overall} ({len(review.checks)} checks)")
    for check in review.checks:
        symbol = "✓" if check.verdict == "pass" else "✗"
        print(f"  {symbol} {check.check_name}: {check.reason}")
    if review.rejection_reasons:
        print("\n  ⚠️ REJECTIONS:")
        for r in review.rejection_reasons:
            print(f"    ✗ {r}")

    # Render deal memo
    print("\n" + "=" * 70)
    print("  DEAL MEMO")
    print("=" * 70)
    memo = render_memo(cpr, uw, pricing, review=review)
    print(memo)

    print("\n" + "=" * 70)
    print("  NEGOTIATION — Seller playbook + offer generation")
    print("=" * 70)
    offer_path = None
    try:
        from src.negotiation import generate_offer, get_posture
        # Ensure deal_type is set
        dt = cpr.deal_type
        dt_val = dt.value if hasattr(dt, 'value') else dt
        if not dt_val:
            cpr.set_field("deal_type", "DT-PREFORE", "pipeline", 0.80)
            dt_val = "DT-PREFORE"
        posture = get_posture(dt_val)
        offer = generate_offer(cpr, pricing)
        print(f"  Deal type: {dt_val}")
        print(f"  Posture: {posture['framing']}")
        print(f"  Offer price: ${offer.offer_price:,.0f}")
        print(f"  Earnest money: ${offer.earnest_money:,.0f} | Option fee: ${offer.option_fee:,.0f}")
        print(f"  Option period: {offer.option_period_days}d | Closing: {offer.closing_timeline_days}d")
        print(f"  Cash offer: {offer.is_cash} | And/or assigns: {offer.has_and_or_assigns}")
        if offer.blocked:
            print(f"  ⚠️ BLOCKED: {offer.block_reason}")
        else:
            print(f"  Key message: {offer.key_message[:80]}")
        parcel = str(cpr.parcel_id or "").strip() or "unknown_parcel"
        offer_path = write_text(
            os.path.join(output_dir, f"{artifact_prefix}offer_letter_{parcel}.txt"),
            offer.offer_letter)
        print(f"  Saved: {offer_path}")
    except Exception as e:
        if summary is not None:
            summary.bump(source_label, "errors")
        print(f"  Negotiation skipped: {e!r}")

    parcel = str(cpr.parcel_id or "").strip() or "unknown_parcel"
    memo_path = write_text(
        os.path.join(output_dir, f"{artifact_prefix}deal_memo_{parcel}.md"), memo)
    cpr_path = write_json(
        os.path.join(output_dir, f"{artifact_prefix}cpr_{parcel}.json"),
        cpr.to_dict(), default=str)

    return {
        "cpr": cpr, "arv": arv, "rehab": rehab,
        "underwrite": uw, "pricing": pricing, "review": review,
        "memo": memo, "memo_path": memo_path, "cpr_path": cpr_path,
        "offer_path": offer_path,
    }


# --- F1: --resume-valued — consume HITL-valued CPRs ---------------------------
# hitl.py writes *.valued.json files that nothing consumed (OPPORTUNITY_MAP
# F1): price_deal/render_memo/generate_offer sat idle downstream of them.

RESUME_SUMMARY_SOURCE = "resume"  # RL4 counts bucket for this mode
DATASET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "current_deals.json")


def restore_serialized_valuation(raw: dict) -> ValuationObject | None:
    """Rebuild the ValuationObject that src.hitl.load_cpr deliberately drops.

    load_cpr ignores any stored valuation by contract ("the HITL run replaces
    it wholesale") — right for intake, wrong for resume: a *.valued.json IS
    the post-HITL artifact and its attached GATE-VAL valuation is the whole
    point of resuming it. Tolerant by design: junk shapes degrade into a
    valuation without an ARV, which process_valued_cpr then refuses honestly.
    """
    import dataclasses

    from src.cpr import CompRecord, ProvenanceField, ValuationObject

    val_raw = raw.get("valuation")
    if not isinstance(val_raw, dict):
        return None

    def _pf(d):
        if not isinstance(d, dict) or d.get("value") in (None, ""):
            return None
        pf = ProvenanceField(value=d.get("value"),
                             source=str(d.get("source") or ""))
        if d.get("retrieved_at"):
            pf.retrieved_at = str(d["retrieved_at"])
        try:
            pf.confidence = float(d.get("confidence") or 0.0)
        except (TypeError, ValueError):
            pf.confidence = 0.0
        return pf

    comp_fields = {f.name for f in dataclasses.fields(CompRecord)}
    comp_set = []
    for cd in (val_raw.get("comp_set") or []):
        if isinstance(cd, dict) and cd.get("address"):
            comp_set.append(CompRecord(**{k: cd[k] for k in comp_fields
                                          if k in cd}))

    return ValuationObject(
        as_is_value=_pf(val_raw.get("as_is_value")),
        arv=_pf(val_raw.get("arv")),
        rent_estimate=_pf(val_raw.get("rent_estimate")),
        comp_set=comp_set,
        comp_source=str(val_raw.get("comp_source") or ""),
        gate_val_verdict=str(val_raw.get("gate_val_verdict") or ""),
        gate_val_reason=str(val_raw.get("gate_val_reason") or ""),
    )


def _stamp_val_gate(records: list[dict], county: str, address: str,
                    value: str = "pass") -> int:
    """Stamp *value* onto dataset rows matching this parcel's identity.

    Uses the same normalize_deal_identity merge key as find_deals.py's
    merge-on-write and the DCAD write-back, so a resumed parcel matches its
    dataset row exactly the way every other producer does. Rows already at
    *value* are left untouched (idempotent re-runs don't inflate counts).
    Mutates *records* in place; returns how many rows changed — the caller
    rewrites the file atomically.
    """
    from src.enrichment import normalize_deal_identity

    key = normalize_deal_identity(county, address)
    stamped = 0
    for rec in records:
        if rec.get("val_gate") == value:
            continue
        if normalize_deal_identity(rec.get("county"), rec.get("address")) == key:
            rec["val_gate"] = value
            stamped += 1
    return stamped


def _resume_stamp_dataset(cpr: CPR, dataset_path: str) -> int:
    """Atomically stamp val_gate='pass' on *cpr*'s row in current_deals.json."""
    if not os.path.exists(dataset_path):
        print(f"  Dataset not found at {dataset_path} — val_gate stamp skipped")
        return 0
    with open(dataset_path, encoding="utf-8") as f:
        records = json.load(f)
    stamped = _stamp_val_gate(records, cpr.county, cpr.situs_address)
    if stamped:
        write_json(dataset_path, records)  # atomic merge, DCAD write-back pattern
        print(f"  Dataset stamp: val_gate=pass → {stamped} row(s) in "
              f"{dataset_path} (atomic)")
    else:
        print("  Dataset stamp: no matching row (or already pass) — dataset untouched")
    return stamped


def run_resume_valued_pipeline(valued_path: str,
                               rehab: float = DEFAULT_REHAB_ESTIMATE,
                               summary: RunSummary | None = None,
                               output_dir: str | None = None,
                               dataset_path: str | None = None) -> int:
    """F1: close the HITL loop — finish one valued CPR end-to-end.

    Loads *valued_path* (a saved post-HITL ``*.valued.json``) through
    src.hitl.load_cpr, re-attaches the serialized GATE-VAL valuation that
    loader drops by contract (restore_serialized_valuation), then runs the
    IDENTICAL underwrite → price → review → memo → negotiation tail as
    --fixture via the shared process_valued_cpr.

    Artifacts land in data/ under PRODUCTION names keyed by parcel
    (deal_memo_<parcel>.md, offer_letter_<parcel>.txt, cpr_<parcel>.json).
    When the file's carried GATE-VAL verdict is 'pass' and a current_deals.json
    row matches by county+address, that row's val_gate is atomically stamped
    'pass' like the bulk write-back.

    Returns a docs/EXIT_CODES.md exit code: 0 ok · 1 input absent/unreadable ·
    2 non-pass verdict (pricing a deal the valuation gate rejected would
    fabricate a signal).

    *output_dir*/*dataset_path* default to data/ production locations;
    tests override both to keep runs hermetic.
    """
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data")
    if dataset_path is None:
        dataset_path = DATASET_PATH

    print("\n" + "=" * 70)
    print("  HERMES-REO PIPELINE — RESUME VALUED CPR")
    print("=" * 70)

    if not os.path.exists(valued_path):
        print(f"!! No such valued CPR file: {valued_path}")
        return exit_code_for("data_missing")

    from src import hitl

    try:
        with open(valued_path, encoding="utf-8") as f:
            raw = json.load(f)
        cpr = hitl.load_cpr(valued_path)
    except (OSError, ValueError) as exc:
        print(f"!! Could not load valued CPR {valued_path}: {exc!r}")
        return exit_code_for("data_missing")

    cpr.valuation = restore_serialized_valuation(raw)
    verdict = cpr.valuation.gate_val_verdict if cpr.valuation else ""
    print(f"  Resumed: {cpr.parcel_id} ({cpr.county}) — {cpr.situs_address}")
    print(f"  Carried GATE-VAL: {verdict or 'none'}")
    if summary is not None:
        summary.bump(RESUME_SUMMARY_SOURCE, "parsed")

    if verdict != "pass":
        if summary is not None:
            summary.bump(RESUME_SUMMARY_SOURCE, "errors")
        print("!! Not a VALUED verdict — run python -m src.hitl first to "
              "attach an approved-source valuation.")
        return exit_code_for("gate_fail")

    result = process_valued_cpr(cpr, rehab=rehab, output_dir=output_dir,
                                artifact_prefix="", summary=summary,
                                source_label=RESUME_SUMMARY_SOURCE,
                                gate_val_result=None)
    if result is None:
        if summary is not None:
            summary.bump(RESUME_SUMMARY_SOURCE, "errors")
        print("!! RESUME-INCOMPLETE: no usable ARV attached — run the HITL "
              "comp intake (python -m src.hitl) before resuming")
        return exit_code_for("usage")

    stamped = _resume_stamp_dataset(cpr, dataset_path)
    if summary is not None and stamped:
        summary.bump(RESUME_SUMMARY_SOURCE, "written_back", stamped)

    print("\n" + "=" * 70)
    print("  RESUME COMPLETE — memo/offer written under production names")
    print("=" * 70)
    return EXIT_OK


def run_resume_valued_dir_pipeline(dir_path: str,
                                   rehab: float = DEFAULT_REHAB_ESTIMATE,
                                   summary: RunSummary | None = None,
                                   output_dir: str | None = None,
                                   dataset_path: str | None = None) -> int:
    """F2: loop F1 --resume-valued semantics over every *.valued.json in a dir.

    Thin driver over run_resume_valued_pipeline (identical per-file behavior:
    artifacts, dataset stamping, summary counts). Files are processed in
    sorted order and one bad file never stops the batch; the aggregate
    {resumed, skipped_not_pass, errors} is printed at the end.

    Returns 0 when EVERY file was processed to a disposition (resumed or
    skipped-not-pass); 1 (data-missing) when any file errored or the
    directory itself does not exist. An existing-but-empty directory is ok.
    """
    if not os.path.isdir(dir_path):
        print(f"!! No such valued-CPR directory: {dir_path}")
        return exit_code_for("data_missing")

    files = sorted(os.path.join(dir_path, name)
                   for name in os.listdir(dir_path)
                   if name.endswith(".valued.json"))
    aggregate = {"resumed": 0, "skipped_not_pass": 0, "errors": 0}
    if not files:
        print(f"No *.valued.json files in {dir_path} — nothing to resume")
    for valued_path in files:
        try:
            rc = run_resume_valued_pipeline(valued_path, rehab=rehab,
                                            summary=summary,
                                            output_dir=output_dir,
                                            dataset_path=dataset_path)
        except (OSError, ValueError) as exc:
            print(f"!! Unexpected failure resuming {valued_path}: {exc!r}")
            rc = exit_code_for("data_missing")
        if rc == EXIT_OK:
            aggregate["resumed"] += 1
        elif rc == EXIT_GATE_FAIL:
            aggregate["skipped_not_pass"] += 1
        else:
            aggregate["errors"] += 1

    print("\n" + "=" * 70)
    print(f"  RESUME-DIR COMPLETE — {dir_path}")
    print(f"  Aggregate: resumed={aggregate['resumed']} "
          f"skipped_not_pass={aggregate['skipped_not_pass']} "
          f"errors={aggregate['errors']} total={len(files)}")
    print("=" * 70)
    return EXIT_OK if aggregate["errors"] == 0 else exit_code_for("data_missing")


def run_fixture_pipeline(allow_gate_fail_demo: bool = False,
                         summary: RunSummary | None = None) -> None:
    """Run the full pipeline on fixture data.

    Uses the verified Collin County parcel from lucid-noether (entry_id 4003592).
    All outputs go to data/fixtures/ with a fixture_ prefix — fixture runs NEVER
    touch production-named files.

    Steps 5–8 (underwrite → price → review → memo → negotiation) are the
    shared process_valued_cpr tail, identical to --resume-valued (F1).
    """
    from src.cpr import CompRecord, BlastTier
    from src.enrichment import enrich_from_collin_details
    from src.adapters.base import NormalizedLead
    from src.valuation import gate_val, attach_valuation

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(repo_dir, "data", "fixtures")
    summary_source = "fixture"
    if summary is not None:
        summary.bump(summary_source, "leads")

    print("=" * 70)
    print("  HERMES-REO PIPELINE — FIXTURE RUN (entry_id 4003592)")
    print("=" * 70)

    # Step 1: Source — fixture lead (from lucid-noether verified data)
    print("\n[1/7] SOURCE — Collin County foreclosure notice (fixture)")
    lead = NormalizedLead(
        county="collin", parcel_id="4003592", channel="foreclosure_notice",
        situs_address="10022 Plainsman Ln Frisco, TX 75035",
        latitude=33.17922234, longitude=-96.79394064,
        raw_data={"details": {
            "property_info": {
                "Address": "10022 Plainsman Ln Frisco, TX 75035",
                "Type": "Residential Single Family (A1)",
                "Deed Date": "7/25/2013",
                "Legal Description": "Village At Panther Creek Phase One, The (Cfr), Blk N, Lot 32",
                "Commercial": "False",
            },
            "improvement_info": {
                "Improvement Area": "4,332 sq ft",
                "Year Built": "2004",
                "Bedrooms": "5",
                "Bathrooms": "4",
            },
            "jurisdiction_info": {
                "Taxing Entities": "Collin County (GCN) / Frisco ISD (SFR) / Frisco City (CFR)",
            },
            "owner_info": {
                "Name": "Islam Mazhar & Ambreen",
                "Address": "10022 Plainsman Ln / Frisco, TX 75035-6919",
            },
            "tax_info": {
                "Appraised Value (2026)": "$730,673.00",
            },
        }},
    )
    print(f"  Lead: {lead.situs_address} ({lead.county})")

    # Step 2: Enrich — assemble CPR
    print("\n[2/7] ENRICH — Assemble Canonical Property Record")
    cpr = enrich_from_collin_details(lead)
    cpr.lien_lookup_status = "not_run"  # until a lookup actually runs (S7 contract)
    print(f"  CPR: {cpr.parcel_id} | {cpr.situs_address}")
    print(f"  Owner: {cpr.owner_name.value} | Sqft: {cpr.improvement_sqft.value} | Beds: {cpr.beds.value}")
    print(f"  CAD: ${cpr.cad_appraised_value.value:,.0f} | Column: {cpr.column.value}")
    print(f"  Ledger entries: {len(cpr.ledger)}")
    if summary is not None:
        summary.bump(summary_source, "parsed")
        summary.bump(summary_source, "enriched")

    # Step 2b: Lien stack — SEEDED from the live clerk verification of this
    # parcel (Aug 2026: 31 instruments, 12 encumbrances). Fixture runs are
    # fully offline by contract; live runs perform the real lookup.
    if cpr.county == "collin":
        print("\n[2b] CLERK LIEN STACK — fixture seed (offline; verified 4003592 instruments)")
        from src.cpr import LienRecord
        for ln in [
            LienRecord(lien_type="mortgage", amount=412_300.0, recording_date="1/2/2015",
                       lender="UNKNOWN", priority=2, survives_foreclosure=False, survives_tax_sale=False),
            LienRecord(lien_type="mortgage", amount=96_800.0, recording_date="1/28/2005",
                       lender="UNKNOWN", priority=3, survives_foreclosure=False, survives_tax_sale=False),
            LienRecord(lien_type="tax", amount=9_412.0, recording_date="11/11/2014",
                       lender="COLLIN COUNTY TAX OFFICE", priority=8,
                       survives_foreclosure=True, survives_tax_sale=True),
            LienRecord(lien_type="hoa", amount=1_240.0, recording_date="9/9/2024",
                       lender="VILLAGE AT PANTHER CREEK HOA", priority=31,
                       survives_foreclosure=False, survives_tax_sale=False),
        ]:
            cpr.add_lien(ln, "fixture_clerk_seed")
        cpr.lien_lookup_status = "ok"  # seeded from a previously verified live search
        print(f"  Seeded {4} encumbrances (mortgages, SURVIVING tax lien, HOA)")
    elif cpr.county == "dallas":
        print("\n[2b] DCAD CROSS-REFERENCE — Dallas CAD enrichment")
        cpr = _dcad_xref(cpr)

    # Step 3: GATE-LEGAL-LIEN — mandatory before pricing
    print("\n[3/7] GATE-LEGAL-LIEN — Title & lien priority check")
    lien_gate, lookup_caused = _effective_lien_gate(cpr)
    print(f"  Verdict: {lien_gate.verdict} — {lien_gate.reason}")
    if lien_gate.verdict in ("fail", "hitl"):
        # Fixture mode: this is a demo dataset — the honest verdict (here: a
        # SURVIVING tax lien) is expected. Print it loudly, stamp the CPR,
        # and continue so the spine exercises end-to-end offline.
        print("!! FIXTURE MODE: gate block noted — continuing (demo dataset; "
              "live runs halt here with exit 2/3).")
        cpr.dead_reason = cpr.dead_reason or f"fixture_gate_{lien_gate.verdict}"

    # Step 4: Valuation — GATE-VAL
    print("\n[4/7] VALUATION — GATE-VAL (ARV grounding gate)")
    # Simulate MLS agent comps (in production, these come from the MLS HITL).
    # Sale dates are dynamically recent (30d) so recency scoring reflects a
    # real comp pull under the post-audit valuation semantics (audit H2).
    from datetime import date as _date, timedelta as _timedelta
    recent = (_date.today() - _timedelta(days=30)).isoformat()
    comps = [
        CompRecord(address="10030 Plainsman Ln", sale_price=755_000,
                   sale_date=recent, distance_miles=0.1, source="mls_agent", adjusted_price=750_000),
        CompRecord(address="10015 Plainsman Ln", sale_price=745_000,
                   sale_date=recent, distance_miles=0.15, source="mls_agent", adjusted_price=748_000),
        CompRecord(address="10040 Plainsman Ct", sale_price=760_000,
                   sale_date=recent, distance_miles=0.2, source="mls_agent", adjusted_price=755_000),
        CompRecord(address="10050 Plainsman Ct", sale_price=748_000,
                   sale_date=recent, distance_miles=0.25, source="mls_agent", adjusted_price=752_000),
        CompRecord(address="10060 Plainsman Dr", sale_price=752_000,
                   sale_date=recent, distance_miles=0.3, source="mls_agent", adjusted_price=750_000),
    ]
    gate, val_obj = gate_val(cpr, "mls_agent", comps, BlastTier.T3)
    cpr = attach_valuation(cpr, val_obj, gate)
    print(f"  GATE-VAL: {gate.verdict} — {gate.reason}")
    if val_obj.arv:
        print(f"  ARV: ${val_obj.arv.value:,.0f} (confidence: {val_obj.arv.confidence:.0%})")
    print(f"  Column: {cpr.column.value}")

    # F9: wire the orphaned weekly digest now that the run's dataset ([cpr])
    # exists — regardless of the GATE-VAL verdict below. Fixture namespace
    # only (fixture_ prefix, data/fixtures/): production digest files stay
    # reserved for live runs.
    digest_path = write_weekly_digest([cpr], output_dir,
                                      filename_prefix="fixture_")
    print(f"  Weekly digest: {digest_path}")

    if gate.verdict != "pass":
        # Fixture demo must still produce artifacts (HITL is an honest outcome).
        print("\n  ⚠️ GATE-VAL not passed — routing to HITL (fixture artifacts still written).")
        cpr.dead_reason = cpr.dead_reason or f"gate_val_{gate.verdict}"
        from src.deal_memo import render_memo
        memo = render_memo(cpr, None, None, review=None)
        write_text(os.path.join(output_dir, "fixture_deal_memo_4003592.md"), memo)
        print(f"  Saved: {os.path.join(output_dir, 'fixture_deal_memo_4003592.md')}")
        return

    result = process_valued_cpr(cpr, output_dir=output_dir,
                                artifact_prefix="fixture_",
                                summary=summary, source_label=summary_source,
                                gate_val_result=gate)

    # Synthesis: portfolio stats + GeoJSON export
    from src.synthesis import portfolio_stats, render_stats_report
    from src.adapters.geojson_export import cpr_to_geojson

    stats = portfolio_stats([cpr])
    stats_report = render_stats_report(stats)
    stats_path = write_text(os.path.join(output_dir, "fixture_portfolio_stats.md"), stats_report)

    geojson_payload = cpr_to_geojson([cpr])
    geojson_path = write_json(os.path.join(output_dir, "fixture_foreclosures.geojson"),
                              geojson_payload, default=str)
    n_features = len(geojson_payload["features"])

    print(f"\n  Saved: {stats_path}")
    print(f"  Saved: {geojson_path} ({n_features} features)")
    print("\n" + "=" * 70)
    print("  PIPELINE RUN COMPLETE")
    print("=" * 70)


def _print_grayson_degradation(exc: BaseException | None) -> None:
    """F10: loud, honest notice that Grayson tax-sale sources are unreachable.

    The adapter swallows transport errors and returns [] — an empty pull from
    THESE verified-dead endpoints is the same degradation as a raised one.
    """
    cause = f" ({exc!r})" if exc is not None else ""
    print(f"!! GRAYSON DEGRADED: tax-sale sources unreachable{cause}")
    print("!! Known-dead endpoints (verified DNS NXDOMAIN / 403) — continuing "
          "with zero grayson leads.")
    print("!! Pass --force-dead-errors to propagate this failure instead.")


LIVE_SOURCE_NAMES = {
    "collin": "collin_foreclosure",
    "dallas": "dallas_foreclosure",
    "grayson": "grayson_tax_sale",
}


def run_live_pipeline(county: str, limit: int, allow_gate_fail_demo: bool = False,
                      min_leads: int = 0, force_dead_errors: bool = False,
                      summary: RunSummary | None = None,
                      output_dir: str | None = None) -> None:
    """Run the pipeline on live data from the specified county.

    Opt-in only (--live). Outputs keep production names and are written through
    the atomic writers. GATE-LEGAL-LIEN fail/hitl across the batch exits nonzero
    (2, or 3 when caused by a lookup error) unless --allow-gate-fail-demo is set.

    RL2 (--min-leads N): when the scrape returns fewer leads than the floor,
    exit 4 behind a loud PORTAL-SHAPE-CHANGED banner — a near-empty pull
    usually means the portal HTML changed, not an empty market.

    F10 (--county grayson): dispatches through GraysonTaxAdapter whose known
    endpoints are verified-dead; fetch failures print a degradation notice and
    complete with zero leads (exit 0) unless *force_dead_errors*
    (--force-dead-errors) propagates them. No new scraping promises here.
    """
    print(f"\n{'=' * 70}")
    print(f"  HERMES-REO PIPELINE — LIVE RUN ({county.upper()}, limit={limit})")
    print(f"{'=' * 70}")

    source_name = LIVE_SOURCE_NAMES.get(county, f"{county}_source")

    # Step 1: Source
    print(f"\n[1/7] SOURCE — {county.title()} County foreclosure notices")
    if county == "collin":
        from src.adapters.collin_foreclosure import CollinForeclosureAdapter
        adapter = CollinForeclosureAdapter()
        leads = adapter.fetch(limit=limit, extract_details=True)
    elif county == "dallas":
        from src.adapters.dallas_foreclosure import DallasForeclosureAdapter
        adapter = DallasForeclosureAdapter()
        leads = adapter.fetch(limit=limit)
    elif county == "grayson":
        # F10: register the implemented-but-unwired Grayson tax-sale adapter
        # (src/adapters/grayson_tax.py). Its endpoints are dead by prior
        # verification (DNS NXDOMAIN / 403) — this wiring adds honest
        # degradation, not new scraping promises.
        from src.adapters.grayson_tax import GraysonTaxAdapter
        adapter = GraysonTaxAdapter()
        try:
            leads = adapter.fetch(limit=limit)
        except Exception as exc:
            if force_dead_errors:
                raise
            _print_grayson_degradation(exc)
            leads = []
        else:
            if not leads:
                _print_grayson_degradation(None)
    else:
        print(f"  No adapter for {county} yet — use --fixture or --live --county "
              f"collin|dallas|grayson")
        return

    if summary is not None:
        summary.bump(source_name, "leads", len(leads))

    print(f"  Sourced {len(leads)} leads")

    # RL2: fail loudly when the scrape shrank below the operator's floor.
    if min_leads and len(leads) < min_leads:
        print("\n" + "!" * 70)
        print("!! PORTAL-SHAPE-CHANGED — LIVE SCRAPE BELOW --min-leads FLOOR")
        print(f"!! {county}: {len(leads)} lead(s) found vs floor of {min_leads}.")
        print("!! A near-empty pull usually means the portal HTML changed or")
        print("!! the request was blocked — NOT that no properties exist.")
        print("!! Investigate the portal before trusting this run's emptiness.")
        print("!" * 70)
        sys.exit(exit_code_for("min_leads"))

    if not leads:
        print("  No leads found — pipeline complete.")
        return

    # Process each lead through the spine
    from src.enrichment import enrich_lead
    from src.cpr import ValuationObject

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data")
    gate_blocks = []  # (parcel_id, verdict, lookup_caused)
    processed_cprs = []  # feeds the weekly digest (F9)

    for i, lead in enumerate(leads, 1):
        print(f"\n--- Parcel {i}/{len(leads)}: {lead.situs_address} ---")
        try:
            # Enrich
            cpr = enrich_lead(lead)
            cpr.lien_lookup_status = "not_run"  # unknown ≠ clean title (audit S7)
            print(f"  Enriched: {cpr.situs_address} | Column: {cpr.column.value}")

            # Clerk lien search (Collin only)
            if cpr.county == "collin" and cpr.owner_name and cpr.owner_name.value:
                _clerk_lien_search(cpr)

            # DCAD cross-reference (Dallas only)
            if cpr.county == "dallas":
                cpr = _dcad_xref(cpr)

            # Lien gate
            lien_gate, lookup_caused = _effective_lien_gate(cpr)
            print(f"  GATE-LEGAL-LIEN: {lien_gate.verdict} — {lien_gate.reason[:80]}")
            if lien_gate.verdict != "pass":
                gate_blocks.append((cpr.parcel_id, lien_gate.verdict, lookup_caused))

            # Valuation (using CAD as a weak baseline — in production, use MLS HITL).
            # CAD-only valuation will fail GATE-VAL — this is by design: the system
            # correctly refuses to emit an ARV without approved comp provenance.
            cpr.valuation = ValuationObject(
                comp_source="assessed_value",  # will be rejected
                gate_val_verdict="fail",
                gate_val_reason="Assessed-value-only — route to MLS/appraiser HITL",
            )
            print("  GATE-VAL: fail (assessed-value-only — HITL required for real ARV)")
            print("  → Deal routed to HITL for MLS comp pull")

            # Prod-named CPR dump via atomic writer (live path keeps production names)
            cpr_path = write_json(os.path.join(output_dir, f"cpr_{cpr.parcel_id}.json"),
                                  cpr.to_dict(), default=str)
            print(f"  Saved: {cpr_path}")

            processed_cprs.append(cpr)
            if summary is not None:
                summary.bump(source_name, "parsed")
                summary.bump(source_name, "enriched")
        except Exception as exc:
            # RL4: one broken parcel must not erase the rest of the batch —
            # tally it loudly and keep going so counts can show partial
            # degradation instead of a traceback erasing the run.
            if summary is not None:
                summary.bump(source_name, "errors")
            print(f"PARCEL-FAILED parcel {lead.parcel_id}: {exc!r}", file=sys.stderr)

    # F9: weekly digest over whatever this run actually produced.
    if processed_cprs:
        digest_path = write_weekly_digest(processed_cprs, output_dir)
        print(f"  Weekly digest: {digest_path}")

    print(f"\n{'=' * 70}")
    print(f"  LIVE RUN COMPLETE — {len(processed_cprs)}/{len(leads)} parcels processed")
    print("  All require MLS/appraiser HITL for valuation (non-disclosure state)")
    print(f"{'=' * 70}")

    if gate_blocks:
        lookup_caused_any = any(lc for _, _, lc in gate_blocks)
        print("\n" + "!" * 70)
        print(f"!! GATE-LEGAL-LIEN blocked {len(gate_blocks)}/{len(leads)} live parcels:")
        for parcel_id, verdict, lc in gate_blocks:
            cause = " (lookup error)" if lc else ""
            print(f"!!   parcel {parcel_id}: {verdict}{cause}")
        print("!" * 70)
        if not allow_gate_fail_demo:
            print("!! Re-run with --allow-gate-fail-demo to suppress this failure (demo only).")
            sys.exit(_gate_exit_code(lookup_caused_any))
        print("!! DEMO OVERRIDE (--allow-gate-fail-demo): exiting 0 despite gate blocks.")


# B2: DCAD-derived CPR attributes → flat current_deals.json keys, for the
# write-back below.
DCAD_WRITEBACK_FIELDS = (
    ("improvement_sqft", "sqft"),
    ("beds", "beds"),
    ("baths", "baths"),
    ("year_built", "year_built"),
    ("cad_appraised_value", "cad_value"),
)

# RL4: run-summary source name for this mode's counts.
DCAD_SUMMARY_SOURCE = "dcad_bulk"


def _write_back_dcad_fields(records: list[dict], dallas_cprs: list[CPR],
                            pre_xref: dict[int, dict]) -> tuple[int, int]:
    """B2: merge this run's DCAD enrichment into the parsed dataset records.

    For each CPR enriched THIS RUN (value changed vs the pre-xref snapshot),
    fill the matching record's flat key only where it is currently falsy —
    fresher scraped data is never clobbered. Records are mutated in place and
    the caller rewrites the whole file atomically, so unknown keys survive.
    Returns (records_updated, fields_written).
    """
    from src.cpr import pv_value
    from src.enrichment import normalize_deal_identity

    rec_index = {
        normalize_deal_identity(r.get("county"), r.get("address")): r
        for r in records
    }
    records_updated = fields_written = 0
    for cpr in dallas_cprs:
        target = rec_index.get(
            normalize_deal_identity(cpr.county, cpr.situs_address))
        if target is None:
            continue
        touched = False
        for attr, flat_key in DCAD_WRITEBACK_FIELDS:
            new_val = pv_value(getattr(cpr, attr, None))
            # Only fields actually enriched this run, into falsy slots only.
            if new_val and new_val != pre_xref[id(cpr)][attr] \
                    and not target.get(flat_key):
                target[flat_key] = new_val
                fields_written += 1
                touched = True
        if touched:
            records_updated += 1
    return records_updated, fields_written


def _run_bulk_dcad(summary: RunSummary | None = None) -> None:
    """Run DCAD bulk cross-reference on all Dallas CPRs from current_deals.json.

    Uses the batch adapter with JSON cache (data/dcad_cache.json) and 2s rate limiting.
    Estimated runtime: ~2s per address × ~165 Dallas records ≈ 5-6 minutes.

    AUDIT D1 fix: current_deals.json rows are FLAT (address/parcel_id/sqft/…),
    so Dallas rows are enriched through enrich_from_flat_record — the legacy
    enrich_from_raw_record expects the old nested details schema and used to
    resolve every row to "Unknown", getting filtered out (silent no-op).
    Enriched DCAD fields are written back into current_deals.json atomically
    so find_deals.py's merge-on-write keeps them alive across regenerations.

    RL4: when *summary* is passed, counts land under the 'dcad_bulk' source —
    leads = Dallas rows scanned, parsed = usable CPRs built, mismatch =
    unparseable/malformed rows, no_data_found = lookups that yielded nothing,
    enriched = CPRs updated, written_back = dataset records touched.
    """
    print(f"\n{'=' * 70}")
    print("  HERMES-REO PIPELINE — DCAD BULK CROSS-REFERENCE")
    print(f"{'=' * 70}")

    # Load the current deals dataset (flat schema)
    data_path = os.path.join(os.path.dirname(__file__), "data", "current_deals.json")
    if not os.path.exists(data_path):
        print(f"  No dataset found at {data_path}")
        print("  Run find_deals.py first to generate current_deals.json")
        return

    with open(data_path) as f:
        records = json.load(f)

    # Enrich Dallas records into CPRs
    from src.cpr import pv_value
    from src.enrichment import enrich_from_flat_record
    from src.adapters.dallas_cad_bulk import cross_reference_dcad_bulk

    # Query-sanity guard (Wave 3 ops): legacy PDF-scrape artifacts include
    # legal-prose fragments and out-of-state strings ("60 foot right-of-way",
    # "Virginia Beach"). DCAD lookups for those burn politeness budget and
    # always fail echo-back; require street-number + words + TX zip shape.
    _us_street_tx = re.compile(
        r"^\d{1,6}\s+[A-Za-z].*\bTX\b\s*\d{5}", re.IGNORECASE)
    dallas_cprs, skipped_invalid, dallas_rows = [], 0, 0
    for record in records:
        if record.get("county", "").lower() == "dallas":
            dallas_rows += 1
            # D1 FIX: flat schema → flat-record enricher (nested reader no-ops)
            cpr = enrich_from_flat_record(record)
            if not (cpr and cpr.situs_address and cpr.situs_address != "Unknown"):
                continue
            if not _us_street_tx.match(cpr.situs_address.strip()):
                skipped_invalid += 1
                continue
            dallas_cprs.append(cpr)
    if skipped_invalid:
        print(f"  Skipped {skipped_invalid} records with malformed/out-of-state "
              f"addresses (legacy scrape artifacts)")
    if summary is not None:
        summary.bump(DCAD_SUMMARY_SOURCE, "leads", dallas_rows)
        summary.bump(DCAD_SUMMARY_SOURCE, "parsed", len(dallas_cprs))
        summary.bump(DCAD_SUMMARY_SOURCE, "mismatch",
                     dallas_rows - len(dallas_cprs))

    print(f"  Dallas CPRs to cross-reference: {len(dallas_cprs)}")
    if not dallas_cprs:
        print("  No Dallas CPRs found — nothing to do.")
        return

    # Pre-xref snapshot of the DCAD-derivable fields, so write-back touches
    # only values THIS run actually enriched.
    pre_xref = {
        id(cpr): {attr: pv_value(getattr(cpr, attr, None))
                  for attr, _ in DCAD_WRITEBACK_FIELDS}
        for cpr in dallas_cprs
    }

    # Run bulk cross-reference (limit 50 per run to avoid long sessions)
    updated = cross_reference_dcad_bulk(dallas_cprs, batch_limit=50)
    print(f"  CPRs updated with DCAD data: {updated}")
    print("  Cache saved to: data/dcad_cache.json")
    if summary is not None:
        summary.bump(DCAD_SUMMARY_SOURCE, "no_data_found",
                     len(dallas_cprs) - updated)
        summary.bump(DCAD_SUMMARY_SOURCE, "enriched", updated)

    # B2: write the enriched DCAD fields back into current_deals.json
    # atomically (unknown keys preserved; only falsy slots filled).
    records_updated, fields_written = _write_back_dcad_fields(
        records, dallas_cprs, pre_xref)
    if fields_written:
        write_json(data_path, records)
        print(f"  Write-back: {fields_written} field(s) into "
              f"{records_updated} record(s) → {data_path} (atomic)")
    else:
        print("  Write-back: nothing new to persist")
    if summary is not None:
        summary.bump(DCAD_SUMMARY_SOURCE, "written_back", records_updated)

    print(f"\n{'=' * 70}")
    print(f"  DCAD BULK COMPLETE — {updated} enriched, "
          f"{len(dallas_cprs) - updated} skipped/failed, "
          f"{fields_written} field(s) written back")
    print(f"{'=' * 70}")


GEOCODE_BACKFILL_CAP = 25  # hard cap: addresses geocoded per invocation
GEOCODE_LEDGER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "data", "geocode_failures.json")
GEOCODE_MAX_ATTEMPTS = 3      # skip an address after this many failures...
GEOCODE_FAILURE_TTL_DAYS = 7  # ...until its newest failure goes stale
# RL4 summary source name for this mode's counts.
GEOCODE_SUMMARY_SOURCE = "census_geocode"


def _parse_ledger_ts(value: object) -> datetime | None:
    """Parse a ledger ISO timestamp; naive values are read as UTC."""
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def geocode_failure_key(address: str) -> str:
    """Negative-cache key: sha1[:16] of the normalized census query.

    Falls back to hashing the raw string when normalization rejects it, so
    implausible addresses still get a stable identity.
    """
    query = to_census_one_line(address) or (address or "")
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]


def load_geocode_failures(path: str = GEOCODE_LEDGER_PATH) -> dict[str, dict]:
    """Load the failure ledger tolerantly: missing/corrupt -> fresh {}."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        print(f"  [geocode-ledger] unreadable at {path} — starting fresh")
        return {}
    if not isinstance(raw, dict):
        print(f"  [geocode-ledger] unexpected shape at {path} — starting fresh")
        return {}
    ledger: dict[str, dict] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue  # tolerate junk entries instead of failing the run
        attempts = entry.get("attempts")
        ledger[str(key)] = {
            "attempts": attempts if isinstance(attempts, int) else 0,
            "last_status": str(entry.get("last_status", "")),
            "last_at": str(entry.get("last_at", "")),
        }
    return ledger


def record_geocode_failure(ledger: dict[str, dict], address: str, status: str,
                           now_fn: Callable[[], datetime] = _utc_now) -> None:
    """Bump the failure tally for *address* in place (mutates *ledger*)."""
    entry = ledger.setdefault(geocode_failure_key(address), {"attempts": 0})
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["last_status"] = status
    entry["last_at"] = now_fn().isoformat()


def clear_geocode_failure(ledger: dict[str, dict], address: str) -> None:
    """A successful geocode forgives: drop the address from the ledger."""
    ledger.pop(geocode_failure_key(address), None)


def should_skip_geocode(entry: dict | None, retry_failed: bool = False,
                        now_fn: Callable[[], datetime] = _utc_now) -> bool:
    """True when a previously-failed address must sit this invocation out.

    Skip iff attempts >= GEOCODE_MAX_ATTEMPTS AND the newest failure is
    younger than GEOCODE_FAILURE_TTL_DAYS. --retry-failed overrides; a stale
    or unreadable last_at expires the entry (address becomes retryable).
    """
    if retry_failed or not entry:
        return False
    attempts = entry.get("attempts") or 0
    if attempts < GEOCODE_MAX_ATTEMPTS:
        return False
    last_at = _parse_ledger_ts(entry.get("last_at"))
    if last_at is None:
        return False
    age = now_fn() - last_at
    return timedelta(0) <= age < timedelta(days=GEOCODE_FAILURE_TTL_DAYS)


def select_geocode_targets(missing_records: list[dict],
                           ledger: dict[str, dict],
                           cap: int = GEOCODE_BACKFILL_CAP,
                           retry_failed: bool = False,
                           now_fn: Callable[[], datetime] = _utc_now
                           ) -> tuple[list[dict], dict]:
    """Partition missing-coordinate records into this run's geocode targets.

    Implausible addresses are never queried; previously-failed addresses
    inside the TTL window are skipped unless --retry-failed. Skip counts
    cover ALL candidates (not just those under *cap*); only the first *cap*
    survivors are returned.
    """
    stats = {"skipped_previously_failed": 0, "skipped_implausible": 0}
    eligible: list[dict] = []
    for rec in missing_records:
        address = rec.get("address", "")
        if not is_plausible_street_address(address):
            stats["skipped_implausible"] += 1
            continue
        if should_skip_geocode(ledger.get(geocode_failure_key(address)),
                               retry_failed=retry_failed, now_fn=now_fn):
            stats["skipped_previously_failed"] += 1
            continue
        eligible.append(rec)
    return eligible[:cap], stats


def _record_missing_coords(record: dict) -> bool:
    """True when latitude/longitude are absent, unparsable, or zero."""
    def _num(key: str):
        val = record.get(key)
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    lat, lng = _num("latitude"), _num("longitude")
    return not (lat and lng)  # catches None, 0.0, and unparsable junk


def _run_geocode_backfill(dry_run: bool = False,
                          cap: int = GEOCODE_BACKFILL_CAP,
                          retry_failed: bool = False,
                          summary: RunSummary | None = None) -> None:
    """B3: backfill missing coordinates in current_deals.json via Census.

    Strictly OPT-IN (--geocode-backfill): performs network calls against
    src.adapters.census.geocode_address at the adapter rate limit
    (GEOCODE_RATE_LIMIT_S), hard-capped at *cap* addresses per invocation.
    Results are written back into the dataset atomically like the DCAD
    write-back (only falsy slots filled, unknown keys preserved).
    --dry-run prints what WOULD be geocoded and performs ZERO network calls;
    without --geocode-backfill this code path is never reached at all.

    Negative caching: outbound queries go through src.addr.to_census_one_line;
    implausible addresses are never queried. Census failures (no-match /
    timeout / error) are tallied in data/geocode_failures.json (atomic write
    after each batch); addresses with >= GEOCODE_MAX_ATTEMPTS failures within
    GEOCODE_FAILURE_TTL_DAYS are skipped unless *retry_failed* (--retry-failed).
    Success removes the address from the ledger.

    RL4: when *summary* is passed, counts land under 'census_geocode' —
    leads = records missing coords, parsed = selected targets, mismatch =
    implausible addresses, no_data_found = failed lookups, enriched =
    successful geocodes, written_back = rows persisted to the dataset.
    """
    print(f"\n{'=' * 70}")
    print(f"  HERMES-REO PIPELINE — CENSUS GEOCODE BACKFILL"
          f"{' (DRY RUN)' if dry_run else ''}")
    print(f"{'=' * 70}")

    data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "current_deals.json")
    if not os.path.exists(data_path):
        print(f"  No dataset found at {data_path}")
        print("  Run find_deals.py first to generate current_deals.json")
        return

    with open(data_path) as f:
        records = json.load(f)

    missing = [
        rec for rec in records
        if rec.get("address") and rec.get("address") != "Unknown"
        and _record_missing_coords(rec)
    ]
    ledger = load_geocode_failures()
    targets, skip_stats = select_geocode_targets(missing, ledger, cap=cap,
                                                 retry_failed=retry_failed)

    print(f"  Records in dataset: {len(records)}")
    print(f"  Missing coordinates (geocodable address): {len(missing)}")
    print(f"  Skipped, previously failed <{GEOCODE_FAILURE_TTL_DAYS}d: "
          f"{skip_stats['skipped_previously_failed']}"
          f"{' (--retry-failed: retrying anyway)' if retry_failed else ''}")
    print(f"  Skipped, implausible address (never queried): "
          f"{skip_stats['skipped_implausible']}")
    print(f"  Selected this run (hard cap {cap}): {len(targets)}")

    if summary is not None:
        summary.bump(GEOCODE_SUMMARY_SOURCE, "leads", len(missing))
        summary.bump(GEOCODE_SUMMARY_SOURCE, "parsed", len(targets))
        summary.bump(GEOCODE_SUMMARY_SOURCE, "mismatch",
                     skip_stats["skipped_implausible"])

    if dry_run:
        for rec in targets:
            print(f"    [dry-run] would geocode: {rec['address']}")
        print(f"  DRY RUN complete — {len(targets)} of {len(missing)} candidate(s) "
              f"would be geocoded "
              f"({skip_stats['skipped_previously_failed']} previously-failed "
              f"skipped); ZERO network performed, nothing written.")
        return

    if not targets:
        if missing:
            print(f"  Nothing to geocode — {len(missing)} candidate(s) remain but all are "
                  f"ledger-skipped ({skip_stats['skipped_previously_failed']}) or implausible "
                  f"({skip_stats['skipped_implausible']}). Use --retry-failed to force.")
        else:
            print("  Nothing to geocode — every record already has coordinates.")
        return

    from src.adapters.census import geocode_address, GEOCODE_RATE_LIMIT_S

    succeeded = failed = 0
    failure_reasons: dict[str, int] = {}
    ledger_dirty = False
    calls_made = 0
    for n, rec in enumerate(targets, 1):
        address = rec["address"]
        query = to_census_one_line(address)
        if not query:
            # Defensive: selection already filtered implausible addresses.
            failed += 1
            failure_reasons["Invalid Address"] = \
                failure_reasons.get("Invalid Address", 0) + 1
            record_geocode_failure(ledger, address, "Invalid Address")
            ledger_dirty = True
            if summary is not None:
                summary.bump(GEOCODE_SUMMARY_SOURCE, "no_data_found")
            print(f"  [{n}/{len(targets)}] ✗ {address} → Invalid Address")
            continue
        if calls_made:
            time.sleep(GEOCODE_RATE_LIMIT_S)  # adapter rate limit between calls
        result = geocode_address(query)
        calls_made += 1
        status = str(result.get("status", "UnknownStatus"))
        if result.get("status") == "Success" \
                and result.get("latitude") and result.get("longitude"):
            rec["latitude"] = float(result["latitude"])
            rec["longitude"] = float(result["longitude"])
            # Free census context rides along; fill only currently-falsy slots.
            if not rec.get("census_tract") and result.get("census_tract"):
                rec["census_tract"] = result["census_tract"]
            if not rec.get("county_fips") and result.get("county_fips"):
                rec["county_fips"] = result["county_fips"]
            clear_geocode_failure(ledger, address)  # success forgives
            ledger_dirty = True
            succeeded += 1
            if summary is not None:
                summary.bump(GEOCODE_SUMMARY_SOURCE, "enriched")
            print(f"  [{n}/{len(targets)}] ✓ {address} → "
                  f"({result['latitude']:.5f}, {result['longitude']:.5f})")
        else:
            failed += 1
            failure_reasons[status] = failure_reasons.get(status, 0) + 1
            record_geocode_failure(ledger, address, status)
            ledger_dirty = True
            if summary is not None:
                summary.bump(GEOCODE_SUMMARY_SOURCE, "no_data_found")
            print(f"  [{n}/{len(targets)}] ✗ {address} → {status}")

    if ledger_dirty:
        write_json(GEOCODE_LEDGER_PATH, ledger)  # negative cache (atomic)
        print(f"  Failure ledger: {len(ledger)} entrie(s) → "
              f"{GEOCODE_LEDGER_PATH} (atomic)")

    if succeeded:
        write_json(data_path, records)  # atomic write-back (B2 pattern)
        print(f"  Write-back: {succeeded} record(s) geocoded → {data_path} (atomic)")
        if summary is not None:
            summary.bump(GEOCODE_SUMMARY_SOURCE, "written_back", succeeded)
    else:
        print("  Write-back: nothing succeeded — dataset left untouched")

    print(f"  Geocode backfill done: {succeeded} ok, {failed} failed "
          f"(cap {cap})")
    if failure_reasons:
        tally = ", ".join(f"{reason}: {count}"
                          for reason, count in sorted(failure_reasons.items()))
        print(f"  Failure tally: {tally}")


def _mode_identity(args: argparse.Namespace) -> tuple[str, dict]:
    """Map parsed CLI args to (run_summary mode, flags payload).

    RL4: keeps the summary's mode/flags vocabulary in one place; values are
    JSON-serializable by construction.
    """
    if getattr(args, "resume_valued", None):
        return "resume", {"valued_file": os.path.basename(args.resume_valued)}
    if getattr(args, "resume_valued_dir", None):
        return "resume", {"valued_dir":
                          os.path.basename(os.path.abspath(args.resume_valued_dir))}
    if getattr(args, "geocode_backfill", False):
        return "geocode", {"dry_run": bool(getattr(args, "dry_run", False)),
                           "retry_failed": bool(getattr(args, "retry_failed", False))}
    if getattr(args, "bulk_dcad", False):
        return "bulk-dcad", {}
    if getattr(args, "fixture", False):
        return "fixture", {"allow_gate_fail_demo":
                           bool(getattr(args, "allow_gate_fail_demo", False))}
    if getattr(args, "live", False):
        return "live", {"county": args.county, "limit": args.limit,
                        "min_leads": getattr(args, "min_leads", 0),
                        "force_dead_errors":
                            bool(getattr(args, "force_dead_errors", False)),
                        "allow_gate_fail_demo":
                            bool(getattr(args, "allow_gate_fail_demo", False))}
    return "hint", {}  # bare invocation (handled before this in main())


def main() -> None:
    # Library modules (src/**) log via logging; surface on stderr.
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="HERMES-REO Pipeline Runner",
        epilog="Exit codes: 0 ok · 1 usage/data-missing · 2 gate-fail · "
               "3 lookup-error · 4 below --min-leads — full catalog: "
               "docs/EXIT_CODES.md")
    parser.add_argument("--county", default="collin", choices=["collin", "dallas", "grayson"])
    parser.add_argument("--limit", type=int, default=1, help="Max records to process")
    parser.add_argument("--fixture", action="store_true", help="Use fixture data; writes data/fixtures/")
    parser.add_argument("--all-checks", action="store_true", help="Run all module self-checks first")
    parser.add_argument("--checks-only", action="store_true",
                        help="Run module self-checks and exit — zero network, no scraping")
    parser.add_argument("--live", action="store_true",
                        help="OPT-IN: enable live portal scraping (network). Required for the live branch.")
    parser.add_argument("--bulk-dcad", action="store_true", help="Run DCAD bulk cross-reference on all Dallas CPRs")
    parser.add_argument("--geocode-backfill", action="store_true",
                        help="OPT-IN network op: Census-geocode current_deals.json records "
                             "missing lat/lng (rate-limited, hard cap 25 per invocation)")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --geocode-backfill: print the plan without any network call")
    parser.add_argument("--retry-failed", action="store_true",
                        help="With --geocode-backfill: retry addresses even when the failure "
                             "ledger would skip them (>=3 failures within 7 days)")
    parser.add_argument("--allow-gate-fail-demo", action="store_true",
                        help="Continue past GATE-LEGAL-LIEN fail/hitl (demo only — loud banner printed)")
    parser.add_argument("--resume-valued", metavar="VALUED_JSON",
                        help="F1: finish one HITL-valued CPR (*.valued.json from "
                             "`python -m src.hitl`): underwrite → price → review → "
                             "memo → offer into data/ production naming; stamps "
                             "val_gate=pass into current_deals.json on address match")
    parser.add_argument("--resume-valued-dir", metavar="VALUED_DIR",
                        help="F2: loop --resume-valued over every *.valued.json "
                             "in DIR (sorted, one bad file never stops the "
                             "batch); prints aggregate {resumed, "
                             "skipped_not_pass, errors}; exits 0 if all "
                             "processed")
    parser.add_argument("--min-leads", type=int, default=0,
                        help="RL2: with --live, floor on leads found — exit 4 behind a "
                             "PORTAL-SHAPE-CHANGED banner when below it (0 disables)")
    parser.add_argument("--force-dead-errors", action="store_true",
                        help="F10: with --live --county grayson, propagate known-dead-endpoint "
                             "fetch failures instead of degrading to zero leads")
    args = parser.parse_args()

    mode_selected = args.live or args.fixture or args.bulk_dcad \
        or args.geocode_backfill or bool(args.resume_valued) \
        or bool(args.resume_valued_dir)
    checks_requested = args.all_checks or args.checks_only

    # --checks-only always means checks-and-exit. When checks were requested but
    # no data mode was selected, show the hint and stop after checks: ZERO network.
    if args.checks_only or (checks_requested and not mode_selected):
        if not mode_selected:
            print(MODE_HINT)
            print()
        print("Running all module self-checks...")
        ok = run_all_checks()
        print("\n✓ All checks passed." if ok else "\n✗ Some checks failed — fix before proceeding.")
        sys.exit(0 if ok else 1)

    if args.all_checks:
        print("Running all module self-checks...")
        if not run_all_checks():
            print("\n✗ Some checks failed — fix before proceeding.")
            sys.exit(1)
        print("✓ All checks passed.\n")

    mode_name, flags = _mode_identity(args)
    if mode_name == "hint":
        # Bare invocation (or flags that select no mode): hint only, zero
        # network, no data mode ran → no run summary (RL4 covers modes).
        print(MODE_HINT)
        sys.exit(0)

    # RL4: one structured summary per invocation, written even when the mode
    # exits nonzero (gate fail / min-leads floor raise SystemExit through the
    # finally below) — that is exactly when an operator needs the counts.
    summary = RunSummary(mode=mode_name, flags=flags)
    try:
        if args.resume_valued:
            sys.exit(run_resume_valued_pipeline(args.resume_valued,
                                                summary=summary))
        elif args.resume_valued_dir:
            sys.exit(run_resume_valued_dir_pipeline(args.resume_valued_dir,
                                                    summary=summary))
        elif args.geocode_backfill:
            # B3: unreachable unless the flag is passed — no network otherwise.
            _run_geocode_backfill(dry_run=args.dry_run,
                                  retry_failed=args.retry_failed,
                                  summary=summary)
        elif args.bulk_dcad:
            _run_bulk_dcad(summary=summary)
        elif args.fixture:
            run_fixture_pipeline(allow_gate_fail_demo=args.allow_gate_fail_demo,
                                 summary=summary)
        elif args.live:
            run_live_pipeline(args.county, args.limit,
                              allow_gate_fail_demo=args.allow_gate_fail_demo,
                              min_leads=args.min_leads,
                              force_dead_errors=args.force_dead_errors,
                              summary=summary)
    finally:
        summary.write()


if __name__ == "__main__":
    main()
