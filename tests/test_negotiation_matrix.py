# tests/test_negotiation_matrix.py
"""generate_offer matrix over DealTypes/postures (FN5: 0%-coverage money path).

Pins the offer-package contract of ``src/negotiation.generate_offer``:
- earnest/option-fee policy math against src.policy scalars (1% / $1,000 min
  earnest; ~0.1% / $200 min option fee),
- option-period and closing-timeline windows (standard 7/7 vs DT-SHORT
  extended 14/45),
- cash vs financed wording branches and posture playbooks per deal type,
- the DT-WHOLESALE BLOCKED path, where blocking authority belongs to
  GATE-LEGAL-WHOLESALE via src.compliance.gate_legal_wholesale (X6), and
- every rendered letter section for human review.
"""
import pytest

from src import policy
from src.cpr import CPR
from src.negotiation import POSTURES, generate_offer, get_posture
from src.pricing import PricingResult


# --- fixtures/helpers -------------------------------------------------------

CASH_DEAL_TYPES = ["DT-AUCTION", "DT-PREFORE", "DT-REO"]
FINANCED_DEAL_TYPES = ["DT-SHORT", "DT-TAXDEED", "DT-OFFMARKET"]

POSTURE_BY_DEAL_TYPE = {
    "DT-PREFORE": "pre-foreclosure",
    "DT-TAXDEED": "tax-delinquent",
    "DT-AUCTION": "pre-auction",
    "DT-REO": "bank-owned",
    "DT-SHORT": "short sale",
}


def _cpr(deal_type="DT-PREFORE", **overrides):
    fields = dict(
        parcel_id="P-TEST",
        county="collin",
        situs_address="100 Offer Ave, Frisco, TX 75035",
        deal_type=deal_type,
        channel="foreclosure_notice",
    )
    fields.update(overrides)
    return CPR(**fields)


def _pricing(deal_type="DT-PREFORE", opening=153_000):
    mao = opening + 27_000
    return PricingResult(
        deal_type=deal_type,
        mao=mao,
        bid_ceiling=mao - 5_000,
        offer_ladder={"opening": opening, "target": mao,
                      "walk_away": mao - 5_000},
    )


def _package(deal_type="DT-PREFORE", opening=153_000):
    return generate_offer(_cpr(deal_type), _pricing(deal_type, opening))


# --- offer price + policy math ----------------------------------------------

def test_offer_price_taken_from_ladder_opening():
    package = _package("DT-PREFORE", opening=153_000)

    assert package.offer_price == 153_000
    assert "$153,000" in package.offer_letter


@pytest.mark.parametrize(
    "opening, expected_earnest",
    [
        (153_000, 1530.0),   # 1% branch above the floor
        (340_000, 3400.0),   # comfortably above the floor
        (51_000, 1_000),     # 1% would be $510 -> EARNEST_MONEY_MIN applies
    ],
    ids=["one-percent", "large-offer", "minimum-floor"],
)
def test_earnest_money_policy_math(opening, expected_earnest):
    package = _package("DT-AUCTION", opening=opening)

    assert package.earnest_money == expected_earnest
    assert package.earnest_money >= max(
        package.offer_price * policy.EARNEST_MONEY_PCT,
        policy.EARNEST_MONEY_MIN) - 1e-9


@pytest.mark.parametrize(
    "opening, expected_option_fee",
    [
        (340_000, 340.0),   # ~0.1% branch above the floor
        (153_000, 200.0),   # ~0.1% would be $153 -> OPTION_FEE_MIN applies
        (60_000, 200.0),    # deep floor territory
    ],
    ids=["tenth-percent", "minimum-floor", "small-offer"],
)
def test_option_fee_policy_math(opening, expected_option_fee):
    package = _package("DT-PREFORE", opening=opening)

    assert package.option_fee == expected_option_fee


def test_standard_option_period_and_closing_timeline():
    package = _package("DT-AUCTION")

    assert package.option_period_days == policy.OPTION_PERIOD_DAYS
    assert package.closing_timeline_days == policy.CLOSING_TIMELINE_DAYS


def test_short_sale_gets_extended_windows():
    package = _package("DT-SHORT")

    assert package.option_period_days == policy.OPTION_PERIOD_DAYS_EXTENDED
    assert package.closing_timeline_days == policy.CLOSING_TIMELINE_DAYS_MAX


# --- cash vs financed branches ----------------------------------------------

@pytest.mark.parametrize("deal_type", CASH_DEAL_TYPES)
def test_cash_deal_types_flag_and_wording(deal_type):
    package = _package(deal_type)

    assert package.is_cash is True
    assert "Cash — no financing contingency" in package.offer_letter
    assert "Conventional/DSCR financing" not in package.offer_letter


@pytest.mark.parametrize("deal_type", FINANCED_DEAL_TYPES)
def test_financed_deal_types_flag_and_wording(deal_type):
    package = _package(deal_type)

    assert package.is_cash is False
    assert "Conventional/DSCR financing" in package.offer_letter
    assert "Cash — no financing contingency" not in package.offer_letter


# --- posture playbooks -------------------------------------------------------

