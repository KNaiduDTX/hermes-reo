# src/adapters/census.py
"""Census geocoder enrichment — PORTED from lucid-noether.

Free US Census Geocoder API for lat/lng → census tract/block group.
Rate-limited (0.5s between calls), batched.

N2 retry policy: only timeouts, other transport errors, and HTTP 5xx are
retried. 4xx responses and undecodable JSON fail fast. Backoff carries
uniform jitter of up to half the computed delay, and there is deliberately
no terminal sleep after the final attempt.

RL11 unified negative cache: the CLI backfill (pipeline.py
--geocode-backfill) keeps a failure ledger in data/geocode_failures.json
while enrich_batch only ever consulted each record's own last stored status
— the two caches never saw each other. The ledger_key / should_skip /
record_result helpers below re-express the CLI semantics (sha1[:16] of the
normalized census query; skip after LEDGER_MAX_ATTEMPTS fresh failures;
success forgives) so any caller shares ONE policy and ONE key space. The
ledgers they produce and consume are byte-compatible with pipeline's
geocode_failures.json by construction.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ..addr import to_census_one_line
from ..io_utils import write_json

logger = logging.getLogger(__name__)

CENSUS_COORD_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
CENSUS_ADDR_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"
MAX_RETRIES = 3
BACKOFF_BASE = 1.0

# Adapter rate limit: minimum seconds between Census API calls. Single source
# of truth for every caller (enrich_batch below, pipeline --geocode-backfill).
GEOCODE_RATE_LIMIT_S = 0.5

# N2: the complete set of retryable exception types. TimeoutException is a
# TransportError subclass in httpx; both are listed for explicitness.
RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.TransportError)

# --- RL11: shared negative-ledger policy ------------------------------------
# The one status that counts as "resolved" for every consumer.
CENSUS_SUCCESS_STATUS = "Success"
# Skip an address after this many failures... ...until its newest failure
# goes stale. Mirrors pipeline.GEOCODE_MAX_ATTEMPTS / GEOCODE_FAILURE_TTL_DAYS
# (assert-equal in tests) so both caches expire on the same policy.
LEDGER_MAX_ATTEMPTS = 3
LEDGER_FAILURE_TTL_DAYS = 7
# Default ledger location: the SAME file the CLI backfill uses.
DEFAULT_LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "data",
    "geocode_failures.json")


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


# --- RL11: shared negative-ledger helpers ------------------------------------

def _ledger_now() -> datetime:
    """Timezone-aware UTC now (never naive datetime.utcnow — see hooks)."""
    return datetime.now(timezone.utc)


def ledger_key(addr_key: str) -> str:
    """Negative-cache key: sha1[:16] of the normalized census query.

    Identical algorithm to pipeline.geocode_failure_key — falls back to
    hashing the raw string when normalization rejects it, so implausible
    addresses still get a stable identity and both caches interoperate.
    """
    query = to_census_one_line(addr_key or "") or (addr_key or "")
    return hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]


def parse_ledger_ts(value) -> Optional[datetime]:
    """Parse a ledger ISO timestamp; naive values are read as UTC."""
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def should_skip(addr_key: str, ledger: dict, retry_failed: bool = False,
                now_fn=None) -> bool:
    """True when *addr_key* must sit this invocation out (RL11 policy).

    Skip iff its ledger entry records >= LEDGER_MAX_ATTEMPTS failures AND
    the newest failure is younger than LEDGER_FAILURE_TTL_DAYS. A missing
    entry, a sub-threshold tally, an unparsable/stale last_at, or
    *retry_failed* all make the address retryable. Pure — no I/O; inject a
    clock via *now_fn* for tests.
    """
    if retry_failed or not ledger:
        return False
    entry = ledger.get(ledger_key(addr_key))
    if not entry:
        return False
    attempts = entry.get("attempts") or 0
    if attempts < LEDGER_MAX_ATTEMPTS:
        return False
    last_at = parse_ledger_ts(entry.get("last_at"))
    if last_at is None:
        return False
    now = now_fn() if now_fn is not None else _ledger_now()
    age = now - last_at
    return timedelta(0) <= age < timedelta(days=LEDGER_FAILURE_TTL_DAYS)


def record_result(addr_key: str, ledger: dict, status: str,
                  now_fn=None) -> bool:
    """Fold one geocode outcome for *addr_key* into *ledger* (in place).

    CENSUS_SUCCESS_STATUS forgives: any existing entry is dropped and False
    is returned. Any other status bumps the failure tally, stamps
    last_status/last_at, and returns True. Entry shape matches pipeline's
    record_geocode_failure exactly so the shared file stays uniform.
    """
    stamp = (now_fn() if now_fn is not None else _ledger_now()).isoformat()
    key = ledger_key(addr_key)
    if status == CENSUS_SUCCESS_STATUS:
        ledger.pop(key, None)
        return False
    entry = ledger.setdefault(key, {"attempts": 0})
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["last_status"] = status
    entry["last_at"] = stamp
    return True


def load_ledger(path: str = DEFAULT_LEDGER_PATH) -> dict:
    """Load the shared negative ledger tolerantly: missing/corrupt -> {}.

    Accepts the exact file pipeline.load_geocode_failures writes; junk
    entries are dropped instead of failing the run.
    """
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("census ledger unreadable at %s — starting fresh", path)
        return {}
    if not isinstance(raw, dict):
        logger.warning("census ledger unexpected shape at %s — starting fresh",
                       path)
        return {}
    ledger: dict[str, dict] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        attempts = entry.get("attempts")
        ledger[str(key)] = {
            "attempts": attempts if isinstance(attempts, int) else 0,
            "last_status": str(entry.get("last_status", "")),
            "last_at": str(entry.get("last_at", "")),
        }
    return ledger


def save_ledger(ledger: dict, path: str = DEFAULT_LEDGER_PATH) -> str:
    """Persist the shared negative ledger atomically (io_utils.write_json)."""
    return write_json(path, ledger)


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


def enrich_batch(records: list[dict], batch_limit: int = 30,
                 ledger: Optional[dict] = None,
                 now_fn=None) -> list[dict]:
    """Enrich a batch of records with census demographics.

    Each record must have 'latitude' and 'longitude' (or 'property_address').
    Adds 'census_demographics' to record['details'].

    RL11: when *ledger* (a mutable dict) is given, the shared negative cache
    is consulted before any network work — an address whose key holds fresh
    failures (should_skip) is left untouched WITHOUT consuming *batch_limit*
    — and every actual geocode outcome is folded back in via record_result
    (success forgives, failure tallies). Pass load_ledger()'s dict to share
    the CLI's data/geocode_failures.json. ledger=None keeps the historical
    behavior byte-for-byte.
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

        if ledger is not None and should_skip(addr, ledger, now_fn=now_fn):
            continue

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
        if ledger is not None:
            record_result(addr, ledger, geo.get("status", ""), now_fn=now_fn)
        if geo.get("status") == "Success":
            changes += 1
        time.sleep(GEOCODE_RATE_LIMIT_S)

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

    # B3: rate limit is a positive constant shared with pipeline backfill
    assert GEOCODE_RATE_LIMIT_S > 0
    assert enrich_batch.__doc__  # batch entry point documented

    # Self-check (RL11): negative-ledger policy, fully offline
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    fixed_now = _dt(2026, 2, 10, 12, 0, 0, tzinfo=_tz.utc)
    clock = lambda: fixed_now  # noqa: E731
    iso_ago = lambda **kw: (fixed_now - _td(**kw)).isoformat()  # noqa: E731
    addr = "1311 FOSTER ST CEDAR HILL TX 75104"
    key = ledger_key(addr)
    assert len(key) == 16
    assert ledger_key(addr.lower()) == key          # normalized identity
    empty: dict = {}
    assert should_skip(addr, empty, now_fn=clock) is False
    ledger = {key: {"attempts": LEDGER_MAX_ATTEMPTS - 1,
                    "last_status": "Timeout", "last_at": iso_ago(hours=1)}}
    assert should_skip(addr, ledger, now_fn=clock) is False   # below threshold
    ledger[key]["attempts"] = LEDGER_MAX_ATTEMPTS
    assert should_skip(addr, ledger, now_fn=clock) is True    # fresh + at cap
    ledger[key]["last_at"] = iso_ago(days=LEDGER_FAILURE_TTL_DAYS)
    assert should_skip(addr, ledger, now_fn=clock) is False   # stale -> retry
    ledger[key]["last_at"] = "garbage"
    assert should_skip(addr, ledger, now_fn=clock) is False   # unreadable -> retry
    assert should_skip(addr, ledger, retry_failed=True, now_fn=clock) is False
    # record_result: failure tallies, success forgives
    fresh: dict = {}
    assert record_result(addr, fresh, "No Matches Found", now_fn=clock) is True
    assert record_result(addr, fresh, "Timeout", now_fn=clock) is True
    entry = fresh[key]
    assert entry["attempts"] == 2 and entry["last_status"] == "Timeout"
    assert entry["last_at"] == fixed_now.isoformat()
    assert record_result(addr, fresh, CENSUS_SUCCESS_STATUS, now_fn=clock) is False
    assert key not in fresh                                # forgiven
    assert record_result(addr, fresh, CENSUS_SUCCESS_STATUS, now_fn=clock) is False
    print("census.py: RL11 ledger self-check passed "
          "(skip matrix + record/forgive round trip)")

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