# tests/test_dashboard_contract.py
"""FN11 phase 1 — dashboard frontend/backend wiring contract (fully offline).

static/index.html no longer carries inline dashboard logic: it declares its
API surface as ``data-endpoint-*`` / ``data-action-*`` hooks on ``<body>`` and
loads ``static/app.js`` deferred. This module pins the three-way contract
between

1. the hook URLs declared by index.html,
2. the ``/api/...`` fetch literals implemented by app.js, and
3. the routes registered on ``server.app``,

and verifies that every payload key the render functions consume actually
appears on live responses. Dataset-backed probes use TestClient against the
real repo files with fixture-less skip semantics (skipped when
data/current_deals.json is absent — CI has no scraped dataset); endpoint
shapes that need fixtures are exercised through a tmp sandbox exactly like
tests/test_dashboard.py. No markers besides ``needs_dataset``: nothing here is
live (pytest addopts already deselect ``-m live``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = REPO_ROOT / "static" / "app.js"
INDEX_PATH = REPO_ROOT / "static" / "index.html"
DEALS_PATH = REPO_ROOT / "data" / "current_deals.json"

client = TestClient(server.app)


# ===========================================================================
# Source parsing helpers
# ===========================================================================
FETCH_LITERAL_RE = re.compile(r"""(['"])(/api/[^'"\s]*)\1""")
COMMENT_LINE_RE = re.compile(r"^\s*(?:/\*|\*|//)")
SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL)
SCRIPT_OPEN_RE = re.compile(r"<script\b[^>]*>")
HOOK_ATTR_RE = re.compile(
    r"""(data-(?:endpoint|action)-[\w-]+)\s*=\s*["']([^"']+)["']"""
)

# The shell's declared API contract — must stay in lockstep with the fetch
# literals in app.js (both directions) and with server.app's route table.
DECLARED_HOOKS = {
    "data-endpoint-summary": "/api/summary",
    "data-endpoint-deals": "/api/deals",
    "data-endpoint-geojson": "/api/geojson",
    "data-endpoint-stats": "/api/stats",
    "data-endpoint-cpr": "/api/cpr/",
    "data-endpoint-memo": "/api/memo/",
    "data-endpoint-market": "/api/market?groupby=",
    "data-action-generate-offer": "/api/generate-offer",
    "data-action-clerk-search": "/api/clerk-search",
}


def _app_js() -> str:
    return APP_JS_PATH.read_text(encoding="utf-8")


def _index_html() -> str:
    return INDEX_PATH.read_text(encoding="utf-8")


def _fetch_literals(js: str) -> list:
    """Distinct ``(url, is_post)`` fetch literals in app.js, comments ignored."""
    code = "\n".join(
        line for line in js.splitlines() if not COMMENT_LINE_RE.match(line)
    )
    out = []
    for m in FETCH_LITERAL_RE.finditer(code):
        url = m.group(2)
        window = code[max(0, m.start() - 120): m.end() + 120]
        is_post = re.search(r"method\s*:\s*['\"]POST['\"]", window) is not None
        if (url, is_post) not in out:
            out.append((url, is_post))
    return out


def _route_table() -> dict:
    """Map each registered route's static prefix -> allowed HTTP methods."""
    table = {}
    for route in server.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if isinstance(path, str) and methods:
            table[path.split("{", 1)[0]] = frozenset(methods)
    return table


def _normalize(url: str) -> str:
    """Strip query string and trailing slash so prefixes compare cleanly."""
    return url.split("?", 1)[0].rstrip("/")


# ===========================================================================
# 1. Route contract — every app.js fetch resolves to a real server route
# ===========================================================================
def test_every_app_js_fetch_literal_is_a_registered_route():
    literals = _fetch_literals(_app_js())
    assert len(literals) >= len(DECLARED_HOOKS), (
        "app.js stopped fetching the documented API surface: "
        f"{sorted(u for u, _ in literals)}")
    table = _route_table()
    for url, is_post in literals:
        hits = [ms for prefix, ms in table.items()
                if _normalize(prefix) == _normalize(url)]
        assert hits, f"app.js fetches {url!r} — no such route on server.app"
        allowed = set().union(*(set(m) for m in hits))
        verb = "POST" if is_post else "GET"
        assert verb in allowed, (
            f"app.js calls {url!r} via {verb} but the route allows {sorted(allowed)}")


def test_ui_consumes_every_api_route_the_server_exposes():
    """No silent server-side route drift: UI literals == exposed /api routes."""
    exposed = {_normalize(p) for p in _route_table() if p.startswith("/api/")}
    consumed = {_normalize(u) for u, _ in _fetch_literals(_app_js())}
    assert consumed == exposed


