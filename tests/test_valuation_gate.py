"""GATE-VAL regression tests (audit H2/F5/F6): honest confidence scoring."""
from datetime import date, timedelta

from src.cpr import BlastTier, CPR, CompRecord
from src.valuation import MAX_COMP_AGE_DAYS, gate_val


def _comp(days_ago=30, price=750_000, adj=None, dist=0.2):
    return CompRecord(
        address="100 X St", sale_price=price,
        sale_date=(date.today() - timedelta(days=days_ago)).isoformat(),
        distance_miles=dist, source="mls_agent",
        adjusted_price=price if adj is None else adj,
    )


def _cpr():
    return CPR(parcel_id="P", county="collin", situs_address="1 Main St")


def test_recent_comps_pass_t3():
    gate, val = gate_val(_cpr(), "mls_agent", [_comp() for _ in range(5)], BlastTier.T3)
    assert gate.verdict == "pass"
    assert val.arv is not None


def test_stale_and_far_comps_are_filtered():
    """A comp older than MAX_COMP_AGE_DAYS must not inflate the ARV set."""
    comps = [_comp(), _comp(), _comp(),
             _comp(days_ago=MAX_COMP_AGE_DAYS + 200)]
    gate, val = gate_val(_cpr(), "mls_agent", comps, BlastTier.T3)
    assert "filtered" in gate.reason.lower()
    # Only 3 usable comps remain; ARV comes from those alone.
    assert val.comp_set and len(val.comp_set) == 3


def test_all_stale_fails_min_comps():
    comps = [_comp(days_ago=MAX_COMP_AGE_DAYS + 400) for _ in range(5)]
    gate, val = gate_val(_cpr(), "mls_agent", comps, BlastTier.T3)
    assert gate.verdict == "fail"


def test_no_priced_comps_never_passes():
    """AUDIT F6: gate used to be able to say 'pass' with no ARV attached."""
    comps = [CompRecord(address="x", sale_price=None, sale_date="2026-01-01",
                        distance_miles=0.2, source="mls_agent")
             for _ in range(5)]
    gate, val = gate_val(_cpr(), "mls_agent", comps, BlastTier.T3)
    assert gate.verdict != "pass"
    if gate.verdict == "pass":
        raise AssertionError("pass without priced comps must be impossible")


def test_rejected_source_hard_fails():
    gate, _ = gate_val(_cpr(), "assessed_only", [_comp() for _ in range(5)], BlastTier.T3)
    assert gate.verdict == "fail"
