# src/pricing.py
"""AGENT-PRICE: MAO, bid ceiling, offer ladder, sensitivity, blast tiering.

Bid ceiling nets out surviving liens AND is capped by MAO and cash on hand,
clamped at $0 (AUDIT C2: a non-positive ceiling marks the deal dead instead
of producing a $0-or-negative "offer"). The sensitivity grid classifies each
deal as base / optimistic_only / never_pencils / unknown (fake-signal tells).
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional
from .cpr import CPR, CPRColumn, BlastTier, LienRecord
from .underwriting import mao, wholesale_mao
from . import policy


# Pencil-status states for PricingResult.pencil_status (AUDIT F12: the legacy
# bool could not distinguish "only pencils up-range" from "never pencils").
PENCIL_BASE = "base"
PENCIL_OPTIMISTIC_ONLY = "optimistic_only"
PENCIL_NEVER = "never_pencils"
PENCIL_UNKNOWN = "unknown"

# Dead-reason code stamped by price_deal when there is nothing left to bid.
DEAD_BID_CEILING_NONPOSITIVE = "bid_ceiling_nonpositive"


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
    offer: Optional[float] = None  # recommended offer; None => dead deal, no offer
    offer_ladder: dict = field(default_factory=dict)  # opening, target, walk_away
    blast_tier: BlastTier = BlastTier.T0
    sensitivity: list[SensitivityCell] = field(default_factory=list)
    pencil_status: str = PENCIL_UNKNOWN  # base | optimistic_only | never_pencils | unknown
    dead_reason: Optional[str] = None  # e.g. "bid_ceiling_nonpositive"; None = alive
    # LEGACY bool (AUDIT F12): derived from pencil_status when not passed
    # explicitly; kept so existing readers (reviewer, pipeline) keep working.
    optimistic_only_flag: Optional[bool] = None

    def __post_init__(self):
        if self.optimistic_only_flag is None:
            self.optimistic_only_flag = self.pencil_status == PENCIL_OPTIMISTIC_ONLY


def compute_bid_ceiling(arv: float, rehab: float, factor: float, mao_val: float,
                        cash_available: float = float("inf"),
                        surviving_liens: list[LienRecord] = None) -> float:
    """Bid ceiling = min(MAO, cash_available, ARV×factor − rehab − surviving_liens), clamped at 0.

    AUDIT C2: the ceiling must never exceed MAO or the cash actually
    available, and surviving liens net out of the raw equity number. A
    non-positive result means there is no bid worth making — callers
    (price_deal) treat that as a dead deal rather than offering ≤ $0.
    """
    surviving_total = sum(l.amount or 0 for l in (surviving_liens or [])
                          if l.survives_foreclosure or l.survives_tax_sale)
    raw_ceiling = (arv * factor) - rehab - surviving_total
    return max(0.0, min(raw_ceiling, mao_val, cash_available))


def build_offer_ladder(mao_val: float, seller_posture: str = "standard",
                       bid_ceiling_val: float = float("inf")) -> dict:
    """Build opening / target / walk-away offer ladder.

    Posture affects how aggressive the opening is:
    - standard: open at 85% of MAO, target at MAO, walk at 100% MAO
    - pre-foreclosure: open at 80% (time pressure on seller)
    - tax-delinquent: open at 75% (low-pressure, problem-solving)

    AUDIT H3: the walk-away multiplier is 1.00 everywhere (was 1.05 — we were
    coaching ourselves to walk ABOVE our own maximum). The walk-away is also
    clamped to min(walk, MAO, bid_ceiling) when the ceiling is finite.
    NOTE: src/negotiation.py POSTURES still carries the old 1.05 walk
    multipliers — that module is DEPRECATED; do NOT copy its numbers here and
    do NOT "fix" it as part of this change.
    """
    multipliers = {
        "standard": (0.85, 1.0, 1.00),
        "pre-foreclosure": (0.80, 0.95, 1.00),
        "tax-delinquent": (0.75, 0.90, 1.00),
    }
    open_m, target_m, walk_m = multipliers.get(seller_posture, multipliers["standard"])
    walk_cap = mao_val
    if math.isfinite(bid_ceiling_val):
        walk_cap = min(walk_cap, bid_ceiling_val)
    walk_raw = min(mao_val * walk_m, walk_cap)  # H3 clamp: never above MAO/ceiling
    walk_away = round(walk_raw, -2)             # round to nearest $100
    if walk_away > walk_raw:
        walk_away -= 100  # rounding must never push the walk ABOVE its cap
    return {
        "opening": round(mao_val * open_m, -2),   # round to nearest $100
        "target": round(mao_val * target_m, -2),
        "walk_away": walk_away,
    }


def sensitivity_grid(arv: float, rehab: float, purchase: float,
                     arv_deltas: list[float] = None,
                     rehab_deltas: list[float] = None,
                     holding: float = 0, financing: float = 0,
                     closing_buy: float = 0,
                     closing_sell_pct: float = policy.CLOSING_SELL_PCT,
                     contingency_pct: float = policy.REHAB_CONTINGENCY_PCT) -> list[SensitivityCell]:
    """2D grid over ARV (±) × rehab (±) showing profit/margin.

    classify_pencil() flags deals that only pencil at optimistic ARV or
    nowhere at all (fake-signal tell).
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


