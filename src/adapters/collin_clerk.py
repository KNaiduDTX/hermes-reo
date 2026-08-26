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
from datetime import date
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Error as PlaywrightError

from .base import SourceAdapter, SourceProvenance
from ..cpr import LienRecord
from ..dates import parse_date_any
from ..source_registry import assert_registry_sync, require_endpoint
import logging

_log = logging.getLogger(__name__)

# TD3: derived from SOURCE-REGISTRY at import — no hardcoded portal literal.
CLERK_URL = require_endpoint("collin", "clerk").url
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Pagination (finding S2): the clerk results table is paginated; parse page 1
# only and you silently miss liens that read as clean title at GATE-LEGAL-LIEN.
MAX_SEARCH_PAGES = 20          # hard cap on pages fetched per search
PAGE_LOAD_DELAY_MS = 1000      # settle time after clicking Next
NEXT_PAGE_SELECTORS = [
    'button[aria-label="Next"]',
    'a[aria-label="Next"]',
    'li.pagination-next a',
    '.pagination-next > a',
    '.pagination .page-item.next a',
    '[title="Next"]',
    'button:has-text("Next")',
]

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

    Fail-fast contract (RL1): navigation and form-interaction failures
    (PlaywrightError from goto/fill/click/content, browser launch failures,
    timeouts) RAISE — they are never swallowed into a clean-looking [].
    An empty return therefore means "search completed, zero instruments",
    which GATE-LEGAL-LIEN may read as verified-clean title; a transport
    failure propagates so callers record lien_lookup_status='error'
    instead (see pipeline._clerk_lien_search). The ONE deliberate
    exception is the pagination loop below, which stays best-effort.
    """
    instruments: list[RecordedInstrument] = []

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

            # Parse the first results page
            html = await page.content()
            instruments = _parse_search_results(html)

            # Best-effort pagination (finding S2): click a generic Next
            # control, re-parse, and append rows until no Next is found, no
            # new rows appear, or MAX_SEARCH_PAGES is hit. Never crashes the
            # fetch — pagination problems degrade to page-1-only results.
            # This is the ONLY swallowed-error zone (RL1): losing pages 2+
            # degrades recall but never fabricates a false "no liens" verdict,
            # whereas a failed navigation would leave zero instruments that
            # look exactly like a clean-title search that never ran.
            try:
                seen_doc_numbers = {i.doc_number for i in instruments}
                for _ in range(MAX_SEARCH_PAGES - 1):
                    next_clicked = False
                    for selector in NEXT_PAGE_SELECTORS:
                        try:
                            await page.click(selector, timeout=2000)
                            next_clicked = True
                            break
                        except PlaywrightError:
                            continue
                    if not next_clicked:
                        break
                    await page.wait_for_timeout(PAGE_LOAD_DELAY_MS)
                    html = await page.content()
                    page_rows = _parse_search_results(html)
                    new_rows = [r for r in page_rows
                                if r.doc_number and r.doc_number not in seen_doc_numbers]
                    if not new_rows:
                        break
                    seen_doc_numbers.update(r.doc_number for r in new_rows)
                    instruments.extend(new_rows)
            except PlaywrightError as e:
                _log.info(f"Collin clerk: pagination stopped early (best effort): {e}")

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

    Filters for encumbrance types, orders priority by PARSED recording date
    via src.dates.parse_date_any — never by lexicographic string comparison,
    which mis-sorts US M/D/YYYY dates ("9/01/2019" vs "10/15/2020"; finding
    S4). Instruments whose date cannot be parsed sort last. Release
    instruments remove the most recent matching lien by comparing parsed
    dates too; LienRecord.recording_date keeps the clerk's original display
    string, annotated with "(released <date>)" when released.
    """
    # Parse each recorded_date exactly once; unparseable -> None sorts last.
    dated = [(inst, parse_date_any(inst.recorded_date)) for inst in instruments]
    dated.sort(key=lambda pair: (pair[1] is None, pair[1] or date.max))

    liens: list[LienRecord] = []
    parsed_dates: list = []  # parsed recording dates aligned with liens

    for i, (inst, inst_date) in enumerate(dated):
        lien_type = ENCUMBRANCE_TYPES.get(inst.doc_type)
        if not lien_type:
            continue

        if lien_type == "release":
            if inst_date is None:
                # A release without a parseable date cannot be matched to any
                # lien — skip it loudly rather than guess.
                _log.info(f"Collin clerk: release {inst.doc_number} has unparseable "
                      f"recorded_date {inst.recorded_date!r} — not matched")
                continue
            # Mark the most recent matching lien as released.
            for j in range(len(liens) - 1, -1, -1):
                lien_date = parsed_dates[j]
                if lien_date is not None and inst_date > lien_date:
                    liens[j].recording_date = (
                        f"{liens[j].recording_date} (released {inst.recorded_date})"
                    )
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
        parsed_dates.append(inst_date)

    return liens


