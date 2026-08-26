r"""Offline pins for the Grayson County tax/sheriff-sale HTML parsers.

Covers src/adapters/grayson_tax.py exactly as shipped:

* ``_parse_tax_sale_page`` — the taxsearch "ssale" sheriff-list grid. A
  ``<tr>`` becomes a sale dict iff it carries >= 3 ``<td>`` cells (first
  three consumed positionally: address / sale_date / minimum_bid; extras
  dropped); ``<th>`` header rows and navigation/menu rows (< 3 td) are
  rejected. Every accepted row is tagged source="grayson_tax_sale_portal".
* ``_parse_county_tax_page`` — the county-site fallback. Anchors qualify
  iff their href matches /\.pdf|sale|sheriff/i (substring match, so even
  plain navigation INTO a "...sale..." page is captured by design);
  href-less anchors are rejected; relative hrefs are prefixed with
  COUNTY_MAIN (registry-derived, currently https://www.co.grayson.tx.us).
  Listing divs (class ~ sale|property|listing) qualify only when stripped
  text exceeds 20 chars and is truncated to the first 200.
* ``GraysonTaxAdapter.fetch`` -> NormalizedLead shapes, exercised offline
  with scripted fake Playwright pages (same pattern as
  tests/test_collin_clerk_failfast.py — no browser is launched).

Discovered-contract notes (documented instead of changing src, per the
work order):

1. Transport errors are NOT uniformly typed-and-raised. Navigation
   failures (page.goto raising playwright Error / OSError / timeout) on
   the dead TAX_SALE_URL (:8443, DNS NXDOMAIN) and on the county page are
   deliberately swallowed: the chain degrades portal -> county page ->
   clean [], so a dead endpoint alone never raises from fetch(). The
   typed playwright.async_api.Error DOES propagate out of fetch() from
   the unguarded lifecycle points — chromium.launch(), new_page(), and
   close() — because they sit outside the best-effort try blocks. Both
   halves are pinned below; only the launch/new_page half matches the
   "dead-endpoint fetch raises typed transport error" wording.
2. The portal fetcher additionally refuses HTML whose FIRST 200 chars
   contain "error" (case-insensitive) — such a document skips parsing
   entirely even if a valid sale table sits further down.
3. Lenient edge, noted but intentionally NOT enshrined as desirable: a
   row of three EMPTY <td>s still yields a sale dict with "" fields.
"""
from __future__ import annotations

import pytest
from playwright.async_api import Error as PlaywrightError

from src.adapters import grayson_tax
from src.adapters.base import NormalizedLead
from src.adapters.grayson_tax import (
    COUNTY_TAX_PAGE,
    SHERIFF_SALE_PAGE,
    TAX_SALE_URL,
    GraysonTaxAdapter,
    _parse_county_tax_page,
    _parse_tax_sale_page,
)


# ------------------------------------------------------------ HTML fixtures --
# Authored pages shaped like the real endpoints described in the module
# comments (taxsearch ssale grid; co.grayson.tx.us county pages).


def _row(*cells) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


SHERIFF_LIST_HEADER = (
    "<tr><th>Address</th><th>Sale Date</th>"
    "<th>Minimum Bid</th><th>Status</th></tr>"
)

SHERIFF_LIST_PAGE = f"""<html><head><title>Sales - ssale</title></head>
<body><div id="nav"><a href="home.html">Home</a></div>
<table id="salesGrid">
{SHERIFF_LIST_HEADER}
{_row('123 Main St, Sherman, TX 75090', '2026-09-01', '$5,000', 'Active')}
{_row('42 Oak Ridge Dr, Denison, TX 75020  ', '10/06/2026', '$100 + fees', 'Struck Off')}
{_row('7 Pecan Grove Ln, Van Alstyne, TX 75495', '10/06/2026', '$12,400', 'Active', 'BID-9')}
</table></body></html>"""

