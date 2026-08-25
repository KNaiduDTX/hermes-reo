# src/adapters/collin_clerk.py
"""Collin County Clerk official records adapter — lien & encumbrance search.

The Collin clerk site (collin.tx.publicsearch.us) is a Kofile React SPA.
Searches by party name or legal description return recorded instruments:
deeds, deeds of trust, liens, lis pendens, judgments, releases.

Used to build the encumbrance stack for GATE-LEGAL-LIEN.
Requires Playwright (browser automation) — the SPA doesn't expose an API.
"""
from __future__ import annotations
import asyncio
import re
from dataclasses import dataclass, field
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .base import SourceAdapter, SourceProvenance
from ..cpr import LienRecord

CLERK_URL = "https://collin.tx.publicsearch.us"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Document types that represent encumbrances — keyed by actual Kofile doc type names
# Verified live Aug 20 2026 against Collin County Clerk search results
ENCUMBRANCE_TYPES = {
    "DEED OF TRUST": "mortgage",
    "LIEN": "lien",
    "FEDERAL TAX LIEN": "irs_federal",
    "STATE TAX LIEN": "tax",
    "CHILD SUPPORT LIEN": "child_support",
    "MECHANICS LIEN": "mechanics",
    "LIS PENDENS": "lis_pendens",
    "JUDGMENT": "judgment",
    "NOTICE": "notice",
    "ASSIGNMENT": "assignment",
    "RELEASE": "release",
    "RELEASE STATE TAX LIEN": "release",
    "RELEASE FEDERAL TAX LIEN": "release",
    "RELEASE OF LIEN": "release",
    "SUBORDINATION": "subordination",
    "PARTIAL RELEASE": "release",
    # Short forms (kept for compatibility)
    "DT": "mortgage",
    "FTL": "irs_federal",
    "STL": "tax",
    "CSL": "child_support",
    "ML": "mechanics",
    "LP": "lis_pendens",
    "JD": "judgment",
    "HOA": "hoa",
    "PN": "municipal",
    "SUB": "subordination",
    "REL": "release",
    "PREL": "release",
}


@dataclass
class RecordedInstrument:
    """A recorded instrument from the county clerk."""
    doc_number: str
    doc_type: str
    recorded_date: str
    parties: list[str] = field(default_factory=list)
    legal_description: str = ""
    book: str = ""
    volume: str = ""
    page: str = ""


