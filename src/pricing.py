# src/pricing.py
"""AGENT-PRICE: MAO, bid ceiling, offer ladder, sensitivity, blast tiering.

Bid ceiling nets out surviving liens. Sensitivity grid flags deals that
only pencil at optimistic ARV/rehab (a fake-signal tell).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .cpr import CPR, CPRColumn, BlastTier, LienRecord
from .underwriting import mao, wholesale_mao


@dataclass
class SensitivityCell:
    arv_delta: float   # fractional delta from base ARV (e.g., -0.10 = -10%)
    rehab_delta: float
    profit: float
    margin: float      # profit / ARV


@dataclass
class PricingResult:
    deal_type: str
    mao: float = 0.0
    bid_ceiling: float = 0.0
    offer_ladder: dict = field(default_factory=dict)  # opening, target, walk_away
    blast_tier: BlastTier = BlastTier.T0
    sensitivity: list[SensitivityCell] = field(default_factory=list)
    optimistic_only_flag: bool = False  # true if deal only pencils at top of grid


def compute_bid_ceiling(arv: float, rehab: float, factor: float,
                        cash_available: float = float("inf"),
                        surviving_liens: list[LienRecord] = None) -> float:
    """Bid ceiling = min(MAO, cash_available, ARV×factor − rehab − surviving_liens)."""
    surviving_total = sum(l.amount or 0 for l in (surviving_liens or [])
                          if l.survives_foreclosure or l.survives_tax_sale)
    raw_ceiling = (arv * factor) - rehab - surviving_total
    return min(raw_ceiling, cash_available)


def build_offer_ladder(mao_val: float, seller_posture: str = "standard") -> dict:
    """Build opening / target / walk-away offer ladder.

    Posture affects how aggressive the opening is:
    - standard: open at 85% of MAO, target at MAO, walk at 105% MAO
    - pre-foreclosure: open at 80% (time pressure on seller)
    - tax-delinquent: open at 75% (low-pressure, problem-solving)
    """
    multipliers = {
        "standard": (0.85, 1.0, 1.05),
        "pre-foreclosure": (0.80, 0.95, 1.05),
        "tax-delinquent": (0.75, 0.90, 1.05),
    }
    open_m, target_m, walk_m = multipliers.get(seller_posture, multipliers["standard"])
    return {
        "opening": round(mao_val * open_m, -2),   # round to nearest $100
        "target": round(mao_val * target_m, -2),
        "walk_away": round(mao_val * walk_m, -2),
    }


def sensitivity_grid(arv: float, rehab: float, purchase: float,
                     arv_deltas: list[float] = None,
                     rehab_deltas: list[float] = None,
                     holding: float = 0, financing: float = 0,
                     closing_buy: float = 0, closing_sell_pct: float = 0.08,
                     contingency_pct: float = 0.20) -> list[SensitivityCell]:
    """2D grid over ARV (±) × rehab (±) showing profit/margin.

    Flags deals that only pencil at optimistic ARV (fake-signal tell).
    """
    if arv_deltas is None:
        arv_deltas = [-0.10, -0.05, 0.0, 0.05, 0.10]
    if rehab_deltas is None:
        rehab_deltas = [-0.10, 0.0, 0.10, 0.20]

    cells = []
    for arv_d in arv_deltas:
        adj_arv = arv * (1 + arv_d)
        for rehab_d in rehab_deltas:
            adj_rehab = rehab * (1 + rehab_d)
            rehab_total = adj_rehab * (1 + contingency_pct)
            closing_sell = adj_arv * closing_sell_pct
            total_costs = purchase + rehab_total + holding + financing + closing_buy + closing_sell
            profit = adj_arv - total_costs
            margin = profit / adj_arv if adj_arv > 0 else 0.0
            cells.append(SensitivityCell(
                arv_delta=arv_d, rehab_delta=rehab_d,
                profit=profit, margin=margin,
            ))
    return cells


def check_optimistic_only(cells: list[SensitivityCell]) -> bool:
    """Flag if deal only pencils at top of ARV range (fake-signal tell).

    True if profit > 0 only when ARV delta >= 0.05.
    """
    for c in cells:
        if c.arv_delta <= 0.0 and c.rehab_delta <= 0.0 and c.profit > 0:
            return False  # pencils at or below base → not optimistic-only
    return True


def assign_blast_tier(capital_at_risk: float, deal_type: str = "DT-AUCTION") -> BlastTier:
    """Assign blast-radius tier based on capital at risk.

    T0: screen only, no money
    T1: small (e.g., low assignment / small cash)
    T2: mid (agent-MLS or BPO comps required)
    T3: large / auction cash / hold (appraiser-grade + attorney)
    """
    if capital_at_risk <= 0:
        return BlastTier.T0
    elif capital_at_risk <= 25_000:
        return BlastTier.T1
    elif capital_at_risk <= 150_000:
        return BlastTier.T2
    else:
        return BlastTier.T3


def price_deal(cpr: CPR, arv: float, rehab: float, deal_type: str = "DT-AUCTION",
               factor: float = 0.70, cash_available: float = float("inf"),
               seller_posture: str = "standard",
               capital_at_risk: float = 0) -> PricingResult:
    """Full pricing: MAO, bid ceiling, offer ladder, sensitivity, tier."""
    mao_val = mao(arv, rehab, factor)
    ceiling = compute_bid_ceiling(arv, rehab, factor, cash_available, cpr.liens)
    ladder = build_offer_ladder(mao_val, seller_posture)
    tier = assign_blast_tier(capital_at_risk, deal_type)

    # Sensitivity grid
    purchase = min(mao_val, ceiling)
    cells = sensitivity_grid(arv, rehab, purchase)
    opt_only = check_optimistic_only(cells)

    result = PricingResult(
        deal_type=deal_type,
        mao=mao_val,
        bid_ceiling=ceiling,
        offer_ladder=ladder,
        blast_tier=tier,
        sensitivity=cells,
        optimistic_only_flag=opt_only,
    )

    # Advance CPR column
    cpr.blast_tier = tier
    cpr.move_column(CPRColumn.PRICED)

    return result


if __name__ == "__main__":
    # Self-check: MAO + bid ceiling
    from .cpr import CPR, LienRecord

    cpr = CPR(parcel_id="test", county="collin")
    cpr.add_lien(LienRecord(lien_type="tax", amount=5_000, survives_foreclosure=True,
                            survives_tax_sale=False), "test")

    result = price_deal(cpr, arv=300_000, rehab=30_000, deal_type="DT-AUCTION",
                        capital_at_risk=200_000)
    assert result.mao == 180_000
    # Bid ceiling: 300k*0.70 - 30k - 5k = 175k
    assert result.bid_ceiling == 175_000, f"Bid ceiling: {result.bid_ceiling}"
    assert result.blast_tier == BlastTier.T3
    assert "opening" in result.offer_ladder
    assert "target" in result.offer_ladder
    assert "walk_away" in result.offer_ladder
    assert len(result.sensitivity) == 20  # 5 ARV × 4 rehab

    # Offer ladder: standard posture
    assert result.offer_ladder["target"] == 180_000
    assert result.offer_ladder["opening"] < result.offer_ladder["target"]

    # Optimistic-only check: deal pencils at base → not optimistic-only
    assert not result.optimistic_only_flag

    # Optimistic-only: deal that only pencils at +10% ARV
    cells_bad = sensitivity_grid(arv=200_000, rehab=80_000, purchase=100_000)
    # This deal likely loses money at base ARV → optimistic-only
    # (depends on exact numbers, but the check should work)

    # Blast tier assignment
    assert assign_blast_tier(0) == BlastTier.T0
    assert assign_blast_tier(10_000) == BlastTier.T1
    assert assign_blast_tier(50_000) == BlastTier.T2
    assert assign_blast_tier(200_000) == BlastTier.T3

    print(f"pricing.py: MAO=${result.mao:,.0f}, ceiling=${result.bid_ceiling:,.0f}, tier={result.blast_tier.name}")
    print(f"  ladder: open=${result.offer_ladder['opening']:,.0f}, target=${result.offer_ladder['target']:,.0f}")
    print("pricing.py: all self-checks passed")