COUNTY_FALLBACK_PAGE = """<html><head><title>Grayson County</title></head>
<body><nav><a href="/page/home">Home</a>
<a href="/page/tax.sale">Tax Sale Information</a>
<a href="mailto:tax@co.grayson.tx.us">Email Us</a></nav>
<h2>Notices</h2>
<a href="/docs/gcso/Sheriff_Sale_List.pdf">Sheriff Sale List - current month</a>
<a href="https://dallas.texas.sheriffsaleauctions.com/auctions">Auction Portal</a>
<a>No-href sales anchor</a>
<div class="propertyListing">Struck-off: 7 Pecan Grove Ln, Van Alstyne TX -
bid deadline 5:00 PM at the courthouse steps, cash or cashier check.</div>
<div class="sale">short</div>
<div class="listing">&nbsp;</div>
</body></html>"""

PORTAL_ERROR_BANNER_PAGE = """<html><head><title>System Error</title></head>
<body><h1>Application error - contact the county clerk</h1>
<table>{}</table></body></html>""".format(
    _row('999 Ghost St, Denison, TX', '2026-11-04', '$1')
)

EMPTY_PAGE = "<html><body></body></html>"


# -------------------------------------------------- fake Playwright objects --
# Mirrors tests/test_collin_clerk_failfast.py: async surface only, scripted.


class FakePage:
    """Scripted stand-in for playwright.async_api Page.

    content_queue — documents returned by successive content() calls
                    (blank page once exhausted)
    goto_errors   — {url-substring: exception}; a goto whose URL contains
                    a listed substring raises it, others succeed
    """

    def __init__(self, content_queue=(), goto_errors=None):
        self.contents = list(content_queue)
        self.goto_errors = dict(goto_errors or {})
        self.gotos: list[str] = []
        self.closed = False

    async def goto(self, url, timeout=None):
        self.gotos.append(url)
        for fragment, err in self.goto_errors.items():
            if fragment in url:
                raise err
        return None

    async def wait_for_load_state(self, state=None, timeout=None):
        return None

    async def content(self):
        if not self.contents:
            return EMPTY_PAGE
        return self.contents.pop(0)


class FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page

    async def close(self):
        self._page.closed = True


class FakePlaywrightSession:
    def __init__(self, page):
        self._browser = FakeBrowser(page)

    class _Chromium:
        def __init__(self, browser):
            self._browser = browser

        async def launch(self, headless=True):
            return self._browser

    @property
    def chromium(self):
        return self._Chromium(self._browser)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def install_fake_page(monkeypatch, page: FakePage) -> None:
    monkeypatch.setattr(
        grayson_tax, "async_playwright",
        lambda: FakePlaywrightSession(page))


# ------------------------------------------- _parse_tax_sale_page (ssale grid)


def test_sheriff_list_table_rows_yield_positional_sale_dicts():
    props = _parse_tax_sale_page(SHERIFF_LIST_PAGE)
    assert len(props) == 3
    assert props[0] == {
        "address": "123 Main St, Sherman, TX 75090",
        "sale_date": "2026-09-01",
        "minimum_bid": "$5,000",
        "source": "grayson_tax_sale_portal",
    }
    # cells are stripped; dates pass through verbatim (no normalization)
    assert props[1]["address"] == "42 Oak Ridge Dr, Denison, TX 75020"
    assert props[1]["sale_date"] == "10/06/2026"
    assert props[1]["minimum_bid"] == "$100 + fees"


def test_rows_beyond_third_cell_are_dropped():
    html = "<table>" + _row("9 Elm St", "2026-09-01", "$1", "IGNORED") + "</table>"
    (prop,) = _parse_tax_sale_page(html)
    assert set(prop) == {"address", "sale_date", "minimum_bid", "source"}
    assert "IGNORED" not in prop.values()


def test_header_and_navigation_rows_are_rejected():
    th_header = "<tr><th>Address</th><th>Sale Date</th><th>Bid</th></tr>"
    one_td_menu = "<tr><td>Home</td></tr>"
    two_td_menu = "<tr><td>Services</td><td>Contact</td></tr>"
    html = "<table>" + th_header + one_td_menu + two_td_menu + "</table>"
    assert _parse_tax_sale_page(html) == []


def test_empty_and_markupless_input_yield_no_sales():
    assert _parse_tax_sale_page("") == []
    assert _parse_tax_sale_page(EMPTY_PAGE) == []


