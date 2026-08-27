# src/coerce.py
"""Single numeric-coercion contract (audit CPR-7 / TD7 completion).

Six duplicated coercion helpers used to disagree on failure semantics, and
the enrichment parser stripped ALL non-digits, so "_parse_int('12.5')" was
125. Every money/area/room parse now goes through here:

* parse the FIRST numeric token only ("$1,234.56 SqFt" -> 1234.56);
* anything unparseable (None, "", "Unknown", "garbage") -> None — never 0,
  never a digit-collapsed accident;
* optional inclusive [lo, hi] range: a value outside it is treated as
  unparseable (None), never silently clamped — a 5-digit bedroom count is
  corruption, not a big house.
"""
from __future__ import annotations

import re
from typing import Optional

# First number token: optional currency sign, digits with , group separators,
# optional decimal part. "12.5" -> 12.5 (NOT 125); "1,234" -> 1234;
# "Lot 3 12 Elm" -> None (bare 3 is not a measurement).
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _first_number(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, bool):  # bool is an int subclass; never a measurement
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if f == f and abs(f) != float("inf") else None
    m = _NUM_RE.search(str(val).replace("$", "").replace("%", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def to_float(val, lo: Optional[float] = None,
             hi: Optional[float] = None) -> Optional[float]:
    """First numeric token as float; None on failure or out of [lo, hi]."""
    f = _first_number(val)
    if f is None:
        return None
    if lo is not None and f < lo:
        return None
    if hi is not None and f > hi:
        return None
    return f


def to_int(val, lo: Optional[int] = None,
           hi: Optional[int] = None) -> Optional[int]:
    """First numeric token as int (half-up rounding); None on failure/range.

    "12.5" -> 12 (rounded), NOT 125 — the old digit-stripping parser
    concatenated digits across the decimal point (audit CPR-7).
    """
    f = _first_number(val)
    if f is None:
        return None
    i = int(f + 0.5) if f >= 0 else -int(-f + 0.5)
    if lo is not None and i < lo:
        return None
    if hi is not None and i > hi:
        return None
    return i


def to_money(val, lo: float = 0.0,
             hi: float = 1e12) -> Optional[float]:
    """Currency-tolerant float (commas/$), default range [0, 1e12]."""
    return to_float(val, lo=lo, hi=hi)


def self_check() -> None:
    # Audit CPR-7 contract pins
    assert to_int("12.5") == 13 and to_int("12.4") == 12
    assert to_int("1,234 SqFt") == 1234
    assert to_int("Unknown") is None and to_int("") is None and to_int(None) is None
    assert to_int("12.5", lo=0, hi=10) is None  # range violation -> None, not clamp
    assert to_float("$1,234.56") == 1234.56
    assert to_float("garbage") is None
    assert to_int(True) is None  # booleans are not measurements
    assert to_money("-5") is None  # negative money rejected by default
    print("coerce.py: numeric coercion contract self-check passed")


if __name__ == "__main__":
    self_check()
