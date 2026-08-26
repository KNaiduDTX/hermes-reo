# tests/test_dashboard.py
"""Dashboard smoke tests — server.py + static UI, fully offline.

Uses starlette/fastapi TestClient against the real repo data files when they
exist; skips gracefully when a data file is absent. No markers: none of these
tests are live (pytest.ini already deselects `-m live and -m recalc`).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server

REPO_ROOT = Path(__file__).resolve().parents[1]
DEALS_PATH = REPO_ROOT / "data" / "current_deals.json"
DATA_PRESENT = DEALS_PATH.exists()

GEOJSON_PATH = REPO_ROOT / "data" / "live_foreclosures.geojson"

client = TestClient(server.app)


def _read_json_file(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- static UI --
def test_index_serves_html():
    """GET / serves the dashboard shell as HTML."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text.lower()
    assert "<!doctype html" in body or "<html" in body
    assert "hermes" in body                      # brand header present
    assert "/api/deals" in resp.text             # UI is wired to the JSON APIs


def test_static_dir_mounted():
    """Files under static/ are served via the /static mount."""
    resp = client.get("/static/index.html")
    assert resp.status_code == 200


# ----------------------------------------------------------------- summary ---
@pytest.mark.needs_dataset
def test_api_summary_shape():
    """/api/summary returns the documented small KPI object."""
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    payload = resp.json()
    for key in ("total", "counties", "with_sqft", "with_debt", "geocoded"):
        assert key in payload, f"missing summary key {key}"
    assert isinstance(payload["total"], int)
    assert isinstance(payload["counties"], dict)
    for key in ("with_sqft", "with_debt", "geocoded"):
        assert isinstance(payload[key], int)
    assert payload["geocoded"] <= payload["total"] or payload["total"] == 0


@pytest.mark.needs_dataset
def test_api_summary_total_matches_file():
    if not DEALS_PATH.exists():
        pytest.skip("data/current_deals.json not present")
    deals = _read_json_file(DEALS_PATH)
    payload = client.get("/api/summary").json()
    assert payload["total"] == len(deals)
    expected_counties: dict[str, int] = {}
    for deal in deals:
        county = str(deal.get("county") or "").strip().lower() or "unknown"
        expected_counties[county] = expected_counties.get(county, 0) + 1
    assert payload["counties"] == expected_counties


# ------------------------------------------------------------------- deals ---
@pytest.mark.needs_dataset
def test_api_deals_matches_file():
    if not DEALS_PATH.exists():
        pytest.skip("data/current_deals.json not present")
    resp = client.get("/api/deals")
    assert resp.status_code == 200
    deals = resp.json()
    on_disk = _read_json_file(DEALS_PATH)
    assert isinstance(deals, list)
    assert len(deals) == len(on_disk)            # fresh read, whole file, no trimming
    if deals:
        assert deals[0].get("parcel_id") == on_disk[0].get("parcel_id")


@pytest.mark.needs_dataset
def test_api_geojson_feature_collection():
    if not GEOJSON_PATH.exists():
        pytest.skip("data/live_foreclosures.geojson not present")
    resp = client.get("/api/geojson")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload.get("type") == "FeatureCollection"
    on_disk = _read_json_file(GEOJSON_PATH)
    assert len(payload.get("features", [])) == len(on_disk.get("features", []))


# ------------------------------------------------------------------- stats ---
@pytest.mark.needs_dataset
def test_api_stats_renders_report():
    """/api/stats renders markdown via portfolio_stats over enriched CPRs."""
    if not DEALS_PATH.exists():
        pytest.skip("data/current_deals.json not present")
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    payload = resp.json()
    report = payload.get("report")
    assert isinstance(report, str) and report.strip()
    assert "PORTFOLIO STATISTICAL SUMMARY" in report          # render_stats_report header
    stats = payload.get("stats")
    assert isinstance(stats, dict)
    assert stats["total_properties"] + payload.get("skipped", 0) == len(_read_json_file(DEALS_PATH))


# ------------------------------------------------------- failure degradation --
def test_missing_data_file_returns_503(tmp_path, monkeypatch):
    """A missing backing file must yield a clear 503, never a crash."""
    monkeypatch.setattr(server, "DEALS_PATH", tmp_path / "missing.json")
    resp = client.get("/api/deals")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "data_unavailable"
    assert "missing.json" in body["detail"]
    assert client.get("/api/summary").status_code == 503
    assert client.get("/api/stats").status_code == 503


