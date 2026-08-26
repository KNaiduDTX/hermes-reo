# tests/test_dashboard.py
"""Dashboard smoke tests — server.py + static UI, fully offline.

Uses starlette/fastapi TestClient against the real repo data files when they
exist; skips gracefully when a data file is absent. No markers: none of these
tests are live (pytest.ini already deselects `-m live and -m recalc`).
"""
from __future__ import annotations

import json
from pathlib import Path

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
