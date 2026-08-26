# src/underwriting.py
"""AGENT-UW: deal-type router + financial models.

All formulas from spec §7.1 as pure, testable functions. Benchmarks are
inputs, not constants.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
from .cpr import CPR
from . import legal_rules
from .legal_rules import DealType, RedemptionClass, compute_redemption_yield
from . import policy


@dataclass
class UnderwriteResult:
    deal_type: str
    model: str  # "flip", "brrrr", "rental", "redemption_yield", "wholesale",
    #             "short_sale", "subject_to"
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
    # F6: subject-to deals always carry due-on-sale exposure (the lender may
    # call the loan on transfer); subject_to_model stamps it via
    # _due_on_sale_risk_default() — legal_rules constant if present, else
    # policy, else conservative True. None for every other model — the field
    # means "not applicable", not "safe".
    due_on_sale_risk: Optional[bool] = None
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


# --- F6: SHORT / SUBTO models (mirror the Excel-tab economics) ------------

# Typical lender-response window for a short-sale approval package (months).
# The patience posture is real economics, not just negotiation framing:
# carrying costs accrue every month the lender reviews the package (the
# offer terms in src/negotiation.py give DT-SHORT the extended option period
# and the max closing timeline for the same reason).
SHORT_SALE_APPROVAL_MONTHS_DEFAULT = 3.0


def short_sale_model(arv: float, purchase: float, rehab: float,
                     holding: float = 0.0,
                     holding_monthly: float = policy.HOLDING_UTILITIES_MONTHLY,
                     approval_months: float = SHORT_SALE_APPROVAL_MONTHS_DEFAULT,
                     financing: float = 0.0, closing_buy: float = 0.0,
                     closing_sell: float = 0.0,
                     contingency_pct: float = policy.REHAB_CONTINGENCY_PCT,
                     deal_type: Optional[str] = None) -> UnderwriteResult:
    """Short sale P&L: flip math plus the lender-approval patience tax.

    Mirrors the flip-tab cost stack but holds the property for however long
    the lender takes to approve the discounted payoff:

        holding_total = holding + holding_monthly × approval_months
        net_profit    = ARV − (purchase + rehab×(1+contingency)
                               + holding_total + financing + closings)
        ROI           = net_profit / cash invested
        annualized_ROI= ROI scaled to a 12-month clock — capital is tied up
                        through the approval window, so a patient deal must
                        earn more per month to compete.

    deal_type: optional override; defaults to the model's stamp "DT-SHORT".
    """
    if approval_months < 0:
        raise ValueError("short_sale_model: approval_months cannot be negative")
    holding_total = holding + (holding_monthly * approval_months)
    rehab_with_contingency = rehab * (1 + contingency_pct)
    total_costs = (purchase + rehab_with_contingency + holding_total
                   + financing + closing_buy + closing_sell)
    net_profit = arv - total_costs
    cash_invested = purchase + rehab_with_contingency + holding_total + financing + closing_buy
    roi = net_profit / cash_invested if cash_invested > 0 else 0.0
    annualized_roi = roi * (12.0 / approval_months) if approval_months > 0 else roi

    return UnderwriteResult(
        deal_type=deal_type if deal_type is not None else DealType.SHORT.value,
        model="short_sale",
        arv=arv, purchase_price=purchase, rehab=rehab,
        rehab_contingency_pct=contingency_pct,
        holding_costs=holding_total, financing_costs=financing,
        closing_costs_buy=closing_buy, closing_costs_sell=closing_sell,
        net_profit=net_profit, roi=roi, cash_invested=cash_invested,
        line_items={
            "rehab_with_contingency": rehab_with_contingency,
            "total_costs": total_costs,
            "holding_lump_sum": holding,
            "holding_monthly": holding_monthly,
            "approval_months": approval_months,
            "holding_total": holding_total,
            "annualized_roi": annualized_roi,
        },
    )


def _due_on_sale_risk_default() -> bool:
    """F6: resolve the due-on-sale risk stamp from the rule hierarchy.

    Order of authority: a versioned constant in src/legal_rules.py
    (``SUBJECT_TO_DUE_ON_SALE_RISK`` — legal truth lives there) if present,
    else a policy constant (``DUE_ON_SALE_RISK``), else the conservative
    default True. Resolved at call time so a future attorney-verified
    constant lands in legal_rules and is picked up without touching this
    module.
    """
    legal_flag = getattr(legal_rules, "SUBJECT_TO_DUE_ON_SALE_RISK", None)
    if legal_flag is not None:
        return bool(legal_flag)
    policy_flag = getattr(policy, "DUE_ON_SALE_RISK", None)
    if policy_flag is not None:
        return bool(policy_flag)
    return True


def subject_to_model(arv: float, existing_loan_balance: float,
                     cash_to_seller: float = 0.0, rent_monthly: float = 0.0,
                     existing_payment_monthly: float = 0.0,
                     existing_rate: float = 0.0, closing_buy: float = 0.0,
                     deal_type: Optional[str] = None) -> UnderwriteResult:
    """Subject-to economics: equity capture against the existing lien.

    Mirrors the workbook Subject-To tab row-for-row:

        equity_captured   = ARV − existing loan − cash to seller   (tab B11)
        cash_to_control   = cash to seller                         (tab B12)
        monthly_cash_flow = rent − existing P&I payment            (tab B13)
        annual_cash_flow  = monthly_cash_flow × 12                 (tab B14)
        cash-on-cash      = annual_cash_flow / cash actually in     (tab B15)

    ``net_profit`` carries the captured equity; ``roi``/``cash_on_cash``
    carry the tab's cash-on-cash yield on the cash invested (cash to seller
    plus any buy-side closing costs). Every subject-to acquisition carries
    due-on-sale exposure — the lender may call the loan on transfer — so
    ``due_on_sale_risk`` is stamped from the rule hierarchy (legal_rules
    constant if present, else policy, else conservative True; see
    ``_due_on_sale_risk_default``). The workbook forces an explicit
    acknowledgment; this flag is what surfaces it downstream.

    deal_type: optional override; defaults to the model's stamp "DT-SUBTO".
    """
    equity_captured = arv - existing_loan_balance - cash_to_seller
    monthly_cash_flow = rent_monthly - existing_payment_monthly
    annual_cash_flow = monthly_cash_flow * 12.0
    cash_invested = cash_to_seller + closing_buy
    coc = annual_cash_flow / cash_invested if cash_invested > 0 else 0.0

    return UnderwriteResult(
        deal_type=deal_type if deal_type is not None else "DT-SUBTO",
        model="subject_to",
        arv=arv, purchase_price=existing_loan_balance,
        closing_costs_buy=closing_buy,
        net_profit=equity_captured, roi=coc, cash_invested=cash_invested,
        cash_on_cash=coc,
        due_on_sale_risk=_due_on_sale_risk_default(),
        line_items={
            "equity_captured": equity_captured,
            "cash_to_control": cash_to_seller,
            "existing_loan_balance": existing_loan_balance,
            "existing_rate": existing_rate,
            "existing_payment_monthly": existing_payment_monthly,
            "monthly_cash_flow": monthly_cash_flow,
            "annual_cash_flow": annual_cash_flow,
            "due_on_sale_note": (
                "Lender may call the loan due on transfer — verify with "
                "attorney and acknowledge before proceeding"
            ),
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


def _require_existing_loan_balance(kwargs: dict[str, Any]) -> float:
    """F6: DT-SUBTO needs the existing lien — it IS the deal.

    Assuming a zero balance would fabricate the entire equity capture and
    hide the due-on-sale exposure, so the balance is required explicitly.
    """
    if ("existing_loan_balance" not in kwargs
            or kwargs["existing_loan_balance"] is None):
        raise ValueError(
            "route_underwrite: 'existing_loan_balance' is required for "
            "DT-SUBTO — refusing to assume a zero balance (it defines both "
            "the equity capture and the due-on-sale exposure)"
        )
    return kwargs["existing_loan_balance"]


# F6: every deal type this engine underwrites, mapped explicitly. There is
# deliberately NO fallback branch: an unmapped deal_type raises instead of
# quietly getting flip economics (the pre-F6 silent default).
_FLIP_DEAL_TYPES = ("DT-AUCTION", "DT-PREFORE", "DT-REO",
                    "DT-OFFMARKET", "DT-ONMARKET")


def route_underwrite(cpr: Optional[CPR], arv: float, rehab: float,
                     deal_type: str = "DT-AUCTION",
                     **kwargs: Any) -> UnderwriteResult:
    """Route to the correct underwriting model based on deal type.

    F6 mapping (model names are stamped on the returned result):
      * DT-AUCTION / DT-PREFORE / DT-REO / DT-OFFMARKET / DT-ONMARKET
        → flip_pnl (REO/OFFMARKET/ONMARKET were previously silent-defaulted
        here; they are now explicit flips — retail-listing acquisitions with
        full inspection/title, same cost stack).
      * DT-WHOLESALE → flip_pnl with closing costs PASSED THROUGH (the old
        branch dropped closing_buy/closing_sell on the floor; a double-close
        pays two title bills).
      * DT-TAXDEED → tax_deed_yield.
      * DT-SHORT → short_sale_model (lender-approval patience economics;
        requires `purchase`).
      * DT-SUBTO → subject_to_model (equity capture vs the existing lien +
        due-on-sale risk flag; requires `existing_loan_balance`).
      * DT-RENTAL → rental_metrics (`purchase` required — H1 applies).
      * DT-BRRRR → brrrr_recycle (`purchase` required — verified buy box,
        no MAO default).

    AUDIT H1: `purchase` is REQUIRED for every purchase-price model
    (flip / wholesale / short / rental / BRRRR) — a missing purchase raises
    ValueError instead of defaulting to MAO. The routed `deal_type` is
    stamped onto the result (the models' own defaults only apply to direct
    calls). An unrecognized deal_type raises ValueError — no silent flip
    fallback. ``cpr`` is accepted for interface symmetry with the other
    AGENT-* entrypoints; the pure models here do not read it.
    """
    dt = str(getattr(deal_type, "value", deal_type) or "").upper()
    if dt in _FLIP_DEAL_TYPES:
        return flip_pnl(arv, _require_purchase(kwargs), rehab,
                        holding=kwargs.get("holding", 0),
                        financing=kwargs.get("financing", 0),
                        closing_buy=kwargs.get("closing_buy", 0),
                        closing_sell=kwargs.get("closing_sell", arv * policy.CLOSING_SELL_PCT),
                        deal_type=dt)
    elif dt == "DT-WHOLESALE":
        # F6: assignment/double-close closing costs must survive routing —
        # the old branch silently dropped whatever the caller passed.
        return flip_pnl(arv, _require_purchase(kwargs), rehab,
                        holding=kwargs.get("holding", 0),
                        financing=kwargs.get("financing", 0),
                        closing_buy=kwargs.get("closing_buy", 0),
                        closing_sell=kwargs.get("closing_sell", 0),
                        deal_type=dt)
    elif dt == "DT-TAXDEED":
        return tax_deed_yield(kwargs.get("bid", 0),
                              recording_fee=kwargs.get("recording_fee", 0),
                              redemption_class=kwargs.get("redemption_class", RedemptionClass.OTHER),
                              year=kwargs.get("year", 1),
                              deal_type=dt)
    elif dt == "DT-SHORT":
        return short_sale_model(
            arv, _require_purchase(kwargs), rehab,
            holding=kwargs.get("holding", 0),
            holding_monthly=kwargs.get("holding_monthly", policy.HOLDING_UTILITIES_MONTHLY),
            approval_months=kwargs.get("approval_months", SHORT_SALE_APPROVAL_MONTHS_DEFAULT),
            financing=kwargs.get("financing", 0),
            closing_buy=kwargs.get("closing_buy", 0),
            closing_sell=kwargs.get("closing_sell", arv * policy.CLOSING_SELL_PCT),
            deal_type=dt)
    elif dt == "DT-SUBTO":
        return subject_to_model(
            arv,
            existing_loan_balance=_require_existing_loan_balance(kwargs),
            cash_to_seller=kwargs.get("cash_to_seller", 0),
            rent_monthly=kwargs.get("rent_monthly", 0),
            existing_payment_monthly=kwargs.get("existing_payment_monthly", 0),
            existing_rate=kwargs.get("existing_rate", 0),
            closing_buy=kwargs.get("closing_buy", 0),
            deal_type=dt)
    elif dt == "DT-RENTAL":
        return rental_metrics(
            price=_require_purchase(kwargs),
            rent_monthly=kwargs.get("rent_monthly", 0),
            opex_annual=kwargs.get("opex_annual", 0),
            taxes_annual=kwargs.get("taxes_annual", 0),
            insurance_annual=kwargs.get("insurance_annual", 0),
            mgmt_pct=kwargs.get("mgmt_pct", 0.08),
            vacancy_pct=kwargs.get("vacancy_pct", 0.05),
            maintenance_pct=kwargs.get("maintenance_pct", 0.05),
            debt_service_annual=kwargs.get("debt_service_annual", 0),
            equity_invested=kwargs.get("equity_invested"),
            deal_type=dt)
    elif dt == "DT-BRRRR":
        return brrrr_recycle(
            arv, _require_purchase(kwargs), rehab,
            refi_ltv=kwargs.get("refi_ltv", 0.75),
            refi_appraisal=kwargs.get("refi_appraisal", 0),
            holding=kwargs.get("holding", 0),
            financing=kwargs.get("financing", 0),
            closing_buy=kwargs.get("closing_buy", 0),
            closing_sell=kwargs.get("closing_sell", 0),
            deal_type=dt)
    else:
        raise ValueError(
            f"route_underwrite: no underwriting model mapped for deal_type "
            f"{deal_type!r} — refusing to default to flip economics silently"
        )


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

    # --- F6: SHORT / SUBTO models ---
    ss = short_sale_model(arv=300_000, purchase=150_000, rehab=30_000)
    _check("short_sale stamps DT-SHORT", ss.deal_type == "DT-SHORT"
           and ss.model == "short_sale")
    expected_holding = 3 * policy.HOLDING_UTILITIES_MONTHLY  # default window
    _check("short sale holding accrues over default approval window",
           ss.line_items["holding_total"] == expected_holding
           and ss.holding_costs == expected_holding)
    expected_profit = 300_000 - (150_000 + 36_000 + expected_holding)
    _check("short sale net profit = ARV − costs incl. patience tax",
           abs(ss.net_profit - expected_profit) < 1e-9)

    ss_fast = short_sale_model(arv=300_000, purchase=150_000, rehab=30_000,
                               approval_months=1.0)
    ss_slow = short_sale_model(arv=300_000, purchase=150_000, rehab=30_000,
                               approval_months=6.0)
    _check("patience costs money: longer approval -> less profit",
           ss_slow.net_profit < ss_fast.net_profit)
    _check("annualized ROI rewards faster approvals",
           ss_fast.line_items["annualized_roi"] > ss_slow.line_items["annualized_roi"])
    print(f"underwriting.py: short-sale — profit=${ss.net_profit:,.0f} "
          f"@ {ss.line_items['approval_months']:.0f}mo approval")

    st = subject_to_model(arv=320_000, existing_loan_balance=280_000,
                          cash_to_seller=10_000, rent_monthly=2_200,
                          existing_payment_monthly=1_500, existing_rate=0.03)
    _check("subject_to stamps DT-SUBTO", st.deal_type == "DT-SUBTO"
           and st.model == "subject_to")
    _check("subto equity capture = ARV − loan − cash to seller (tab B11)",
           st.net_profit == 30_000)
    _check("subto cash-to-control recorded (tab B12)",
           st.line_items["cash_to_control"] == 10_000)
    _check("subto monthly cash flow = rent − P&I (tab B13)",
           st.line_items["monthly_cash_flow"] == 700)
    _check("subto CoC = annual CF / cash in (tab B15)",
           abs(st.cash_on_cash - (700 * 12) / 10_000) < 1e-9
           and abs(st.roi - st.cash_on_cash) < 1e-9)
    _check("subto due-on-sale risk always flagged", st.due_on_sale_risk is True)
    _check("due-on-sale flag resolves True from the rule hierarchy today",
           _due_on_sale_risk_default() is True)
    flip_result_flag = flip_pnl(arv=1, purchase=1, rehab=0).due_on_sale_risk
    _check("non-subto models leave due_on_sale_risk unset (None)",
           flip_result_flag is None)
    print(f"underwriting.py: subject-to — equity=${st.net_profit:,.0f}, "
          f"CoC={st.cash_on_cash:.0%}, due-on-sale flagged")

    # --- F6: full router matrix — every deal type routes non-default ---
    _matrix = {
        "DT-AUCTION":   ("flip", dict(purchase=170_000)),
        "DT-PREFORE":   ("flip", dict(purchase=170_000)),
        "DT-REO":       ("flip", dict(purchase=170_000)),
        "DT-OFFMARKET": ("flip", dict(purchase=170_000)),
        "DT-ONMARKET":  ("flip", dict(purchase=170_000)),
        "DT-WHOLESALE": ("flip", dict(purchase=150_000,
                                      closing_buy=2_000, closing_sell=5_000)),
        "DT-TAXDEED":   ("redemption_yield", dict(bid=100_000)),
        "DT-SHORT":     ("short_sale", dict(purchase=150_000)),
        "DT-SUBTO":     ("subject_to", dict(existing_loan_balance=200_000,
                                            cash_to_seller=10_000,
                                            rent_monthly=2_000,
                                            existing_payment_monthly=1_400)),
        "DT-RENTAL":    ("rental", dict(purchase=200_000, rent_monthly=2_000,
                                        taxes_annual=4_000,
                                        insurance_annual=1_500)),
        "DT-BRRRR":     ("brrrr", dict(purchase=170_000)),
    }
    for _dt, (_expect_model, _kw) in sorted(_matrix.items()):
        _r = route_underwrite(None, arv=300_000, rehab=30_000,
                              deal_type=_dt, **_kw)
        _check(f"router {_dt} -> {_expect_model}",
               _r.model == _expect_model and _r.deal_type == _dt)
    _uncovered = [m.value for m in DealType if m.value not in _matrix]
    _check("matrix covers every DealType enum member", not _uncovered)

    # Case-insensitive routing on raw strings
    _check("router is case-insensitive ('dt-reo')",
           route_underwrite(None, arv=300_000, rehab=30_000, deal_type="dt-reo",
                            purchase=170_000).model == "flip")

    # Unknown deal types fail loudly — no silent flip fallback (F6)
    try:
        route_underwrite(None, arv=300_000, rehab=30_000,
                         deal_type="DT-NONSENSE", purchase=100_000)
        raised_unknown = False
    except ValueError:
        raised_unknown = True
    _check("unknown deal_type raises ValueError (no silent default)",
           raised_unknown)

    # F6: wholesale closing costs pass through routing
    ws_clean = route_underwrite(None, arv=300_000, rehab=30_000,
                                deal_type="DT-WHOLESALE", purchase=150_000)
    ws_costed = route_underwrite(None, arv=300_000, rehab=30_000,
                                 deal_type="DT-WHOLESALE", purchase=150_000,
                                 closing_buy=3_000, closing_sell=9_000)
    _check("wholesale closing costs present on routed result",
           ws_costed.closing_costs_buy == 3_000
           and ws_costed.closing_costs_sell == 9_000)
    _check("wholesale closing costs reduce net profit dollar-for-dollar",
           abs((ws_clean.net_profit - ws_costed.net_profit) - 12_000) < 1e-9)

    # H1 extends to the new branches: rental needs purchase, subto needs loan
    for _dt_bad, _kw_bad in (("DT-RENTAL", dict(rent_monthly=2_000)),
                             ("DT-BRRRR", dict()),
                             ("DT-SHORT", dict())):
        try:
            route_underwrite(None, arv=300_000, rehab=30_000,
                             deal_type=_dt_bad, **_kw_bad)
            raised_req = False
        except ValueError:
            raised_req = True
        _check(f"{_dt_bad} without required input raises ValueError", raised_req)
    try:
        route_underwrite(None, arv=300_000, rehab=0, deal_type="DT-SUBTO",
                         cash_to_seller=5_000)
        raised_loan = False
    except ValueError:
        raised_loan = True
    _check("DT-SUBTO without existing_loan_balance raises ValueError",
           raised_loan)

    print(f"\nunderwriting.py: checks {_results['pass']} passed, {_results['fail']} failed")
    print("underwriting.py: all self-checks passed")