"""Transaction state-machine + statutory calendar regression tests (task D2)."""
from datetime import date, timedelta

import pytest

from src import policy
from src.cpr import CPR, CPRColumn
from src.legal_rules import RedemptionClass
from src.transaction import (
    ALLOWED_TRANSITIONS,
    COLUMN_TO_STATE,
    STATE_TO_COLUMN,
    TransactionState,
    escrow_schedule,
    redemption_deadlines,
    transaction_history,
    transition,
)


def _cpr() -> CPR:
    return CPR(parcel_id="T1", county="collin", situs_address="1 Test Ln")


def _under_contract() -> CPR:
    cpr = _cpr()
    cpr.move_column(CPRColumn.UNDER_CONTRACT, reason="fixture: offer accepted")
    return cpr


# --- Enum / table alignment ------------------------------------------------

def test_states_align_with_cpr_columns():
    """Every transaction state round-trips to exactly one CPRColumn."""
    assert set(STATE_TO_COLUMN) == set(TransactionState)
    for state, col in STATE_TO_COLUMN.items():
        assert COLUMN_TO_STATE[col] is state


def test_every_state_has_a_transition_row():
    assert set(ALLOWED_TRANSITIONS) == set(TransactionState)
    # Terminal states allow nothing.
    assert ALLOWED_TRANSITIONS[TransactionState.DISPOSED] == frozenset()
    assert ALLOWED_TRANSITIONS[TransactionState.DEAD] == frozenset()


# --- transition(): valid paths ---------------------------------------------

def test_full_valid_path_to_disposed():
    cpr = _under_contract()
    transition(cpr, TransactionState.OPTION_PERIOD)
    assert cpr.column == CPRColumn.OPTION_PERIOD
    transition(cpr, TransactionState.TITLE_ESCROW)
    assert cpr.column == CPRColumn.TITLE_ESCROW
    transition(cpr, TransactionState.CLEAR_TO_CLOSE)
    transition(cpr, TransactionState.CLOSED)
    assert cpr.column == CPRColumn.CLOSED
    transition(cpr, TransactionState.DISPOSED)
    assert cpr.column == CPRColumn.DISPOSED
    assert [h["state"] for h in transaction_history(cpr)] == [
        "OPTION_PERIOD", "TITLE_ESCROW", "CLEAR_TO_CLOSE", "CLOSED", "DISPOSED"]


def test_closed_fans_out_to_hold_or_redemption_watch():
    for branch in (TransactionState.REHAB_HOLD, TransactionState.REDEMPTION_WATCH):
        cpr = _under_contract()
        for step in (TransactionState.OPTION_PERIOD, TransactionState.TITLE_ESCROW,
                     TransactionState.CLEAR_TO_CLOSE, TransactionState.CLOSED,
                     branch):
            transition(cpr, step)
        assert cpr.column == STATE_TO_COLUMN[branch]
        if branch is TransactionState.REDEMPTION_WATCH:
            transition(cpr, TransactionState.DEAD)  # lost to a redeeming owner
            assert cpr.dead_reason  # reason chain stamped the DEAD move
        else:
            transition(cpr, TransactionState.DISPOSED)


def test_back_out_of_option_period():
    cpr = _under_contract()
    transition(cpr, TransactionState.OPTION_PERIOD)
    transition(cpr, TransactionState.UNDER_CONTRACT, note="option terminated")
    assert cpr.column == CPRColumn.UNDER_CONTRACT


def test_transition_accepts_string_target_value():
    cpr = _under_contract()
    transition(cpr, "OPTION_PERIOD")
    assert cpr.column == CPRColumn.OPTION_PERIOD


def test_receipts_record_actor_and_keep_chain_intact():
    cpr = _under_contract()
    n0 = len(cpr.ledger)
    transition(cpr, TransactionState.OPTION_PERIOD, actor="auction_agent")
    receipts = cpr.ledger[n0:]
    assert len(receipts) == 2  # column receipt + tx_<STATE> receipt
    tx_receipt = receipts[-1]
    assert tx_receipt.field_name == "tx_OPTION_PERIOD"
    assert tx_receipt.source == "auction_agent"
    # The tx_* marker field carries confidence=1.0 (our own action, not an
    # external fact); the ledger entry itself has no confidence attribute.
    assert cpr.tx_OPTION_PERIOD.confidence == pytest.approx(1.0)
    assert cpr.tx_OPTION_PERIOD.value  # ISO timestamp
    # Hash chain still intact end-to-end.
    prev_hash = ""
    for entry in cpr.ledger:
        assert entry.prev_hash == prev_hash
        assert entry.entry_hash == entry.compute_hash()
        prev_hash = entry.entry_hash


# --- transition(): rejections leave no trace -------------------------------

@pytest.mark.parametrize("target", [
    TransactionState.CLOSED,
    TransactionState.CLEAR_TO_CLOSE,
    TransactionState.REHAB_HOLD,
])
def test_invalid_transition_raises(target):
    cpr = _under_contract()
    n = len(cpr.ledger)
    with pytest.raises(ValueError):
        transition(cpr, target)
    assert cpr.column == CPRColumn.UNDER_CONTRACT  # untouched
    assert len(cpr.ledger) == n                    # wrote nothing


