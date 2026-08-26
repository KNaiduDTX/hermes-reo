"""F6 regression tests: the complete deal-type underwriting router.

Every deal type must route to a real model with its name and deal_type
stamped — SHORT/SUBTO used to fall through to flip economics and wholesale
dropped closing costs. Unknown deal types must fail loudly, never default.
"""
import pytest

from src.cpr import CPR
from src.legal_rules import DealType
from src.underwriting import (
    SHORT_SALE_APPROVAL_MONTHS_DEFAULT,
    route_underwrite,
    short_sale_model,
    subject_to_model,
)

# Router kwargs per deal type; every purchase-price model names its inputs
# explicitly (AUDIT H1 — no MAO defaults).
ROUTER_MATRIX = {
    "DT-AUCTION": ("flip", dict(purchase=170_000)),
    "DT-PREFORE": ("flip", dict(purchase=170_000)),
    "DT-REO": ("flip", dict(purchase=170_000)),
    "DT-OFFMARKET": ("flip", dict(purchase=170_000)),
    "DT-ONMARKET": ("flip", dict(purchase=170_000)),
    "DT-WHOLESALE": ("flip", dict(purchase=150_000,
                                   closing_buy=2_000, closing_sell=5_000)),
    "DT-TAXDEED": ("redemption_yield", dict(bid=100_000)),
    "DT-SHORT": ("short_sale", dict(purchase=150_000)),
    "DT-SUBTO": ("subject_to", dict(existing_loan_balance=200_000,
                                    cash_to_seller=10_000,
                                    rent_monthly=2_000,
                                    existing_payment_monthly=1_400)),
    "DT-RENTAL": ("rental", dict(purchase=200_000, rent_monthly=2_000,
                                 taxes_annual=4_000, insurance_annual=1_500)),
    "DT-BRRRR": ("brrrr", dict(purchase=170_000)),
}

NON_FLIP = ("DT-TAXDEED", "DT-SHORT", "DT-SUBTO", "DT-RENTAL", "DT-BRRRR")


@pytest.mark.parametrize("deal_type", sorted(ROUTER_MATRIX))
def test_every_deal_type_routes_non_default(deal_type):
    """Each mapped deal type reaches its model and carries both stamps."""
    expected_model, kwargs = ROUTER_MATRIX[deal_type]
    result = route_underwrite(None, arv=300_000, rehab=30_000,
                              deal_type=deal_type, **kwargs)
    assert result.model == expected_model
    assert result.deal_type == deal_type  # routed stamp, not a silent one


def test_non_flip_deal_types_never_report_flip_model():
    """SHORT/SUBTO used to silently get flip economics; guard the stamps."""
    for dt in NON_FLIP:
        expected_model, kwargs = ROUTER_MATRIX[dt]
        result = route_underwrite(None, arv=300_000, rehab=30_000,
                                  deal_type=dt, **kwargs)
        assert result.model == expected_model != "flip"


def test_matrix_covers_every_deal_type_enum_member():
    """The 8 legal_rules.DealType codes all route — none left unmapped."""
    missing = [m.value for m in DealType if m.value not in ROUTER_MATRIX]
    assert not missing


def test_unknown_deal_type_raises_no_silent_flip():
    """Pre-F6 an unknown code silently got flip economics; now it raises."""
    with pytest.raises(ValueError):
        route_underwrite(None, arv=300_000, rehab=30_000,
                         deal_type="DT-NONSENSE", purchase=100_000)


def test_router_is_case_insensitive():
    result = route_underwrite(None, arv=300_000, rehab=30_000,
                              deal_type="dt-reo", purchase=170_000)
    assert result.model == "flip"
    assert result.deal_type == "DT-REO"


def test_wholesale_closing_costs_pass_through():
    """F6: the wholesale branch used to drop closing costs on the floor."""
    clean = route_underwrite(None, arv=300_000, rehab=30_000,
                             deal_type="DT-WHOLESALE", purchase=150_000)
    costed = route_underwrite(None, arv=300_000, rehab=30_000,
                              deal_type="DT-WHOLESALE", purchase=150_000,
                              closing_buy=3_000, closing_sell=9_000)
    assert costed.closing_costs_buy == 3_000
    assert costed.closing_costs_sell == 9_000
    # Double-close bills reduce profit dollar-for-dollar.
    assert clean.net_profit - costed.net_profit == pytest.approx(12_000)


# --- DT-SHORT economics sanity -------------------------------------------

def test_short_sale_economics_and_patience_tax():
    ss = short_sale_model(arv=300_000, purchase=150_000, rehab=30_000)
    assert ss.model == "short_sale"
    assert ss.deal_type == "DT-SHORT"
    holding_total = SHORT_SALE_APPROVAL_MONTHS_DEFAULT * 200.0  # policy $/mo
    assert ss.line_items["holding_total"] == holding_total
    # ARV − (150k purchase + 36k rehab w/20% contingency + holding)
    assert ss.net_profit == pytest.approx(300_000 - (150_000 + 36_000
                                                     + holding_total))
    fast = short_sale_model(arv=300_000, purchase=150_000, rehab=30_000,
                            approval_months=1.0)
    slow = short_sale_model(arv=300_000, purchase=150_000, rehab=30_000,
                            approval_months=12.0)
    assert slow.net_profit < fast.net_profit  # patience costs money
    assert (fast.line_items["annualized_roi"]
            > slow.line_items["annualized_roi"])
    assert slow.holding_costs == pytest.approx(12 * 200.0)


