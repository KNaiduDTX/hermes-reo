# src/adapters/collin_foreclosure.py
"""Collin County foreclosure-notice adapter — PORTED from lucid-noether.

The Collin portal is a Blazor Server SPA that pushes data via SignalR.
The only way to get listings is to intercept window.setProperties in Playwright.
Detail pages are accessible via plain HTTP (urllib/httpx) once you have the entryID.

Source: /Users/knaidu/Documents/antigravity/lucid-noether/foreclosure_crawler.py

FIXES:
- M3/S5: browser close guaranteed via try/finally; the SignalR interceptor
  no longer swallows errors from the wrapped setProperties; a missing
  Chromium raises RuntimeError with the manual install command instead of
  running os.system("playwright install ...") from library code.
- S6: failed detail scrapes are flagged on the lead (raw_data
  details_status="error") so downstream can skip error payloads instead of
  mistaking them for parsed data.
"""
from __future__ import annotations
import asyncio
import time
import urllib.request
import urllib.error
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .base import SourceAdapter, NormalizedLead, SourceProvenance
from ..source_registry import assert_registry_sync, require_endpoint
import logging

_log = logging.getLogger(__name__)

# TD3: derived from SOURCE-REGISTRY at import — no hardcoded portal literal.
BASE_URL = require_endpoint("collin", "foreclosure_notice").url
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

DETAILS_STATUS_OK = "ok"
DETAILS_STATUS_ERROR = "error"


def _clean(text: str) -> str:
    if not text:
        return "Unknown"
    return " ".join(text.split()).strip()


# Blazor SignalR interceptor. The original wrapped callback is invoked
# WITHOUT a swallowing try/catch so its failures surface as Playwright
# evaluate errors instead of being silently eaten inside the page.
_INTERCEPTOR_JS = """
    window.myItemsData = null;
    if (typeof window.setProperties === 'function') {
        const originalSetProperties = window.setProperties;
        window.setProperties = function(myItems) {
            window.myItemsData = myItems;
            originalSetProperties(myItems);
        };
    }
"""


async def _get_foreclosure_list(limit: int | None = None) -> list[dict]:
    """Navigate to the Collin portal, intercept Blazor SignalR data push.

    Ported from lucid-noether foreclosure_crawler.py lines 25-84.
    The portal is a Blazor Server app — no HTTP API, no static HTML.
    window.setProperties is the callback Blazor uses to push listing data.
    """
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:
            raise RuntimeError(
                "Chromium is not available to Playwright. Install it manually "
                "with: `playwright install chromium` "
                "(or `.venv/bin/python -m playwright install chromium`)."
            ) from e

        page = await browser.new_page()
        try:
            await page.goto(BASE_URL, timeout=60000)

            # Intercept the Blazor data push (errors surface, never swallowed)
            await page.evaluate(_INTERCEPTOR_JS)

            # Poll for data (up to 10 seconds)
            captured = None
            for _ in range(20):
                await asyncio.sleep(0.5)
                data = await page.evaluate("window.myItemsData")
                if data is not None:
                    captured = data
                    break
        finally:
            await browser.close()

    if not captured:
        raise RuntimeError("Failed to retrieve listings from Blazor SignalR socket.")

    if limit:
        captured = captured[:limit]
    return captured


def _scrape_detail_page(entry_id: int) -> dict:
    """Fetch detail page for a specific foreclosure record.

    Ported from lucid-noether foreclosure_crawler.py lines 86-159.
    Detail pages are plain HTTP (not Blazor) — accessible via urllib.
    Uses BeautifulSoup to parse MudBlazor grid layout.

    On fetch failure returns {"error": <message>} — callers MUST treat any
    dict containing an "error" key as a failure payload, not parsed data
    (see _details_status / finding S6).
    """
    url = f"{BASE_URL}/DetailPage/{entry_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read()
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} fetching {url}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

    soup = BeautifulSoup(html, "html.parser")
    details = {
        "detail_page_url": url,
        "property_info": {},
        "improvement_info": {},
        "jurisdiction_info": {},
        "owner_info": {},
        "tax_info": {},
        "scanned_notice_image": None,
    }

    def parse_section(section_title: str) -> dict:
        section_data = {}
        h2 = soup.find(lambda tag: tag.name == "h2" and section_title in tag.get_text())
        if not h2:
            return section_data
        container = h2.find_next_sibling("div")
        if not container:
            return section_data
        rows = container.find_all("div", class_="mud-grid")
        for row in rows:
            key_el = row.find(class_="item-title")
            if key_el:
                key = _clean(key_el.get_text()).replace(":", "")
                grid_items = row.find_all("div", class_="mud-grid-item")
                if len(grid_items) > 1:
                    val_paragraphs = grid_items[1].find_all("p")
                    if val_paragraphs:
                        val = " / ".join([_clean(p.get_text()) for p in val_paragraphs if p.get_text()])
                    else:
                        val = _clean(grid_items[1].get_text())
                else:
                    val = "Unknown"
                section_data[key] = val
        return section_data

    details["property_info"] = parse_section("Property Information")
    details["improvement_info"] = parse_section("Improvement Information")
    details["jurisdiction_info"] = parse_section("Jurisdiction Information")
    details["owner_info"] = parse_section("Owner Information")
    details["tax_info"] = parse_section("Tax Information")

    img = soup.find("img", class_="mud-image")
    if img:
        details["scanned_notice_image"] = img.get("src", "")

    return details


