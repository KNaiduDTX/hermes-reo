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
#
# Live scraping is strictly opt-in via --live. With no data mode selected the
# runner prints a hint and exits 0 without touching the network.
from __future__ import annotations
import argparse
import re
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import logging

# Ensure src is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.io_utils import write_json, write_text  # atomic crash-safe writers (shared contract)
from src import policy  # B4: single source of truth for decision-policy constants

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

  Live scraping is opt-in: pass --live explicitly."""


def run_all_checks() -> bool:
    """Run all module self-checks concurrently. Returns True if all pass."""
    def _check(mod: str):
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


def _clerk_lien_search(cpr) -> None:
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


def _dcad_xref(cpr):
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


def _effective_lien_gate(cpr):
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
    return 3 if lookup_caused else 2


def _print_gate_block_banner(verdict: str, reason: str, lookup_caused: bool) -> None:
    print("\n" + "!" * 70)
    print(f"!! GATE-LEGAL-LIEN {verdict.upper()} — pricing/bidding BLOCKED")
    print(f"!! Reason: {reason}")
    if lookup_caused:
        print("!! Cause: lien LOOKUP ERROR (not clean title — see CLERK-LOOKUP-FAILED above)")
    print("!" * 70)


def run_fixture_pipeline(allow_gate_fail_demo: bool = False):
    """Run the full pipeline on fixture data.

    Uses the verified Collin County parcel from lucid-noether (entry_id 4003592).
    All outputs go to data/fixtures/ with a fixture_ prefix — fixture runs NEVER
    touch production-named files.
    """
    from src.cpr import CompRecord, BlastTier
    from src.enrichment import enrich_from_collin_details
    from src.adapters.base import NormalizedLead
    from src.valuation import gate_val, attach_valuation
    from src.underwriting import flip_pnl, mao
    from src.pricing import price_deal
    from src.deal_memo import render_memo
    from src.reviewer import run_review

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(repo_dir, "data", "fixtures")

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

    if gate.verdict != "pass":
        # Fixture demo must still produce artifacts (HITL is an honest outcome).
        print("\n  ⚠️ GATE-VAL not passed — routing to HITL (fixture artifacts still written).")
        cpr.dead_reason = cpr.dead_reason or f"gate_val_{gate.verdict}"
        memo = render_memo(cpr, None, None, review=None)
        write_text(os.path.join(output_dir, "fixture_deal_memo_4003592.md"), memo)
        print(f"  Saved: {os.path.join(output_dir, 'fixture_deal_memo_4003592.md')}")
        return

    # Step 5: Underwrite
    print("\n[5/7] UNDERWRITE — Fix-and-flip model")
    arv = val_obj.arv.value
    rehab = 50_000  # estimated rehab for a 2004 build, 4332 sqft
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
    pricing = price_deal(
        cpr, arv=arv, rehab=rehab, deal_type="DT-AUCTION",
        factor=policy.MAO_FACTOR_DEFAULT, capital_at_risk=uw.cash_invested,
    )
    print(f"  MAO: ${pricing.mao:,.0f}")
    print(f"  Bid ceiling: ${pricing.bid_ceiling:,.0f}")
    print(f"  Blast tier: {pricing.blast_tier.name}")
    print(f"  Offer ladder: open ${pricing.offer_ladder['opening']:,.0f} → target ${pricing.offer_ladder['target']:,.0f} → walk ${pricing.offer_ladder['walk_away']:,.0f}")
    print(f"  Optimistic-only flag: {pricing.optimistic_only_flag}")

    # Step 7: Reviewer — fake-signal harness
    print("\n[7/7] REVIEWER — AGENT-REV fake-signal harness")
    review = run_review(cpr, gate, pricing)
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
        offer_path = write_text(os.path.join(output_dir, "fixture_offer_letter_4003592.txt"),
                                offer.offer_letter)
        print(f"  Saved: {offer_path}")
    except Exception as e:
        print(f"  Negotiation skipped: {e!r}")

    # Save outputs — fixture namespace ONLY (never production-named files)
    memo_path = write_text(os.path.join(output_dir, "fixture_deal_memo_4003592.md"), memo)
    cpr_path = write_json(os.path.join(output_dir, "fixture_cpr_4003592.json"),
                          cpr.to_dict(), default=str)

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

    print(f"\n  Saved: {memo_path}")
    print(f"  Saved: {cpr_path}")
    print(f"  Saved: {stats_path}")
    print(f"  Saved: {geojson_path} ({n_features} features)")
    print("\n" + "=" * 70)
    print("  PIPELINE RUN COMPLETE")
    print("=" * 70)


def run_live_pipeline(county: str, limit: int, allow_gate_fail_demo: bool = False):
    """Run the pipeline on live data from the specified county.

    Opt-in only (--live). Outputs keep production names and are written through
    the atomic writers. GATE-LEGAL-LIEN fail/hitl across the batch exits nonzero
    (2, or 3 when caused by a lookup error) unless --allow-gate-fail-demo is set.
    """
    print(f"\n{'=' * 70}")
    print(f"  HERMES-REO PIPELINE — LIVE RUN ({county.upper()}, limit={limit})")
    print(f"{'=' * 70}")

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
    else:
        print(f"  No adapter for {county} yet — use --fixture or --live --county collin")
        return

    print(f"  Sourced {len(leads)} leads")
    if not leads:
        print("  No leads found — pipeline complete.")
        return

    # Process each lead through the spine
    from src.enrichment import enrich_lead
    from src.cpr import ValuationObject

    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    gate_blocks = []  # (parcel_id, verdict, lookup_caused)

    for i, lead in enumerate(leads, 1):
        print(f"\n--- Parcel {i}/{len(leads)}: {lead.situs_address} ---")

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

    print(f"\n{'=' * 70}")
    print(f"  LIVE RUN COMPLETE — {len(leads)} parcels processed")
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


def _write_back_dcad_fields(records: list[dict], dallas_cprs: list,
                            pre_xref: dict) -> tuple[int, int]:
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


def _run_bulk_dcad():
    """Run DCAD bulk cross-reference on all Dallas CPRs from current_deals.json.

    Uses the batch adapter with JSON cache (data/dcad_cache.json) and 2s rate limiting.
    Estimated runtime: ~2s per address × ~165 Dallas records ≈ 5-6 minutes.

    AUDIT D1 fix: current_deals.json rows are FLAT (address/parcel_id/sqft/…),
    so Dallas rows are enriched through enrich_from_flat_record — the legacy
    enrich_from_raw_record expects the old nested details schema and used to
    resolve every row to "Unknown", getting filtered out (silent no-op).
    Enriched DCAD fields are written back into current_deals.json atomically
    so find_deals.py's merge-on-write keeps them alive across regenerations.
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
    dallas_cprs, skipped_invalid = [], 0
    for record in records:
        if record.get("county", "").lower() == "dallas":
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

    print(f"\n{'=' * 70}")
    print(f"  DCAD BULK COMPLETE — {updated} enriched, "
          f"{len(dallas_cprs) - updated} skipped/failed, "
          f"{fields_written} field(s) written back")
    print(f"{'=' * 70}")


