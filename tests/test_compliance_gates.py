"""Compliance-gate regression tests (audit C1/H5/X2 + S7 lookup contract)."""
from src.cpr import CPR, LienRecord
from src.compliance import (
    gate_legal_eligibility,
    gate_legal_lien,
    run_all_gates,
)


def _cpr(county="collin"):
    return CPR(parcel_id="P", county=county, situs_address="1 Main St")


def test_homestead_sachse_regression():
    """AUDIT C1: 'HS' substring flagged City of Sachse properties homestead."""
    from src.enrichment import _homestead_from_taxing  # or inline fallback
    assert _homestead_from_taxing("Collin County (GCN) / City of Sachse (CFR)") is False
    assert _homestead_from_taxing("Collin County (GCN) / Frisco City (HS)") is True
    assert _homestead_from_taxing("Property is HOMESTEAD exempt") is True


def test_errored_lien_lookup_is_never_clean_title():
    """AUDIT S7: swallowed lookups used to pass the legal gate vacuously."""
    cpr = _cpr()
    cpr.lien_lookup_status = "error"
    gate = gate_legal_lien(cpr)
    assert gate.verdict == "hitl"
    assert "fail" in gate.reason.lower() or "lookup" in gate.reason.lower()


def test_dallas_requires_realauction_registration():
    """AUDIT H5: certified funds alone satisfied the wrong requirement."""
    cpr = _cpr("dallas")
    g = gate_legal_eligibility(cpr, has_written_statement=True,
                               has_certified_funds=True,
                               has_realauction_registration=False)
    assert g.verdict in ("fail", "hitl")


def test_grayson_never_auto_passes():
    """AUDIT H5: a TBD legal requirement must not silently pass."""
    cpr = _cpr("grayson")
    g = gate_legal_eligibility(cpr, has_written_statement=True)
    assert g.verdict == "hitl"


def test_run_all_gates_kwargs_mapping_no_crash():
    """AUDIT X2: shared kwargs fan-out used to raise TypeError.

    The wholesale gate only runs for DT-WHOLESALE deals, so set deal_type
    and confirm its kwargs actually reach it (the old **kwargs fan-out
    crashed with TypeError before this could even be exercised).
    """
    cpr = _cpr()
    cpr.deal_type = "DT-WHOLESALE"
    gates = run_all_gates(cpr, gates_kwargs={
        "GATE-LEGAL-REDEMPTION": {"deed_recording_date": "2026-01-01"},
        "GATE-LEGAL-WHOLESALE": {"has_disclosure": False},
    })
    assert isinstance(gates, list) and len(gates) >= 2
    wholesale = [g for g in gates if g.gate_name == "GATE-LEGAL-WHOLESALE"]
    assert wholesale, "wholesale gate should run for DT-WHOLESALE"
    # has_disclosure=False must produce a fail (kwargs genuinely consumed).
    assert wholesale[0].verdict == "fail"
    assert "disclosure" in (wholesale[0].reason or "").lower()


def test_run_all_gates_unknown_gate_name_raises():
    with __import__("pytest").raises(ValueError):
        run_all_gates(_cpr(), gates_kwargs={"NOT-A-GATE": {}})
