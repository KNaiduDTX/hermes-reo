"""RL1: Collin clerk fail-fast contract tests (offline, fake Playwright).

Navigation / form-interaction / parse failures must PROPAGATE out of
_search_clerk and CollinClerkAdapter.fetch — never be swallowed into a
clean-looking []. The pagination loop is the ONE deliberate best-effort
zone: a PlaywrightError raised after page 1 has parsed degrades to
page-1-only results instead of raising.

No browser is launched: collin_clerk.async_playwright is replaced with a
scripted fake whose async surface mirrors the calls the adapter makes.
"""
from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError

from src.adapters import collin_clerk
from src.adapters.collin_clerk import (
    CLERK_URL,
    CollinClerkAdapter,
    RecordedInstrument,
    _parse_search_results,
    _search_clerk,
)


# ------------------------------------------------------------ HTML fixtures --

def _row(grantor="JOHN DOE", grantee="JANE ROE", doc_type="LIEN",
         recorded="9/01/2019", doc_num="D0001",
         book="2021/0827/0174", legal="LOT 12 ADDISON 2"):
    return (
        "<tr><td></td><td></td><td></td>"
        f"<td>{grantor}</td><td>{grantee}</td><td>{doc_type}</td>"
        f"<td>{recorded}</td><td>{doc_num}</td><td>{book}</td>"
        f"<td>{legal}</td></tr>"
    )


HEADER = ("<tr><td></td><td></td><td></td><td>Grantor</td><td>Grantee</td>"
          "<td>Doc Type</td><td>Recorded Date</td><td>Doc Number</td>"
          "<td>Book/Vol/Page</td><td>Legal Description</td></tr>")


def results_html(rows) -> str:
    body = "".join(rows)
    return f"<html><body><table>{HEADER}{body}</table></body></html>"


ONE_ROW = results_html([_row(doc_num="D0001")])
TWO_ROWS = results_html([_row(doc_num="D0001"),
                         _row(doc_type="DEED OF TRUST", doc_num="D0002",
                              recorded="10/15/2020")])


# ----------------------------------------------------------- Playwright fake --

class FakePage:
    """Scripted stand-in for playwright.async_api Page.

    content_queue   — HTML documents returned by successive content() calls
    goto_error      — exception for goto() to raise (transport failure)
    fill_error      — exception for the search-input fill() to raise
    click_errors    — {selector-substring: exception}; a click whose selector
                      contains a listed substring raises it; unlisted clicks
                      succeed and are recorded in self.clicks
    content_error   — exception for content() to raise
    """

    def __init__(self, content_queue=(), goto_error=None, fill_error=None,
                 click_errors=None, content_error=None):
        self.contents = list(content_queue)
        self.goto_error = goto_error
        self.fill_error = fill_error
        self.click_errors = dict(click_errors or {})
        self.content_error = content_error
        self.clicks: list[str] = []
        self.closed = False

    async def goto(self, url, timeout=None):
        if self.goto_error is not None:
            raise self.goto_error
        return None

    async def wait_for_load_state(self, state=None, timeout=None):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def fill(self, selector, text):
        if self.fill_error is not None:
            raise self.fill_error
        return None

    async def click(self, selector, timeout=None):
        for fragment, err in self.click_errors.items():
            if fragment in selector:
                raise err
        self.clicks.append(selector)
        return None

    async def content(self):
        if self.content_error is not None:
            raise self.content_error
        if not self.contents:
            return "<html><body></body></html>"
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
        collin_clerk, "async_playwright",
        lambda: FakePlaywrightSession(page))


# ------------------------------------------------------------------ RL1 -----

def test_navigation_failure_raises_not_empty_list(monkeypatch):
    page = FakePage(goto_error=PlaywrightError("net::ERR_CONNECTION_REFUSED"))
    install_fake_page(monkeypatch, page)
    with pytest.raises(PlaywrightError):
        asyncio.run(_search_clerk(party_name="DOE INDUSTRIES"))
    # nothing was parsed — the failure never masquerades as zero instruments


def test_form_fill_failure_raises(monkeypatch):
    page = FakePage(fill_error=PlaywrightError("selector #basicSearchInputBox gone"))
    install_fake_page(monkeypatch, page)
    with pytest.raises(PlaywrightError):
        asyncio.run(_search_clerk(party_name="DOE INDUSTRIES"))


def test_search_button_failure_raises(monkeypatch):
    page = FakePage(click_errors={"Search": PlaywrightError("button detached")})
    install_fake_page(monkeypatch, page)
    with pytest.raises(PlaywrightError):
        asyncio.run(_search_clerk(party_name="DOE INDUSTRIES"))


def test_content_fetch_failure_after_navigation_raises(monkeypatch):
    page = FakePage(content_error=PlaywrightError("Execution context destroyed"))
    install_fake_page(monkeypatch, page)
    with pytest.raises(PlaywrightError):
        asyncio.run(_search_clerk(party_name="DOE INDUSTRIES"))


