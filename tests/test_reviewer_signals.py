# tests/test_reviewer_signals.py
"""The reviewer's eight fake-signal checks, pinned as pytest cases (FN5).

These are ports of the self-check scenarios embedded in ``src/reviewer.py``
(``if __name__ == "__main__"``) — the audit found them running only when the
module was executed directly, so the 8-check harness sat at 0% coverage.
Each check family gets parametrized cases over its accept/reject boundaries,
plus ``run_review`` aggregation verdicts.

Checks under test (spec §12.5):
  1. fabricated_confidence   5. stale_notice        (incl. legacy date formats
  2. assessed_laundering     6. disclosure_gap       via src.dates.parse_date_any)
  3. optimistic_only         7. redemption_blindspot
  4. missing_surviving_lien  8. identity_mismatch    (ProvenanceField-"Unknown")
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.cpr import (
    CPR,
    CPRColumn,
    LienRecord,
    ProvenanceField,
    ValuationObject,
)
from src.dates import parse_date_any
from src.legal_rules import DealType
from src.pricing import (
    PENCIL_BASE,
    PENCIL_NEVER,
    PENCIL_OPTIMISTIC_ONLY,
    PricingResult,
)
from src.reviewer import (
    check_assessed_laundering,
    check_disclosure_gap,
    check_fabricated_confidence,
    check_identity_mismatch,
    check_missing_surviving_lien,
    check_optimistic_only,
    check_redemption_blindspot,
    check_stale_notice,
    run_review,
)
from src.valuation import GateValResult


# --- fixtures/helpers ------------------------------------------------------

ALL_CHECK_NAMES = {
    "fabricated_confidence",
    "assessed_laundering",
    "optimistic_only",
    "missing_surviving_lien",
    "stale_notice",
    "disclosure_gap",
    "redemption_blindspot",
    "identity_mismatch",
}

STALE_MARKER = "unknown-sale-date-manual-verification-required"


def _today():
    return datetime.now(timezone.utc).date()


def _iso(days_from_today):
    """Sale date N days from today, ISO form."""
    return (_today() + timedelta(days=days_from_today)).isoformat()


def _prose(days_from_today):
    """Sale date N days from today in the Dallas prose form.

    e.g. 'Tuesday, the 3rd day of March, 2026' — the form the enriched
    pipeline produces and the legacy ISO-only strptime never matched (X4).
    """
    d = _today() + timedelta(days=days_from_today)
    if d.day % 100 in (11, 12, 13):
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    return "{weekday}, the {day}{suffix} day of {month}, {year}".format(
        weekday=d.strftime("%A"), day=d.day, suffix=suffix,
        month=d.strftime("%B"), year=d.year,
    )


def _cpr(**overrides):
    """CPR with all three identity fields populated (passes check #8)."""
    fields = dict(
        parcel_id="R-TEST",
        county="collin",
        situs_address="100 Test St",
        legal_description="Lot 1, Block A",
        deal_type="DT-AUCTION",
    )
    fields.update(overrides)
    return CPR(**fields)


def _gate(confidence, comp_source, comp_count):
    return GateValResult(
        verdict="pass", reason="ok",
        confidence=confidence, comp_source=comp_source,
        comp_count=comp_count, floor=0.60,
    )


def _pricing(pencil_status=PENCIL_BASE):
    return PricingResult(
        deal_type="DT-AUCTION", mao=180_000, bid_ceiling=175_000,
        offer_ladder={"opening": 153_000, "target": 180_000,
                      "walk_away": 175_000},
        pencil_status=pencil_status,
    )


def _attach_valuation(cpr, arv, source, confidence):
    cpr.valuation = ValuationObject(
        arv=ProvenanceField(value=arv, source=source, confidence=confidence),
        comp_source=source,
    )
    return cpr


# --- 1. fabricated_confidence ----------------------------------------------

@pytest.mark.parametrize(
    "comp_source, confidence, comp_count, expect_reject, needle",
    [
        # Ported bad-deal scenario: vendor AVM claiming high confidence.
        ("vendor_avm", 0.90, 3, True, "vendor AVM"),
        ("vendor_avm", 0.95, 12, True, "vendor AVM"),
        # High confidence but too few comps — insufficient regardless of source.
        ("mls_agent", 0.95, 3, True, "only 3 comps"),
        ("appraiser_bpo", 0.91, 2, True, "only 2 comps"),
        # Healthy: MLS comps, supported confidence, enough comps.
        ("mls_agent", 0.85, 5, False, "supported"),
        ("title_feed", 0.80, 4, False, "supported"),
    ],
)
def test_fabricated_confidence(comp_source, confidence, comp_count,
                               expect_reject, needle):
    cpr = _attach_valuation(_cpr(), 300_000, comp_source, confidence)
    gate = _gate(confidence, comp_source, comp_count)

    check = check_fabricated_confidence(cpr, gate)

    assert check.check_name == "fabricated_confidence"
    assert (check.verdict == "reject") is expect_reject
    assert needle in check.reason


@pytest.mark.parametrize(
    "cpr_factory",
    [
        lambda: _cpr(),                                   # no valuation at all
        lambda: _attach_valuation(_cpr(), 300_000, "mls_agent", 0.85),  # no gate
    ],
    ids=["no_valuation", "valuation_but_no_gate"],
)
def test_fabricated_confidence_passes_without_inputs(cpr_factory):
    check = check_fabricated_confidence(cpr_factory(), None)
    assert check.verdict == "pass"
    assert "No valuation to check" in check.reason


# --- 2. assessed_laundering -------------------------------------------------

@pytest.mark.parametrize(
    "arv, cad_value, source, expect_reject, needle",
    [
        # Rejected consumer/assessed sources never wear a comp costume.
        (730_000, None, "zestimate", True, "rejected"),
        (730_000, None, "consumer_avm", True, "rejected"),
        (730_000, None, "assessed_value", True, "rejected"),
        # ARV within 2% of CAD with a non-whitelisted source = laundering.
        (731_000, 730_673, "vendor_avm", True, "laundering"),
        (744_000, 730_673, "title_feed", True, "laundering"),
        # Same closeness but agent/BPO sources are allowed to agree with CAD.
        (731_000, 730_673, "mls_agent", False, "No laundering"),
        (731_000, 730_673, "appraiser_bpo", False, "No laundering"),
        # Far from CAD — no laundering signal.
        (300_000, 730_673, "vendor_avm", False, "No laundering"),
    ],
)
def test_assessed_laundering(arv, cad_value, source, expect_reject, needle):
    cpr = _attach_valuation(_cpr(), arv, source, 0.70)
    if cad_value is not None:
        cpr.set_field("cad_appraised_value", cad_value, "collin_cad", 0.90)

    check = check_assessed_laundering(cpr)

    assert check.check_name == "assessed_laundering"
    assert (check.verdict == "reject") is expect_reject
    assert needle in check.reason


def test_assessed_laundering_passes_without_valuation():
    assert check_assessed_laundering(_cpr()).verdict == "pass"


# --- 3. optimistic_only -----------------------------------------------------

@pytest.mark.parametrize(
    "pencil_status, expect_reject",
    [
        (PENCIL_OPTIMISTIC_ONLY, True),
        (PENCIL_BASE, False),
        (PENCIL_NEVER, False),  # worse than optimistic-only, but not THIS tell
    ],
)
def test_optimistic_only(pencil_status, expect_reject):
    check = check_optimistic_only(_cpr(), _pricing(pencil_status))
    assert check.check_name == "optimistic_only"
    assert (check.verdict == "reject") is expect_reject


def test_optimistic_only_passes_without_pricing():
    assert check_optimistic_only(_cpr(), None).verdict == "pass"


# --- 4. missing_surviving_lien ----------------------------------------------

@pytest.mark.parametrize(
    "column, liens_attached, expect_reject",
    [
        (CPRColumn.PRICED, False, True),
        (CPRColumn.OFFER_OUT, False, True),
        (CPRColumn.UNDER_CONTRACT, False, True),
        (CPRColumn.PRICED, True, False),
        (CPRColumn.OFFER_OUT, True, False),
        (CPRColumn.INBOX, False, False),  # not priced yet — no requirement
        (CPRColumn.ENRICHED, False, False),
    ],
)
def test_missing_surviving_lien(column, liens_attached, expect_reject):
    cpr = _cpr()
    if liens_attached:
        cpr.add_lien(LienRecord(lien_type="mortgage", amount=200_000,
                                priority=1, survives_foreclosure=False), "test")
    cpr.move_column(column)

    check = check_missing_surviving_lien(cpr)

    assert check.check_name == "missing_surviving_lien"
    assert (check.verdict == "reject") is expect_reject
    if expect_reject:
        assert "GATE-LEGAL-LIEN" in check.reason


# --- 5. stale_notice (incl. legacy date formats via parse_date_any) ---------

@pytest.mark.parametrize(
    "sale_raw, expect_reject, needle",
    [
        # No sale date set at all — live-by-default contract.
        pytest.param(None, False, "", id="no-sale-date-at-all"),
        pytest.param("LEGACY_US_PAST", True, "in the past",
                     id="us-format-past-7-25-2013"),
        pytest.param("PROSE_FUTURE", False, "current",
                     id="dallas-prose-future"),
        pytest.param("PROSE_PAST", True, "in the past",
                     id="dallas-prose-past"),
        pytest.param("Unknown", True, STALE_MARKER,
                     id="unknown-sentinel-rejected-not-current"),
        pytest.param("sometime soon-ish", True, STALE_MARKER,
                     id="unparsable-rejected-not-current"),
        pytest.param("", True, STALE_MARKER,
                     id="empty-string-rejected-not-current"),
    ],
)
def test_stale_notice(sale_raw, expect_reject, needle):
    resolved = {
        "LEGACY_US_PAST": "7/25/2013",      # legacy US format, always stale
        "PROSE_FUTURE": _prose(90),
        "PROSE_PAST": _prose(-400),
        "Unknown": "Unknown",
        "sometime soon-ish": "sometime soon-ish",
        "": "",
    }.get(sale_raw, sale_raw)

    cpr = _cpr()
    if resolved is not None:
        cpr.set_field("sale_date", resolved, "notice-source", 0.9)

    check = check_stale_notice(cpr)

    assert check.check_name == "stale_notice"
    assert (check.verdict == "reject") is expect_reject
    if needle:
        assert needle in check.reason


def test_stale_notice_future_iso_sale_date_passes():
    cpr = _cpr()
    cpr.set_field("sale_date", _iso(45), "collin_foreclosure", 1.0)
    check = check_stale_notice(cpr)
    assert check.verdict == "pass"


def test_stale_notice_prose_dates_parse_through_parse_date_any():
    """The prose forms fed to the checker must be machine-parseable (X4)."""
    assert parse_date_any(_prose(90)) is not None
    assert parse_date_any(_prose(-400)) is not None
    assert parse_date_any(_prose(-400)) < _today()


def test_stale_notice_unset_field_reads_as_live_but_none_sentinel_flags():
    # Field never set -> ProvenanceField is None -> current contract passes.
    unset = check_stale_notice(_cpr())
    assert unset.verdict == "pass"
    # Field explicitly None -> unparsable -> flagged for manual verification.
    explicit_none = _cpr()
    explicit_none.set_field("sale_date", None, "broken-export", 0.5)
    flagged = check_stale_notice(explicit_none)
    assert flagged.verdict == "reject"
    assert STALE_MARKER in flagged.reason


# --- 6. disclosure_gap -------------------------------------------------------

@pytest.mark.parametrize(
    "deal_type, has_disclosure, expect_reject",
    [
        (DealType.WHOLESALE.value, False, True),
        (DealType.WHOLESALE.value, True, False),
        (DealType.AUCTION.value, False, False),
        (DealType.PREFORE.value, False, False),
        ("", False, False),
    ],
)
def test_disclosure_gap(deal_type, has_disclosure, expect_reject):
    cpr = _cpr(deal_type=deal_type)

    check = check_disclosure_gap(cpr, has_disclosure=has_disclosure)

    assert check.check_name == "disclosure_gap"
    assert (check.verdict == "reject") is expect_reject
    if expect_reject:
        assert "GATE-LEGAL-WHOLESALE" in check.reason
        assert "bilateral disclosure" in check.reason.lower()


# --- 7. redemption_blindspot -------------------------------------------------

@pytest.mark.parametrize(
    "deal_type, making_improvements, expect_reject",
    [
        (DealType.TAXDEED.value, True, True),
        (DealType.TAXDEED.value, False, False),
        (DealType.AUCTION.value, True, False),
        (DealType.PREFORE.value, True, False),
    ],
)
def test_redemption_blindspot(deal_type, making_improvements, expect_reject):
    cpr = _cpr(deal_type=deal_type)

    check = check_redemption_blindspot(cpr,
                                       making_improvements=making_improvements)

    assert check.check_name == "redemption_blindspot"
    assert (check.verdict == "reject") is expect_reject
    if expect_reject:
        assert "redemption window" in check.reason.lower()


# --- 8. identity_mismatch ----------------------------------------------------

@pytest.mark.parametrize(
    "overrides, expect_reject, needles",
    [
        # Fully populated identity passes.
        ({}, False, ["All 3 identity fields populated"]),
        # Whitespace-only padding around a real legal description is populated.
        ({"legal_description": "  Lot 3  "}, False, []),
        ({"parcel_id": ""}, True, ["parcel_id", "(2/3 populated)"]),
        ({"legal_description": "Unknown"}, True,
         ["legal_description", "(2/3 populated)"]),
        ({"legal_description": ""}, True,
         ["legal_description", "(2/3 populated)"]),
        ({"situs_address": ""}, True, ["situs_address"]),
        # X8 regression: ProvenanceField-wrapped "Unknown" must NOT read as a
        # populated object-vs-string pass.
        ({"situs_address": "PF_UNKNOWN"}, True,
         ["situs_address", "(2/3 populated)"]),
    ],
)
def test_identity_mismatch(overrides, expect_reject, needles):
    resolved = {}
    for key, value in overrides.items():
        resolved[key] = (ProvenanceField(value="Unknown", source="clerk-test")
                         if value == "PF_UNKNOWN" else value)
    cpr = _cpr(**resolved)

    check = check_identity_mismatch(cpr)

    assert check.check_name == "identity_mismatch"
    assert (check.verdict == "reject") is expect_reject
    for needle in needles:
        assert needle in check.reason


# --- run_review aggregation verdicts ----------------------------------------

def test_run_review_clean_deal_passes_all_eight_checks():
    """Ported clean scenario from reviewer.__main__: overall pass, 8 checks."""
    cpr = _attach_valuation(_cpr(parcel_id="R123"), 300_000, "mls_agent", 0.85)
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=200_000, priority=1,
                            survives_foreclosure=False), "test")
    cpr.set_field("sale_date", _iso(45), "test", 1.0)
    cpr.move_column(CPRColumn.PRICED)

    result = run_review(cpr, _gate(0.85, "mls_agent", 5), _pricing())

    assert result.overall == "pass"
    assert result.rejection_reasons == []
    assert len(result.checks) == 8
    assert {c.check_name for c in result.checks} == ALL_CHECK_NAMES
    assert all(c.verdict == "pass" for c in result.checks)


