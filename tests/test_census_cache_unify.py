"""RL11: census negative-cache unification tests (offline only).

Proves that src/adapters/census.py's ledger helpers share ONE key space and
ONE skip policy with pipeline.py's CLI geocode ledger, that ledgers round-
trip through the shared data/geocode_failures.json shape, and that
enrich_batch consults and records the ledger only when handed one.

No network: geocoding functions are scripted; clocks are injected.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import (
    GEOCODE_FAILURE_TTL_DAYS,
    GEOCODE_MAX_ATTEMPTS,
    geocode_failure_key,
    record_geocode_failure,
    should_skip_geocode,
)
from src.adapters import census
from src.addr import to_census_one_line

FIXED_NOW = datetime(2026, 2, 10, 12, 0, 0, tzinfo=timezone.utc)
ADDR = "1311 FOSTER ST CEDAR HILL TX 75104"
ADDR_B = "2739 SHEILAH DRIVE BALCHSPRINGS TX 75180"


def _clock():
    return FIXED_NOW


def _iso(**delta) -> str:
    return (FIXED_NOW - timedelta(**delta)).isoformat()


def _entry(attempts, last_at, last_status="No Matches Found") -> dict:
    return {"attempts": attempts, "last_status": last_status,
            "last_at": last_at}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(census, "GEOCODE_RATE_LIMIT_S", 0)


# ------------------------------------------------- one policy, one key space -

def test_policy_constants_mirror_the_cli_ledger():
    assert census.LEDGER_MAX_ATTEMPTS == GEOCODE_MAX_ATTEMPTS
    assert census.LEDGER_FAILURE_TTL_DAYS == GEOCODE_FAILURE_TTL_DAYS


def test_key_matches_pipeline_key_for_plausible_address():
    assert to_census_one_line(ADDR)          # fixture sanity
    assert census.ledger_key(ADDR) == geocode_failure_key(ADDR)


def test_key_matches_pipeline_key_for_implausible_address():
    junk = "1255 West !$th Street, Suite 1060 Plano, Dallas, TX 75075"
    assert to_census_one_line(junk) == ""     # rejected by the normalizer
    assert census.ledger_key(junk) == geocode_failure_key(junk)


def test_key_is_stable_sha1_16_of_normalized_query():
    expected = hashlib.sha1(
        to_census_one_line(ADDR).encode("utf-8")).hexdigest()[:16]
    assert census.ledger_key(ADDR) == expected
    assert len(census.ledger_key(ADDR)) == 16
    assert census.ledger_key(ADDR.lower()) == census.ledger_key(ADDR)


# --------------------------------------------------------- skip policy ------

def test_skip_matrix():
    k = census.ledger_key(ADDR)

    def ledger_with(entry):
        return {k: entry} if entry is not None else {}

    # no ledger / no entry -> never skipped
    assert census.should_skip(ADDR, {}, now_fn=_clock) is False
    assert census.should_skip(ADDR, {}, now_fn=_clock, retry_failed=True) is False

    # below threshold -> retryable regardless of freshness
    below = ledger_with(_entry(census.LEDGER_MAX_ATTEMPTS - 1, _iso(hours=1)))
    assert census.should_skip(ADDR, below, now_fn=_clock) is False

    # at threshold + fresh failure -> skipped
    fresh = ledger_with(_entry(census.LEDGER_MAX_ATTEMPTS, _iso(hours=1)))
    assert census.should_skip(ADDR, fresh, now_fn=_clock) is True

    # exactly TTL days old -> expired (strict window), retryable
    edge = ledger_with(_entry(census.LEDGER_MAX_ATTEMPTS,
                              _iso(days=census.LEDGER_FAILURE_TTL_DAYS)))
    assert census.should_skip(ADDR, edge, now_fn=_clock) is False

    # one second inside the window -> still skipped
    inside = ledger_with(_entry(
        census.LEDGER_MAX_ATTEMPTS,
        _iso(days=census.LEDGER_FAILURE_TTL_DAYS - 1, seconds=86399)))
    assert census.should_skip(ADDR, inside, now_fn=_clock) is True

    # unparsable timestamp and future timestamps -> retryable, not fatal
    broken = ledger_with(_entry(census.LEDGER_MAX_ATTEMPTS, "not-a-timestamp"))
    assert census.should_skip(ADDR, broken, now_fn=_clock) is False
    future = ledger_with(_entry(census.LEDGER_MAX_ATTEMPTS,
                                _iso(days=-2)))
    assert census.should_skip(ADDR, future, now_fn=_clock) is False

    # naive timestamps read as UTC
    naive = (FIXED_NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert census.should_skip(
        ADDR, ledger_with(_entry(census.LEDGER_MAX_ATTEMPTS, naive)),
        now_fn=_clock) is True

    # explicit override wins over a fresh at-cap entry
    assert census.should_skip(ADDR, fresh, retry_failed=True,
                              now_fn=_clock) is False


# ------------------------------------------------------- record / forgive ---

def test_record_failure_tallies_and_success_forgives():
    ledger: dict = {}
    assert census.record_result(ADDR, ledger, "No Matches Found",
                                now_fn=_clock) is True
    assert census.record_result(ADDR, ledger, "Timeout",
                                now_fn=_clock) is True
    entry = ledger[census.ledger_key(ADDR)]
    assert entry["attempts"] == 2
    assert entry["last_status"] == "Timeout"          # newest status wins
    assert entry["last_at"] == FIXED_NOW.isoformat()

    # success forgives — and is idempotent on an absent key
    assert census.record_result(ADDR, ledger, "Success",
                                now_fn=_clock) is False
    assert census.ledger_key(ADDR) not in ledger
    assert census.record_result(ADDR_B, ledger, "Success",
                                now_fn=_clock) is False


def test_recorded_entry_shape_is_cli_compatible(tmp_path):
    ledger: dict = {}
    census.record_result(ADDR, ledger, "Invalid Address", now_fn=_clock)
    path = str(tmp_path / "geocode_failures.json")
    census.save_ledger(ledger, path)
    reloaded = json.load(open(path))
    stored = reloaded[geocode_failure_key(ADDR)]
    assert set(stored) == {"attempts", "last_status", "last_at"}
    assert stored == ledger[geocode_failure_key(ADDR)]


# --------------------------------------------- interop with the CLI ledger --

def test_cli_recorded_failures_are_skipped_by_census_helpers():
    ledger: dict = {}
    record_geocode_failure(ledger, ADDR, "Timeout", now_fn=_clock)
    record_geocode_failure(ledger, ADDR, "Timeout", now_fn=_clock)
    record_geocode_failure(ledger, ADDR, "Timeout", now_fn=_clock)
    assert census.should_skip(ADDR, ledger, now_fn=_clock) is True
    assert census.should_skip(ADDR_B, ledger, now_fn=_clock) is False


def test_census_recorded_failures_are_skipped_by_cli_helper():
    ledger: dict = {}
    for _ in range(census.LEDGER_MAX_ATTEMPTS):
        census.record_result(ADDR, ledger, "No Matches Found", now_fn=_clock)
    assert should_skip_geocode(ledger.get(geocode_failure_key(ADDR)),
                               now_fn=_clock) is True


# ------------------------------------------------------ tolerant load/save --

def test_load_ledger_missing_and_corrupt_files(tmp_path):
    assert census.load_ledger(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "geocode_failures.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    assert census.load_ledger(str(bad)) == {}
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    assert census.load_ledger(str(bad)) == {}


def test_load_ledger_tolerates_junk_entries_and_coerces(tmp_path):
    path = tmp_path / "geocode_failures.json"
    path.write_text(json.dumps({
        "good": _entry(2, _iso(days=1)),
        "junk": "not-a-dict",
        "weird": {"attempts": "three", "last_status": None},
    }), encoding="utf-8")
    ledger = census.load_ledger(str(path))
    assert set(ledger) == {"good", "weird"}
    assert ledger["good"]["attempts"] == 2
    assert ledger["weird"]["attempts"] == 0
    assert ledger["weird"]["last_status"] == "None"


def test_save_then_load_round_trip(tmp_path):
    ledger: dict = {}
    census.record_result(ADDR, ledger, "Timeout", now_fn=_clock)
    path = str(tmp_path / "ledger.json")
    census.save_ledger(ledger, path)
    reloaded = census.load_ledger(path)
    assert reloaded[census.ledger_key(ADDR)]["attempts"] == 1
    assert reloaded[census.ledger_key(ADDR)]["last_status"] == "Timeout"


# --------------------------------------------------- enrich_batch wiring ----

FAILING_GEO = {"status": "No Matches Found"}


def _rec(addr, **extra):
    rec = {"property_address": addr}
    rec.update(extra)
    return rec


def test_enrich_batch_without_ledger_unchanged(monkeypatch):
    calls = []

    monkeypatch.setattr(census, "geocode_address",
                        lambda a: calls.append(a) or FAILING_GEO)
    records = [_rec(ADDR)]
    out = census.enrich_batch(records)
    assert calls == [ADDR]                       # attempted despite nothing
    assert out[0]["details"]["census_demographics"]["status"] == \
        "No Matches Found"


def test_enrich_batch_records_failures_into_ledger(monkeypatch):
    calls = []
    monkeypatch.setattr(census, "geocode_address",
                        lambda a: calls.append(a) or FAILING_GEO)
    ledger: dict = {}
    census.enrich_batch([_rec(ADDR)], ledger=ledger, now_fn=_clock)
    assert len(calls) == 1
    assert ledger[geocode_failure_key(ADDR)]["attempts"] == 1


def test_enrich_batch_honors_shared_negative_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(census, "geocode_address",
                        lambda a: calls.append(a) or FAILING_GEO)
    ledger: dict = {}
    for _ in range(census.LEDGER_MAX_ATTEMPTS):
        census.record_result(ADDR, ledger, "No Matches Found", now_fn=_clock)

    records = [_rec(ADDR), _rec(ADDR_B)]
    out = census.enrich_batch(records, batch_limit=1, ledger=ledger,
                              now_fn=_clock)
    # ADDR sits out; the cap was NOT consumed by the skip, so B ran instead
    assert calls == [ADDR_B]
    assert "census_demographics" not in out[0]["details"]
    assert out[1]["details"]["census_demographics"]["status"] == \
        "No Matches Found"
    assert ledger[geocode_failure_key(ADDR)]["attempts"] == \
        census.LEDGER_MAX_ATTEMPTS               # untouched by the skip
    assert ledger[geocode_failure_key(ADDR_B)]["attempts"] == 1


def test_enrich_batch_success_clears_ledger_entry(monkeypatch):
    success_geo = {"status": "Success", "latitude": 32.6, "longitude": -96.9}
    monkeypatch.setattr(census, "geocode_address", lambda a: success_geo)
    ledger: dict = {
        geocode_failure_key(ADDR): _entry(census.LEDGER_MAX_ATTEMPTS - 1,
                                          _iso(days=1)),
    }
    census.enrich_batch([_rec(ADDR)], ledger=ledger, now_fn=_clock)
    assert geocode_failure_key(ADDR) not in ledger      # forgiven
    assert ledger == {}                                 # nothing else added


def test_enrich_batch_coordinate_path_also_records(monkeypatch):
    monkeypatch.setattr(census, "geocode_coordinates",
                        lambda lng, lat: FAILING_GEO)
    ledger: dict = {}
    census.enrich_batch([_rec("Unknown Address", latitude=32.6,
                              longitude=-96.9)],
                        ledger=ledger, now_fn=_clock)
    # keyed on property_address identity even when geocoding via coordinates;
    # an Unknown address hashes under its raw-string fallback key
    assert any(v["attempts"] == 1 for v in ledger.values())


def test_enrich_batch_never_queries_for_local_validation_skips(monkeypatch):
    calls = []
    monkeypatch.setattr(census, "geocode_address",
                        lambda a: calls.append(a) or FAILING_GEO)
    monkeypatch.setattr(census, "geocode_coordinates",
                        lambda lng, lat: (_ for _ in ()).throw(
                            AssertionError("coordinates must not be used")))
    ledger: dict = {}
    census.enrich_batch([_rec("")], ledger=ledger, now_fn=_clock)
    assert calls == []
    assert ledger == {}                          # local skip writes nothing
