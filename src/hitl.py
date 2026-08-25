# src/hitl.py
"""HITL comp intake — the human layer that lets GATE-VAL pass on real deals.

SYSTEM_REPORT section 11: with no MLS feed, GATE-VAL correctly fails every
real deal on assessed-value provenance and routes to HITL — but no tool
existed to actually perform that HITL step. This module is that tool:

  1. load a saved CPR dict (data/cpr_*.json or data/fixtures/fixture_cpr_*.json)
  2. validate + normalize a hand-collected comp set (agent MLS pull or BPO)
  3. re-run GATE-VAL against an approved source (mls_agent / appraiser_bpo)
  4. attach the valuation and advance the Kanban column exactly like the
     pipeline does, then atomically write the valued CPR back to disk.

CLI:
    python -m src.hitl --cpr data/cpr_4003592.json --comps comps.json [--out out.json]
    python -m src.hitl --self-check        (also runs bare, with no args)
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any

from .cpr import (
    BlastTier,
    CPR,
    CPRColumn,
    CompRecord,
    LienRecord,
    ProvenanceEntry,
    ProvenanceField,
    pv_value,
)
from .dates import parse_date_any
from .io_utils import write_json
from .valuation import GateValResult, ValuationObject, attach_valuation, gate_val

# HITL intake is restricted to the two approved human-verified channels.
ALLOWED_COMP_SOURCES = ("mls_agent", "appraiser_bpo")
DEFAULT_COMP_SOURCE = "mls_agent"

# Intake hard limits: beyond MAX the comp can never survive GATE-VAL's
# distance filter, so it is rejected at intake instead of silently dropped
# later; between WARN and MAX it is accepted but flagged.
MAX_INTAKE_DISTANCE_MI = 2.0
WARN_INTAKE_DISTANCE_MI = 1.0


def _warn(msg: str) -> None:
    print(f"hitl: WARNING {msg}", file=sys.stderr)


def coerce_blast_tier(value: Any, default: BlastTier = BlastTier.T2) -> BlastTier:
    """Map an int / str / BlastTier to BlastTier safely; fall back loudly.

    Saved CPR JSON stores blast_tier as its int value ("blast_tier": 3);
    hand-built dicts may carry "T2" or "t2". Anything unrecognized warns and
    returns *default* rather than raising — the tier is recoverable context,
    not worth crashing an intake run over.
    """
    if value is None:
        return default
    if isinstance(value, BlastTier):
        return value
    if isinstance(value, bool):  # bool is an int subclass — never a tier
        _warn(f"unrecognized blast_tier {value!r}; using {default.name}")
        return default
    try:
        return BlastTier(int(value))
    except (TypeError, ValueError):
        pass
    name = str(value).strip().upper()
    if name.isdigit():
        name = "T" + name
    try:
        return BlastTier[name]
    except KeyError:
        _warn(f"unrecognized blast_tier {value!r}; using {default.name}")
        return default


def _is_provenance_dict(d: Any) -> bool:
    """True for a serialized ProvenanceField ({value, source, ...})."""
    return isinstance(d, dict) and "value" in d and "source" in d


def _restore_provenance(d: dict) -> ProvenanceField:
    pf = ProvenanceField(
        value=d.get("value"),
        source=str(d.get("source") or ""),
        confidence=float(d.get("confidence") or 0.0),
    )
    if d.get("retrieved_at"):
        pf.retrieved_at = str(d["retrieved_at"])
    return pf


def load_cpr(path: str) -> CPR:
    """Load a saved CPR dict (cpr.to_dict() output) back into a CPR.

    Reconstructs enough state for a HITL valuation run: identity fields,
    provenance-wrapped fields (restored directly — no new ledger receipts for
    rehydration), the append-only ledger itself, liens via add_lien (receipted,
    chained onto the restored ledger) and pipeline state. The stored valuation
    is deliberately ignored: the HITL run replaces it wholesale.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a CPR dict at top level, got {type(raw).__name__}")

    cpr = CPR()
    declared = {f.name for f in dataclasses.fields(CPR)}

    # Pass 1 — plain + provenance fields (skip the structured keys handled below).
    for key, val in raw.items():
        if key in ("valuation", "liens", "ledger", "column", "blast_tier",
                   "lien_lookup_status"):
            continue
        if key == "lien_priority_ranking":
            cpr.lien_priority_ranking = [str(x) for x in (val or [])]
            continue
        if _is_provenance_dict(val):
            setattr(cpr, key, _restore_provenance(val))  # extras like deed_number OK
        elif key in declared or not isinstance(val, (dict, list)):
            try:
                setattr(cpr, key, val)
            except (AttributeError, TypeError) as exc:
                _warn(f"{path}: could not restore field {key!r}: {exc}")

    # Pass 2 — ledger first so add_lien receipts chain onto restored history.
    for entry in raw.get("ledger") or []:
        if not isinstance(entry, dict):
            continue
        e = ProvenanceEntry(
            field_name=str(entry.get("field_name") or ""),
            old_value=entry.get("old_value"),
            new_value=entry.get("new_value"),
            source=str(entry.get("source") or ""),
            timestamp=str(entry.get("timestamp") or ""),
            prev_hash=str(entry.get("prev_hash") or ""),
            note=str(entry.get("note") or ""),
        )
        e.entry_hash = str(entry.get("entry_hash") or "")
        cpr.ledger.append(e)

    # Pass 3 — liens via add_lien, per contract (receipted, chain-preserving).
    for ld in raw.get("liens") or []:
        if not isinstance(ld, dict):
            continue
        lien = LienRecord(
            lien_type=str(ld.get("lien_type") or "unknown"),
            amount=(float(ld["amount"]) if ld.get("amount") is not None else None),
            recording_date=ld.get("recording_date"),
            lender=ld.get("lender"),
            priority=int(ld.get("priority") or 0),
            survives_foreclosure=bool(ld.get("survives_foreclosure")),
            survives_tax_sale=bool(ld.get("survives_tax_sale")),
        )
        cpr.add_lien(lien, source="hitl:cpr_rehydrate", confidence=0.0)

    # Pass 4 — pipeline state.
    cpr.blast_tier = coerce_blast_tier(raw.get("blast_tier"), default=BlastTier.T2)
    col_val = raw.get("column")
    if col_val is not None:
        try:
            cpr.column = CPRColumn(str(col_val))
        except ValueError:
            _warn(f"{path}: unrecognized column {col_val!r}; keeping {cpr.column.value}")
    status = raw.get("lien_lookup_status")
    if status is not None:
        try:
            cpr.lien_lookup_status = str(status)
        except ValueError:
            _warn(f"{path}: invalid lien_lookup_status {status!r}; keeping 'not_run'")
    if raw.get("dead_reason"):
        cpr.dead_reason = str(raw["dead_reason"])

    return cpr