def classify_pencil(cells: list[SensitivityCell]) -> str:
    """Three-state pencil classification over the sensitivity grid (AUDIT F12).

    - PENCIL_BASE: profitable at (or below) base ARV/rehab assumptions.
    - PENCIL_OPTIMISTIC_ONLY: unprofitable at base but positive somewhere
      up-grid — the classic fake-signal tell.
    - PENCIL_NEVER: unprofitable at every grid point.
    - PENCIL_UNKNOWN: empty grid (nothing computed).
    """
    if not cells:
        return PENCIL_UNKNOWN
    pencils_at_base = any(
        c.arv_delta <= 0.0 and c.rehab_delta <= 0.0 and c.profit > 0
        for c in cells
    )
    if pencils_at_base:
        return PENCIL_BASE
    pencils_anywhere = any(c.profit > 0 for c in cells)
    return PENCIL_OPTIMISTIC_ONLY if pencils_anywhere else PENCIL_NEVER


def check_optimistic_only(cells: list[SensitivityCell]) -> bool:
    """LEGACY bool view of classify_pencil(): True iff status is optimistic_only.

    Note this is now strictly narrower than before: a grid that NEVER pencils
    reports False here (it is worse than optimistic-only — see classify_pencil).
    """
    return classify_pencil(cells) == PENCIL_OPTIMISTIC_ONLY


def assign_blast_tier(capital_at_risk: float, deal_type: str = "DT-AUCTION") -> BlastTier:
    """Assign blast-radius tier based on capital at risk.

    T0: screen only, no money
    T1: small (e.g., low assignment / small cash)
    T2: mid (agent-MLS or BPO comps required)
    T3: large / auction cash / hold (appraiser-grade + attorney)
    """
    if capital_at_risk <= 0:
        return BlastTier.T0
    elif capital_at_risk <= policy.BLAST_TIER_T2_MAX:
        return BlastTier.T1
    elif capital_at_risk <= policy.BLAST_TIER_T3_MAX:
        return BlastTier.T2
    else:
        return BlastTier.T3


