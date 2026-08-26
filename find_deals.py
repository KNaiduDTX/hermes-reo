#!/usr/bin/env python3
# find_deals.py — End-to-end run that finds and analyzes ALL current deals.
#
# Loads the full 335-record lucid-noether dataset, enriches each into a CPR,
# runs all gates, and outputs deal analysis + portfolio stats + GeoJSON.
# Set HERMES_SKIP_LIVE=1 to skip the live Collin scrape and run fully offline
# against the prior dataset (default behavior unchanged: live scrape runs).
from __future__ import annotations
import json
import re
import sys
import os
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.enrichment import (
    enrich_from_flat_record,
    enrich_from_raw_record,
    enrich_lead,
    normalize_deal_identity,
)
from src.adapters.collin_foreclosure import CollinForeclosureAdapter
from src.adapters.geojson_export import cpr_to_geojson
from src.underwriting import mao
from src.pricing import assign_blast_tier
from src.compliance import gate_legal_lien
from src.synthesis import portfolio_stats, render_stats_report, get_city
from src.cpr import CPR, pv_value
from src.addr import is_plausible_street_address, normalize_address
from src.dates import parse_date_any
from src.identity import notice_id, property_id  # FN1: identity spine contract
from src.io_utils import write_json, write_text  # atomic crash-safe writers (shared contract)
# TD8: shared deal-economics kernel (equity/LTV trio) so analysis and portfolio
# rollups can never drift apart on the arithmetic.
from src.metrics import compute_leverage_metrics

# F1: dataset location is overridable via env; default keeps the lucid-noether path.
DEFAULT_LUCID_DATA = "/Users/knaidu/Documents/antigravity/lucid-noether/foreclosure_report.json"
LUCID_DATA = os.environ.get("HERMES_LUCID_DATA") or DEFAULT_LUCID_DATA

# --- B2: MERGE-ON-WRITE ------------------------------------------------------
# This script regenerates current_deals.json FROM SCRATCH on every run, which
# used to destroy any enrichment previously written back into the dataset by
# `pipeline.py --bulk-dcad` or `--geocode-backfill` (AUDIT D1, second half).
# Before writing, the EXISTING dataset (if any) is loaded and keyed by
# normalize_deal_identity() — (county.lower(), upper-collapsed address) — and
# each fresh record inherits the prior value for the enrichment fields below
# whenever the fresh scrape left that field empty/"Unknown"/0. Fresh non-empty
# values always win (a genuine re-scrape still refreshes data); only gaps are
# filled from the previous generation.
ENRICHMENT_CARRY_FORWARD_FIELDS = (
    "sqft", "beds", "baths", "year_built", "stories", "pool",
    "cad_value", "cad_value_2025", "census_tract", "county_fips",
    "latitude", "longitude",
    "val_gate",  # human HITL gate decisions must survive regeneration (reviewer low-note)
)


