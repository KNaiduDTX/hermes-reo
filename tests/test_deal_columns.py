"""FN4-lite / F7 / HERMES_SKIP_LIVE tests for find_deals.py regeneration.

Covers:
- FN4-lite: additive address-quality columns (zip5, city_norm, addr_valid,
  invalid_reason) derived from src.addr.normalize_address, including the
  BALCHSPRINGS city fixup, additive-only schema growth, and survival through
  strip_invalid_query_markers (the persisted current_deals.json payload).
- F7: GeoJSON built from enrich_from_flat_record CPRs — feature count parity
  against the geocoded rows of a synthetic mixed set, richer per-feature
  properties (sale_date, equity_pct, lender, lien_gate, blast_tier) where
  available, and legacy feature properties untouched.
- HERMES_SKIP_LIVE=1 offline knob (default behavior unchanged).
"""
import json
import os

import pytest

from src.addr import normalize_address
from src.cpr import CPR

import find_deals
from find_deals import (
    ADDRESS_COLUMN_KEYS,
    GEO_DEAL_PROPERTIES,
    INVALID_QUERY_KEY,
    SKIP_LIVE_ENV,
    address_columns,
    build_geojson_from_deals,
    copy_cpr_coordinates,
    fetch_live_cprs,
    flat_record_has_coordinates,
    mark_invalid_query,
    skip_live_scrape,
    stamp_address_columns,
    strip_invalid_query_markers,
)

BALCH = "2739 SHEILAH DRIVE, BALCHSPRINGS, TX 75180"
JUNK = "60 foot right-of-way, at the northeast comer of said lot"
OOS = "3637 Sentara Way, Virginia Beach, VA 23452"


# --- FN4-lite column values -----------------------------------------------------
def test_address_columns_balch_springs_fixup():
    """Legacy misspelling repaired by src.addr CITY_FIXUPS lands in city_norm."""
    cols = address_columns(BALCH)
    assert cols == {
        "zip5": "75180",
        "city_norm": "Balch Springs",
        "addr_valid": True,
        "invalid_reason": "",
    }
    # Columns faithfully mirror normalize_address.
    norm = normalize_address(BALCH)
    assert cols["zip5"] == norm.zip5 and cols["city_norm"] == norm.city


@pytest.mark.parametrize(
    ("raw", "reason_fragment"),
    [(JUNK, "junk pattern"), (OOS, "out-of-state")],
)
def test_address_columns_for_invalid_addresses(raw, reason_fragment):
    cols = address_columns(raw)
    assert cols["addr_valid"] is False
    assert reason_fragment in cols["invalid_reason"]
    for key in ADDRESS_COLUMN_KEYS:
        assert key in cols


def test_address_columns_missing_address_never_crashes():
    for raw in (None, "", "   "):
        cols = address_columns(raw)
        assert cols["addr_valid"] is False
        assert cols["invalid_reason"]


def test_stamp_additive_keys_only_legacy_values_untouched():
    """Stamping grows the row by exactly the four columns — nothing else moves."""
    deal = {
        "address": BALCH, "county": "dallas", "city": "Unknown",
        "sqft": None, "lender": None, "all_balances": [1.5, 2.5],
        "equity_pct": 12.5,
    }
    before = dict(deal)
    stamped = stamp_address_columns(deal)
    assert stamped is deal                       # in-place, same object
    assert set(stamped) - set(before) == set(ADDRESS_COLUMN_KEYS)
    for key, value in before.items():
        assert stamped[key] == value, key


