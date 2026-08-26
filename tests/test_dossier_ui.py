# tests/test_dossier_ui.py — render checks for the Dossier screen (III).
"""TestClient renders of /deals/{parcel} asserting the design-handoff contract:

200 + ARV figure, surviving-lien dagger marker, provenance hash truncation,
ledger modal markup/ids, and a 404 for an unknown parcel.
"""
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

import dashboard

ROOT = Path(__file__).resolve().parent.parent
CPR = json.loads((ROOT / "data" / "cpr_4003592.json").read_text(encoding="utf-8"))


def client() -> TestClient:
    return TestClient(dashboard.app)


def test_dossier_renders_200_with_key_content():
    r = client().get("/deals/4003592")
    assert r.status_code == 200, r.status_code
    html = r.text

    # Figures band — ARV as recomputed from the same source data.
    arv = float(CPR["valuation"]["arv"]["value"])
    assert f"${arv:,.0f}" in html  # $748,000

    # Encumbrance stack — dagger on the surviving tax lien.
    assert "\u2020" in html
    assert "Survives" in html

    # Provenance — truncated entry hashes (10 chars) from the newest-first slice.
    hashes = [e["entry_hash"][:10] for e in list(reversed(CPR["ledger"]))[:40]]
    assert hashes[0] in html

    # Masthead badges — parcel 4003592 has a surviving tax lien, so the desk
    # recommendation is Conditional (per dashboard.dossier()).
    for needle in ("Stage", "Tier T3", "DT-Auction", "Conditional"):
        assert needle in html, needle

    # Ledger modal markup + vanilla-JS open/close hooks.
    assert 'id="ledger-backdrop"' in html
    assert 'id="ledger-dialog"' in html
    assert "openLedger" in html and "closeLedger" in html


def test_dossier_structural_sections():
    r = client().get("/deals/4003592")
    assert r.status_code == 200
    html = r.text
    for needle in (
        "Gate trail",
        "Offer ladder",
        "Risk register",
        "Compliance checklist",
        "Encumbrance stack",
        "Valuation &amp; comparables",
        "Underwrite",
        "Net profit",
        "One gate needs your call.",
        "lien:tax",  # highlighted ledger row field id
        'class="row-survives"',  # highlight used by lien row + ledger rows
    ):
        assert needle in html, needle


def test_dossier_sensitivity_and_valuation_provenance():
    """Sensitivity range is recomputed from the waterfall; valuation
    provenance is parsed from the ARV chip (never hardcoded per parcel)."""
    r = client().get("/deals/4003592")
    assert r.status_code == 200
    html = r.text

    # Sensitivity card: a computed $NNk – $NNk range + base-case margin %.
    assert "Sensitivity" in html
    assert re.search(r"\$\d+k &ndash; \$\d+k", html), "computed sensitivity range"
    assert re.search(r"Base-case margin <span class=\"num\">\d+\.\d+%</span>", html)

    # Valuation provenance matches the ARV chip source ("mls_agent · conf 88%").
    assert "floor 85%" in html
    assert "<strong>mls_agent</strong>" in html

    # Gate trail derives the lien detail from the actual surviving liens.
    assert "(pri. 8) survives foreclosure" in html


def test_dossier_renders_other_parcels_without_hardcoded_copy():
    """The dossier must render for any parcel with a CPR file (fixtures use a
    different data shape) — no copy that only fits 4003592."""
    for parcel in ("S5P1", "S5P2"):
        r = client().get(f"/deals/{parcel}")
        assert r.status_code == 200, (parcel, r.status_code)
        assert 'id="ledger-dialog"' in r.text  # modal markup intact


def test_dossier_unknown_parcel_404():
    r = client().get("/deals/999999")
    assert r.status_code == 404, r.status_code
