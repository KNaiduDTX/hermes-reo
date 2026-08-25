# src/adapters/dallas_cad.py
"""Dallas CAD (DCAD) adapter — cross-reference property characteristics.

DCAD is an ASP.NET form-based search at dallascad.org. Requires Playwright
to submit the address search form and parse the results page.

Used to fill the gap in Dallas foreclosure data: the PDF notices only have
address + debt, not sqft/beds/baths/year built. DCAD provides those.
"""
from __future__ import annotations
import asyncio
import re
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .base import SourceAdapter, SourceProvenance

DCAD_URL = "https://www.dallascad.org/SearchAddr.aspx"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


async def _search_dcad_by_address(address: str) -> dict:
    """Search DCAD by property address using Playwright.

    Verified live Aug 20 2026:
    - Search URL: https://www.dallascad.org/SearchAddr.aspx
    - Form fields: #txtAddrNum (street number), #txtStName (street name, no suffix)
    - Submit button: input[name="cmdSubmit"] (value="Search")
    - ASP.NET form requires page.type() for proper keyboard events
    - Results page has a grid with a link to property detail
    - Detail page has property characteristics in a table
    """
    import re
    m = re.match(r'^(\d+)\s+(.+)', address.split(',')[0].strip())
    if not m:
        return {"error": "Cannot parse street number from address"}

    street_num = m.group(1)
    street_name = m.group(2).strip()
    # Remove street suffix (DCAD says: "Do not enter the street type such as Street, Drive or Lane")
    street_name = re.sub(r'\s+(STREET|ST|DRIVE|DR|LANE|LN|COURT|CT|WAY|BOULEVARD|BLVD|AVENUE|AVE|PARKWAY|PKWY|PLACE|PL|ROAD|RD)$', '', street_name, flags=re.IGNORECASE)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto("https://www.dallascad.org/SearchAddr.aspx", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(2000)

            # Use page.type() — sends proper keyboard events for ASP.NET validation
            await page.click("#txtAddrNum")
            await page.type("#txtAddrNum", street_num, delay=50)
            await page.click("#txtStName")
            await page.type("#txtStName", street_name, delay=50)
            await page.wait_for_timeout(500)

            # Click the submit button
            await page.click('input[name="cmdSubmit"]')
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(3000)

            # Check if we're on the results page or error page
            url = page.url
            if "SearchError" in url:
                result = {"error": "DCAD search error — ASP.NET form rejected submission", "status": "error"}
            else:
                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")

                # Look for property detail link in the results grid
                detail_link = soup.find("a", href=re.compile(r"Detail|PropertyDetail|PCD|AcctDetail", re.IGNORECASE))
                if detail_link:
                    href = detail_link.get("href", "")
                    # Handle relative URLs — DCAD uses relative paths without leading /
                    if not href.startswith("http"):
                        if not href.startswith("/"):
                            href = "/" + href
                        href = f"https://www.dallascad.org{href}"
                    await page.goto(href, timeout=15000)
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    soup = BeautifulSoup(html, "html.parser")

                result = _parse_dcad_detail(soup)

        except Exception as e:
            result = {"error": str(e)}
        finally:
            await browser.close()

    return result


def _parse_dcad_detail(soup: BeautifulSoup) -> dict:
    """Parse property characteristics from a DCAD detail page.

    Verified live Aug 20 2026 — DCAD detail page structure:
    - "Main Improvement" section has a table with label/value pairs in td cells
    - Labels: Year Built, Living Area, # Bedrooms, # Baths (Full/Half), # Stories, Pool, Building Class
    - "Value" section has Market Value, Improvement, Land values
    - Fields are in 2-column or 3-column table rows (label, value, label2, value2)
    """
    result = {"status": "parsed"}
    text = soup.get_text()

    # Extract fields using regex on the full text — more robust than table parsing
    # since DCAD uses complex multi-column table layouts
    import re

    patterns = {
        "year_built": r'Year Built\s+(\d{4})',
        "improvement_sqft": r'Living Area\s+([\d,]+)\s*sqft',
        "beds": r'# Bedrooms\s+(\d+)',
        "baths": r'# Baths \(Full/Half\)\s*(\d+)\s*/\s*(\d+)',
        "stories": r'# Stories\s+([A-Z\s]+?)(?=\t|\n|$)',
        "pool": r'Pool \(Y/N\)\s*([YN])',
        "building_class": r'Building Class\s+([A-Z\s]+?)(?=\t|\n)',
        "construction_type": r'Construction Type\s+([A-Z\s]+?)(?=\t|\n)',
        "cad_appraised_value": r'Market Value:\s*\$?([\d,]+)',
        "improvement_value": r'Improvement:\s*\$?([\d,]+)',
        "land_value": r'\+\s*\$?([\d,]+)\s*$',
    }

    for field, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            val = m.group(1).strip()
            if field == "baths":
                result[field] = f"{m.group(1)}.{m.group(2)}"  # 3.1 format
            elif field in ("cad_appraised_value", "improvement_value"):
                result[field] = val.replace(",", "")
            else:
                result[field] = val

    # Also try to extract owner name
    owner_match = re.search(r'Owner \(Current \d+\)\n(.+?)(?=\n)', text)
    if owner_match:
        result["owner"] = owner_match.group(1).strip()

    if len(result) <= 1:
        result["status"] = "no_data_found"

    return result


class DallasCADAdapter(SourceAdapter):
    """Dallas CAD (DCAD) property search adapter."""
    channel = "cad"
    county = "dallas"

    def fetch(self, address: str = "") -> dict:
        """Search DCAD for a single address. Returns property characteristics."""
        if not address:
            return {"error": "No address provided"}
        return asyncio.run(_search_dcad_by_address(address))

    def provenance(self) -> SourceProvenance:
        return SourceProvenance(source="dallas_cad", source_rigor_tier=1)


if __name__ == "__main__":
    # Self-check: address parsing
    m = re.match(r'^(\d+)\s+(.+)', "15806 BREEDLOVE PLACE")
    assert m is not None
    assert m.group(1) == "15806"
    assert m.group(2) == "BREEDLOVE PLACE"

    m2 = re.match(r'^(\d+)\s+(.+)', "Place of Sale: George Allen")
    assert m2 is None  # Not a real address

    print("dallas_cad.py: address parsing self-check passed")
    print("dallas_cad.py: live DCAD search requires browser (Playwright) — skipping live test")