def _enrichment_field_is_empty(value) -> bool:
    """True when a freshly scraped value is unusable as enrichment data."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip().upper() in ("", "UNKNOWN"):
        return True
    if isinstance(value, (int, float)) and value == 0:
        # bools are ints: False (e.g. a defaulted pool flag) counts as empty,
        # so a prior DCAD-confirmed True can be carried forward.
        return True
    return False


def merge_prior_enrichment(deals: list[dict], prior_path: str) -> list[dict]:
    """Carry prior enrichment into freshly scraped deals before the write.

    Loads the previous current_deals.json at *prior_path* (tolerating absence
    or corruption) and fills empty ENRICHMENT_CARRY_FORWARD_FIELDS on each
    fresh record from its predecessor. Mutates and returns *deals*.

    FN1 re-key: the PRIMARY match key is now the durable property_id column.
    The legacy (county, upper-collapsed address) identity remains only as a
    FALLBACK, used when either side lacks an id (old datasets carry none
    until they are regenerated once through the identity spine). When both
    sides carry ids that DISAGREE there is deliberately no fallback match:
    two different asset ids over one address stay two separate assets.
    """
    prior_by_property: dict[str, dict] = {}
    prior_by_address: dict[tuple[str, str], dict] = {}
    if os.path.exists(prior_path):
        try:
            with open(prior_path, encoding="utf-8") as fh:
                for old in json.load(fh):
                    pid = str(old.get("property_id") or "")
                    if pid:
                        prior_by_property[pid] = old
                    prior_by_address[normalize_deal_identity(
                        old.get("county"), old.get("address"))] = old
        except (OSError, ValueError) as exc:
            print(f"  ⚠️ Merge-on-write: unreadable prior dataset ({exc!r}) "
                  f"— continuing without carry-forward")
            prior_by_property, prior_by_address = {}, {}

    records_merged = fields_carried = id_matches = fallback_matches = 0
    for fresh in deals:
        fresh_pid = str(fresh.get("property_id") or "")
        prior = prior_by_property.get(fresh_pid) if fresh_pid else None
        if prior is not None:
            id_matches += 1
        else:
            # FN1 fallback: only when at least one side lacks an id.
            prior = prior_by_address.get(normalize_deal_identity(
                fresh.get("county"), fresh.get("address")))
            if prior is not None and fresh_pid \
                    and str(prior.get("property_id") or ""):
                prior = None              # both have ids and they disagree
            if prior is not None:
                fallback_matches += 1
        if not prior:
            continue
        carried_here = 0
        for field_name in ENRICHMENT_CARRY_FORWARD_FIELDS:
            if _enrichment_field_is_empty(fresh.get(field_name)) and \
                    not _enrichment_field_is_empty(prior.get(field_name)):
                fresh[field_name] = prior[field_name]
                carried_here += 1
        if carried_here:
            records_merged += 1
            fields_carried += carried_here

    print(f"  Merge-on-write: {records_merged}/{len(deals)} refreshed record(s), "
          f"{fields_carried} enrichment field(s) carried forward from prior "
          f"dataset (by property_id: {id_matches}, by legacy address fallback: "
          f"{fallback_matches})")
    return deals


# --- ADDRESS-QUALITY GATE (src.addr shared contract) --------------------------
# Every record address is routed through src.addr.normalize_address. Records
# whose address is not a plausible street address (legal prose, out-of-state,
# corrupted tokens, missing zip/state) carry an in-memory "invalid_query"
# marker, are excluded from every TOP table and the TOP-EQUITY-PROSPECTS list,
# but REMAIN in the saved dataset untouched: strip_invalid_query_markers()
# removes the runtime-only key before write_json so the current_deals.json
# row schema stays exactly as before.
INVALID_QUERY_KEY = "invalid_query"


def mark_invalid_query(deal: dict) -> dict:
    """Stamp *deal* with INVALID_QUERY_KEY when its address is implausible.

    The marker value is the normalize_address invalid_reason (truthy string).
    Plausible addresses are returned unmodified — no key is added, so valid
    rows keep their exact legacy schema.
    """
    addr = deal.get("address") or ""
    if is_plausible_street_address(addr):
        return deal
    deal[INVALID_QUERY_KEY] = \
        normalize_address(addr).invalid_reason or "unparseable address"
    return deal


def has_plausible_address(deal: dict) -> bool:
    """True unless the deal carries an invalid_query marker."""
    return not deal.get(INVALID_QUERY_KEY)


def rankable_deals(deals: list[dict]) -> list[dict]:
    """Deals eligible for TOP tables / equity lists (address-quality gate)."""
    return [d for d in deals if has_plausible_address(d)]


def top_equity_prospects(deals: list[dict], limit: int = 10) -> list[dict]:
    """Positive-equity deals sorted desc — malformed addresses excluded."""
    candidates = [d for d in rankable_deals(deals)
                  if d.get("equity_estimate") and d["equity_estimate"] > 0]
    candidates.sort(key=lambda d: d["equity_estimate"], reverse=True)
    return candidates[:limit]


def count_malformed_addresses(deals: list[dict]) -> int:
    """Number of deals excluded by the address-quality gate."""
    return sum(1 for d in deals if not has_plausible_address(d))


def strip_invalid_query_markers(deals: list[dict]) -> list[dict]:
    """Copies of *deals* WITHOUT the runtime-only invalid_query marker.

    The saved dataset must stay byte-schema-identical to the legacy format;
    callers pass this result to write_json instead of the marked lists.
    """
    return [{k: v for k, v in d.items() if k != INVALID_QUERY_KEY}
            for d in deals]


# --- FN2: per-run history store (data/history/) --------------------------------
# After every regeneration the full deals payload is snapshotted to
# data/history/YYYY-MM-DD.json (atomic io_utils.write_json) together with a
# derived "notices" block: the accumulated per-property observation history
# folded across every snapshot on disk. data/history/index.json keeps the
# compact {property_id: {first_seen, last_seen, seen_count}} rollup merged
# across runs, so first-seen memory survives even if daily snapshots are
# later pruned. All derivation lives in derive_history — a pure function.
HISTORY_INDEX_FILENAME = "index.json"
_HISTORY_SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def _load_json_object(path: str):
    """Parsed dict at *path*, or None when missing/corrupt/not an object."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def load_previous_snapshots(history_dir: str, exclude_date: str = "") -> list:
    """Every well-formed daily snapshot on disk, oldest first.

    Only files named YYYY-MM-DD.json are considered; corrupt or non-object
    payloads are skipped with a warning (history derivation degrades, never
    crashes). *exclude_date* drops today's own file so a same-day re-run
    cannot double-count observations it is about to overwrite anyway.
    """
    if not os.path.isdir(history_dir):
        return []
    snapshots = []
    for name in sorted(os.listdir(history_dir)):
        if not _HISTORY_SNAPSHOT_RE.match(name):
            continue
        date_str = name[:10]
        if date_str == exclude_date:
            continue
        snapshot = _load_json_object(os.path.join(history_dir, name))
        if snapshot is None:
            print(f"  ⚠️ History: unreadable snapshot skipped: {name}")
            continue
        snapshot.setdefault("date", date_str)
        snapshots.append(snapshot)
    snapshots.sort(key=lambda snap: str(snap.get("date") or ""))
    return snapshots


