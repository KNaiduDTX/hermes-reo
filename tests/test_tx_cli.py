"""Audit ORCH-1/F8: the transaction state machine finally has a production
surface (pipeline --tx-status / --tx-advance). Pins the CLI behavior:
current-state readout, whitelisted-transition enforcement, atomic write-back,
and honest failure codes. Hermetic via monkeypatched CPR location.
"""
import json
from pathlib import Path

import pytest

import pipeline as pl
from src.cpr import CPR, CPRColumn


def _make_cpr_file(tmp_path, column):
    cpr = CPR(parcel_id="TX1", county="collin",
              situs_address="1 Main St", deal_type="DT-AUCTION")
    if column:
        cpr.move_column(CPRColumn(column), reason="seed")
    path = tmp_path / "cpr_TX1.json"
    path.write_text(json.dumps(cpr.to_dict(), default=str), encoding="utf-8")
    return str(path)


@pytest.fixture
def seed_cpr(tmp_path, monkeypatch):
    def _seed(column="UNDER-CONTRACT"):
        p = _make_cpr_file(tmp_path, column)
        monkeypatch.setattr(pl, "_locate_cpr_file",
                            lambda pid: p if pid == "TX1" else None)
        return p
    return _seed


def test_status_reports_current_state(seed_cpr, capsys):
    seed_cpr()
    assert pl.run_tx_status("TX1") == pl.EXIT_OK
    out = capsys.readouterr().out
    assert "UNDER_CONTRACT" in out
    assert "Allowed next" in out


def test_status_missing_cpr_exits_data_missing(monkeypatch):
    monkeypatch.setattr(pl, "_locate_cpr_file", lambda pid: None)
    assert pl.run_tx_status("NOPE") == pl.EXIT_DATA_MISSING


def test_advance_applies_whitelisted_transition_and_writes_back(seed_cpr):
    path = seed_cpr()
    rc = pl.run_tx_advance("TX1", "OPTION_PERIOD", note="test-actor")
    assert rc == pl.EXIT_OK
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    assert raw["column"] == "OPTION-PERIOD"


def test_illegal_transition_refused_and_file_untouched(seed_cpr):
    path = seed_cpr("UNDER-CONTRACT")
    before = Path(path).read_text(encoding="utf-8")
    rc = pl.run_tx_advance("TX1", "CLOSED", note="test")  # not whitelisted
    assert rc == pl.EXIT_USAGE
    after = Path(path).read_text(encoding="utf-8")
    assert json.loads(after)["column"] == "UNDER-CONTRACT"


def test_unknown_state_exits_usage(seed_cpr):
    seed_cpr()
    assert pl.run_tx_advance("TX1", "DT-NONSENSE", "t") == pl.EXIT_USAGE


def test_advance_requires_a_transaction_state_column(seed_cpr):
    """PRICED (a pre-offer column) is not a transaction state - the CLI
    refuses instead of inventing a lifecycle."""
    seed_cpr("PRICED")
    assert pl.run_tx_advance("TX1", "OPTION_PERIOD", "t") == pl.EXIT_USAGE


def test_status_reports_transition_history(seed_cpr, capsys):
    path = seed_cpr()
    pl.run_tx_advance("TX1", "OPTION_PERIOD", note="test-actor")
    capsys.readouterr()
    assert pl.run_tx_status("TX1") == pl.EXIT_OK
    out = capsys.readouterr().out
    assert "OPTION_PERIOD" in out and "test-actor" in out
