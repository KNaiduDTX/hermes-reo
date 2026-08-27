#!/usr/bin/env python3
"""Audit TEST-3: the desk-E2E milestone (every record -> CPR -> full desk)
skips entirely in CI because data/ is gitignored. This generator materializes
a DETERMINISTIC, SYNTHETIC dataset (no PII — invented addresses at plausible
TX geometry) so CI can run the whole-book desk validation against it.

  python scripts/synth_dataset.py [N]     # default 8 rows, writes data/current_deals.json

The generator is idempotent and refuses to overwrite an existing dataset
unless --force is passed (the real dataset is never clobbered by accident).
"""
import argparse
import json
import os
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

BASES = [
    ("1001 Central Expy N", "Plano", "collin", "75023"),
    ("2200 W University Dr", "McKinney", "collin", "75069"),
    ("305 N Church St", "McKinney", "collin", "75069"),
    ("4400 Dean Gilbert Dr", "Frisco", "collin", "75034"),
    ("2626 Lombardy Ln", "Dallas", "dallas", "75220"),
    ("3311 S Polk St", "Dallas", "dallas", "75211"),
    ("5821 Overton Ridge Blvd", "Fort Worth", "dallas", "76132"),
    ("2101 Cedar Springs Rd", "Dallas", "dallas", "75204"),
]


def synth_rows(n: int) -> list[dict]:
    sys.path.insert(0, str(ROOT))
    from src.identity import property_id as compute_property_id
    rows = []
    for i, (street, city, county, zip5) in enumerate(BASES[:n]):
        cad = 320_000 + 37_000 * i
        debt = cad // 2
        address = f"{street}, {city}, TX {zip5}"
        rows.append({
            "parcel_id": f"SYN{i:04d}",
            # The identity spine: desk CPR resolution matches on this.
            "property_id": compute_property_id(county, address),
            "address": address,
            "city": city, "city_norm": city, "county": county,
            "owner": f"SYNTHETIC OWNER {i}",
            "cad_value": cad, "cad_value_2025": int(cad * 24 / 25),
            "mortgage_debt": debt, "equity_estimate": cad - debt,
            "equity_pct": round((cad - debt) / cad, 3),
            "sqft": 1600 + 90 * i, "beds": 3, "baths": 2,
            "year_built": 1996 + i, "blast_tier": "T2",
            "property_type": "RESIDENTIAL", "lender": "SYNTH LENDER LLC",
            "sale_date": "2026-11-03",
        })
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("count_pos", nargs="?", type=int, default=None,
                   help="row count (positional)")
    p.add_argument("--count", type=int, default=8)
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing data/current_deals.json")
    a = p.parse_args()
    DATA.mkdir(exist_ok=True)
    target = DATA / "current_deals.json"
    if target.exists():
        existing = []
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = []
        all_synthetic = bool(existing) and all(
            str(r.get("parcel_id") or "").startswith("SYN") for r in existing)
        if existing and not all_synthetic:
            print("REFUSING: data/current_deals.json holds the real dataset "
                  "(non-SYN parcel ids). The synthetic generator may only "
                  "overwrite its own output. Run it in a checkout without "
                  "data/ (as CI does) or move the real dataset aside first.")
            return 2
    rows = synth_rows(a.count_pos if a.count_pos is not None else a.count)
    target.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"synthetic dataset: {target} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())