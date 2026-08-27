# src/comps.py
"""Shared comp store (OPPORTUNITY_MAP F2).

One JSON file per property under ``data/comps/{property_id}.json``:

    {"property_id": "...", "updated_at": "<iso-8601>", "comps": [ {...}, ... ]}

Each entry of ``comps`` is a CompRecord-shaped dict (src.cpr.CompRecord
fields), so what src.hitl intake validates is byte-compatible with what this
store persists and re-seeds. The store lets one agent's hand-collected comp
set be reused by every later notice on the SAME asset instead of being
trapped inside a single *.valued.json.

Identity (FN1 spine): store keys prefer src.identity.property_id(county,
address, owner); when the county is unknown we fall back to a sha1 of the
normalized address so an asset still gets ONE stable key.

Every write goes through src.io_utils.write_json (atomic tmp+fsync+replace)
under file_lock() so concurrent HITL runs cannot drop each other's comps;
every read is tolerant — a missing/corrupt/foreign-shaped entry degrades to
None (or []) with a stderr warning, never a crash mid-batch.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from typing import Any, Optional

try:
    from .addr import normalize_address
    from .cpr import CompRecord
    from .io_utils import file_lock, write_json
    from .policy import MAX_COMP_AGE_DAYS, MAX_COMP_DISTANCE_MI
except ImportError:  # direct-script execution (`python src/comps.py`)
    from addr import normalize_address  # type: ignore
    from cpr import CompRecord  # type: ignore
    from io_utils import file_lock, write_json  # type: ignore
    from policy import MAX_COMP_AGE_DAYS, MAX_COMP_DISTANCE_MI  # type: ignore

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE_DIR = os.path.join(REPO_ROOT, "data", "comps")

ENV_STORE_DIR = "HERMES_COMPS_DIR"  # hermetic override for tests/operators


def _warn(msg: str) -> None:
    print(f"comps: WARNING {msg}", file=sys.stderr)


def default_store_dir() -> str:
    """Store root for this invocation: $HERMES_COMPS_DIR else data/comps."""
    override = os.environ.get(ENV_STORE_DIR, "").strip()
    return override or DEFAULT_STORE_DIR


# --- identity -----------------------------------------------------------------

def _sha(text: str, n: int = 12) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def _norm_component(value: Any) -> str:
    s = str(value or "").strip().upper()
    return re.sub(r"\s+", " ", s)


def derive_property_id(county: str, address: str, owner: str = "") -> str:
    """Stable store key for an asset (F2 identity rule).

    Prefers the FN1 ``property_id`` when a county is known; without a county
    we still need ONE durable key, so hash the normalized address ("RAW"
    namespace keeps those ids visibly distinct from county-anchored ones).
    The owner tiebreaker is only folded in when explicitly supplied, so
    owner-name drift across scrapes cannot silently split a store entry.
    """
    try:
        from .identity import property_id
    except ImportError:  # direct-script execution
        from identity import property_id  # type: ignore

    if str(county or "").strip():
        return property_id(county, address, owner)
    n = normalize_address(address)
    geo = (f"{n.street_number} {n.street_name} {n.street_type}".strip()
           if getattr(n, "valid", False) else _norm_component(address))
    base = f"RAW|{geo}"
    if owner:
        base += "|O:" + _norm_component(owner)
    return "P" + _sha(base)


def store_key_for_cpr(cpr: Any, explicit_pid: Optional[str] = None) -> str:
    """Resolve the store key for *cpr*: explicit --property-id wins, then any
    cpr.property_id attribute, then county+situs derivation."""
    if explicit_pid:
        return sanitize_pid(explicit_pid)
    attr_pid = getattr(cpr, "property_id", None)
    if attr_pid:
        return sanitize_pid(str(attr_pid))
    return derive_property_id(getattr(cpr, "county", ""),
                              getattr(cpr, "situs_address", ""))


def sanitize_pid(pid: str) -> str:
    """Filesystem-safe id; FN1 ids are alnum already, this guards hand input."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(pid or "").strip())
    if not safe:
        raise ValueError("empty property_id — cannot key a comp-store entry")
    return safe


# --- record coercion ----------------------------------------------------------