def intake_comps(comp_dicts: list[dict]) -> list[CompRecord]:
    """Validate hand-collected comp dicts into CompRecords — loud failures.

    Required per comp: address, sale_price (> 0), sale_date parseable by
    src.dates.parse_date_any, distance_miles <= 2.0 mi. Source must be one of
    the approved human channels (default mls_agent); adjusted_price and the
    sqft/beds/baths similarity trio are optional. Distance > 1.0 mi is
    accepted with a warning (GATE-VAL still drops anything beyond 0.5 mi).

    Note (T3 gating): at appraiser-grade tiers the confidence scorer only
    clears the floor when comps carry agent-supplied adjusted_price values;
    raw unadjusted comps cap below the T3 floor and route to TRIAGE by design.
    """
    records: list[CompRecord] = []
    for i, d in enumerate(comp_dicts):
        if not isinstance(d, dict):
            raise ValueError(f"intake_comps[{i}]: expected a dict, got {type(d).__name__}")
        label = str(d.get("address") or f"comp#{i}")

        address = str(d.get("address") or "").strip()
        if not address:
            raise ValueError(f"intake_comps[{i}]: missing required 'address'")

        try:
            sale_price = float(d.get("sale_price"))
        except (TypeError, ValueError):
            raise ValueError(
                f"intake_comps[{i}] ({label}): missing/unparseable required "
                f"'sale_price': {d.get('sale_price')!r}"
            )
        if sale_price <= 0:
            raise ValueError(
                f"intake_comps[{i}] ({label}): sale_price must be > 0, got {sale_price!r}"
            )

        parsed_date = parse_date_any(d.get("sale_date"))
        if parsed_date is None:
            raise ValueError(
                f"intake_comps[{i}] ({label}): required 'sale_date' is unparseable: "
                f"{d.get('sale_date')!r} (parse_date_any rejected it)"
            )

        source = str(d.get("source") or DEFAULT_COMP_SOURCE).strip()
        if source not in ALLOWED_COMP_SOURCES:
            raise ValueError(
                f"intake_comps[{i}] ({label}): source {source!r} not approved for HITL "
                f"intake — must be one of {list(ALLOWED_COMP_SOURCES)}"
            )

        try:
            distance = float(d.get("distance_miles"))
        except (TypeError, ValueError):
            raise ValueError(
                f"intake_comps[{i}] ({label}): missing/unparseable required "
                f"'distance_miles': {d.get('distance_miles')!r}"
            )
        if distance < 0:
            raise ValueError(
                f"intake_comps[{i}] ({label}): negative distance_miles {distance!r}"
            )
        if distance > MAX_INTAKE_DISTANCE_MI:
            raise ValueError(
                f"intake_comps[{i}] ({label}): distance_miles {distance} > "
                f"{MAX_INTAKE_DISTANCE_MI} mi — outside any usable comp radius"
            )
        if distance > WARN_INTAKE_DISTANCE_MI:
            _warn(
                f"{label}: distance_miles {distance} exceeds {WARN_INTAKE_DISTANCE_MI} mi "
                f"— weak proximity, confidence will suffer"
            )

        def _opt_num(key: str, cast):
            val = d.get(key)
            if val is None or val == "":
                return None
            try:
                return cast(float(val))
            except (TypeError, ValueError):
                _warn(f"{label}: ignoring unparseable '{key}': {val!r}")
                return None

        adjusted_raw = d.get("adjusted_price")
        try:
            adjusted = float(adjusted_raw) if adjusted_raw not in (None, "") else None
        except (TypeError, ValueError):
            _warn(f"{label}: ignoring unparseable 'adjusted_price': {adjusted_raw!r}")
            adjusted = None

        records.append(CompRecord(
            address=address,
            sale_price=sale_price,
            sale_date=parsed_date.isoformat(),  # normalized; parse_date_any-stable
            distance_miles=distance,
            sqft=_opt_num("sqft", int),
            beds=_opt_num("beds", int),
            baths=_opt_num("baths", float),
            source=source,
            adjusted_price=adjusted,
        ))
    return records


