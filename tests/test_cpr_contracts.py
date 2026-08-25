"""CPR provenance-contract regression tests (audit D3/D4 + S7 contract)."""
import pytest

from src.cpr import CPR, LienRecord, ProvenanceField, pv_value


def _cpr() -> CPR:
    return CPR(parcel_id="P1", county="collin", situs_address="1 Main St")


def test_confidence_validation_rejects_out_of_range():
    cpr = _cpr()
    with pytest.raises(ValueError):
        cpr.set_field("city", "Frisco", "test", 1.5)
    with pytest.raises(ValueError):
        cpr.set_field("city", "Frisco", "test", -0.1)


def test_lien_lookup_status_contract():
    cpr = _cpr()
    assert cpr.lien_lookup_status == "not_run"
    cpr.lien_lookup_status = "ok"
    assert cpr.lien_lookup_status == "ok"
    with pytest.raises(ValueError):
        cpr.lien_lookup_status = "banana"


def test_pv_value_unwraps_provenance_fields():
    wrapped = ProvenanceField(value="Unknown", source="t", confidence=0.9)
    assert pv_value(wrapped) == "Unknown"
    assert pv_value("plain") == "plain"
    assert pv_value(None) is None


def test_lien_receipt_records_type_and_survival():
    """Audit D3: ledger receipts previously recorded amounts only."""
    cpr = _cpr()
    lien = LienRecord(lien_type="tax", amount=9_412.0, recording_date="11/11/2014",
                      survives_foreclosure=True, survives_tax_sale=True)
    n_before = len(cpr.ledger)
    cpr.add_lien(lien, "fixture")
    receipts = [e for e in cpr.ledger[n_before:] if e.field_name.startswith("lien")]
    assert receipts, "expected a ledger receipt for the lien"
    payload = receipts[-1].new_value
    assert isinstance(payload, dict)
    assert payload.get("lien_type") == "tax"
    assert payload.get("survives_foreclosure") is True