def comp_to_dict(comp: Any) -> dict:
    """CompRecord (or already-dict) -> plain CompRecord-shaped dict."""
    if isinstance(comp, dict):
        return {k: comp.get(k) for k in (
            "address", "sale_price", "sale_date", "distance_miles",
            "sqft", "beds", "baths", "source", "adjusted_price")}
    return {
        "address": getattr(comp, "address", ""),
        "sale_price": getattr(comp, "sale_price", None),
        "sale_date": getattr(comp, "sale_date", None),
        "distance_miles": getattr(comp, "distance_miles", None),
        "sqft": getattr(comp, "sqft", None),
        "beds": getattr(comp, "beds", None),
        "baths": getattr(comp, "baths", None),
        "source": getattr(comp, "source", ""),
        "adjusted_price": getattr(comp, "adjusted_price", None),
    }


def coerce_comp(raw: Any) -> Optional[CompRecord]:
    """Tolerant stored-dict -> CompRecord; returns None for junk entries."""
    if not isinstance(raw, dict):
        return None
    address = str(raw.get("address") or "").strip()
    if not address:
        return None

    def _opt_float(key):
        val = raw.get(key)
        if val in (None, ""):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    sqft = _opt_float("sqft")
    beds = _opt_float("beds")
    return CompRecord(
        address=address,
        sale_price=_opt_float("sale_price"),
        sale_date=(str(raw["sale_date"]) if raw.get("sale_date") else None),
        distance_miles=_opt_float("distance_miles"),
        sqft=(int(sqft) if sqft is not None else None),
        beds=(int(beds) if beds is not None else None),
        baths=_opt_float("baths"),
        source=str(raw.get("source") or ""),
        adjusted_price=_opt_float("adjusted_price"),
    )


def _dedupe_key(comp_dict: dict) -> tuple:
    """Merge key so repeated saves accumulate instead of duplicating rows."""
    addr = _norm_component(comp_dict.get("address"))
    raw_date = str(comp_dict.get("sale_date") or "")
    try:
        from .dates import parse_date_any
    except ImportError:  # direct-script execution
        from dates import parse_date_any  # type: ignore
    parsed = parse_date_any(raw_date) if raw_date else None
    sale_date = parsed.isoformat() if parsed else raw_date.upper()
    price = comp_dict.get("sale_price")
    try:
        price_key = round(float(price), 2) if price not in (None, "") else None
    except (TypeError, ValueError):
        price_key = price
    return (addr, sale_date, price_key)


def merge_comp_records(records: list[Any]) -> list[dict]:
    """Order-preserving dedupe across CompRecords/dicts (first occurrence wins)."""
    merged: list[dict] = []
    seen = set()
    for rec in records:
        d = comp_to_dict(rec)
        if not str(d.get("address") or "").strip():
            continue
        key = _dedupe_key(d)
        if key in seen:
            continue
        seen.add(key)
        merged.append(d)
    return merged


# --- store primitives ---------------------------------------------------------

def entry_path(property_id: str, comps_dir: Optional[str] = None) -> str:
    directory = comps_dir or default_store_dir()
    return os.path.join(directory, sanitize_pid(property_id) + ".json")


def load_entry(property_id: str,
               comps_dir: Optional[str] = None) -> Optional[dict]:
    """Full stored entry or None (missing / corrupt / foreign shape)."""
    path = entry_path(property_id, comps_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, ValueError) as exc:
        _warn(f"{path}: unreadable comp-store entry ({exc!r}) — ignoring")
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("comps"), list):
        _warn(f"{path}: not a comp-store entry shape — ignoring")
        return None
    return entry


def load_comps(property_id: str,
               comps_dir: Optional[str] = None) -> Optional[list[dict]]:
    """Stored comps list for *property_id*; None when absent/unusable."""
    entry = load_entry(property_id, comps_dir)
    if entry is None:
        return None
    return [c for c in entry["comps"] if isinstance(c, dict)]