def run_hitl_valuation(cpr: CPR, comps: list[CompRecord],
                       comp_source: str = DEFAULT_COMP_SOURCE
                       ) -> tuple[GateValResult, ValuationObject]:
    """Re-run GATE-VAL at the CPR's own blast tier and attach the result.

    Column moves follow attach_valuation semantics: pass -> VALUED,
    hitl -> TRIAGE, fail -> DEAD(low_confidence_comp).
    """
    tier = coerce_blast_tier(getattr(cpr, "blast_tier", None))
    gate, val_obj = gate_val(cpr, comp_source, comps, tier)
    attach_valuation(cpr, val_obj, gate)
    return gate, val_obj


def _default_out_path(input_path: str) -> str:
    """data/cpr_X.json -> data/cpr_X.valued.json (alongside the input)."""
    root, ext = os.path.splitext(input_path)
    if ext == ".json":
        root = input_path[: -len(".json")]
    return root + ".valued.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.hitl",
        description=("HITL comp intake: load a saved CPR, validate a hand-collected "
                     "comp set, re-run GATE-VAL on an approved source, write the "
                     "valued CPR atomically."),
    )
    parser.add_argument("--cpr", help="path to a saved CPR json (to_dict() output)")
    parser.add_argument("--comps", help="path to a comps json file (list of dicts)")
    parser.add_argument("--out", help="output path (default: alongside --cpr as *.valued.json)")
    parser.add_argument("--self-check", action="store_true",
                        help="run the module self-check instead of an intake run")
    args = parser.parse_args(argv)

    if args.self_check or (args.cpr is None and args.comps is None and args.out is None):
        self_check()
        print("hitl.py: self-check passed")
        return 0

    missing = [flag for flag, val in (("--cpr", args.cpr), ("--comps", args.comps))
               if val is None]
    if missing:
        parser.error(f"{', '.join(missing)} required for an intake run "
                     f"(or run with no args for the self-check)")

    cpr = load_cpr(args.cpr)
    with open(args.comps, "r", encoding="utf-8") as f:
        raw_comps = json.load(f)
    if isinstance(raw_comps, dict):  # tolerate {"comps": [...]} wrappers
        raw_comps = raw_comps.get("comps") or raw_comps.get("comp_set")
    if not isinstance(raw_comps, list):
        raise ValueError(f"{args.comps}: expected a JSON list of comp dicts")

    comps = intake_comps(raw_comps)
    prev_column = cpr.column
    gate, val_obj = run_hitl_valuation(cpr, comps)

    out_path = args.out or _default_out_path(os.path.abspath(args.cpr))
    write_json(out_path, cpr.to_dict(), indent=2, default=str)

    arv = pv_value(val_obj.arv) if val_obj.arv else None
    print(f"CPR: {cpr.parcel_id} ({cpr.county}) — {cpr.situs_address} [{cpr.blast_tier.name}]")
    print(f"Comps accepted: {len(comps)} (source {gate.comp_source})")
    print(f"GATE-VAL: {gate.verdict.upper()} — {gate.reason}")
    print(f"Confidence: {gate.confidence:.2f} vs floor {gate.floor:.2f} "
          f"({cpr.blast_tier.name}) | comps used: {gate.comp_count}")
    print(f"ARV: {'${:,.0f}'.format(arv) if arv else 'n/a'}")
    print(f"Column: {prev_column.value} -> {cpr.column.value}")
    print(f"Updated CPR written: {out_path}")
    return 0


