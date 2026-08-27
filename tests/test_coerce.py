"""Audit CPR-7: shared numeric-coercion contract (src/coerce.py).

Regression: the enrichment parser stripped ALL non-digits, so
"_parse_int('12.5')" was 125 - digits concatenated across the decimal
point - and "Lot 3, 12 Elm St" became lot-size 312. Everything routes
through src/coerce now.
"""
import pytest

from src.coerce import to_float, to_int, to_money


class TestToInt:
    @pytest.mark.parametrize("val,expected", [
        ("12.5", 13),        # half-up; NEVER 125 (the regression)
        ("12.4", 12),
        ("1,234 SqFt", 1234),
        ("$1,234", 1234),
        (1998, 1998),
        ("  42 units ", 42),
    ])
    def test_parses_first_numeric_token(self, val, expected):
        assert to_int(val) == expected

    @pytest.mark.parametrize("val", [
        None, "", "Unknown", "garbage", "N/A", True,
    ])
    def test_unparseable_is_none(self, val):
        assert to_int(val) is None

    def test_first_token_wins_predictably(self):
        # "Lot 3, 12 Elm" -> first numeric token is 3; the contract is
        # first-token, not digit-concatenation ("12.5" never becomes 125).
        assert to_int("Lot 3, 12 Elm") == 3

    def test_range_violation_is_none_not_clamped(self):
        assert to_int("12.5", lo=0, hi=10) is None
        assert to_int("300", lo=0, hi=50) is None
        assert to_int("12", lo=0, hi=50) == 12


class TestToFloat:
    def test_decimal_parsed_not_collapsed(self):
        assert to_float("12.5") == 12.5
        assert to_float("$1,234.56") == 1234.56
        assert to_float("12.5 %") == 12.5

    def test_garbage_is_none(self):
        for v in (None, "", "Unknown", "n/a"):
            assert to_float(v) is None

    def test_range(self):
        assert to_float("-3", lo=0) is None
        assert to_float("3.5", lo=0, hi=3) is None


class TestToMoney:
    def test_commas_and_currency(self):
        assert to_money("$412,300.00") == 412300.0

    def test_negative_rejected_by_default(self):
        assert to_money("-500") is None

    def test_hi_guard(self):
        assert to_money("3000000000000") is None  # 3e12 > default hi
        assert to_money("1000000000") == 1e9
