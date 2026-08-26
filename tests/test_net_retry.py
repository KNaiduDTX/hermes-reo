"""RL8 phase 1: net_retry engine + census migration parity tests (offline).

Covers, with zero network:
- the shared N2 decision helpers (table-driven truth table — the exact
  semantics formerly encoded in census._should_retry),
- call_with_retry mechanics: exhaustion vs fail-fast, backoff/jitter bounds
  via an injected rng and identity jitter, pluggable predicates and clocks,
- the census adapter over httpx.MockTransport: success / retry / permanent /
  invalid-JSON / transport-exhaustion cases with recorded (not slept) waits,
- parity pins: public signatures, status vocabulary, and the removed private
  duplicates.

Backoff waits are captured through net_retry._sleep so the whole module runs
instantly; clocks are injected.
"""
from __future__ import annotations

import inspect
import logging
import random

import httpx
import pytest

from src import net_retry
from src.adapters import census
from src.errors import (PermanentSourceError, ParseShapeError, SourceError,
                        TransientSourceError)
from src.net_retry import (TRANSPORT_EXCEPTIONS, backoff_delay,
                           call_with_retry, half_up_jitter,
                           is_retryable_status, should_retry)


# ------------------------------------------------------------- fixtures -----

@pytest.fixture(autouse=True)
def waits(monkeypatch):
    """Record backoff waits instead of sleeping (keeps the suite instant)."""
    recorded: list[float] = []
    monkeypatch.setattr(net_retry, "_sleep", recorded.append)
    return recorded


def _route_through(monkeypatch, handler):
    """Point every census httpx.Client at ``handler`` for one test.

    census.httpx IS the global httpx module, so this is reverted by
    monkeypatch automatically after each test.
    """
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(census.httpx, "Client", factory)


class Flaky:
    """Callable that raises queued exceptions, then returns its value."""

    def __init__(self, script, value="ok"):
        self.script = list(script)
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.script:
            raise self.script.pop(0)
        return self.value


# -------------------------------------------------------- error family ------

class TestErrorFamily:
    def test_all_three_share_sourceerror_and_carry_source(self):
        for cls in (TransientSourceError, PermanentSourceError,
                    ParseShapeError):
            assert issubclass(cls, SourceError)
            assert issubclass(cls, Exception)
            err = cls("upstream down", source="county-clerk")
            assert str(err) == "upstream down"
            assert err.source == "county-clerk"

    def test_source_defaults_to_none_but_is_always_readable(self):
        assert SourceError("x").source is None
        assert TransientSourceError("x").source is None
        assert PermanentSourceError("x").source is None
        assert ParseShapeError("x").source is None

    def test_classes_are_mutually_distinct(self):
        family = (TransientSourceError, PermanentSourceError, ParseShapeError)
        for a in family:
            for b in family:
                if a is not b:
                    assert not issubclass(a, b)

    def test_transient_predicate_discriminates_the_family(self):
        assert net_retry.is_transient_source_error(
            TransientSourceError("t")) is True
        assert net_retry.is_transient_source_error(
            PermanentSourceError("p")) is False
        assert net_retry.is_transient_source_error(
            ParseShapeError("s")) is False
        assert net_retry.is_transient_source_error(ValueError("v")) is False


# --------------------------------------------- N2 decision truth table ------

RETRY_MATRIX = [
    # (exc, status, expected) — the exact former census._should_retry table.
    (httpx.ConnectTimeout("timed out"), None, True),
    (httpx.ReadTimeout("timed out"), None, True),
    (httpx.ConnectError("transport down"), None, True),
    (httpx.ReadError("reset by peer"), None, True),
    (None, 500, True),
    (None, 502, True),
    (None, 503, True),
    (None, 599, True),
    (None, 400, False),          # client error -> fail fast
    (None, 404, False),
    (None, 429, False),          # historic policy: still fail fast
    (None, 301, False),
    (None, 200, False),
    (ValueError("bad json"), 200, False),   # JSON decode -> fail fast...
    (ValueError("bad json"), 503, False),   # ...even against a 5xx status
    (KeyError("bug"), None, False),         # unexpected exc never retries
    (PermanentSourceError("4xx"), 500, False),  # typed ≠ transport family
    (ParseShapeError("shape"), 503, False),
    # the pure N2 policy judges RAW exceptions only — a typed error is not in
    # the httpx transport family, so it does not retry here; typed retry
    # decisions flow through is_transient_source_error at the engine instead
    (TransientSourceError("5xx"), None, False),
    (None, None, False),        # neither signal -> no retry
]


