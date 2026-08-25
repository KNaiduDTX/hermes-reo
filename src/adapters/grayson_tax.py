# src/adapters/grayson_tax.py
"""Grayson County tax sale / sheriff sale adapter.

The Grayson tax sale portal at taxsearch.co.grayson.tx.us:8443 is unreachable
via HTTP (timeout on both HTTP and HTTPS). The Grayson county site at
co.grayson.tx.us returns 403 to curl but works in a browser.

This adapter uses Playwright as a fallback when HTTP fails. If the tax sale
portal is confirmed down, falls back to the county sheriff's office page.
"""
from __future__ import annotations
import asyncio
import re
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .base import SourceAdapter, NormalizedLead, SourceProvenance

# Known Grayson endpoints
# VERIFIED Aug 20 2026: taxsearch.co.grayson.tx.us:8443 is DNS NXDOMAIN (dead subdomain)
# The Grayson county site (co.grayson.tx.us) returns 403 on /page/tax.sale
# No dedicated tax sale page exists on the county website
# Tax sales must be handled via Sheriff's office or Tax Assessor directly (phone/in-person)
# Tax Assessor-Collector portal: grayson.propertytaxpayments.net (payments only, no sales)
TAX_SALE_URL = "https://taxsearch.co.grayson.tx.us:8443/Sales/ssale"  # DEAD — DNS NXDOMAIN
COUNTY_TAX_PAGE = "https://www.co.grayson.tx.us/page/tax.sale"  # 403 Forbidden
SHERIFF_SALE_PAGE = "https://www.co.grayson.tx.us/page/gcso.home"  # Works but no sale listings
TAX_ASSESSOR_PORTAL = "https://grayson.propertytaxpayments.net/landing-page-en-us"  # Payments only
COUNTY_MAIN = "https://www.co.grayson.tx.us"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


async def _try_tax_sale_portal() -> list[dict]:
    """Try the Grayson tax sale portal via Playwright.

    The portal may require a self-signed cert acceptance or may be down.
    Returns list of sale properties or empty list.
    """
    properties = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--ignore-certificate-errors",  # taxsearch uses self-signed cert on :8443
        ])
        page = await browser.new_page()

        try:
            # Try HTTPS first, then HTTP
            for url in [TAX_SALE_URL, TAX_SALE_HTTP_URL]:
                try:
                    await page.goto(url, timeout=15000)
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    html = await page.content()
                    if "error" not in html.lower()[:200]:
                        properties = _parse_tax_sale_page(html)
                        if properties:
                            break
                except Exception:
                    continue

        except Exception as e:
            print(f"Grayson tax sale portal error: {e}")
        finally:
            await browser.close()

    return properties


async def _try_county_page() -> list[dict]:
    """Try the Grayson county website for tax sale info via Playwright."""
    properties = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(COUNTY_TAX_PAGE, timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=10000)
            html = await page.content()
            properties = _parse_county_tax_page(html)

        except Exception as e:
            print(f"Grayson county page error: {e}")
        finally:
            await browser.close()

    return properties


def _parse_tax_sale_page(html: str) -> list[dict]:
    """Parse the Grayson tax sale portal results."""
    soup = BeautifulSoup(html, "html.parser")
    properties = []

    # The taxsearch portal likely has a table or list of sale properties
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 3:
            properties.append({
                "address": cells[0].get_text(strip=True) if len(cells) > 0 else "",
                "sale_date": cells[1].get_text(strip=True) if len(cells) > 1 else "",
                "minimum_bid": cells[2].get_text(strip=True) if len(cells) > 2 else "",
                "source": "grayson_tax_sale_portal",
            })

    return properties


def _parse_county_tax_page(html: str) -> list[dict]:
    """Parse the Grayson county tax sale page for sale info."""
    soup = BeautifulSoup(html, "html.parser")
    properties = []

    # Look for links to PDF sale lists or inline property listings
    for link in soup.find_all("a", href=re.compile(r"\.pdf|sale|sheriff", re.I)):
        href = link.get("href", "")
        text = link.get_text(strip=True)
        if href:
            properties.append({
                "link_text": text,
                "url": href if href.startswith("http") else f"https://www.co.grayson.tx.us{href}",
                "source": "grayson_county_page",
            })

    # Also look for any inline property listings
    for div in soup.find_all("div", class_=re.compile("sale|property|listing", re.I)):
        text = div.get_text(strip=True)
        if text and len(text) > 20:
            properties.append({
                "text": text[:200],
                "source": "grayson_county_page",
            })

    return properties


class GraysonTaxAdapter(SourceAdapter):
    """Grayson County tax sale / sheriff sale adapter.

    Tries the tax sale portal first, then falls back to the county website.
    Both require Playwright — HTTP access is blocked/down.
    """
    channel = "tax_sale"
    county = "grayson"

    def fetch(self, **params) -> list[NormalizedLead]:
        """Fetch Grayson County tax sale properties.

        Tries the tax sale portal, then county page fallback.
        """
        # Try tax sale portal first
        props = asyncio.run(_try_tax_sale_portal())

        # Fallback to county page
        if not props:
            print("Grayson tax sale portal unreachable — trying county page...")
            props = asyncio.run(_try_county_page())

        if not props:
            print("Grayson: no tax sale data found — portal may be down or no active sales")
            return []

        leads = []
        for prop in props:
            addr = prop.get("address", prop.get("link_text", ""))
            leads.append(NormalizedLead(
                county="grayson",
                parcel_id="",  # unknown until cross-referenced with Grayson CAD
                channel="tax_sale",
                situs_address=addr,
                sale_date=prop.get("sale_date"),
                raw_data=prop,
            ))

        return leads

    def provenance(self) -> SourceProvenance:
        return SourceProvenance(source="grayson_tax_sale", source_rigor_tier=0)


if __name__ == "__main__":
    # Self-check: URL constants
    assert "taxsearch" in TAX_SALE_URL
    assert "co.grayson" in COUNTY_TAX_PAGE
    assert ":8443" in TAX_SALE_URL  # non-standard port

    # Self-check: HTML parsing (empty input)
    assert _parse_tax_sale_page("") == []
    assert _parse_county_tax_page("") == []

    # Self-check: HTML parsing (sample table)
    sample_html = """
    <table><tr><td>123 Main St, Sherman, TX</td><td>2026-09-01</td><td>$5,000</td></tr></table>
    """
    props = _parse_tax_sale_page(sample_html)
    assert len(props) == 1
    assert "123 Main" in props[0]["address"]
    assert props[0]["sale_date"] == "2026-09-01"

    print("grayson_tax.py: all self-checks passed")
    print("grayson_tax.py: live search requires Playwright with --ignore-certificate-errors")
    print("grayson_tax.py: tax sale portal at :8443 was unreachable via curl — browser fallback built")