def derive_history(previous_snapshots: list, current: dict) -> dict:
    """Accumulated per-property history across snapshots (pure, testable).

    Walks every snapshot's deals oldest-first (previous snapshots ordered by
    their "date", then *current*'s own rows last) and folds, per property_id:

      first_seen / last_seen — oldest/newest run date observed
      seen_count             — number of DISTINCT run dates the property
                               appeared on (same-day re-runs never inflate)
      sale_dates             — ISO-normalized, in first-observation order;
                               format drift collapses ("Tuesday, the 1st day
                               of September, 2026" == "09/01/2026")
      debt_changes           — [(from, to)] between consecutive run dates,
                               recorded only when BOTH ends are known values
      lender_changes         — [(from, to)] between consecutive run dates,
                               recorded only when BOTH ends are non-empty

    Snapshots without a usable "date" and rows without a property_id are
    skipped. Returns {property_id: {...}} — JSON-ready (lists, not tuples).
    """
    dated: list = []
    for snapshot in sorted(previous_snapshots,
                           key=lambda snap: str(snap.get("date") or "")):
        date_str = str(snapshot.get("date") or "")
        if not date_str:
            continue
        for row in (snapshot.get("deals") or []):
            if isinstance(row, dict):
                dated.append((date_str, row))
    current_date = str(current.get("date") or "")
    for row in (current.get("deals") or []):
        if isinstance(row, dict):
            dated.append((current_date, row))

    history: dict = {}
    last_seen: dict = {}
    for date_str, row in dated:
        pid = str(row.get("property_id") or "")
        if not pid:
            continue
        entry = history.get(pid)
        if entry is None:
            entry = history[pid] = {
                "first_seen": date_str, "last_seen": date_str,
                "seen_count": 0, "sale_dates": [],
                "debt_changes": [], "lender_changes": [],
            }
            last_seen[pid] = {"date": None, "debt": None, "lender": None}
        if date_str < entry["first_seen"]:
            entry["first_seen"] = date_str
        if date_str > entry["last_seen"]:
            entry["last_seen"] = date_str
        prev = last_seen[pid]
        new_day = bool(date_str) and date_str != prev["date"]
        if new_day:
            entry["seen_count"] += 1
            if prev["date"] is not None:
                debt = row.get("mortgage_debt")
                if debt is not None and prev["debt"] is not None \
                        and debt != prev["debt"]:
                    entry["debt_changes"].append([prev["debt"], debt])
                lender = str(row.get("lender") or "").strip()
                if lender and prev["lender"] and lender != prev["lender"]:
                    entry["lender_changes"].append([prev["lender"], lender])
        parsed = parse_date_any(row.get("sale_date"))
        sale_iso = parsed.isoformat() if parsed else \
            str(row.get("sale_date") or "").strip()
        if sale_iso and sale_iso.upper() != "UNKNOWN" \
                and sale_iso not in entry["sale_dates"]:
            entry["sale_dates"].append(sale_iso)
        prev.update({"date": date_str,
                     "debt": row.get("mortgage_debt"),
                     "lender": str(row.get("lender") or "").strip()})
    return history


def merge_history_index(existing: dict, derived: dict) -> dict:
    """Fold newly derived rollups into the persistent index (monotone).

    Dates take min/max across both sources so an entry outlives its daily
    snapshots (pruning old files can never regress first_seen/last_seen);
    seen_count takes the max. Output rows keep exactly the index schema
    {first_seen, last_seen, seen_count}, sorted by property_id.
    """
    merged: dict = {}
    for pid in sorted(set(existing) | set(derived)):
        sides = [side for side in (existing.get(pid), derived.get(pid))
                 if isinstance(side, dict)]
        firsts = [s["first_seen"] for s in sides
                  if isinstance(s.get("first_seen"), str) and s["first_seen"]]
        lasts = [s["last_seen"] for s in sides
                 if isinstance(s.get("last_seen"), str) and s["last_seen"]]
        counts = [s["seen_count"] for s in sides
                  if isinstance(s.get("seen_count"), int)
                  and not isinstance(s.get("seen_count"), bool)]
        merged[pid] = {
            "first_seen": min(firsts) if firsts else "",
            "last_seen": max(lasts) if lasts else "",
            "seen_count": max(counts) if counts else 0,
        }
    return merged


def update_history_store(history_dir: str, run_date: str,
                         deals_payload: list) -> tuple:
    """Snapshot this run into the history store and refresh the index.

    Writes <history_dir>/<run_date>.json = {"date", "deals", "notices"}
    atomically (full deals snapshot plus the derived notices block), then
    rewrites index.json atomically with merge_history_index. Returns
    (snapshot_path, churn_line) where churn_line reads
    "N properties carried, K new, M with sale-date changes"; churn is
    measured against every snapshot strictly BEFORE <run_date>, so a
    same-day re-run replaces today's baseline rather than stacking on it.
    """
    os.makedirs(history_dir, exist_ok=True)
    run_date = str(run_date)
    previous = load_previous_snapshots(history_dir, exclude_date=run_date)
    snapshot = {"date": run_date, "deals": list(deals_payload)}
    notices = derive_history(previous, snapshot)
    snapshot_path = write_json(
        os.path.join(history_dir, f"{run_date}.json"),
        dict(snapshot, notices=notices))

    index_path = os.path.join(history_dir, HISTORY_INDEX_FILENAME)
    existing_index = _load_json_object(index_path) or {}
    write_json(index_path, merge_history_index(existing_index, notices))

    # Churn over THIS run's deals: "carried" = already tracked before today,
    # "new" = first appearance today; sale-date changes count among carried.
    # (Properties absent from today's dataset stay in history/index but are
    # neither carried nor new — the line partitions the current run.)
    prior_state = derive_history(previous, {"date": run_date, "deals": []})
    current_pids = {str(row.get("property_id") or "")
                    for row in snapshot["deals"]}
    current_pids.discard("")
    carried = sale_changed = 0
    for pid in sorted(current_pids):
        was = prior_state.get(pid)
        if not was:
            continue
        carried += 1
        if notices[pid]["sale_dates"] != was.get("sale_dates"):
            sale_changed += 1
    churn_line = (f"{carried} properties carried, "
                  f"{len(current_pids) - carried} new, "
                  f"{sale_changed} with sale-date changes")
    return snapshot_path, churn_line


