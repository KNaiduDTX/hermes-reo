# src/adapters/collin_foreclosure.py
"""Collin County foreclosure-notice adapter — PORTED from lucid-noether.

The Collin portal is a Blazor Server SPA that pushes data via SignalR.
The only way to get listings is to intercept window.setProperties in Playwright.
Detail pages are accessible via plain HTTP (urllib/httpx) once you have the entryID.

Source: /Users/knaidu/Documents/antigravity/lucid-noether/foreclosure_crawler.py
"""
from __future__ import annotations
import asyncio
import json
import time
import urllib.request
import urllib.error
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .base import SourceAdapter, NormalizedLead, SourceProvenance

BASE_URL = "https://apps2.collincountytx.gov/ForeclosureNotices"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _clean(text: str) -> str:
    if not text:
        return "Unknown"
    return " ".join(text.split()).strip()


async def _get_foreclosure_list(limit: int | None = None) -> list[dict]:
    """Navigate to the Collin portal, intercept Blazor SignalR data push.

    Ported from lucid-noether foreclosure_crawler.py lines 25-84.
    The portal is a Blazor Server app — no HTTP API, no static HTML.
    window.setProperties is the callback Blazor uses to push listing data.
    """
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception:
            import os
            os.system("playwright install chromium")
            browser = await p.chromium.launch(headless=True)

        page = await browser.new_page()
        await page.goto(BASE_URL, timeout=60000)

        # Intercept the Blazor data push
        await page.evaluate("""
            window.myItemsData = null;
            if (typeof window.setProperties === 'function') {
                const originalSetProperties = window.setProperties;
                window.setProperties = function(myItems) {
                    window.myItemsData = myItems;
                    try { originalSetProperties(myItems); } catch (err) {}
                };
            }
        """)

        # Poll for data (up to 10 seconds)
        captured = None
        for _ in range(20):
            await asyncio.sleep(0.5)
            data = await page.evaluate("window.myItemsData")
            if data is not None:
                captured = data
                break

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
    """
    url = f"{BASE_URL}/DetailPage/{entry_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read()
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


class CollinForeclosureAdapter(SourceAdapter):
    """Collin County foreclosure-notice portal adapter."""
    channel = "foreclosure_notice"
    county = "collin"

    def fetch(self, limit: int | None = None, extract_details: bool = True) -> list[NormalizedLead]:
        """Fetch foreclosure listings from the Collin portal.

        Args:
            limit: Max number of listings to retrieve (None = all).
            extract_details: If True, fetch detail pages for each listing.
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
                lead.raw_data["details"] = details

            leads.append(lead)

        return leads

    def provenance(self) -> SourceProvenance:
        return SourceProvenance(source="collin_foreclosure_notice", source_rigor_tier=0)


if __name__ == "__main__":
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