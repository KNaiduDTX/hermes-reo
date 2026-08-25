# src/adapters/census.py
"""Census geocoder enrichment — PORTED from lucid-noether.

Free US Census Geocoder API for lat/lng → census tract/block group.
Rate-limited (0.5s between calls), batched.
"""
from __future__ import annotations
import time
import httpx

CENSUS_COORD_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
CENSUS_ADDR_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
MAX_RETRIES = 3
BACKOFF_BASE = 1.0


def geocode_coordinates(lng: float, lat: float) -> dict:
    """Geocode lat/lng → census tract/block group."""
    params = {
        "x": lng, "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Census2020_Current",
        "format": "json",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = httpx.get(CENSUS_COORD_URL, params=params, timeout=10)
            if resp.status_code == 200:
                result = resp.json().get("result", {})
                geos = result.get("geographies", {})
                tracts = geos.get("Census Tracts", [])
                blocks = geos.get("Census Blocks", [])
                if tracts or blocks:
                    return {
                        "census_tract": tracts[0].get("TRACT") if tracts else None,
                        "block_group": blocks[0].get("BLKGRP") if blocks else None,
                        "block": blocks[0].get("BLOCK") if blocks else None,
                        "county_fips": (tracts[0].get("COUNTY") if tracts else blocks[0].get("COUNTY")) if (tracts or blocks) else None,
                        "state_fips": (tracts[0].get("STATE") if tracts else blocks[0].get("STATE")) if (tracts or blocks) else None,
                        "status": "Success",
                    }
                return {"status": "No Census Geographies Resolved"}
        except Exception:
            pass
        time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
    return {"status": "Failed after max retries"}


def geocode_address(address: str) -> dict:
    """Geocode address → lat/lng + census tract/block group."""
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "vintage": "Census2020_Current",
        "format": "json",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = httpx.get(CENSUS_ADDR_URL, params=params, timeout=15)
            if resp.status_code == 200:
                matches = resp.json().get("result", {}).get("addressMatches", [])
                if matches:
                    m = matches[0]
                    coords = m.get("coordinates", {})
                    geos = m.get("geographies", {})
                    tracts = geos.get("Census Tracts", [])
                    blocks = geos.get("Census Blocks", [])
                    return {
                        "latitude": coords.get("y"),
                        "longitude": coords.get("x"),
                        "census_tract": tracts[0].get("TRACT") if tracts else None,
                        "block_group": blocks[0].get("BLKGRP") if blocks else None,
                        "county_fips": (tracts[0].get("COUNTY") if tracts else blocks[0].get("COUNTY")) if (tracts or blocks) else None,
                        "status": "Success",
                    }
                return {"status": "No Matches Found"}
        except Exception:
            pass
        time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
    return {"status": "Failed after max retries"}


def enrich_batch(records: list[dict], batch_limit: int = 30) -> list[dict]:
    """Enrich a batch of records with census demographics.

    Each record must have 'latitude' and 'longitude' (or 'property_address').
    Adds 'census_demographics' to record['details'].
    """
    changes = 0
    for item in records:
        if changes >= batch_limit:
            break
        details = item.setdefault("details", {})
        if "census_demographics" in details and details["census_demographics"].get("status") == "Success":
            continue

        lat = item.get("latitude")
        lng = item.get("longitude")
        addr = item.get("property_address", "")

        if lat and lng and lat != 0 and lng != 0:
            geo = geocode_coordinates(lng, lat)
        elif addr and addr != "Unknown":
            geo = geocode_address(addr)
            if geo.get("status") == "Success":
                item["latitude"] = geo.get("latitude")
                item["longitude"] = geo.get("longitude")
        else:
            geo = {"status": "No valid address or coordinates"}
            continue

        details["census_demographics"] = geo
        if geo.get("status") == "Success":
            changes += 1
        time.sleep(0.5)

    return records


if __name__ == "__main__":
    # Self-check: geocode a known DFW address
    result = geocode_address("10022 Plainsman Ln, Frisco, TX 75035")
    if result.get("status") == "Success":
        assert result.get("latitude") is not None
        print(f"census.py: geocoded — lat={result['latitude']:.4f}, lng={result['longitude']:.4f}")
        print(f"  tract={result.get('census_tract')}, county_fips={result.get('county_fips')}")
    else:
        print(f"census.py: geocode returned {result.get('status')} (network may be down)")
    print("census.py: self-check complete")