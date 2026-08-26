"""Desk smoke suite (audit UI-06): the 7 desk pages + /lab mount had ZERO
tests — system POST buttons, /map, /underwriting, /pipeline, /api/parcels and
the unified-SPA mount were invisible to CI. Offline, fast, hermetic."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard

ROOT = Path(__file__).resolve().parent.parent
DEALS = ROOT / "data" / "current_deals.json"

client = TestClient(dashboard.app)


@pytest.mark.parametrize("path", [
    "/", "/deals", "/map", "/underwriting", "/offers", "/pipeline",
])
def test_desk_pages_render(path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)


def test_static_assets_served():
    assert client.get("/static/app-pipeline.js").status_code == 200


def test_parcels_endpoint_shape():
    r = client.get("/api/parcels")
    assert r.status_code == 200
    assert "parcels" in r.json()


def test_valued_parcels_endpoint():
    r = client.get("/api/valued-parcels")
    assert r.status_code == 200
    assert "parcels" in r.json()


@pytest.mark.skipif(not DEALS.exists(),
                    reason="data/current_deals.json not present")
def test_dossier_renders_for_first_parcel():
    first = str(DEALS.read_text(encoding="utf-8")[:1])  # cheap existence probe
    assert first  # file readable
    import json
    rows = json.loads(DEALS.read_text(encoding="utf-8"))
    pid = str(rows[0].get("parcel_id"))
    assert client.get(f"/deals/{pid}").status_code == 200


@pytest.mark.skipif(not DEALS.exists(),
                    reason="data/current_deals.json not present")
def test_unified_spa_served_under_lab():
    """UI-01/UI-09: one process serves the legacy SPA under /lab."""
    r = client.get("/lab/api/deals")
    assert r.status_code == 200, r.status_code
    assert isinstance(r.json(), list)
    assert client.get("/lab/api/summary").status_code == 200


def test_clerk_search_proxied_under_lab_route():
    """The dossier clerk button posts to /lab/api/clerk-search; a malformed
    payload must get the SPA's clean 400 validation — proving the route is
    mounted and reachable without touching the live site (no 404)."""
    r = client.post("/lab/api/clerk-search", json={"party_name": "   "})
    assert r.status_code == 400, r.status_code
    assert r.json()["error"] == "invalid_request"
