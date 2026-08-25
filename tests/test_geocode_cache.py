"""Unit tests for the geocode negative-cache ledger (pipeline.py helpers).

Pure-unit: tmp_path ledger files, injected clocks — ZERO network, and the
real current_deals.json / data/ directory are never touched.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from src.io_utils import write_json
from src.addr import to_census_one_line
from pipeline import (
    GEOCODE_FAILURE_TTL_DAYS,
    GEOCODE_MAX_ATTEMPTS,
    clear_geocode_failure,
    geocode_failure_key,
    load_geocode_failures,
    record_geocode_failure,
    select_geocode_targets,
    should_skip_geocode,
)

FIXED_NOW = datetime(2026, 2, 10, 12, 0, 0, tzinfo=timezone.utc)
PLAUSIBLE_A = "1311 FOSTER ST CEDAR HILL TX 75104"
PLAUSIBLE_B = "2739 SHEILAH DRIVE BALCHSPRINGS TX 75180"
IMPLAUSIBLE = "1255 West !$th Street, Suite 1060 Plano, Dallas, TX 75075"


def _fixed_clock():
    return FIXED_NOW


def _iso(**delta) -> str:
    return (FIXED_NOW - timedelta(**delta)).isoformat()


def _entry(attempts: int, last_at: str, last_status: str = "No Matches Found") -> dict:
    return {"attempts": attempts, "last_status": last_status, "last_at": last_at}


def _rec(address: str) -> dict:
    return {"address": address}  # no lat/lng -> counts as missing coords


# ---------------------------------------------------------------- keying ---

def test_key_is_sha1_16_of_normalized_census_query():
    query = to_census_one_line(PLAUSIBLE_A)
    assert query  # sanity: the fixture address must normalize
    assert geocode_failure_key(PLAUSIBLE_A) == \
        hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]
    assert len(geocode_failure_key(PLAUSIBLE_A)) == 16


def test_key_normalizes_case_and_spacing_to_same_identity():
    assert geocode_failure_key(PLAUSIBLE_A.lower()) == \
        geocode_failure_key(PLAUSIBLE_A)


def test_key_falls_back_to_raw_when_implausible():
    expected = hashlib.sha1(IMPLAUSIBLE.encode("utf-8")).hexdigest()[:16]
    assert to_census_one_line(IMPLAUSIBLE) == ""  # sanity: truly rejected
    assert geocode_failure_key(IMPLAUSIBLE) == expected


# ------------------------------------------------------------ tolerant load --

def test_load_missing_file_returns_empty_ledger(tmp_path):
    assert load_geocode_failures(str(tmp_path / "nope.json")) == {}


def test_load_corrupt_file_returns_fresh_ledger(tmp_path):
    bad = tmp_path / "geocode_failures.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    assert load_geocode_failures(str(bad)) == {}


def test_load_non_dict_root_returns_fresh(tmp_path):
    bad = tmp_path / "geocode_failures.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_geocode_failures(str(bad)) == {}


def test_load_tolerates_junk_entries_and_coerces_fields(tmp_path):
    path = tmp_path / "geocode_failures.json"
    path.write_text(json.dumps({
        "good": _entry(2, _iso(days=1)),
        "junk": "not-a-dict",
        "weird": {"attempts": "three", "last_status": None},
    }), encoding="utf-8")
    ledger = load_geocode_failures(str(path))
    assert set(ledger) == {"good", "weird"}
    assert ledger["good"]["attempts"] == 2
    assert ledger["weird"]["attempts"] == 0
    assert ledger["weird"]["last_status"] == "None"


def test_record_write_reload_round_trip(tmp_path):
    path = str(tmp_path / "geocode_failures.json")
    ledger: dict = {}
    record_geocode_failure(ledger, PLAUSIBLE_A, "Timeout", now_fn=_fixed_clock)
    write_json(path, ledger)
    reloaded = load_geocode_failures(path)
    assert reloaded[geocode_failure_key(PLAUSIBLE_A)]["attempts"] == 1
    assert reloaded[geocode_failure_key(PLAUSIBLE_A)]["last_status"] == "Timeout"
    datetime.fromisoformat(reloaded[geocode_failure_key(PLAUSIBLE_A)]["last_at"])


# ------------------------------------------------------- record / forgive ---

def test_record_failure_accumulates_attempts_and_stamps_meta():
    ledger: dict = {}
    record_geocode_failure(ledger, PLAUSIBLE_A, "No Matches Found",
                           now_fn=_fixed_clock)
    record_geocode_failure(ledger, PLAUSIBLE_A, "Timeout", now_fn=_fixed_clock)
    entry = ledger[geocode_failure_key(PLAUSIBLE_A)]
    assert entry["attempts"] == 2
    assert entry["last_status"] == "Timeout"  # newest status wins
    assert entry["last_at"] == FIXED_NOW.isoformat()


def test_success_clears_the_key():
    ledger: dict = {}
    record_geocode_failure(ledger, PLAUSIBLE_A, "Timeout", now_fn=_fixed_clock)
    clear_geocode_failure(ledger, PLAUSIBLE_A)
    assert geocode_failure_key(PLAUSIBLE_A) not in ledger
    clear_geocode_failure(ledger, PLAUSIBLE_A)  # idempotent, no KeyError


# ------------------------------------------------------------ skip policy ---

def test_below_threshold_is_never_skipped():
    entry = _entry(GEOCODE_MAX_ATTEMPTS - 1, _iso(hours=1))
    assert should_skip_geocode(entry, now_fn=_fixed_clock) is False


def test_fresh_failures_at_threshold_are_skipped():
    entry = _entry(GEOCODE_MAX_ATTEMPTS, _iso(hours=1))
    assert should_skip_geocode(entry, now_fn=_fixed_clock) is True


def test_ttl_expiry_makes_entry_retryable():
    entry = _entry(GEOCODE_MAX_ATTEMPTS,
                   _iso(days=GEOCODE_FAILURE_TTL_DAYS + 1))
    assert should_skip_geocode(entry, now_fn=_fixed_clock) is False


def test_ttl_boundary_exactly_n_days_is_expired():
    # Strict window: an entry aged exactly TTL days is stale -> retryable.
    entry = _entry(GEOCODE_MAX_ATTEMPTS, _iso(days=GEOCODE_FAILURE_TTL_DAYS))
    assert should_skip_geocode(entry, now_fn=_fixed_clock) is False


def test_just_inside_ttl_is_still_skipped():
    entry = _entry(GEOCODE_MAX_ATTEMPTS,
                   _iso(days=GEOCODE_FAILURE_TTL_DAYS - 1, hours=23))
    assert should_skip_geocode(entry, now_fn=_fixed_clock) is True


def test_retry_failed_overrides_a_fresh_entry():
    entry = _entry(GEOCODE_MAX_ATTEMPTS + 5, _iso(minutes=5))
    assert should_skip_geocode(entry, retry_failed=True,
                               now_fn=_fixed_clock) is False


def test_unparsable_last_at_is_retryable_not_fatal():
    entry = _entry(GEOCODE_MAX_ATTEMPTS, "not-a-timestamp")
    assert should_skip_geocode(entry, now_fn=_fixed_clock) is False
    assert should_skip_geocode(None, now_fn=_fixed_clock) is False


def test_naive_last_at_is_read_as_utc():
    naive = (FIXED_NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    entry = _entry(GEOCODE_MAX_ATTEMPTS, naive)
    assert should_skip_geocode(entry, now_fn=_fixed_clock) is True


# ------------------------------------------------------- target selection ---

def test_select_partitions_implausible_previously_failed_and_clean():
    ledger = {
        geocode_failure_key(PLAUSIBLE_A): _entry(GEOCODE_MAX_ATTEMPTS,
                                                 _iso(days=2)),
        geocode_failure_key(PLAUSIBLE_B): _entry(GEOCODE_MAX_ATTEMPTS,
                                                 _iso(days=GEOCODE_FAILURE_TTL_DAYS + 2)),
    }
    records = [_rec(PLAUSIBLE_A),      # fresh failures -> skip
               _rec(IMPLAUSIBLE),      # junk pattern -> never queried
               _rec(PLAUSIBLE_B),      # stale failures -> retryable
               _rec("600 Commerce Street, Suite 700 Dallas TX 75202")]
    targets, stats = select_geocode_targets(records, ledger, cap=25,
                                            now_fn=_fixed_clock)
    addresses = [r["address"] for r in targets]
    assert PLAUSIBLE_B in addresses
    assert PLAUSIBLE_A not in addresses
    assert IMPLAUSIBLE not in addresses
    assert stats == {"skipped_previously_failed": 1, "skipped_implausible": 1}


def test_select_counts_all_candidates_beyond_cap():
    ledger = {
        geocode_failure_key(PLAUSIBLE_A): _entry(GEOCODE_MAX_ATTEMPTS,
                                                 _iso(hours=2)),
        geocode_failure_key(IMPLAUSIBLE): _entry(9, _iso(hours=2)),
    }
    records = [_rec(PLAUSIBLE_B),
               _rec("600 Commerce Street, Suite 700 Dallas TX 75202"),
               _rec(PLAUSIBLE_A),
               _rec(IMPLAUSIBLE)]
    targets, stats = select_geocode_targets(records, ledger, cap=1,
                                            now_fn=_fixed_clock)
    assert [r["address"] for r in targets] == [PLAUSIBLE_B]  # cap=1 slices
    assert stats["skipped_previously_failed"] == 1  # counted even beyond cap
    assert stats["skipped_implausible"] == 1  # counted regardless of ledger


def test_select_with_retry_failed_reincludes_fresh_failures():
    ledger = {geocode_failure_key(PLAUSIBLE_A): _entry(GEOCODE_MAX_ATTEMPTS,
                                                       _iso(hours=2))}
    records = [_rec(PLAUSIBLE_A), _rec(PLAUSIBLE_B)]
    targets, stats = select_geocode_targets(records, ledger, cap=25,
                                            retry_failed=True,
                                            now_fn=_fixed_clock)
    assert len(targets) == 2
    assert stats["skipped_previously_failed"] == 0
