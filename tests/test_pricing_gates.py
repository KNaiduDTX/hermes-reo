"""Financial-gate regression tests: the audit's Critical money-answer bugs.

C2  negative bid ceiling -> negative purchase -> fake profit
H1  underwriting defaulting purchase to MAO
H3  walk-away exceeding MAO
F12 optimistic_only mislabeling never-pencils deals
M6  bid ceiling subtracting liens that do not survive the deal type
"""
import pytest

from src.cpr import CPR, LienRecord
from src.pricing import (
    PENCIL_BASE,
    PENCIL_NEVER,
    PENCIL_OPTIMISTIC_ONLY,
    build_offer_ladder,
    compute_bid_ceiling,
    price_deal,
)
from src.underwriting import flip_pnl, route_underwrite


LIEN = LienRecord(lien_type="tax", amount=100_000.0,
                  survives_foreclosure=True, survives_tax_sale=True)


def test_ceiling_clamps_at_zero_with_surviving_liens():
    """AUDIT C2: ARV 200k / rehab 50k / 100k surviving lien used to give -10k."""
    ceiling = compute_bid_ceiling(arv=200_000, rehab=50_000, factor=0.70,
                                  mao_val=140_000, surviving_liens=[LIEN])
    assert ceiling == 0


def test_ceiling_never_exceeds_mao():
    ceiling = compute_bid_ceiling(arv=500_000, rehab=20_000, factor=0.70,
                                  mao_val=150_000)
    assert ceiling <= 150_000


# --- AUDIT M6: deal-type-aware lien survival selection ---

FC_ONLY = LienRecord(lien_type="irs_federal", amount=100_000.0,
                     survives_foreclosure=True, survives_tax_sale=False)
TAX_ONLY = LienRecord(lien_type="tax", amount=100_000.0,
                      survives_foreclosure=False, survives_tax_sale=True)

# Raw equity before liens: 200k * 0.70 - 50k rehab = 90k.
_ARV, _REHAB, _FACTOR, _MAO = 200_000.0, 50_000.0, 0.70, 140_000.0


def test_unknown_or_empty_deal_type_keeps_legacy_union():
    """Backward compat: without a deal type either survivor nets out."""
    assert compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                               mao_val=_MAO, surviving_liens=[FC_ONLY]) == 0
    assert compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                               mao_val=_MAO, surviving_liens=[TAX_ONLY]) == 0


def test_taxdeed_ceiling_ignores_foreclosure_only_survivor():
    """AUDIT M6: a tax-deed bid does not pay off liens that die at tax sale.

    The IRS lien (foreclosure-only survivor) must be excluded from a
    DT-TAXDEED ceiling: raw equity 90k stays intact instead of clamping
    to 0 under the legacy union.
    """
    ceiling = compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                                  mao_val=_MAO, surviving_liens=[FC_ONLY],
                                  deal_type="DT-TAXDEED")
    assert ceiling == 90_000
    # Case-insensitive matching.
    assert compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                               mao_val=_MAO, surviving_liens=[FC_ONLY],
                               deal_type="dt-taxdeed") == 90_000


def test_auction_ceiling_ignores_tax_sale_only_survivor():
    """AUDIT M6: an auction bid does not pay off tax-sale-only survivors."""
    ceiling = compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                                  mao_val=_MAO, surviving_liens=[TAX_ONLY],
                                  deal_type="DT-AUCTION")
    assert ceiling == 90_000


def test_price_deal_passes_deal_type_to_ceiling():
    """price_deal threads deal_type through to compute_bid_ceiling (M6).

    Same CPR, same numbers: the foreclosure-only IRS survivor is ignored
    for DT-TAXDEED (alive at a 90k ceiling) but honored for DT-AUCTION
    (ceiling swamped -> dead).
    """
    cpr = CPR(parcel_id="M6", county="collin", situs_address="1 Main St")
    cpr.add_lien(FC_ONLY, "test")

    priced_td = price_deal(cpr, arv=_ARV, rehab=_REHAB,
                           deal_type="DT-TAXDEED")
    assert priced_td.bid_ceiling == 90_000
    assert priced_td.dead_reason is None

    priced_auc = price_deal(cpr, arv=_ARV, rehab=_REHAB,
                            deal_type="DT-AUCTION")
    assert priced_auc.bid_ceiling == 0
    assert priced_auc.dead_reason is not None


def test_route_underwrite_requires_purchase():
    """AUDIT H1: silently pricing a deal at its own MAO is self-confirming."""
    cpr = CPR(parcel_id="P", county="collin", situs_address="1 Main St")
    with pytest.raises(ValueError):
        route_underwrite(cpr, arv=300_000, rehab=30_000)


def test_walk_away_capped_at_mao():
    """AUDIT H3: ladder used to offer walk-away = 105% of the stated maximum."""
    ladder = build_offer_ladder(mao_val=300_000, bid_ceiling_val=400_000,
                                seller_posture="standard")
    assert ladder["walk_away"] <= 300_000 + 1e-6
    assert ladder["opening"] <= ladder["target"] <= ladder["walk_away"]


def test_walk_away_capped_at_bid_ceiling():
    ladder = build_offer_ladder(mao_val=300_000, bid_ceiling_val=200_000,
                                seller_posture="standard")
    assert ladder["walk_away"] <= 200_000 + 1e-6


def test_flip_still_pencils_on_sane_inputs():
    uw = flip_pnl(arv=300_000, purchase=180_000, rehab=30_000,
                  holding=5_000, financing=10_000, closing_buy=5_000,
                  closing_sell=24_000)
    assert uw.net_profit > 0
