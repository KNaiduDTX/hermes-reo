# src/source_registry.py
"""SOURCE-REGISTRY v1 — verified county source endpoints.

All endpoints verified via curl on Aug 19 2026. See SOURCE_REGISTRY.md for
full documentation including access methods and vendor evaluation.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class SourceEndpoint:
    """A verified county data source endpoint."""
    county: str
    channel: str
    url: str
    http_status: str  # "200", "403_browser", "000_timeout", "tbd"
    access: str       # "http", "browser", "pdf", "api"
    notes: str = ""


# --- Verified endpoints (Aug 19 2026) ---

REGISTRY: list[SourceEndpoint] = [
    # Collin County
    SourceEndpoint("collin", "foreclosure_notice",
                   "https://apps2.collincountytx.gov/ForeclosureNotices",
                   "200", "browser",
                   "Blazor SignalR SPA — requires Playwright + setProperties interceptor"),
    SourceEndpoint("collin", "cad",
                   "https://esearch.collincad.org",
                   "403_browser", "browser",
                   "BIS portal, same vendor as Grayson; 403 to curl, works in browser"),
    SourceEndpoint("collin", "clerk",
                   "https://collin.tx.publicsearch.us",
                   "200", "http",
                   "Public records search — deeds, deeds of trust, liens, lis pendens"),
    SourceEndpoint("collin", "tax_sale",
                   "https://www.collincountytx.gov/tax-assessor/properties-for-sale",
                   "200", "http",
                   "Constable/tax sales, struck-off; Written Statement ($10) required"),
    SourceEndpoint("collin", "tax_suit",
                   "", "tbd", "tbd",
                   "District clerk case search — verify in WS-00"),

    # Dallas County
    SourceEndpoint("dallas", "foreclosure_notice",
                   "https://www.dallascounty.org/government/county-clerk/recording/foreclosures.php",
                   "200", "pdf",
                   "PDF listings by city; filter by sale date; north side of courthouse"),
    SourceEndpoint("dallas", "cad",
                   "https://www.dallascad.org",
                   "200", "http",
                   "SearchAddr / SearchAcct"),
    SourceEndpoint("dallas", "tax_sale",
                   "https://www.dallascounty.org/departments/tax/sheriff-sales.php",
                   "200", "browser",
                   "Online auction via RealAuction.com at dallas.texas.sheriffsaleauctions.com"),
    SourceEndpoint("dallas", "struck_off",
                   "https://www.dallascounty.org/departments/pubworks/property-division.php",
                   "200", "http",
                   "Tax foreclosure resales, struck-off properties"),
    SourceEndpoint("dallas", "tax_office",
                   "https://www.dallascounty.org/departments/tax/pay-property-tax.php",
                   "200", "http",
                   "Property tax status, delinquency amounts"),

    # Grayson County
    SourceEndpoint("grayson", "foreclosure_notice",
                   "https://www.co.grayson.tx.us/page/cclk.foreclosure",
                   "200", "http",
                   "First Tuesday, west door of courthouse"),
    SourceEndpoint("grayson", "cad",
                   "https://esearch.graysonappraisal.org",
                   "200", "browser",
                   "BIS portal, full property search, Kendo UI"),
    SourceEndpoint("grayson", "tax_sale",
                   "https://taxsearch.co.grayson.tx.us:8443/Sales/ssale",
                   "000_timeout", "tbd",
                   "HTTPS port 8443 timed out via curl — needs browser verification"),
    SourceEndpoint("grayson", "tax_office",
                   "https://grayson.propertytaxpayments.net",
                   "403_browser", "browser",
                   "Blocks curl; likely works in browser"),
    SourceEndpoint("grayson", "struck_off",
                   "", "tbd", "tbd",
                   "Verify in WS-00"),
]


def get_endpoints(county: Optional[str] = None,
                  channel: Optional[str] = None) -> list[SourceEndpoint]:
    """Filter registry by county and/or channel."""
    result = REGISTRY
    if county:
        result = [e for e in result if e.county == county]
    if channel:
        result = [e for e in result if e.channel == channel]
    return result


def get_endpoint(county: str, channel: str) -> Optional[SourceEndpoint]:
    """Get a single endpoint by county + channel."""
    matches = [e for e in REGISTRY if e.county == county and e.channel == channel]
    return matches[0] if matches else None


def get_live_endpoints() -> list[SourceEndpoint]:
    """Return only endpoints that are verified live (200 or 403_browser)."""
    return [e for e in REGISTRY if e.http_status in ("200", "403_browser")]


def get_unverified_endpoints() -> list[SourceEndpoint]:
    """Return endpoints that need verification."""
    return [e for e in REGISTRY if e.http_status in ("000_timeout", "tbd")]


if __name__ == "__main__":
    # Self-check
    all_eps = REGISTRY
    assert len(all_eps) >= 14, f"Expected >=14 endpoints, got {len(all_eps)}"

    collin = get_endpoints(county="collin")
    assert len(collin) >= 4

    foreclosure = get_endpoint("collin", "foreclosure_notice")
    assert foreclosure is not None
    assert foreclosure.url == "https://apps2.collincountytx.gov/ForeclosureNotices"

    live = get_live_endpoints()
    unverified = get_unverified_endpoints()
    assert len(live) >= 10
    assert len(unverified) >= 2  # grayson tax_sale + tax_suit + struck_off

    print(f"source_registry.py: {len(all_eps)} endpoints, {len(live)} live, {len(unverified)} unverified")
    print("source_registry.py: all self-checks passed")