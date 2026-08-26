"""RL4/F1/RL2/F10 tests: run summaries, exit codes, --resume-valued.

Coverage contract for this module:
- RL4: every data mode ends by writing data/run_summary.json with a fixed
  schema {mode, started_at ISO, duration_s, counts per source, flags}; the
  fixture mode is exercised as a real subprocess exactly as an operator runs it.
- Exit-code helper mapping is unit-tested against docs/EXIT_CODES.md.
- F1: --resume-valued consumes a genuine src.hitl-produced *.valued.json
  (synthesized in-test — data/fixtures/fixture_cpr_4003592.valued.json is NOT
  required to exist), producing production-named artifacts and stamping
  val_gate=pass into a hermetic dataset copy.
- RL2/F10: min-leads floor and grayson degradation run against monkeypatched
  adapters — zero network, like everything else in this file.

All artifact writes go to tmp_path; only the subprocess fixture run touches the
(gitignored) repo data/ directory.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pipeline  # noqa: E402
from src import hitl  # noqa: E402
from src.io_utils import write_json  # noqa: E402

PY = sys.executable  # environment-portable: works in CI and local venv
PIPELINE = os.path.join(REPO, "pipeline.py")
RUN_SUMMARY = os.path.join(REPO, "data", "run_summary.json")

SUMMARY_KEYS = {"mode", "started_at", "duration_s", "counts", "flags"}


# --- Exit-code helper mapping (docs/EXIT_CODES.md executable truth) ----------

def test_exit_code_helper_mapping():
    assert pipeline.EXIT_OK == 0
    assert pipeline.exit_code_for("ok") == 0
    assert pipeline.exit_code_for("usage") == 1
    assert pipeline.exit_code_for("data_missing") == 1
    assert pipeline.exit_code_for("gate_fail") == 2
    assert pipeline.exit_code_for("lookup_error") == 3
    assert pipeline.exit_code_for("min_leads") == 4


def test_exit_code_unknown_outcome_raises_loudly():
    with pytest.raises(ValueError) as ei:
        pipeline.exit_code_for("banana")
    assert "banana" in str(ei.value)


def test_exit_codes_doc_catalogs_every_code():
    """Guard against doc drift: docs/EXIT_CODES.md lists every EXIT_CODES row."""
    with open(os.path.join(REPO, "docs", "EXIT_CODES.md"), encoding="utf-8") as f:
        doc = f.read()
    for name, code in pipeline.EXIT_CODES.items():
        assert f"| {code} |" in doc, f"exit code {code} ({name}) missing from doc"
    for phrase in ("PORTAL-SHAPE-CHANGED", "--force-dead-errors", "run_summary.json"):
        assert phrase in doc


# --- RunSummary schema --------------------------------------------------------

def test_run_summary_schema_fixed_keys_and_bump_guards(tmp_path):
    s = pipeline.RunSummary(mode="fixture", flags={"k": "v"},
                            path=str(tmp_path / "rs.json"))
    s.bump("fixture", "leads", 3)
    s.bump("fixture", "parsed")
    with pytest.raises(ValueError):
        s.bump("fixture", "not_a_real_count_key")

    payload = s.as_dict()
    assert set(payload) == SUMMARY_KEYS
    assert payload["mode"] == "fixture"
    assert payload["flags"] == {"k": "v"}
    datetime.fromisoformat(payload["started_at"])  # ISO-parseable timestamp
    assert isinstance(payload["duration_s"], float) and payload["duration_s"] >= 0
    row = payload["counts"]["fixture"]
    assert set(row) == set(pipeline.SUMMARY_COUNT_KEYS)
    assert row["leads"] == 3 and row["parsed"] == 1
    assert row["errors"] == 0  # untouched counters are present and zeroed


def test_run_summary_write_persists_atomically(tmp_path):
    s = pipeline.RunSummary(mode="live", flags={},
                            path=str(tmp_path / "rs.json"))
    s.bump("collin_foreclosure", "leads", 7)
    path = s.write()
    assert path.endswith("rs.json") and os.path.exists(path)
    on_disk = json.load(open(path, encoding="utf-8"))
    assert on_disk["mode"] == "live"
    assert on_disk["counts"]["collin_foreclosure"]["leads"] == 7
    assert set(on_disk) == SUMMARY_KEYS


def test_mode_identity_maps_every_dispatch_branch():
    base = dict(resume_valued=None, geocode_backfill=False, dry_run=False,
                retry_failed=False, bulk_dcad=False, fixture=False, live=False,
                county="dallas", limit=2, min_leads=5, force_dead_errors=True,
                allow_gate_fail_demo=False)

    def ns(**over):
        return argparse.Namespace(**{**base, **over})

    assert pipeline._mode_identity(ns())[0] == "hint"
    mode, flags = pipeline._mode_identity(ns(fixture=True))
    assert mode == "fixture" and flags == {"allow_gate_fail_demo": False}
    mode, flags = pipeline._mode_identity(ns(live=True))
    assert mode == "live"
    assert flags == {"county": "dallas", "limit": 2, "min_leads": 5,
                     "force_dead_errors": True, "allow_gate_fail_demo": False}
    mode, flags = pipeline._mode_identity(ns(bulk_dcad=True))
    assert mode == "bulk-dcad"
    mode, flags = pipeline._mode_identity(ns(geocode_backfill=True, dry_run=True))
    assert mode == "geocode" and flags == {"dry_run": True, "retry_failed": False}
    mode, flags = pipeline._mode_identity(ns(resume_valued="/x/data/cpr_9.valued.json"))
    assert mode == "resume"
    assert flags == {"valued_file": "cpr_9.valued.json"}


# --- RL4: fixture mode subprocess writes the summary --------------------------

def test_fixture_subprocess_writes_run_summary_with_expected_keys():
    r = subprocess.run([PY, PIPELINE, "--fixture"], capture_output=True,
                       text=True, timeout=600, cwd=REPO)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert os.path.exists(RUN_SUMMARY), "fixture run must persist run_summary.json"

    with open(RUN_SUMMARY, encoding="utf-8") as f:
        payload = json.load(f)
    assert set(payload) == SUMMARY_KEYS
    assert payload["mode"] == "fixture"
    datetime.fromisoformat(payload["started_at"])
    assert payload["duration_s"] >= 0
    assert "allow_gate_fail_demo" in payload["flags"]

    fixture_row = payload["counts"].get("fixture")
    assert fixture_row is not None, "fixture source row must exist"
    assert set(fixture_row) == set(pipeline.SUMMARY_COUNT_KEYS)
    assert fixture_row["leads"] >= 1
    assert fixture_row["parsed"] >= 1
    assert fixture_rows_clean(fixture_row)


def fixture_rows_clean(row):
    return row["errors"] == 0 and row["enriched"] >= 1


# --- F1: restore_serialized_valuation -----------------------------------------

def test_restore_serialized_valuation_tolerant_of_junk():
    assert pipeline.restore_serialized_valuation({}) is None
    assert pipeline.restore_serialized_valuation({"valuation": "junk"}) is None
    val = pipeline.restore_serialized_valuation({"valuation": {
        "arv": {"value": 305000, "source": "mls_agent", "confidence": "bogus"},
        "comp_source": "mls_agent",
        "gate_val_verdict": "pass",
        "comp_set": [{"address": "1 A St", "sale_price": 300000},
                     "junk-entry", {}],
    }})
    assert val.arv.value == 305000
    assert val.arv.confidence == 0.0  # unparsable confidence degrades, never crashes
    assert val.gate_val_verdict == "pass"
    assert len(val.comp_set) == 1 and val.comp_set[0].address == "1 A St"


# --- F1: --resume-valued end-to-end (synthesized via src.hitl) -----------------

@pytest.fixture(scope="module")
def valued_cpr_path(tmp_path_factory):
    """Produce a REAL *.valued.json through the hitl CLI (not hand-written)."""
    tmp = tmp_path_factory.mktemp("hitl_synth")
    today = date.today()
    cpr_dict = {
        "parcel_id": "HITL-TEST-1",
        "county": "grayson",
        "situs_address": "900 Test Ave, Sherman, TX 75090",
        "channel": "tax_sale",
        "column": "ENRICHED",
        "blast_tier": 3,
        "owner_name": {"value": "Test Owner", "source": "unit_test",
                       "retrieved_at": "2026-08-25T00:00:00", "confidence": 0.9},
        "liens": [{"lien_type": "mortgage", "amount": 250000.0,
                   "recording_date": "3/3/2020", "lender": "TEST BANK",
                   "priority": 1, "survives_foreclosure": False,
                   "survives_tax_sale": False}],
        "valuation": {"gate_val_verdict": "stale", "comp_set": []},
    }
    comp_dicts = [
        {"address": f"{100 + i} Neighbor St",
         "sale_price": 300_000 + i * 5_000,
         "adjusted_price": 301_000 + i * 5_000,
         "sale_date": (today - timedelta(days=10 + i)).isoformat(),
         "distance_miles": round(0.2 + 0.05 * i, 2),
         "source": "mls_agent"}
        for i in range(3)
    ]
    cpr_path = str(tmp / "cpr_HITL-TEST-1.json")
    comps_path = str(tmp / "comps.json")
    valued_path = str(tmp / "cpr_HITL-TEST-1.valued.json")
    write_json(cpr_path, cpr_dict)
    write_json(comps_path, comp_dicts)
    rc = hitl.main(["--cpr", cpr_path, "--comps", comps_path,
                    "--out", valued_path])
    assert rc == 0
    with open(valued_path, encoding="utf-8") as f:
        valued = json.load(f)
    assert valued["valuation"]["gate_val_verdict"] == "pass"
    assert valued["valuation"]["arv"]["value"] > 0
    return valued_path


def test_resume_valued_smoke_end_to_end(tmp_path, valued_cpr_path):
    out_dir = str(tmp_path / "prod")
    dataset = str(tmp_path / "current_deals.json")
    write_json(dataset, [
        {"county": "grayson", "address": "900 Test Ave, Sherman, TX 75090",
         "parcel_id": "HITL-TEST-1", "val_gate": "NO DATA"},
        {"county": "collin", "address": "1 Elsewhere Rd, Plano, TX 75023",
         "parcel_id": "OTHER", "val_gate": "NO DATA"},
    ])
    summary = pipeline.RunSummary(mode="resume",
                                  path=str(tmp_path / "run_summary.json"))

    rc = pipeline.run_resume_valued_pipeline(
        valued_cpr_path, summary=summary,
        output_dir=out_dir, dataset_path=dataset)

    assert rc == pipeline.EXIT_OK == 0
    # Production naming, keyed by parcel (no fixture_ prefix).
    assert os.path.exists(os.path.join(out_dir, "deal_memo_HITL-TEST-1.md"))
    assert os.path.exists(os.path.join(out_dir, "offer_letter_HITL-TEST-1.txt"))
    assert os.path.exists(os.path.join(out_dir, "cpr_HITL-TEST-1.json"))
    # Dataset stamped ONLY on the address-matched row (atomic merge semantics).
    rows = json.load(open(dataset, encoding="utf-8"))
    assert rows[0]["val_gate"] == "pass"
    assert rows[1]["val_gate"] == "NO DATA"
    # Summary carries the resume counts.
    payload = summary.as_dict()
    assert payload["mode"] == "resume"
    assert payload["counts"]["resume"]["parsed"] == 1
    assert payload["counts"]["resume"]["written_back"] == 1
    assert payload["counts"]["resume"]["errors"] == 0


def test_resume_missing_input_returns_data_missing(tmp_path):
    rc = pipeline.run_resume_valued_pipeline(str(tmp_path / "nope.json"),
                                             output_dir=str(tmp_path),
                                             dataset_path=str(tmp_path / "d.json"))
    assert rc == pipeline.EXIT_DATA_MISSING == 1


def test_resume_unparseable_input_returns_data_missing(tmp_path):
    bad = tmp_path / "cpr_bad.json"
    bad.write_text("{not json at all", encoding="utf-8")
    rc = pipeline.run_resume_valued_pipeline(str(bad),
                                             output_dir=str(tmp_path),
                                             dataset_path=str(tmp_path / "d.json"))
    assert rc == pipeline.EXIT_DATA_MISSING == 1


def test_resume_non_pass_verdict_is_gate_fail_and_never_stamps(tmp_path):
    """A file whose GATE-VAL did not pass is not a valued CPR: refuse loudly."""
    bad = tmp_path / "cpr_BAD-1.valued.json"
    write_json(str(bad), {
        "parcel_id": "BAD-1", "county": "collin",
        "situs_address": "1 Main St, Plano, TX 75023",
        "valuation": {"gate_val_verdict": "fail",
                      "gate_val_reason": "unit-test rejection",
                      "arv": {"value": 999_999.0, "source": "vendor_avm",
                              "confidence": 0.99}},
    })
    dataset = str(tmp_path / "ds.json")
    write_json(dataset, [{"county": "collin",
                          "address": "1 Main St, Plano, TX 75023",
                          "val_gate": "REJECTED"}])

    rc = pipeline.run_resume_valued_pipeline(str(bad), output_dir=str(tmp_path),
                                             dataset_path=dataset)

    assert rc == pipeline.EXIT_GATE_FAIL == 2
    rows = json.load(open(dataset, encoding="utf-8"))
    assert rows[0]["val_gate"] == "REJECTED", \
        "a non-pass verdict must never stamp the dataset"


# --- F1: val_gate stamping identity --------------------------------------------

def test_stamp_val_gate_identity_match_and_idempotence():
    records = [
        {"county": "Grayson", "address": "900  Test AVE", "val_gate": "NO DATA"},
        {"county": "grayson", "address": "900 Test Ave", "val_gate": "pass"},
        {"county": "collin", "address": "900 Test Ave", "val_gate": "NO DATA"},
    ]
    stamped = pipeline._stamp_val_gate(records, "grayson", "900 Test Ave")
    assert stamped == 1  # county+address identity; already-pass row untouched
    assert records[0]["val_gate"] == "pass"
    assert records[1]["val_gate"] == "pass"
    assert records[2]["val_gate"] == "NO DATA"
    assert pipeline._stamp_val_gate(records, "grayson", "900 Test Ave") == 0


def test_resume_dataset_absent_is_not_fatal(tmp_path, valued_cpr_path):
    """Artifacts are the product of resume; a missing dataset just skips the stamp."""
    rc = pipeline.run_resume_valued_pipeline(
        valued_cpr_path, output_dir=str(tmp_path / "o"),
        dataset_path=str(tmp_path / "missing_dataset.json"))
    assert rc == pipeline.EXIT_OK == 0
    assert os.path.exists(os.path.join(tmp_path / "o",
                                       "deal_memo_HITL-TEST-1.md"))


# --- RL2: --min-leads floor -----------------------------------------------------

def _install_collin_adapter(monkeypatch, leads):
    import src.adapters.collin_foreclosure as cf_mod

    class FakeCollinAdapter:
        def __init__(self):
            pass

        def fetch(self, limit=1, extract_details=False):
            return leads

    monkeypatch.setattr(cf_mod, "CollinForeclosureAdapter", FakeCollinAdapter)


def test_live_below_min_leads_floor_exits_four_with_banner(monkeypatch, capsys):
    _install_collin_adapter(monkeypatch, leads=[])  # portal-shape-changed sim
    with pytest.raises(SystemExit) as ei:
        pipeline.run_live_pipeline("collin", limit=5, min_leads=3)
    assert ei.value.code == pipeline.EXIT_MIN_LEADS == 4
    out = capsys.readouterr().out
    assert "PORTAL-SHAPE-CHANGED" in out
    assert "0 lead(s) found vs floor of 3" in out


def test_live_at_or_above_floor_continues(monkeypatch, capsys):
    _install_collin_adapter(monkeypatch, leads=[])
    pipeline.run_live_pipeline("collin", limit=5, min_leads=0)  # floor disabled
    out = capsys.readouterr().out
    assert "PORTAL-SHAPE-CHANGED" not in out
    assert "No leads found" in out


# --- F10: grayson dispatch degradation -------------------------------------------

def _install_grayson_adapter(monkeypatch, behavior):
    import src.adapters.grayson_tax as gt_mod

    class FakeGraysonAdapter:
        def __init__(self):
            pass

        def fetch(self, **params):
            return behavior()

    monkeypatch.setattr(gt_mod, "GraysonTaxAdapter", FakeGraysonAdapter)


def test_grayson_dead_endpoint_exception_degrades_to_zero_leads(monkeypatch, capsys):
    def boom():
        raise ConnectionError("DNS NXDOMAIN")
    _install_grayson_adapter(monkeypatch, boom)
    pipeline.run_live_pipeline("grayson", limit=5)  # must NOT raise
    out = capsys.readouterr().out
    assert "GRAYSON DEGRADED" in out
    assert "--force-dead-errors" in out
    assert "No leads found" in out


def test_grayson_empty_pull_also_reports_degradation(monkeypatch, capsys):
    # The adapter swallows transport errors internally and returns [] — the
    # empty pull from these known-dead endpoints is the same degradation.
    _install_grayson_adapter(monkeypatch, list)
    pipeline.run_live_pipeline("grayson", limit=5)
    out = capsys.readouterr().out
    assert "GRAYSON DEGRADED" in out


def test_grayson_force_dead_errors_propagates_failure(monkeypatch):
    def boom():
        raise ConnectionError("DNS NXDOMAIN")
    _install_grayson_adapter(monkeypatch, boom)
    with pytest.raises(ConnectionError):
        pipeline.run_live_pipeline("grayson", limit=5, force_dead_errors=True)


# --- timezone sanity --------------------------------------------------------------

def test_utc_now_is_timezone_aware():
    assert pipeline._utc_now().tzinfo is timezone.utc
