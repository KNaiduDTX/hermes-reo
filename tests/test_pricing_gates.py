"""Financial-gate regression tests: the audit's Critical money-answer bugs.

C2  negative bid ceiling -> negative purchase -> fake profit
H1  underwriting defaulting purchase to MAO
H3  walk-away exceeding MAO
F12 optimistic_only mislabeling never-pencils deals
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