@pytest.mark.parametrize("exc,status,expected", RETRY_MATRIX)
def test_should_retry_truth_table(exc, status, expected):
    assert should_retry(exc, status) is expected


@pytest.mark.parametrize("exc,status,expected", RETRY_MATRIX)
def test_predicates_decompose_the_decision(exc, status, expected):
    if exc is not None:
        # with an exception present, the transport-family check decides alone
        assert should_retry(exc, status) is \
            net_retry.is_transient_exception(exc)
    else:
        assert is_retryable_status(status) is expected


def test_transport_family_tuple_shape_and_census_alias():
    assert TRANSPORT_EXCEPTIONS == (httpx.TimeoutException,
                                    httpx.TransportError)
    # kept as a re-export so existing importers see one name
    assert census.RETRYABLE_EXCEPTIONS is TRANSPORT_EXCEPTIONS


def test_census_private_duplicates_removed():
    # the logic moved to net_retry; census must not carry stale copies
    for name in ("_should_retry", "_backoff_delay", "_sleep_backoff"):
        assert not hasattr(census, name)


# ----------------------------------------------------- engine mechanics -----

def test_success_first_try_makes_one_call_and_never_sleeps(waits):
    fn = Flaky([])
    assert call_with_retry(fn, is_retryable=lambda e: True,
                           max_attempts=3, base_delay=1.0) == "ok"
    assert fn.calls == 1
    assert waits == []


def test_transient_then_success_retries_with_bounded_jitter(waits):
    fn = Flaky([TransientSourceError("a"), TransientSourceError("b")],
               value="payload")
    assert call_with_retry(fn, is_retryable=net_retry.is_transient_source_error,
                           max_attempts=3, base_delay=1.0) == "payload"
    assert fn.calls == 3
    assert len(waits) == 2                       # no terminal sleep
    assert 0.5 <= waits[0] <= 1.0                # jitter of 2**0 * base
    assert 1.0 <= waits[1] <= 2.0                # jitter of 2**1 * base


def test_identity_jitter_yields_exact_exponential_delays(waits):
    fn = Flaky([TransientSourceError("x"), TransientSourceError("y")])
    call_with_retry(fn, is_retryable=lambda e: True, max_attempts=3,
                    base_delay=2.0, jitter_fn=lambda d: d)
    assert waits == [2.0, 4.0]


def test_exhaustion_raises_the_last_transient_after_all_attempts(waits):
    last = TransientSourceError("final")
    fn = Flaky([TransientSourceError("1"), TransientSourceError("2"), last])
    with pytest.raises(TransientSourceError) as err:
        call_with_retry(fn, is_retryable=net_retry.is_transient_source_error,
                        max_attempts=3, base_delay=1.0)
    assert err.value is last                     # the LAST failure propagates
    assert fn.calls == 3
    assert len(waits) == 2                       # sleeps only BETWEEN attempts


def test_permanent_error_fails_fast_on_first_attempt(waits):
    fn = Flaky([PermanentSourceError("HTTP 404")])
    with pytest.raises(PermanentSourceError):
        call_with_retry(fn, is_retryable=net_retry.is_transient_source_error,
                        max_attempts=3, base_delay=1.0)
    assert fn.calls == 1
    assert waits == []


def test_parse_shape_error_fails_fast_without_sleeping(waits):
    fn = Flaky([ParseShapeError("invalid JSON")])
    with pytest.raises(ParseShapeError):
        call_with_retry(fn, is_retryable=net_retry.is_transient_source_error,
                        max_attempts=3, base_delay=1.0)
    assert fn.calls == 1
    assert waits == []


