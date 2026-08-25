# src/negotiation.py
"""AGENT-NEG: seller playbooks + offer generation + disposition.

All party-facing comms are drafts staged for human send — the fleet never
acts as a licensed broker (Occ. Code §1101.351). Wholesale offers are blocked
by GATE-LEGAL-WHOLESALE until bilateral disclosure is attached.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from .cpr import CPR, CPRColumn, BlastTier
from .pricing import PricingResult
from .legal_rules import DealType, WHOLESALE_BILATERAL_DISCLOSURE


# --- Seller postures keyed to distress type ---

POSTURES = {
    "DT-PREFORE": {
        "name": "pre-foreclosure",
        "opening_mult": 0.80,  # aggressive — time pressure on seller
        "target_mult": 0.95,
        "walk_away_mult": 1.05,
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
        "opening_mult": 0.75,
        "target_mult": 0.90,
        "walk_away_mult": 1.05,
        "framing": "problem-solving, low pressure",
        "key_message": "We can pay your delinquent taxes and put cash in your pocket. You keep your credit clean and avoid a tax foreclosure.",
        "objections": {
            "I owe more in taxes than the house is worth": "We can negotiate with the taxing units. In some cases we can buy subject-to the tax lien and handle the redemption.",
            "I want to catch up on taxes myself": "If you can pay the full delinquency + penalties, that's your best option. If not, we can structure a deal before the tax sale.",
        },
    },
    "DT-AUCTION": {
        "name": "pre-auction",
        "opening_mult": 0.80,
        "target_mult": 0.95,
        "walk_away_mult": 1.05,
        "framing": "last chance before sale",
        "key_message": "The auction is days away. Cash now is better than hoping the property doesn't sell for less than you owe.",
        "objections": {
            "The bank will credit-bid and I'll be fine": "The bank's credit bid covers their loan, not your equity. If the property sells below your loan balance, you face a deficiency judgment.",
            "I'll file bankruptcy": "Bankruptcy delays but doesn't prevent foreclosure. A Chapter 13 requires you to resume payments. A Chapter 7 still loses the house.",
        },
    },
    "DT-REO": {
        "name": "bank-owned",
        "opening_mult": 0.90,
        "target_mult": 1.00,
        "walk_away_mult": 1.05,
        "framing": "condition and DOM leverage",
        "key_message": "We're a cash buyer with no financing contingency. We can close quickly on this REO property.",
        "objections": {
            "We have multiple offers": "Our offer is cash, as-is, no contingencies. We can close in 7 days. What's the highest offer you've received?",
            "The property is listed at market": "The property has been on the market for X days. Our offer reflects the condition and carrying costs the bank is incurring.",
        },
    },
    "DT-SHORT": {
        "name": "short sale",
        "opening_mult": 0.85,
        "target_mult": 0.95,
        "walk_away_mult": 1.05,
        "framing": "patience — lender approval takes time",
        "key_message": "We can make a strong offer to your lender for short-sale approval. You avoid foreclosure and may qualify for deficiency waiver.",
        "objections": {
            "My lender won't approve": "We've structured short sales before. The key is a clean package and a realistic offer close to BPO value.",
            "I don't want to show financial hardship": "The lender needs to see hardship to approve. We can help you prepare the package — it's confidential.",
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
    earnest_money = max(offer_price * 0.01, 1_000)  # 1% or $1K min
    option_fee = max(offer_price * 0.001, 200)  # ~0.1% or $200 min
    option_period = 7 if deal_type != "DT-SHORT" else 14  # short sales need more time
    closing_days = 7 if deal_type != "DT-SHORT" else 45
    is_cash = deal_type in ("DT-AUCTION", "DT-PREFORE", "DT-REO")

    # Wholesale-specific
    has_and_or_assigns = (deal_type == "DT-WHOLESALE")
    disclosure_attached = False  # must be manually attached
    blocked = False
    block_reason = ""

    if deal_type == "DT-WHOLESALE" and not disclosure_attached:
        blocked = True
        block_reason = (
            "BLOCKED — bilateral written equitable-interest disclosure required "
            "(TX Prop. Code §5.0205 / SB 1577, eff. Jan 2024). "
            "Generate disclosure to BOTH seller and buyer before any contract or marketing."
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


def match_buyer(cpr: CPR, buyers: list[EndBuyer]) -> list[EndBuyer]:
    """Match a CPR to end-buyers whose buy-box fits."""
    # ponytail: simple filter — match county and price range
    cad = cpr.cad_appraised_value.value if cpr.cad_appraised_value else 0
    matches = []
    for b in buyers:
        if cpr.county in b.counties or "all" in b.counties:
            if b.buy_box_min <= cad <= b.buy_box_max:
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

    # Self-check: wholesale blocked
    cpr2 = CPR(parcel_id="test", county="collin",
               situs_address="100 Test St", deal_type="DT-WHOLESALE")
    pricing2 = PricingResult(
        deal_type="DT-WHOLESALE", mao=180_000, bid_ceiling=180_000,
        offer_ladder={"opening": 153_000, "target": 180_000, "walk_away": 189_000},
        blast_tier=BlastTier.T1,
    )
    offer2 = generate_offer(cpr2, pricing2)
    assert offer2.blocked == True
    assert "bilateral" in offer2.block_reason.lower()
    assert "BLOCKED" in offer2.offer_letter
    print(f"negotiation.py: wholesale blocked — {offer2.block_reason[:60]}")

    # Self-check: buyer matching — $730K CAD only matches buyer B (400-800K range)
    buyers = [
        EndBuyer("Flip Investor A", 200_000, 500_000, ["collin"], "flip"),
        EndBuyer("Retail Buyer B", 400_000, 800_000, ["collin", "dallas"], "rental"),
        EndBuyer("Wholesale C", 100_000, 300_000, ["all"], "wholesale"),
    ]
    matches = match_buyer(cpr, buyers)
    assert len(matches) == 1  # only B matches ($730K in 400K-800K)
    assert matches[0].name == "Retail Buyer B"
    print(f"negotiation.py: buyer match — {matches[0].name}")

    print("negotiation.py: all self-checks passed")