def test_whitespace_only_cells_still_pass_through_lenient_edge():
    # Documented lenient edge (see module docstring note 3): the >= 3 td
    # rule is positional only; empty cells are stripped to "" and kept.
    (prop,) = _parse_tax_sale_page("<table><tr><td></td><td></td><td></td></tr></table>")
    assert prop["address"] == ""
    assert prop["source"] == "grayson_tax_sale_portal"


# --------------------------------------- _parse_county_tax_page (county site)


def test_pdf_and_sheriff_links_captured_with_relative_urls_prefixed():
    props = _parse_county_tax_page(COUNTY_FALLBACK_PAGE)
    links = [p for p in props if "link_text" in p]
    by_text = {p["link_text"]: p["url"] for p in links}

    assert by_text["Sheriff Sale List - current month"] == \
        "https://www.co.grayson.tx.us/docs/gcso/Sheriff_Sale_List.pdf"
    # absolute http(s) hrefs pass through untouched
    assert by_text["Auction Portal"] == \
        "https://dallas.texas.sheriffsaleauctions.com/auctions"
    # navigation into a "...sale..." page matches the substring contract
    assert by_text["Tax Sale Information"] == \
        "https://www.co.grayson.tx.us/page/tax.sale"
    assert all(p["source"] == "grayson_county_page" for p in links)


def test_links_without_matching_or_missing_href_are_rejected():
    props = _parse_county_tax_page(COUNTY_FALLBACK_PAGE)
    texts = [p.get("link_text") for p in props if "link_text" in p]
    assert "Home" not in texts          # href lacks .pdf/sale/sheriff
    assert "Email Us" not in texts      # mailto: has no sale substring
    assert None not in texts            # href-less anchor skipped
    assert all(p["url"].startswith("http") for p in props if "url" in p)


def test_listing_divs_gated_on_length_and_truncated_to_200_chars():
    props = _parse_county_tax_page(COUNTY_FALLBACK_PAGE)
    divs = [p for p in props if "text" in p]
    assert len(divs) == 1
    assert divs[0]["text"].startswith("Struck-off: 7 Pecan Grove Ln")
    assert divs[0]["source"] == "grayson_county_page"

    long_text = "x" * 500
    html = f'<html><body><div class="propertyListing">{long_text}</div></body></html>'
    (prop,) = _parse_county_tax_page(html)
    assert prop["text"] == "x" * 200  # hard truncation at 200 chars


def test_short_listing_divs_rejected_and_empty_input_yields_none():
    assert _parse_county_tax_page('<div class="sale">short</div>') == []
    assert _parse_county_tax_page('<div class="listing">&nbsp;</div>') == []
    assert _parse_county_tax_page("") == []
    assert _parse_county_tax_page(EMPTY_PAGE) == []


# ------------------------------- adapter fetch -> NormalizedLead (fake pages)


def test_sheriff_list_rows_become_normalized_leads(monkeypatch):
    page = FakePage(content_queue=[SHERIFF_LIST_PAGE])
    install_fake_page(monkeypatch, page)

    leads = GraysonTaxAdapter().fetch()

    assert len(leads) == 3
    assert all(isinstance(lead, NormalizedLead) for lead in leads)
    lead = leads[0]
    assert lead.county == "grayson"
    assert lead.channel == "tax_sale"
    assert lead.parcel_id == ""  # unknown until cross-referenced with CAD
    assert lead.situs_address == "123 Main St, Sherman, TX 75090"
    assert lead.sale_date == "2026-09-01"
    assert lead.owner_name is None
    assert lead.raw_data["source"] == "grayson_tax_sale_portal"
    assert lead.raw_data["minimum_bid"] == "$5,000"
    # S10: empty parcel falls back to an addr-prefixed identity component
    assert lead.dedupe_key().startswith("grayson|addr:")
    assert lead.dedupe_key().endswith("|tax_sale|2026-09-01")
    assert leads[0].dedupe_key() != leads[2].dedupe_key()
    # portal succeeded — county fallback must never be contacted
    assert page.gotos == [TAX_SALE_URL]