def test_transient_then_permanent_stops_mid_sequence(waits):
    fn = Flaky([TransientSourceError("blip"), PermanentSourceError("404")])
    with pytest.raises(PermanentSourceError):
        call_with_retry(fn, is_retryable=net_retry.is_transient_source_error,
                        max_attempts=3, base_delay=1.0)
    assert fn.calls == 2                         # stopped at the 404
    assert len(waits) == 1                       # slept once, before attempt 2


def test_unrelated_exception_propagates_untouched(waits):
    boom = ValueError("unexpected bug")
    fn = Flaky([boom])
    with pytest.raises(ValueError) as err:
        call_with_retry(fn, is_retryable=lambda e: False,
                        max_attempts=5, base_delay=1.0)
    assert err.value is boom
    assert fn.calls == 1
    assert waits == []


def test_pluggable_predicate_can_retry_anything(waits):
    fn = Flaky([OSError("e1"), OSError("e2"), OSError("e3")])
    with pytest.raises(OSError):
        call_with_retry(fn, is_retryable=lambda e: True,
                        max_attempts=3, base_delay=0)
    assert fn.calls == 3


def test_zero_base_delay_never_waits_even_under_default_jitter(waits):
    fn = Flaky([TransientSourceError("x"), TransientSourceError("y")])
    assert call_with_retry(fn, is_retryable=lambda e: True,
                           max_attempts=3, base_delay=0) == "ok"
    assert waits == [0.0, 0.0]                   # uniform(0, 0)


def test_result_passthrough_keeps_object_identity():
    sentinel = {"payload": True}
    out = call_with_retry(lambda: sentinel, is_retryable=lambda e: False,
                          max_attempts=1, base_delay=1.0)
    assert out is sentinel


@pytest.mark.parametrize("kwargs", [
    {"max_attempts": 0},
    {"max_attempts": -3},
])
def test_max_attempts_below_one_rejected_before_any_call(kwargs, waits):
    fn = Flaky([])
    with pytest.raises(ValueError):
        call_with_retry(fn, is_retryable=lambda e: True,
                        base_delay=1.0, **kwargs)
    assert fn.calls == 0
    assert waits == []


def test_negative_base_delay_rejected():
    with pytest.raises(ValueError):
        call_with_retry(lambda: "x", is_retryable=lambda e: True,
                        max_attempts=1, base_delay=-1.0)


def test_now_fn_clock_drives_per_attempt_telemetry(waits):
    ticks = []

    def clock() -> float:
        ticks.append(1)
        return float(len(ticks))

    def flaky():
        if len(ticks) < 2:                       # first attempt fails
            raise TransientSourceError("once")
        return "recovered"

    assert call_with_retry(flaky, is_retryable=net_retry.is_transient_source_error,
                           max_attempts=2, base_delay=0,
                           now_fn=clock) == "recovered"
    # failed attempt consumes two reads (start + elapsed); success one (start)
    assert len(ticks) == 3
    assert len(waits) == 1


def test_engine_audit_single_loop_single_sleep_no_terminal_wait():
    src = inspect.getsource(call_with_retry)
    loop = [ln for ln in src.splitlines() if "for attempt in range" in ln]
    assert len(loop) == 1
    assert src.count("_sleep(") == 1
    tail = src.split("return fn()", 1)[1]
    assert "raise" in tail.split("_sleep(")[0], \
        "fail-fast raise must precede the only sleep"


# ------------------------------------------------ backoff/jitter bounds -----

