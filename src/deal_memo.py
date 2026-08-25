# src/deal_memo.py
"""Deal memo builder — deterministic Markdown render from CPR + underwrite.

Fixed sections every time so deals are comparable and portfolio analytics trivial.
Render targets: Markdown (agent-native). PDF/XLSX export via skill.
"""
from __future__ import annotations
from datetime import datetime
from .cpr import CPR, CPRColumn, BlastTier
from .underwriting import UnderwriteResult
from .pricing import PricingResult, SensitivityCell


def render_memo(cpr: CPR, underwrite: UnderwriteResult, pricing: PricingResult) -> str:
    """Render a deterministic deal memo in Markdown.

    Sections (spec §8):
    1. Header
    2. Property snapshot
    3. Distress & legal state
    4. Valuation
    5. Underwrite
    6. Pricing
    7. Exit & disposition plan
    8. Risk register
    9. Compliance checklist
    10. Recommendation + gate trail
    """
    lines = []
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # 1. Header
    lines.append(f"# DEAL MEMO — {cpr.parcel_id} — {cpr.situs_address} — {cpr.county.title()} County")
    lines.append(f"**Deal type:** {cpr.deal_type} | **Channel:** {cpr.channel} | **Date:** {now}")
    sale = cpr.sale_date.value if cpr.sale_date else "N/A"
    lines.append(f"**Sale date:** {sale} | **Blast tier:** {cpr.blast_tier.name}")
    lines.append("")

    # 2. Property snapshot
    lines.append("## Property Snapshot")
    lines.append(f"| Field | Value | Source | Confidence |")
    lines.append(f"|-------|-------|--------|------------|")
    for label, field in [
        ("Address", cpr.situs_address),
        ("Legal", cpr.legal_description or "—"),
        ("Type", cpr.property_type),
        ("Sqft", cpr.improvement_sqft),
        ("Beds", cpr.beds),
        ("Baths", cpr.baths),
        ("Year Built", cpr.year_built),
        ("Homestead", cpr.homestead),
    ]:
        if field:
            v = field.value if hasattr(field, "value") else field
            s = field.source if hasattr(field, "source") else "—"
            c = f"{field.confidence:.0%}" if hasattr(field, "confidence") else "—"
            lines.append(f"| {label} | {v} | {s} | {c} |")
    lines.append("")

    # 3. Distress & legal state
    lines.append("## Distress & Legal State")
    lines.append(f"- **NOD/NOS status:** {cpr.nod_status.value if cpr.nod_status else 'N/A'}")
    lines.append(f"- **Sale date:** {sale}")
    lines.append(f"- **Opening/credit bid:** {cpr.opening_bid.value if cpr.opening_bid else 'Unknown'}")
    lines.append(f"- **Redemption class:** {cpr.redemption_class.value if cpr.redemption_class else 'N/A'}")
    lines.append("")
    if cpr.liens:
        lines.append("### Encumbrance Stack")
        lines.append("| Type | Amount | Lender | Priority | Survives FC | Survives Tax Sale |")
        lines.append("|------|--------|--------|----------|-------------|-------------------|")
        for lien in cpr.liens:
            surv_fc = "Yes" if lien.survives_foreclosure else "No"
            surv_tax = "Yes" if lien.survives_tax_sale else "No"
            lines.append(f"| {lien.lien_type} | ${lien.amount or 0:,.0f} | {lien.lender or '—'} | {lien.priority} | {surv_fc} | {surv_tax} |")
    else:
        lines.append("### Encumbrance Stack\n_No liens recorded — GATE-LEGAL-LIEN required before bidding._")
    lines.append("")

    # 4. Valuation
    lines.append("## Valuation")
    if cpr.valuation:
        v = cpr.valuation
        if v.arv:
            lines.append(f"- **ARV:** ${v.arv.value:,.0f} (source: {v.arv.source}, confidence: {v.arv.confidence:.0%})")
        if v.as_is_value:
            lines.append(f"- **As-is value:** ${v.as_is_value.value:,.0f}")
        if v.rent_estimate:
            lines.append(f"- **Rent estimate:** ${v.rent_estimate.value:,.0f}/mo")
        lines.append(f"- **Comp source:** {v.comp_source}")
        lines.append(f"- **GATE-VAL verdict:** {v.gate_val_verdict} — {v.gate_val_reason}")
        if v.comp_set:
            lines.append(f"- **Comp count:** {len(v.comp_set)}")
    else:
        lines.append("_Valuation not yet performed — GATE-VAL required._")
    lines.append("")

    # 5. Underwrite
    lines.append("## Underwrite")
    lines.append(f"- **Model:** {underwrite.model}")
    lines.append(f"- **ARV:** ${underwrite.arv:,.0f}")
    lines.append(f"- **Purchase:** ${underwrite.purchase_price:,.0f}")
    lines.append(f"- **Rehab:** ${underwrite.rehab:,.0f} (contingency: {underwrite.rehab_contingency_pct:.0%})")
    lines.append(f"- **Holding:** ${underwrite.holding_costs:,.0f}")
    lines.append(f"- **Financing:** ${underwrite.financing_costs:,.0f}")
    lines.append(f"- **Closing (buy+sell):** ${underwrite.closing_costs_buy + underwrite.closing_costs_sell:,.0f}")
    lines.append(f"- **Net profit:** ${underwrite.net_profit:,.0f}")
    lines.append(f"- **ROI:** {underwrite.roi:.1%}")
    if underwrite.cap_rate is not None:
        lines.append(f"- **Cap rate:** {underwrite.cap_rate:.2%}")
    if underwrite.cash_on_cash is not None:
        lines.append(f"- **Cash-on-cash:** {underwrite.cash_on_cash:.2%}")
    if underwrite.dscr is not None:
        lines.append(f"- **DSCR:** {underwrite.dscr:.2f}")
    if underwrite.redemption_yield is not None:
        lines.append(f"- **Redemption yield:** ${underwrite.redemption_yield:,.0f}")
    if underwrite.refi_recycle_pct is not None:
        lines.append(f"- **BRRRR recycle:** {underwrite.refi_recycle_pct:.1%}")
    lines.append("")

    # 6. Pricing
    lines.append("## Pricing")
    lines.append(f"- **MAO:** ${pricing.mao:,.0f}")
    lines.append(f"- **Bid ceiling:** ${pricing.bid_ceiling:,.0f}")
    lines.append(f"- **Blast tier:** {pricing.blast_tier.name}")
    lad = pricing.offer_ladder
    lines.append(f"- **Offer ladder:** opening ${lad.get('opening', 0):,.0f} → target ${lad.get('target', 0):,.0f} → walk-away ${lad.get('walk_away', 0):,.0f}")
    if pricing.optimistic_only_flag:
        lines.append("- ⚠️ **OPTIMISTIC-ONLY FLAG:** Deal only pencils at top of ARV/rehab sensitivity grid (fake-signal tell)")
    if pricing.sensitivity:
        lines.append("")
        lines.append("### Sensitivity Grid (ARV × Rehab)")
        lines.append("| ARV Δ | Rehab Δ | Profit | Margin |")
        lines.append("|-------|---------|--------|--------|")
        for c in pricing.sensitivity:
            lines.append(f"| {c.arv_delta:+.0%} | {c.rehab_delta:+.0%} | ${c.profit:,.0f} | {c.margin:.1%} |")
    lines.append("")

    # 7. Exit & disposition plan
    lines.append("## Exit & Disposition Plan")
    lines.append(f"- **Exit strategy:** {underwrite.model}")
    if cpr.deal_type == "DT-WHOLESALE":
        lines.append("- **Disposition:** assign contract to end buyer (requires bilateral disclosure)")
    elif cpr.deal_type == "DT-TAXDEED":
        lines.append("- **Disposition:** hold through redemption → quiet-title → sell/refi")
    else:
        lines.append("- **Disposition:** fix-and-flip or BRRRR")
    lines.append("")

    # 8. Risk register
    lines.append("## Risk Register")
    risks = []
    if cpr.valuation and cpr.valuation.gate_val_verdict != "pass":
        risks.append("Valuation gate not passed — ARV may be unreliable")
    if not cpr.liens:
        risks.append("No lien search performed — title risk unknown")
    elif any(l.survives_foreclosure for l in cpr.liens):
        risks.append("Surviving liens detected — reduce bid ceiling")
    if cpr.deal_type == "DT-AUCTION":
        risks.append("Auction: no inspection, no title insurance, sight-unseen")
        risks.append("Occupied property → eviction cost/time (3-day notice)")
    if cpr.deal_type == "DT-TAXDEED":
        risks.append("No clear title at purchase — quiet-title required post-redemption")
        risks.append("No irreversible improvements during redemption period")
    if pricing.optimistic_only_flag:
        risks.append("⚠️ Optimistic-only: deal only pencils at optimistic ARV")
    for r in risks:
        lines.append(f"- {r}")
    if not risks:
        lines.append("- No risks identified")
    lines.append("")

    # 9. Compliance checklist
    lines.append("## Compliance Checklist")
    lines.append(f"- [ ] Buyer eligibility verified (Written Statement / RealAuction registration)")
    if cpr.deal_type == "DT-WHOLESALE":
        lines.append(f"- [ ] Bilateral equitable-interest disclosure generated (Prop. Code §5.0205)")
        lines.append(f"- [ ] Assignable contract with 'and/or assigns'")
    lines.append(f"- [ ] GATE-LEGAL-LIEN: title/lien priority confirmed")
    lines.append(f"- [ ] LEGAL-RULES version verified ({datetime.utcnow().strftime('%Y-%m-%d')})")
    lines.append("")

    # 10. Recommendation + gate trail
    lines.append("## Recommendation")
    if cpr.valuation and cpr.valuation.gate_val_verdict == "pass" and not pricing.optimistic_only_flag:
        lines.append(f"**PROCEED** — Valuation gate passed, pricing within bounds.")
    elif cpr.valuation and cpr.valuation.gate_val_verdict == "hitl":
        lines.append(f"**HITL** — Route to MLS/appraiser for real comp pull before proceeding.")
    else:
        lines.append(f"**DO NOT PROCEED** — Gate failures must be resolved first.")
    lines.append("")
    lines.append("### Gate Trail")
    lines.append(f"- GATE-VAL: {cpr.valuation.gate_val_verdict if cpr.valuation else 'not run'}")
    lines.append(f"- GATE-LEGAL-LIEN: {'not run' if not cpr.liens else 'lien stack present'}")
    lines.append(f"- AGENT-REV: not run")
    lines.append(f"- LEGAL-RULES: v1.0 (verified_by: spec_research)")

    return "\n".join(lines)