# --- HERMES_SKIP_LIVE (offline knob) ------------------------------------------
# HERMES_SKIP_LIVE=1 skips the live Collin portal scrape and proceeds with the
# prior dataset alone — enabling offline runs/tests. Default (unset/other) is
# unchanged: the live scrape runs exactly as before.
SKIP_LIVE_ENV = "HERMES_SKIP_LIVE"
_TRUTHY = ("1", "true", "yes", "on")


def skip_live_scrape() -> bool:
    """True when HERMES_SKIP_LIVE requests offline (no live scrape) operation."""
    return os.environ.get(SKIP_LIVE_ENV, "").strip().lower() in _TRUTHY


def fetch_live_cprs(limit=5):
    """Live Collin scrape unless HERMES_SKIP_LIVE requests offline mode."""
    if skip_live_scrape():
        print(f"\n[COLLIN] {SKIP_LIVE_ENV} is set — skipping live scrape; "
              f"proceeding with prior dataset only")
        return []
    return scrape_live_collin(limit=limit)


# --- F7 companion: CPR → flat-row coordinate passthrough ----------------------
# analyze_deal emits no latitude/longitude keys, so a live-scraped lead whose
# CPR carries geocoded coordinates would lose its position the moment the
# GeoJSON export reads flat records instead of raw CPRs. copy_cpr_coordinates
# copies usable CPR coordinates into the row under the same latitude/longitude
# keys pipeline.py --geocode-backfill writes.

def _coord_float(value: Any) -> Optional[float]:
    """Unwrap a provenance field/plain value (pv_value) and parse to float, or None."""
    unwrapped = pv_value(value)
    try:
        return float(unwrapped) if unwrapped not in (None, "") else None
    except (TypeError, ValueError):
        return None


def copy_cpr_coordinates(cpr: CPR, deal: dict) -> bool:
    """Stamp cpr.gis_lat/gis_lng into *deal* when both are usable (nonzero).

    Returns True when coordinates were copied. Never removes or overwrites
    with empties — rows keep whatever they already had otherwise.
    """
    lat = _coord_float(cpr.get("gis_lat"))
    lng = _coord_float(cpr.get("gis_lng"))
    if lat and lng and lat != 0 and lng != 0:
        deal["latitude"] = lat
        deal["longitude"] = lng
        return True
    return False


# --- FN4-lite: persisted address-quality columns (ADDITIVE schema) ------------
# Every saved row gains four derived columns computed from
# src.addr.normalize_address(address). Existing keys are untouched — the row
# schema only grows. city_norm carries normalize_address's repairs (e.g.
# BALCHSPRINGS → "Balch Springs"); invalid_reason explains any addr_valid=False.
ADDRESS_COLUMN_KEYS = ("zip5", "city_norm", "addr_valid", "invalid_reason")


def address_columns(address) -> dict:
    """The four FN4-lite columns for one raw address string."""
    norm = normalize_address(str(address or ""))
    return {
        "zip5": norm.zip5 or "",
        "city_norm": norm.city or "",
        "addr_valid": bool(norm.valid),
        "invalid_reason": norm.invalid_reason or "",
    }


def stamp_address_columns(deal: dict) -> dict:
    """Append ADDRESS_COLUMN_KEYS to *deal* (mutates and returns it)."""
    deal.update(address_columns(deal.get("address")))
    return deal


# --- FN1: stable identity spine (src.identity shared contract) -----------------
# Every analyzed row gains two ADDITIVE identity columns stamped at deal
# construction time; they replace positional parcel ids as the join key
# everywhere downstream:
#
#   notice_id   — ONE foreclosure notice: sha1 over normalized address +
#                 lender + debt + sale-date. A postponement or re-notice
#                 mints a NEW notice_id by design.
#   property_id — the durable ASSET: county + canonical address. Zip noise
#                 deliberately cannot split one asset into two ids, and the
#                 owner joins the hash ONLY when the address is unparseable
#                 (tiebreaker for otherwise-colliding garbage rows).
#
# Ids are computed from the freshly scraped row values — NOT from
# merge-on-write carry-forward values — so they identify the notice actually
# observed this run while carried-forward enrichment only backfills display
# gaps. The legacy parcel_id column is left byte-for-byte untouched.
IDENTITY_COLUMN_KEYS = ("notice_id", "property_id")


def identity_columns(address, county, lender="", debt=None, sale_date="",
                     owner="") -> dict:
    """The two FN1 identity columns for one row's raw field values."""
    addr_text = str(address or "")
    owner_key = str(owner or "").strip()
    norm = normalize_address(addr_text)
    return {
        "notice_id": notice_id(address=addr_text,
                               lender=str(lender or ""),
                               debt=debt,
                               sale_date=str(sale_date or "")),
        "property_id": property_id(
            str(county or ""), addr_text,
            owner=owner_key if (owner_key and not norm.valid) else ""),
    }


