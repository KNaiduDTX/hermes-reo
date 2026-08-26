# src/net_retry.py
"""RL8 phase 1: generalized retry engine + the shared N2 decision policy.

``call_with_retry`` is the ONE attempt loop for outbound source calls: it
re-runs ``fn`` while ``is_retryable`` classifies the raised exception as
transient, sleeping an exponentially growing, jitter-de-synchronized delay
between attempts, and deliberately never sleeping after the final attempt.
It returns ``fn()``'s result or re-raises the last exception.

The pure decision helpers below re-express — verbatim — the semantics of
the former ``census._should_retry`` (only timeouts/transport errors and
HTTP >= 500 retry; any other exception fails fast; an exception outweighs a
supplied status). ``src/adapters/census.py`` now delegates to them and to
the src.errors family (TransientSourceError / PermanentSourceError /
ParseShapeError), so every adapter shares one vocabulary:

    is_retryable=is_transient_source_error   # retry Transient only

Fully unit-testable offline: per-attempt timing comes in via ``now_fn``,
backoff shaping via ``jitter_fn``, and the real sleeper sits behind the
module-level ``_sleep`` indirection so tests record waits instead of dozing.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional, Tuple, TypeVar

import httpx

from .errors import PermanentSourceError, TransientSourceError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# N2: the complete set of retryable exception types. TimeoutException is a
# TransportError subclass in httpx; both are listed for explicitness.
TRANSPORT_EXCEPTIONS: Tuple[type, ...] = (httpx.TimeoutException,
                                          httpx.TransportError)

# Smallest HTTP status code classified as transient (server-side fault).
RETRYABLE_MIN_STATUS = 500


# --- pure decision helpers (verbatim census._should_retry semantics) --------

def is_transient_exception(exc: BaseException) -> bool:
    """True iff *exc* belongs to the retryable httpx transport family."""
    return isinstance(exc, TRANSPORT_EXCEPTIONS)


def is_retryable_status(status: Optional[int]) -> bool:
    """True iff a bare HTTP *status* alone justifies another attempt."""
    return status is not None and status >= RETRYABLE_MIN_STATUS


def should_retry(exc: Optional[BaseException],
                 status: Optional[int]) -> bool:
    """Pure N2 retry policy (formerly ``census._should_retry``).

    Retry ONLY timeouts/transport errors and HTTP 5xx. When *exc* is given
    it decides alone: any non-transport exception (JSON decode errors,
    unexpected bugs) fails fast regardless of status. A bare status retries
    iff >= RETRYABLE_MIN_STATUS; a missing status with no exception (e.g. a
    connection refusal surfaced as neither) does not retry.
    """
    if exc is not None:
        return is_transient_exception(exc)
    return is_retryable_status(status)


def is_transient_source_error(exc: BaseException) -> bool:
    """Ready-made ``is_retryable`` predicate for the src.errors family."""
    return isinstance(exc, TransientSourceError)


# --- timing primitives -------------------------------------------------------

def backoff_delay(attempt: int, base_delay: float = 1.0) -> float:
    """Exponential backoff seconds for the given 1-based *attempt*."""
    return base_delay * (2 ** (attempt - 1))


def half_up_jitter(delay: float) -> float:
    """Uniform draw from [delay/2, delay].

    Concurrent workers de-synchronize while staying bounded by the plain
    backoff — the historic census jitter distribution.
    """
    return random.uniform(delay / 2.0, delay)


def _sleep(seconds: float) -> None:
    """Indirection point for time.sleep — tests patch this, not the clock."""
    time.sleep(seconds)


def call_with_retry(fn: Callable[[], T], *, is_retryable: Callable[[BaseException], bool],
                    max_attempts: int, base_delay: float,
                    jitter_fn: Optional[Callable[[float], float]] = None,
                    now_fn: Optional[Callable[[], float]] = None) -> T:
    """Run ``fn()`` under the shared retry policy: result or last exception.

    Parameters:
        fn: zero-argument callable performing ONE attempt; its return value
            is forwarded unchanged on success.
        is_retryable: predicate over the raised exception; True earns
            another attempt while attempts remain, False re-raises at once.
        max_attempts: total attempts INCLUDING the first; must be >= 1.
        base_delay: exponential backoff multiplier; the plain delay before
            attempt n+1 is ``base_delay * 2**n``. Must be >= 0.
        jitter_fn: maps the plain delay to the actual wait (default
            ``half_up_jitter``, uniform in [delay/2, delay]).
        now_fn: monotonic seconds clock (default ``time.monotonic``) used
            solely for per-attempt timing telemetry in debug logs.

    No sleep ever follows the final attempt, and non-retryable failures
    re-raise immediately without sleeping.
    """
    attempts = int(max_attempts)
    if attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
    if base_delay < 0:
        raise ValueError(f"base_delay must be >= 0, got {base_delay!r}")
    jitter = jitter_fn if jitter_fn is not None else half_up_jitter
    clock = now_fn if now_fn is not None else time.monotonic
    for attempt in range(1, attempts + 1):
        started = clock()
        try:
            return fn()
        except Exception as exc:
            elapsed = clock() - started
            if not is_retryable(exc) or attempt >= attempts:
                raise
            delay = jitter(backoff_delay(attempt, base_delay))
            logger.debug("net_retry attempt %d/%d failed after %.3fs: %s: %s"
                         " — retrying in %.3fs", attempt, attempts, elapsed,
                         type(exc).__name__, exc, delay)
            _sleep(delay)
    raise AssertionError("call_with_retry attempt loop fell through")  # pragma: no cover


if __name__ == "__main__":
    # Fully-offline self-check: decision matrix + engine mechanics.
    assert should_retry(httpx.ReadTimeout("timed out"), None) is True
    assert should_retry(httpx.ConnectError("down"), None) is True
    assert should_retry(None, 500) is True
    assert should_retry(None, 503) is True
    assert should_retry(None, 429) is False      # client error -> fail fast
    assert should_retry(ValueError("bad json"), 503) is False  # exc wins
    assert should_retry(None, None) is False

    assert tuple(backoff_delay(a, 0.5) for a in (1, 2, 3)) == (0.5, 1.0, 2.0)

    _orig_sleep = _sleep
    _sleep = lambda _seconds: None  # noqa: E731  (offline: elide backoff)
    try:
        state = {"n": 0}

        def _flaky() -> str:
            state["n"] += 1
            if state["n"] < 3:
                raise TransientSourceError("flaky", source="unit-test")
            return "payload"

        assert call_with_retry(_flaky, is_retryable=is_transient_source_error,
                               max_attempts=3, base_delay=1.0) == "payload"
        assert state["n"] == 3

        calls = {"n": 0}

        def _permanent() -> None:
            calls["n"] += 1
            raise PermanentSourceError("4xx", source="unit-test")

        try:
            call_with_retry(_permanent, is_retryable=is_transient_source_error,
                            max_attempts=3, base_delay=1.0)
            raise SystemExit("permanent failure should have propagated")
        except PermanentSourceError:
            pass
        assert calls["n"] == 1                      # failed fast, one attempt
    finally:
        _sleep = _orig_sleep

    print("net_retry.py: self-check passed "
          "(N2 decision matrix + engine fail-fast/exhaustion mechanics)")
