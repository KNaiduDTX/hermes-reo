# src/adapters/census.py
"""Census geocoder enrichment — PORTED from lucid-noether.

Free US Census Geocoder API for lat/lng → census tract/block group.
Rate-limited (0.5s between calls), batched.

N2 retry policy: only timeouts, other transport errors, and HTTP 5xx are
retried. 4xx responses and undecodable JSON fail fast. Backoff carries
uniform jitter of up to half the computed delay, and there is deliberately
no terminal sleep after the final attempt.
"""
from __future__ import annotations
import logging
import random
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CENSUS_COORD_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
CENSUS_ADDR_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
MAX_RETRIES = 3
BACKOFF_BASE = 1.0

# N2: the complete set of retryable exception types. TimeoutException is a
# TransportError subclass in httpx; both are listed for explicitness.
RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.TransportError)


def _should_retry(exc: Optional[BaseException], status: Optional[int]) -> bool:
    """Pure N2 retry policy — safe to unit-test offline (no I/O).

    Retry ONLY timeouts/transport errors and HTTP 5xx. When *exc* is given it
    decides alone: any non-transport exception (JSON decode errors, unexpected
    bugs) fails fast regardless of status. A bare status retries iff >= 500;
    a missing status with no exception (e.g. connection refused surfaced as
    neither) does not retry.
    """
    if exc is not None:
        return isinstance(exc, RETRYABLE_EXCEPTIONS)
    return status is not None and status >= 500


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff seconds for the given 1-based attempt."""
    return BACKOFF_BASE * (2 ** (attempt - 1))


def _sleep_backoff(attempt: int) -> None:
    """Sleep the exponential backoff with uniform jitter of half the delay.

    The actual sleep is drawn uniformly from [delay/2, delay] so concurrent
    workers de-synchronize while staying bounded by the plain backoff.
    """
    delay = _backoff_delay(attempt)
    time.sleep(random.uniform(delay / 2.0, delay))


def _request_json(url: str, params: dict, timeout_s: float) -> tuple[Optional[dict], Optional[str]]:
    """GET *url* and JSON-decode it under the N2 retry policy.

    Returns (payload, None) on success, or (None, failure_status_string).
    No terminal sleep runs after the final attempt.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        exc: Optional[BaseException] = None
        status: Optional[int] = None
        try:
            resp = httpx.get(url, params=params, timeout=timeout_s)
            status = resp.status_code
        except RETRYABLE_EXCEPTIONS as e:
            exc = e
            logger.warning("census attempt %d/%d failed: %s: %s",
                           attempt, MAX_RETRIES, type(e).__name__, e)
        except Exception as e:
            # Anything outside the transport family never justifies a retry.
            logger.warning("census non-retryable failure: %s: %s", type(e).__name__, e)
            return None, f"Census request failed ({type(e).__name__})"
        else:
            if status == 200:
                try:
                    return resp.json(), None
                except ValueError as e:
                    logger.warning("census invalid JSON response: %s: %s",
                                   type(e).__name__, e)
                    return None, "Census returned invalid JSON"
            logger.warning("census attempt %d/%d got HTTP %s from %s",
                           attempt, MAX_RETRIES, status, url)

        if not _should_retry(exc, status):
            if status is not None:
                return None, f"Census HTTP {status} — failing fast"
            return None, "Census request failed"
        if attempt < MAX_RETRIES:
            _sleep_backoff(attempt)

    return None, "Failed after max retries"


def geocode_coordinates(lng: float, lat: float) -> dict:
    """Geocode lat/lng → census tract/block group."""
    params = {
        "x": lng, "y": lat,
        "benchmark": "Public_AR_Current",
        "vintage": "Census2020_Current",
        "format": "json",
    }
    payload, err = _request_json(CENSUS_COORD_URL, params, timeout_s=10)
    if err or payload is None:
        return {"status": err or "Failed after max retries"}
    result = payload.get("result", {})
    geos = result.get("geographies", {})
    tracts = geos.get("Census Tracts", [])
    blocks = geos.get("Census Blocks", [])
    if tracts or blocks:
        first_geo = tracts[0] if tracts else blocks[0]
        return {
            "census_tract": tracts[0].get("TRACT") if tracts else None,
            "block_group": blocks[0].get("BLKGRP") if blocks else None,
            "block": blocks[0].get("BLOCK") if blocks else None,
            "county_fips": first_geo.get("COUNTY"),
            "state_fips": first_geo.get("STATE"),
            "status": "Success",
        }
    return {"status": "No Census Geographies Resolved"}


def geocode_address(address: str) -> dict:
    """Geocode address → lat/lng + census tract/block group."""
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "vintage": "Census2020_Current",
        "format": "json",
    }
    payload, err = _request_json(CENSUS_ADDR_URL, params, timeout_s=15)
    if err or payload is None:
        return {"status": err or "Failed after max retries"}
    matches = payload.get("result", {}).get("addressMatches", [])
    if not matches:
        return {"status": "No Matches Found"}
    m = matches[0]
    coords = m.get("coordinates", {})
    geos = m.get("geographies", {})
    tracts = geos.get("Census Tracts", [])
    blocks = geos.get("Census Blocks", [])
    first_geo = tracts[0] if tracts else blocks[0]
    return {
        "latitude": coords.get("y"),
        "longitude": coords.get("x"),
        "census_tract": tracts[0].get("TRACT") if tracts else None,
        "block_group": blocks[0].get("BLKGRP") if blocks else None,
        "county_fips": first_geo.get("COUNTY"),
        "state_fips": first_geo.get("STATE"),
        "status": "Success",
    }


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
    # Self-check (N2): retry policy decisions, fully offline
    assert issubclass(httpx.TimeoutException, httpx.TransportError)
    assert _should_retry(httpx.ConnectTimeout("timed out"), None) is True
    assert _should_retry(httpx.ReadTimeout("timed out"), None) is True
    assert _should_retry(httpx.ConnectError("transport down"), None) is True
    assert _should_retry(None, 500) is True
    assert _should_retry(None, 503) is True
    assert _should_retry(None, 400) is False      # client error -> fail fast
    assert _should_retry(None, 404) is False
    assert _should_retry(ValueError("bad json"), 200) is False  # JSON fail fast
    assert _should_retry(KeyError("bug"), None) is False        # unexpected exc
    assert _should_retry(None, None) is False
    # exception wins over status when both are supplied
    assert _should_retry(httpx.ReadTimeout("t"), 400) is True

    import inspect
    src = inspect.getsource(_request_json)
    loop = [ln for ln in src.splitlines() if "for attempt in range" in ln]
    assert len(loop) == 1
    tail = src.split("_sleep_backoff(attempt)")[1]
    assert "time.sleep" not in tail, "terminal sleep after final attempt (N2 violation)"

    print("census.py: N2 retry policy self-check passed "
          "(retry = timeouts/transport/5xx only; 4xx + JSON fail fast)")

    # Live smoke test: geocode a known DFW address (graceful when offline)
    result = geocode_address("10022 Plainsman Ln, Frisco, TX 75035")
    if result.get("status") == "Success":
        assert result.get("latitude") is not None
        print(f"census.py: geocoded — lat={result['latitude']:.4f}, lng={result['longitude']:.4f}")
        print(f"  tract={result.get('census_tract')}, county_fips={result.get('county_fips')}")
    else:
        print(f"census.py: geocode returned {result.get('status')} (network may be down)")
    print("census.py: self-check complete")