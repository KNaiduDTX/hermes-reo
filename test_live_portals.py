#!/usr/bin/env python3
# test_live_portals.py — Live integration tests for Collin Clerk, DCAD, Grayson
"""AGENT-REV live portal verification — run against real county portals.

All tests are marked `live` (pytest -m live; `make test-live`). They hit real
county sites and are excluded from the offline suite (`make test`).

Tests:
1. Collin County Clerk search → instruments found + lien stack built
   (party configurable via HERMES_TEST_PARTY, default "ISLAM MAZHAR")
2. Dallas CAD (DCAD) search → property characteristics parsed
   (address configurable via HERMES_TEST_ADDRESS,
    default "15806 BREEDLOVE PLACE, ADDISON, TX 75001")
3. Grayson County registry consistency → adapter endpoints agree with the
   verified SOURCE-REGISTRY entries (offline assertions, no network)
"""
import os
import sys

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.live

TEST_PARTY = os.environ.get("HERMES_TEST_PARTY", "ISLAM MAZHAR")
TEST_ADDRESS = os.environ.get("HERMES_TEST_ADDRESS", "15806 BREEDLOVE PLACE, ADDISON, TX 75001")


def test_collin_clerk_live():
    """Collin County Clerk — live search and lien stack building."""
    from src.adapters.collin_clerk import CollinClerkAdapter, build_lien_stack

    adapter = CollinClerkAdapter()
    instruments = adapter.fetch(party_name=TEST_PARTY)

    assert len(instruments) > 0, f"No instruments found for party {TEST_PARTY!r} — clerk search failed"
    assert any(i.doc_type == "DEED OF TRUST" for i in instruments), "No deeds of trust found"

    liens = build_lien_stack(instruments)
    assert len(liens) > 0, "No liens built from instruments"
    assert any(l.lien_type == "mortgage" for l in liens), "No mortgages in lien stack"

    # Verify surviving-lien flags
    tax_liens = [l for l in liens if l.lien_type == "tax"]
    if tax_liens:
        assert tax_liens[0].survives_foreclosure, "Tax lien should survive foreclosure"


def test_dcad_live():
    """Dallas CAD (DCAD) — live property search."""
    from src.adapters.dallas_cad import DallasCADAdapter

    adapter = DallasCADAdapter()
    result = adapter.fetch(address=TEST_ADDRESS)

    assert result.get("status") == "parsed", f"DCAD search failed: {result}"
    assert "year_built" in result, "Year built not found"
    assert "improvement_sqft" in result, "Square footage not found"
    assert "beds" in result, "Bedrooms not found"


def test_grayson_registry_consistency():
    """Grayson County — adapter endpoint constants agree with SOURCE-REGISTRY v1.

    The tax-sale portal is verified dead (DNS NXDOMAIN / timeout); the registry
    must document the same URL the adapter ships, still marked unverified.
    """
    from src.adapters.grayson_tax import TAX_SALE_URL, COUNTY_TAX_PAGE
    from src.source_registry import get_endpoint

    tax_sale = get_endpoint("grayson", "tax_sale")
    assert tax_sale is not None, "registry missing grayson tax_sale endpoint"
    assert TAX_SALE_URL == tax_sale.url, (
        f"adapter TAX_SALE_URL {TAX_SALE_URL!r} drifted from registry {tax_sale.url!r}"
    )
    assert tax_sale.http_status != "200", (
        "registry marks grayson tax sale as live — portal is verified dead; fix the registry"
    )

    # The county fallback page lives on the same verified grayson county host
    foreclosure = get_endpoint("grayson", "foreclosure_notice")
    assert foreclosure is not None, "registry missing grayson foreclosure_notice endpoint"
    county_host = foreclosure.url.split("/")[2]
    assert county_host in COUNTY_TAX_PAGE, (
        f"COUNTY_TAX_PAGE host mismatch: {COUNTY_TAX_PAGE!r} vs registry host {county_host!r}"
    )
    assert "co.grayson.tx.us" in TAX_SALE_URL


if __name__ == "__main__":
    print("=" * 60)
    print("LIVE PORTAL INTEGRATION TESTS (pytest)")
    print("=" * 60)
    raise SystemExit(pytest.main([__file__, "-v"]))