@pytest.mark.parametrize(
    "deal_type, expected_posture",
    sorted(POSTURE_BY_DEAL_TYPE.items()),
    ids=[dt.lower() for dt in sorted(POSTURE_BY_DEAL_TYPE)],
)
def test_posture_playbook_selected_per_deal_type(deal_type, expected_posture):
    package = _package(deal_type)
    playbook = get_posture(deal_type)

    assert package.seller_posture == expected_posture
    assert playbook["name"] == expected_posture
    assert playbook["framing"] in POSTURES[deal_type]["framing"]
    assert package.key_message == playbook["key_message"] != ""
    assert package.objection_handling == playbook["objections"] != {}


def test_unknown_deal_type_defaults_to_bank_owned_playbook():
    package = _package("DT-ONMARKET")
    default_playbook = get_posture("DT-NOT-A-REAL-TYPE")

    assert package.seller_posture == "bank-owned"
    assert default_playbook is POSTURES["DT-REO"]
    assert default_playbook["framing"] == "condition and DOM leverage"


def test_ladder_multipliers_stay_out_of_negotiation_playbooks():
    """pricing.build_offer_ladder owns the ladder; playbooks stay prose-only."""
    for deal_type in POSTURES:
        playbook = get_posture(deal_type)
        assert not any(k in playbook for k in
                       ("opening_mult", "target_mult", "walk_away_mult"))


# --- DT-WHOLESALE BLOCKED path (compliance-gate authority) -------------------

def test_wholesale_without_disclosure_is_blocked_by_compliance_gate():
    package = _package("DT-WHOLESALE", opening=85_000)

    assert package.blocked is True
    assert package.has_and_or_assigns is True
    assert package.disclosure_attached is False  # only a human may attach
    assert package.compliance_status == "BLOCKED"
    # The gate's own verdict/reason is carried through verbatim (X6).
    assert package.block_reason.startswith("BLOCKED — GATE-LEGAL-WHOLESALE FAIL")
    assert "Bilateral disclosure required before contracting" in \
        package.block_reason
    assert "§5.0205" in package.block_reason and "SB 1577" in package.block_reason


def test_wholesale_block_renders_gate_line_in_letter():
    package = _package("DT-WHOLESALE")

    assert "⚠ COMPLIANCE GATE:" in package.offer_letter
    assert "GATE-LEGAL-WHOLESALE FAIL" in package.offer_letter
    assert 'Buyer: "[Buyer Name] and/or assigns"' in package.offer_letter
    assert package.offer_price > 0  # terms are drafted; sending stays blocked


def test_wholesale_gate_is_the_blocking_authority_not_a_hardcode():
    """The block must come from gate_legal_wholesale's verdict (X6 contract)."""
    from src.compliance import gate_legal_wholesale

    cpr = _cpr("DT-WHOLESALE")
    gate = gate_legal_wholesale(cpr, has_disclosure=False)
    package = generate_offer(cpr, _pricing("DT-WHOLESALE"))

    assert gate.verdict == "fail"
    assert gate.reason.split("(")[0].strip() in package.block_reason
    # Non-wholesale deals sail past the same gate untouched.
    non_ws = _package("DT-AUCTION")
    assert non_ws.blocked is False
    assert non_ws.compliance_status.startswith("PENDING")


@pytest.mark.parametrize(
    "deal_type",
    ["DT-AUCTION", "DT-PREFORE", "DT-REO", "DT-TAXDEED", "DT-SHORT"],
)
def test_non_wholesale_packages_are_never_blocked(deal_type):
    package = _package(deal_type)

    assert package.blocked is False
    assert package.block_reason == ""
    assert package.compliance_status.startswith("PENDING")
    assert "COMPLIANCE GATE" not in package.offer_letter
    assert "and/or assigns" not in package.offer_letter


# --- letter sections --------------------------------------------------------

LETTER_SECTIONS = [
    "DRAFT OFFER — 100 Offer Ave, Frisco, TX 75035",
    "Parcel: P-TEST | County: Collin",
    "Deal type: DT-PREFORE | Posture: pre-foreclosure",
    "Offer price: $153,000",
    "Earnest money: $1,530",
    "(to escrow within 3 days of effective date)",
    "Option fee:",
    "(buys unrestricted termination right for 7 days)",
    "Closing timeline: 7 days from effective date",
    "Key message:",
    "OBJECTION HANDLING (for human negotiator reference):",
]


@pytest.mark.parametrize("section", LETTER_SECTIONS)
def test_offer_letter_sections_present(section):
    letter = _package("DT-PREFORE").offer_letter

    assert section in letter


def test_offer_letter_objections_rendered_as_qa_pairs():
    objections = get_posture("DT-PREFORE")["objections"]
    letter = _package("DT-PREFORE").offer_letter

    for objection, response in objections.items():
        assert f"Q: {objection}" in letter
        assert f"A: {response}" in letter


def test_offer_letter_carries_draft_only_footer():
    letter = _package("DT-REO").offer_letter

    assert "NOTE: This is a DRAFT generated by the fleet for human review." \
        in letter
    assert "must be reviewed and sent by a human" in letter


@pytest.mark.parametrize(
    "deal_type",
    ["DT-AUCTION", "DT-PREFORE", "DT-REO", "DT-TAXDEED", "DT-SHORT",
     "DT-WHOLESALE"],
)
def test_disclosure_attached_is_always_false_for_generated_packages(deal_type):
    """No generated package ever self-attests disclosure — human-only act."""
    package = _package(deal_type)

    assert package.disclosure_attached is False