def test_browser_launch_failure_raises(monkeypatch):
    class LaunchBoomSession(FakePlaywrightSession):
        class _Chromium(FakePlaywrightSession._Chromium):
            async def launch(self, headless=True):
                raise PlaywrightError("Executable doesn't exist")

    monkeypatch.setattr(collin_clerk, "async_playwright",
                        lambda: LaunchBoomSession(FakePage()))
    with pytest.raises(PlaywrightError):
        asyncio.run(_search_clerk(party_name="DOE"))


def test_adapter_fetch_propagates_transport_error(monkeypatch):
    page = FakePage(goto_error=PlaywrightError("timeout 30000ms exceeded"))
    install_fake_page(monkeypatch, page)
    adapter = CollinClerkAdapter()
    with pytest.raises(PlaywrightError):
        adapter.fetch(party_name="DOE INDUSTRIES")


def test_completed_zero_result_search_returns_empty_not_raise(monkeypatch):
    # The other half of the RL1 contract: an empty list means the search RAN
    # and matched zero instruments (header row only).
    page = FakePage(content_queue=[results_html([])])
    install_fake_page(monkeypatch, page)
    instruments = asyncio.run(_search_clerk(party_name="NOBODY KNOWN"))
    assert instruments == []


# ------------------------------------------------------- happy path/parsing --

def test_happy_path_returns_parsed_instruments(monkeypatch):
    page = FakePage(content_queue=[TWO_ROWS])
    install_fake_page(monkeypatch, page)
    instruments = asyncio.run(_search_clerk(party_name="DOE"))
    assert [i.doc_number for i in instruments] == ["D0001", "D0002"]
    first = instruments[0]
    assert isinstance(first, RecordedInstrument)
    assert first.doc_type == "LIEN"
    assert first.parties == ["JOHN DOE", "JANE ROE"]
    assert first.book == "2021" and first.volume == "0827" and first.page == "0174"


def test_pagination_click_miss_degrades_to_page_one(monkeypatch):
    # Every Next-selector click fails -> loop breaks cleanly on page 1.
    page = FakePage(content_queue=[TWO_ROWS])
    original_click = page.click

    async def fail_non_search_clicks(selector, timeout=None):
        if "Search" not in selector:  # i.e. every Next attempt
            raise PlaywrightError("no next control")
        return await original_click(selector, timeout=timeout)

    page.click = fail_non_search_clicks
    install_fake_page(monkeypatch, page)
    instruments = asyncio.run(_search_clerk(party_name="DOE"))
    assert [i.doc_number for i in instruments] == ["D0001", "D0002"]
    # only the Search button was ever clicked; Next attempts all failed
    assert page.clicks == ['button:has-text("Search")']


def test_pagination_playwright_error_is_swallowed_mid_loop(monkeypatch):
    # Page 2 parses fine; the click that would reach page 3 raises mid-loop.
    # RL1's single sanctioned swallow: keep pages 1-2, never raise.
    page1 = results_html([_row(doc_num="D0001")])
    page2 = results_html([
        _row(doc_num="D0002"),
        _row(doc_num="D0001"),  # duplicate across pages must be deduped
    ])
    state = {"next_clicks": 0}

    page = FakePage(content_queue=[page1, page2])
    original_click = page.click

    async def scripted_click(selector, timeout=None):
        if "Next" in selector or "pagination" in selector or 'has-text("Next")' in selector:
            state["next_clicks"] += 1
            if state["next_clicks"] > 1:
                raise PlaywrightError("results grid re-rendered under us")
        return await original_click(selector, timeout=timeout)

    page.click = scripted_click  # FakePage.click is plain attribute access
    install_fake_page(monkeypatch, page)

    instruments = asyncio.run(_search_clerk(party_name="DOE"))
    assert [i.doc_number for i in instruments] == ["D0001", "D0002"], \
        "pages 1-2 kept (deduped), no raise despite mid-loop PlaywrightError"


def test_non_playwright_parse_errors_still_raise(monkeypatch):
    # The best-effort carve-out covers ONLY transport errors: a bug in the
    # parser itself must stay loud even inside the pagination loop.
    page = FakePage(content_queue=[ONE_ROW])

    real_parse = collin_clerk._parse_search_results
    state = {"calls": 0}

    def flaky_parse(html):
        state["calls"] += 1
        if state["calls"] > 1:  # first call = page 1; later = pagination
            raise TypeError("'NoneType' object is not subscriptable")
        return real_parse(html)

    monkeypatch.setattr(collin_clerk, "_parse_search_results", flaky_parse)
    install_fake_page(monkeypatch, page)
    with pytest.raises(TypeError):
        asyncio.run(_search_clerk(party_name="DOE"))


# --------------------------------------------------------- parser contract --

def test_parser_skips_void_and_placeholder_rows():
    html = results_html([
        _row(doc_type="VOID", doc_num="V1"),
        _row(doc_num="N/A"),
        _row(recorded="1/1/1900"),
        _row(doc_num="OK1"),
    ])
    docs = [i.doc_number for i in _parse_search_results(html)]
    assert docs == ["OK1"]


def test_fetch_docstring_documents_raise_on_transport():
    # The contract lives in the public API surface too.
    assert "raise" in CollinClerkAdapter.fetch.__doc__.lower()
    assert "empty" in CollinClerkAdapter.fetch.__doc__.lower()