def price_deal(cpr: CPR, arv: float, rehab: float, deal_type: str = "DT-AUCTION",
               factor: float = policy.MAO_FACTOR_DEFAULT, cash_available: float = float("inf"),
               seller_posture: str = "standard",
               capital_at_risk: float = 0) -> PricingResult:
    """Full pricing: MAO, bid ceiling, offer ladder, sensitivity, tier.

    AUDIT C2: when the bid ceiling clamps to ≤ 0 (surviving liens / cash /
    MAO leave nothing to bid) the result is marked dead with
    dead_reason="bid_ceiling_nonpositive" and offer=None — no ladder, no
    fabricated offer.
    """
    mao_val = mao(arv, rehab, factor)
    ceiling = compute_bid_ceiling(arv, rehab, factor, mao_val,
                                  cash_available, cpr.liens)
    tier = assign_blast_tier(capital_at_risk, deal_type)

    # Grid purchase basis: the most we would actually pay. When the ceiling is
    # dead (≤ 0) fall back to MAO so the grid answers "does this ever pencil
    # at our max?" instead of manufacturing phantom profits off a ~$0 basis.
    grid_purchase = min(mao_val, ceiling) if ceiling > 0 else max(mao_val, 0.0)
    cells = sensitivity_grid(arv, rehab, grid_purchase)
    pencil_status = classify_pencil(cells)

    if ceiling <= 0:
        result = PricingResult(
            deal_type=deal_type,
            mao=mao_val,
            bid_ceiling=ceiling,
            offer=None,
            offer_ladder={},  # dead deals get no ladder — do not fake one
            blast_tier=tier,
            sensitivity=cells,
            pencil_status=pencil_status,
            dead_reason=DEAD_BID_CEILING_NONPOSITIVE,
        )
    else:
        ladder = build_offer_ladder(mao_val, seller_posture, bid_ceiling_val=ceiling)
        result = PricingResult(
            deal_type=deal_type,
            mao=mao_val,
            bid_ceiling=ceiling,
            offer=ladder.get("target"),
            offer_ladder=ladder,
            blast_tier=tier,
            sensitivity=cells,
            pencil_status=pencil_status,
            dead_reason=None,
        )

    # Advance CPR column
    cpr.blast_tier = tier
    cpr.move_column(CPRColumn.PRICED)

    return result