GEOCODE_BACKFILL_CAP = 25  # hard cap: addresses geocoded per invocation


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
                          cap: int = GEOCODE_BACKFILL_CAP):
    """B3: backfill missing coordinates in current_deals.json via Census.

    Strictly OPT-IN (--geocode-backfill): performs network calls against
    src.adapters.census.geocode_address at the adapter rate limit
    (GEOCODE_RATE_LIMIT_S), hard-capped at *cap* addresses per invocation.
    Results are written back into the dataset atomically like the DCAD
    write-back (only falsy slots filled, unknown keys preserved).
    --dry-run prints what WOULD be geocoded and performs ZERO network calls;
    without --geocode-backfill this code path is never reached at all.
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
    targets = missing[:cap]

    print(f"  Records in dataset: {len(records)}")
    print(f"  Missing coordinates (geocodable address): {len(missing)}")
    print(f"  Selected this run (hard cap {cap}): {len(targets)}")

    if dry_run:
        for rec in targets:
            print(f"    [dry-run] would geocode: {rec['address']}")
        print(f"  DRY RUN complete — {len(targets)} of {len(missing)} candidate(s) "
              f"would be geocoded; ZERO network performed, nothing written.")
        return

    if not targets:
        print("  Nothing to geocode — every record already has coordinates.")
        return

    from src.adapters.census import geocode_address, GEOCODE_RATE_LIMIT_S

    succeeded = failed = 0
    failure_reasons: dict[str, int] = {}
    for n, rec in enumerate(targets, 1):
        address = rec["address"]
        result = geocode_address(address)
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
            succeeded += 1
            print(f"  [{n}/{len(targets)}] ✓ {address} → "
                  f"({result['latitude']:.5f}, {result['longitude']:.5f})")
        else:
            failed += 1
            failure_reasons[status] = failure_reasons.get(status, 0) + 1
            print(f"  [{n}/{len(targets)}] ✗ {address} → {status}")
        if n < len(targets):
            time.sleep(GEOCODE_RATE_LIMIT_S)  # adapter rate limit between calls

    if succeeded:
        write_json(data_path, records)  # atomic write-back (B2 pattern)
        print(f"  Write-back: {succeeded} record(s) geocoded → {data_path} (atomic)")
    else:
        print("  Write-back: nothing succeeded — dataset left untouched")

    print(f"  Geocode backfill done: {succeeded} ok, {failed} failed "
          f"(cap {cap})")
    if failure_reasons:
        tally = ", ".join(f"{reason}: {count}"
                          for reason, count in sorted(failure_reasons.items()))
        print(f"  Failure tally: {tally}")


def main():
    # Library modules (src/**) log via logging; surface on stderr.
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="HERMES-REO Pipeline Runner")
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
    parser.add_argument("--allow-gate-fail-demo", action="store_true",
                        help="Continue past GATE-LEGAL-LIEN fail/hitl (demo only — loud banner printed)")
    args = parser.parse_args()

    mode_selected = args.live or args.fixture or args.bulk_dcad or args.geocode_backfill
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

    if args.geocode_backfill:
        # B3: unreachable unless the flag is passed — no network otherwise.
        _run_geocode_backfill(dry_run=args.dry_run)
    elif args.bulk_dcad:
        _run_bulk_dcad()
    elif args.fixture:
        run_fixture_pipeline(allow_gate_fail_demo=args.allow_gate_fail_demo)
    elif args.live:
        run_live_pipeline(args.county, args.limit, allow_gate_fail_demo=args.allow_gate_fail_demo)
    else:
        # Bare invocation (or flags that select no mode): hint only, zero network.
        print(MODE_HINT)
        sys.exit(0)


if __name__ == "__main__":
    main()
