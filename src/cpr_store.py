# src/cpr_store.py
"""Batch CPR generation + storage for every record in the book (E2E goal).

Gives every row in current_deals.json a saved, reloadable CPR under
data/cprs/{parcel_id}.json so Dossier/Underwriting/Offers work for the
ENTIRE book — not just fixture parcels.

Keys are parcel_ids exactly as used by desk routes (/deals/{parcel_id}),
so load paths stay uniform. Identity ids (property_id/notice_id) are
computed via src.identity and embedded.
"""
from __future__ import annotations

import json
import sys

try:
    from .io_utils import write_json
    from .identity import property_id as compute_property_id
    from .enrichment import enrich_from_flat_record
except ImportError:  # direct-script execution
    from io_utils import write_json  # type: ignore
    from identity import compute_property_id  # type: ignore
    from enrichment import enrich_from_flat_record  # type: ignore


def _safe_name(pid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in pid) or "unknown"


def build_cpr(record: dict) -> tuple[str, dict]:
    """Enrich one flat record -> (property_id, full to_dict() CPR payload).

    Files are keyed by the DURABLE property_id (unique per asset); all
    parcel_ids that observed the asset ride inside the payload so desk
    routes can resolve any of them to the same file.
    """
    cpr = enrich_from_flat_record(record)
    # Mirror find_deals.identity_columns EXACTLY: owner tiebreaker only when
    # the address is implausible, so store keys match desk row ids 1:1.
    from .addr import is_plausible_street_address
    owner_key = str(record.get("owner") or "").strip()
    addr_valid = is_plausible_street_address(str(record.get("address") or ""))
    prop = compute_property_id(
        str(record.get("county") or ""),
        str(record.get("address") or ""),
        owner=owner_key if (owner_key and not addr_valid) else "",
    )
    cpr.property_id = prop
    payload = cpr.to_dict()
    payload["parcel_ids"] = [str(record.get("parcel_id") or "")
                             if record.get("parcel_id") else prop]
    payload["source_row"] = {
        k: record.get(k) for k in ("address", "city", "county", "owner",
                                   "cad_value", "mortgage_debt", "lender",
                                   "sale_date", "blast_tier")
        if record.get(k) not in (None, "", "Unknown", 0)
    }
    return prop, payload


def generate_all(deals_path: str, out_dir: str, limit: int | None = None,
                 on_progress=None) -> dict:
    """Generate/save CPRs for every record. Returns a tally dict."""
    with open(deals_path, encoding="utf-8") as f:
        records = json.load(f)
    import os
    os.makedirs(out_dir, exist_ok=True)
    ok = failed = 0
    failures = []
    # Audit CPR-2: seed pid from the record BEFORE build_cpr so the except
    # handler below can never hit an UnboundLocalError (a first-record crash
    # used to escape the "batch must continue" catch and kill the whole book),
    # and later failures are never mislabeled with a previous record's id.
    selected = records[:limit] if limit else records
    for i, rec in enumerate(selected, 1):
        pid = str(rec.get("parcel_id") or "") or f"row-{i}"
        try:
            pid, payload = build_cpr(rec)
            write_json(os.path.join(out_dir, _safe_name(pid) + ".json"), payload)
            ok += 1
        except Exception as exc:  # noqa: BLE001 — batch must continue
            failed += 1
            failures.append({"parcel": pid, "error": repr(exc)})
            continue
        if on_progress and i % 25 == 0:
            on_progress(i, ok, failed)
    # Audit CPR-2: 'skipped' now means "not attempted because of limit",
    # not a meaningless near-zero residual.
    skipped = len(records) - len(selected)
    return {"total": len(records), "ok": ok, "failed": failed,
            "skipped": skipped, "failures": failures}


def self_check() -> None:
    import tempfile
    rec = {"address": "15806 BREEDLOVE PLACE, ADDISON, TX 75001",
           "city": "Addison", "county": "dallas", "parcel_id": "T1",
           "sqft": 2388, "cad_value": 518510}
    d = tempfile.mkdtemp()
    out = generate_all.__wrapped__ if hasattr(generate_all, "__wrapped__") else None
    # inline single-record generation
    import json as _json
    src = d + "/in.json"
    _json.dump([rec], open(src, "w"))
    res = generate_all(src, d)
    assert res["ok"] == 1 and res["failed"] == 0, res
    saved = _json.load(open(d + "/T1.json"))
    assert saved["parcel_id"] == "T1"
    print("cpr_store.py: batch generation self-check passed")


if __name__ == "__main__":
    self_check()