if __name__ == "__main__":
    # Self-check: MAO + bid ceiling + pencil classification + dead-deal handling
    from .cpr import CPR, LienRecord

    _results = {"pass": 0, "fail": 0}

    def _check(name: str, cond: bool) -> None:
        _results["pass" if cond else "fail"] += 1
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        assert cond, name

    # --- Healthy deal ---
    cpr = CPR(parcel_id="test", county="collin")
    cpr.add_lien(LienRecord(lien_type="tax", amount=5_000, survives_foreclosure=True,
                            survives_tax_sale=False), "test")

    result = price_deal(cpr, arv=300_000, rehab=30_000, deal_type="DT-AUCTION",
                        capital_at_risk=200_000)
    _check("MAO = $180,000 (300k*0.70 - 30k)", result.mao == 180_000)
    # Bid ceiling: min(180k MAO, inf cash, 300k*0.70 - 30k - 5k liens) = 175k
    _check("bid ceiling = $175,000", result.bid_ceiling == 175_000)
    _check("bid ceiling <= MAO (C2)", result.bid_ceiling <= result.mao)
    _check("blast tier T3", result.blast_tier == BlastTier.T3)
    _check("ladder has opening/target/walk_away",
           all(k in result.offer_ladder for k in ("opening", "target", "walk_away")))
    _check("sensitivity grid = 20 cells (5 ARV x 4 rehab)", len(result.sensitivity) == 20)

    # Offer ladder: standard posture, H3 semantics
    _check("target = $180,000 (100% MAO)", result.offer_ladder["target"] == 180_000)
    _check("opening < target", result.offer_ladder["opening"] < result.offer_ladder["target"])
    _check("walk-away never exceeds MAO (H3)",
           result.offer_ladder["walk_away"] <= result.mao + 1e-9)
    _check("walk-away clamped to bid ceiling (H3)",
           result.offer_ladder["walk_away"] <= result.bid_ceiling + 1e-9)
    _check("walk-away = $175,000 (= ceiling, was 189k under 1.05 rule)",
           result.offer_ladder["walk_away"] == 175_000)
    _check("recommended offer set (target)", result.offer == result.offer_ladder["target"])
    _check("alive: dead_reason is None", result.dead_reason is None)

    # Optimistic-only check: deal pencils at base -> status 'base'
    _check("pencil_status = 'base'", result.pencil_status == PENCIL_BASE)
    _check("legacy optimistic_only_flag False", result.optimistic_only_flag is False)

    # --- compute_bid_ceiling unit behavior (C2) ---
    _check("ceiling capped by required mao param",
           compute_bid_ceiling(300_000, 30_000, 0.70, mao_val=100_000) == 100_000)
    _check("ceiling clamped at 0 when liens swamp equity",
           compute_bid_ceiling(300_000, 30_000, 0.70, mao_val=400_000,
                               surviving_liens=[LienRecord(lien_type="irs_federal",
                                                           amount=500_000,
                                                           survives_foreclosure=True)]) == 0.0)
    _check("ceiling respects cash_available cap",
           compute_bid_ceiling(300_000, 30_000, 0.70, mao_val=400_000,
                               cash_available=50_000) == 50_000)

    # --- AUDIT C2: lien overload — ceiling clamps to 0, deal goes dead ---
    cpr_dead = CPR(parcel_id="dead-test", county="collin")
    cpr_dead.add_lien(LienRecord(lien_type="irs_federal", amount=500_000,
                                 priority=1, survives_foreclosure=True,
                                 survives_tax_sale=False), "test")
    dead = price_deal(cpr_dead, arv=300_000, rehab=30_000, deal_type="DT-AUCTION",
                      capital_at_risk=200_000)
    _check("overload: bid ceiling returns 0", dead.bid_ceiling == 0)
    _check("overload: dead_reason == 'bid_ceiling_nonpositive'",
           dead.dead_reason == DEAD_BID_CEILING_NONPOSITIVE)
    _check("overload: offer is None", dead.offer is None)
    _check("overload: no offer ladder fabricated", dead.offer_ladder == {})

    # --- Pencil classification (F12) ---
    _check("empty grid -> 'unknown'", classify_pencil([]) == PENCIL_UNKNOWN)
    never_cells = sensitivity_grid(arv=200_000, rehab=80_000, purchase=150_000)
    _check("grid that loses everywhere -> 'never_pencils'",
           classify_pencil(never_cells) == PENCIL_NEVER)
    opt_cells = sensitivity_grid(arv=200_000, rehab=80_000, purchase=100_000)
    _check("grid that only pencils up-range -> 'optimistic_only'",
           classify_pencil(opt_cells) == PENCIL_OPTIMISTIC_ONLY)
    _check("legacy check_optimistic_only False for never_pencils grids",
           check_optimistic_only(never_cells) is False)
    _check("legacy check_optimistic_only True for optimistic-only grids",
           check_optimistic_only(opt_cells) is True)
    never_result = PricingResult(deal_type="DT-AUCTION", pencil_status=PENCIL_NEVER,
                                 sensitivity=never_cells)
    _check("never_pencils does NOT raise legacy optimistic_only_flag",
           never_result.optimistic_only_flag is False)

    # --- Blast tier assignment ---
    _check("tier T0 at $0 risk", assign_blast_tier(0) == BlastTier.T0)
    _check("tier T1 at $10k risk", assign_blast_tier(10_000) == BlastTier.T1)
    _check("tier T2 at $50k risk", assign_blast_tier(50_000) == BlastTier.T2)
    _check("tier T3 at $200k risk", assign_blast_tier(200_000) == BlastTier.T3)

    print(f"\npricing.py: MAO=${result.mao:,.0f}, ceiling=${result.bid_ceiling:,.0f}, "
          f"tier={result.blast_tier.name}, pencil={result.pencil_status}")
    print(f"  ladder: open=${result.offer_ladder['opening']:,.0f}, "
          f"target=${result.offer_ladder['target']:,.0f}, "
          f"walk=${result.offer_ladder['walk_away']:,.0f}")
    print(f"  checks: {_results['pass']} passed, {_results['fail']} failed")
    print("pricing.py: all self-checks passed")