def stamp_identity_columns(deal: dict) -> dict:
    """Append IDENTITY_COLUMN_KEYS to *deal* (mutates and returns it)."""
    deal.update(identity_columns(
        address=deal.get("address"), county=deal.get("county"),
        lender=deal.get("lender"), debt=deal.get("mortgage_debt"),
        sale_date=deal.get("sale_date"), owner=deal.get("owner")))
    return deal


def enrichment_richness(deal: dict) -> int:
    """How many carry-forward enrichment fields hold usable values."""
    return sum(1 for field_name in ENRICHMENT_CARRY_FORWARD_FIELDS
               if not _enrichment_field_is_empty(deal.get(field_name)))


def dedupe_by_property_id(deals: list[dict]) -> list[dict]:
    """Collapse within-run duplicates keyed by the durable property_id.

    Per property_id the RICHEST record survives — most populated
    ENRICHMENT_CARRY_FORWARD_FIELDS; ties keep the first occurrence and
    output order follows first appearance, so the result stays deterministic.
    Rows without a property_id (never produced by stamp_identity_columns,
    but possible in hand-built lists) bypass dedupe untouched.
    """
    winner_by_pid: dict[str, tuple] = {}
    passthrough: dict = {}
    for index, deal in enumerate(deals):
        pid = str(deal.get("property_id") or "")
        if not pid:
            passthrough[index] = deal
            continue
        incumbent = winner_by_pid.get(pid)
        if incumbent is None or \
                enrichment_richness(deal) > enrichment_richness(incumbent[1]):
            winner_by_pid[pid] = (index, deal)
    survivors = dict(passthrough)
    for index, deal in winner_by_pid.values():
        survivors[index] = deal
    dropped = len(deals) - len(survivors)
    if dropped:
        print(f"  Identity dedupe: collapsed {dropped} duplicate row(s); "
              f"{len(winner_by_pid)} distinct property_id(s) kept")
    return [survivors[index] for index in sorted(survivors)]


# --- F7: GeoJSON from flat-record CPRs (coordinate-leak fix) ------------------
# The export used to serialize the RAW prior-dataset CPRs, of which only ~30
# carry coordinates, while 115 rows in current_deals.json were geocoded by
# pipeline.py --geocode-backfill and carried forward by merge_prior_enrichment.
# Building the map from enrich_from_flat_record CPRs (which maps latitude/
# longitude onto gis_lat/gis_lng) restores full coordinate parity, and each
# feature additionally carries the deal-analysis properties below when the
# underlying row has them.
GEO_DEAL_PROPERTIES = ("sale_date", "equity_pct", "lender", "lien_gate",
                       "blast_tier",
                       # Desk-map payload: cad_value sizes the /map circle
                       # markers, parcel_id powers the "Open dossier" deep
                       # link (/deals/{parcel}), address/city_norm carry the
                       # normalized display strings. Absent values stay
                       # omitted — the /map route falls back gracefully.
                       "cad_value", "parcel_id", "address", "city_norm")


def flat_record_has_coordinates(record: dict) -> bool:
    """Mirror of enrich_from_flat_record's coordinate gate for one flat row."""
    lat = _coord_float(record.get("latitude"))
    lng = _coord_float(record.get("longitude"))
    return bool(lat and lng and lat != 0 and lng != 0)


def build_geojson_from_deals(deals: list[dict]) -> dict:
    """GeoJSON FeatureCollection built from enrich_from_flat_record CPRs.

    Feature count therefore matches the number of geocoded flat rows (~115)
    instead of the ~30 coordinate-bearing raw-dataset CPRs (F7). Each feature
    inherits cpr_to_geojson's category/marker properties and is augmented with
    GEO_DEAL_PROPERTIES from its source row where available.

    Geometry repair: cpr_to_geojson reads cpr.gis_lat/gis_lng raw, which
    enrich_from_flat_record wraps in ProvenanceField — the legacy export
    serialized those objects' str() form as coordinates, which no map can
    plot. Because features pair 1:1 (in order) with the coordinate-bearing
    rows, the geometry is re-stamped here as plain [lng, lat] JSON numbers.
    If the base exporter ever drifts from the coordinate gate, features are
    returned unaugmented with a loud warning rather than mis-paired.
    """
    cprs = [enrich_from_flat_record(d) for d in deals]
    fc = cpr_to_geojson(cprs)
    coord_deals = [d for d in deals if flat_record_has_coordinates(d)]
    features = fc.get("features") or []
    if len(features) != len(coord_deals):
        print(f"  ⚠️ GeoJSON parity drift: {len(features)} feature(s) vs "
              f"{len(coord_deals)} geocoded row(s) — deal properties skipped")
        return fc
    for feature, deal in zip(features, coord_deals):
        props = feature.setdefault("properties", {})
        for key in GEO_DEAL_PROPERTIES:
            value = deal.get(key)
            if value not in (None, "", "Unknown"):
                props[key] = value
        # Plain-number geometry (see docstring): [lng, lat], GeoJSON order.
        lat = _coord_float(deal.get("latitude"))
        lng = _coord_float(deal.get("longitude"))
        if lat is not None and lng is not None:
            geometry = feature.setdefault("geometry", {})
            if geometry.get("type") == "Point":
                geometry["coordinates"] = [lng, lat]
    return fc


