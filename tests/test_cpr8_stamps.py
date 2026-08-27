"""Audit CPR-8: carried-forward enrichment must be distinguishable from a
fresh scrape. merge_prior_enrichment now stamps carried_fields (which keys
came from the prior snapshot) and carried_from (prior-snapshot date marker).
"""
import json

from find_deals import ENRICHMENT_CARRY_FORWARD_FIELDS, merge_prior_enrichment


def test_carried_fields_are_provenance_stamped(tmp_path):
    prior = {"property_id": "P1", "county": "collin",
             "address": "1 Main St", "sqft": 2000, "beds": 3,
             "scraped_at": "2026-08-01T00:00:00Z"}
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(json.dumps([prior]), encoding="utf-8")

    fresh = [{"property_id": "P1", "county": "collin",
              "address": "1 Main St"}]  # sqft/beds empty -> carried
    out = merge_prior_enrichment(fresh, str(prior_path))
    row = out[0]
    assert row["sqft"] == 2000 and row["beds"] == 3
    assert "sqft" in row["carried_fields"] and "beds" in row["carried_fields"]
    assert row["carried_from"] == "2026-08-01T00:00:00Z"


def test_fresh_values_never_stamped_or_overwritten(tmp_path):
    prior = {"property_id": "P1", "address": "1 Main St", "sqft": 2000}
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(json.dumps([prior]), encoding="utf-8")
    fresh = [{"property_id": "P1", "address": "1 Main St", "sqft": 2500}]
    out = merge_prior_enrichment(fresh, str(prior_path))
    assert out[0]["sqft"] == 2500, "fresher scrape must win"
    assert "carried_fields" not in out[0] or "sqft" not in out[0]["carried_fields"]


def test_carry_forward_fields_catalog_unchanged():
    # The catalog is the contract for what survives regeneration; this pins it
    # so silent additions/removals get reviewed.
    assert ENRICHMENT_CARRY_FORWARD_FIELDS == (
        "sqft", "beds", "baths", "year_built", "stories", "pool",
        "cad_value", "cad_value_2025", "census_tract", "county_fips",
        "latitude", "longitude",
        "val_gate",
    )