# ===========================================================================
# 2. Hook contract — index.html declarations stay in lockstep with app.js
# ===========================================================================
def test_index_html_declares_the_documented_endpoint_hooks():
    found = dict(HOOK_ATTR_RE.findall(_index_html()))
    assert set(found) == set(DECLARED_HOOKS), (
        f"hook drift — expected {sorted(DECLARED_HOOKS)}, got {sorted(found)}")
    for attr, url in DECLARED_HOOKS.items():
        assert found[attr] == url, f"hook {attr} must keep declaring {url!r}"


def test_hooks_and_app_js_literals_stay_in_lockstep():
    hook_urls = {url for _, url in HOOK_ATTR_RE.findall(_index_html())}
    literal_urls = {u for u, _ in _fetch_literals(_app_js())}
    assert {_normalize(u) for u in hook_urls} == {_normalize(u) for u in literal_urls}, (
        "index.html data-* hooks and app.js fetch literals disagree")


# ===========================================================================
# 3. Shell contract — app.js is referenced, served, and nothing is inline
# ===========================================================================
def test_index_html_references_app_js():
    html = _index_html()
    assert '<script src="app.js" defer></script>' in html


def test_served_shell_references_app_js_and_keeps_hooks():
    html = client.get("/").text
    assert '<script src="app.js" defer></script>' in html
    for attr, url in DECLARED_HOOKS.items():
        assert f'{attr}="{url}"' in html, f"served shell lost hook {attr}"


def test_no_inline_dashboard_logic_remains_in_shell():
    """Every <script> tag loads via src= with an empty body (tiny bootstrap)."""
    html = client.get("/").text
    tags = SCRIPT_TAG_RE.findall(html)
    assert len(tags) == len(SCRIPT_OPEN_RE.findall(html)) >= 2  # leaflet + app.js
    for tag in tags:
        assert "src=" in tag.split(">")[0], f"inline script block remains: {tag[:60]}"
        assert tag.split(">", 1)[1].rsplit("</script>", 1)[0].strip() == ""


def test_static_mount_serves_app_js_as_javascript():
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"].lower()
    assert "fetchJSON" in resp.text          # the moved engine really is there


# ===========================================================================
# 4. Payload-key contracts — documented keys vs live responses
#
# Each tuple below is what the named render function actually reads. The
# drift-guard pins keep these tuples honest: remove a key from app.js and the
# pin fails; rename it on the server and the live probe fails.
# ===========================================================================
SUMMARY_KEYS = ("total", "counties", "with_sqft", "with_debt", "geocoded",
                "geocoded_source")

DEAL_KEYS = (  # COLUMNS[] + aggregate()/filteredDeals()/populateFilterOptions()
    "address", "county", "city", "owner", "parcel_id", "sqft", "cad_value",
    "mortgage_debt", "lender", "ltv_pct", "equity_pct", "preliminary_mao",
    "lien_gate", "val_gate", "blast_tier", "hitl_required",
)

MARKET_KEYS = ("groupby", "groups")
MARKET_ROW_KEYS = {"group", "deals", "median_cad", "total_debt",
                   "positive_equity_share", "upcoming_sales"}

STATS_KEYS = ("report", "enriched", "skipped")

GEOJSON_PROPS = ("address", "title", "city", "county", "category",
                 "marker-color", "appraised_value", "price_per_sqft",
                 "deal_type", "blast_tier")

CPR_CORE_KEYS = ("parcel_id", "county", "situs_address", "channel", "column",
                 "blast_tier", "lien_lookup_status", "owner_name",
                 "cad_appraised_value", "liens", "ledger")
VALUATION_KEYS = ("arv", "comp_set", "comp_source", "gate_val_verdict",
                  "gate_val_reason")

MEMO_KEYS = ("parcel_id", "content")

OFFER_KEYS = ("dead", "terms", "letter", "parties")   # note/block_reason optional
TERMS_KEYS = ("offer_price", "earnest_money", "option_fee",
              "option_period_days", "closing_timeline_days", "is_cash",
              "blocked")

CLERK_KEYS = ("party_name", "instrument_count", "encumbrance_count",
              "preview", "preview_truncated", "politeness")
CLERK_PREVIEW_KEYS = ("doc_number", "doc_type", "recorded_date", "parties",
                      "legal_description")

