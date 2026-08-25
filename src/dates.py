# src/dates.py
"""Single date-parsing entry point.

AUDIT findings H6/X4/X5/S4 all trace back to dates being parsed ad hoc (or
not at all): lexicographic MM/DD/YYYY lien sorting, a stale-notice check
that only accepted ISO strings, and fabricated fallback dates. Every module
parses dates through parse_date_any() or not at all.
"""
from __future__ import annotations

import re
from datetime import date, datetime

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"], 1
    )
}

# "Tuesday, the 3rd day of March, 2026" / "the 25th day of July, 2013"
_PROSE_RE = re.compile(
    r"the\s+(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+([A-Za-z]+),?\s+(\d{4})",
    re.IGNORECASE,
)
# "7/25/2013", "7-25-2013" (US convention: month/day/year)
_NUMERIC_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")


def parse_date_any(value) -> date | None:
    """Parse a date from any format the pipeline actually produces.

    Supported: datetime.date/datetime passthrough, ISO YYYY-MM-DD,
    US M/D/Y or M-D-Y, and Dallas prose form ("Tuesday, the 3rd day of
    March, 2026"). Returns None for anything unparseable (including the
    "Unknown" sentinel) -- callers MUST treat None as unknown, never as
    "current".
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.lower() == "unknown":
        return None

    m = _NUMERIC_RE.match(s)
    if m:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(yr, mo, dy)
        except ValueError:
            return None

    m = _PROSE_RE.search(s)
    if m:
        day, mon_name, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = _MONTHS.get(mon_name)
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                return None

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    # ISO with time component ("2026-03-03T10:00:00")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def days_between(later: date, earlier: date) -> int:
    """Positive when *later* is after *earlier*."""
    return (later - earlier).days


def self_check() -> None:
    today = date(2026, 8, 24)
    assert parse_date_any("2026-03-03") == date(2026, 3, 3)
    assert parse_date_any("7/25/2013") == date(2013, 7, 25)
    assert parse_date_any("Tuesday, the 3rd day of March, 2026") == date(2026, 3, 3)
    assert parse_date_any("12/01/2019") == date(2019, 12, 1)
    assert parse_date_any("Unknown") is None
    assert parse_date_any("") is None
    assert parse_date_any(None) is None
    assert parse_date_any("not a date") is None
    assert days_between(today, date(2026, 2, 24)) == 181
    # Lexicographic trap that corrupted lien priority (finding S4):
    assert parse_date_any("9/01/2019") < parse_date_any("10/15/2020")


if __name__ == "__main__":
    self_check()
    print("dates.py: parse self-check passed")