def save_comps(property_id: str, comps: list[Any],
               comps_dir: Optional[str] = None,
               updated_at: Optional[str] = None) -> str:
    """Merge *comps* into the store entry for *property_id* (atomic).

    Existing rows are kept verbatim; incoming rows are appended unless an
    identical (address, sale_date, sale_price) row is already present, so
    re-running a HITL pass never duplicates. Writes happen under file_lock()
    with io_utils' atomic replace; on platforms without flock we degrade to
    the still-crash-safe unlocked atomic write rather than refusing to save.
    """
    if not isinstance(comps, list):
        raise TypeError(f"save_comps expects a list, got {type(comps).__name__}")
    pid = sanitize_pid(property_id)
    directory = comps_dir or default_store_dir()
    path = entry_path(pid, directory)

    existing: list[dict] = []
    prior = load_entry(pid, comps_dir)
    if prior is not None:
        existing = [c for c in prior["comps"] if isinstance(c, dict)]

    incoming = merge_comp_records(comps)
    existing_keys = {_dedupe_key(c) for c in existing}
    added = [d for d in incoming if _dedupe_key(d) not in existing_keys]
    merged_entry = {
        "property_id": pid,
        "updated_at": updated_at or datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "comps": existing + added,
    }

    payload = dict(merged_entry)
    try:
        with file_lock(path, timeout=10.0):
            write_json(path, payload)
    except RuntimeError:
        # No flock support (or lock timeout surfaced as RuntimeError): the
        # atomic replace alone still guarantees no truncated entry.
        write_json(path, payload)
    if added:
        _warn(f"{pid}: +{len(added)} comp(s) stored at {path}")
    else:
        _warn(f"{pid}: no new comps (all duplicates) — entry refreshed at {path}")
    return path


def list_properties(comps_dir: Optional[str] = None) -> list[str]:
    """All property_ids currently in the store (tolerant, sorted)."""
    directory = comps_dir or default_store_dir()
    if not os.path.isdir(directory):
        return []
    pids: set = set()
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        stem = name[:-len(".json")]
        entry = load_entry(stem, comps_dir)
        if entry is None:
            _warn(f"skipping unusable store file {name}")
            continue
        pids.add(str(entry.get("property_id") or stem))
    return sorted(pids)


# --- seeding ------------------------------------------------------------------

def filter_usable(records: list[CompRecord],
                  as_of: Optional[date] = None) -> list[CompRecord]:
    """Keep comps within MAX_COMP_DISTANCE_MI / MAX_COMP_AGE_DAYS.

    Mirrors valuation._filter_usable_comps semantics exactly (same policy
    constants, unknown distance/date KEPT because they cannot be proven bad
    — scoring penalizes them instead); tests/test_comps_platform.py asserts
    parity so the two can never drift. Implemented inline rather than
    imported because valuation uses package-relative imports and this module
    must also run as a bare script.
    """
    try:
        from .dates import parse_date_any
    except ImportError:  # direct-script execution
        from dates import parse_date_any  # type: ignore

    as_of = as_of or date.today()
    usable = []
    for c in records:
        dist = getattr(c, "distance_miles", None)
        if dist is not None and dist > MAX_COMP_DISTANCE_MI:
            continue
        parsed = parse_date_any(getattr(c, "sale_date", None))
        if parsed is not None and (as_of - parsed).days > MAX_COMP_AGE_DAYS:
            continue
        usable.append(c)
    return usable




def suggest_nearby_seeds(subject_address: str, subject_county: str,
                         comps_dir: Optional[str] = None,
                         radius_mi: float = 0.5, limit: int = 10,
                         geocode_fn=None) -> list[dict]:
    """Audit HITL-4: neighborhood-radius comp seeding (SUGGESTIONS ONLY).

    best_seed_for is exact-identity keyed; F2's stated goal was amortizing
    one comp pull across a SUBDIVISION. This scans every store entry,
    geocodes the candidate addresses (census geocoder by default - network,
    opt-in by flag), and returns entries with a usable sale inside
    *radius_mi* of the subject, labeled as suggestions. Nothing here writes
    the store or attaches a valuation - the human still curates.
    """
    try:
        from .adapters.census import geocode_address
        from .adapters.route_optimizer import haversine
    except ImportError:
        from adapters.census import geocode_address
        from adapters.route_optimizer import haversine

    geocode_fn = geocode_fn or geocode_address
    subject = geocode_fn(subject_address)
    if subject.get("status") != "OK" or "lat" not in subject:
        return []
    sub_lat, sub_lng = float(subject["lat"]), float(subject["lng"])

    seen: set = set()
    suggestions: list[dict] = []
    for pid in list_properties(comps_dir):
        if pid in seen:
            continue
        seen.add(pid)
        comps = load_comps(pid, comps_dir)
        for comp in comps:
            addr = getattr(comp, "address", "")
            if not addr or getattr(comp, "sale_price", None) is None:
                continue
            coords = geocode_fn(addr)
            if (coords.get("status") != "OK" and "lat" not in coords) \
                    or coords.get("lat") is None:
                continue
            dist = haversine(sub_lat, sub_lng,
                             float(coords["lat"]), float(coords["lng"]))
            if dist > radius_mi:
                continue
            suggestions.append({
                "source_property_id": pid,
                "address": addr,
                "sale_price": getattr(comp, "sale_price", None),
                "sale_date": getattr(comp, "sale_date", None),
                "distance_miles": round(dist, 3),
                "source": getattr(comp, "source", ""),
                "suggestion": True,  # never auto-accepted
            })
            if len(suggestions) >= limit:
                return suggestions
    return suggestions


