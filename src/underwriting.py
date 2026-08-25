# src/underwriting.py
"""AGENT-UW: deal-type router + financial models.

All formulas from spec §7.1 as pure, testable functions. Benchmarks are
inputs, not constants.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
from .cpr import CPR
from .legal_rules import DealType, RedemptionClass, compute_redemption_yield
from . import policy


@dataclass
class UnderwriteResult:
    deal_type: str
    model: str  # "flip", "brrrr", "rental", "redemption_yield", "wholesale"
    # Inputs
    arv: float = 0.0
    purchase_price: float = 0.0
    rehab: float = 0.0
    rehab_contingency_pct: float = policy.REHAB_CONTINGENCY_PCT  # 20% contingency
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
    line_items: dict[str, Any] = field(default_factory=dict)


def mao(arv: float, rehab: float, factor: float = policy.MAO_FACTOR_DEFAULT) -> float:
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
             contingency_pct: float = policy.REHAB_CONTINGENCY_PCT,
             deal_type: Optional[str] = None) -> UnderwriteResult:
    """Fix-and-flip P&L.

    Net profit = ARV − (purchase + rehab×(1+contingency) + holding + financing + closing)
    ROI = net profit / cash invested.

    deal_type: optional override; defaults to the model's stamp "DT-AUCTION".
    """
    rehab_with_contingency = rehab * (1 + contingency_pct)
    total_costs = (purchase + rehab_with_contingency + holding + financing
                   + closing_buy + closing_sell)
    net_profit = arv - total_costs
    cash_invested = purchase + rehab_with_contingency + holding + financing + closing_buy
    roi = net_profit / cash_invested if cash_invested > 0 else 0.0

    return UnderwriteResult(
        deal_type=deal_type if deal_type is not None else DealType.AUCTION.value,
        model="flip",
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
                  contingency_pct: float = policy.REHAB_CONTINGENCY_PCT,
                  deal_type: Optional[str] = None) -> UnderwriteResult:
    """BRRRR: buy like a flip, hold like a rental, refi to recycle capital.

    Target: all-in ≤ refi appraisal × LTV so capital recycles.

    deal_type: optional override; defaults to the model's stamp "DT-AUCTION".
    """
    appraisal = refi_appraisal or arv
    rehab_with_contingency = rehab * (1 + contingency_pct)
    all_in = purchase + rehab_with_contingency + holding + financing + closing_buy
    refi_proceeds = appraisal * refi_ltv
    cash_recycled = refi_proceeds - all_in
    recycle_pct = cash_recycled / all_in if all_in > 0 else 0.0

    return UnderwriteResult(
        deal_type=deal_type if deal_type is not None else DealType.AUCTION.value,
        model="brrrr",
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
                   debt_service_annual: float = 0.0,
                   equity_invested: Optional[float] = None,
                   deal_type: Optional[str] = None) -> UnderwriteResult:
    """Rental / hold: cap rate, cash-on-cash, DSCR, GRM, 1% rule.

    NOI = rent − opex (taxes, insurance, mgmt, maintenance, vacancy)
    cap rate = NOI / price
    cash-on-cash = pre-tax cash flow / equity actually invested
    DSCR = NOI / debt service
    GRM = price / gross annual rent

    AUDIT C3 (cash-on-cash basis): when the deal is financed
    (debt_service_annual > 0) and no equity_invested is supplied, the true
    cash-in basis is unknown — cash_on_cash is None (rendered as a dash)
    rather than the old wrong ratio that divided by full price.
    Unfinanced deals keep the historical all-cash basis (equity ≈ price).
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
    financed = debt_service_annual > 0
    if financed:
        if equity_invested is not None and equity_invested > 0:
            cash_on_cash = cash_flow / equity_invested
        else:
            # AUDIT C3: equity basis unknown on a financed deal — report None
            # (rendered as a dash) instead of a ratio against the full price.
            cash_on_cash = None
    else:
        # All-cash purchase: invested equity ≈ price.
        cash_on_cash = cash_flow / price if price > 0 else 0.0
    dscr = noi / debt_service_annual if debt_service_annual > 0 else None
    grm = price / gross_annual if gross_annual > 0 else 0.0
    one_pct = (rent_monthly / price) if price > 0 else 0.0

    return UnderwriteResult(
        deal_type=deal_type if deal_type is not None else DealType.AUCTION.value,
        model="rental",
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
                   year: int = 1,
                   deal_type: Optional[str] = None) -> UnderwriteResult:
    """Tax-deed redemption yield: guaranteed statutory premium.

    premium = aggregate × rate
    aggregate = bid + recording_fee + post_sale_costs

    deal_type: optional override; defaults to the model's stamp "DT-TAXDEED".
    """
    aggregate = bid + recording_fee + post_sale_costs
    rate = compute_redemption_yield(bid, recording_fee, post_sale_costs,
                                     redemption_class, year) / aggregate if aggregate > 0 else 0.0
    premium = compute_redemption_yield(bid, recording_fee, post_sale_costs,
                                        redemption_class, year)

    return UnderwriteResult(
        deal_type=deal_type if deal_type is not None else DealType.TAXDEED.value,
        model="redemption_yield",
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


def _require_purchase(kwargs: dict[str, Any]) -> float:
    """AUDIT H1: the purchase price must be supplied explicitly.

    The old default-to-MAO silently underwrote unpriced deals at their own
    MAO — manufacturing a profitable-looking P&L for a deal nobody priced.
    This repo prices real deals: fail loud instead.
    """
    if "purchase" not in kwargs or kwargs["purchase"] is None:
        raise ValueError(
            "route_underwrite: 'purchase' is required — refusing to default "
            "to MAO (an unpriced deal must be priced, not assumed)"
        )
    return kwargs["purchase"]


def route_underwrite(cpr: Optional[CPR], arv: float, rehab: float,
                     deal_type: str = "DT-AUCTION",
                     **kwargs: Any) -> UnderwriteResult:
    """Route to the correct underwriting model based on deal type.

    AUDIT H1: `purchase` is REQUIRED for every purchase-price model
    (flip / wholesale / default) — a missing purchase raises ValueError
    instead of defaulting to MAO. The routed `deal_type` is stamped onto
    the result (the models' own defaults only apply to direct calls).
    ``cpr`` is accepted for interface symmetry with the other AGENT-*
    entrypoints; the pure models here do not read it.
    """
    dt = getattr(deal_type, "value", deal_type)
    if dt == "DT-AUCTION" or dt == "DT-PREFORE":
        return flip_pnl(arv, _require_purchase(kwargs), rehab,
                        holding=kwargs.get("holding", 0),
                        financing=kwargs.get("financing", 0),
                        closing_buy=kwargs.get("closing_buy", 0),
                        closing_sell=kwargs.get("closing_sell", arv * policy.CLOSING_SELL_PCT),
                        deal_type=dt)
    elif dt == "DT-WHOLESALE":
        return flip_pnl(arv, _require_purchase(kwargs), rehab,
                        holding=kwargs.get("holding", 0), deal_type=dt)
    elif dt == "DT-TAXDEED":
        return tax_deed_yield(kwargs.get("bid", 0),
                              recording_fee=kwargs.get("recording_fee", 0),
                              redemption_class=kwargs.get("redemption_class", RedemptionClass.OTHER),
                              year=kwargs.get("year", 1),
                              deal_type=dt)
    else:
        # Default to flip
        return flip_pnl(arv, _require_purchase(kwargs), rehab, deal_type=dt)


if __name__ == "__main__":
    # Self-check: 70% rule + model routing + H1 purchase requirement
    _results = {"pass": 0, "fail": 0}

    def _check(name: str, cond: bool) -> None:
        _results["pass" if cond else "fail"] += 1
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        assert cond, name

    # 70% rule
    _check("mao(300k, 30k) == 180k", mao(300_000, 30_000) == 180_000)
    _check("mao(300k, 30k, factor=0.65) == 165k", mao(300_000, 30_000, factor=0.65) == 165_000)

    # Wholesale MAO
    wm = wholesale_mao(300_000, 30_000, 10_000)
    _check("wholesale_mao(300k, 30k, 10k) == 155k", wm == 155_000)

    # Flip P&L (purchase passed explicitly)
    r = flip_pnl(arv=300_000, purchase=180_000, rehab=30_000,
                 holding=3_000, financing=5_000, closing_buy=2_000, closing_sell=24_000)
    _check("flip net_profit > 0", r.net_profit > 0)
    _check("flip roi > 0", r.roi > 0)
    _check("flip default stamp is DT-AUCTION", r.deal_type == "DT-AUCTION")

    # Flip P&L with explicit deal_type override
    r_dt = flip_pnl(arv=300_000, purchase=180_000, rehab=30_000,
                    holding=3_000, financing=5_000, closing_buy=2_000,
                    closing_sell=24_000, deal_type="DT-PREFORE")
    _check("flip honors deal_type='DT-PREFORE' override", r_dt.deal_type == "DT-PREFORE")
    print(f"underwriting.py: flip — profit=${r.net_profit:,.0f}, ROI={r.roi:.1%}")

    # BRRRR (purchase passed explicitly)
    b = brrrr_recycle(arv=300_000, purchase=180_000, rehab=30_000, refi_ltv=0.75,
                      deal_type="DT-AUCTION")
    _check("brrrr refi_recycle_pct set", b.refi_recycle_pct is not None)
    _check("brrrr stamped DT-AUCTION", b.deal_type == "DT-AUCTION")
    print(f"underwriting.py: brrrr — recycle={b.refi_recycle_pct:.1%}")

    # Rental — unfinanced (all-cash): coc keeps historical price basis
    rt = rental_metrics(price=200_000, rent_monthly=2_000,
                        taxes_annual=4_000, insurance_annual=1_500)
    _check("rental cap_rate > 0", rt.cap_rate > 0)
    _check("unfinanced rental dscr is None", rt.dscr is None)  # no debt service
    _check("unfinanced rental cash_on_cash computed (equity = price)",
           rt.cash_on_cash is not None and rt.cash_on_cash > 0)
    print(f"underwriting.py: rental — cap={rt.cap_rate:.2%}, GRM={rt.grm:.1f}")

    # Rental — AUDIT C3: financed without equity basis -> cash_on_cash None
    rt_fin = rental_metrics(price=200_000, rent_monthly=2_000,
                            taxes_annual=4_000, insurance_annual=1_500,
                            debt_service_annual=12_000)
    _check("financed rental without equity_invested -> cash_on_cash is None",
           rt_fin.cash_on_cash is None)
    # Financed WITH an explicit equity basis -> true cash-on-cash ratio
    rt_eq = rental_metrics(price=200_000, rent_monthly=2_000,
                           taxes_annual=4_000, insurance_annual=1_500,
                           debt_service_annual=12_000, equity_invested=50_000)
    noi = rt_eq.line_items["noi"]
    expected_coc = (noi - 12_000) / 50_000
    _check("financed rental uses equity_invested basis",
           rt_eq.cash_on_cash is not None
           and abs(rt_eq.cash_on_cash - expected_coc) < 1e-9)

    # Tax deed yield
    td = tax_deed_yield(bid=100_000, redemption_class=RedemptionClass.OTHER, year=1)
    _check("tax-deed yield premium == $25,000", td.redemption_yield == 25_000)
    _check("tax-deed default stamp is DT-TAXDEED", td.deal_type == "DT-TAXDEED")
    print(f"underwriting.py: tax-deed — yield=${td.redemption_yield:,.0f}")

    # --- AUDIT H1: route_underwrite requires an explicit purchase ---
    try:
        route_underwrite(None, arv=300_000, rehab=30_000, deal_type="DT-AUCTION")
        raised = False
    except ValueError:
        raised = True
    _check("route_underwrite without purchase raises ValueError (no default-to-MAO)", raised)

    try:
        route_underwrite(None, arv=300_000, rehab=30_000, deal_type="DT-WHOLESALE")
        raised_ws = False
    except ValueError:
        raised_ws = True
    _check("route_underwrite wholesale branch also requires purchase", raised_ws)

    # Explicit purchase routes through and stamps the routed deal_type
    routed = route_underwrite(None, arv=300_000, rehab=30_000, deal_type="DT-PREFORE",
                              purchase=170_000, holding=3_000, financing=5_000,
                              closing_buy=2_000, closing_sell=24_000)
    _check("routed DT-PREFORE result stamped 'DT-PREFORE'", routed.deal_type == "DT-PREFORE")
    _check("routed DT-PREFORE uses flip model", routed.model == "flip")
    _check("routed purchase honored (not MAO-defaulted)",
           routed.purchase_price == 170_000)

    routed_td = route_underwrite(None, arv=0, rehab=0, deal_type="DT-TAXDEED",
                                 bid=100_000)
    _check("routed DT-TAXDEED stamped 'DT-TAXDEED'", routed_td.deal_type == "DT-TAXDEED")
    _check("routed DT-TAXDEED uses redemption_yield model",
           routed_td.model == "redemption_yield")

    print(f"\nunderwriting.py: checks {_results['pass']} passed, {_results['fail']} failed")
    print("underwriting.py: all self-checks passed")