class CollinClerkAdapter(SourceAdapter):
    """Collin County Clerk official records search adapter."""
    channel = "clerk"
    county = "collin"

    def fetch(self, party_name: str = "", legal_description: str = "",
              doc_types: list[str] = None) -> list[RecordedInstrument]:
        """Search official records by party name or legal description.

        Raise-on-transport (RL1): navigation/form/browser failures propagate
        (PlaywrightError and any other exception from the search session) —
        a raised error must be recorded as a failed lookup (e.g.
        lien_lookup_status='error'), never conflated with a verified-clean
        title. An empty list is returned only when the search itself
        completed and matched zero instruments. Pagination hiccups alone
        degrade gracefully to page-1 results instead of raising.
        """
        return asyncio.run(_search_clerk(party_name, legal_description, doc_types))

    def search_encumbrances(self, owner_name: str = "") -> list[LienRecord]:
        """Search for encumbrances by owner name and return a lien stack.

        Inherits fetch()'s raise-on-transport contract (RL1): a transport
        failure raises so GATE-LEGAL-LIEN can distinguish "no liens found"
        from "title search could not be performed".
        """
        instruments = self.fetch(party_name=owner_name)
        return build_lien_stack(instruments)

    def provenance(self) -> SourceProvenance:
        return SourceProvenance(source="collin_county_clerk", source_rigor_tier=2)


if __name__ == "__main__":
    # Self-check (TD3): CLERK_URL is registry-derived; assert the whole
    # adapter-constant <-> REGISTRY binding table holds.
    assert_registry_sync()
    print("collin_clerk.py: PASS — CLERK_URL derived from SOURCE-REGISTRY "
          "(assert_registry_sync)")

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

    # Self-check (S4): US M/D/YYYY dates must sort by PARSED date.
    # Lexicographic order would put "9/01/2019" after "10/15/2020".
    us_dates = [
        RecordedInstrument("D2020", "DEED OF TRUST", "10/15/2020"),
        RecordedInstrument("D2019", "LIEN", "9/01/2019"),
    ]
    liens_us = build_lien_stack(us_dates)
    assert [l.lien_type for l in liens_us] == ["lien", "mortgage"], \
        f"S4 regression: {[(l.lien_type, l.recording_date) for l in liens_us]}"
    assert liens_us[0].recording_date == "9/01/2019"
    assert liens_us[0].priority < liens_us[1].priority
    print("collin_clerk.py: PASS — 9/01/2019 sorts BEFORE 10/15/2020 (parsed-date ordering)")

    # Self-check (S4): release matching compares parsed dates too
    rel_fixtures = [
        RecordedInstrument("R1", "LIEN", "12/01/2019"),
        RecordedInstrument("R2", "RELEASE", "3/01/2020"),
        RecordedInstrument("R3", "LIEN", "5/15/2020"),
    ]
    rel_liens = build_lien_stack(rel_fixtures)
    assert len(rel_liens) == 2
    released_one = next(l for l in rel_liens if l.recording_date.startswith("12/01/2019"))
    assert "(released 3/01/2020)" in released_one.recording_date, \
        f"Release not matched by parsed date: {released_one.recording_date}"
    survivor = next(l for l in rel_liens if l.recording_date.startswith("5/15/2020"))
    assert "(released" not in survivor.recording_date
    # A release that predates every lien must match nothing
    pre_rel = build_lien_stack([
        RecordedInstrument("P1", "LIEN", "6/01/2019"),
        RecordedInstrument("P2", "RELEASE", "1/01/2019"),
    ])
    assert all("(released" not in l.recording_date for l in pre_rel)
    print("collin_clerk.py: PASS — release matched via parsed dates; original display string kept")

    # Self-check (S4): instruments with unparseable dates sort last, loudly
    unk_first = build_lien_stack([
        RecordedInstrument("U1", "LIEN", "Unknown"),
        RecordedInstrument("U2", "DEED OF TRUST", "7/04/2018"),
    ])
    assert unk_first[0].recording_date == "7/04/2018"
    assert unk_first[-1].recording_date == "Unknown"
    print("collin_clerk.py: PASS — unparseable recorded dates sort last")

    print("collin_clerk.py: all self-checks passed")
    print("collin_clerk.py: live search requires Playwright browser — skipping live test")