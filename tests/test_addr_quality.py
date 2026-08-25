"""Address-quality tests: src.addr contract vs REAL dataset garbage + the
find_deals.py ranking gate that keeps malformed addresses out of TOP tables
and TOP-EQUITY-PROSPECTS while their saved rows stay untouched.

The garbage samples below are verbatim strings from the legacy 335-record
lucid-noether scrape (legal prose fragments, out-of-state listings,
corrupted tokens) plus the odd-but-valid-shaped records the filter must NOT
reject.
"""
import json

import pytest

from src.addr import (
    is_plausible_street_address,
    normalize_address,
    to_census_one_line,
)
from src.cpr import CPR
import find_deals
from find_deals import (
    INVALID_QUERY_KEY,
    count_malformed_addresses,
    has_plausible_address,
    mark_invalid_query,
    merge_prior_enrichment,
    rankable_deals,
    strip_invalid_query_markers,
    top_equity_prospects,
)

# --- REAL dataset garbage (verbatim) ------------------------------------------
GARBAGE_PROSE = "60 foot right-af-way, at the northeast comer of sa"
GARBAGE_LEGAL = "002 of the Texas Property Code as the place where "
GARBAGE_OOS = "3637 Sentara Way, Virginia Beach"
GARBAGE_CORRUPT = "1255 West !$th Street, Suite 1060 _ Plano, Dallas,"
GARBAGE_NO_NUMBER = "Suite 1100 Dallas Texas 75201"

# Odd-looking but VALID-shaped records that must survive the filter.
VALID_ODD = "7 PROPERTIES DALLAS, TX 75216"
VALID_DUP_STATE_ZIP = "1011 E ALAN AVE CARROLLTON, TEXAS 75006, TX 75202"

# Clean records exercising city fixups, ZIP+4 collapse and unit parsing.
CLEAN_BALCH = "2739 SHEILAH DRIVE, BALCHSPRINGS, TX 75180"
CLEAN_ZIP4 = "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334"
CLEAN_UNIT = "708 West 10th Street, UNIT 101 -106, Dallas, TX 75208"

# The exact flat row schema find_deals.analyze_deal has always written to
# data/current_deals.json (guards the "JSON schema must not change" rule).
LEGACY_DEAL_KEYS = frozenset({
    "address", "city", "county", "parcel_id", "owner", "property_type",
    "sqft", "beds", "baths", "year_built", "stories", "pool", "commercial",
    "cad_value", "cad_value_2025", "price_per_sqft", "val_change_pct",
    "isd", "deed_number", "mortgage_debt", "lender", "trustee",
    "sale_date", "sale_time", "original_principal", "unpaid_principal",
    "all_balances", "census_tract", "county_fips", "equity_estimate",
    "equity_pct", "ltv_pct", "preliminary_mao", "rough_rehab",
    "blast_tier", "lien_gate", "val_gate", "hitl_required",
})


# --- normalize_address vs real garbage ----------------------------------------
@pytest.mark.parametrize(
    ("raw", "reason_fragment"),
    [
        (GARBAGE_PROSE, "junk pattern"),        # "...northeast comer of..."
        (GARBAGE_LEGAL, "junk pattern"),        # "...Texas Property Code..."
        (GARBAGE_CORRUPT, "junk pattern"),      # corrupted token: !$th
        (GARBAGE_OOS, "no zip code found"),     # Virginia Beach, no TX anchor
        (GARBAGE_NO_NUMBER, "no leading street number"),
    ],
)
def test_real_dataset_garbage_is_rejected(raw, reason_fragment):
    n = normalize_address(raw)
    assert not n.valid, f"must be rejected: {raw!r}"
    assert reason_fragment in n.invalid_reason, (raw, n.invalid_reason)
    assert not n.street_number and not n.street_name


def test_valid_shaped_odd_record_is_kept():
    """'7 PROPERTIES DALLAS, TX 75216' is number+word+TX+zip: plausible."""
    n = normalize_address(VALID_ODD)
    assert n.valid is True
    assert n.street_number == "7"
    assert n.zip5 == "75216"
    assert n.state == "TX"


def test_duplicate_state_zip_artifact_resolves_to_leftmost_zip():
    n = normalize_address(VALID_DUP_STATE_ZIP)
    assert n.valid is True
    assert n.street_number == "1011"
    assert n.street_name == "E ALAN"
    assert n.street_type == "AVE"
    assert n.city == "Carrollton"
    assert n.zip5 == "75006"          # first <ST> <zip> anchor wins, not 75202
    assert n.state == "TX"


@pytest.mark.parametrize(
    ("raw", "checks"),
    [
        # Legacy city misspelling repaired.
        (CLEAN_BALCH, {"street_number": "2739", "street_type": "DR",
                       "city": "Balch Springs", "zip5": "75180"}),
        # ZIP+4 collapses to zip5.
        (CLEAN_ZIP4, {"street_number": "15806", "street_type": "PL",
                      "city": "Addison", "zip5": "75001"}),
        # Unit extraction survives.
        (CLEAN_UNIT, {"street_number": "708", "street_type": "ST",
                      "city": "Dallas", "zip5": "75208"}),
    ],
)
def test_clean_addresses_normalize(raw, checks):
    n = normalize_address(raw)
    assert n.valid is True, (raw, n.invalid_reason)
    for field_name, expected in checks.items():
        assert getattr(n, field_name) == expected, (field_name, raw)


