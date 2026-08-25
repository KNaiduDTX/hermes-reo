"""Merge-on-write tests: enrichment must survive dataset regeneration (D1)."""
import json

from find_deals import merge_prior_enrichment


def _prior(tmp_path, payload):
    p = tmp_path / "prior.json"
    p.write_text(json.dumps(payload))
    return str(p)


def test_sparse_fresh_record_inherits_prior_enrichment(tmp_path):
    prior = _prior(tmp_path, [{
        "address": "1 A ST, DALLAS, TX 75201", "county": "dallas",
        "sqft": 2000, "cad_value": 300000, "beds": 3,
    }])
    fresh = [{"address": "1 A St, Dallas, TX 75201", "county": "dallas",
              "sqft": None, "cad_value": "", "beds": 0}]
    merged = merge_prior_enrichment(fresh, prior)
    assert merged[0]["sqft"] == 2000
    assert merged[0]["cad_value"] == 300000
    assert merged[0]["beds"] == 3


def test_fresh_values_win_over_stale_prior(tmp_path):
    prior = _prior(tmp_path, [{
        "address": "1 A ST, DALLAS, TX 75201", "county": "dallas",
        "sqft": 2000,
    }])
    fresh = [{"address": "1 A ST, DALLAS, TX 75201", "county": "dallas",
              "sqft": 2500}]
    merged = merge_prior_enrichment(fresh, prior)
    assert merged[0]["sqft"] == 2500


def test_missing_prior_file_is_tolerated(tmp_path):
    fresh = [{"address": "X", "county": "dallas", "sqft": None}]
    merged = merge_prior_enrichment(fresh, str(tmp_path / "nope.json"))
    assert merged == fresh


def test_corrupt_prior_file_is_tolerated(tmp_path):
    p = tmp_path / "corrupt.json"
    p.write_text("{not json")
    fresh = [{"address": "X", "county": "dallas", "sqft": None}]
    assert merge_prior_enrichment(fresh, str(p)) == fresh