def test_persisted_payload_keeps_columns_and_drops_runtime_marker():
    """Full write-path algebra: legacy keys + the four columns, no marker."""
    cpr = CPR(parcel_id="T1", county="dallas", situs_address=BALCH)
    cpr.set_field("stories", None, "test")
    cpr.set_field("pool", None, "test")
    deal = find_deals.analyze_deal(cpr)
    legacy_keys = set(deal)

    junk_row = find_deals.analyze_deal(_cpr(JUNK))
    assert INVALID_QUERY_KEY in junk_row
    stamped_junk = stamp_address_columns(junk_row)
    payload = strip_invalid_query_markers([stamp_address_columns(deal),
                                           stamped_junk])
    clean_valid, clean_junk = payload
    assert INVALID_QUERY_KEY not in clean_valid
    assert INVALID_QUERY_KEY not in clean_junk
    # Exactly the four additive columns on both rows; legacy keys verbatim.
    assert set(clean_valid) - legacy_keys == set(ADDRESS_COLUMN_KEYS)
    assert set(clean_junk) - legacy_keys - {INVALID_QUERY_KEY} \
        == set(ADDRESS_COLUMN_KEYS)
    assert clean_valid["addr_valid"] is True
    assert clean_valid["zip5"] == "75180"
    assert clean_valid["city_norm"] == "Balch Springs"
    assert clean_junk["addr_valid"] is False
    assert "junk pattern" in clean_junk["invalid_reason"]
    # And the payload round-trips as JSON (persistable).
    assert json.loads(json.dumps(payload))[0]["addr_valid"] is True


def _cpr(address):
    cpr = CPR(parcel_id="TEST-COLS", county="dallas", situs_address=address)
    cpr.set_field("stories", None, "test")
    cpr.set_field("pool", None, "test")
    return cpr


# --- F7: GeoJSON parity vs geocoded rows ----------------------------------------
def _mixed_deals():
    return [
        {"address": "101 MAIN ST, DALLAS, TX 75201", "county": "dallas",
         "latitude": 32.78, "longitude": -96.80,
         "sale_date": "2026-09-01", "equity_pct": 12.5,
         "lender": "EXAMPLE BANK", "lien_gate": "pass", "blast_tier": "T2",
         "cad_value": 300000, "sqft": 1500},
        {"address": "202 OAK AVE, PLANO, TX 75023", "county": "collin"},
        {"address": "303 ELM ST, GARLAND, TX 75040", "county": "dallas",
         "latitude": "", "longitude": ""},
        {"address": "404 PINE RD, MESQUITE, TX 75149", "county": "dallas",
         "latitude": "bad", "longitude": -96.6},
        {"address": "505 BIRCH LN, RICHARDSON, TX 75080", "county": "dallas",
         "latitude": 0, "longitude": -96.7},
        {"address": "606 CEDAR CT, CARROLLTON, TX 75006", "county": "dallas",
         "latitude": 32.95, "longitude": -96.89},
    ]


def test_coordinate_gate_mirrors_enrichment_predicate():
    geocoded = [d for d in _mixed_deals() if flat_record_has_coordinates(d)]
    assert len(geocoded) == 2                 # rows 0 and 5 only


def test_geojson_feature_count_matches_geocoded_rows():
    deals = _mixed_deals()
    fc = build_geojson_from_deals(deals)
    expected = sum(1 for d in deals if flat_record_has_coordinates(d))
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == expected


def test_geojson_rich_properties_where_available():
    fc = build_geojson_from_deals(_mixed_deals())
    rich, sparse = (f["properties"] for f in fc["features"])
    # Geometry stays [lng, lat] from the flat row.
    assert fc["features"][0]["geometry"]["coordinates"] == [-96.80, 32.78]
    assert fc["features"][1]["geometry"]["coordinates"] == [-96.89, 32.95]
    # Richer properties attached to the matching feature...
    for key in GEO_DEAL_PROPERTIES:
        assert rich[key] == {"sale_date": "2026-09-01", "equity_pct": 12.5,
                             "lender": "EXAMPLE BANK", "lien_gate": "pass",
                             "blast_tier": "T2"}[key]
    # ...and omitted (not fabricated) when the row lacks them.
    for key in ("sale_date", "equity_pct", "lender", "lien_gate"):
        assert key not in sparse
    # The analyzed tier overrides the CPR-default blast_tier on rich rows.
    assert rich["blast_tier"] == "T2"


