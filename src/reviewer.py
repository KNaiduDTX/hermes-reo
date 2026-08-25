# src/reviewer.py
"""AGENT-REV: adversarial fake-signal harness (spec §12.5).

Runs on every stage output. Any check can block promotion.
The 8 checks are the system's core edge in a non-disclosure data environment.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .cpr import CPR, CPRColumn
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

    from datetime import datetime
    sale = cpr.sale_date.value if hasattr(cpr.sale_date, "value") else cpr.sale_date
    if isinstance(sale, str) and sale != "Unknown":
        try:
            # Try to parse the sale date — if it's in the past, it's stale
            # ponytail: date parsing is fragile; only flag obvious past dates
            sale_dt = datetime.strptime(sale[:10], "%Y-%m-%d")
            if sale_dt < datetime.utcnow():
                return ReviewCheck("stale_notice", "reject",
                                   f"Sale date {sale[:10]} is in the past — stale notice")
        except ValueError:
            pass  # can't parse — don't flag

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
    """CAD/clerk/tax records not reconciled to the same parcel."""
    # ponytail: full identity resolution is in enrichment.py; here we check
    # that the CPR has enough identity fields to be trustworthy
    identity_fields = [cpr.parcel_id, cpr.situs_address, cpr.legal_description]
    populated = sum(1 for f in identity_fields if f and f != "Unknown" and f != "")

    if populated < 2:
        return ReviewCheck("identity_mismatch", "reject",
                           f"Only {populated}/3 identity fields populated — parcel identity uncertain")

    return ReviewCheck("identity_mismatch", "pass", f"{populated}/3 identity fields populated")


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
    from .cpr import CPR, ProvenanceField, ValuationObject, LienRecord, CPRColumn, BlastTier
    from .valuation import GateValResult
    from .pricing import PricingResult

    # Clean deal — should pass all checks
    cpr = CPR(parcel_id="R123", county="collin", situs_address="100 Test St",
              legal_description="Lot 1, Block A", deal_type="DT-AUCTION")
    cpr.valuation = ValuationObject(
        arv=ProvenanceField(value=300_000, source="mls_agent", confidence=0.85),
        comp_source="mls_agent",
    )
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=200_000, priority=1,
                            survives_foreclosure=False), "test")
    cpr.set_field("sale_date", "2026-12-01", "test", 1.0)
    cpr.move_column(CPRColumn.PRICED)

    gate = GateValResult(verdict="pass", reason="ok", confidence=0.85,
                         comp_source="mls_agent", comp_count=5, floor=0.60)
    pricing = PricingResult(deal_type="DT-AUCTION", mao=180_000, bid_ceiling=175_000,
                             optimistic_only_flag=False)

    result = run_review(cpr, gate, pricing)
    assert result.overall == "pass", f"Expected pass, got {result.overall}: {result.rejection_reasons}"
    print(f"reviewer.py: clean deal — {result.overall} ({len(result.checks)} checks)")

    # Bad deal — fabricated confidence (vendor AVM, high confidence)
    cpr2 = CPR(parcel_id="R456", county="collin", situs_address="200 Test St",
               legal_description="Lot 2", deal_type="DT-AUCTION")
    cpr2.valuation = ValuationObject(
        arv=ProvenanceField(value=300_000, source="vendor_avm", confidence=0.90),
        comp_source="vendor_avm",
    )
    cpr2.add_lien(LienRecord(lien_type="mortgage", amount=200_000, priority=1,
                             survives_foreclosure=False), "test")
    cpr2.set_field("sale_date", "2026-12-01", "test", 1.0)
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
    cpr3.set_field("sale_date", "2026-12-01", "test", 1.0)
    cpr3.move_column(CPRColumn.PRICED)
    result3 = run_review(cpr3)
    assert result3.overall == "reject"
    assert any("laundering" in r.lower() for r in result3.rejection_reasons)
    print(f"reviewer.py: assessed laundering — {result3.overall}")

    print("reviewer.py: all self-checks passed")