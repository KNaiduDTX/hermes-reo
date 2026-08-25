#!/usr/bin/env python3
# test_recalc.py — GATE-VAL + recalculation acceptance tests (pytest).
"""AGENT-REV acceptance pass, converted to asserting tests.

Verifies the LibreOffice-recalculated workbook (out/recalc/): headline outputs
are computed values (not formula errors), GATE-VAL is live and passing, the
S&U balance ties within tolerance, sensitivity grid computes, tax-deed
redemption math is correct, and the measured named-range count is sane.

Run:  .venv/bin/python -m pytest test_recalc.py     (or `make recalc`)
Skips cleanly when the recalculated workbook is absent.
"""
from pathlib import Path

import openpyxl
import pytest

REPO_ROOT = Path(__file__).resolve().parent
RECALC_WB = REPO_ROOT / "out" / "recalc" / "HERMES-REO_Model_Suite.xlsx"
BASE_WB = REPO_ROOT / "out" / "HERMES-REO_Model_Suite.xlsx"

# The workbook's own S&U check uses ABS(uses - sources) < 1 ("PASS"/"FAIL").
BALANCE_TOLERANCE = 1.0
MIN_NAMED_RANGES = 85

pytestmark = pytest.mark.recalc

if not RECALC_WB.exists():
    pytest.skip(
        f"recalculated workbook absent: {RECALC_WB} — run 'make recalc' first",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def wb():
    return openpyxl.load_workbook(RECALC_WB, data_only=True)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_valid_text(v) -> bool:
    # "PASS", "MARGINAL — review", "BLOCKED …" are valid verdicts; "#REF!"-style
    # error literals are not.
    return isinstance(v, str) and v.strip() != "" and not v.lstrip().startswith("#")


HEADLINE_CELLS = {
    "Fix-and-Flip": [("B42", "Net profit"), ("B44", "ROI"), ("B51", "MAO")],
    "BRRRR": [("B16", "Refi loan"), ("B19", "Cash left in"), ("B20", "% recycled"), ("B14", "DSCR")],
    "Deal Screener": [("B10", "MAO"), ("B12", "1% rule"), ("B14", "Cap rate"), ("B17", "Verdict")],
    "Comp & ARV": [("B14", "Weighted ARV"), ("B22", "GATE-VAL verdict")],
    "Tax-Deed": [("B14", "Premium yr1"), ("B17", "Guaranteed return"), ("B18", "Annualized yield")],
    "Sensitivity": [("C8", "Base case profit"), ("B6", "Top-left stress")],
    "Rental DCF": [("B29", "Cap rate yr1"), ("B30", "CoC yr1"), ("B31", "DSCR yr1")],
    "Auction Bid": [("B13", "Bid ceiling")],
    "Wholesale": [("B11", "Wholesale MAO"), ("B13", "Spread"), ("B24", "Compliance")],
    "Subject-To": [("B11", "Equity captured"), ("B15", "CoC")],
}


def test_headline_outputs_are_formula_computed(wb):
    """[1] Every headline cell holds a number or a valid text verdict — never an
    Excel error literal (#VALUE!, #NAME?, #REF!), i.e. formulas actually recalculated."""
    bad = []
    for tab, cells in HEADLINE_CELLS.items():
        ws = wb[tab]
        for cell, label in cells:
            v = ws[cell].value
            if not (_is_number(v) or _is_valid_text(v)):
                bad.append(f"{tab}!{cell} ({label}) = {v!r}")
            elif _is_number(v):
                # numeric cells must be genuinely numeric-typed, not numeric strings
                assert isinstance(v, (int, float))
    assert not bad, "formula-error or empty headline cells:\n  " + "\n  ".join(bad)


def test_gate_val_verdict_is_live_and_passing(wb):
    """[2] GATE-VAL verdict equality: 'PASS' with a True pass flag."""
    ws = wb["Comp & ARV"]
    assert ws["B22"].value == "PASS", f"GATE-VAL verdict = {ws['B22'].value!r}"
    assert ws["B23"].value is True, "GATE-VAL pass flag is not True"


def test_sources_and_uses_balance_within_tolerance(wb):
    """[3] S&U ties: |total uses − (debt + equity)| <= $1 and the workbook's own
    balance check reports PASS."""
    su_check = wb["Checks"]["B13"].value
    assert isinstance(su_check, str) and su_check.startswith("PASS"), \
        f"Checks!B13 S&U tie = {su_check!r}"

    ff = wb["Fix-and-Flip"]
    uses = ff["B10"].value
    debt = ff["B13"].value
    equity = ff["B14"].value
    for name, v in (("uses", uses), ("debt", debt), ("equity", equity)):
        assert _is_number(v), f"Fix-and-Flip {name} is not numeric: {v!r}"
    assert abs(uses - (debt + equity)) <= BALANCE_TOLERANCE, (
        f"S&U imbalance ${uses - (debt + equity):,.2f} exceeds ${BALANCE_TOLERANCE:.0f}"
    )


def test_sensitivity_grid_computes(wb):
    """[4] Sensitivity grid corners are formula-driven numbers."""
    ws = wb["Sensitivity"]
    center = ws.cell(row=8, column=3).value      # base case
    top_left = ws.cell(row=6, column=2).value    # stress corner
    bottom_right = ws.cell(row=10, column=5).value
    for label, v in (("center C8", center), ("top-left B6", top_left),
                     ("bottom-right E10", bottom_right)):
        assert _is_number(v), f"Sensitivity {label} is not a computed number: {v!r}"


def test_deal_screener_verdict_present(wb):
    """[5] Deal Screener emits a real verdict and binding constraint."""
    ws = wb["Deal Screener"]
    verdict = ws["B17"].value
    constraint = ws["B18"].value
    assert _is_valid_text(verdict), f"Deal Screener B17 verdict = {verdict!r}"
    assert _is_valid_text(constraint), f"Deal Screener B18 constraint = {constraint!r}"


def test_tax_deed_redemption_math(wb):
    """[6] Redemption math: premium yr1 ≈ $25,500 ($102k × 25%), return + yield numeric."""
    ws = wb["Tax-Deed"]
    premium = ws["B14"].value
    guaranteed_return = ws["B17"].value
    ann_yield = ws["B18"].value
    assert _is_number(premium), f"Premium yr1 not numeric: {premium!r}"
    assert premium == pytest.approx(25_500, abs=1.0), \
        f"Redemption premium {premium!r} ≠ $102k × 25% = $25,500"
    assert _is_number(guaranteed_return) and guaranteed_return > 0, \
        f"Guaranteed return not a positive number: {guaranteed_return!r}"
    assert _is_number(ann_yield) and 0 < ann_yield < 10, \
        f"Annualized yield implausible: {ann_yield!r}"


def test_brrrr_capital_recycle_numeric(wb):
    """[7] BRRRR refi stack computes numerically with a verdict string."""
    ws = wb["BRRRR"]
    refi = ws["B16"].value
    cash_left = ws["B19"].value
    recycled = ws["B20"].value
    dscr = ws["B14"].value
    verdict = ws["B28"].value
    for label, v in (("refi loan B16", refi), ("cash left B19", cash_left),
                     ("% recycled B20", recycled), ("DSCR B14", dscr)):
        assert _is_number(v), f"BRRRR {label} is not numeric: {v!r}"
    assert _is_valid_text(verdict), f"BRRRR B28 verdict = {verdict!r}"


def test_named_ranges_measured():
    """Measured named-range count on the built workbook is at least 85."""
    if not BASE_WB.exists():
        pytest.skip(f"built workbook absent: {BASE_WB} — run 'make build' first")
    wbn = openpyxl.load_workbook(BASE_WB)
    n_names = len(list(wbn.defined_names))
    assert n_names >= MIN_NAMED_RANGES, (
        f"only {n_names} named ranges (expected >= {MIN_NAMED_RANGES})"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
