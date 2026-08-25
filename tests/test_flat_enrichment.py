"""Flat-schema enrichment + merge-on-write tests (Wave 3, audit D1)."""
from src.enrichment import enrich_from_flat_record


FLAT_DALLAS = {
    "address": "15806 BREEDLOVE PLACE, ADDISON, TX 75001",
    "city": "Addison",
    "county": "dallas",
    "parcel_id": "ABC123",
    "sqft": 2388,
    "beds": 3,
    "baths": 2.1,
    "year_built": 1985,
    "cad_value": 518510,
}


def test_flat_record_maps_identity_and_fields():
    """AUDIT D1: bulk-dcad used to silently no-op on the flat dataset."""
    cpr = enrich_from_flat_record(FLAT_DALLAS)
    assert cpr.county == "dallas"
    assert "BREEDLOVE" in cpr.situs_address
    assert cpr.parcel_id == "ABC123"
    assert cpr.improvement_sqft.value == 2388
    assert cpr.beds.value == 3
    assert cpr.cad_appraised_value.value == 518510


def test_flat_record_sparse_is_none_safe():
    rec = {"address": "9 X ST, DALLAS, TX", "county": "DALLAS"}
    cpr = enrich_from_flat_record(rec)
    assert cpr.county == "dallas"
    assert not (cpr.improvement_sqft and cpr.improvement_sqft.value)


def test_flat_record_positive_debt_creates_mortgage_lien():
    rec = dict(FLAT_DALLAS, mortgage_debt=250_000.0)
    cpr = enrich_from_flat_record(rec)
    assert any(ln.lien_type == "mortgage" and ln.amount == 250_000.0
               for ln in cpr.liens)


def test_flat_record_nonpositive_debt_creates_no_lien():
    for bad in (0, None, "Unknown"):
        rec = dict(FLAT_DALLAS, mortgage_debt=bad)
        cpr = enrich_from_flat_record(rec)
        assert not any(ln.lien_type == "mortgage" for ln in cpr.liens), bad
