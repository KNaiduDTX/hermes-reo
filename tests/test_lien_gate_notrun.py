"""Audit CPR-3: GATE-LEGAL-LIEN must not pass on an unverified (not_run) stack.

Regression: a Dallas live parcel carrying only an OCR-derived mortgage lien
(lien_lookup_status='not_run') fell through both the error guard and the
empty-stack guard into a clean 'pass' — zero verification, clean-title verdict.
"""
from src.cpr import CPR, LienRecord
from src.compliance import gate_legal_lien


def _cpr():
    return CPR(parcel_id="P", county="dallas", situs_address="1 Main St")


def _mortgage_only(cpr):
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=250_000.0,
                            recording_date="1/2/2015", lender="UNKNOWN",
                            priority=2, survives_foreclosure=False,
                            survives_tax_sale=False), "test_seed")
    return cpr


def test_not_run_with_liens_routes_to_hitl_not_pass():
    cpr = _mortgage_only(_cpr())  # default status: not_run
    gate = gate_legal_lien(cpr)
    assert gate.verdict == "hitl", gate
    assert "title-search-pending" in gate.reason
    assert gate.details["lien_lookup_status"] == "not_run"


def test_not_run_without_liens_still_fails():
    gate = gate_legal_lien(_cpr())
    assert gate.verdict == "fail"


def test_error_still_routes_to_hitl():
    cpr = _cpr()
    cpr.lien_lookup_status = "error"
    assert gate_legal_lien(cpr).verdict == "hitl"


def test_ok_stack_still_passes_when_no_surviving_liens():
    cpr = _mortgage_only(_cpr())
    cpr.lien_lookup_status = "ok"
    gate = gate_legal_lien(cpr)
    assert gate.verdict == "pass", gate
