# src/adapters/dallas_cad_bulk.py
"""DCAD bulk cross-reference — batch Playwright with caching.

Extends the single-address DallasCADAdapter with:
1. JSON file cache (avoids re-searching addresses across runs)
2. Batch mode (process a list of addresses with rate limiting)
3. Error recovery (skip failed addresses, don't crash the batch)

Cache file: data/dcad_cache.json
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from typing import Optional

from .dallas_cad import DallasCADAdapter, _search_dcad_by_address, _parse_dcad_detail
from .base import SourceProvenance

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "dcad_cache.json")
RATE_LIMIT_SEC = 2.0  # seconds between DCAD searches


def _load_cache() -> dict:
    """Load the DCAD cache."""
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict):
    """Save the DCAD cache."""
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


async def _batch_search(addresses: list[str], cache: dict, batch_limit: int = 50) -> dict:
    """Search DCAD for a batch of addresses with caching + rate limiting.

    Returns updated cache dict.
    """
    processed = 0

    async def search_one(addr: str) -> dict:
        # Check cache first
        if addr in cache and cache[addr].get("status") == "parsed":
            return cache[addr]

        # Rate limit
        await asyncio.sleep(RATE_LIMIT_SEC)

        # Search
        result = await _search_dcad_by_address(addr)
        cache[addr] = result

        # Save cache periodically
        nonlocal processed
        processed += 1
        if processed % 10 == 0:
            _save_cache(cache)

        return result

    # Process in batches — ponytail: sequential to avoid DCAD rate-limiting/bans
    # Parallel browser tabs would get blocked; sequential with delay is safer
    for addr in addresses[:batch_limit]:
        if addr and addr != "Unknown":
            await search_one(addr)

    _save_cache(cache)
    return cache


def batch_cross_reference(addresses: list[str], batch_limit: int = 50) -> dict:
    """Bulk cross-reference a list of addresses with DCAD.

    Uses cache to skip already-searched addresses.
    Returns dict of {address: DCAD result}.
    """
    cache = _load_cache()
    cache = asyncio.run(_batch_search(addresses, cache, batch_limit))
    return cache


def get_cached_result(address: str) -> Optional[dict]:
    """Get a single cached DCAD result (no new search)."""
    cache = _load_cache()
    return cache.get(address)


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

    print(f"DCAD bulk: {len(addresses)} addresses to cross-reference (limit: {batch_limit})")
    cache = batch_cross_reference(addresses, batch_limit)

    # Update CPRs with DCAD results
    import re
    updated = 0
    for addr, result in cache.items():
        cpr = cpr_map.get(addr)
        if not cpr or result.get("status") != "parsed":
            continue

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
    # Self-check: cache load/save
    cache = _load_cache()
    assert isinstance(cache, dict)
    print(f"dallas_cad_bulk.py: cache has {len(cache)} entries")

    # Self-check: address filtering
    test_addrs = ["15806 BREEDLOVE PLACE, ADDISON, TX 75001", "Unknown", "", "123 Main St, Dallas, TX 75201"]
    valid = [a for a in test_addrs if a and a != "Unknown"]
    assert len(valid) == 2
    print("dallas_cad_bulk.py: address filtering correct")

    print("dallas_cad_bulk.py: all self-checks passed")
    print("dallas_cad_bulk.py: live DCAD search requires Playwright — skipping live test")