def best_seed_for(cpr: Any, comps_dir: Optional[str] = None,
                  as_of: Optional[date] = None,
                  pid: Optional[str] = None) -> list[CompRecord]:
    """Stored comps reusable as GATE-VAL seeds for *cpr* (F2).

    Resolves the store key exactly like persistence does (explicit *pid* >
    cpr.property_id attribute > county+situs derivation), loads tolerantly
    (no entry => []), coerces junk rows away, then applies the shared
    0.5mi/180d usability filter.
    """
    key = sanitize_pid(pid) if pid else store_key_for_cpr(cpr)
    stored = load_comps(key, comps_dir)
    if not stored:
        return []
    records = [r for r in (coerce_comp(c) for c in stored) if r is not None]
    return filter_usable(records, as_of)


# --- self-check ---------------------------------------------------------------

def self_check() -> None:
    """Offline end-to-end check of store + seed semantics (tmp dir only)."""
    import tempfile
    import shutil
    from types import SimpleNamespace

    tmpdir = tempfile.mkdtemp(prefix="comps_selfcheck_")
    try:
        store = os.path.join(tmpdir, "store")

        # Identity: county-anchored preferred, no-county fallback stable.
        pid_county = derive_property_id("grayson",
                                        "900 Test Ave, Sherman, TX 75090")
        assert pid_county.startswith("P") and len(pid_county) == 13, pid_county
        assert derive_property_id(
            "GRAYSON", "900  Test AVE, Sherman, TX 75090-0000") == pid_county, \
            "county case/address noise must not split the asset"
        pid_fallback_a = derive_property_id("", "900 Test Ave, Sherman, TX")
        pid_fallback_b = derive_property_id(None, "900 TEST AVE, SHERMAN, TX")
        assert pid_fallback_a == pid_fallback_b, "fallback must be normalized"
        assert pid_fallback_a != pid_county

        base = {"address": "101 Neighbor St", "sale_price": 300_000.0,
                "sale_date": "2026-08-01", "distance_miles": 0.3,
                "source": "mls_agent"}
        fresh = dict(base)
        near = dict(base, address="102 Neighbor St", distance_miles=0.4)
        far = dict(base, address="103 Neighbor St", distance_miles=0.9)
        stale = dict(base, address="104 Neighbor St", sale_date="2025-01-01")

        save_comps(pid_county, [fresh], comps_dir=store)
        save_comps(pid_county, [near], comps_dir=store)      # accumulates
        save_comps(pid_county, [dict(fresh)], comps_dir=store)  # dup: no growth
        entry = load_entry(pid_county, store)
        assert entry is not None and entry["property_id"] == pid_county
        assert len(entry["comps"]) == 2, entry["comps"]
        loaded = load_comps(pid_county, store)
        assert loaded is not None and len(loaded) == 2

        assert list_properties(store) == [pid_county]

        # Seed selection honors the shared 0.5mi / 180d filters.
        cpr_like = SimpleNamespace(
            county="grayson", situs_address="900 Test Ave, Sherman, TX 75090")
        save_comps(pid_county, [far, stale], comps_dir=store)
        seeds = best_seed_for(cpr_like, comps_dir=store)
        addrs = {s.address for s in seeds}
        assert addrs == {"101 Neighbor St", "102 Neighbor St"}, addrs
        empty = best_seed_for(SimpleNamespace(county="", situs_address="nowhere"),
                              comps_dir=store)
        assert empty == []

        # Tolerant reads: corrupt file degrades to None, batch keeps running.
        corrupt = entry_path(pid_fallback_a, store)
        with open(corrupt, "w", encoding="utf-8") as f:
            f.write("{not json")
        assert load_comps(pid_fallback_a, store) is None
        assert list_properties(store) == [pid_county]

        # Missing entry is None, not an exception.
        assert load_comps("P000000000000", store) is None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    self_check()
    print("comps.py: comp-store self-check passed")
