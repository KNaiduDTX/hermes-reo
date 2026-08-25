# src/transaction.py
"""AGENT-TX: post-offer transaction state machine + statutory calendars.

Owns everything that happens AFTER an offer is accepted / an auction is won:

* the Kanban lifecycle (contract -> option -> title/escrow -> clear-to-close
  -> closed -> rehab-hold | redemption-watch -> disposed/dead), expressed as a
  :class:`TransactionState` enum aligned 1:1 with the post-offer slice of
  :class:`src.cpr.CPRColumn`, with a whitelisted ``ALLOWED_TRANSITIONS`` table;
* the tax-sale redemption calendar (Tex. Tax Code §34.21 statutory periods via
  ``legal_rules.get_redemption_days``, plus the IRS 120-day federal-tax-lien
  redemption window);
* the escrow milestone schedule driven by the policy constants in
  ``policy.OPTION_PERIOD_DAYS`` / ``policy.CLOSING_TIMELINE_DAYS``.

Receipt design (documented choice): ``CPR.set_field`` OVERWRITES the field
value, so naively "appending history" through it would lose earlier entries.
Each successful :func:`transition` therefore writes TWO append-only receipts
into ``cpr.ledger`` — which never overwrites and is hash-chained:

1. the standard ``column`` receipt emitted by ``CPR.move_column``; and
2. a ``tx_<STATE>`` receipt via ``set_field(f"tx_{state.value}",
   utc_iso_timestamp, actor, 1.0)``: the latest marker per state stays on the
   CPR as a quick-access ProvenanceField, while the FULL ordered history
   remains recoverable from the ledger (see :func:`transaction_history`).
   Confidence is 1.0 because the receipt records our own action, not an
   external fact.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Union

from .cpr import CPR, CPRColumn
from .dates import parse_date_any
from .legal_rules import IRS_REDEMPTION_DAYS, RedemptionClass, get_redemption_days
from . import policy

DateLike = Union[str, date]


class TransactionState(str, Enum):
    """Post-offer lifecycle states, aligned with the transaction CPRColumns."""
    UNDER_CONTRACT = "UNDER_CONTRACT"
    OPTION_PERIOD = "OPTION_PERIOD"
    TITLE_ESCROW = "TITLE_ESCROW"
    CLEAR_TO_CLOSE = "CLEAR_TO_CLOSE"
    CLOSED = "CLOSED"
    REHAB_HOLD = "REHAB_HOLD"
    REDEMPTION_WATCH = "REDEMPTION_WATCH"
    DISPOSED = "DISPOSED"
    DEAD = "DEAD"


# CPRColumn value -> TransactionState (post-offer slice of the Kanban board).
COLUMN_TO_STATE = {
    CPRColumn.UNDER_CONTRACT: TransactionState.UNDER_CONTRACT,
    CPRColumn.OPTION_PERIOD: TransactionState.OPTION_PERIOD,
    CPRColumn.TITLE_ESCROW: TransactionState.TITLE_ESCROW,
    CPRColumn.CLEAR_TO_CLOSE: TransactionState.CLEAR_TO_CLOSE,
    CPRColumn.CLOSED: TransactionState.CLOSED,
    CPRColumn.REHAB_HOLD: TransactionState.REHAB_HOLD,
    CPRColumn.REDEMPTION_WATCH: TransactionState.REDEMPTION_WATCH,
    CPRColumn.DISPOSED: TransactionState.DISPOSED,
    CPRColumn.DEAD: TransactionState.DEAD,
}
STATE_TO_COLUMN = {state: col for col, state in COLUMN_TO_STATE.items()}

# Whitelisted flow. Anything not listed is illegal (loud ValueError).
# UNDER_CONTRACT -> OPTION_PERIOD|DEAD; OPTION_PERIOD may back out to
# UNDER_CONTRACT (option termination); CLOSED fans out to hold /
# redemption-watch / straight dispose; DISPOSED and DEAD are terminal.
ALLOWED_TRANSITIONS = {
    TransactionState.UNDER_CONTRACT: frozenset({
        TransactionState.OPTION_PERIOD,
        TransactionState.DEAD,
    }),
    TransactionState.OPTION_PERIOD: frozenset({
        TransactionState.TITLE_ESCROW,
        TransactionState.UNDER_CONTRACT,  # back-out within option period
        TransactionState.DEAD,
    }),
    TransactionState.TITLE_ESCROW: frozenset({
        TransactionState.CLEAR_TO_CLOSE,
        TransactionState.DEAD,
    }),
    TransactionState.CLEAR_TO_CLOSE: frozenset({
        TransactionState.CLOSED,
        TransactionState.DEAD,
    }),
    TransactionState.CLOSED: frozenset({
        TransactionState.REHAB_HOLD,
        TransactionState.REDEMPTION_WATCH,
        TransactionState.DISPOSED,
    }),
    TransactionState.REHAB_HOLD: frozenset({
        TransactionState.DISPOSED,
    }),
    TransactionState.REDEMPTION_WATCH: frozenset({
        TransactionState.DISPOSED,
        TransactionState.DEAD,
    }),
    TransactionState.DISPOSED: frozenset(),
    TransactionState.DEAD: frozenset(),
}


def _coerce_state(to_state) -> TransactionState:
    """Normalize *to_state* to a TransactionState member or raise ValueError.

    Accepts TransactionState members and, defensively, their string values
    (including CPRColumn members, whose values coincide on this slice).
    """
    if isinstance(to_state, TransactionState):
        return to_state
    if isinstance(to_state, Enum):
        to_state = to_state.value
    try:
        return TransactionState(str(to_state))
    except ValueError:
        raise ValueError(
            f"unknown transaction state {to_state!r}; expected one of "
            f"{[s.value for s in TransactionState]}"
        ) from None


def _current_state(cpr: CPR) -> TransactionState:
    """Map the CPR's current column to its TransactionState or raise ValueError.

    Pre-offer Kanban stages (INBOX..OFFER-OUT) are not transaction states.
    """
    col = cpr.column
    if isinstance(col, str) and not isinstance(col, CPRColumn):
        try:
            col = CPRColumn(col)
        except ValueError:
            col = None
    state = COLUMN_TO_STATE.get(col)
    if state is None:
        raise ValueError(
            f"current column {cpr.column!r} is not a transaction state; "
            f"transition() requires one of "
            f"{sorted(c.value for c in COLUMN_TO_STATE)}"
        )
    return state


def transition(cpr: CPR, to_state: TransactionState, actor: str = "pipeline",
               note: str = "") -> CPR:
    """Move *cpr* to *to_state*, validating against ALLOWED_TRANSITIONS.

    Raises ValueError when the current column is not a transaction state, when
    *to_state* is unknown, or when the move is not whitelisted — a rejected
    transition leaves the CPR untouched. A successful move appends two ledger
    receipts (see module docstring): the ``column`` receipt from
    ``move_column(reason=note or actor)`` and a ``tx_<STATE>`` receipt whose
    source records the actor. Moving to DEAD stamps ``dead_reason`` with the
    reason, mirroring CPR.move_column semantics.

    Returns the same CPR instance for chaining.
    """
    target = _coerce_state(to_state)
    current = _current_state(cpr)
    allowed = ALLOWED_TRANSITIONS[current]
    if target not in allowed:
        raise ValueError(
            f"illegal transition {current.value} -> {target.value}; allowed: "
            f"{sorted(s.value for s in allowed) or 'none (terminal state)'}"
        )
    cpr.move_column(STATE_TO_COLUMN[target], reason=(note or actor))
    cpr.set_field(f"tx_{target.value}", datetime.now(timezone.utc).isoformat(), actor, 1.0)
    return cpr


def transaction_history(cpr: CPR) -> list:
    """Ordered transition receipts recovered from the append-only ledger.

    Returns a list of {state, at, actor} dicts, one per successful
    :func:`transition`, in ledger order (oldest first). This is the full
    history the per-state tx_* fields cannot express by themselves.
    """
    out = []
    for e in cpr.ledger:
        if e.field_name.startswith("tx_"):
            out.append({"state": e.field_name[len("tx_"):],
                        "at": e.new_value, "actor": e.source})
    return out


def _coerce_redemption_class(redemption_class) -> RedemptionClass:
    """Accept RedemptionClass members or their string values; else ValueError.

    Explicit validation is required because legal_rules.get_redemption_days
    silently falls back to the 180-day class for anything unrecognized.
    """
    if isinstance(redemption_class, RedemptionClass):
        return redemption_class
    try:
        return RedemptionClass(str(redemption_class))
    except ValueError:
        raise ValueError(
            f"unknown redemption class {redemption_class!r}; expected one of "
            f"{[c.value for c in RedemptionClass]}"
        ) from None


def redemption_deadlines(deed_recording_date: DateLike,
                         redemption_class) -> dict:
    """Statutory redemption calendar for a recorded tax deed.

    Returns ISO-date strings:
      * ``original_deadline`` — deed recording date + the §34.21 period for
        the class (180 days OTHER / 730 days HOMESTEAD_AG, via
        ``legal_rules.get_redemption_days``);
      * ``irs_120_day`` — deed recording date + 120 days, the IRS
        federal-tax-lien redemption window (runs alongside, not instead of,
        the state period).

    Accepts anything ``parse_date_any`` understands (ISO, M/D/Y, Dallas prose)
    or a date/datetime. Unknown redemption class or unparseable date raises
    ValueError.
    """
    deed_date = parse_date_any(deed_recording_date)
    if deed_date is None:
        raise ValueError(
            f"unparseable deed_recording_date: {deed_recording_date!r}"
        )
    cls = _coerce_redemption_class(redemption_class)
    return {
        "original_deadline": (
            deed_date + timedelta(days=get_redemption_days(cls))
        ).isoformat(),
        "irs_120_day": (
            deed_date + timedelta(days=IRS_REDEMPTION_DAYS)
        ).isoformat(),
    }


def _coerce_days(value, name: str) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative int, got {value!r}") from None
    if iv < 0:
        raise ValueError(f"{name} must be >= 0, got {iv}")
    return iv


def escrow_schedule(contract_date: DateLike, option_days: Optional[int] = None,
                    closing_days: Optional[int] = None) -> dict:
    """Escrow milestones from the contract effective date.

    Returns ISO-date strings:
      * ``option_end`` — contract date + option_days (default
        ``policy.OPTION_PERIOD_DAYS``);
      * ``closing_target`` — option_end + closing_days (default
        ``policy.CLOSING_TIMELINE_DAYS``), i.e. the closing clock starts when
        the option expires; with policy defaults that is option+7 then
        close+7 = 14 days from the effective date. Pass
        ``policy.OPTION_PERIOD_DAYS_EXTENDED`` or up to
        ``policy.CLOSING_TIMELINE_DAYS_MAX`` to stretch either leg (the MAX is
        an upstream guardrail, not re-enforced here).

    Unparseable contract date or negative/non-numeric day counts raise
    ValueError.
    """
    start = parse_date_any(contract_date)
    if start is None:
        raise ValueError(f"unparseable contract_date: {contract_date!r}")
    od = (_coerce_days(option_days, "option_days") if option_days is not None
          else policy.OPTION_PERIOD_DAYS)
    cd = (_coerce_days(closing_days, "closing_days") if closing_days is not None
          else policy.CLOSING_TIMELINE_DAYS)
    option_end = start + timedelta(days=od)
    return {
        "option_end": option_end.isoformat(),
        "closing_target": (option_end + timedelta(days=cd)).isoformat(),
    }


if __name__ == "__main__":
    # Self-check 1: happy path — auction win -> closed -> disposed.
    cpr = CPR(parcel_id="R777", county="collin",
              situs_address="900 Auction Way, McKinney, TX 75069")
    cpr.move_column(CPRColumn.UNDER_CONTRACT, reason="won_trustee_sale")
    transition(cpr, TransactionState.OPTION_PERIOD, actor="auction_agent",
               note="premium paid, title run started")
    transition(cpr, TransactionState.TITLE_ESCROW, actor="title_co")
    transition(cpr, TransactionState.CLEAR_TO_CLOSE, actor="title_co",
               note="clean commitment")
    transition(cpr, TransactionState.CLOSED, actor="escrow_officer",
               note="funded + recorded")
    transition(cpr, TransactionState.DISPOSED, actor="dispo",
               note="retail flip closed")
    assert cpr.column == CPRColumn.DISPOSED
    assert [h["state"] for h in transaction_history(cpr)] == [
        "OPTION_PERIOD", "TITLE_ESCROW", "CLEAR_TO_CLOSE", "CLOSED", "DISPOSED"]
    assert all(h["actor"] for h in transaction_history(cpr))
    # Ledger chain intact across every receipt we appended (2 per transition).
    prev_hash = ""
    for entry in cpr.ledger:
        assert entry.prev_hash == prev_hash
        assert entry.entry_hash == entry.compute_hash()
        prev_hash = entry.entry_hash

    # Self-check 2: invalid transitions raise loudly and leave no trace.
    c2 = CPR(parcel_id="R778", county="collin", situs_address="10 Test Ln")
    c2.move_column(CPRColumn.UNDER_CONTRACT)
    n_ledger = len(c2.ledger)
    for bad_target in (TransactionState.CLOSED, TransactionState.DISPOSED,
                       TransactionState.TITLE_ESCROW):
        try:
            transition(c2, bad_target)
            raise AssertionError(f"expected ValueError for {bad_target}")
        except ValueError:
            pass
    assert c2.column == CPRColumn.UNDER_CONTRACT
    assert len(c2.ledger) == n_ledger  # rejected moves wrote nothing
    # Back-out path IS legal: OPTION_PERIOD -> UNDER_CONTRACT.
    transition(c2, TransactionState.OPTION_PERIOD)
    transition(c2, TransactionState.UNDER_CONTRACT, note="buyer backed out")
    assert c2.column == CPRColumn.UNDER_CONTRACT
    # Terminal states allow nothing.
    for terminal in (TransactionState.DISPOSED, TransactionState.DEAD):
        try:
            transition(cpr, terminal)
            raise AssertionError(f"expected ValueError from terminal {terminal}")
        except ValueError:
            pass
    # Unknown target value raises.
    try:
        transition(c2, "NOT-A-STATE")
        raise AssertionError("expected ValueError for unknown target")
    except ValueError:
        pass
    # Current column unmapped (pre-offer stage) raises.
    c3 = CPR(parcel_id="R779", county="collin", situs_address="20 Test Ln")
    assert c3.column == CPRColumn.INBOX
    try:
        transition(c3, TransactionState.UNDER_CONTRACT)
        raise AssertionError("expected ValueError for INBOX column")
    except ValueError:
        pass
    # DEAD transition stamps dead_reason via the reason chain.
    c4 = CPR(parcel_id="R780", county="collin", situs_address="30 Test Ln")
    c4.move_column(CPRColumn.UNDER_CONTRACT)
    transition(c4, TransactionState.DEAD, note="redemption_risk")
    assert c4.dead_reason == "redemption_risk"
    assert c4.column == CPRColumn.DEAD

    # Self-check 3: calendar math vs known dates.
    cal_other = redemption_deadlines("2026-01-01", RedemptionClass.OTHER)
    assert cal_other["original_deadline"] == "2026-06-30"  # +180d
    assert cal_other["irs_120_day"] == "2026-05-01"        # +120d
    cal_homestead = redemption_deadlines(date(2026, 1, 1),
                                         RedemptionClass.HOMESTEAD_AG)
    assert cal_homestead["original_deadline"] == "2028-01-01"  # +730d
    assert cal_homestead["irs_120_day"] == "2026-05-01"
    # parse_date_any forms flow through (Dallas prose + US numeric).
    cal_prose = redemption_deadlines(
        "Tuesday, the 3rd day of March, 2026", RedemptionClass.OTHER)
    assert cal_prose["original_deadline"] == "2026-08-30"
    cal_us = redemption_deadlines("7/25/2013", "homestead_ag")
    assert cal_us["original_deadline"] == "2015-07-25"  # +730d, no leap day in span
    # Unknown class + unparseable date raise.
    for bad_cls in ("banana", None, 123):
        try:
            redemption_deadlines("2026-01-01", bad_cls)
            raise AssertionError(f"expected ValueError for class {bad_cls!r}")
        except ValueError:
            pass
    try:
        redemption_deadlines("Unknown", RedemptionClass.OTHER)
        raise AssertionError("expected ValueError for unparseable date")
    except ValueError:
        pass

    # Self-check 4: escrow schedule defaults + overrides.
    sch = escrow_schedule("2026-03-03")
    assert sch["option_end"] == "2026-03-10"       # policy 7d option
    assert sch["closing_target"] == "2026-03-17"   # then 7d close
    sch_ext = escrow_schedule(date(2026, 3, 3),
                              option_days=policy.OPTION_PERIOD_DAYS_EXTENDED,
                              closing_days=30)
    assert sch_ext["option_end"] == "2026-03-17"
    assert sch_ext["closing_target"] == "2026-04-16"
    assert escrow_schedule("2026-03-03", 0, 0) == {
        "option_end": "2026-03-03", "closing_target": "2026-03-03"}
    for bad in (-1, "soon"):
        try:
            escrow_schedule("2026-03-03", option_days=bad)
            raise AssertionError(f"expected ValueError for option_days={bad!r}")
        except ValueError:
            pass
    try:
        escrow_schedule("not a date")
        raise AssertionError("expected ValueError for bad contract_date")
    except ValueError:
        pass

    print("transaction.py: all self-checks passed")