def self_check() -> None:
    """End-to-end synthetic check: save -> load -> intake -> gate -> write."""
    tmpdir = tempfile.mkdtemp(prefix="hitl_selfcheck_")
    try:
        today = datetime.now(timezone.utc).date()

        def _dated(days_ago: int) -> str:
            return (today - timedelta(days=days_ago)).isoformat()

        cpr_dict = {
            "parcel_id": "HITL-TEST-1",
            "county": "grayson",
            "situs_address": "900 Test Ave, Sherman, TX 75090",
            "channel": "foreclosure_notice",
            "deal_type": "DT-AUCTION",
            "column": "ENRICHED",
            "blast_tier": 3,
            "owner_name": {"value": "Test Owner", "source": "unit_test",
                           "retrieved_at": "2026-08-25T00:00:00", "confidence": 0.9},
            "liens": [{"lien_type": "mortgage", "amount": 250000.0,
                       "recording_date": "3/3/2020", "lender": "TEST BANK",
                       "priority": 1, "survives_foreclosure": False,
                       "survives_tax_sale": False}],
            # Stale valuation must be ignored and replaced by the HITL run.
            "valuation": {"gate_val_verdict": "stale", "comp_set": []},
        }
        cpr_path = os.path.join(tmpdir, "cpr_test.json")
        write_json(cpr_path, cpr_dict)

        cpr = load_cpr(cpr_path)
        assert cpr.parcel_id == "HITL-TEST-1"
        assert cpr.blast_tier == BlastTier.T3, cpr.blast_tier
        assert pv_value(cpr.owner_name) == "Test Owner"
        assert len(cpr.liens) == 1 and cpr.liens[0].amount == 250000.0
        assert cpr.valuation is None, "stored valuation must be ignored at load"
        assert len(cpr.ledger) == 1 and cpr.ledger[-1].field_name == "lien:mortgage"

        # Default-tier path: absent blast_tier falls back to T2.
        minimal_path = os.path.join(tmpdir, "cpr_minimal.json")
        write_json(minimal_path, {"parcel_id": "HITL-TEST-2", "county": "collin"})
        assert load_cpr(minimal_path).blast_tier == BlastTier.T2

        comp_dicts = [
            {"address": f"{100 + i} Neighbor St", "sale_price": 300000 + i * 5000,
             "adjusted_price": 301000 + i * 5000, "sale_date": _dated(10 + i),
             "distance_miles": round(0.2 + 0.05 * i, 2), "source": "mls_agent"}
            for i in range(3)
        ]
        records = intake_comps(comp_dicts)
        assert len(records) == 3 and records[0].source == "mls_agent"
        assert records[0].sale_date == _dated(10)  # normalized ISO date

        gate, val_obj = run_hitl_valuation(cpr, records, DEFAULT_COMP_SOURCE)
        assert gate.verdict in {"pass", "fail", "hitl"}, gate.verdict
        assert gate.verdict == "pass", (
            f"strong synthetic comp set should clear the T3 floor, got "
            f"{gate.verdict}: {gate.reason}"
        )
        assert val_obj.arv is not None
        assert abs(pv_value(val_obj.arv) - 306000.0) < 0.01  # median of adjusted
        assert cpr.valuation is val_obj
        assert cpr.column == CPRColumn.VALUED, cpr.column

        # Unparseable dates are rejected loudly, never silently dropped.
        try:
            intake_comps([{**comp_dicts[0], "sale_date": "sometime soon"}])
            raise AssertionError("expected ValueError for unparseable sale_date")
        except ValueError:
            pass
        # Non-approved sources are rejected at the HITL door.
        try:
            intake_comps([{**comp_dicts[0], "source": "vendor_avm"}])
            raise AssertionError("expected ValueError for non-HITL comp source")
        except ValueError:
            pass
        # Over-radius comps are rejected (>2mi), mid ones warn only.
        try:
            intake_comps([{**comp_dicts[0], "distance_miles": 2.5}])
            raise AssertionError("expected ValueError for distance > 2.0 mi")
        except ValueError:
            pass

        # CLI end-to-end on the same temp files: verdict computed + file written.
        comps_path = os.path.join(tmpdir, "comps.json")
        write_json(comps_path, comp_dicts)
        out_path = os.path.join(tmpdir, "cpr_test.valued.json")
        rc = main(["--cpr", cpr_path, "--comps", comps_path, "--out", out_path])
        assert rc == 0
        with open(out_path, encoding="utf-8") as f:
            valued = json.load(f)
        assert valued["valuation"]["gate_val_verdict"] == "pass"
        assert valued["column"] == "VALUED"
        assert valued["parcel_id"] == "HITL-TEST-1"
        assert _default_out_path("/x/data/cpr_1.json").endswith("cpr_1.valued.json")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
