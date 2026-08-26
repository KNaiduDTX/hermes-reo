# src/source_registry.py
"""SOURCE-REGISTRY v1 — verified county source endpoints.

All endpoints verified via curl on Aug 19 2026. See SOURCE_REGISTRY.md for
full documentation including access methods and vendor evaluation.

TD3 single URL authority: every portal URL used by an adapter is DERIVED
from a REGISTRY entry via get_endpoint()/require_endpoint() at adapter
import time — adapter modules carry no hardcoded portal-URL literals.
assert_registry_sync() enforces the binding table (ADAPTER_URL_BINDINGS)
and fails listing every drifted constant. This module imports nothing but
the stdlib, so registry lookups never pull Playwright-heavy modules.
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
    # TD3 gap fill: site root the adapter resolves relative PDF hrefs against.
    SourceEndpoint("dallas", "county_site",
                   "https://www.dallascounty.org",
                   "200", "http",
                   "Dallas County portal root — foreclosure PDFs, sheriff sales and "
                   "struck-off pages all live under this host (TD3)"),
    SourceEndpoint("dallas", "cad",
                   "https://www.dallascad.org",
                   "200", "http",
                   "SearchAddr / SearchAcct"),
    # TD3 gap fill: the address-search form the DCAD adapter actually drives.
    SourceEndpoint("dallas", "cad_search",
                   "https://www.dallascad.org/SearchAddr.aspx",
                   "200", "http",
                   "ASP.NET address search form — #txtAddrNum/#txtStName + "
                   "input[name=cmdSubmit] (TD3)"),
    # TD3 gap fill: detail links on the results grid are relative; the adapter
    # resolves them against this base URL.
    SourceEndpoint("dallas", "cad_detail",
                   "https://www.dallascad.org",
                   "200", "http",
                   "DCAD property-detail base URL — relative detail hrefs resolve "
                   "against this host (TD3)"),
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
    # TD3 gap fills: the Grayson adapter ships four more county URLs that had
    # no registry entries; statuses mirror grayson_tax.py's verified comments.
    SourceEndpoint("grayson", "county_site",
                   "https://www.co.grayson.tx.us",
                   "403_browser", "browser",
                   "Grayson county site root — 403 to curl, renders in browser (TD3)"),
    SourceEndpoint("grayson", "county_tax_page",
                   "https://www.co.grayson.tx.us/page/tax.sale",
                   "403_browser", "browser",
                   "County tax-sale info page — 403 Forbidden to curl (TD3); "
                   "fallback when the taxsearch portal is unreachable"),
    SourceEndpoint("grayson", "sheriff_sale",
                   "https://www.co.grayson.tx.us/page/gcso.home",
                   "403_browser", "browser",
                   "Sheriff's office page — live in browser but carries no sale "
                   "listings (TD3)"),
    SourceEndpoint("grayson", "tax_assessor_portal",
                   "https://grayson.propertytaxpayments.net/landing-page-en-us",
                   "403_browser", "browser",
                   "Tax Assessor-Collector payments portal — payments only, no "
                   "sale listings (TD3)"),
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


def require_endpoint(county: str, channel: str) -> SourceEndpoint:
    """get_endpoint that RAISES instead of returning None.

    Adapter modules call this at import time to derive their URL constants,
    so a registry entry deleted or renamed fails loudly with a actionable
    message instead of surfacing later as AttributeError on None.
    """
    endpoint = get_endpoint(county, channel)
    if endpoint is None:
        raise LookupError(
            f"source_registry has no endpoint for (county={county!r}, "
            f"channel={channel!r}) — add it to REGISTRY; adapters must not "
            f"hardcode portal URLs"
        )
    return endpoint


def get_live_endpoints() -> list[SourceEndpoint]:
    """Return only endpoints that are verified live (200 or 403_browser)."""
    return [e for e in REGISTRY if e.http_status in ("200", "403_browser")]


def get_unverified_endpoints() -> list[SourceEndpoint]:
    """Return endpoints that need verification."""
    return [e for e in REGISTRY if e.http_status in ("000_timeout", "tbd")]


# --- TD3: adapter-constant <-> registry bindings -----------------------------
# Each tuple pins one adapter module-level URL constant to the single registry
# endpoint whose .url it must equal. assert_registry_sync() enforces every
# pair; tests/test_registry_urls.py iterates the same table.
ADAPTER_URL_BINDINGS: list[tuple[str, str, str, str]] = [
    # (adapter module path, constant name, county, channel)
    ("src.adapters.collin_foreclosure", "BASE_URL", "collin", "foreclosure_notice"),
    ("src.adapters.collin_clerk", "CLERK_URL", "collin", "clerk"),
    ("src.adapters.dallas_cad", "DCAD_URL", "dallas", "cad_search"),
    ("src.adapters.dallas_cad", "DCAD_DETAIL_BASE_URL", "dallas", "cad_detail"),
    ("src.adapters.dallas_foreclosure", "PORTAL_URL", "dallas", "foreclosure_notice"),
    ("src.adapters.dallas_foreclosure", "BASE_URL", "dallas", "county_site"),
    ("src.adapters.grayson_tax", "TAX_SALE_URL", "grayson", "tax_sale"),
    ("src.adapters.grayson_tax", "COUNTY_TAX_PAGE", "grayson", "county_tax_page"),
    ("src.adapters.grayson_tax", "SHERIFF_SALE_PAGE", "grayson", "sheriff_sale"),
    ("src.adapters.grayson_tax", "TAX_ASSESSOR_PORTAL", "grayson", "tax_assessor_portal"),
    ("src.adapters.grayson_tax", "COUNTY_MAIN", "grayson", "county_site"),
]


def assert_registry_sync() -> None:
    """Fail with a full drift listing when adapters and REGISTRY disagree.

    TD3: adapter URL constants are derived from get_endpoint()/require_endpoint()
    at import time, so any drift means a hardcoded portal literal was
    reintroduced or this binding table went stale. Every problem is collected
    and reported in ONE AssertionError rather than failing on the first.

    Adapter modules are imported lazily INSIDE this helper (they pull
    Playwright/bs4/pypdf); importing source_registry itself stays stdlib-only.
    """
    import importlib

    problems: list[str] = []
    for module_name, const_name, county, channel in ADAPTER_URL_BINDINGS:
        label = f"{module_name}.{const_name} <- ({county}, {channel})"
        endpoint = get_endpoint(county, channel)
        if endpoint is None:
            problems.append(f"{label}: registry entry missing")
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 — report, never propagate mid-listing
            problems.append(f"{label}: import failed ({type(exc).__name__}: {exc})")
            continue
        value = getattr(module, const_name, None)
        if value is None:
            problems.append(f"{label}: constant not defined on module")
        elif value != endpoint.url:
            problems.append(
                f"{label}: constant is {value!r}, registry url is {endpoint.url!r}")
    if problems:
        raise AssertionError(
            "source-registry sync FAILED — "
            f"{len(problems)} drifted/unresolvable binding(s):\n  - "
            + "\n  - ".join(problems)
        )


if __name__ == "__main__":
    # Self-check
    all_eps = REGISTRY
    assert len(all_eps) >= 21, f"Expected >=21 endpoints, got {len(all_eps)}"

    collin = get_endpoints(county="collin")
    assert len(collin) >= 4

    foreclosure = get_endpoint("collin", "foreclosure_notice")
    assert foreclosure is not None
    assert foreclosure.url == "https://apps2.collincountytx.gov/ForeclosureNotices"

    live = get_live_endpoints()
    unverified = get_unverified_endpoints()
    assert len(live) >= 14
    assert len(unverified) >= 2  # grayson tax_sale + collin tax_suit + grayson struck_off

    # TD3: every adapter-bound channel resolves; require_endpoint raises loudly
    # on gaps. Every bound URL must be absolute https.
    for _module, _const, county, channel in ADAPTER_URL_BINDINGS:
        ep = require_endpoint(county, channel)
        assert ep.url.startswith("https://"), (county, channel, ep.url)

    print(f"source_registry.py: {len(all_eps)} endpoints, {len(live)} live, {len(unverified)} unverified")
    print(f"source_registry.py: {len(ADAPTER_URL_BINDINGS)} adapter-constant bindings declared")
    print("source_registry.py: all self-checks passed")