def test_geojson_geometry_is_plain_json_numbers():
    """Regression: cpr_to_geojson serializes the ProvenanceField-wrapped
    gis_lat/gis_lng via str(), so legacy exports carried unrenderable
    'ProvenanceField(value=…)' coordinate strings. build_geojson_from_deals
    must re-stamp plain [lng, lat] numbers that survive a JSON round-trip.
    """
    fc = build_geojson_from_deals(_mixed_deals())
    for feature in fc["features"]:
        lng, lat = feature["geometry"]["coordinates"]
        assert isinstance(lng, float) and isinstance(lat, float)
        assert not isinstance(lng, str) and not isinstance(lat, str)
    payload = json.loads(json.dumps(fc))
    assert payload["features"][0]["geometry"]["coordinates"] == [-96.8, 32.78]


def test_geojson_legacy_properties_untouched():
    fc = build_geojson_from_deals(_mixed_deals())
    props = fc["features"][0]["properties"]
    assert props["title"] == "101 MAIN ST, DALLAS, TX 75201"
    assert props["address"] == "101 MAIN ST, DALLAS, TX 75201"
    assert props["county"] == "dallas"
    assert props["category"] in ("Commercial", "Discount Flip", "Rental",
                                 "Standard")
    assert props["appraised_value"] == 300000.0
    assert props["deal_type"] == "DT-AUCTION"
    for legacy_key in ("marker-color", "marker-symbol", "marker-size",
                       "price_per_sqft", "market_pps_discount_pct",
                       "year_built", "owner"):
        assert legacy_key in props


# --- F7 companion: CPR → flat-row coordinate passthrough -------------------------
def test_copy_cpr_coordinates_plain_and_provenance_values():
    plain = CPR(parcel_id="L1", county="collin",
                situs_address="10022 Plainsman Ln Frisco, TX 75035")
    plain.gis_lat = 33.1792
    plain.gis_lng = -96.7939
    deal = {}
    assert copy_cpr_coordinates(plain, deal) is True
    assert deal["latitude"] == 33.1792
    assert deal["longitude"] == -96.7939

    wrapped = CPR(parcel_id="L2", county="collin", situs_address="x")
    wrapped.set_field("gis_lat", 33.5, "test", 0.9)
    wrapped.set_field("gis_lng", -96.5, "test", 0.9)
    deal2 = {}
    assert copy_cpr_coordinates(wrapped, deal2) is True
    assert deal2["latitude"] == 33.5


def test_copy_cpr_coordinates_rejects_unusable_pairs():
    for lat, lng in ((None, -96.8), (32.9, None), (0, -96.7), (32.9, 0),
                     ("bad", -96.7)):
        cpr = CPR(parcel_id="L3", county="collin", situs_address="x")
        cpr.gis_lat, cpr.gis_lng = lat, lng
        deal = {}
        assert copy_cpr_coordinates(cpr, deal) is False
        assert "latitude" not in deal and "longitude" not in deal


# --- HERMES_SKIP_LIVE offline knob ------------------------------------------------
def test_skip_live_env_default_is_live(monkeypatch):
    monkeypatch.delenv(SKIP_LIVE_ENV, raising=False)
    assert skip_live_scrape() is False          # default unchanged


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " 1 "])
def test_skip_live_env_truthy_values(monkeypatch, value):
    monkeypatch.setenv(SKIP_LIVE_ENV, value)
    assert skip_live_scrape() is True


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_skip_live_env_falsy_values(monkeypatch, value):
    monkeypatch.setenv(SKIP_LIVE_ENV, value)
    assert skip_live_scrape() is False


def test_fetch_live_cprs_skipped_under_env(monkeypatch):
    calls = []

    def _fake_scrape(limit=5):
        calls.append(limit)
        return []

    monkeypatch.setattr(find_deals, "scrape_live_collin", _fake_scrape)
    monkeypatch.setenv(SKIP_LIVE_ENV, "1")
    assert fetch_live_cprs() == []
    assert calls == []                          # scrape never invoked
    monkeypatch.delenv(SKIP_LIVE_ENV)
    assert fetch_live_cprs() == []
    assert calls == [5]                         # default still scrapes live