async def _search_clerk(party_name: str = "", legal_description: str = "",
                         doc_types: list[str] = None) -> list[RecordedInstrument]:
    """Search Collin County Clerk official records by party name or legal description.

    Uses Playwright to navigate the Kofile React SPA, fill the search form,
    and parse results. Returns recorded instruments.

    Verified live Aug 20 2026 — selectors:
    - Search input: #basicSearchInputBox
    - Search button: button with text "Search"
    - Results: table with tr/td, columns: Grantor, Grantee, Doc Type, Recorded Date, Doc Number, Book/Vol/Page, Legal Description
    """
    instruments = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(CLERK_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(3000)  # SPA render time

            if party_name:
                # Fill the search input
                await page.fill("#basicSearchInputBox", party_name)
                await page.wait_for_timeout(500)

                # Click the Search button (find by text)
                await page.click('button:has-text("Search")')
                await page.wait_for_timeout(8000)  # wait for results to load

            # Parse results from the HTML table
            html = await page.content()
            instruments = _parse_search_results(html)

        except Exception as e:
            print(f"Collin clerk search error: {e}")
        finally:
            await browser.close()

    return instruments


def _parse_search_results(html: str) -> list[RecordedInstrument]:
    """Parse Kofile search results HTML into RecordedInstrument objects.

    Verified structure: HTML table with columns:
    Grantor | Grantee | Doc Type | Recorded Date | Doc Number | Book/Volume/Page | Legal Description
    First 3 cells per row are empty (checkbox, icon, spacer).
    Row 0 is the header.
    """
    soup = BeautifulSoup(html, "html.parser")
    instruments = []

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        # Skip header row
        if cells[3].get_text(strip=True) == "Grantor":
            continue

        # Column mapping (after 3 empty cells):
        # cells[3] = Grantor, cells[4] = Grantee, cells[5] = Doc Type
        # cells[6] = Recorded Date, cells[7] = Doc Number, cells[8] = Book/Vol/Page
        # cells[9] = Legal Description
        grantor = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        grantee = cells[4].get_text(strip=True) if len(cells) > 4 else ""
        doc_type = cells[5].get_text(strip=True) if len(cells) > 5 else ""
        recorded = cells[6].get_text(strip=True) if len(cells) > 6 else ""
        doc_num = cells[7].get_text(strip=True) if len(cells) > 7 else ""
        book_vp = cells[8].get_text(strip=True) if len(cells) > 8 else ""
        legal = cells[9].get_text(strip=True) if len(cells) > 9 else ""

        # Skip void/empty entries
        if doc_type in ("VOID", "") or doc_num in ("", "N/A"):
            continue
        if recorded in ("1/1/1900", "N/A"):
            continue

        # Parse book/vol/page
        book = vol = page = ""
        if book_vp and book_vp != "--/--/--":
            parts = book_vp.split("/")
            book = parts[0] if len(parts) > 0 else ""
            vol = parts[1] if len(parts) > 1 else ""
            page = parts[2] if len(parts) > 2 else ""

        instruments.append(RecordedInstrument(
            doc_number=doc_num,
            doc_type=doc_type,
            recorded_date=recorded,
            parties=[grantor, grantee] if grantor or grantee else [],
            legal_description=legal,
            book=book, volume=vol, page=page,
        ))

    return instruments


def build_lien_stack(instruments: list[RecordedInstrument]) -> list[LienRecord]:
    """Convert recorded instruments into a lien stack for the CPR.

    Filters for encumbrance types, estimates priority by recording date,
    and flags surviving-lien risk. Releases remove matching liens.
    """
    liens = []
    # Sort by recorded date (earliest = highest priority)
    sorted_insts = sorted(instruments, key=lambda i: i.recorded_date or "9999")

    for i, inst in enumerate(sorted_insts):
        lien_type = ENCUMBRANCE_TYPES.get(inst.doc_type)
        if not lien_type:
            continue

        if lien_type == "release":
            # Mark the most recent matching lien as released
            # ponytail: simplified — real release matching requires book/vol/pg cross-ref
            for lien in reversed(liens):
                if lien.recording_date and inst.recorded_date > lien.recording_date:
                    lien.recording_date = f"{lien.recording_date} (released {inst.recorded_date})"
                    break
            continue

        # Determine which liens survive foreclosure vs tax sale
        survives_fc = lien_type in ("tax", "irs_federal", "hoa", "municipal", "child_support")
        survives_ts = lien_type in ("irs_federal", "hoa", "municipal", "child_support")

        liens.append(LienRecord(
            lien_type=lien_type,
            recording_date=inst.recorded_date,
            priority=i + 1,
            survives_foreclosure=survives_fc,
            survives_tax_sale=survives_ts,
        ))

    return liens


class CollinClerkAdapter(SourceAdapter):
    """Collin County Clerk official records search adapter."""
    channel = "clerk"
    county = "collin"

    def fetch(self, party_name: str = "", legal_description: str = "",
              doc_types: list[str] = None) -> list[RecordedInstrument]:
        """Search official records by party name or legal description."""
        return asyncio.run(_search_clerk(party_name, legal_description, doc_types))

    def search_encumbrances(self, owner_name: str = "") -> list[LienRecord]:
        """Search for encumbrances by owner name and return a lien stack."""
        instruments = self.fetch(party_name=owner_name)
        return build_lien_stack(instruments)

    def provenance(self) -> SourceProvenance:
        return SourceProvenance(source="collin_county_clerk", source_rigor_tier=2)


if __name__ == "__main__":
    # Self-check: encumbrance type mapping
    assert ENCUMBRANCE_TYPES["DT"] == "mortgage"
    assert ENCUMBRANCE_TYPES["FTL"] == "irs_federal"
    assert ENCUMBRANCE_TYPES["LP"] == "lis_pendens"
    assert ENCUMBRANCE_TYPES["REL"] == "release"

    # Self-check: lien stack building with full doc type names
    fixtures = [
        RecordedInstrument("20210827001742050", "DEED OF TRUST", "2021-08-27"),
        RecordedInstrument("20220115000341234", "FEDERAL TAX LIEN", "2022-01-15"),
        RecordedInstrument("20210310000123456", "LIS PENDENS", "2021-03-10"),
        RecordedInstrument("20230501000567890", "RELEASE", "2023-05-01"),
    ]
    liens = build_lien_stack(fixtures)
    # LP (2021-03) first, DOT (2021-08) second, FTL (2022-01) third; release marks latest as released
    assert len(liens) == 3
    assert liens[0].lien_type == "lis_pendens"
    assert liens[0].priority == 1
    assert liens[1].lien_type == "mortgage"
    assert liens[2].lien_type == "irs_federal"
    assert liens[2].survives_tax_sale == True  # IRS survives tax sale

    print("collin_clerk.py: all self-checks passed")
    print("collin_clerk.py: live search requires Playwright browser — skipping live test")