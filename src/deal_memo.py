# src/deal_memo.py
"""Deal memo builder — deterministic Markdown render from CPR + underwrite.

Fixed sections every time so deals are comparable and portfolio analytics trivial.
Render targets: Markdown (agent-native). PDF/XLSX export via skill.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional
from .cpr import CPR, CPRColumn, BlastTier
from .dates import parse_date_any
from .underwriting import UnderwriteResult
from .pricing import (
    PricingResult,
    SensitivityCell,
    PENCIL_OPTIMISTIC_ONLY,
    PENCIL_NEVER,
)

if TYPE_CHECKING:  # avoid a hard runtime dependency on the reviewer module
    from .reviewer import ReviewResult


def _unwrap(value: Any, placeholder: str = "—") -> Any:
    """Unwrap ProvenanceField-style objects; None/'' values render as placeholder.

    AUDIT fix: the memo used to print the literal string "None" whenever a
    ProvenanceField existed but its .value was None (the object itself is
    truthy). Unknown is now rendered as an em-dash / caller-supplied marker.
    """
    v = getattr(value, "value", value)
    if v is None or v == "":
        return placeholder
    return v


def recommend_deal(cpr: CPR, underwrite: UnderwriteResult, pricing: PricingResult,
                   review: Optional["ReviewResult"] = None) -> tuple[str, list[str], list[str]]:
    """Gate PROCEED on hard evidence. Returns (verdict, blockers, cautions).

    AUDIT C2/C3: PROCEED requires ALL of —
      * GATE-VAL verdict == "pass"
      * underwrite net_profit > 0
      * review.overall == "pass" (reviewer absent → capped at CONDITIONAL)
      * PricingResult.dead_reason is None
      * pencil_status != "never_pencils"
    Any failed requirement is a blocker → DO NOT PROCEED with the first
    failing reason. Unknown inputs (gate not run, empty sensitivity grid)
    and optimistic-only pencils are cautions → capped at CONDITIONAL HITL.
    """
    blockers: list[str] = []
    cautions: list[str] = []

    # 1. GATE-VAL
    gate_verdict = cpr.valuation.gate_val_verdict if cpr.valuation else ""
    if gate_verdict == "pass":
        pass
    elif gate_verdict == "hitl":
        cautions.append("GATE-VAL verdict 'hitl' — route to MLS/appraiser for a real comp pull")
    elif gate_verdict == "":
        cautions.append("GATE-VAL not run — ARV unknown")
    else:
        gate_reason = f" — {cpr.valuation.gate_val_reason}" if cpr.valuation else ""
        blockers.append(f"GATE-VAL failed ('{gate_verdict}'){gate_reason}")

    # 2. Underwriting must actually make money
    if underwrite.net_profit <= 0:
        blockers.append(
            f"Underwrite net profit ${underwrite.net_profit:,.0f} ≤ 0 — deal does not pencil"
        )

    # 2b. Lien-gate integrity (AUDIT C3 follow-up): an errored lookup or a
    # surviving tax/IRS/municipal lien must never yield unconditional PROCEED.
    if getattr(cpr, "lien_lookup_status", "not_run") == "error":
        blockers.append("Clerk lien lookup FAILED — clean title cannot be asserted")
    else:
        surviving = [
            ln for ln in (cpr.liens or [])
            if getattr(ln, "survives_foreclosure", False)
            and getattr(ln, "lien_type", "") in ("tax", "irs_federal", "municipal")
        ]
        if surviving:
            cautions.append(
                f"{len(surviving)} surviving priority lien(s) "
                f"(e.g. {surviving[0].lien_type} recorded {surviving[0].recording_date}) — "
                "title/redemption review required before contract"
            )

    # 3. Pricing must be alive (AUDIT C2: dead ceiling ⇒ no offer exists)
    if pricing.dead_reason is not None:
        blockers.append(
            f"Pricing dead ({pricing.dead_reason}) — bid ceiling "
            f"${pricing.bid_ceiling:,.0f} leaves nothing to bid"
        )

    # 4. Pencil status (AUDIT F12: never_pencils is fatal, optimistic-only is a caution)
    if pricing.pencil_status == PENCIL_NEVER:
        blockers.append("Pencil status 'never_pencils' — loses money across the entire ARV/rehab grid")
    elif pricing.pencil_status == PENCIL_OPTIMISTIC_ONLY or pricing.optimistic_only_flag:
        cautions.append("Pencil status 'optimistic_only' — only pencils at top of the ARV/rehab grid")
    elif pricing.pencil_status == "unknown":
        cautions.append("Pencil status unknown — sensitivity grid is empty")

    # 5. Adversarial review; absent reviewer caps the recommendation
    if review is not None:
        overall = getattr(review, "overall", "")
        if overall != "pass":
            reasons = list(getattr(review, "rejection_reasons", []) or [])
            first = reasons[0] if reasons else f"reviewer verdict '{overall or 'unknown'}'"
            blockers.append(f"AGENT-REV overall '{overall}' — {first}")
    else:
        cautions.append("AGENT-REV not run — recommendation capped at CONDITIONAL")

    if blockers:
        return "DO NOT PROCEED", blockers, cautions
    if cautions:
        return "CONDITIONAL", blockers, cautions
    return "PROCEED", blockers, cautions


def _render_header(lines: list, cpr: CPR, now: str, sale: str) -> None:
    """Section 1 — Header."""
    lines.append(f"# DEAL MEMO — {cpr.parcel_id} — {cpr.situs_address} — {cpr.county.title()} County")
    lines.append(f"**Deal type:** {cpr.deal_type} | **Channel:** {cpr.channel} | **Date:** {now}")
    lines.append(f"**Sale date:** {sale} | **Blast tier:** {cpr.blast_tier.name}")
    lines.append("")


def _render_snapshot(lines: list, cpr: CPR) -> None:
    """Section 2 — Property snapshot."""
    lines.append("## Property Snapshot")
    lines.append(f"| Field | Value | Source | Confidence |")
    lines.append(f"|-------|-------|--------|------------|")
    for label, field_obj in [
        ("Address", cpr.situs_address),
        ("Legal", cpr.legal_description),
        ("Type", cpr.property_type),
        ("Sqft", cpr.improvement_sqft),
        ("Beds", cpr.beds),
        ("Baths", cpr.baths),
        ("Year Built", cpr.year_built),
        ("Homestead", cpr.homestead),
    ]:
        v = _unwrap(field_obj, None)
        if v is None:
            # Unknown value (missing field, or ProvenanceField(None)) — never
            # print a literal "None"; show an em-dash row instead.
            lines.append(f"| {label} | — | — | — |")
            continue
        s = _unwrap(getattr(field_obj, "source", None))
        conf = getattr(field_obj, "confidence", None)
        c = f"{conf:.0%}" if isinstance(conf, (int, float)) else "—"
        lines.append(f"| {label} | {v} | {s} | {c} |")
    lines.append("")


def _render_distress(lines: list, cpr: CPR, sale: str) -> None:
    """Section 3 — Distress & legal state."""
    lines.append("## Distress & Legal State")
    lines.append(f"- **NOD/NOS status:** {_unwrap(cpr.nod_status, 'N/A')}")
    lines.append(f"- **Sale date:** {sale}")
    lines.append(f"- **Opening/credit bid:** {_unwrap(cpr.opening_bid, 'Unknown')}")
    lines.append(f"- **Redemption class:** {_unwrap(cpr.redemption_class, 'N/A')}")
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


def _render_valuation(lines: list, cpr: CPR) -> None:
    """Section 4 — Valuation."""
    lines.append("## Valuation")
    if cpr.valuation:
        v = cpr.valuation
        arv_val = _unwrap(v.arv, None)
        as_is_val = _unwrap(v.as_is_value, None)
        rent_val = _unwrap(v.rent_estimate, None)
        if arv_val is not None:
            lines.append(f"- **ARV:** ${arv_val:,.0f} (source: {v.arv.source}, confidence: {v.arv.confidence:.0%})")
        if as_is_val is not None:
            lines.append(f"- **As-is value:** ${as_is_val:,.0f}")
        if rent_val is not None:
            lines.append(f"- **Rent estimate:** ${rent_val:,.0f}/mo")
        lines.append(f"- **Comp source:** {v.comp_source or '—'}")
        lines.append(f"- **GATE-VAL verdict:** {v.gate_val_verdict or 'not run'} — {v.gate_val_reason or '—'}")
        if v.comp_set:
            lines.append(f"- **Comp count:** {len(v.comp_set)}")
    else:
        lines.append("_Valuation not yet performed — GATE-VAL required._")
    lines.append("")


def _render_underwrite(lines: list, underwrite: UnderwriteResult) -> None:
    """Section 5 — Underwrite."""
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
    if underwrite.cash_on_cash is not None:  # AUDIT C3: unknown basis renders as dash below
        lines.append(f"- **Cash-on-cash:** {underwrite.cash_on_cash:.2%}")
    elif underwrite.model == "rental":
        lines.append("- **Cash-on-cash:** — (equity basis unknown for financed deal)")
    if underwrite.dscr is not None:
        lines.append(f"- **DSCR:** {underwrite.dscr:.2f}")
    if underwrite.redemption_yield is not None:
        lines.append(f"- **Redemption yield:** ${underwrite.redemption_yield:,.0f}")
    if underwrite.refi_recycle_pct is not None:
        lines.append(f"- **BRRRR recycle:** {underwrite.refi_recycle_pct:.1%}")
    lines.append("")


def _render_pricing(lines: list, pricing: PricingResult) -> None:
    """Section 6 — Pricing."""
    lines.append("## Pricing")
    lines.append(f"- **MAO:** ${pricing.mao:,.0f}")
    lines.append(f"- **Bid ceiling:** ${pricing.bid_ceiling:,.0f}")
    lines.append(f"- **Blast tier:** {pricing.blast_tier.name}")
    if pricing.dead_reason is not None:
        # AUDIT C2: dead deal — no offer exists, say so loudly.
        lines.append(f"- 🚫 **DEAD — {pricing.dead_reason}:** nothing left to bid after "
                     f"liens/caps; offer withheld (None), no ladder generated.")
    else:
        lad = pricing.offer_ladder
        if pricing.offer is not None:
            lines.append(f"- **Offer (target):** ${pricing.offer:,.0f}")
        if lad:
            lines.append(f"- **Offer ladder:** opening ${lad.get('opening', 0):,.0f} → target ${lad.get('target', 0):,.0f} → walk-away ${lad.get('walk_away', 0):,.0f}")
    # Pencil status (AUDIT F12): never_pencils renders distinctly from optimistic-only
    if pricing.pencil_status == PENCIL_NEVER:
        lines.append("- 🚫 **NEVER PENCILS:** loses money at every point of the ARV/rehab sensitivity grid — this is not a negotiation problem.")
    elif pricing.pencil_status == PENCIL_OPTIMISTIC_ONLY or pricing.optimistic_only_flag:
        lines.append("- ⚠️ **OPTIMISTIC-ONLY FLAG:** Deal only pencils at top of ARV/rehab sensitivity grid (fake-signal tell)")
    elif pricing.pencil_status == "unknown":
        lines.append("- ℹ️ **Pencil status unknown:** sensitivity grid is empty.")
    else:
        lines.append("- ✓ Pencils at base assumptions.")
    if pricing.sensitivity:
        lines.append("")
        lines.append("### Sensitivity Grid (ARV × Rehab)")
        lines.append("| ARV Δ | Rehab Δ | Profit | Margin |")
        lines.append("|-------|---------|--------|--------|")
        for cell in pricing.sensitivity:
            lines.append(f"| {cell.arv_delta:+.0%} | {cell.rehab_delta:+.0%} | ${cell.profit:,.0f} | {cell.margin:.1%} |")
    lines.append("")


def _render_exit(lines: list, cpr: CPR, underwrite: UnderwriteResult) -> None:
    """Section 7 — Exit & disposition plan.

    Audit UW-8: SHORT/SUBTO/RENTAL/BRRRR used to get the generic
    "fix-and-flip or BRRRR" line. Audit R-5: the three exit models are all
    built — the memo now shows them side by side at the memo's own
    ARV/purchase basis instead of a single-model exit narrative.
    """
    lines.append("## Exit & Disposition Plan")
    lines.append(f"- **Exit strategy:** {underwrite.model}")
    dt = str(getattr(cpr, "deal_type", "") or "")
    disposition = {
        "DT-WHOLESALE": "assign contract to end buyer (requires bilateral disclosure)",
        "DT-TAXDEED": "hold through redemption → quiet-title → sell/refi",
        "DT-SHORT": "lender-approved sale — patience path; expect 60-120 days approval",
        "DT-SUBTO": "take title subject-to the existing loan (disclose due-on-sale risk)",
        "DT-RENTAL": "hold as long-term rental — cap rate / DSCR drive the decision",
        "DT-BRRRR": "renovate → lease → cash-out refi to recycle capital",
    }.get(dt, "fix-and-flip (retail resale) or hold via BRRRR refi")
    lines.append(f"- **Disposition:** {disposition}")

    # R-5: side-by-side exit comparison at the memo's own economics.
    try:
        arv = float(underwrite.arv or 0)
        purchase = float(underwrite.purchase_price or 0)
        rehab = float(underwrite.rehab or 0)
        if arv > 0 and purchase > 0 and underwrite.cash_invested:
            from .underwriting import brrrr_recycle, rental_metrics

            brr = brrrr_recycle(arv, purchase, rehab)
            rent_est = round(arv * 0.006)          # 0.6%/mo rent convention
            tax_est = arv * 0.022                   # TX county-average rate
            rent = rental_metrics(price=purchase, rent_monthly=rent_est,
                                  taxes_annual=tax_est,
                                  equity_invested=purchase + rehab)
            lines.append("")
            lines.append("### Side-by-side exit comparison (same ARV/purchase basis)")
            lines.append("")
            lines.append("| Strategy | Cash in | Net / Recycled | Return |")
            lines.append("|---|---|---|---|")
            lines.append(f"| Fix-and-flip | ${underwrite.cash_invested:,.0f} | "
                         f"${underwrite.net_profit:,.0f} net | {underwrite.roi:.1%} |")
            lines.append(f"| BRRRR refi | ${brr.cash_invested:,.0f} all-in | "
                         f"${brr.net_profit:,.0f} recycled "
                         f"({brr.roi * 100:.0f}% of all-in) | {brr.roi:.1%} |")
            coc = rent.cash_on_cash
            lines.append(f"| Rental hold (rent est ${rent_est:,}/mo) | "
                         f"${(purchase + rehab):,.0f} equity | "
                         f"cap {(rent.cap_rate or 0):.1%} | "
                         f"{'—' if coc is None else format(coc, '.1%')} cash-on-cash |")
            lines.append("")
            lines.append("_Rental row is illustrative: rent estimated at 0.6%/mo of ARV "
                         "and taxes at 2.2%/yr — tune per property in the workbook "
                         "before acting._")
    except Exception:  # noqa: BLE001 — comparison is additive, never fatal
        pass
    lines.append("")


def _render_risks(lines: list, cpr: CPR, pricing: PricingResult) -> None:
    """Section 8 — Risk register."""
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
    if pricing.dead_reason is not None:
        risks.append(f"Pricing dead ({pricing.dead_reason}) — no offer possible")
    if pricing.pencil_status == PENCIL_NEVER:
        risks.append("🚫 Never pencils: unprofitable across the entire ARV/rehab grid")
    elif pricing.pencil_status == PENCIL_OPTIMISTIC_ONLY or pricing.optimistic_only_flag:
        risks.append("⚠️ Optimistic-only: deal only pencils at optimistic ARV")
    for r in risks:
        lines.append(f"- {r}")
    if not risks:
        lines.append("- No risks identified")
    lines.append("")


def _render_compliance(lines: list, cpr: CPR) -> None:
    """Section 9 — Compliance checklist."""
    lines.append("## Compliance Checklist")
    lines.append(f"- [ ] Buyer eligibility verified (Written Statement / RealAuction registration)")
    if cpr.deal_type == "DT-WHOLESALE":
        lines.append(f"- [ ] Bilateral equitable-interest disclosure generated (Prop. Code §5.0205)")
        lines.append(f"- [ ] Assignable contract with 'and/or assigns'")
    lines.append(f"- [ ] GATE-LEGAL-LIEN: title/lien priority confirmed")
    lines.append(f"- [ ] LEGAL-RULES version verified ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})")
    lines.append("")


def _render_recommendation(lines: list, cpr: CPR, underwrite: UnderwriteResult,
                           pricing: PricingResult,
                           review: Optional["ReviewResult"]) -> None:
    """Section 10 — Recommendation + gate trail."""
    lines.append("## Recommendation")
    verdict, blockers, cautions = recommend_deal(cpr, underwrite, pricing, review)
    if verdict == "DO NOT PROCEED":
        lines.append(f"**DO NOT PROCEED** — {blockers[0]}")
        for extra in blockers[1:]:
            lines.append(f"  - {extra}")
    elif verdict == "CONDITIONAL":
        lines.append(f"**CONDITIONAL — HITL REVIEW** — {cautions[0]}")
        for extra in cautions[1:]:
            lines.append(f"  - {extra}")
    else:
        lines.append("**PROCEED** — Valuation gate passed, adversarial review passed, "
                     "deal pencils at base assumptions, pricing live.")
    lines.append("")
    lines.append("### Gate Trail")
    lines.append(f"- GATE-VAL: {cpr.valuation.gate_val_verdict if cpr.valuation else 'not run'}")
    lines.append(f"- GATE-LEGAL-LIEN: {'not run' if not cpr.liens else 'lien stack present'}")
    lines.append(f"- AGENT-REV: {review.overall if review is not None else 'not run'}")
    lines.append(f"- LEGAL-RULES: v1.0 (verified_by: spec_research)")


def render_memo(cpr: CPR, underwrite: UnderwriteResult, pricing: PricingResult,
                review: Optional["ReviewResult"] = None) -> str:
    """Render a deterministic deal memo in Markdown.

    Sections (spec §8), each built by its own helper, always in this order
    so memos stay comparable and portfolio analytics stay trivial:
    1. Header (_render_header)
    2. Property snapshot (_render_snapshot)
    3. Distress & legal state (_render_distress)
    4. Valuation (_render_valuation)
    5. Underwrite (_render_underwrite)
    6. Pricing (_render_pricing)
    7. Exit & disposition plan (_render_exit)
    8. Risk register (_render_risks)
    9. Compliance checklist (_render_compliance)
    10. Recommendation + gate trail (_render_recommendation)

    `review` (optional ReviewResult) feeds the recommendation gate; when it
    is omitted the recommendation is capped at CONDITIONAL (HITL).
    """
    lines: list = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parsed_sale = parse_date_any(_unwrap(cpr.sale_date, None))
    sale = parsed_sale.isoformat() if parsed_sale else "N/A"

    _render_header(lines, cpr, now, sale)
    _render_snapshot(lines, cpr)
    _render_distress(lines, cpr, sale)
    _render_valuation(lines, cpr)
    _render_underwrite(lines, underwrite)
    _render_pricing(lines, pricing)
    _render_exit(lines, cpr, underwrite)
    _render_risks(lines, cpr, pricing)
    _render_compliance(lines, cpr)
    _render_recommendation(lines, cpr, underwrite, pricing, review)

    return "\n".join(lines)


if __name__ == "__main__":
    # Self-check: render memos from fixture data across win/lose/dead scenarios
    from .cpr import CPR, ProvenanceField, LienRecord, ValuationObject, CompRecord, CPRColumn, BlastTier
    from .reviewer import ReviewResult
    from .underwriting import flip_pnl
    from .pricing import PricingResult, SensitivityCell

    _results = {"pass": 0, "fail": 0}

    def _check(name: str, cond: bool) -> None:
        _results["pass" if cond else "fail"] += 1
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        assert cond, name

    def build_fixture_cpr() -> CPR:
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
        # ProvenanceField(None) — the memo must render an em-dash, not "None"
        cpr.set_field("homestead", None, "collin_cad", 0.0)
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
        return cpr

    uw_win = flip_pnl(arv=750_000, purchase=450_000, rehab=50_000,
                      holding=5_000, financing=10_000, closing_buy=5_000, closing_sell=60_000)
    pricing_alive = PricingResult(
        deal_type="DT-AUCTION", mao=475_000, bid_ceiling=470_000,
        offer_ladder={"opening": 404_000, "target": 475_000, "walk_away": 470_000},
        blast_tier=BlastTier.T3,
        pencil_status="base",
        sensitivity=[SensitivityCell(arv_delta=0.0, rehab_delta=0.0, profit=100_000, margin=0.13)],
    )
    review_pass = ReviewResult(overall="pass")

    # --- Scenario 1: winning deal + full gates -> PROCEED ---
    memo = render_memo(build_fixture_cpr(), uw_win, pricing_alive, review_pass)
    _check("memo has header/address", "DEAL MEMO" in memo and "10022 Plainsman" in memo)
    _check("valuation section present", "GATE-VAL" in memo)
    _check("pricing section present", "MAO" in memo)
    _check("sensitivity grid present", "Sensitivity" in memo)
    _check("risk register present", "Risk Register" in memo)
    _check("compliance checklist present", "Compliance" in memo)
    _check("winning deal prints **PROCEED**", "**PROCEED**" in memo)
    _check("winning deal does NOT print DO NOT PROCEED", "DO NOT PROCEED" not in memo)
    _check("no literal 'None' rendered anywhere (ProvenanceField(None) fix)",
           "None" not in memo)
    _check("unset homestead renders em-dash row", "| Homestead | — | — | — |" in memo)
    _check("sale date parsed to ISO via parse_date_any", "2026-09-01" in memo)
    _check("gate trail shows AGENT-REV pass", "- AGENT-REV: pass" in memo)

    # --- Scenario 2: losing underwrite -> DO NOT PROCEED with reason ---
    uw_loss = flip_pnl(arv=750_000, purchase=700_000, rehab=50_000,
                       holding=5_000, financing=10_000, closing_buy=5_000, closing_sell=60_000)
    memo_loss = render_memo(build_fixture_cpr(), uw_loss, pricing_alive, review_pass)
    _check("losing deal prints **DO NOT PROCEED**", "**DO NOT PROCEED**" in memo_loss)
    _check("losing deal cites non-positive net profit",
           "net profit $" in memo_loss and "does not pencil" in memo_loss)
    _check("losing deal does NOT print bare PROCEED verdict", "\n**PROCEED**" not in memo_loss)

    # --- Scenario 3: dead pricing (C2) -> DO NOT PROCEED, distinct from loss ---
    pricing_dead = PricingResult(
        deal_type="DT-AUCTION", mao=475_000, bid_ceiling=0,
        offer=None, offer_ladder={},
        blast_tier=BlastTier.T3,
        pencil_status=PENCIL_NEVER,
        dead_reason="bid_ceiling_nonpositive",
        sensitivity=[],
    )
    memo_dead = render_memo(build_fixture_cpr(), uw_win, pricing_dead, review_pass)
    _check("dead-pricing deal prints **DO NOT PROCEED**", "**DO NOT PROCEED**" in memo_dead)
    _check("dead reason surfaced (bid_ceiling_nonpositive)",
           "bid_ceiling_nonpositive" in memo_dead)
    _check("never_pencils rendered distinctly", "NEVER PENCILS" in memo_dead)

    # --- Scenario 4: optimistic-only renders distinctly from never_pencils ---
    pricing_opt = PricingResult(
        deal_type="DT-AUCTION", mao=475_000, bid_ceiling=470_000,
        offer_ladder={"opening": 404_000, "target": 475_000, "walk_away": 470_000},
        blast_tier=BlastTier.T3,
        pencil_status="optimistic_only",
        sensitivity=[SensitivityCell(arv_delta=0.10, rehab_delta=-0.10, profit=12_000, margin=0.05)],
    )
    memo_opt = render_memo(build_fixture_cpr(), uw_win, pricing_opt, review_pass)
    _check("optimistic-only flag line rendered", "OPTIMISTIC-ONLY FLAG" in memo_opt)
    _check("optimistic-only does not claim NEVER PENCILS", "NEVER PENCILS" not in memo_opt)
    _check("optimistic-only caps at CONDITIONAL even when review passed",
           "**CONDITIONAL — HITL REVIEW**" in memo_opt)

    # --- Scenario 5: reviewer absent -> capped at CONDITIONAL ---
    memo_norev = render_memo(build_fixture_cpr(), uw_win, pricing_alive)
    _check("absent reviewer caps recommendation at CONDITIONAL",
           "**CONDITIONAL — HITL REVIEW**" in memo_norev)
    _check("no PROCEED verdict without reviewer sign-off", "\n**PROCEED**" not in memo_norev)
    _check("gate trail shows AGENT-REV not run", "- AGENT-REV: not run" in memo_norev)

    # --- Scenario 6: legacy positional call signature still works ---
    memo_legacy = render_memo(build_fixture_cpr(), uw_win, pricing_alive)
    _check("legacy 3-arg render_memo call still renders", "DEAL MEMO" in memo_legacy)

    print(f"\ndeal_memo.py: rendered {len(memo)} chars (winning scenario)")
    print(f"deal_memo.py: checks {_results['pass']} passed, {_results['fail']} failed")
    print("deal_memo.py: all self-checks passed")