class TestBackoffAndJitter:
    @pytest.mark.parametrize("base,expected", [
        (1.0, [1.0, 2.0, 4.0, 8.0, 16.0]),
        (0.5, [0.5, 1.0, 2.0, 4.0, 8.0]),
        (0.25, [0.25, 0.5, 1.0, 2.0, 4.0]),
    ])
    def test_backoff_sequence_is_exact(self, base, expected):
        assert [backoff_delay(attempt, base)
                for attempt in range(1, 6)] == expected

    def test_default_backoff_matches_census_history(self):
        assert [backoff_delay(a) for a in (1, 2, 3)] == \
            [census.BACKOFF_BASE * 2 ** (a - 1) for a in (1, 2, 3)]

    def test_half_up_jitter_stays_within_half_to_full_delay(self, monkeypatch):
        rng = random.Random(1234)
        monkeypatch.setattr(net_retry.random, "uniform", rng.uniform)
        for attempt in range(1, 7):
            delay = backoff_delay(attempt, base_delay=0.25)
            for _ in range(200):
                drawn = half_up_jitter(delay)
                assert delay / 2 <= drawn <= delay

    def test_custom_jitter_fn_shapes_the_actual_wait(self, waits):
        fn = Flaky([TransientSourceError("x")])
        call_with_retry(fn, is_retryable=lambda e: True, max_attempts=2,
                        base_delay=10.0, jitter_fn=lambda d: d / 10.0)
        assert waits == [1.0]


# ------------------------------- census over MockTransport (RL8 wiring) -----

ADDR_OK_PAYLOAD = {
    "result": {"addressMatches": [{
        "coordinates": {"x": -96.8818, "y": 32.9168},
        "geographies": {
            "Census Tracts": [{"TRACT": "020331"}],
            "Census Blocks": [{"BLKGRP": "3", "BLOCK": "3033"}],
        },
    }]},
}

COORD_OK_PAYLOAD = {
    "result": {"geographies": {
        "Census Tracts": [{"TRACT": "010100", "COUNTY": "113", "STATE": "48"}],
        "Census Blocks": [{"BLKGRP": "1", "BLOCK": "1011"}],
    }},
}


def _counting(codes):
    """Handler returning queued status codes (then repeating the last)."""
    hits = []
    queue = list(codes)

    def handler(request):
        hits.append(request.url.params.get("address"))
        code = queue.pop(0) if queue else codes[-1]
        return httpx.Response(code)

    return handler, hits