def load_prior_dataset():
    """Load the 335-record lucid-noether dataset and enrich each into a CPR."""
    print("\n[DATASET] Loading prior 335-record foreclosure dataset...")
    if not os.path.exists(LUCID_DATA):
        # F1: fail loudly but gracefully — no traceback.
        print(f"  ✗ Dataset not found: {LUCID_DATA}")
        print("    Fix: set HERMES_LUCID_DATA to your foreclosure_report.json path,")
        print("    or restore the default lucid-noether checkout, then re-run.")
        sys.exit(1)
    with open(LUCID_DATA) as f:
        records = json.load(f)
    print(f"  ✓ Loaded {len(records)} records ({sum(1 for r in records if r.get('county') == 'Collin')} Collin, {sum(1 for r in records if r.get('county') == 'Dallas')} Dallas)")

    cprs = []
    for i, record in enumerate(records, 1):
        cpr = enrich_from_raw_record(record)
        cprs.append(cpr)
    print(f"  ✓ Enriched {len(cprs)} CPRs")
    return cprs


def scrape_live_collin(limit=5):
    """Live scrape Collin County for current notices."""
    print(f"\n[COLLIN] Live scraping foreclosure portal (limit={limit})...")
    adapter = CollinForeclosureAdapter()
    try:
        leads = adapter.fetch(limit=limit, extract_details=True)
        print(f"  ✓ Sourced {len(leads)} live leads")
        cprs = [enrich_lead(l) for l in leads]
        return cprs
    except Exception as e:
        print(f"  ✗ Collin scrape failed: {e}")
        return []


def analyze_deal(cpr: CPR) -> dict:
    """Analyze a single deal — everything we know, what's blocked.

    TD1: every provenance-managed key is read through CPR.get() (the
    provenance-unwrapping accessor over declared fields), replacing the old
    ``cpr.f.value if cpr.f else default`` hasattr walls. Semantics are
    unchanged, including the edge where a receipted field holds None
    (CPR.get unwraps it to None rather than substituting the default).
    """
    addr = cpr.situs_address
    # F3: CPR may or may not carry a parsed city field — fall back to get_city().
    city = cpr.get("city") or get_city(addr)
    sqft = cpr.get("improvement_sqft")
    beds = cpr.get("beds")
    baths = cpr.get("baths")
    yr_built = cpr.get("year_built")
    stories = cpr.get("stories")
    pool = cpr.get("pool")
    cad_val = cpr.get("cad_appraised_value")
    cad_val_2025 = cpr.get("cad_appraised_value_2025")
    owner = cpr.get("owner_name", "Unknown")
    ptype = cpr.get("property_type", "Unknown")
    sale_date = cpr.get("sale_date", "Unknown")
    isd = cpr.get("isd", "Unknown")
    lender = cpr.get("foreclosing_lender", "Unknown")
    trustee = cpr.get("substitute_trustee", "Unknown")
    sale_time = cpr.get("scheduled_sale_time", "Unknown")
    orig_principal = cpr.get("original_principal")
    unpaid_principal = cpr.get("unpaid_principal_balance")
    all_balances = cpr.get("all_extracted_balances", [])
    census_tract = cpr.get("census_tract")
    county_fips = cpr.get("county_fips")
    commercial = cpr.get("commercial", False)
    deed_number = cpr.get("deed_number")

    # Debt from liens
    mortgage_debt = sum(l.amount or 0 for l in cpr.liens if l.lien_type == "mortgage")

    # Lien gate
    lien_gate = gate_legal_lien(cpr)

    # GATE-VAL — CAD-only, will fail (non-disclosure)
    val_status = "REJECTED (assessed-value-only — non-disclosure state)"
    if not cad_val:
        val_status = "NO DATA"

    # Preliminary MAO using 70% rule on CAD (directional only)
    preliminary_mao = None
    rough_rehab = 0
    if cad_val and sqft:
        rough_rehab = sqft * (20 if (yr_built and yr_built < 2010) else 10)
        preliminary_mao = mao(cad_val, rough_rehab, 0.70)

    # Equity / LTV trio — shared kernel (TD8), historical inline math.
    leverage = compute_leverage_metrics(cad_val, mortgage_debt)

    # Valuation change 2025→2026
    val_change = None
    if cad_val and cad_val_2025 and cad_val_2025 > 0:
        val_change = ((cad_val - cad_val_2025) / cad_val_2025) * 100

    # Blast tier
    capital = preliminary_mao or 0
    tier = assign_blast_tier(capital)

    deal = {
        "address": addr, "city": city, "county": cpr.county,
        "parcel_id": cpr.parcel_id, "owner": owner, "property_type": ptype,
        "sqft": sqft, "beds": beds, "baths": baths, "year_built": yr_built,
        "stories": stories, "pool": pool, "commercial": commercial,
        "cad_value": cad_val, "cad_value_2025": cad_val_2025,
        "price_per_sqft": (cad_val / sqft) if (cad_val and sqft) else None,
        "val_change_pct": val_change,
        "isd": isd, "deed_number": deed_number,
        "mortgage_debt": mortgage_debt if mortgage_debt > 0 else None,
        "lender": lender if lender != "Unknown" else None,
        "trustee": trustee if trustee != "Unknown" else None,
        "sale_date": sale_date, "sale_time": sale_time,
        "original_principal": orig_principal,
        "unpaid_principal": unpaid_principal,
        "all_balances": all_balances,
        "census_tract": census_tract, "county_fips": county_fips,
        "equity_estimate": leverage["equity_estimate"],
        "equity_pct": leverage["equity_pct"],
        "ltv_pct": leverage["ltv_pct"],
        "preliminary_mao": preliminary_mao,
        "rough_rehab": rough_rehab if rough_rehab > 0 else None,
        "blast_tier": tier.name,
        "lien_gate": lien_gate.verdict,
        "val_gate": val_status,
        "hitl_required": "MLS/appraiser comp pull for real ARV",
    }
    # Address-quality gate: stamp implausible scrape garbage so ranking code
    # can skip it; the saved record itself stays untouched (schema included).
    return mark_invalid_query(deal)


