"""Offline regression tests for DCAD disambiguation (src/disambiguate.py).

Covers build_queue / purge_mismatches / export round-trips on synthetic cache
dicts plus every non-live CLI path via a temp cache override (--cache flag and
HERMES_DCAD_CACHE env). Live browser behavior is exercised only through mocks;
no test here touches the network.
"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest
from bs4 import BeautifulSoup

import src.disambiguate as dz
from src.disambiguate import (
    age_days,
    build_queue,
    collect_candidates,
    format_age,
    main,
    purge_mismatches,
    render_queue_markdown,
    resolve_cache_path,
)

CACHE_ENV_VAR = "HERMES_DCAD_CACHE"

NEW_TS = "2026-08-25T22:52:16.987920+00:00"
MID_TS = "2026-08-25T22:51:50.653016+00:00"
OLD_TS = "2026-08-01T10:00:00+00:00"


@pytest.fixture
def synth_cache():
    """Synthetic stand-in for data/dcad_cache.json (mixed statuses)."""
    return {
        "707 LISA LN, CEDAR HILL, TX 75104 _": {
            "status": "mismatch",
            "error": "echo-back verification failed",
            "displayed_address": "2026 Appraisal Notice",
            "retrieved_at": NEW_TS,
        },
        "14160 Dallas Parkway Danas, Carrollton, TX 75254": {
            "status": "mismatch",
            "displayed_address": "link to view the details of your account.",
            "retrieved_at": MID_TS,
        },
        "OLD MISMATCH RD, DALLAS, TX 75201": {
            "status": "mismatch",
            "displayed_address": None,
            "retrieved_at": OLD_TS,
        },
        "NO STAMP MISMATCH, DALLAS, TX 75201": {"status": "mismatch"},
        "A|B PIPE ST, DALLAS, TX 75201": {
            "status": "mismatch",
            "displayed_address": "cell | pipes | everywhere",
            "retrieved_at": "2026-08-25T23:59:59+00:00",
        },
        "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334": {
            "status": "parsed",
            "beds": "3",
            "match_quality": "partial",
            "retrieved_at": "2026-08-25T22:24:16.620325+00:00",
        },
        "NEGATIVE ST, DALLAS, TX 75201": {
            "status": "no_data_found",
            "retrieved_at": MID_TS,
        },
    }


def _write_cache(tmp_path, cache):
    path = tmp_path / "dcad_cache.json"
    path.write_text(json.dumps(cache), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# build_queue
# --------------------------------------------------------------------------

def test_build_queue_selects_only_mismatches_newest_first(synth_cache):
    queue = build_queue(synth_cache)
    assert [r["key"] for r in queue] == [
        "A|B PIPE ST, DALLAS, TX 75201",                       # 23:59:59
        "707 LISA LN, CEDAR HILL, TX 75104 _",                 # 22:52:16
        "14160 Dallas Parkway Danas, Carrollton, TX 75254",    # 22:51:50
        "OLD MISMATCH RD, DALLAS, TX 75201",                   # Aug 1
        "NO STAMP MISMATCH, DALLAS, TX 75201",                 # undated last
    ]
    assert all(r["queried_address"] == r["key"] for r in queue)
    top = queue[0]
    assert top["attempted_at"] == "2026-08-25T23:59:59+00:00"
    assert top["displayed_diagnostic"] == "cell | pipes | everywhere"


def test_build_queue_row_shape_matches_contract(synth_cache):
    for row in build_queue(synth_cache):
        assert set(row) == {
            "key", "queried_address", "displayed_diagnostic", "attempted_at",
        }


# --------------------------------------------------------------------------
# purge_mismatches
# --------------------------------------------------------------------------

def test_purge_mismatches_removes_only_mismatches_and_is_pure(synth_cache):
    snapshot = json.loads(json.dumps(synth_cache))
    kept, purged = purge_mismatches(synth_cache)
    assert purged == 5
    assert len(kept) == 2
    assert set(kept) == {
        "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334",
        "NEGATIVE ST, DALLAS, TX 75201",
    }
    assert synth_cache == snapshot, "purge_mismatches must not mutate input"
    # Round-trip: purging twice changes nothing further.
    kept2, purged2 = purge_mismatches(kept)
    assert purged2 == 0 and kept2 == kept


# --------------------------------------------------------------------------
# ages + markdown rendering
# --------------------------------------------------------------------------

def test_age_days_and_format_age():
    ts = "2026-08-20T22:00:00+00:00"
    assert age_days(ts, today=date(2026, 8, 26)) == 6
    assert age_days("garbage", today=date(2026, 8, 26)) is None
    assert age_days(None, today=date(2026, 8, 26)) is None
    assert format_age(6) == "6d"
    assert format_age(None) == "?"


def test_markdown_escapes_pipes_and_counts_rows(synth_cache):
    rows = build_queue(synth_cache)
    md = render_queue_markdown(
        rows, "data/dcad_cache.json", generated_at="2026-08-26T00:00:00+00:00",
    )
    lines = [ln for ln in md.splitlines() if ln.startswith("| ")]
    assert len(lines) == len(rows) + 2, "header + separator + one row per entry"
    assert "| # | Query | Diagnostic | Age |" in md
    assert "`A\\|B PIPE ST, DALLAS, TX 75201`" in md
    assert "cell \\| pipes \\| everywhere" in md
    assert f"- Mismatched entries: {len(rows)}" in md
    assert "| ? |" in md  # undated row -> unknown age


# --------------------------------------------------------------------------
# export round-trip
# --------------------------------------------------------------------------

def test_export_round_trip_json_matches_build_queue(tmp_path, synth_cache):
    cache_path = _write_cache(tmp_path, synth_cache)
    rc = main(["--export", "--cache", cache_path])
    assert rc == 0

    queue_json = tmp_path / "dcad_disambiguation_queue.json"
    queue_md = tmp_path / "dcad_disambiguation_queue.md"
    assert queue_json.exists() and queue_md.exists()

    exported = json.loads(queue_json.read_text(encoding="utf-8"))
    on_disk = json.loads(open(cache_path, encoding="utf-8").read())
    assert exported == build_queue(on_disk)
    md = queue_md.read_text(encoding="utf-8")
    assert len([ln for ln in md.splitlines() if ln.startswith("| ")]) == \
        len(exported) + 2


# --------------------------------------------------------------------------
# cache-path precedence
# --------------------------------------------------------------------------

def test_resolve_cache_path_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv(CACHE_ENV_VAR, raising=False)
    assert resolve_cache_path().endswith(os.path.join("data", "dcad_cache.json"))

    env_path = str(tmp_path / "env_cache.json")
    monkeypatch.setenv(CACHE_ENV_VAR, env_path)
    assert resolve_cache_path() == os.path.abspath(env_path)

    flag_path = str(tmp_path / "flag_cache.json")
    assert resolve_cache_path(flag_path) == os.path.abspath(flag_path)


# --------------------------------------------------------------------------
# CLI offline paths
# --------------------------------------------------------------------------

def test_cli_list_via_env_override(tmp_path, synth_cache, monkeypatch, capsys):
    cache_path = _write_cache(tmp_path, synth_cache)
    monkeypatch.setenv(CACHE_ENV_VAR, cache_path)
    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("5 mismatched entries in")
    assert "A|B PIPE ST, DALLAS, TX 75201" in out      # newest first
    assert "diagnostic: cell | pipes | everywhere" in out
    assert "NO STAMP MISMATCH" in out                  # undated rows still queue
    assert out.rstrip().endswith("NO STAMP MISMATCH, DALLAS, TX 75201")
    assert "15806 BREEDLOVE" not in out                # parsed never queued


def test_cli_list_respects_limit(tmp_path, synth_cache, capsys):
    cache_path = _write_cache(tmp_path, synth_cache)
    assert main(["--list", "--limit", "2", "--cache", cache_path]) == 0
    out = capsys.readouterr().out
    assert out.startswith("5 mismatched entries")
    assert ", showing first 2)" in out
    assert "A|B PIPE ST" in out and "707 LISA LN" in out
    assert "14160 Dallas Parkway" not in out


def test_cli_show_prints_entry_and_missing_key_fails(tmp_path, synth_cache, capsys):
    cache_path = _write_cache(tmp_path, synth_cache)
    key = "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334"
    assert main(["--show", key, "--cache", cache_path]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["key"] == key
    assert shown["status"] == "parsed" and shown["beds"] == "3"

    assert main(["--show", "NOT THERE, DALLAS, TX", "--cache", cache_path]) == 1
    err = capsys.readouterr().err
    assert "no cached entry" in err


def test_cli_purge_mismatches_rewrites_disk(tmp_path, synth_cache, capsys):
    cache_path = _write_cache(tmp_path, synth_cache)
    assert main(["--purge-mismatches", "--cache", cache_path]) == 0
    out = capsys.readouterr().out
    assert "purged 5 mismatched entries" in out
    remaining = json.loads(open(cache_path, encoding="utf-8").read())
    assert set(remaining) == {
        "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334",
        "NEGATIVE ST, DALLAS, TX 75201",
    }
    assert all(e["status"] != "mismatch" for e in remaining.values())


def test_cli_no_mode_prints_help(capsys):
    assert main([]) == 0
    assert "usage:" in capsys.readouterr().out


def test_cli_choice_without_resolve_is_argparse_error():
    with pytest.raises(SystemExit) as excinfo:
        main(["--choice", "1"])
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------
# LIVE orchestration behind mocks (no browser, no network)
# --------------------------------------------------------------------------

IMPLAUSIBLE = "60 foot right-of-way, at the northeast comer of said lot"
PLAUSIBLE = "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334"


def test_resolve_refuses_implausible_query_offline(tmp_path, monkeypatch, capsys):
    def _boom(*a, **k):  # any call means the guard leaked into the live path
        raise AssertionError("_resolve_live must not run for junk queries")

    monkeypatch.setattr(dz, "_resolve_live", _boom)
    cache_path = _write_cache(tmp_path, {})
    assert main(["--resolve", IMPLAUSIBLE, "--cache", cache_path]) == 1
    out = capsys.readouterr().out
    assert "refusing to search DCAD" in out
    assert open(cache_path, encoding="utf-8").read() == "{}"


@pytest.fixture
def live_mock(monkeypatch):
    """Replace the live Playwright flow with a scripted result."""
    def install(payload):
        async def fake_resolve_live(query, choice):
            return payload
        monkeypatch.setattr(dz, "_resolve_live", fake_resolve_live)
    return install


def test_resolve_lists_candidates_then_exits_for_choice(tmp_path, live_mock, capsys):
    cache_path = _write_cache(tmp_path, {})
    live_mock({
        "stage": "candidates",
        "results_url": "https://www.dallascad.org/Results.aspx",
        "candidates": [
            {"choice": 1, "url": "https://www.dallascad.org/Detail.aspx?ID=1",
             "row_text": "14160 DALLAS PKWY | DALLAS TX 75254"},
            {"choice": 2, "url": "https://www.dallascad.org/Detail.aspx?ID=2",
             "row_text": "707 LISA LN | CEDAR HILL TX 75104"},
        ],
    })
    assert main(["--resolve", PLAUSIBLE, "--cache", cache_path]) == 0
    out = capsys.readouterr().out
    assert "2 candidate detail link(s)" in out
    assert "1. 14160 DALLAS PKWY | DALLAS TX 75254" in out
    assert "Re-run with --choice N" in out
    assert json.loads(open(cache_path, encoding="utf-8").read()) == {}


def test_resolve_success_updates_cache_in_bulk_shape(tmp_path, live_mock, capsys):
    cache_path = _write_cache(tmp_path, {PLAUSIBLE: {"status": "mismatch"}})
    gated = {
        "status": "parsed",
        "year_built": "2006",
        "improvement_sqft": "2,388",
        "cad_appraised_value": "518510",
        "match_quality": "partial",
    }
    live_mock({
        "stage": "resolved",
        "gated": gated,
        "displayed_address": "15806 BREEDLOVE PL",
        "picked": {"choice": 2, "url": ".../Detail.aspx?ID=2", "row_text": "x"},
        "candidates_total": 3,
    })
    assert main(["--resolve", PLAUSIBLE, "--choice", "2",
                 "--cache", cache_path]) == 0
    out = capsys.readouterr().out
    assert "CACHE UPDATED" in out
    entry = json.loads(open(cache_path, encoding="utf-8").read())[PLAUSIBLE]
    assert entry["status"] == "parsed"
    assert entry["match_quality"] == "partial"
    assert entry["improvement_sqft"] == "2,388"
    assert dz.parse_date_any(entry["retrieved_at"]) is not None, \
        "resolved entry must carry a bulk-compatible retrieved_at"
    assert set(entry) - set(gated) == {"retrieved_at"}


def test_resolve_gate_failure_leaves_cache_untouched(tmp_path, live_mock, capsys):
    original = {PLAUSIBLE: {"status": "mismatch", "displayed_address": "old"}}
    cache_path = _write_cache(tmp_path, original)
    live_mock({
        "stage": "resolved",
        "gated": {
            "status": "mismatch",
            "error": "DCAD detail page failed address echo-back verification "
                     "(quality=mismatch); property fields withheld",
            "displayed_address": "999 WRONG ST, DALLAS, TX 75201",
        },
        "displayed_address": "999 WRONG ST, DALLAS, TX 75201",
        "picked": {"choice": 1, "url": ".../Detail.aspx?ID=1", "row_text": "x"},
        "candidates_total": 1,
    })
    assert main(["--resolve", PLAUSIBLE, "--choice", "1",
                 "--cache", cache_path]) == 1
    out = capsys.readouterr().out
    assert "ECHO GATE FAILED" in out and "cache untouched" in out
    after = json.loads(open(cache_path, encoding="utf-8").read())
    assert after == original


def test_resolve_live_error_reports_and_keeps_cache(tmp_path, live_mock, capsys):
    cache_path = _write_cache(tmp_path, {})
    live_mock({"stage": "error", "message": "DCAD search error — ASP.NET form "
                                            "rejected submission"})
    assert main(["--resolve", PLAUSIBLE, "--choice", "1",
                 "--cache", cache_path]) == 1
    out = capsys.readouterr().out
    assert "FAIL: DCAD search error" in out
    assert "Cache left untouched." in out
    assert json.loads(open(cache_path, encoding="utf-8").read()) == {}


# --------------------------------------------------------------------------
# candidate collection from results-page HTML (pure)
# --------------------------------------------------------------------------

RESULTS_HTML = """
<table>
  <tr>
    <td><a href="Detail.aspx?ID=100">Select</a></td>
    <td>14160&nbsp;DALLAS PKWY</td><td>DALLAS&nbsp;&nbsp;TX 75254</td>
  </tr>
  <tr>
    <td><a href="/AcctDetail.aspx?ID=200">Select</a></td>
    <td>707 LISA LN</td>
  </tr>
  <tr>
    <!-- same URL as row 1: ASP.NET grids repeat the link per pager row -->
    <td><a href="Detail.aspx?ID=100">Select</a></td>
    <td>duplicate of row 1</td>
  </tr>
</table>
<a href="/SearchAddr.aspx">new search</a>
<a href="http://elsewhere.example/Detail/x">foreign detail link</a>
"""


def test_collect_candidates_numbers_dedupes_and_labels_rows():
    soup = BeautifulSoup(RESULTS_HTML, "html.parser")
    cands = collect_candidates(soup)
    assert [c["choice"] for c in cands] == [1, 2]
    assert cands[0]["url"] == "https://www.dallascad.org/Detail.aspx?ID=100"
    assert cands[1]["url"] == "https://www.dallascad.org/AcctDetail.aspx?ID=200"
    assert "14160 DALLAS PKWY" in cands[0]["row_text"]
    assert "DALLAS TX 75254" in cands[0]["row_text"], \
        "nbsp runs must collapse into single spaces"
    assert "707 LISA LN" in cands[1]["row_text"]
    assert all("SearchAddr" not in c["url"] for c in cands)
    assert all(not c["url"].startswith("http://elsewhere.example")
               for c in cands), "off-domain detail links must be dropped"


def test_collect_candidates_empty_results_page():
    soup = BeautifulSoup("<p>no accounts found</p>", "html.parser")
    assert collect_candidates(soup) == []