# (needle, why) — every documented key must still be read somewhere in app.js.
DOCUMENTED_NEEDLES = [
    ("geocoded_source", "summary header"),
    ("preliminary_mao", "deals COLUMNS"),
    ("val_gate", "deals COLUMNS"),
    ("hitl_required", "aggregate() gate snapshot"),
    ("positive_equity_share", "market rows"),
    ("upcoming_sales", "market rows"),
    ("median_cad", "market rows"),
    ("geocoded", "overview KPI"),
    ("appraised_value", "map featureOf()"),
    ("marker-color", "map featureOf()"),
    ("gate_val_verdict", "renderDetail valuation box"),
    ("comp_set", "renderDetail comp count"),
    ("survives_foreclosure", "renderDetail lien stack"),
    ("survives_tax_sale", "renderDetail lien stack"),
    ("entry_hash", "renderDetail provenance ledger"),
    ("opening_bid", "renderDetail owner/distress table"),
    ("option_period_days", "runGenerateOffer term chips"),
    ("is_cash", "runGenerateOffer financing chip"),
    ("instrument_count", "runClerkSearch summary line"),
    ("preview_truncated", "runClerkSearch preview cap"),
]


@pytest.mark.parametrize("needle,why", DOCUMENTED_NEEDLES,
                         ids=[n for n, _ in DOCUMENTED_NEEDLES])
def test_documented_keys_are_actually_read_by_app_js(needle, why):
    assert needle in _app_js(), (
        f"{why}: key {needle!r} left the renderer — update the contract tuples "
        "in this module together with app.js")


@pytest.fixture(autouse=True)
def _require_dataset(request):
    """Dataset-backed probes skip without data/; CI has no scraped dataset."""
    if request.node.get_closest_marker("needs_dataset") and not DEALS_PATH.exists():
        pytest.skip("data/current_deals.json not present on this environment")


# ------------------------------------------------- live: summary + deals ---
@pytest.mark.needs_dataset
def test_summary_payload_carries_documented_keys_live():
    payload = client.get("/api/summary").json()
    for key in SUMMARY_KEYS:
        assert key in payload, f"/api/summary missing documented key {key!r}"
    assert isinstance(payload["counties"], dict)
    assert isinstance(payload["geocoded_source"], str)


@pytest.mark.needs_dataset
def test_deal_rows_carry_every_key_the_table_renders_live():
    deals = client.get("/api/deals").json()
    assert isinstance(deals, list)
    if not deals:
        pytest.skip("current_deals.json holds zero rows")
    union = set()
    for deal in deals:
        if isinstance(deal, dict):
            union |= set(deal)
    missing = [key for key in DEAL_KEYS if key not in union]
    assert not missing, f"dataset lost columns the deals table renders: {missing}"


# --------------------------------------------- live: market/stats/geojson ---
@pytest.mark.needs_dataset
def test_market_payload_shape_matches_renderer_live():
    payload = client.get("/api/market?groupby=county").json()
    for key in MARKET_KEYS:
        assert key in payload, f"/api/market missing documented key {key!r}"
    assert payload["groups"], "expected ≥1 county group from local dataset"
    for row in payload["groups"]:
        assert set(row) == MARKET_ROW_KEYS


@pytest.mark.needs_dataset
def test_stats_payload_carries_documented_keys_live():
    payload = client.get("/api/stats").json()
    for key in STATS_KEYS:
        assert key in payload, f"/api/stats missing documented key {key!r}"
    assert payload["report"].strip()


@pytest.mark.needs_dataset
def test_geojson_features_carry_properties_featureOf_reads_live():
    gj_path = REPO_ROOT / "data" / "live_foreclosures.geojson"
    if not gj_path.exists():
        pytest.skip("data/live_foreclosures.geojson not present")
    payload = client.get("/api/geojson").json()
    assert payload.get("type") == "FeatureCollection"
    union = set()
    for feature in payload.get("features", []):
        union |= set((feature.get("properties") or {}))
    missing = [key for key in GEOJSON_PROPS if key not in union]
    assert not missing, f"geojson lost properties the map renders: {missing}"


# ------------------------------------------------------- sandboxed shapes ---
def _synth_cpr(parcel_id: str = "SYNTHC", **over) -> dict:
    """Minimal CPR shaped like renderDetail expects (see test_dashboard.py)."""
    provenance = {"source": "test-fixture", "confidence": 1.0}
    cpr = {
        "parcel_id": parcel_id,
        "county": "collin",
        "situs_address": f"7 Contract Ave, Plano, TX 75023 ({parcel_id})",
        "channel": "DT-AUCTION",
        "column": "PRICED",
        "blast_tier": 1,
        "lien_lookup_status": "ok",
        "owner_name": {"value": "Doe Jane", **provenance},
        "cad_appraised_value": {"value": 410000, **provenance},
        "liens": [{"lien_type": "mortgage", "amount": 150000,
                   "recording_date": "01/15/2020", "lender": "Test Bank NA",
                   "priority": 1, "survives_foreclosure": False,
                   "survives_tax_sale": False}],
        "ledger": [{"field_name": "cad_appraised_value", "old_value": None,
                    "new_value": 410000, "source": "test-fixture",
                    "timestamp": "2026-01-01T00:00:00Z", "prev_hash": "",
                    "note": "", "entry_hash": "abc123"}],
        "valuation": {
            "arv": {"value": 455000, "source": "test-fixture", "confidence": 0.8},
            "comp_set": [], "comp_source": "hitl",
            "gate_val_verdict": "pass", "gate_val_reason": "",
        },
    }
    cpr.update(over)
    return cpr


