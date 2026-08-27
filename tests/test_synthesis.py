"""Audit TEST-6: src/synthesis.py had ZERO pytest coverage — the only gate
was print-based __main__ self-checks. Portfolio rollups and the weekly digest
are money-adjacent analytics; pin them here with a small CPR-list fixture.
Ported from the __main__ asserts (kept there too, so make checks still works)."""
import json

import pytest

from src.synthesis import portfolio_stats, weekly_digest


@pytest.fixture
def flat_rows():
    return [
        {"address": "10022 Plainsman Ln, Frisco, TX 75035",
         "county": "collin",
         "owner": "Islam Mazhar & Ambreen",
         "property_type": "Residential Single Family (A1)",
         "sqft": 4332, "year_built": 2004,
         "cad_value": 730673.00,
         "mortgage_debt": 350000, "lender": "Wells Fargo"},
        {"address": "15806 Breedlove Pl, Addison, TX 75001",
         "county": "dallas",
         "owner": "KERRY P. GRAY",
         "property_type": "Residential Single Family (A1)",
         "cad_value": 387000.00,
         "mortgage_debt": 309600, "lender": "CTX Mortgage"},
    ]


def test_portfolio_stats_over_flat_rows(flat_rows):
    stats = portfolio_stats(flat_rows)
    assert stats["total_properties"] == 2
    assert "Frisco" in stats["cities"]
    assert "Addison" in stats["cities"]
    assert stats["appraised"]["total"] == 1_117_673.0
    assert stats["debt"]["total"] == 659_600.0
    assert "Wells Fargo" in stats["lender_concentration"]
    assert "CTX Mortgage" in stats["lender_concentration"]
    # Neither flat row carries isd/taxing entities -> honest Unknown for both.
    assert stats["isds"] == {"Unknown": 2}


def test_weekly_digest_renders_deterministically(flat_rows):
    digest = weekly_digest(flat_rows)
    assert isinstance(digest, str) and digest.strip()
    assert "## PORTFOLIO SUMMARY" in digest
    # Deterministic per input set (the Generated stamp is the only drift).
    def body(d):
        return [l for l in d.splitlines() if not l.startswith("Generated:")]
    assert body(digest) == body(weekly_digest(flat_rows))
    # And it is JSON-stable when a caller serializes it (artifact contract).
    json.dumps({"digest": digest})


def test_portfolio_stats_empty_is_honest():
    stats = portfolio_stats([])
    assert stats["total_properties"] == 0
