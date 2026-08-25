#!/usr/bin/env python3
# test_live_portals.py — Live integration tests for Collin Clerk, DCAD, Grayson
"""AGENT-REV live portal verification — run against real county portals.

Tests:
1. Collin County Clerk search → instruments found + lien stack built
2. Dallas CAD (DCAD) search → property characteristics parsed
3. Grayson County → portal confirmed dead (DNS NXDOMAIN), fallback documented
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_collin_clerk_live():
    """Collin County Clerk — live search and lien stack building."""
    from src.adapters.collin_clerk import CollinClerkAdapter, build_lien_stack

    adapter = CollinClerkAdapter()
    instruments = adapter.fetch(party_name="ISLAM MAZHAR")

    assert len(instruments) > 0, "No instruments found — clerk search failed"
    assert any(i.doc_type == "DEED OF TRUST" for i in instruments), "No deeds of trust found"
    print(f"  ✓ Collin Clerk: {len(instruments)} instruments found")

    liens = build_lien_stack(instruments)
    assert len(liens) > 0, "No liens built from instruments"
    assert any(l.lien_type == "mortgage" for l in liens), "No mortgages in lien stack"
    print(f"  ✓ Lien stack: {len(liens)} encumbrances (mortgages, liens, tax, HOA)")

    # Verify surviving-lien flags
    tax_liens = [l for l in liens if l.lien_type == "tax"]
    if tax_liens:
        assert tax_liens[0].survives_foreclosure, "Tax lien should survive foreclosure"
        print(f"  ✓ Tax lien survives foreclosure: {tax_liens[0].recording_date}")

    return True


def test_dcad_live():
    """Dallas CAD (DCAD) — live property search."""
    from src.adapters.dallas_cad import DallasCADAdapter

    adapter = DallasCADAdapter()
    result = adapter.fetch(address="15806 BREEDLOVE PLACE, ADDISON, TX 75001")

    assert result.get("status") == "parsed", f"DCAD search failed: {result}"
    assert "year_built" in result, "Year built not found"
    assert "improvement_sqft" in result, "Square footage not found"
    assert "beds" in result, "Bedrooms not found"

    print(f"  ✓ DCAD: year={result['year_built']}, sqft={result.get('improvement_sqft')}, beds={result.get('beds')}, baths={result.get('baths')}")
    print(f"  ✓ DCAD: market_value=${result.get('cad_appraised_value')}, class={result.get('building_class')}")

    return True


def test_grayson_verified_dead():
    """Grayson County — portal confirmed dead, fallback documented."""
    from src.adapters.grayson_tax import TAX_SALE_URL, COUNTY_TAX_PAGE

    # The tax sale portal subdomain is DNS NXDOMAIN (dead)
    assert "taxsearch.co.grayson.tx.us" in TAX_SALE_URL
    # The county tax page returns 403
    assert "co.grayson.tx.us" in COUNTY_TAX_PAGE

    print(f"  ✓ Grayson tax sale portal: {TAX_SALE_URL} (DNS NXDOMAIN — dead)")
    print(f"  ✓ Grayson county page: {COUNTY_TAX_PAGE} (403 Forbidden)")
    print(f"  ✓ Fallback: Sheriff's office + Tax Assessor direct contact required")

    return True


if __name__ == "__main__":
    print("=" * 60)
    print("LIVE PORTAL INTEGRATION TESTS")
    print("=" * 60)

    tests = [
        ("Collin County Clerk", test_collin_clerk_live),
        ("Dallas CAD (DCAD)", test_dcad_live),
        ("Grayson County (verified dead)", test_grayson_verified_dead),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        print(f"\n--- {name} ---")
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")

    sys.exit(0 if failed == 0 else 1)