def test_run_review_bad_vendor_avm_deal_rejects():
    """Ported bad-deal scenario: high-confidence vendor AVM gets caught."""
    cpr = _attach_valuation(_cpr(parcel_id="R456"), 300_000, "vendor_avm", 0.90)
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=200_000, priority=1,
                            survives_foreclosure=False), "test")
    cpr.set_field("sale_date", _iso(45), "test", 1.0)
    cpr.move_column(CPRColumn.PRICED)

    result = run_review(cpr, _gate(0.90, "vendor_avm", 3), _pricing())

    assert result.overall == "reject"
    assert len(result.rejection_reasons) >= 1
    assert any("vendor AVM" in r for r in result.rejection_reasons)


def test_run_review_assessed_laundering_surfaces_in_reasons():
    """Ported laundering scenario: ARV within 2% of CAD on vendor AVM."""
    cpr = _attach_valuation(_cpr(parcel_id="R789"), 730_000, "vendor_avm", 0.70)
    cpr.set_field("cad_appraised_value", 730_673, "collin_cad", 0.90)
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=300_000, priority=1,
                            survives_foreclosure=False), "test")
    cpr.set_field("sale_date", _iso(45), "test", 1.0)
    cpr.move_column(CPRColumn.PRICED)

    result = run_review(cpr)

    assert result.overall == "reject"
    assert any("laundering" in r.lower() for r in result.rejection_reasons)


def test_run_review_wholesale_without_disclosure_rejects_via_aggregation():
    cpr = _cpr(deal_type=DealType.WHOLESALE.value)

    result = run_review(cpr)

    assert result.overall == "reject"
    assert any("GATE-LEGAL-WHOLESALE" in r for r in result.rejection_reasons)


def test_run_review_minimal_unvalued_record_passes_everything():
    """No valuation/pricing/liens/sale-date: every check reads as pass."""
    result = run_review(_cpr())

    assert result.overall == "pass"
    assert result.rejection_reasons == []
    assert {c.check_name for c in result.checks} == ALL_CHECK_NAMES
