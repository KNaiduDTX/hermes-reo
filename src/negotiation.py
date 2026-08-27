# src/negotiation.py
"""AGENT-NEG: seller playbooks + offer generation + disposition.

All party-facing comms are drafts staged for human send — the fleet never
acts as a licensed broker (Occ. Code §1101.351). Wholesale offers are blocked
by GATE-LEGAL-WHOLESALE until bilateral disclosure is attached.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from .cpr import CPR, CPRColumn, BlastTier, pv_value
from .compliance import gate_legal_wholesale
from .pricing import PricingResult
from .legal_rules import DealType
from . import policy


# --- Seller postures keyed to distress type ---
# Playbooks ONLY (framing, messaging, objection handling). The offer ladder —
# opening/target/walk-away multipliers — is owned by pricing.build_offer_ladder
# as the single source of truth (audit H3: the duplicated multiplier table that
# used to live here disagreed with pricing and was never consumed; it is gone).

POSTURES = {
    "DT-PREFORE": {
        "name": "pre-foreclosure",
        "framing": "speed and certainty",
        "key_message": "We can close in 7 days with cash. No repairs needed. You avoid the foreclosure on your record.",
        "objections": {
            "I need full price": "The foreclosure sale is in ~41 days. After that you get nothing and still have the deficiency. Cash now beats hoping for a retail buyer.",
            "I want to keep my home": "We can explore reinstatement or loan modification with your lender. If that fails, a short sale protects your credit better than a foreclosure.",
            "I need time to think": "Every day that passes reduces our ability to close before the sale date. We can extend the option period if needed.",
        },
    },
    "DT-TAXDEED": {
        "name": "tax-delinquent",
        "framing": "problem-solving, low pressure",
        "key_message": "We can pay your delinquent taxes and put cash in your pocket. You keep your credit clean and avoid a tax foreclosure.",
        "objections": {
            "I owe more in taxes than the house is worth": "We can negotiate with the taxing units. In some cases we can buy subject-to the tax lien and handle the redemption.",
            "I want to catch up on taxes myself": "If you can pay the full delinquency + penalties, that's your best option. If not, we can structure a deal before the tax sale.",
        },
    },
    "DT-AUCTION": {
        "name": "pre-auction",
        "framing": "last chance before sale",
        "key_message": "The auction is days away. Cash now is better than hoping the property doesn't sell for less than you owe.",
        "objections": {
            "The bank will credit-bid and I'll be fine": "The bank's credit bid covers their loan, not your equity. If the property sells below your loan balance, you face a deficiency judgment.",
            "I'll file bankruptcy": "Bankruptcy delays but doesn't prevent foreclosure. A Chapter 13 requires you to resume payments. A Chapter 7 still loses the house.",
        },
    },
    "DT-REO": {
        "name": "bank-owned",
        "framing": "condition and DOM leverage",
        "key_message": "We're a cash buyer with no financing contingency. We can close quickly on this REO property.",
        "objections": {
            "We have multiple offers": "Our offer is cash, as-is, no contingencies. We can close in 7 days. What's the highest offer you've received?",
            "The property is listed at market": "The property has been on the market for X days. Our offer reflects the condition and carrying costs the bank is incurring.",
        },
    },
    "DT-SHORT": {
        "name": "short sale",
        "framing": "patience — lender approval takes time",
        "key_message": "We can make a strong offer to your lender for short-sale approval. You avoid foreclosure and may qualify for deficiency waiver.",
        "objections": {
            "My lender won't approve": "We've structured short sales before. The key is a clean package and a realistic offer close to BPO value.",
            "I don't want to show financial hardship": "The lender needs to see hardship to approve. We can help you prepare the package — it's confidential.",
        },
    },
    # Audit UW-4: these six types silently inherited the bank-owned REO
    # script (an on-market owner is not a distressed seller); each now gets
    # honest framing. REO remains the last-resort default.
    "DT-WHOLESALE": {
        "name": "wholesale (investor-to-investor)",
        "framing": "clean assignable contract, transparent assignment fee",
        "key_message": "I'm an investor reselling this contract. My assignment fee is disclosed up front; you get a clear, written number and a fast close.",
        "objections": {
            "What's your assignment fee?": "Disclosed in writing before we sign — typically $5-15k on this size of deal. Transparency is the whole model.",
            "Why not buy it yourself?": "Assignment keeps my capital free; you still get the same contract price and timeline. If you'd rather I close, I can double-close with full disclosure.",
        },
    },
    "DT-ONMARKET": {
        "name": "on-market seller",
        "framing": "convenience and certainty vs a retail listing",
        "key_message": "No listing period, no showings, no repairs, no agent commission wait. Net comparison: our cash number minus zero fees versus your expected net after 5-6% commission and 30-60 days.",
        "objections": {
            "I can get more on the open market": "Possibly — after commission, closing concessions, and 45+ days of showings. Net-net, most sellers keep 90-94% of retail. Our number is 91-97% of that, today.",
            "Is this a lowball offer?": "It's the arithmetic of certainty: no financing fallout, no inspection renegotiation, no 45-day wait. You can compare it side by side with a net-listing sheet.",
        },
    },
    "DT-OFFMARKET": {
        "name": "off-market owner",
        "framing": "no-pressure, direct, private",
        "key_message": "I buy directly, privately, as-is. No listing, no strangers through the house, no commissions. If the numbers work for you, we can close on your timeline.",
        "objections": {
            "How did you get my information?": "Public county records — ownership and tax status are public record. I reach out respectfully; a simple no is a complete answer.",
            "I wasn't planning to sell": "Many owners aren't — until they see a clean cash number. If the number doesn't work, keep it for reference; markets change.",
        },
    },
    "DT-SUBTO": {
        "name": "subject-to (existing loan stays in place)",
        "framing": "transparent about the transfer mechanics",
        "key_message": "We take title subject to your existing mortgage — the payment stays current automatically, your credit is protected, and you walk away from the payment obligation today.",
        "objections": {
            "What happens to my loan?": "It stays in your name; we make every payment on time via a documented servicing arrangement. The transfer may give the lender a due-on-sale option — we disclose that risk to you in writing before contracting.",
            "Is this even legal?": "Yes — taking title subject-to an existing mortgage is a standard, disclosed transfer. You'll see the exact mechanics in the contract, and your attorney is welcome to review everything.",
        },
    },
    "DT-RENTAL": {
        "name": "tired landlord",
        "framing": "exit from management burden",
        "key_message": "We buy rental properties as-is, with tenants in place, no repairs, and no vacancy wait. You exchange a management headache for cash.",
        "objections": {
            "I'd get more listing it vacant": "Maybe, after vacancy, make-ready, turnover, and 60-90 days. Our number is net of all of that, closed on your date, tenants handled.",
            "What about my tenants?": "We honor existing leases and security deposits to the letter — no eviction surprise dumped on you or them.",
        },
    },
    "DT-BRRRR": {
        "name": "BRRRR acquisition (retail-quality buy box)",
        "framing": "verified-buy-box purchase, no distressed-seller pressure",
        "key_message": "We buy to renovate and hold long term — clean, fast, financed close with no contingency ladder. The numbers only work for both of us at a fair price, so we won't waste your time.",
        "objections": {
            "Your offer is below comps": "Comps assume retail condition and timelines. Ours prices today's condition plus the cost and time of the work — that spread is the business.",
            "Why should I care about your strategy?": "You shouldn't beyond this: cash, as-is, your timeline, no financing contingency.",
        },
    },
}


@dataclass
class OfferPackage:
    """A complete offer package staged for human review and send."""
    deal_type: str
    offer_price: float
    earnest_money: float
    option_fee: float
    option_period_days: int
    closing_timeline_days: int
    is_cash: bool
    has_and_or_assigns: bool = False
    disclosure_attached: bool = False
    seller_posture: str = ""
    key_message: str = ""
    objection_handling: dict = field(default_factory=dict)
    offer_letter: str = ""
    compliance_status: str = ""
    blocked: bool = False
    block_reason: str = ""


def get_posture(deal_type: str) -> dict:
    """Get seller posture playbook for the deal type."""
    return POSTURES.get(deal_type, POSTURES["DT-REO"])  # default to REO


def generate_offer(cpr: CPR, pricing: PricingResult) -> OfferPackage:
    """Generate an offer package from CPR + pricing.

    All comms are drafts — the fleet never sends anything.
    Wholesale offers are blocked until bilateral disclosure is attached.
    """
    # Handle both raw string and ProvenanceField for deal_type
    dt = cpr.deal_type
    deal_type = dt.value if hasattr(dt, 'value') else (dt or 'DT-REO')
    posture = get_posture(deal_type)
    ladder = pricing.offer_ladder

    # Base offer terms
    offer_price = ladder.get("opening", 0)
    earnest_money = max(offer_price * policy.EARNEST_MONEY_PCT, policy.EARNEST_MONEY_MIN)  # 1% or $1K min
    option_fee = max(offer_price * policy.OPTION_FEE_PCT, policy.OPTION_FEE_MIN)  # ~0.1% or $200 min
    option_period = (policy.OPTION_PERIOD_DAYS if deal_type != "DT-SHORT"
                     else policy.OPTION_PERIOD_DAYS_EXTENDED)  # short sales need more time
    closing_days = (policy.CLOSING_TIMELINE_DAYS if deal_type != "DT-SHORT"
                    else policy.CLOSING_TIMELINE_DAYS_MAX)
    is_cash = deal_type in ("DT-AUCTION", "DT-PREFORE", "DT-REO")

    # Wholesale-specific — GATE-LEGAL-WHOLESALE is the single compliance
    # authority (X6 fix: the hardcoded block duplicated the gate's statute
    # citations and could drift from it; now the gate's own verdict/reason is
    # what blocks the package).
    has_and_or_assigns = (deal_type == "DT-WHOLESALE")
    disclosure_attached = False  # must be manually attached by a human
    wholesale_gate = gate_legal_wholesale(cpr, has_disclosure=disclosure_attached)
    blocked = wholesale_gate.verdict != "pass"
    block_reason = ""
    if blocked:
        block_reason = (
            f"BLOCKED — GATE-LEGAL-WHOLESALE {wholesale_gate.verdict.upper()} — "
            f"{wholesale_gate.reason}"
        )

    # Generate offer letter
    letter = _render_offer_letter(
        cpr=cpr, offer_price=offer_price, earnest_money=earnest_money,
        option_fee=option_fee, option_period=option_period,
        closing_days=closing_days, is_cash=is_cash,
        posture=posture, has_and_or_assigns=has_and_or_assigns,
        blocked=blocked, block_reason=block_reason, deal_type=deal_type,
    )

    compliance = "BLOCKED" if blocked else "PENDING — all comms are drafts for human review and send"

    return OfferPackage(
        deal_type=deal_type,
        offer_price=offer_price,
        earnest_money=earnest_money,
        option_fee=option_fee,
        option_period_days=option_period,
        closing_timeline_days=closing_days,
        is_cash=is_cash,
        has_and_or_assigns=has_and_or_assigns,
        disclosure_attached=disclosure_attached,
        seller_posture=posture["name"],
        key_message=posture["key_message"],
        objection_handling=posture["objections"],
        offer_letter=letter,
        compliance_status=compliance,
        blocked=blocked,
        block_reason=block_reason,
    )


def _render_offer_letter(cpr: CPR, offer_price: float, earnest_money: float,
                          option_fee: float, option_period: int, closing_days: int,
                          is_cash: bool, posture: dict, has_and_or_assigns: bool,
                          blocked: bool, block_reason: str, deal_type: str = "") -> str:
    """Render a draft offer letter for human review."""
    lines = []
    lines.append(f"DRAFT OFFER — {cpr.situs_address}")
    lines.append(f"Parcel: {cpr.parcel_id} | County: {cpr.county.title()}")
    lines.append(f"Deal type: {deal_type} | Posture: {posture['name']}")
    lines.append("")
    lines.append(f"Offer price: ${offer_price:,.0f}")
    lines.append(f"Earnest money: ${earnest_money:,.0f} (to escrow within 3 days of effective date)")
    lines.append(f"Option fee: ${option_fee:,.0f} (buys unrestricted termination right for {option_period} days)")
    lines.append(f"Closing timeline: {closing_days} days from effective date")
    lines.append(f"Financing: {'Cash — no financing contingency' if is_cash else 'Conventional/DSCR financing'}")
    if has_and_or_assigns:
        lines.append('Buyer: "[Buyer Name] and/or assigns"')
    lines.append("")
    lines.append(f"Key message: {posture['key_message']}")
    lines.append("")
    if posture["objections"]:
        lines.append("OBJECTION HANDLING (for human negotiator reference):")
        for objection, response in posture["objections"].items():
            lines.append(f"  Q: {objection}")
            lines.append(f"  A: {response}")
            lines.append("")
    if blocked:
        lines.append(f"⚠ COMPLIANCE GATE: {block_reason}")
        lines.append("")
    lines.append("NOTE: This is a DRAFT generated by the fleet for human review.")
    lines.append("The fleet does not represent parties, list on MLS, or send communications.")
    lines.append("All party-facing communications must be reviewed and sent by a human.")
    return "\n".join(lines)


# --- End-buyer disposition ---

@dataclass
class EndBuyer:
    """A buyer in the investor database."""
    name: str
    buy_box_min: float
    buy_box_max: float
    counties: list[str]
    preference: str  # "flip", "rental", "wholesale"
    contact: str = ""


def match_buyer(cpr: CPR, buyers: list[EndBuyer], disposition: str = "") -> list[EndBuyer]:
    """Match a CPR to end-buyers whose buy-box fits.

    D5 fix: the screen price is the GATED ARV when the valuation carries one —
    buyers buy future value, not the CAD appraisal — falling back to
    cad_appraised_value, and to 0 when neither exists (a $0 screen matches
    nobody, which surfaces missing-valuation data instead of faking matches).

    EndBuyer.preference is honored against ``disposition`` when the caller
    passes one (e.g. "flip" / "rental" / "wholesale"); DT-WHOLESALE CPRs infer
    disposition "wholesale", because only assignment buyers can take a
    wholesale deal.
    """
    arv = pv_value(cpr.valuation.arv) if (cpr.valuation and cpr.valuation.arv) else None
    cad = pv_value(cpr.cad_appraised_value) if cpr.cad_appraised_value else None
    if arv is not None:
        screen_price = arv
    elif cad is not None:
        screen_price = cad
    else:
        screen_price = 0  # no valuation data at all → match nobody, loudly

    dt_raw = pv_value(getattr(cpr, "deal_type", "")) or ""
    inferred = "wholesale" if dt_raw == DealType.WHOLESALE.value else ""
    want = (disposition or inferred).lower()

    matches = []
    for b in buyers:
        if cpr.county in b.counties or "all" in b.counties:
            if not (b.buy_box_min <= screen_price <= b.buy_box_max):
                continue
            if want and b.preference.lower() != want:
                continue
            matches.append(b)
    return matches


if __name__ == "__main__":
    from .cpr import CPR, ProvenanceField, ValuationObject, LienRecord, CPRColumn, BlastTier
    from .pricing import PricingResult, SensitivityCell

    # Self-check: offer generation for a flip
    cpr = CPR(parcel_id="4003592", county="collin",
              situs_address="10022 Plainsman Ln, Frisco, TX 75035",
              deal_type="DT-PREFORE", channel="foreclosure_notice")
    cpr.set_field("cad_appraised_value", 730_673, "collin_cad", 0.90)

    pricing = PricingResult(
        deal_type="DT-PREFORE",
        mao=304_064,
        bid_ceiling=300_000,
        offer_ladder={"opening": 243_251, "target": 304_064, "walk_away": 319_267},
        blast_tier=BlastTier.T3,
        sensitivity=[SensitivityCell(0, 0, 100_000, 0.13)],
    )

    offer = generate_offer(cpr, pricing)
    assert offer.offer_price == 243_251
    assert offer.is_cash == True
    assert offer.option_period_days == 7
    assert offer.blocked == False
    assert "DRAFT OFFER" in offer.offer_letter
    assert "speed and certainty" in get_posture("DT-PREFORE")["framing"]
    assert "cash" in offer.key_message.lower()
    print(f"negotiation.py: flip offer — ${offer.offer_price:,.0f}, posture={offer.seller_posture}")

    # Self-check: wholesale blocked — by GATE-LEGAL-WHOLESALE's own verdict (X6)
    cpr2 = CPR(parcel_id="test", county="collin",
               situs_address="100 Test St", deal_type="DT-WHOLESALE")
    pricing2 = PricingResult(
        deal_type="DT-WHOLESALE", mao=180_000, bid_ceiling=180_000,
        offer_ladder={"opening": 153_000, "target": 180_000, "walk_away": 189_000},
        blast_tier=BlastTier.T1,
    )
    offer2 = generate_offer(cpr2, pricing2)
    assert offer2.blocked == True
    assert "GATE-LEGAL-WHOLESALE FAIL" in offer2.block_reason
    assert "bilateral" in offer2.block_reason.lower()  # gate reason carried through
    assert "BLOCKED" in offer2.offer_letter
    print(f"negotiation.py: wholesale blocked via compliance gate — {offer2.block_reason[:70]}")

    # Non-wholesale offers are untouched by the wholesale gate
    assert offer.blocked == False and offer.compliance_status.startswith("PENDING")

    # Posture playbooks survive; the dead multiplier columns are gone
    assert "framing" in get_posture("DT-PREFORE")
    assert not any(
        k in get_posture(dt) for dt in POSTURES for k in
        ("opening_mult", "target_mult", "walk_away_mult")
    ), "dead multiplier table must stay deleted — pricing owns the ladder"

    # Self-check: buyer matching — $730K CAD only matches buyer B (400-800K range)
    buyers = [
        EndBuyer("Flip Investor A", 200_000, 500_000, ["collin"], "flip"),
        EndBuyer("Retail Buyer B", 400_000, 800_000, ["collin", "dallas"], "rental"),
        EndBuyer("Wholesale C", 100_000, 300_000, ["all"], "wholesale"),
    ]
    matches = match_buyer(cpr, buyers)
    assert len(matches) == 1  # only B matches ($730K in 400K-800K)
    assert matches[0].name == "Retail Buyer B"
    print(f"negotiation.py: buyer match (CAD fallback) — {matches[0].name}")

    # D5: gated ARV drives the screen when present
    from .cpr import ValuationObject, ProvenanceField as _PF
    cpr_arv = CPR(parcel_id="4003593", county="collin",
                  situs_address="101 Plainsman Ln, Frisco, TX 75035",
                  deal_type="DT-PREFORE")
    cpr_arv.set_field("cad_appraised_value", 900_000, "collin_cad", 0.90)  # CAD alone → nobody
    assert match_buyer(cpr_arv, buyers) == []
    cpr_arv.valuation = ValuationObject(
        arv=_PF(value=450_000, source="mls_agent", confidence=0.85),
        comp_source="mls_agent")
    matches_arv = match_buyer(cpr_arv, buyers)  # ARV 450K fits A (200-500) and B (400-800)
    assert {m.name for m in matches_arv} == {"Flip Investor A", "Retail Buyer B"}

    # D5: EndBuyer.preference honored against an explicit disposition
    matches_rental = match_buyer(cpr_arv, buyers, disposition="rental")
    assert [m.name for m in matches_rental] == ["Retail Buyer B"]

    # D5: wholesale deals infer disposition "wholesale" — only assignment buyers
    cpr_ws = CPR(parcel_id="ws1", county="collin", situs_address="5 Flip Rd",
                 deal_type="DT-WHOLESALE")
    cpr_ws.set_field("cad_appraised_value", 250_000, "collin_cad", 0.9)
    matches_ws = match_buyer(cpr_ws, buyers)  # price fits A and C; preference keeps C
    assert [m.name for m in matches_ws] == ["Wholesale C"]
    print("negotiation.py: D5 — ARV-first screening + preference honoring verified")

    print("negotiation.py: all self-checks passed")