def test_short_sale_routes_require_purchase():
    """H1 extends to DT-SHORT: no pricing an unpriced short sale at its MAO."""
    with pytest.raises(ValueError):
        route_underwrite(None, arv=300_000, rehab=30_000, deal_type="DT-SHORT")


# --- DT-SUBTO economics sanity --------------------------------------------

def test_subject_to_matches_excel_tab_rows():
    st = subject_to_model(arv=320_000, existing_loan_balance=280_000,
                          cash_to_seller=10_000, rent_monthly=2_200,
                          existing_payment_monthly=1_500, existing_rate=0.03)
    assert st.model == "subject_to"
    assert st.deal_type == "DT-SUBTO"
    assert st.net_profit == 30_000            # tab B11 equity capture
    assert st.line_items["cash_to_control"] == 10_000   # tab B12
    assert st.line_items["monthly_cash_flow"] == 700    # tab B13
    assert st.line_items["annual_cash_flow"] == 8_400   # tab B14
    assert st.cash_on_cash == pytest.approx(8_400 / 10_000)  # tab B15
    assert st.roi == pytest.approx(st.cash_on_cash)


def test_subject_to_due_on_sale_risk_always_flagged():
    flagged = subject_to_model(arv=300_000, existing_loan_balance=250_000)
    assert flagged.due_on_sale_risk is True
    assert "loan due" in flagged.line_items["due_on_sale_note"].lower()
    # Other models leave the flag unset rather than implying safety.
    from src.underwriting import flip_pnl
    assert flip_pnl(arv=1, purchase=1, rehab=0).due_on_sale_risk is None


def test_due_on_sale_flag_resolves_legal_rules_then_policy(monkeypatch):
    """F6: flag authority is legal_rules first, then policy, then True."""
    from src import legal_rules, policy
    from src.underwriting import _due_on_sale_risk_default

    # Neither module declares a constant today -> conservative default.
    assert _due_on_sale_risk_default() is True

    # A future attorney-verified legal_rules constant wins.
    monkeypatch.setattr(legal_rules, "SUBJECT_TO_DUE_ON_SALE_RISK", False,
                        raising=False)
    assert _due_on_sale_risk_default() is False
    st = subject_to_model(arv=300_000, existing_loan_balance=250_000)
    assert st.due_on_sale_risk is False

    # Without the legal_rules constant, policy's value applies.
    monkeypatch.delattr(legal_rules, "SUBJECT_TO_DUE_ON_SALE_RISK")
    monkeypatch.setattr(policy, "DUE_ON_SALE_RISK", False, raising=False)
    assert _due_on_sale_risk_default() is False


def test_subto_cash_basis_includes_closing_buy():
    st = subject_to_model(arv=320_000, existing_loan_balance=280_000,
                          cash_to_seller=10_000, rent_monthly=2_200,
                          existing_payment_monthly=1_500, closing_buy=2_000)
    # 12k cash actually in -> CoC dilutes accordingly.
    assert st.cash_invested == 12_000
    assert st.cash_on_cash == pytest.approx(8_400 / 12_000)


def test_subto_routes_require_existing_loan_balance():
    with pytest.raises(ValueError):
        route_underwrite(None, arv=300_000, rehab=0, deal_type="DT-SUBTO",
                         cash_to_seller=5_000)
    routed = route_underwrite(None, arv=300_000, rehab=0, deal_type="DT-SUBTO",
                              existing_loan_balance=200_000,
                              cash_to_seller=10_000, rent_monthly=2_000,
                              existing_payment_monthly=1_400)
    assert routed.model == "subject_to"
    assert routed.due_on_sale_risk is True


# --- DT-RENTAL / DT-BRRRR / DT-TAXDEED routing ----------------------------

def test_rental_requires_purchase_and_computes_metrics():
    with pytest.raises(ValueError):
        route_underwrite(None, arv=300_000, rehab=0, deal_type="DT-RENTAL",
                         rent_monthly=2_000)
    r = route_underwrite(None, arv=300_000, rehab=0, deal_type="DT-RENTAL",
                         purchase=200_000, rent_monthly=2_000,
                         taxes_annual=4_000, insurance_annual=1_500)
    assert r.model == "rental"
    assert r.purchase_price == 200_000
    assert r.cap_rate is not None and r.cap_rate > 0


def test_brrrr_requires_purchase_and_recycles():
    with pytest.raises(ValueError):
        route_underwrite(None, arv=300_000, rehab=30_000, deal_type="DT-BRRRR")
    b = route_underwrite(None, arv=300_000, rehab=30_000, deal_type="DT-BRRRR",
                         purchase=170_000, refi_ltv=0.75)
    assert b.model == "brrrr"
    assert b.refi_recycle_pct is not None


def test_taxdeed_still_routes_to_redemption_yield():
    td = route_underwrite(None, arv=0, rehab=0, deal_type="DT-TAXDEED",
                          bid=100_000)
    assert td.model == "redemption_yield"
    assert td.redemption_yield == 25_000  # 25% yr-1 premium on the bid


def test_reo_offmarket_onmarket_map_explicitly_to_flip():
    for dt in ("DT-REO", "DT-OFFMARKET", "DT-ONMARKET"):
        r = route_underwrite(None, arv=300_000, rehab=30_000, deal_type=dt,
                             purchase=170_000)
        assert r.model == "flip"
        assert r.deal_type == dt
