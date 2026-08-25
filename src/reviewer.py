# src/reviewer.py
"""AGENT-REV: adversarial fake-signal harness (spec §12.5).

Runs on every stage output. Any check can block promotion.
The 8 checks are the system's core edge in a non-disclosure data environment.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from .cpr import CPR, CPRColumn
from .dates import parse_date_any
from .valuation import GateValResult
from .pricing import PricingResult
from .legal_rules import is_comp_source_rejected, is_comp_source_approved


@dataclass
class ReviewCheck:
    check_name: str
    verdict: str   # "pass", "reject"
    reason: str


@dataclass
class ReviewResult:
    overall: str  # "pass", "reject"
    checks: list[ReviewCheck] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)


# 1. Fabricated-confidence ARV
def check_fabricated_confidence(cpr: CPR, gate: GateValResult = None) -> ReviewCheck:
    """Claimed confidence not supported by comp provenance/recency/proximity."""
    if not cpr.valuation or not gate:
        return ReviewCheck("fabricated_confidence", "pass", "No valuation to check")

    if gate.confidence > 0.8 and gate.comp_source == "vendor_avm":
        return ReviewCheck("fabricated_confidence", "reject",
                           "High confidence claimed on vendor AVM — insufficient rigor")
    if gate.confidence > 0.9 and gate.comp_count < 4:
        return ReviewCheck("fabricated_confidence", "reject",
                           f"High confidence with only {gate.comp_count} comps — insufficient")

    return ReviewCheck("fabricated_confidence", "pass", "Confidence supported by comp quality")


# 2. Assessed-value/Zestimate laundering
def check_assessed_laundering(cpr: CPR) -> ReviewCheck:
    """ARV that traces back to CAD or consumer AVM wearing a comp costume."""
    if not cpr.valuation:
        return ReviewCheck("assessed_laundering", "pass", "No valuation to check")

    source = cpr.valuation.comp_source
    if is_comp_source_rejected(source):
        return ReviewCheck("assessed_laundering", "reject",
                           f"Comp source '{source}' is explicitly rejected (assessed/AVM laundering)")

    # Check if ARV suspiciously close to CAD appraised value
    if cpr.valuation.arv and cpr.cad_appraised_value:
        arv = cpr.valuation.arv.value
        cad = cpr.cad_appraised_value.value
        if cad and abs(arv - cad) / cad < 0.02:  # within 2% of CAD
            if source not in ("mls_agent", "appraiser_bpo"):
                return ReviewCheck("assessed_laundering", "reject",
                                   f"ARV ${arv:,.0f} within 2% of CAD ${cad:,.0f} — "
                                   f"likely assessed-value laundering (source: {source})")

    return ReviewCheck("assessed_laundering", "pass", "No laundering detected")


# 3. Optimistic-only pencil
def check_optimistic_only(cpr: CPR, pricing: PricingResult = None) -> ReviewCheck:
    """Deal profitable only at the top of the ARV/rehab sensitivity grid."""
    if not pricing:
        return ReviewCheck("optimistic_only", "pass", "No pricing to check")

    if pricing.optimistic_only_flag:
        return ReviewCheck("optimistic_only", "reject",
                           "Deal only pencils at optimistic ARV — fake-signal tell")

    return ReviewCheck("optimistic_only", "pass", "Deal pencils at or below base assumptions")


# 4. Missing surviving lien
def check_missing_surviving_lien(cpr: CPR) -> ReviewCheck:
    """Priced/bid without GATE-LEGAL-LIEN clearance."""
    if cpr.column in (CPRColumn.PRICED, CPRColumn.OFFER_OUT, CPRColumn.UNDER_CONTRACT):
        if not cpr.liens:
            return ReviewCheck("missing_surviving_lien", "reject",
                               "Priced without lien search — GATE-LEGAL-LIEN not run")

    return ReviewCheck("missing_surviving_lien", "pass", "Lien stack present")


# 5. Stale notice
def check_stale_notice(cpr: CPR) -> ReviewCheck:
    """Trustee/tax-sale data past the sale date or removed at source, treated as live."""
    if not cpr.sale_date:
        return ReviewCheck("stale_notice", "pass", "No sale date to check")

    # X4 fix: parse through src.dates.parse_date_any — the old ISO-only
    # strptime never matched the enriched formats ("7/25/2013", Dallas prose),
    # so real stale notices always read as "current". Unparsable dates are
    # flagged for manual verification, NEVER reported current.
    sale_raw = cpr.pv_value(cpr.sale_date)
    parsed = parse_date_any(sale_raw)
    if parsed is None:
        return ReviewCheck(
            "stale_notice", "reject",
            f"unknown-sale-date-manual-verification-required — sale date {sale_raw!r} "
            "is missing or unparseable; manual verification required before treating "
            "the notice as live",
        )

    today = datetime.now(timezone.utc).date()
    if parsed < today:
        return ReviewCheck("stale_notice", "reject",
                           f"Sale date {parsed.isoformat()} is in the past — stale notice")

    return ReviewCheck("stale_notice", "pass", "Sale date is current")


# 6. Disclosure gap
def check_disclosure_gap(cpr: CPR, has_disclosure: bool = False) -> ReviewCheck:
    """Wholesale action without bilateral disclosure attached."""
    from .legal_rules import DealType
    if cpr.deal_type != DealType.WHOLESALE.value:
        return ReviewCheck("disclosure_gap", "pass", "Not a wholesale deal")

    if not has_disclosure:
        return ReviewCheck("disclosure_gap", "reject",
                           "Wholesale deal without bilateral disclosure — GATE-LEGAL-WHOLESALE failure")

    return ReviewCheck("disclosure_gap", "pass", "Disclosure attached")


# 7. Redemption blindspot
def check_redemption_blindspot(cpr: CPR, making_improvements: bool = False) -> ReviewCheck:
    """Improvements/exit modeled during an active redemption window."""
    from .legal_rules import DealType
    if cpr.deal_type != DealType.TAXDEED.value:
        return ReviewCheck("redemption_blindspot", "pass", "Not a tax-deed deal")

    if making_improvements:
        return ReviewCheck("redemption_blindspot", "reject",
                           "Irreversible improvements during redemption window — prohibited")

    return ReviewCheck("redemption_blindspot", "pass", "No improvements during redemption")


# 8. Identity mismatch
def check_identity_mismatch(cpr: CPR) -> ReviewCheck:
    """CAD/clerk/tax records not reconciled to the same parcel.

    X8 fix: fields are unwrapped with cpr.pv_value before comparison —
    comparing a ProvenanceField object against "Unknown" was always True, so
    unpopulated identity read as a false PASS. All three identity fields
    (parcel_id AND situs_address AND legal_description) must be populated.
    """
    identity = {
        "parcel_id": cpr.pv_value(cpr.parcel_id),
        "situs_address": cpr.pv_value(cpr.situs_address),
        "legal_description": cpr.pv_value(cpr.legal_description),
    }

    def _populated(v) -> bool:
        if v is None:
            return False
        if isinstance(v, str):
            return v.strip() not in ("", "Unknown")
        return True

    missing = sorted(k for k, v in identity.items() if not _populated(v))
    if missing:
        populated_count = len(identity) - len(missing)
        return ReviewCheck(
            "identity_mismatch", "reject",
            f"Missing/unpopulated identity fields: {', '.join(missing)} "
            f"({populated_count}/3 populated) — parcel identity uncertain",
        )

    return ReviewCheck("identity_mismatch", "pass", "All 3 identity fields populated")


def run_review(cpr: CPR, gate_val: GateValResult = None,
               pricing: PricingResult = None,
               has_disclosure: bool = False,
               making_improvements: bool = False) -> ReviewResult:
    """Run all 8 fake-signal checks on a CPR."""
    checks = [
        check_fabricated_confidence(cpr, gate_val),
        check_assessed_laundering(cpr),
        check_optimistic_only(cpr, pricing),
        check_missing_surviving_lien(cpr),
        check_stale_notice(cpr),
        check_disclosure_gap(cpr, has_disclosure),
        check_redemption_blindspot(cpr, making_improvements),
        check_identity_mismatch(cpr),
    ]

    rejections = [c for c in checks if c.verdict == "reject"]
    overall = "reject" if rejections else "pass"

    return ReviewResult(
        overall=overall,
        checks=checks,
        rejection_reasons=[c.reason for c in rejections],
    )


if __name__ == "__main__":
    from datetime import timedelta
    from .cpr import CPR, ProvenanceField, ValuationObject, LienRecord, CPRColumn, BlastTier
    from .valuation import GateValResult
    from .pricing import PricingResult

    # Sale dates in fixtures are computed relative to today so this
    # self-check cannot silently rot as the calendar advances.
    future_sale = (datetime.now(timezone.utc).date() + timedelta(days=45)).isoformat()

    # Clean deal — should pass all checks
    cpr = CPR(parcel_id="R123", county="collin", situs_address="100 Test St",
              legal_description="Lot 1, Block A", deal_type="DT-AUCTION")
    cpr.valuation = ValuationObject(
        arv=ProvenanceField(value=300_000, source="mls_agent", confidence=0.85),
        comp_source="mls_agent",
    )
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=200_000, priority=1,
                            survives_foreclosure=False), "test")
    cpr.set_field("sale_date", future_sale, "test", 1.0)
    cpr.move_column(CPRColumn.PRICED)

    gate = GateValResult(verdict="pass", reason="ok", confidence=0.85,
                         comp_source="mls_agent", comp_count=5, floor=0.60)
    pricing = PricingResult(deal_type="DT-AUCTION", mao=180_000, bid_ceiling=175_000,
                             optimistic_only_flag=False)

    result = run_review(cpr, gate, pricing)
    assert result.overall == "pass", f"Expected pass, got {result.overall}: {result.rejection_reasons}"
    print(f"reviewer.py: clean deal — {result.overall} ({len(result.checks)} checks)")

    # X4: US-format past sale date is now parsed and flagged stale
    cpr_us = CPR(parcel_id="R123x", county="collin", situs_address="100 Test St",
                 legal_description="Lot 1, Block A")
    cpr_us.set_field("sale_date", "7/25/2013", "collin_detail", 0.9)
    chk = check_stale_notice(cpr_us)
    assert chk.verdict == "reject" and "in the past" in chk.reason, chk.reason

    # X4: Dallas prose form parses too
    cpr_prose = CPR(parcel_id="R123p", county="dallas", situs_address="100 Test St",
                    legal_description="Lot 1")
    cpr_prose.set_field("sale_date", "Tuesday, the 3rd day of March, 2026",
                        "dallas_pdf", 0.8)
    chk_p = check_stale_notice(cpr_prose)  # 2026-03-03 vs today
    expected_stale = parse_date_any("2026-03-03") < datetime.now(timezone.utc).date()
    assert chk_p.verdict == ("reject" if expected_stale else "pass"), chk_p.reason

    # X4: unparsable sale date is flagged for manual verification, never current
    cpr_bad = CPR(parcel_id="R123b", county="collin", situs_address="100 Test St",
                  legal_description="Lot 1")
    cpr_bad.set_field("sale_date", "Unknown", "source", 0.5)
    chk_b = check_stale_notice(cpr_bad)
    assert chk_b.verdict == "reject"
    assert "unknown-sale-date-manual-verification-required" in chk_b.reason
    cpr_gib = CPR(parcel_id="R123g", county="collin", situs_address="100 Test St",
                  legal_description="Lot 1")
    cpr_gib.set_field("sale_date", "sometime soon-ish", "source", 0.5)
    chk_g = check_stale_notice(cpr_gib)
    assert chk_g.verdict == "reject"
    assert "unknown-sale-date-manual-verification-required" in chk_g.reason
    print("reviewer.py: X4 — stale/unparsable sale dates rejected (never 'current')")

    # X8: identity requires all three fields, compared after ProvenanceField unwrap
    cpr_no_legal = CPR(parcel_id="R200", county="collin", situs_address="200 Test St",
                       legal_description="Unknown")  # clerk legal not yet pulled
    chk_i = check_identity_mismatch(cpr_no_legal)
    assert chk_i.verdict == "reject"
    assert "legal_description" in chk_i.reason and "(2/3 populated)" in chk_i.reason

    cpr_pf_wrap = CPR(parcel_id="R201", county="collin",
                      situs_address=ProvenanceField(value="Unknown", source="test"),
                      legal_description="Lot 2")
    chk_pf = check_identity_mismatch(cpr_pf_wrap)
    assert chk_pf.verdict == "reject"  # object-vs-string comparison no longer fooled
    assert "situs_address" in chk_pf.reason

    cpr_ok = CPR(parcel_id="R202", county="collin", situs_address="300 Test St",
                 legal_description=" Lot 3 ")
    assert check_identity_mismatch(cpr_ok).verdict == "pass"
    print("reviewer.py: X8 — identity unwraps ProvenanceField; all 3 fields required")

    # Bad deal — fabricated confidence (vendor AVM, high confidence)
    cpr2 = CPR(parcel_id="R456", county="collin", situs_address="200 Test St",
               legal_description="Lot 2", deal_type="DT-AUCTION")
    cpr2.valuation = ValuationObject(
        arv=ProvenanceField(value=300_000, source="vendor_avm", confidence=0.90),
        comp_source="vendor_avm",
    )
    cpr2.add_lien(LienRecord(lien_type="mortgage", amount=200_000, priority=1,
                             survives_foreclosure=False), "test")
    cpr2.set_field("sale_date", future_sale, "test", 1.0)
    cpr2.move_column(CPRColumn.PRICED)

    gate2 = GateValResult(verdict="pass", reason="ok", confidence=0.90,
                          comp_source="vendor_avm", comp_count=3, floor=0.50)
    result2 = run_review(cpr2, gate2, pricing)
    assert result2.overall == "reject"
    # Should catch either fabricated confidence or assessed-value laundering
    assert len(result2.rejection_reasons) > 0
    print(f"reviewer.py: bad deal — {result2.overall} ({len(result2.rejection_reasons)} rejections)")
    for r in result2.rejection_reasons:
        print(f"  ✗ {r}")

    # Assessed-value laundering
    cpr3 = CPR(parcel_id="R789", county="collin", situs_address="300 Test St",
               legal_description="Lot 3", deal_type="DT-AUCTION")
    cpr3.valuation = ValuationObject(
        arv=ProvenanceField(value=730_000, source="vendor_avm", confidence=0.70),
        comp_source="vendor_avm",
    )
    cpr3.set_field("cad_appraised_value", 730_673, "collin_cad", 0.90)
    cpr3.add_lien(LienRecord(lien_type="mortgage", amount=300_000, priority=1,
                             survives_foreclosure=False), "test")
    cpr3.set_field("sale_date", future_sale, "test", 1.0)
    cpr3.move_column(CPRColumn.PRICED)
    result3 = run_review(cpr3)
    assert result3.overall == "reject"
    assert any("laundering" in r.lower() for r in result3.rejection_reasons)
    print(f"reviewer.py: assessed laundering — {result3.overall}")

    print("reviewer.py: all self-checks passed")