def test_terminal_state_allows_nothing():
    cpr = _under_contract()
    transition(cpr, TransactionState.DEAD, note="lost_at_auction")
    with pytest.raises(ValueError):
        transition(cpr, TransactionState.DISPOSED)
    with pytest.raises(ValueError):
        transition(cpr, TransactionState.UNDER_CONTRACT)


def test_transition_from_preoffer_column_raises():
    """INBOX..OFFER-OUT are not transaction states — loud failure required."""
    for stage in (CPRColumn.INBOX, CPRColumn.ENRICHED, CPRColumn.PRICED,
                  CPRColumn.OFFER_OUT):
        cpr = _cpr()
        cpr.move_column(stage)
        with pytest.raises(ValueError):
            transition(cpr, TransactionState.UNDER_CONTRACT)


def test_unknown_target_raises():
    cpr = _under_contract()
    for bad in ("NOT-A-STATE", "inbox", None, 3.14):
        with pytest.raises(ValueError):
            transition(cpr, bad)


def test_dead_transition_stamps_dead_reason_from_note():
    cpr = _under_contract()
    transition(cpr, TransactionState.DEAD, note="title_fatal")
    assert cpr.column == CPRColumn.DEAD
    assert cpr.dead_reason == "title_fatal"


# --- redemption calendar ----------------------------------------------------

def test_other_class_180_day_and_irs_120_math():
    cal = redemption_deadlines("2026-01-01", RedemptionClass.OTHER)
    assert cal["original_deadline"] == "2026-06-30"   # Jan 1 + 180d
    assert cal["irs_120_day"] == "2026-05-01"         # Jan 1 + 120d


def test_homestead_class_730_day_math():
    cal = redemption_deadlines(date(2026, 1, 1), RedemptionClass.HOMESTEAD_AG)
    assert cal["original_deadline"] == "2028-01-01"   # Jan 1 + 730d (2 years)
    assert cal["irs_120_day"] == "2026-05-01"         # IRS window unchanged


def test_calendar_accepts_parse_date_any_forms_and_class_strings():
    prose = redemption_deadlines(
        "Tuesday, the 3rd day of March, 2026", RedemptionClass.OTHER)
    assert prose["original_deadline"] == "2026-08-30"  # Mar 3 + 180d
    us = redemption_deadlines("7/25/2013", "homestead_ag")  # str value accepted
    assert us["original_deadline"] == (date(2013, 7, 25)
                                       + timedelta(days=730)).isoformat()
    assert us["irs_120_day"] == (date(2013, 7, 25)
                                 + timedelta(days=120)).isoformat()


def test_unknown_redemption_class_raises():
    for bad in ("banana", "residential", None, 180):
        with pytest.raises(ValueError):
            redemption_deadlines("2026-01-01", bad)


def test_unparseable_deed_date_raises():
    for bad in ("Unknown", "", None, "not a date"):
        with pytest.raises(ValueError):
            redemption_deadlines(bad, RedemptionClass.OTHER)


# --- escrow schedule ---------------------------------------------------------

def test_escrow_schedule_uses_policy_defaults():
    sch = escrow_schedule("2026-03-03")
    start = date(2026, 3, 3)
    assert sch["option_end"] == \
        (start + timedelta(days=policy.OPTION_PERIOD_DAYS)).isoformat()
    assert sch["closing_target"] == \
        (start + timedelta(days=policy.OPTION_PERIOD_DAYS
                           + policy.CLOSING_TIMELINE_DAYS)).isoformat()


def test_escrow_schedule_custom_overrides():
    sch = escrow_schedule(date(2026, 3, 3), option_days=14, closing_days=45)
    assert sch["option_end"] == "2026-03-17"
    assert sch["closing_target"] == "2026-05-01"


def test_escrow_schedule_extended_option_constant():
    sch = escrow_schedule("2026-03-03",
                          option_days=policy.OPTION_PERIOD_DAYS_EXTENDED)
    assert sch["option_end"] == "2026-03-17"
    assert sch["closing_target"] == (date(2026, 3, 17)
                                     + timedelta(days=policy.CLOSING_TIMELINE_DAYS)
                                     ).isoformat()


@pytest.mark.parametrize("bad_days", [-1, -7, "soon"])
def test_escrow_schedule_rejects_bad_day_counts(bad_days):
    with pytest.raises(ValueError):
        escrow_schedule("2026-03-03", option_days=bad_days)
    with pytest.raises(ValueError):
        escrow_schedule("2026-03-03", closing_days=bad_days)


def test_escrow_schedule_rejects_unparseable_date():
    with pytest.raises(ValueError):
        escrow_schedule("Unknown")


def test_transaction_receipts_are_timezone_aware():
    """Reviewer HIGH fix: tx_* receipts must carry a UTC offset."""
    from src.cpr import CPR
    cpr = CPR(parcel_id="T", county="collin", situs_address="1 Main St",
              deal_type="DT-AUCTION")
    cpr.move_column(__import__("src.cpr", fromlist=["CPRColumn"]).CPRColumn.UNDER_CONTRACT)
    transition(cpr, TransactionState.OPTION_PERIOD)
    history = transaction_history(cpr)
    assert history and all(h["at"].endswith("+00:00") for h in history)
