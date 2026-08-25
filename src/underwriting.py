# src/underwriting.py
"""AGENT-UW: deal-type router + financial models.

All formulas from spec §7.1 as pure, testable functions. Benchmarks are
inputs, not constants.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from .legal_rules import DealType, RedemptionClass, compute_redemption_yield


@dataclass
class UnderwriteResult:
    deal_type: str
    model: str  # "flip", "brrrr", "rental", "redemption_yield", "wholesale"
    # Inputs
    arv: float = 0.0
    purchase_price: float = 0.0
    rehab: float = 0.0
    rehab_contingency_pct: float = 0.20  # 20% contingency
    holding_costs: float = 0.0
    financing_costs: float = 0.0
    closing_costs_buy: float = 0.0
    closing_costs_sell: float = 0.0
    # Results
    net_profit: float = 0.0
    roi: float = 0.0
    cash_invested: float = 0.0
    # Model-specific
    cap_rate: Optional[float] = None
    cash_on_cash: Optional[float] = None
    dscr: Optional[float] = None
    grm: Optional[float] = None
    redemption_yield: Optional[float] = None
    refi_recycle_pct: Optional[float] = None
    # Details
    line_items: dict = field(default_factory=dict)


def mao(arv: float, rehab: float, factor: float = 0.70) -> float:
    """70% rule: MAO = (ARV × factor) − rehab."""
    return (arv * factor) - rehab


def wholesale_mao(arv: float, rehab: float, assignment_fee: float,
                   factor: float = 0.65) -> float:
    """Wholesale MAO: (ARV × factor) − rehab − assignment_fee.

    Use 0.65 or subtract $10-15k so the end buyer still clears 70%.
    """
    return (arv * factor) - rehab - assignment_fee


def flip_pnl(arv: float, purchase: float, rehab: float,
             holding: float = 0.0, financing: float = 0.0,
             closing_buy: float = 0.0, closing_sell: float = 0.0,
             contingency_pct: float = 0.20) -> UnderwriteResult:
    """Fix-and-flip P&L.

    Net profit = ARV − (purchase + rehab×(1+contingency) + holding + financing + closing)
    ROI = net profit / cash invested.
    """
    rehab_with_contingency = rehab * (1 + contingency_pct)
    total_costs = (purchase + rehab_with_contingency + holding + financing
                   + closing_buy + closing_sell)
    net_profit = arv - total_costs
    cash_invested = purchase + rehab_with_contingency + holding + financing + closing_buy
    roi = net_profit / cash_invested if cash_invested > 0 else 0.0

    return UnderwriteResult(
        deal_type="DT-AUCTION", model="flip",
        arv=arv, purchase_price=purchase, rehab=rehab,
        rehab_contingency_pct=contingency_pct,
        holding_costs=holding, financing_costs=financing,
        closing_costs_buy=closing_buy, closing_costs_sell=closing_sell,
        net_profit=net_profit, roi=roi, cash_invested=cash_invested,
        line_items={
            "rehab_with_contingency": rehab_with_contingency,
            "total_costs": total_costs,
        },
    )


def brrrr_recycle(arv: float, purchase: float, rehab: float,
                  refi_ltv: float = 0.75, refi_appraisal: float = 0.0,
                  holding: float = 0.0, financing: float = 0.0,
                  closing_buy: float = 0.0, closing_sell: float = 0.0,
                  contingency_pct: float = 0.20) -> UnderwriteResult:
    """BRRRR: buy like a flip, hold like a rental, refi to recycle capital.

    Target: all-in ≤ refi appraisal × LTV so capital recycles.
    """
    appraisal = refi_appraisal or arv
    rehab_with_contingency = rehab * (1 + contingency_pct)
    all_in = purchase + rehab_with_contingency + holding + financing + closing_buy
    refi_proceeds = appraisal * refi_ltv
    cash_recycled = refi_proceeds - all_in
    recycle_pct = cash_recycled / all_in if all_in > 0 else 0.0

    return UnderwriteResult(
        deal_type="DT-AUCTION", model="brrrr",
        arv=arv, purchase_price=purchase, rehab=rehab,
        rehab_contingency_pct=contingency_pct,
        holding_costs=holding, financing_costs=financing,
        closing_costs_buy=closing_buy, closing_costs_sell=closing_sell,
        net_profit=cash_recycled, roi=recycle_pct, cash_invested=all_in,
        refi_recycle_pct=recycle_pct,
        line_items={
            "rehab_with_contingency": rehab_with_contingency,
            "all_in": all_in,
            "refi_proceeds": refi_proceeds,
            "cash_recycled": cash_recycled,
        },
    )


def rental_metrics(price: float, rent_monthly: float,
                   opex_annual: float = 0.0,
                   taxes_annual: float = 0.0, insurance_annual: float = 0.0,
                   mgmt_pct: float = 0.08, vacancy_pct: float = 0.05,
                   maintenance_pct: float = 0.05,
                   debt_service_annual: float = 0.0) -> UnderwriteResult:
    """Rental / hold: cap rate, cash-on-cash, DSCR, GRM, 1% rule.

    NOI = rent − opex (taxes, insurance, mgmt, maintenance, vacancy)
    cap rate = NOI / price
    cash-on-cash = pre-tax cash flow / cash invested
    DSCR = NOI / debt service
    GRM = price / gross annual rent
    """
    gross_annual = rent_monthly * 12
    if opex_annual == 0.0:
        # Compute from components if not provided
        mgmt = gross_annual * mgmt_pct
        vacancy_loss = gross_annual * vacancy_pct
        maintenance = gross_annual * maintenance_pct
        opex_annual = taxes_annual + insurance_annual + mgmt + vacancy_loss + maintenance

    noi = gross_annual - opex_annual
    cap_rate = noi / price if price > 0 else 0.0
    cash_flow = noi - debt_service_annual
    # ponytail: cash_on_cash assumes cash = price (no loan); adjust if down payment provided
    cash_on_cash = cash_flow / price if price > 0 else 0.0
    dscr = noi / debt_service_annual if debt_service_annual > 0 else None
    grm = price / gross_annual if gross_annual > 0 else 0.0
    one_pct = (rent_monthly / price) if price > 0 else 0.0

    return UnderwriteResult(
        deal_type="DT-AUCTION", model="rental",
        purchase_price=price,
        cap_rate=cap_rate, cash_on_cash=cash_on_cash,
        dscr=dscr, grm=grm,
        line_items={
            "gross_annual": gross_annual,
            "opex_annual": opex_annual,
            "noi": noi,
            "cash_flow": cash_flow,
            "one_pct_rule": one_pct,
        },
    )


def tax_deed_yield(bid: float, recording_fee: float = 0.0,
                   post_sale_costs: float = 0.0,
                   redemption_class: RedemptionClass = RedemptionClass.OTHER,
                   year: int = 1) -> UnderwriteResult:
    """Tax-deed redemption yield: guaranteed statutory premium.

    premium = aggregate × rate
    aggregate = bid + recording_fee + post_sale_costs
    """
    aggregate = bid + recording_fee + post_sale_costs
    rate = compute_redemption_yield(bid, recording_fee, post_sale_costs,
                                     redemption_class, year) / aggregate if aggregate > 0 else 0.0
    premium = compute_redemption_yield(bid, recording_fee, post_sale_costs,
                                        redemption_class, year)

    return UnderwriteResult(
        deal_type="DT-TAXDEED", model="redemption_yield",
        purchase_price=bid,
        redemption_yield=premium,
        roi=rate,  # premium / aggregate
        line_items={
            "aggregate": aggregate,
            "premium_rate": rate,
            "premium_amount": premium,
            "redemption_class": redemption_class.value,
            "year": year,
        },
    )


def route_underwrite(cpr, arv: float, rehab: float, deal_type: str = "DT-AUCTION",
                     **kwargs) -> UnderwriteResult:
    """Route to the correct underwriting model based on deal type."""
    if deal_type == "DT-AUCTION" or deal_type == "DT-PREFORE":
        return flip_pnl(arv, kwargs.get("purchase", mao(arv, rehab)), rehab,
                         holding=kwargs.get("holding", 0),
                         financing=kwargs.get("financing", 0),
                         closing_buy=kwargs.get("closing_buy", 0),
                         closing_sell=kwargs.get("closing_sell", arv * 0.08))
    elif deal_type == "DT-WHOLESALE":
        return flip_pnl(arv, kwargs.get("purchase",
                      wholesale_mao(arv, rehab, kwargs.get("assignment_fee", 10_000))),
                      rehab, holding=kwargs.get("holding", 0))
    elif deal_type == "DT-TAXDEED":
        return tax_deed_yield(kwargs.get("bid", 0),
                              recording_fee=kwargs.get("recording_fee", 0),
                              redemption_class=kwargs.get("redemption_class", RedemptionClass.OTHER),
                              year=kwargs.get("year", 1))
    else:
        # Default to flip
        return flip_pnl(arv, kwargs.get("purchase", mao(arv, rehab)), rehab)


if __name__ == "__main__":
    # Self-check: 70% rule
    assert mao(300_000, 30_000) == 180_000, f"MAO: {mao(300_000, 30_000)}"
    assert mao(300_000, 30_000, factor=0.65) == 165_000

    # Wholesale MAO
    wm = wholesale_mao(300_000, 30_000, 10_000)
    assert wm == 155_000, f"Wholesale MAO: {wm}"

    # Flip P&L
    r = flip_pnl(arv=300_000, purchase=180_000, rehab=30_000,
                 holding=3_000, financing=5_000, closing_buy=2_000, closing_sell=24_000)
    assert r.net_profit > 0, f"Flip should be profitable: {r.net_profit}"
    assert r.roi > 0
    print(f"underwriting.py: flip — profit=${r.net_profit:,.0f}, ROI={r.roi:.1%}")

    # BRRRR
    b = brrrr_recycle(arv=300_000, purchase=180_000, rehab=30_000, refi_ltv=0.75)
    assert b.refi_recycle_pct is not None
    print(f"underwriting.py: brrrr — recycle={b.refi_recycle_pct:.1%}")

    # Rental
    rt = rental_metrics(price=200_000, rent_monthly=2_000, taxes_annual=4_000, insurance_annual=1_500)
    assert rt.cap_rate > 0
    assert rt.dscr is None  # no debt service
    print(f"underwriting.py: rental — cap={rt.cap_rate:.2%}, GRM={rt.grm:.1f}")

    # Tax deed yield
    td = tax_deed_yield(bid=100_000, redemption_class=RedemptionClass.OTHER, year=1)
    assert td.redemption_yield == 25_000, f"Tax deed yield: {td.redemption_yield}"
    print(f"underwriting.py: tax-deed — yield=${td.redemption_yield:,.0f}")

    print("underwriting.py: all self-checks passed")