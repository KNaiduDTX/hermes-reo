"""Audit CPR-2: generate_all failure path must never crash or mislabel.

Regression: the except handler referenced 'pid' bound only inside the try, so
a first-record build_cpr crash escaped the batch-continues catch as
UnboundLocalError, and later failures inherited the previous record's id.
"""
import json

import pytest

import src.cpr_store as cpr_store
from src.cpr_store import generate_all


def _book(tmp_path, n):
    records = [{"parcel_id": f"P{i}", "address": f"{i} Main St",
                "city": "Plano", "county": "collin", "cad_value": 300_000 + i}
               for i in range(n)]
    path = tmp_path / "deals.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return str(path)


def test_first_record_failure_does_not_crash_batch(tmp_path, monkeypatch):
    deals = _book(tmp_path, 3)
    calls = {"n": 0}
    real = cpr_store.build_cpr

    def flaky(rec):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("synthetic first-record crash")
        return real(rec)

    monkeypatch.setattr(cpr_store, "build_cpr", flaky)
    out = str(tmp_path / "cprs")
    res = generate_all(deals, out)
    assert res["failed"] == 1
    assert res["ok"] == 2
    assert res["failures"][0]["parcel"] == "P0", "first failure labeled with its own id"
    assert "synthetic first-record crash" in res["failures"][0]["error"]


def test_later_failures_keep_their_own_ids(tmp_path, monkeypatch):
    deals = _book(tmp_path, 3)
    real = cpr_store.build_cpr

    def flaky(rec):
        if rec["parcel_id"] == "P2":
            raise RuntimeError("boom-P2")
        return real(rec)

    monkeypatch.setattr(cpr_store, "build_cpr", flaky)
    res = generate_all(deals, str(tmp_path / "cprs"))
    assert res["failed"] == 1
    assert res["failures"][0]["parcel"] == "P2"


def test_skipped_counts_unattempted_rows_beyond_limit(tmp_path):
    deals = _book(tmp_path, 5)
    res = generate_all(deals, str(tmp_path / "cprs"), limit=2)
    assert res["total"] == 5
    assert res["ok"] == 2
    assert res["skipped"] == 3


def test_record_without_parcel_id_still_fails_cleanly(tmp_path, monkeypatch):
    path = tmp_path / "deals.json"
    path.write_text(json.dumps([{"address": "1 Main St", "county": "collin"}]),
                    encoding="utf-8")

    def always(rec):
        raise RuntimeError("no id")

    monkeypatch.setattr(cpr_store, "build_cpr", always)
    res = generate_all(str(path), str(tmp_path / "cprs"))
    assert res["failed"] == 1
    assert res["failures"][0]["parcel"].startswith("row-")
