"""Date-parsing regression tests (audit H6/X4/X5/S4)."""
from datetime import date

from src.dates import days_between, parse_date_any


def test_iso():
    assert parse_date_any("2026-03-03") == date(2026, 3, 3)


def test_us_numeric():
    assert parse_date_any("7/25/2013") == date(2013, 7, 25)
    assert parse_date_any("12-01-2019") == date(2019, 12, 1)


def test_dallas_prose_form():
    assert parse_date_any("Tuesday, the 3rd day of March, 2026") == date(2026, 3, 3)


def test_unknown_sentinel_is_none():
    assert parse_date_any("Unknown") is None
    assert parse_date_any("") is None
    assert parse_date_any(None) is None
    assert parse_date_any("garbage") is None


def test_lexicographic_trap_is_dead():
    """The MM/DD/YYYY string-sort bug that corrupted lien priority (S4)."""
    a = parse_date_any("9/01/2019")
    b = parse_date_any("10/15/2020")
    assert a < b  # as dates; lexicographically "9/01..." > "10/15..."


def test_days_between():
    assert days_between(date(2026, 8, 24), date(2026, 5, 26)) == 90