def test_helpers_agree_with_normalization():
    for raw in (GARBAGE_PROSE, GARBAGE_OOS, GARBAGE_NO_NUMBER):
        assert not is_plausible_street_address(raw)
        assert to_census_one_line(raw) == ""       # never geocode garbage
    for raw in (VALID_ODD, VALID_DUP_STATE_ZIP, CLEAN_BALCH, CLEAN_ZIP4):
        assert is_plausible_street_address(raw)
        assert to_census_one_line(raw) == normalize_address(raw).one_line != ""


# --- find_deals address-quality gate -------------------------------------------
def _cpr(address: str) -> CPR:
    """Minimal enriched-enough CPR (analyze_deal reads cpr.stories/cpr.pool)."""
    cpr = CPR(parcel_id="TEST-1", county="dallas", situs_address=address)
    cpr.set_field("stories", None, "test")
    cpr.set_field("pool", None, "test")
    return cpr


def _deal(address, equity=None):
    return {"address": address, "county": "dallas", "equity_estimate": equity,
            "mortgage_debt": 100000 if equity else None}


def test_analyze_deal_marks_implausible_only():
    bad = find_deals.analyze_deal(_cpr(GARBAGE_PROSE))
    good = find_deals.analyze_deal(_cpr(CLEAN_BALCH))
    # Invalid: marker present with a reason; record content itself untouched.
    assert bad[INVALID_QUERY_KEY]
    assert bad["address"] == GARBAGE_PROSE
    assert set(bad) - LEGACY_DEAL_KEYS == {INVALID_QUERY_KEY}
    # Valid: exact legacy schema, no runtime key at all.
    assert INVALID_QUERY_KEY not in good
    assert set(good) == LEGACY_DEAL_KEYS
    assert good["address"] == CLEAN_BALCH


def test_equity_list_excludes_malformed_records():
    deals = [
        _deal(GARBAGE_OOS, equity=900_000),        # would rank #1 by equity
        _deal(CLEAN_BALCH, equity=500_000),
        _deal(GARBAGE_NO_NUMBER, equity=700_000),
        _deal(CLEAN_ZIP4, equity=100_000),
        _deal(VALID_DUP_STATE_ZIP, equity=50_000),
        _deal(CLEAN_UNIT),                         # no equity -> never listed
    ]
    for d in deals:
        mark_invalid_query(d)

    ranked = top_equity_prospects(deals)
    assert [d["address"] for d in ranked] == [CLEAN_BALCH, CLEAN_ZIP4,
                                              VALID_DUP_STATE_ZIP]
    assert all(has_plausible_address(d) for d in ranked)
    # Gate bookkeeping agrees on the mixed list.
    assert count_malformed_addresses(deals) == 2
    assert len(rankable_deals(deals)) == len(deals) - 2


def test_top_tables_exclude_but_dataset_rows_stay_untouched():
    debt_deals = [_deal(GARBAGE_LEGAL), _deal(CLEAN_BALCH)]
    for d in debt_deals:
        mark_invalid_query(d)
        d["mortgage_debt"] = 250_000
    table = [d for d in debt_deals if d["mortgage_debt"]
             and has_plausible_address(d)]
    assert [d["address"] for d in table] == [CLEAN_BALCH]

    # Saved payload: marker stripped, everything else preserved verbatim
    # (including None fields and nested lists) — legacy schema intact.
    marked = dict(table[0], all_balances=[1.0, 2.5], lender=None)
    marked[INVALID_QUERY_KEY] = "injected"
    payload = strip_invalid_query_markers([marked])
    assert len(payload) == 1
    assert INVALID_QUERY_KEY not in payload[0]
    assert payload[0]["all_balances"] == [1.0, 2.5]
    assert payload[0]["lender"] is None
    assert {k for k in marked if k != INVALID_QUERY_KEY} == set(payload[0])


def test_merge_prior_enrichment_with_mixed_valid_invalid(tmp_path):
    prior_path = str(tmp_path / "prior.json")
    prior = [
        {"address": CLEAN_BALCH.upper(), "county": "dallas",
         "sqft": 1826, "census_tract": "013628"},
        {"address": GARBAGE_PROSE.upper(), "county": "dallas", "sqft": 1111},
    ]
    with open(prior_path, "w") as fh:
        json.dump(prior, fh)

    fresh = [
        mark_invalid_query({"address": CLEAN_BALCH, "county": "dallas",
                            "sqft": None, "census_tract": None}),
        mark_invalid_query({"address": GARBAGE_PROSE, "county": "dallas",
                            "sqft": None, "census_tract": None}),
    ]
    merged = merge_prior_enrichment(fresh, prior_path)

    # Carry-forward works regardless of validity...
    assert merged[0]["sqft"] == 1826
    assert merged[1]["sqft"] == 1111
    # ...and only fills gaps (never fabricates census where prior had none).
    assert merged[0]["census_tract"] == "013628"
    assert merged[1]["census_tract"] is None
    # Validity classification survives the merge untouched.
    assert merged[0].get(INVALID_QUERY_KEY) is None
    assert merged[1][INVALID_QUERY_KEY]
    # Stripping yields clean legacy rows ready for write_json.
    cleaned = strip_invalid_query_markers(merged)
    assert all(INVALID_QUERY_KEY not in row for row in cleaned)