def test_malformed_data_file_returns_503(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(server, "DEALS_PATH", bad)
    resp = client.get("/api/deals")
    assert resp.status_code == 503
    assert "unreadable" in resp.json()["detail"]


@pytest.fixture(autouse=True)
def _require_dataset(request):
    """Data-backed endpoints 503 without data/; CI has no scraped dataset."""
    if request.node.get_closest_marker("needs_dataset") and not DATA_PRESENT:
        pytest.skip("data/current_deals.json not present on this environment")


# ===========================================================================
# F3/F4/F11 — deal detail, draft actions, market aggregates.
#
# Everything below is offline and deterministic: repo-path module globals are
# redirected into a tmp sandbox (data/ is gitignored, so CI has no dataset),
# the clerk adapter is monkeypatched (never a live Chromium run), and only
# explicitly-marked needs_dataset probes touch the real shipped files.
# ===========================================================================

def _write_json_file(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _synth_cpr(parcel_id: str = "SYNTH1", **over) -> dict:
    """A minimal CPR dict that src.hitl.load_cpr rehydrates cleanly."""
    cpr = {
        "parcel_id": parcel_id,
        "county": "collin",
        "situs_address": f"1 Synthetic Way, Plano, TX 75023 ({parcel_id})",
        "owner_name": {"value": "Doe John & Jane", "source": "test-fixture",
                       "confidence": 1.0},
        "cad_appraised_value": {"value": 400000, "source": "test-fixture",
                                "confidence": 0.9},
        "deal_type": "DT-AUCTION",
        "blast_tier": 2,
        "column": "PRICED",
        "lien_lookup_status": "ok",
        "liens": [{
            "lien_type": "mortgage", "amount": 150000,
            "recording_date": "01/15/2020", "lender": "Test Bank NA",
            "priority": 1, "survives_foreclosure": False,
            "survives_tax_sale": False,
        }],
        "ledger": [{
            "field_name": "cad_appraised_value", "old_value": None,
            "new_value": 400000, "source": "test-fixture",
            "timestamp": "2026-01-01T00:00:00Z", "prev_hash": "",
            "note": "", "entry_hash": "abc123",
        }],
    }
    cpr.update(over)
    return cpr


@pytest.fixture
def sandbox_data(tmp_path, monkeypatch):
    """Redirect every repo-path module global into a tmp sandbox."""
    data_dir = tmp_path / "data"
    (data_dir / "fixtures").mkdir(parents=True)
    drafts_dir = tmp_path / "drafts"
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "DRAFTS_DIR", drafts_dir)
    monkeypatch.setattr(server, "DEALS_PATH", data_dir / "current_deals.json")
    monkeypatch.setattr(server, "GEOJSON_PATH", data_dir / "live_foreclosures.geojson")
    return SimpleNamespace(data_dir=data_dir, fixtures=data_dir / "fixtures",
                           drafts=drafts_dir)


# ------------------------------------------------------- F3: GET /api/cpr --
def test_cpr_detail_serves_fixture_file(sandbox_data):
    """Fixture lookup path answers 200 with the saved CPR passthrough."""
    _write_json_file(sandbox_data.fixtures / "fixture_cpr_SYNTH9.json",
                     _synth_cpr("SYNTH9"))
    resp = client.get("/api/cpr/SYNTH9")
    assert resp.status_code == 200
    body = resp.json()
    assert body["parcel_id"] == "SYNTH9"
    # Provenance-wrapped fields pass through verbatim; the UI unwraps them.
    assert body["owner_name"]["value"] == "Doe John & Jane"
    assert body["owner_name"]["source"] == "test-fixture"


def test_cpr_detail_prefers_production_over_fixture(sandbox_data):
    _write_json_file(sandbox_data.data_dir / "cpr_BOTH1.json",
                     _synth_cpr("BOTH1", county="dallas"))
    _write_json_file(sandbox_data.fixtures / "fixture_cpr_BOTH1.json",
                     _synth_cpr("BOTH1", county="grayson"))
    assert client.get("/api/cpr/BOTH1").json()["county"] == "dallas"


@pytest.mark.needs_dataset
def test_cpr_detail_repo_fixture_parcel():
    """The shipped parcel snapshot serves when the local dataset exists."""
    have = ((REPO_ROOT / "data" / "cpr_4003592.json").exists()
            or (REPO_ROOT / "data" / "fixtures" / "fixture_cpr_4003592.json").exists())
    if not have:
        pytest.skip("no local CPR snapshot for parcel 4003592")
    resp = client.get("/api/cpr/4003592")
    assert resp.status_code == 200
    assert str(resp.json().get("parcel_id")) == "4003592"


def test_cpr_detail_missing_returns_404_json(sandbox_data):
    resp = client.get("/api/cpr/NO_SUCH_PARCEL_X9")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "cpr_not_found"
    assert "NO_SUCH_PARCEL_X9" in body["detail"]
    # Both lookup candidates were named for the operator.
    assert "cpr_NO_SUCH_PARCEL_X9.json" in body["detail"]


@pytest.mark.parametrize("bad", ["a..b", ".hidden", "-lead", "9" * 65])
def test_cpr_detail_malformed_parcel_is_400(sandbox_data, bad):
    resp = client.get(f"/api/cpr/{bad}")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_parcel_id"


def test_cpr_traversal_shapes_rejected_before_fs_access():
    """Traversal-prone ids die in validation — never reach the filesystem."""
    for bad in ("../etc/passwd", "a/b", "..", "c:\\windows"):
        try:
            server._validated_parcel(bad)
        except server.ApiError as exc:
            assert exc.status_code == 400
            assert exc.error == "invalid_parcel_id"
        else:  # pragma: no cover - guards regressions of PARCEL_ID_RE
            raise AssertionError(f"traversal id {bad!r} was not rejected")


# ------------------------------------------------------ F3: GET /api/memo --
def test_memo_serves_fixture_and_missing_is_404(sandbox_data):
    (sandbox_data.fixtures / "fixture_deal_memo_MEMO1.md").write_text(
        "# Deal memo\n\n- ARV basis recorded by human\n", encoding="utf-8")
    resp = client.get("/api/memo/MEMO1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["parcel_id"] == "MEMO1"
    assert "# Deal memo" in body["content"]
    miss = client.get("/api/memo/MEMO_MISSING")
    assert miss.status_code == 404
    assert miss.json()["error"] == "memo_not_found"


def test_memo_unreadable_is_503_safe(sandbox_data):
    """A directory squatting where the .md belongs degrades to a clean 503."""
    (sandbox_data.data_dir / "deal_memo_BROKEN.md").mkdir()
    resp = client.get("/api/memo/BROKEN")
    assert resp.status_code == 503
    assert resp.json()["error"] == "data_unavailable"


def test_memo_malformed_parcel_is_400(sandbox_data):
    assert client.get("/api/memo/bad..id").status_code == 400


@pytest.mark.needs_dataset
def test_memo_repo_fixture_parcel():
    memo = REPO_ROOT / "data" / "deal_memo_4003592.md"
    fixture = REPO_ROOT / "data" / "fixtures" / "fixture_deal_memo_4003592.md"
    if not (memo.exists() or fixture.exists()):
        pytest.skip("no local deal memo for parcel 4003592")
    resp = client.get("/api/memo/4003592")
    assert resp.status_code == 200
    assert resp.json()["content"].strip()


# ------------------------------------------- F4: POST /api/generate-offer --
def test_generate_offer_writes_draft_and_letter_contains_parties(sandbox_data):
    """200 → terms + letter naming the counterparties + atomic draft on disk."""
    _write_json_file(sandbox_data.data_dir / "cpr_SYNTH1.json", _synth_cpr("SYNTH1"))
    resp = client.post("/api/generate-offer", json={"parcel_id": "SYNTH1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dead"] is False
    assert body["parcel_id"] == "SYNTH1"

    terms = body["terms"]
    for key in ("offer_price", "earnest_money", "option_fee",
                "option_period_days", "closing_timeline_days", "is_cash",
                "seller_posture", "blocked"):
        assert key in terms, f"missing term {key}"
    assert terms["offer_price"] > 0

    letter = body["letter"]
    assert letter.strip()
    assert "Doe John & Jane" in letter       # owner of record named
    assert "Test Bank NA" in letter          # lienholder named
    assert [(p["role"], p["name"]) for p in body["parties"]] == [
        ("owner_of_record", "Doe John & Jane"),
        ("lienholders", "Test Bank NA"),
    ]

    draft = sandbox_data.drafts / "draft_offer_SYNTH1.txt"
    assert draft.exists()                    # staged under data/drafts/
    assert draft.read_text(encoding="utf-8") == letter   # staged == returned


def test_generate_offer_input_validation(sandbox_data):
    _write_json_file(sandbox_data.data_dir / "cpr_SYNTH1.json", _synth_cpr("SYNTH1"))
    missing_body = client.post("/api/generate-offer", json={})
    assert missing_body.status_code == 400
    assert missing_body.json()["error"] == "invalid_request"
    assert client.post("/api/generate-offer",
                       json={"parcel_id": 123}).status_code == 400
    assert client.post("/api/generate-offer",
                       json={"parcel_id": "a..b"}).status_code == 400
    ghost = client.post("/api/generate-offer", json={"parcel_id": "GHOST_PARCEL"})
    assert ghost.status_code == 404
    assert ghost.json()["error"] == "cpr_not_found"


def test_generate_offer_unpriceable_rejected_without_fabricating(sandbox_data):
    """No pricing basis → clean 400; never invented economics, never a draft."""
    _write_json_file(
        sandbox_data.data_dir / "cpr_NOVAL.json",
        _synth_cpr("NOVAL",
                   cad_appraised_value={"value": 0, "source": "t", "confidence": 1.0}))
    resp = client.post("/api/generate-offer", json={"parcel_id": "NOVAL"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unpriceable_deal"
    assert list(sandbox_data.drafts.glob("*")) == []


def test_generate_offer_dead_pencil_writes_nothing(sandbox_data):
    """Bid ceiling ≤ 0 → 200 dead:true with no letter and no staged file."""
    underwater = {"lien_type": "irs_federal", "amount": 9_000_000,
                  "recording_date": "05/05/2019", "lender": "IRS",
                  "priority": 1, "survives_foreclosure": True,
                  "survives_tax_sale": True}
    _write_json_file(sandbox_data.data_dir / "cpr_DEADPEN.json",
                     _synth_cpr("DEADPEN", liens=[underwater]))
    resp = client.post("/api/generate-offer", json={"parcel_id": "DEADPEN"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dead"] is True
    assert body["dead_reason"] == "bid_ceiling_nonpositive"
    assert body["letter"] == ""
    assert list(sandbox_data.drafts.glob("*")) == []   # $0 offers are fake signals


@pytest.mark.needs_dataset
def test_generate_offer_repo_fixture_integration(tmp_path, monkeypatch):
    """End-to-end over the real hitl.load_cpr path when local data exists."""
    served = REPO_ROOT / "data" / "cpr_4003592.json"
    if not served.exists():
        served = REPO_ROOT / "data" / "fixtures" / "fixture_cpr_4003592.json"
    if not served.exists():
        pytest.skip("no local CPR snapshot for parcel 4003592")

    drafts = tmp_path / "drafts"
    monkeypatch.setattr(server, "DRAFTS_DIR", drafts)
    raw = _read_json_file(served)
    owner = (raw.get("owner_name") or {}).get("value")

    resp = client.post("/api/generate-offer", json={"parcel_id": "4003592"})
    assert resp.status_code == 200
    body = resp.json()
    if body["dead"]:
        assert body["letter"] == ""
        assert not any(drafts.glob("*"))
    else:
        assert body["letter"].strip()
        if owner:
            assert str(owner).strip() in body["letter"]   # parties block composed
        staged = drafts / "draft_offer_4003592.txt"
        assert staged.exists()
        assert staged.read_text(encoding="utf-8") == body["letter"]


# ---------------------------------------------- F4: POST /api/clerk-search --
def _clerk_module_and_instruments():
    cc = pytest.importorskip(
        "src.adapters.collin_clerk",
        reason="collin_clerk pulls optional Playwright/bs4 deps")
    instruments = [
        cc.RecordedInstrument(
            doc_number="2020-0001", doc_type="DEED OF TRUST",
            recorded_date="01/15/2020", parties=["Doe John", "Test Bank NA"],
            legal_description="LOT 12 BLK 3", book="2100", volume="", page="115"),
        cc.RecordedInstrument(
            doc_number="2021-0099", doc_type="JUDGMENT",
            recorded_date="06/01/2021", parties=["CreditCo", "Doe John"],
            legal_description="", book="", volume="", page=""),
    ]
    return cc, instruments


def test_clerk_search_mocked_adapter(monkeypatch):
    """Adapter fetch is faked at class level — no live Chromium anywhere."""
    cc, instruments = _clerk_module_and_instruments()
    seen: dict = {}

    def fake_fetch(self, party_name="", legal_description="", **kw):
        seen["party_name"] = party_name
        return instruments

    monkeypatch.setattr(cc.CollinClerkAdapter, "fetch", fake_fetch)
    resp = client.post("/api/clerk-search", json={"party_name": "  Doe John  "})
    assert resp.status_code == 200
    body = resp.json()
    assert seen["party_name"] == "Doe John"      # trimmed name reaches the adapter
    assert body["party_name"] == "Doe John"
    assert body["instrument_count"] == 2
    assert body["encumbrance_count"] == 2        # deed of trust + judgment liens
    assert len(body["preview"]) <= server.CLERK_PREVIEW_LIMIT
    assert body["preview"][0]["doc_number"] == "2020-0001"
    assert body["preview"][0]["parties"] == ["Doe John", "Test Bank NA"]
    assert body["politeness"].strip()            # politeness posture documented
    assert body["preview_truncated"] is False


def test_clerk_search_empty_or_oversized_party_is_400():
    assert client.post("/api/clerk-search", json={}).status_code == 400
    assert client.post("/api/clerk-search",
                       json={"party_name": ""}).status_code == 400
    assert client.post("/api/clerk-search",
                       json={"party_name": "   "}).status_code == 400
    long = client.post(
        "/api/clerk-search",
        json={"party_name": "x" * (server.CLERK_MAX_PARTY_LEN + 1)})
    assert long.status_code == 400


def test_clerk_search_transport_failure_is_503(monkeypatch):
    cc, _ = _clerk_module_and_instruments()

    def boom(self, party_name="", legal_description="", **kw):
        raise RuntimeError("chromium died")

    monkeypatch.setattr(cc.CollinClerkAdapter, "fetch", boom)
    resp = client.post("/api/clerk-search", json={"party_name": "Anyone"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "clerk_unavailable"
    assert "RuntimeError" in body["detail"]


# ---------------------------------------------------- F11: GET /api/market --
MARKET_ROW_KEYS = {"group", "deals", "median_cad", "total_debt",
                   "positive_equity_share", "upcoming_sales"}

TODAY = date.today().isoformat()   # sale today counts as upcoming


@pytest.fixture
def market_setup(sandbox_data):
    rows = [
        {"parcel_id": "p1", "address": "1 A St, Frisco, TX 75035",
         "city": "Frisco", "isd": "Frisco ISD", "county": "collin",
         "cad_value": 100000, "mortgage_debt": 50000,
         "equity_estimate": 50000, "sale_date": "2099-01-15"},
        {"parcel_id": "p2", "address": "2 B St, Frisco, TX 75035-1234",
         "city": "Frisco", "isd": "Frisco ISD", "county": "collin",
         "cad_value": 300000, "mortgage_debt": 100000,
         "equity_estimate": -1000, "sale_date": TODAY},
        {"parcel_id": "p3", "address": "3 C St, Plano, TX 75023",
         "city": "Plano", "isd": "Plano ISD", "county": "collin",
         "cad_value": "not-a-number", "mortgage_debt": None,
         "equity_estimate": None, "sale_date": "garbage"},
        {"parcel_id": "p4", "address": "no zip here",
         "city": None, "isd": None, "county": "",
         "cad_value": 200000, "mortgage_debt": 10,
         "equity_estimate": 5, "sale_date": "2020-01-01"},
    ]
    deals_path = sandbox_data.data_dir / "current_deals.json"
    _write_json_file(deals_path, rows)
    return rows


def _groups(groupby: str) -> dict:
    resp = client.get(f"/api/market?groupby={groupby}")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["groupby"] == groupby
    return {row["group"]: row for row in payload["groups"]}


def test_market_groupby_zip(market_setup):
    groups = _groups("zip")
    assert set(groups["75035"]) == MARKET_ROW_KEYS
    assert groups["75035"]["deals"] == 2                 # ZIP+4 tail also matches
    assert groups["75035"]["median_cad"] == pytest.approx(200000.0)
    assert groups["75035"]["total_debt"] == pytest.approx(150000.0)
    assert groups["75035"]["positive_equity_share"] == pytest.approx(0.5)
    assert groups["75035"]["upcoming_sales"] == 2        # future date + today
    assert groups["75023"]["deals"] == 1
    assert groups["75023"]["median_cad"] is None         # unparseable cad skipped
    assert groups["75023"]["total_debt"] is None         # None ≠ a true $0 sum
    assert groups["75023"]["positive_equity_share"] is None
    assert groups["75023"]["upcoming_sales"] == 0        # garbage never counts
    assert groups["unknown"]["deals"] == 1               # address without ZIP tail
    assert groups["unknown"]["median_cad"] == pytest.approx(200000.0)


def test_market_groupby_city(market_setup):
    groups = _groups("city")
    assert set(groups["Frisco"]) == MARKET_ROW_KEYS
    assert groups["Frisco"]["deals"] == 2
    assert groups["Plano"]["deals"] == 1
    assert groups["Plano"]["median_cad"] is None
    assert groups["unknown"]["deals"] == 1               # null city buckets together


def test_market_groupby_isd(market_setup):
    groups = _groups("isd")
    assert set(groups["Frisco ISD"]) == MARKET_ROW_KEYS
    assert groups["Frisco ISD"]["deals"] == 2
    assert groups["Frisco ISD"]["upcoming_sales"] == 2
    assert groups["Plano ISD"]["deals"] == 1
    assert groups["unknown"]["deals"] == 1


def test_market_groupby_county(market_setup):
    groups = _groups("county")
    assert set(groups["collin"]) == MARKET_ROW_KEYS
    assert groups["collin"]["deals"] == 3
    assert sum(g["deals"] for g in groups.values()) == 4


def test_market_groupby_case_insensitive(market_setup):
    assert client.get("/api/market?groupby=ZIP").status_code == 200


@pytest.mark.parametrize("bad", ["bogus", "", "zip+4"])
def test_market_unknown_groupby_is_400(bad):
    resp = client.get(f"/api/market?groupby={bad}")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_groupby"
    assert "zip" in body["detail"] and "county" in body["detail"]


def test_market_absent_groupby_param_is_400():
    resp = client.get("/api/market")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_groupby"


def test_market_missing_deals_file_is_503(sandbox_data):
    resp = client.get("/api/market?groupby=zip")
    assert resp.status_code == 503
    assert resp.json()["error"] == "data_unavailable"


@pytest.mark.needs_dataset
def test_market_real_dataset_county_rows_sum_to_total():
    if not DEALS_PATH.exists():
        pytest.skip("data/current_deals.json not present")
    payload = client.get("/api/market?groupby=county").json()
    total = sum(row["deals"] for row in payload["groups"])
    assert total == len(_read_json_file(DEALS_PATH))
    for row in payload["groups"]:
        assert set(row) == MARKET_ROW_KEYS


# --------------------------------------------- frontend wiring contract ----
def test_index_html_wires_detail_actions_and_market():
    """The shell must reference every F3/F4/F11 endpoint it consumes.

    Cheap contract check against silent field/route renames (FN11 spirit):
    the vanilla-JS UI has no build step, so a rename breaks rendering quietly.
    """
    html = client.get("/").text
    for needle in (
        'id="view-detail"',            # fifth view exists
        'id="view-market"',            # market view exists
        'id="mktGroupby"',             # groupby selector wired to /api/market
        "/api/market?groupby=",        # market fetch
        "/api/cpr/",                   # detail snapshot fetch
        "/api/memo/",                  # embedded memo fetch
        "/api/generate-offer",         # POST action
        "/api/clerk-search",           # POST action
        "openDetail(",                 # row-click handler
        "data-parcel=",                # row carries its parcel id
        "Provenance Ledger",           # ledger section rendered
    ):
        assert needle in html, f"static/index.html missing {needle!r}"
