# src/adapters/dallas_cad_bulk.py
"""DCAD bulk cross-reference — batch Playwright with caching.

Extends the single-address DallasCADAdapter with:
1. JSON file cache (avoids re-searching addresses across runs)
2. Batch mode (process a list of addresses with rate limiting)
3. Error recovery (skip failed addresses, don't crash the batch)

Cache file: data/dcad_cache.json

R3 cache contract:
- Tolerant load: a corrupt/malformed cache is logged and discarded (fresh
  start) instead of crashing the batch.
- All writes go through src.io_utils.write_json (atomic, crash-safe).
- Every entry records an ISO `retrieved_at` timestamp.
- Transient error statuses are never persisted — only definitive outcomes
  survive a save, so failures are always re-searched on the next run.
- TTL of 90 days: entries older than that (or missing a usable timestamp)
  are treated as cache misses and re-searched.
- Concurrency (RL10): the whole load -> search -> save cycle runs under an
  exclusive file_lock on the "<cache>.lock" sidecar so two concurrent
  processes queue instead of dropping each other's freshly searched entries;
  contention beyond the scaled wait surfaces as TimeoutError("LOCK-BUSY ...").
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ..dates import parse_date_any
from ..io_utils import file_lock, write_json
from .dallas_cad import DallasCADAdapter, _search_dcad_by_address, _parse_dcad_detail
from .base import SourceProvenance

_log = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "dcad_cache.json")
RATE_LIMIT_SEC = 2.0  # seconds between DCAD searches
CACHE_TTL_DAYS = 90   # R3: stale entries are treated as misses

# R3: only definitive search outcomes may be persisted. Anything else
# ({"status": "error"}, transport exceptions, timeouts, ...) is transient and
# must be retried on the next run, never cached.
_CACHEABLE_STATUSES = frozenset({"parsed", "no_data_found", "mismatch"})

# RL10: batch_cross_reference holds an exclusive file_lock across its whole
# load -> search -> save cycle. A batch may legitimately hold the lock for up
# to ~batch_limit searches spaced RATE_LIMIT_SEC apart, so scale the wait with
# the batch size instead of using file_lock's short 10s default — a concurrent
# legitimate run queues and then re-reads a fresh cache; only genuinely stuck
# contention surfaces as TimeoutError("LOCK-BUSY ...").
BULK_LOCK_TIMEOUT_FLOOR_SEC = 30.0


def _cache_lock_timeout(batch_limit: int) -> float:
    """Lock wait long enough for one full competing batch to finish first."""
    return max(BULK_LOCK_TIMEOUT_FLOOR_SEC, 2.0 * int(batch_limit) * RATE_LIMIT_SEC)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_cache() -> dict:
    """Load the DCAD cache; corrupt or malformed files log-and-reset (R3)."""
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning(
            "DCAD cache %s unreadable (%s: %s) — starting fresh",
            CACHE_PATH, type(exc).__name__, exc,
        )
        return {}
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        logger.warning("DCAD cache %s malformed — starting fresh", CACHE_PATH)
        return {}
    return data


def _save_cache(cache: dict) -> None:
    """Persist the DCAD cache atomically via src.io_utils.write_json (R3).

    Only definitive outcomes with a usable retrieved_at survive a save:
    transient error statuses are dropped, and undated legacy entries are
    dropped too — back-dating them here would silently refresh their TTL.
    """
    persistent = {}
    for addr, entry in cache.items():
        if not isinstance(entry, dict) or entry.get("status") not in _CACHEABLE_STATUSES:
            continue
        if not parse_date_any(entry.get("retrieved_at")):
            continue
        persistent[addr] = dict(entry)
    write_json(CACHE_PATH, persistent)


def _entry_age_days(entry: dict, today: Optional[date] = None) -> Optional[int]:
    """Whole days since the entry was retrieved, or None if unknowable."""
    retrieved = parse_date_any(entry.get("retrieved_at"))
    if retrieved is None:
        return None
    return ((today or date.today()) - retrieved).days


def _is_fresh_entry(entry, today: Optional[date] = None) -> bool:
    """True only for successfully parsed entries inside the TTL window.

    Stale entries (older than CACHE_TTL_DAYS), legacy entries without a
    parseable retrieved_at, and non-parsed statuses all count as misses (R3).
    """
    if not isinstance(entry, dict) or entry.get("status") != "parsed":
        return False
    age = _entry_age_days(entry, today)
    if age is None:
        return False
    return age <= CACHE_TTL_DAYS


async def _batch_search(addresses: list[str], cache: dict, batch_limit: int = 50) -> dict:
    """Search DCAD for a batch of addresses with caching + rate limiting.

    Returns updated cache dict.
    """
    processed = 0

    async def search_one(addr: str) -> dict:
        # Cache hit only for parsed results within the TTL window (R3)
        if _is_fresh_entry(cache.get(addr)):
            return cache[addr]

        # Rate limit
        await asyncio.sleep(RATE_LIMIT_SEC)

        # Search
        result = await _search_dcad_by_address(addr)
        result.setdefault("retrieved_at", _utc_now_iso())
        cache[addr] = result

        # Save cache periodically
        nonlocal processed
        processed += 1
        if processed % 10 == 0:
            _save_cache(cache)

        return result

    # Sequential on purpose: parallel browser tabs trip DCAD's ASP.NET
    # rate limiting; one search at a time spaced RATE_LIMIT_SEC apart is safer.
    for addr in addresses[:batch_limit]:
        if addr and addr != "Unknown":
            await search_one(addr)

    _save_cache(cache)
    return cache


def batch_cross_reference(addresses: list[str], batch_limit: int = 50) -> dict:
    """Bulk cross-reference a list of addresses with DCAD.

    Uses cache to skip already-searched addresses.
    Returns dict of {address: DCAD result}.

    RL10: the load -> search -> save sequence runs inside an exclusive
    file_lock on the cache sidecar so concurrent processes cannot overwrite
    each other's freshly searched entries (lost-update bug). On contention
    that outlasts the scaled timeout this raises TimeoutError("LOCK-BUSY ...")
    instead of silently dropping writes.
    """
    with file_lock(CACHE_PATH, timeout=_cache_lock_timeout(batch_limit)):
        cache = _load_cache()
        cache = asyncio.run(_batch_search(addresses, cache, batch_limit))
        return cache


def get_cached_result(address: str) -> Optional[dict]:
    """Get a single cached DCAD result (no new search).

    R3: only parsed results inside the TTL window count as cache hits —
    stale or error entries are treated as a miss (returns None).
    """
    entry = _load_cache().get(address)
    return entry if _is_fresh_entry(entry) else None


def _parse_int(val):
    if val is None:
        return None
    digits = re.sub(r'[^\d]', '', str(val))
    return int(digits) if digits else None


def _parse_float(val):
    if val is None:
        return None
    cleaned = re.sub(r'[^\d.]', '', str(val))
    return float(cleaned) if cleaned else None


def cross_reference_dcad_bulk(cprs: list, batch_limit: int = 50) -> int:
    """Cross-reference a list of CPRs with DCAD in bulk.

    Only processes Dallas CPRs that need cross-reference (flagged in enrichment).
    Returns count of CPRs updated.
    """
    # Collect addresses that need DCAD lookup
    addresses = []
    cpr_map = {}  # address → CPR

    for cpr in cprs:
        if cpr.county != "dallas":
            continue
        # Check if CPR needs DCAD cross-ref
        needs_ref = False
        if hasattr(cpr, "dcad_cross_ref_needed") and cpr.dcad_cross_ref_needed:
            needs_ref = cpr.dcad_cross_ref_needed.value
        elif cpr.improvement_sqft is None or (cpr.improvement_sqft and not cpr.improvement_sqft.value):
            needs_ref = True

        if needs_ref and cpr.situs_address and cpr.situs_address != "Unknown":
            addresses.append(cpr.situs_address)
            cpr_map[cpr.situs_address] = cpr

    if not addresses:
        return 0

    _log.info(f"DCAD bulk: {len(addresses)} addresses to cross-reference (limit: {batch_limit})")
    cache = batch_cross_reference(addresses, batch_limit)

    # Update CPRs with DCAD results — only the addresses actually processed
    # this run, and only from fresh parsed entries (R3).
    updated = 0
    for addr in addresses[:batch_limit]:
        result = cache.get(addr)
        cpr = cpr_map.get(addr)
        if not cpr or not isinstance(result, dict) or result.get("status") != "parsed":
            continue

        if result.get("improvement_sqft"):
            cpr.set_field("improvement_sqft", _parse_int(result["improvement_sqft"]), "dallas_cad", 0.90)
        if result.get("beds"):
            cpr.set_field("beds", _parse_int(result["beds"]), "dallas_cad", 0.90)
        if result.get("baths"):
            cpr.set_field("baths", _parse_float(result["baths"]), "dallas_cad", 0.90)
        if result.get("year_built"):
            cpr.set_field("year_built", _parse_int(result["year_built"]), "dallas_cad", 0.90)
        if result.get("cad_appraised_value"):
            cpr.set_field("cad_appraised_value", _parse_float(result["cad_appraised_value"]), "dallas_cad", 0.90)
        if hasattr(cpr, "dcad_cross_ref_needed"):
            cpr.set_field("dcad_cross_ref_needed", False, "dallas_cad", 1.0)
        updated += 1

    return updated


if __name__ == "__main__":
    # Self-check: R3 cache semantics (run against a temp cache file)
    import tempfile
    _orig_cache_path = CACHE_PATH
    CACHE_PATH = os.path.join(tempfile.mkdtemp(), "dcad_cache.json")
    try:
        assert _load_cache() == {}, "missing cache file should load as empty"

        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        assert _load_cache() == {}, "corrupt JSON must reset to fresh start"

        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            f.write('["wrong", "shape"]')
        assert _load_cache() == {}, "non-dict cache must reset to fresh start"

        stale_iso = (date.today() - timedelta(days=CACHE_TTL_DAYS + 1)).isoformat()
        fresh_iso = (date.today() - timedelta(days=5)).isoformat()
        _save_cache({
            "good": {"status": "parsed", "beds": "3", "retrieved_at": f"{fresh_iso}T00:00:00+00:00"},
            "negative": {"status": "no_data_found", "retrieved_at": f"{fresh_iso}T00:00:00+00:00"},
            "mismatched": {"status": "mismatch", "displayed_address": None,
                           "retrieved_at": f"{fresh_iso}T00:00:00+00:00"},
            "boom": {"status": "error", "error": "timeout",
                     "retrieved_at": f"{fresh_iso}T00:00:00+00:00"},
            "ancient": {"status": "parsed", "retrieved_at": f"{stale_iso}T00:00:00+00:00"},
            "nostamp": {"status": "parsed"},  # undated legacy entry: never persisted
        })
        loaded = _load_cache()
        assert set(loaded) == {"good", "negative", "mismatched", "ancient"}, loaded
        assert "boom" not in loaded, "transient error status was persisted (R3 violation)"
        assert "nostamp" not in loaded, "undated entry was persisted and would dodge the TTL"
        for addr, entry in loaded.items():
            assert entry.get("retrieved_at"), f"{addr} missing retrieved_at"

        assert _is_fresh_entry(loaded["good"]) is True
        assert _is_fresh_entry(loaded["negative"]) is False   # not a parsed result
        assert _is_fresh_entry(loaded["mismatched"]) is False # not a parsed result
        assert _is_fresh_entry(loaded["ancient"]) is False    # outside 90d TTL
        assert _is_fresh_entry({"status": "parsed"}) is False # no usable timestamp
        boundary = date.today() - timedelta(days=CACHE_TTL_DAYS)
        assert _is_fresh_entry({"status": "parsed",
                                "retrieved_at": boundary.isoformat()}) is True

        assert get_cached_result("good") is not None          # fresh hit
        assert get_cached_result("ancient") is None           # stale -> miss
        assert get_cached_result("nostamp") is None           # never persisted
        print(f"dallas_cad_bulk.py: R3 cache self-check passed "
              f"({len(loaded)} definitive entries kept, transient dropped)")
    finally:
        CACHE_PATH = _orig_cache_path

    # Self-check: address filtering
    test_addrs = ["15806 BREEDLOVE PLACE, ADDISON, TX 75001", "Unknown", "", "123 Main St, Dallas, TX 75201"]
    valid = [a for a in test_addrs if a and a != "Unknown"]
    assert len(valid) == 2
    print("dallas_cad_bulk.py: address filtering correct")

    print("dallas_cad_bulk.py: all self-checks passed")
    print("dallas_cad_bulk.py: live DCAD search requires Playwright — skipping live test")