@pytest.fixture
def sandbox_data(tmp_path, monkeypatch):
    """Redirect every repo-path module global into a tmp sandbox."""
    data_dir = tmp_path / "data"
    (data_dir / "fixtures").mkdir(parents=True)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "DRAFTS_DIR", tmp_path / "drafts")
    monkeypatch.setattr(server, "DEALS_PATH", data_dir / "current_deals.json")
    monkeypatch.setattr(server, "GEOJSON_PATH", data_dir / "live_foreclosures.geojson")
    return SimpleNamespace(data_dir=data_dir, fixtures=data_dir / "fixtures")


def test_cpr_payload_carries_keys_renderDetail_reads(sandbox_data):
    (sandbox_data.fixtures / "fixture_cpr_SYNTHC.json").write_text(
        json.dumps(_synth_cpr()), encoding="utf-8")
    resp = client.get("/api/cpr/SYNTHC")
    assert resp.status_code == 200
    body = resp.json()
    for key in CPR_CORE_KEYS:
        assert key in body, f"CPR snapshot missing documented key {key!r}"
    for key in VALUATION_KEYS:
        assert key in body["valuation"], f"cpr.valuation missing key {key!r}"
    assert isinstance(body["liens"], list) and isinstance(body["ledger"], list)


def test_memo_payload_carries_documented_keys(sandbox_data):
    (sandbox_data.fixtures / "fixture_deal_memo_CONTRACT.md").write_text(
        "# Deal memo\n\n- ARV recorded by a human\n", encoding="utf-8")
    body = client.get("/api/memo/CONTRACT").json()
    for key in MEMO_KEYS:
        assert key in body, f"/api/memo missing documented key {key!r}"


def test_generate_offer_payload_carries_terms_the_chips_render(sandbox_data):
    (sandbox_data.data_dir / "cpr_SYNTHC.json").write_text(
        json.dumps(_synth_cpr()), encoding="utf-8")
    resp = client.post("/api/generate-offer", json={"parcel_id": "SYNTHC"})
    assert resp.status_code == 200
    body = resp.json()
    for key in OFFER_KEYS:
        assert key in body, f"generate-offer missing documented key {key!r}"
    if not body["dead"]:                       # dead pencils omit economics
        for key in TERMS_KEYS:
            assert key in body["terms"], f"offer terms missing key {key!r}"


def test_clerk_search_payload_carries_documented_keys(monkeypatch):
    cc = pytest.importorskip(
        "src.adapters.collin_clerk",
        reason="collin_clerk pulls optional Playwright/bs4 deps")

    def fake_fetch(self, party_name="", legal_description="", **kw):
        return [cc.RecordedInstrument(
            doc_number="2020-0001", doc_type="DEED OF TRUST",
            recorded_date="01/15/2020", parties=["Doe Jane", "Test Bank NA"],
            legal_description="LOT 12 BLK 3", book="2100", volume="", page="115")]

    monkeypatch.setattr(cc.CollinClerkAdapter, "fetch", fake_fetch)
    body = client.post("/api/clerk-search", json={"party_name": "Doe Jane"}).json()
    for key in CLERK_KEYS:
        assert key in body, f"clerk-search missing documented key {key!r}"
    for key in CLERK_PREVIEW_KEYS:
        assert key in body["preview"][0], f"clerk preview missing key {key!r}"


# ------------------------------------------- live: real repo CPR snapshot ---
@pytest.mark.needs_dataset
def test_repo_cpr_snapshot_matches_renderDetail_contract():
    served = REPO_ROOT / "data" / "cpr_4003592.json"
    fixture = REPO_ROOT / "data" / "fixtures" / "fixture_cpr_4003592.json"
    if not served.exists():
        served = fixture
    if not served.exists():
        pytest.skip("no local CPR snapshot for parcel 4003592")
    resp = client.get("/api/cpr/4003592")
    assert resp.status_code == 200
    body = resp.json()
    for key in CPR_CORE_KEYS:
        assert key in body, f"shipped CPR missing documented key {key!r}"
    for key in VALUATION_KEYS:
        assert key in (body.get("valuation") or {}), (
            f"shipped CPR valuation missing key {key!r}")
