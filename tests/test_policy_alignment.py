"""Policy single-source-of-truth alignment tests (Wave 3, audit P7/X9)."""
from src import policy
from src.legal_rules import RedemptionClass, get_redemption_premium  # noqa: F401


def test_policy_values_match_audited_defaults():
    assert policy.MAO_FACTOR_DEFAULT == 0.70
    assert policy.REHAB_CONTINGENCY_PCT == 0.20
    assert policy.CLOSING_SELL_PCT == 0.075
    assert policy.CLOSING_BUY_PCT == 0.025


def test_valuation_floors_sourced_from_policy():
    from src.cpr import BlastTier
    from src.valuation import CONFIDENCE_FLOORS, MIN_COMPS, MAX_COMP_AGE_DAYS
    assert CONFIDENCE_FLOORS[BlastTier.T3] == policy.CONFIDENCE_FLOOR_T3
    assert CONFIDENCE_FLOORS[BlastTier.T2] == policy.CONFIDENCE_FLOOR_T2
    assert MIN_COMPS == policy.MIN_COMPS
    assert MAX_COMP_AGE_DAYS == policy.MAX_COMP_AGE_DAYS


def test_no_closing_cost_divergence_between_python_and_workbook():
    """AUDIT P7: pipeline used 8% while the workbook used 7.5%."""
    import inspect
    import src.pricing as pricing
    import src.underwriting as underwriting

    for module in (pricing, underwriting):
        src_text = inspect.getsource(module)
        assert "arv * 0.08" not in src_text, (
            f"{module.__name__} still hardcodes an 8% sell closing cost — "
            "use policy.CLOSING_SELL_PCT"
        )
        assert "closing_sell_pct: float = 0.08" not in src_text


def test_workbook_seeds_assumptions_from_policy():
    """The parity guard in build_workbook must exist and hold."""
    import build_workbook as bw
    # Raises on any drift between policy and the literals baked into formulas.
    bw._verify_policy_parity()