def _details_status(details: object) -> str:
    """Classify a detail scrape result (finding S6).

    Payloads containing an "error" key are failure markers, NOT data — they
    get details_status="error" so downstream can skip them.
    """
    if isinstance(details, dict) and "error" in details:
        return DETAILS_STATUS_ERROR
    return DETAILS_STATUS_OK


class CollinForeclosureAdapter(SourceAdapter):
    """Collin County foreclosure-notice portal adapter."""
    channel = "foreclosure_notice"
    county = "collin"

    def fetch(self, limit: int | None = None, extract_details: bool = True) -> list[NormalizedLead]:
        """Fetch foreclosure listings from the Collin portal.

        Args:
            limit: Max number of listings to retrieve (None = all).
            extract_details: If True, fetch detail pages for each listing.
                Leads whose detail scrape failed carry raw_data
                details_status="error" plus details_error; enrichment should
                skip those payloads (flagging lives here, consuming it does
                not — finding S6).
        """
        listings = asyncio.run(_get_foreclosure_list(limit))
        leads = []

        for item in listings:
            entry_id = item.get("entryID")
            address = _clean(item.get("propertyAddress", ""))
            lat = item.get("lat")
            lng = item.get("long")

            lead = NormalizedLead(
                county="collin",
                parcel_id=str(entry_id) if entry_id else "",
                channel="foreclosure_notice",
                situs_address=address,
                latitude=lat,
                longitude=lng,
                raw_data={"entry_id": entry_id, "detail_url": f"{BASE_URL}/DetailPage/{entry_id}"},
            )

            if extract_details and entry_id:
                time.sleep(1.0)  # rate limit — plain sleep, not asyncio
                details = _scrape_detail_page(entry_id)
                lead.raw_data["details_status"] = _details_status(details)
                lead.raw_data["details"] = details
                if lead.raw_data["details_status"] == DETAILS_STATUS_ERROR:
                    _log.warning(f"Collin foreclosure: detail scrape FAILED for "
                          f"entry {entry_id} ({details.get('error')}) — flagged "
                          f"raw_data.details_status='error', do not consume")

            leads.append(lead)

        return leads

    def provenance(self) -> SourceProvenance:
        return SourceProvenance(source="collin_foreclosure_notice", source_rigor_tier=0)


if __name__ == "__main__":
    import ast
    import inspect
    from src.adapters.base import NormalizedLead

    # Self-check (TD3): BASE_URL is registry-derived; assert the whole
    # adapter-constant <-> REGISTRY binding table holds.
    assert_registry_sync()
    print("collin_foreclosure.py: PASS — BASE_URL derived from SOURCE-REGISTRY "
          "(assert_registry_sync)")

    # Self-check (S6): error payloads are classified, ok data passes through
    assert _details_status({"error": "HTTP 500"}) == DETAILS_STATUS_ERROR
    assert _details_status({"property_info": {"Address": "1 Main St"}}) == DETAILS_STATUS_OK
    assert _details_status({"detail_page_url": "x"}) == DETAILS_STATUS_OK
    assert _details_status(None) == DETAILS_STATUS_OK
    print("collin_foreclosure.py: PASS — error dicts flagged details_status='error'")

    # Self-check (S6): flagging wires onto a real lead object shape
    lead = NormalizedLead(county="collin", parcel_id="4003592",
                          channel="foreclosure_notice", situs_address="1 Test St",
                          raw_data={"entry_id": 4003592})
    fake_error_payload = {"error": "connection refused"}
    lead.raw_data["details_status"] = _details_status(fake_error_payload)
    lead.raw_data["details"] = fake_error_payload
    assert lead.raw_data["details_status"] == DETAILS_STATUS_ERROR
    assert "error" in lead.raw_data["details"]
    print("collin_foreclosure.py: PASS — lead.raw_data carries details_status flag")

    # Self-check (M3): no os.system playwright-install in library code; the
    # failure path instructs the manual command instead.
    list_src = inspect.getsource(_get_foreclosure_list)
    tree = ast.parse(list_src)
    os_system_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "system"
    ]
    assert not os_system_calls, \
        "M3 regression: os.system(...) found in library code again"
    assert "finally:" in list_src and "await browser.close()" in list_src.split("finally:")[-1], \
        "M3 regression: browser.close() not guarded by finally"
    assert "playwright install chromium" in list_src, \
        "M3 regression: manual install instruction missing from RuntimeError"
    # Interceptor must invoke the wrapped callback without a swallowing catch
    assert "catch (err)" not in _INTERCEPTOR_JS, \
        "M3/S5 regression: interceptor swallows setProperties errors again"
    print("collin_foreclosure.py: PASS — no os.system install; close-in-finally; "
          "interceptor surfaces errors; manual install command documented")

    # Self-check: detail page parser on a known entry_id from lucid-noether data
    # Use entry_id 4003592 (verified in prior scrape)
    details = _scrape_detail_page(4003592)
    if "error" in details:
        print(f"Detail page fetch failed (expected if portal is down): {details['error']}")
        print("collin_foreclosure.py: skipping live test (portal may require browser session)")
    else:
        prop = details.get("property_info", {})
        assert prop.get("Address"), "Expected address in property_info"
        print(f"collin_foreclosure.py: detail page parsed — {prop.get('Address')}")
        print(f"  Type: {prop.get('Type')}")
        print(f"  Legal: {prop.get('Legal Description', 'N/A')[:60]}")
    print("collin_foreclosure.py: self-check complete")