class TestCensusMockTransport:
    def test_geocode_address_success_contract(self, monkeypatch, waits):
        hits = []

        def ok(request):
            hits.append(len(hits))
            return httpx.Response(200, json=ADDR_OK_PAYLOAD)

        _route_through(monkeypatch, ok)
        geo = census.geocode_address("10022 Plainsman Ln, Frisco, TX")
        assert geo == {
            "latitude": 32.9168,
            "longitude": -96.8818,
            "census_tract": "020331",
            "block_group": "3",
            "county_fips": None,
            "state_fips": None,
            "status": "Success",
        }
        assert len(hits) == 1                        # single HTTP hit
        assert waits == []                           # success never sleeps

    def test_geocode_address_no_match_shape_preserved(self, monkeypatch):
        empty = dict(ADDR_OK_PAYLOAD)
        empty["result"] = {"addressMatches": []}
        _route_through(monkeypatch,
                       lambda request: httpx.Response(200, json=empty))
        assert census.geocode_address("nowhere at all") == \
            {"status": "No Matches Found"}

    def test_5xx_retried_then_success_records_bounded_waits(
            self, monkeypatch, waits):
        state = {"calls": 0}

        def flaky_server(request):
            state["calls"] += 1
            if state["calls"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, json=ADDR_OK_PAYLOAD)

        _route_through(monkeypatch, flaky_server)
        geo = census.geocode_address("10022 Plainsman Ln, Frisco, TX")
        assert geo["status"] == "Success"
        assert state["calls"] == 3
        assert len(waits) == 2
        assert 0.5 <= waits[0] <= 1.0
        assert 1.0 <= waits[1] <= 2.0

    def test_4xx_fails_fast_after_exactly_one_request(self, monkeypatch, waits):
        handler, hits = _counting([404])
        _route_through(monkeypatch, handler)
        assert census.geocode_address("gone") == \
            {"status": "Census HTTP 404 — failing fast"}
        assert len(hits) == 1
        assert waits == []

    def test_persistent_5xx_exhausts_all_attempts(self, monkeypatch, waits):
        handler, hits = _counting([503, 503, 503])
        _route_through(monkeypatch, handler)
        assert census.geocode_address("down") == \
            {"status": "Failed after max retries"}
        assert len(hits) == census.MAX_RETRIES
        assert len(waits) == census.MAX_RETRIES - 1  # none after the final try

    def test_transport_errors_exhaust_and_map_to_history_string(
            self, monkeypatch, waits):
        hits = []

        def dead_network(request):
            hits.append(1)
            raise httpx.ConnectError("connection refused")

        _route_through(monkeypatch, dead_network)
        assert census.geocode_address("unreachable") == \
            {"status": "Failed after max retries"}
        assert len(hits) == census.MAX_RETRIES
        assert len(waits) == census.MAX_RETRIES - 1

    def test_invalid_json_fails_fast_as_parse_shape(self, monkeypatch, waits):
        seen = []
        _route_through(monkeypatch, lambda request: (
            seen.append(1) or httpx.Response(200,
                                             content=b"<html>nope</html>")))
        assert census.geocode_address("junk response") == \
            {"status": "Census returned invalid JSON"}
        assert len(seen) == 1
        assert waits == []

    def test_geocode_coordinates_success_contract(self, monkeypatch):
        _route_through(monkeypatch,
                       lambda request: httpx.Response(200,
                                                      json=COORD_OK_PAYLOAD))
        geo = census.geocode_coordinates(-96.8818, 32.9168)
        assert geo == {
            "census_tract": "010100",
            "block_group": "1",
            "block": "1011",
            "county_fips": "113",
            "state_fips": "48",
            "status": "Success",
        }

    def test_geocode_coordinates_empty_geographies_status(self, monkeypatch):
        _route_through(monkeypatch, lambda request: httpx.Response(
            200, json={"result": {"geographies": {}}}))
        assert census.geocode_coordinates(-96.8818, 32.9168) == \
            {"status": "No Census Geographies Resolved"}

    def test_request_json_accepts_transport_kwarg_directly(self, waits):
        hits = []

        def handler(request):
            hits.append(1)
            return httpx.Response(200, json={"ok": True})

        payload, err = census._request_json(
            census.CENSUS_ADDR_URL, {}, 5,
            transport=httpx.MockTransport(handler))
        assert (payload, err) == ({"ok": True}, None)
        assert hits == [1]
        assert waits == []

    def test_warning_chatter_preserved_on_client_error(self, monkeypatch,
                                                       caplog):
        handler, _ = _counting([404])
        _route_through(monkeypatch, handler)
        with caplog.at_level(logging.WARNING, logger="src.adapters.census"):
            census.geocode_address("gone")
        messages = [record.getMessage() for record in caplog.records]
        assert any("got HTTP 404" in message for message in messages)


# --------------------------------------- contract parity with pre-RL8 -------

class TestContractParity:
    def test_public_signatures_unchanged(self):
        assert list(inspect.signature(
            census.geocode_address).parameters) == ["address"]
        assert list(inspect.signature(
            census.geocode_coordinates).parameters) == ["lng", "lat"]
        assert list(inspect.signature(
            census.enrich_batch).parameters) == ["records", "batch_limit",
                                                 "ledger", "now_fn"]

    def test_policy_constants_unchanged(self):
        assert census.MAX_RETRIES == 3
        assert census.BACKOFF_BASE == 1.0
        assert census.CENSUS_SUCCESS_STATUS == "Success"

    @pytest.mark.parametrize("status", [301, 400, 404, 429])
    def test_client_error_statuses_fail_fast_verbatim(self, monkeypatch,
                                                      status):
        handler, hits = _counting([status])
        _route_through(monkeypatch, handler)
        _, err = census._request_json(census.CENSUS_ADDR_URL, {}, 5)
        assert err == f"Census HTTP {status} — failing fast"
        assert len(hits) == 1

    @pytest.mark.parametrize("status", [500, 503])
    def test_server_error_statuses_exhaust_to_history_string(
            self, monkeypatch, status, waits):
        handler, hits = _counting([status])
        _route_through(monkeypatch, handler)
        _, err = census._request_json(census.CENSUS_ADDR_URL, {}, 5)
        assert err == "Failed after max retries"
        assert len(hits) == census.MAX_RETRIES
        assert len(waits) == census.MAX_RETRIES - 1