def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    print("=" * 70)
    print("  HERMES-REO — FULL DEAL FINDER")
    print(f"  Run time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # Load prior dataset (335 records with full enrichment)
    cprs = load_prior_dataset()

    # Also live scrape Collin for current notices (HERMES_SKIP_LIVE=1 skips)
    live_cprs = fetch_live_cprs(limit=5)

    # Merge: prior dataset + live (dedupe by parcel_id)
    seen_ids = {c.parcel_id for c in cprs}
    for c in live_cprs:
        if c.parcel_id not in seen_ids:
            cprs.append(c)
            seen_ids.add(c.parcel_id)

    print(f"\nTotal CPRs: {len(cprs)} ({len(cprs) - len(live_cprs)} from prior dataset + {len(live_cprs)} live)")

    # Analyze each deal
    print("\n" + "=" * 70)
    print("  DEAL ANALYSIS")
    print("=" * 70)

    deals = []
    for i, cpr in enumerate(cprs, 1):
        deal = analyze_deal(cpr)
        deals.append(deal)

    # F7 companion: carry CPR-side coordinates (live Collin detail / Dallas
    # census geocoder) into the flat row so they survive the switch of the
    # GeoJSON export to flat records below.
    coords_carried = sum(copy_cpr_coordinates(cpr, deal)
                         for cpr, deal in zip(cprs, deals))
    if coords_carried:
        print(f"  Coordinate passthrough: {coords_carried} row(s) gained "
              f"latitude/longitude from their CPR")

    # FN1: stamp the stable identity spine onto every analyzed row (additive
    # notice_id/property_id columns; legacy parcel_id untouched), then
    # collapse within-run duplicates by durable asset id, keeping whichever
    # record carries the most enrichment.
    for deal in deals:
        stamp_identity_columns(deal)
    deals = dedupe_by_property_id(deals)

    # Print summary table for all deals with debt
    # Address-quality tally: malformed records stay in the saved dataset but
    # are skipped by every TOP table / equity list below.
    print(f"\n  Address quality gate: "
          f"{count_malformed_addresses(deals)} addresses skipped as malformed.")

    deals_with_debt = [d for d in deals
                       if d["mortgage_debt"] and has_plausible_address(d)]
    deals_with_debt.sort(key=lambda d: d["mortgage_debt"], reverse=True)

    print(f"\n  Total deals: {len(deals)}")
    print(f"  With extracted debt: {len(deals_with_debt)}")
    print(f"  With property details (sqft): {sum(1 for d in deals if d['sqft'])}")
    print(f"  With census demographics: {sum(1 for d in deals if d['census_tract'])}")

    # Print top 20 deals by debt
    print(f"\n  TOP 20 DEALS BY DEBT (full data):")
    print(f"  {'#':>3} {'Address':<45} {'City':<15} {'Debt':>12} {'CAD':>12} {'LTV':>6} {'Equity':>12} {'Lender'}")
    print(f"  {'':>3} {'':45} {'':15} {'':12} {'':12} {'':6} {'':12} {'':30}")
    for i, d in enumerate(deals_with_debt[:20], 1):
        addr_short = d["address"][:43] if d["address"] else "Unknown"
        city_short = (d["city"] or "Unknown")[:13]
        debt_str = f"${d['mortgage_debt']:>10,.0f}" if d["mortgage_debt"] else "       N/A"
        cad_str = f"${d['cad_value']:>10,.0f}" if d["cad_value"] else "       N/A"
        ltv_str = f"{d['ltv_pct']:>5.0f}%" if d["ltv_pct"] else "   N/A"
        eq_str = f"${d['equity_estimate']:>10,.0f}" if d["equity_estimate"] is not None else "       N/A"
        lender_short = (d["lender"] or "Unknown")[:28]
        print(f"  {i:>3} {addr_short:<45} {city_short:<15} {debt_str} {cad_str} {ltv_str} {eq_str} {lender_short}")

    # Print deals with full property details
    deals_with_details = [d for d in deals
                          if d["sqft"] and has_plausible_address(d)]
    print(f"\n  DEALS WITH FULL PROPERTY DETAILS ({len(deals_with_details)}):")
    for i, d in enumerate(deals_with_details[:10], 1):
        print(f"\n  {i}. {d['address']}")
        print(f"     {d['county'].title()} | {d['city']} | {d['property_type']}")
        print(f"     {d['sqft']:,} sqft | {d['beds']} bed/{d['baths']} bath | Built {d['year_built']} | {d['stories']} stories | Pool: {d['pool']}")
        if d["cad_value"]:
            pps = f" (${d['price_per_sqft']:.0f}/sqft)" if d["price_per_sqft"] else ""
            print(f"     CAD: ${d['cad_value']:,.0f}{pps}" + (f" (2025: ${d['cad_value_2025']:,.0f}, change: {d['val_change_pct']:+.1f}%)" if d['val_change_pct'] is not None else ""))
        if d["mortgage_debt"] and d.get("ltv_pct") is not None:
            lender_str = d.get("lender") or "Unknown"
            print(f"     Debt: ${d['mortgage_debt']:,.0f} ({lender_str}) | LTV: {d['ltv_pct']:.0f}%")
            if d.get("equity_estimate") is not None and d.get("equity_pct") is not None:
                eq = d["equity_estimate"]
                eq_str = f"${eq:,.0f}" if eq >= 0 else f"-${abs(eq):,.0f} (UNDERWATER)"
                print(f"     Equity: {eq_str} ({d['equity_pct']:.0f}%)")
        if d["isd"] != "Unknown":
            print(f"     ISD: {d['isd']}")
        if d["census_tract"]:
            print(f"     Census: tract {d['census_tract']}, FIPS {d['county_fips']}")
        if d["preliminary_mao"]:
            print(f"     Preliminary MAO (70% rule): ${d['preliminary_mao']:,.0f} (rehab est: ${d['rough_rehab']:,.0f})")
        print(f"     Trustee: {d['trustee']} | Sale: {d['sale_time']}")
        print(f"     Blast tier: {d['blast_tier']} | Lien gate: {d['lien_gate']} | VAL gate: {d['val_gate']}")

    # Portfolio summary
    print("\n" + "=" * 70)
    print("  PORTFOLIO SUMMARY")
    print("=" * 70)
    stats = portfolio_stats(cprs)
    report = render_stats_report(stats)
    print(report)

    # Save outputs
    output_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(output_dir, exist_ok=True)

    deals_file = os.path.join(output_dir, "current_deals.json")
    # B2: merge prior enrichment BEFORE the atomic write so bulk-DCAD /
    # geocode-backfill results survive this regeneration (see the
    # MERGE-ON-WRITE comment above).
    merge_prior_enrichment(deals, deals_file)
    # FN4-lite: append the additive address-quality columns (zip5, city_norm,
    # addr_valid, invalid_reason) derived from src.addr.normalize_address.
    for deal in deals:
        stamp_address_columns(deal)
    # Address-quality gate: the invalid_query marker is runtime-only. Strip it
    # so the persisted row schema is legacy keys + the FN4-lite columns only.
    deals_payload = strip_invalid_query_markers(deals)
    deals_path = write_json(deals_file, deals_payload, default=str)

    # FN2: snapshot this regeneration into the history store (atomic daily
    # file + derived notices block) and refresh the cross-run index.
    run_date = datetime.now(timezone.utc).date().isoformat()
    history_path, churn_line = update_history_store(
        os.path.join(output_dir, "history"), run_date, deals_payload)
    print(f"  Saved: {history_path}")
    print(f"  History churn: {churn_line}")

    stats_path = write_text(os.path.join(output_dir, "live_portfolio_stats.md"), report)

    # F7: build the map from flat-record CPRs so feature count matches the
    # geocoded rows in current_deals.json (was ~30 raw-CPR features vs 115
    # geocoded rows), with richer per-feature deal properties.
    geojson_payload = build_geojson_from_deals(deals_payload)
    geojson_path = write_json(os.path.join(output_dir, "live_foreclosures.geojson"),
                              geojson_payload, default=str)
    n_features = len(geojson_payload["features"])

    print(f"\n  Saved: {deals_path} ({len(deals)} deals)")
    print(f"  Saved: {stats_path}")
    print(f"  Saved: {geojson_path} ({n_features} features)")

    # Final summary
    print("\n" + "=" * 70)
    print("  DEAL FLOW SUMMARY")
    print("=" * 70)
    total = len(deals)
    with_equity = sum(1 for d in deals if d["equity_estimate"] and d["equity_estimate"] > 0)
    with_debt = sum(1 for d in deals if d["mortgage_debt"])
    underwater = sum(1 for d in deals if d["equity_estimate"] is not None and d["equity_estimate"] < 0)
    has_sqft = sum(1 for d in deals if d["sqft"])
    has_census = sum(1 for d in deals if d["census_tract"])
    has_lender = sum(1 for d in deals if d["lender"])
    has_trustee = sum(1 for d in deals if d["trustee"])
    has_orig_principal = sum(1 for d in deals if d["original_principal"])

    print(f"  Total deals: {total}")
    print(f"  With property details (sqft/beds/baths): {has_sqft}")
    print(f"  With extracted debt: {with_debt}")
    print(f"  With foreclosing lender: {has_lender}")
    print(f"  With substitute trustee: {has_trustee}")
    print(f"  With original principal: {has_orig_principal}")
    print(f"  With census demographics: {has_census}")
    print(f"  With positive equity (CAD - debt): {with_equity}")
    print(f"  Underwater (debt > CAD): {underwater}")
    print(f"  All require MLS/appraiser HITL for real ARV (non-disclosure state)")

    # Top equity prospects — malformed addresses excluded via the gate
    # (rankable_deals inside top_equity_prospects).
    equity_deals = top_equity_prospects(deals)
    if equity_deals:
        print(f"\n  TOP 10 EQUITY PROSPECTS (CAD-based, needs MLS verification):")
        for i, d in enumerate(equity_deals, 1):
            print(f"  {i:>2}. {d['address'][:40]:<42} equity ~${d['equity_estimate']:>10,.0f} ({d['equity_pct']:>3.0f}%) | debt ${d['mortgage_debt']:>10,.0f} | {d['lender'] or 'Unknown'}")

    print("\n" + "=" * 70)
    print("  RUN COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()