if __name__ == "__main__":
    # Self-check: render a memo from fixture data
    from .cpr import CPR, ProvenanceField, LienRecord, ValuationObject, CompRecord, CPRColumn, BlastTier
    from .underwriting import flip_pnl
    from .pricing import price_deal

    cpr = CPR(parcel_id="R4003592", county="collin",
              situs_address="10022 Plainsman Ln, Frisco, TX 75035",
              deal_type="DT-AUCTION", channel="foreclosure_notice")
    cpr.set_field("improvement_sqft", 4332, "collin_cad", 0.95)
    cpr.set_field("beds", 5, "collin_cad", 0.95)
    cpr.set_field("baths", 4.0, "collin_cad", 0.95)
    cpr.set_field("year_built", 2004, "collin_cad", 0.95)
    cpr.set_field("property_type", "Residential Single Family (A1)", "collin_cad", 0.95)
    cpr.set_field("cad_appraised_value", 730_673, "collin_cad", 0.90)
    cpr.set_field("sale_date", "2026-09-01", "collin_foreclosure", 0.80)
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=350_000, lender="Wells Fargo",
                            priority=1, survives_foreclosure=False), "collin_clerk")

    cpr.valuation = ValuationObject(
        arv=ProvenanceField(value=750_000, source="mls_agent", confidence=0.85),
        comp_source="mls_agent",
        gate_val_verdict="pass",
        gate_val_reason="Confidence 0.85 >= floor 0.60",
        comp_set=[CompRecord(address="comp1", sale_price=740_000, distance_miles=0.3)],
    )
    cpr.move_column(CPRColumn.VALUED)

    uw = flip_pnl(arv=750_000, purchase=450_000, rehab=50_000,
                  holding=5_000, financing=10_000, closing_buy=5_000, closing_sell=60_000)
    pricing = PricingResult(
        deal_type="DT-AUCTION", mao=475_000, bid_ceiling=470_000,
        offer_ladder={"opening": 404_000, "target": 475_000, "walk_away": 499_000},
        blast_tier=BlastTier.T3,
        sensitivity=[SensitivityCell(arv_delta=0.0, rehab_delta=0.0, profit=100_000, margin=0.13)],
    )

    memo = render_memo(cpr, uw, pricing)
    assert "DEAL MEMO" in memo
    assert "10022 Plainsman" in memo
    assert "GATE-VAL" in memo
    assert "MAO" in memo
    assert "Sensitivity" in memo
    assert "Risk Register" in memo
    assert "Compliance" in memo
    assert "PROCEED" in memo or "HITL" in memo or "DO NOT PROCEED" in memo

    print(f"deal_memo.py: rendered {len(memo)} chars")
    print("deal_memo.py: all self-checks passed")