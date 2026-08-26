# tests/test_comps_platform.py
"""F2 comps-platform tests (all offline/hermetic).

Covers:
- src.comps store primitives: save/load/list round trip, dedupe accumulation,
  tolerant loads, identity derivation rules, HERMES_COMPS_DIR override.
- Seed selection: shared 0.5mi/180d usability filter + parity with
  src.valuation._filter_usable_comps.
- src.hitl F2 modes: --comps-dir batch over synthetic parcels (.valued.json
  written, store populated on pass, up-to-date skips on re-run),
  --use-store seeding, pass-only store-feed rule (+ --store-anyway).
- pipeline --resume-valued-dir: aggregate counts, memo artifacts, exit codes,
  including a real-subprocess smoke over the batch-produced valued files.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pipeline
from src import comps, hitl
from src.cpr import CompRecord
from src.identity import property_id
from src.io_utils import write_json

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TODAY = date.today()
STORE_ADDR = "900 Test Ave, Sherman, TX 75090"


def _dated(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


def _good_comps(n: int = 3) -> list[dict]:
    """Self-check-proven GATE-VAL-passing comp recipe (adjusted, near, fresh)."""
    return [
        {"address": f"{100 + i} Neighbor St",
         "sale_price": 300_000 + i * 5_000,
         "adjusted_price": 301_000 + i * 5_000,
         "sale_date": _dated(10 + i),
         "distance_miles": round(0.2 + 0.05 * i, 2),
         "source": "mls_agent"}
        for i in range(n)
    ]


def _write_batch_parcel(directory, parcel: str, address: str,
                        comps_rows: list[dict]) -> None:
    write_json(os.path.join(directory, parcel + ".json"),
               {"parcel_id": parcel, "county": "grayson",
                "situs_address": address, "channel": "tax_sale",
                "column": "ENRICHED", "blast_tier": 2})
    write_json(os.path.join(directory, parcel + ".comp.json"), comps_rows)


# --- store round trip ----------------------------------------------------------

def test_store_save_load_list_round_trip(tmp_path):
    store = str(tmp_path / "store")
    pid = comps.derive_property_id("grayson", STORE_ADDR)

    path = comps.save_comps(pid, _good_comps(2), comps_dir=store)
    assert path == os.path.join(store, pid + ".json")
    assert os.path.exists(path)
    # Stored rows are normalized to the full CompRecord shape (None-filled
    # optional fields), so store -> seed re-intake needs no extra coercion.
    loaded = comps.load_comps(pid, store)
    assert len(loaded) == 2
    for raw, stored_row in zip(_good_comps(2), loaded):
        assert set(stored_row) == {"address", "sale_price", "sale_date",
                                   "distance_miles", "sqft", "beds", "baths",
                                   "source", "adjusted_price"}
        for key, value in raw.items():
            assert stored_row[key] == value

    # Accumulates across saves without duplicating identical rows.
    extra = {"address": "500 Extra St", "sale_price": 280_000,
             "sale_date": _dated(30), "distance_miles": 0.4,
             "source": "mls_agent"}
    comps.save_comps(pid, _good_comps(2) + [extra], comps_dir=store)
    loaded = comps.load_comps(pid, store)
    assert len(loaded) == 3
    assert [c for c in loaded if c["address"] == "500 Extra St"][0] \
        ["sale_price"] == 280_000

    # Entry shape contract: exactly {property_id, updated_at, comps}.
    entry = comps.load_entry(pid, store)
    assert entry["property_id"] == pid
    assert set(entry) == {"property_id", "updated_at", "comps"}
    assert entry["updated_at"]

    # CompRecord inputs are accepted alongside dicts.
    comps.save_comps(pid, [CompRecord(address="9 Object St",
                                      sale_price=310_000.0,
                                      sale_date=_dated(5),
                                      distance_miles=0.1)],
                     comps_dir=store)
    loaded = comps.load_comps(pid, store)
    assert len(loaded) == 4 and any(c["address"] == "9 Object St"
                                    for c in loaded)

    assert comps.list_properties(store) == [pid]


def test_load_tolerant_of_missing_corrupt_and_foreign(tmp_path):
    store = tmp_path / "store"
    store.mkdir()

    # Missing entry -> None (never an exception).
    assert comps.load_comps("Pmissing12345", str(store)) is None

    # Corrupt JSON degrades to None with a warning, batch keeps running.
    (store / "Pcorrupt12345.json").write_text("{not json at all",
                                              encoding="utf-8")
    assert comps.load_comps("Pcorrupt12345", str(store)) is None

    # Foreign shape (not an entry dict) degrades to None.
    (store / "Pforeign12345.json").write_text('{"hello": 1}', encoding="utf-8")
    assert comps.load_comps("Pforeign12345", str(store)) is None

    # Junk rows inside a valid entry are filtered, valid ones survive.
    write_json(str(store / "Pmixedentry12.json"),
               {"property_id": "Pmixedentry12", "updated_at": "x",
                "comps": ["junk", 42, _good_comps(1)[0]]})
    loaded = comps.load_comps("Pmixedentry12", str(store))
    assert loaded == [_good_comps(1)[0]]

    # Unusable files do not surface in list_properties.
    assert comps.list_properties(str(store)) == ["Pmixedentry12"]


# --- identity ------------------------------------------------------------------

def test_derive_property_id_prefers_identity_and_fallback_is_stable():
    anchored = comps.derive_property_id("collin",
                                        "10022 Plainsman Ln, Frisco, TX 75035")
    assert anchored == property_id("COLLIN",
                                   "10022  Plainsman Ln, Frisco, TX 75035-2607")

    # No-county fallback: sha of the NORMALIZED address, stable across spellings.
    fallback_a = comps.derive_property_id("", "10022 Plainsman Ln, Frisco, TX")
    fallback_b = comps.derive_property_id(None,
                                          "10022 PLAINSMAN LN, FRISCO, TX")
    assert fallback_a == fallback_b
    assert fallback_a.startswith("P") and fallback_a != anchored

    # Owner tiebreaker folds in ONLY when explicitly supplied.
    assert comps.derive_property_id("collin", "1 A St") \
        == comps.derive_property_id("collin", "1 A St", owner="")
    assert comps.derive_property_id("collin", "1 A St", owner="X") \
        != comps.derive_property_id("collin", "1 A St")


def test_default_store_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv(comps.ENV_STORE_DIR, raising=False)
    assert comps.default_store_dir() == comps.DEFAULT_STORE_DIR

    monkeypatch.setenv(comps.ENV_STORE_DIR, str(tmp_path / "envstore"))
    assert comps.default_store_dir() == str(tmp_path / "envstore")
    comps.save_comps("Penvoverride1", _good_comps(1))
    assert (tmp_path / "envstore" / "Penvoverride1.json").exists()


# --- seed selection ------------------------------------------------------------

def test_best_seed_filters_distance_recency_and_matches_valuation(tmp_path):
    store = str(tmp_path / "store")
    cpr_like = SimpleNamespace(county="grayson", situs_address=STORE_ADDR)
    pid = comps.store_key_for_cpr(cpr_like)

    rows = _good_comps(2) + [
        {"address": "700 Too Far St", "sale_price": 290_000,
         "sale_date": _dated(20), "distance_miles": 0.6,
         "source": "mls_agent"},                       # beyond 0.5mi
        {"address": "701 Too Old St", "sale_price": 295_000,
         "sale_date": _dated(200), "distance_miles": 0.1,
         "source": "mls_agent"},                       # older than 180d
        {"address": "702 Unknown Provenance St", "sale_price": 285_000,
         "source": "mls_agent"},                       # kept (unprovable bad)
    ]
    comps.save_comps(pid, rows, comps_dir=store)

    seeds = comps.best_seed_for(cpr_like, comps_dir=store)
    addresses = {s.address for s in seeds}
    assert addresses == {"100 Neighbor St", "101 Neighbor St",
                         "702 Unknown Provenance St"}

    # Parity with the GATE-VAL definition of usable (drift guard).
    from src.valuation import _filter_usable_comps
    records = [r for r in (comps.coerce_comp(d) for d in rows) if r]
    expected, _, _ = _filter_usable_comps(records, TODAY)
    assert sorted(s.address for s in seeds) \
        == sorted(c.address for c in expected)

    # Cold store / unknown asset -> [] (seeding is best-effort).
    stranger = SimpleNamespace(county="", situs_address="nowhere at all")
    assert comps.best_seed_for(stranger, comps_dir=store) == []


# --- hitl: pass-only store feed, --store-anyway, --use-store --------------------

def _run_intake(tmp_path, comps_file: str, out_name: str, store: str,
                *extra: str) -> int:
    cpr_path = tmp_path / "cpr_F2.json"
    write_json(str(cpr_path),
               {"parcel_id": "F2-INTAKE", "county": "grayson",
                "situs_address": STORE_ADDR, "channel": "tax_sale",
                "column": "ENRICHED", "blast_tier": 2})
    return hitl.main(["--cpr", str(cpr_path), "--comps", comps_file,
                      "--out", str(tmp_path / out_name),
                      "--store-dir", store, *extra])


def test_hitl_store_feed_pass_only_unless_store_anyway(tmp_path):
    store = str(tmp_path / "store")

    # PASS: the store is fed automatically, keyed by the explicit --property-id.
    good = tmp_path / "good.json"
    write_json(str(good), _good_comps(3))
    assert _run_intake(tmp_path, str(good), "good.valued.json", store,
                       "--property-id", "PF2PASSCASE01") == 0
    stored = comps.load_comps("PF2PASSCASE01", store)
    assert stored is not None and len(stored) == 3

    # FAIL (2 comps < MIN_COMPS=3): nothing is stored by default...
    weak = tmp_path / "weak.json"
    write_json(str(weak), _good_comps(2))
    assert _run_intake(tmp_path, str(weak), "weak.valued.json", store,
                       "--property-id", "PF2FAILCASE01") == 0
    valued = json.loads((tmp_path / "weak.valued.json").read_text())
    assert valued["valuation"]["gate_val_verdict"] == "fail"
    assert valued["column"] != "VALUED"
    assert comps.load_comps("PF2FAILCASE01", store) is None, \
        "a rejected comp set must never poison the store"

    # ...but explicit --store-anyway persists it anyway.
    assert _run_intake(tmp_path, str(weak), "weak2.valued.json", store,
                       "--property-id", "PF2FAILCASE02",
                       "--store-anyway") == 0
    stored = comps.load_comps("PF2FAILCASE02", store)
    assert stored is not None and len(stored) == 2
    # The earlier pass-case entry was untouched by any of this.
    assert len(comps.load_comps("PF2PASSCASE01", store)) == 3


def test_hitl_use_store_seeds_gate_val_from_store(tmp_path, capsys):
    store = str(tmp_path / "store")
    comps.save_comps("PF2SEEDCASE1", _good_comps(3), comps_dir=store)

    # One brand-new validated comp joins three stored seeds.
    fresh = tmp_path / "fresh.json"
    write_json(str(fresh), [{
        "address": "777 Brand New St", "sale_price": 305_000,
        "adjusted_price": 306_000, "sale_date": _dated(4),
        "distance_miles": 0.25, "source": "mls_agent"}])

    assert _run_intake(tmp_path, str(fresh), "seeded.valued.json", store,
                       "--property-id", "PF2SEEDCASE1",
                       "--use-store") == 0
    out = capsys.readouterr().out
    assert "Comp store: seeded 3 usable comp(s)" in out
    assert "comps used: 4" in out  # 3 seeds + 1 newly validated comp
    valued = json.loads((tmp_path / "seeded.valued.json").read_text())
    assert valued["valuation"]["gate_val_verdict"] == "pass"

    # Duplicate suppression: the first seeded run persisted its merged set
    # (3 seeds + 1 fresh = 4) back into the store; re-supplying rows the
    # store already holds must grow nothing on disk.
    dupes = tmp_path / "dupes.json"
    write_json(str(dupes), _good_comps(3))
    assert _run_intake(tmp_path, str(dupes), "dupes.valued.json", store,
                       "--property-id", "PF2SEEDCASE1",
                       "--use-store") == 0
    out = capsys.readouterr().out
    assert "Comp store: seeded 4 usable comp(s)" in out
    assert "comps used: 4" in out  # supplied duplicates collapse onto seeds
    assert len(comps.load_comps("PF2SEEDCASE1", store)) == 4


# --- hitl --comps-dir batch -----------------------------------------------------

PARCELS = ("4100011", "4100022")


def test_hitl_comps_dir_batch_two_parcels_then_skip_on_rerun(tmp_path, capsys):
    batch_dir = tmp_path / "batches"
    batch_dir.mkdir()
    store = str(tmp_path / "store")
    for i, parcel in enumerate(PARCELS):
        _write_batch_parcel(str(batch_dir), parcel,
                            f"{10 + i} Batch Rd, Sherman, TX 75090",
                            _good_comps(3))

    rc = hitl.main(["--comps-dir", str(batch_dir), "--store-dir", store])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Batch complete: 2 parcel(s)" in out and "pass=2" in out

    expected_pids = []
    for i, parcel in enumerate(PARCELS):
        valued_path = batch_dir / f"{parcel}.valued.json"
        assert valued_path.exists(), f"{parcel}: .valued.json must be written"
        valued = json.loads(valued_path.read_text())
        assert valued["parcel_id"] == parcel
        assert valued["valuation"]["gate_val_verdict"] == "pass"
        assert valued["column"] == "VALUED"
        expected_pids.append(comps.derive_property_id(
            "grayson", f"{10 + i} Batch Rd, Sherman, TX 75090"))

    # Store populated on pass, one entry per distinct asset.
    assert comps.list_properties(store) == sorted(expected_pids)
    for pid in expected_pids:
        assert len(comps.load_comps(pid, store)) == 3

    # Idempotent re-run: valued files newer than inputs -> skipped untouched.
    before = {p.name: p.stat().st_mtime_ns
              for p in batch_dir.iterdir()}
    rc = hitl.main(["--comps-dir", str(batch_dir), "--store-dir", store])
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipped_up_to_date=2" in out
    after = {p.name: p.stat().st_mtime_ns for p in batch_dir.iterdir()}
    assert before == after, "skipped parcels must not be rewritten"


def test_hitl_comps_dir_reports_missing_cpr_and_bad_batches(tmp_path, capsys):
    batch_dir = tmp_path / "batches"
    batch_dir.mkdir()
    store = str(tmp_path / "store")

    # Parcel WITH a sibling CPR but a broken comps payload -> error bucket.
    orphan = batch_dir / "4200033.comp.json"
    orphan.write_text("[{broken", encoding="utf-8")
    write_json(str(batch_dir / "4200033.json"),
               {"parcel_id": "4200033", "county": "grayson",
                "situs_address": "33 Orphan Rd, Sherman, TX 75090",
                "channel": "tax_sale", "blast_tier": 2})

    # Comps file with NO sibling CPR -> its own loud bucket.
    (batch_dir / "4400044.comp.json").write_text("[]", encoding="utf-8")

    rc = hitl.main(["--comps-dir", str(batch_dir), "--store-dir", store])
    out = capsys.readouterr().out + capsys.readouterr().err
    assert rc == 1, "batch errors must exit nonzero"
    assert "error=2" in out
    assert not (batch_dir / "4200033.valued.json").exists()
    assert comps.list_properties(store) == []

    # Empty directory is a clean no-op, not an error.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert hitl.main(["--comps-dir", str(empty)]) == 0
    # Not-a-directory refuses loudly.
    assert hitl.main(["--comps-dir", str(tmp_path / "nope")]) == 1


# --- pipeline --resume-valued-dir -----------------------------------------------

def _produce_valued(vdir, parcel: str, address: str) -> None:
    """Create a genuine *.valued.json through the hitl CLI (never hand-written)."""
    tmp = vdir
    cpr_path = os.path.join(tmp, f"cpr_{parcel}.json")
    comps_path = os.path.join(tmp, f"comps_{parcel}.json")
    write_json(cpr_path,
               {"parcel_id": parcel, "county": "grayson",
                "situs_address": address, "channel": "tax_sale",
                "column": "ENRICHED", "blast_tier": 2})
    write_json(comps_path, _good_comps(3))
    assert hitl.main(["--cpr", cpr_path, "--comps", comps_path,
                      "--out", os.path.join(tmp, f"{parcel}.valued.json"),
                      "--store-dir", os.path.join(tmp, "unused_store")]) == 0


RESUME_PARCELS = {
    "F2R1": "800 Resume Cir, Sherman, TX 75090",
    "F2R2": "802 Resume Cir, Sherman, TX 75090",
}


def _resume_fixture_dir(tmp_path):
    """Dir with one genuine pass valued file + one non-pass valued file."""
    vdir = tmp_path / "valued"
    vdir.mkdir()
    first = True
    for parcel, address in RESUME_PARCELS.items():
        if first:
            _produce_valued(str(vdir), parcel, address)
            first = False
        else:
            write_json(str(vdir / f"{parcel}.valued.json"),
                       {"parcel_id": parcel, "county": "collin",
                        "situs_address": address,
                        "valuation": {"gate_val_verdict": "fail",
                                      "gate_val_reason": "unit-test rejection",
                                      "arv": {"value": 999_999.0,
                                              "source": "vendor_avm",
                                              "confidence": 0.99}}})
    (vdir / "notes.txt").write_text("not a valued file", encoding="utf-8")
    return vdir


def test_resume_valued_dir_aggregate_and_stamping(tmp_path, capsys):
    vdir = _resume_fixture_dir(tmp_path)
    out_dir = tmp_path / "prod"
    dataset = tmp_path / "current_deals.json"
    write_json(str(dataset), [
        {"county": "grayson", "address": RESUME_PARCELS["F2R1"],
         "parcel_id": "F2R1", "val_gate": "NO DATA"},
        {"county": "collin", "address": RESUME_PARCELS["F2R2"],
         "parcel_id": "F2R2", "val_gate": "NO DATA"},
    ])

    rc = pipeline.run_resume_valued_dir_pipeline(
        str(vdir), output_dir=str(out_dir), dataset_path=str(dataset))
    assert rc == pipeline.EXIT_OK == 0

    out = capsys.readouterr().out
    assert "Aggregate: resumed=1 skipped_not_pass=1 errors=0 total=2" in out

    # Only the pass parcel produced artifacts and got stamped.
    assert os.path.exists(out_dir / "deal_memo_F2R1.md")
    assert os.path.exists(out_dir / "offer_letter_F2R1.txt")
    assert not os.path.exists(out_dir / "deal_memo_F2R2.md")
    rows = {r["parcel_id"]: r for r in json.loads(dataset.read_text())}
    assert rows["F2R1"]["val_gate"] == "pass"
    assert rows["F2R2"]["val_gate"] == "NO DATA", \
        "a skipped non-pass parcel must never stamp the dataset"

    # Absent directory -> data-missing exit code.
    assert pipeline.run_resume_valued_dir_pipeline(
        str(tmp_path / "nope"), output_dir=str(out_dir),
        dataset_path=str(dataset)) == pipeline.EXIT_DATA_MISSING == 1

    # Empty existing directory is a clean zero-count ok.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pipeline.run_resume_valued_dir_pipeline(
        str(empty), output_dir=str(out_dir),
        dataset_path=str(dataset)) == 0
    assert "nothing to resume" in capsys.readouterr().out


def test_mode_identity_maps_resume_valued_dir():
    namespace = argparse.Namespace(resume_valued=None,
                                   geocode_backfill=False, bulk_dcad=False,
                                   fixture=False, live=False)
    assert pipeline._mode_identity(namespace) == ("hint", {})
    namespace.resume_valued_dir = os.path.join("data", "valued")
    mode, flags = pipeline._mode_identity(namespace)
    assert mode == "resume" and flags == {"valued_dir": "valued"}


def test_resume_valued_dir_subprocess_smoke_over_batch_output(tmp_path):
    """Full F2 chain as a real subprocess: valued dir -> memos + stamps + rc 0."""
    vdir = tmp_path / "chain"
    vdir.mkdir()
    addresses = {}
    for i, parcel in enumerate(("F2S1", "F2S2")):
        address = f"{50 + i} Chain Ct, Sherman, TX 75090"
        addresses[parcel] = address
        _write_batch_parcel(str(vdir), parcel, address, _good_comps(3))
    # Produce the valued files through the batch mode itself (offline).
    assert hitl.main(["--comps-dir", str(vdir),
                      "--store-dir", str(tmp_path / "store")]) == 0

    dataset = tmp_path / "ds.json"
    write_json(str(dataset), [
        {"county": "grayson", "address": addr, "parcel_id": parcel,
         "val_gate": "NO DATA"}
        for parcel, addr in addresses.items()])
    out_dir = tmp_path / "prod"

    script = (
        "import sys; sys.path.insert(0, {repo!r}); import pipeline; "
        "sys.exit(pipeline.run_resume_valued_dir_pipeline("
        "{vdir!r}, output_dir={out!r}, dataset_path={ds!r}))"
    ).format(repo=REPO_ROOT, vdir=str(vdir), out=str(out_dir),
             ds=str(dataset))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True, timeout=600)

    assert proc.returncode == 0, (
        f"subprocess failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert ("Aggregate: resumed=2 skipped_not_pass=0 errors=0 total=2"
            in proc.stdout)
    for parcel in addresses:
        assert os.path.exists(out_dir / f"deal_memo_{parcel}.md")
        assert os.path.exists(out_dir / f"offer_letter_{parcel}.txt")
    rows = {r["parcel_id"]: r for r in json.loads(dataset.read_text())}
    assert all(r["val_gate"] == "pass" for r in rows.values())
