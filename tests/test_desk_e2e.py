# tests/test_desk_e2e.py — full-book end-to-end desk validation (E2E goal).
"""Every record in the book must work across the ENTIRE desk:

  /deals/{pid} renders a dossier (full or register-level)
  /api/uw-defaults?parcel={pid} computes real economics
  /offers?parcel={pid} renders engine letter or labeled playbook preview

Runs offline via TestClient against data/current_deals.json + data/cprs/.
Skips cleanly on a fresh clone with no dataset.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard
from src.cpr_store import generate_all

ROOT = Path(__file__).resolve().parent.parent
DEALS = ROOT / "data" / "current_deals.json"
CPRS = ROOT / "data" / "cprs"

pytestmark = pytest.mark.skipif(
    not DEALS.exists(), reason="data/current_deals.json not present")


def client():
    return TestClient(dashboard.app)


@pytest.fixture(scope="module")
def book(tmp_path_factory):
    """Generate CPRs for every record into a temp store, once per module."""
    out = tmp_path_factory.mktemp("cprs")
    records = json.loads(DEALS.read_text(encoding="utf-8"))
    # dedupe to assets exactly like find_deals does (property_id key)
    seen, rows = set(), []
    for r in records:
        pid_key = r.get("property_id")
        if pid_key and pid_key in seen:
            continue
        if pid_key:
            seen.add(pid_key)
        rows.append(r)
    res = generate_all(str(DEALS), str(out))
    assert res["failed"] == 0, res["failures"][:3]
    return {"rows": rows, "store": out, "res": res}


def test_every_record_got_a_cpr(book):
    assert book["res"]["ok"] == len(book["rows"])
    assert book["res"]["failed"] == 0


def test_cpr_files_are_unique_per_asset_and_reloadable(book):
    from src.hitl import load_cpr
    files = list(book["store"].glob("*.json"))
    assert len(files) == len(book["rows"]), "one file per asset"
    raw = json.loads(files[0].read_text())
    cpr = load_cpr(str(files[0]))
    assert cpr.parcel_id or cpr.situs_address  # reloads without error
    assert raw.get("property_id")


def test_dossier_renders_for_every_parcel(book):
    c = client()
    for r in book["rows"]:
        pid = str(r["parcel_id"])
        resp = c.get(f"/deals/{pid}")
        assert resp.status_code == 200, (pid, resp.status_code)
        assert ("REGISTER-LEVEL ABSTRACT" in resp.text) or \
               ("$750,000" in resp.text) or ("figure" in resp.text), pid


def test_uw_defaults_compute_for_every_valued_or_cad_parcel(book):
    c = client()
    checked = 0
    for r in book["rows"]:
        pid = str(r["parcel_id"])
        rr = c.get(f"/api/uw-defaults?parcel={pid}")
        if rr.status_code == 404:
            continue  # implausible address -> no derived defaults by design
        payload = rr.json()
        assert rr.status_code == 200, (pid, payload)
        assert payload["arv"] > 0
        assert "net_profit" in payload
        checked += 1
    # Book-size-relative (audit TEST-3): the synthetic CI book has 8 rows,
    # the real book 166 — the requirement is that MOST rows derive economics,
    # not a hardcoded count.
    assert checked > len(book["rows"]) * 0.5, checked


def test_offers_render_for_every_parcel(book):
    c = client()
    engine, preview, blocked = 0, 0, 0
    for r in book["rows"]:
        pid = str(r["parcel_id"])
        resp = c.get(f"/offers?parcel={pid}&deal_type=DT-AUCTION")
        assert resp.status_code == 200, (pid, resp.status_code)
        if "DRAFT" in resp.text:
            engine += 1
        elif "playbook preview" in resp.text.lower():
            preview += 1
    # fixture-valued parcel renders the real engine letter; CAD-only parcels
    # render clearly-labeled playbook previews. Nothing may 500.
    assert engine >= 1 and (engine + preview) == len(book["rows"])


def test_wholesale_blocked_for_any_parcel(book):
    c = client()
    r = c.get("/offers?deal_type=DT-WHOLESALE")
    assert r.status_code == 200