def test_error_banner_in_first_200_chars_skips_portal_and_falls_back(
        monkeypatch):
    # Note 2: the banner page CONTAINS a parseable sale row, but the
    # fetcher refuses the whole document before the parser ever runs;
    # the leads must come exclusively from the county fallback page.
    page = FakePage(content_queue=[PORTAL_ERROR_BANNER_PAGE,
                                   COUNTY_FALLBACK_PAGE])
    install_fake_page(monkeypatch, page)

    leads = GraysonTaxAdapter().fetch()

    assert page.gotos == [TAX_SALE_URL, COUNTY_TAX_PAGE]
    # every lead comes exclusively from the county fallback page
    assert all(lead.raw_data.get("source") == "grayson_county_page"
               for lead in leads)
    assert [lead.raw_data.get("link_text") for lead in leads] == \
        ["Tax Sale Information",              # nav into "...sale..." page
         "Sheriff Sale List - current month",
         "Auction Portal",
         None]                                # 4th lead is the listing DIV
    lead = leads[1]
    assert isinstance(lead, NormalizedLead)
    assert lead.situs_address == "Sheriff Sale List - current month"
    assert leads[3].raw_data["text"].startswith("Struck-off: 7 Pecan Grove Ln")
    assert lead.county == "grayson" and lead.channel == "tax_sale"
    assert lead.sale_date is None  # county links carry no date column
    assert lead.raw_data["source"] == "grayson_county_page"


def test_dead_endpoint_navigation_failure_degrades_to_clean_empty_list(
        monkeypatch):
    # Note 1, swallow half: the :8443 portal is DNS NXDOMAIN and the
    # county page 403s curl — in transport terms both gotos fail, and
    # the shipped contract is degrade-to-[] rather than raise.
    page = FakePage(goto_errors={
        "taxsearch.co.grayson": PlaywrightError("net::ERR_NAME_NOT_RESOLVED"),
        "www.co.grayson.tx.us": PlaywrightError("net::ERR_ACCESS_DENIED"),
    })
    install_fake_page(monkeypatch, page)

    leads = GraysonTaxAdapter().fetch()
    assert leads == []
    assert page.gotos == [TAX_SALE_URL, COUNTY_TAX_PAGE]


def test_browser_launch_failure_raises_typed_transport_error(monkeypatch):
    # Note 1, raise half: launch() sits OUTSIDE the best-effort try, so
    # the typed playwright Error propagates out of the async helper and
    # out of adapter.fetch() — it can never masquerade as zero sales.
    class LaunchBoomSession(FakePlaywrightSession):
        class _Chromium(FakePlaywrightSession._Chromium):
            async def launch(self, headless=True):
                raise PlaywrightError("Executable doesn't exist")

    monkeypatch.setattr(grayson_tax, "async_playwright",
                        lambda: LaunchBoomSession(FakePage()))
    with pytest.raises(PlaywrightError):
        GraysonTaxAdapter().fetch()


def test_new_page_failure_propagates_typed_transport_error(monkeypatch):
    class PageBoomBrowser(FakeBrowser):
        async def new_page(self):
            raise PlaywrightError("Target closed")

    class Session(FakePlaywrightSession):
        def __init__(self, page):
            self._browser = PageBoomBrowser(page)

    monkeypatch.setattr(grayson_tax, "async_playwright",
                        lambda: Session(FakePage()))
    with pytest.raises(PlaywrightError):
        GraysonTaxAdapter().fetch()


# --------------------------------------------------------- adapter metadata --


def test_adapter_metadata_and_provenance_contract():
    adapter = GraysonTaxAdapter()
    assert adapter.channel == "tax_sale"
    assert adapter.county == "grayson"
    prov = adapter.provenance()
    assert prov.source == "grayson_tax_sale"
    assert prov.source_rigor_tier == 0
    # the documented endpoint constants stay anchored to Grayson County
    assert TAX_SALE_URL.startswith("https://taxsearch.co.grayson.tx.us")
    assert COUNTY_TAX_PAGE.startswith("https://www.co.grayson.tx.us/")
    assert SHERIFF_SALE_PAGE.startswith("https://www.co.grayson.tx.us/")
