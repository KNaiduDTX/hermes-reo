# src/errors.py
"""RL8 phase 1: shared exception family for external source adapters.

Every adapter that talks to an outside system (Census geocoder, county
portals, clerk sites, ...) classifies its failures into exactly three
shapes so retry engines and callers can react uniformly instead of
pattern-matching vendor-specific exception types:

- TransientSourceError — worth retrying: timeouts, connection resets,
  other transport faults, HTTP 5xx. The engine in src/net_retry.py retries
  these and nothing else.
- PermanentSourceError — never worth retrying: HTTP 4xx, rejected auth,
  unusable request URLs. Fail fast and surface the reason.
- ParseShapeError — the response arrived but cannot be used: undecodable
  JSON, missing expected keys. Fail fast; re-requesting identical bytes is
  futile.

All three carry a ``.source`` attribute naming the upstream system (e.g.
``"census"``), so a single ``except SourceError`` handler can log or route
by origin.
"""
from __future__ import annotations

from typing import Optional


class SourceError(Exception):
    """Base class for classified upstream-source failures.

    ``source`` identifies the upstream system the failure came from. It is
    always readable (class-level ``None`` until set), so handlers never need
    an hasattr guard.
    """

    source: Optional[str] = None

    def __init__(self, message: str,
                 source: Optional[str] = None) -> None:
        super().__init__(message)
        if source is not None:
            self.source = source


class TransientSourceError(SourceError):
    """Retryable upstream failure: timeout, transport error, HTTP 5xx."""


class PermanentSourceError(SourceError):
    """Non-retryable upstream failure: HTTP 4xx and friends — fail fast."""


class ParseShapeError(SourceError):
    """Response arrived but was unusable: bad JSON, unexpected shape."""
