# src/metrics.py
"""Shared deal-economics kernels (TD8).

Single definitions for the arithmetic that more than one consumer needs, so
find_deals.analyze_deal and portfolio-level rollups (src.synthesis) can never
drift apart on how equity / LTV are derived from CAD value and mortgage debt.

Pure functions only: no I/O, no CPR coupling — callers unwrap provenance on
their side (e.g. CPR.get) and pass plain numbers.
"""
from __future__ import annotations
from typing import Any, Dict, Optional


def compute_leverage_metrics(cad_value: Optional[float],
                             mortgage_debt: Optional[float]) -> Dict[str, Optional[float]]:
    """Equity/leverage trio for one parcel from CAD value vs mortgage debt.

    Returns ``{"equity_estimate", "equity_pct", "ltv_pct"}``, each None when
    the inputs are unusable (missing CAD value or non-positive debt). This is
    the exact historical find_deals.analyze_deal inline math:

    - equity_estimate = cad_value - mortgage_debt
    - equity_pct      = equity_estimate / cad_value * 100   (0 when cad <= 0)
    - ltv_pct         = mortgage_debt / cad_value * 100

    >>> compute_leverage_metrics(300_000.0, 150_000.0)
    {'equity_estimate': 150000.0, 'equity_pct': 50.0, 'ltv_pct': 50.0}
    >>> compute_leverage_metrics(None, 150_000.0)["equity_estimate"] is None
    True
    >>> compute_leverage_metrics(300_000.0, 0)["ltv_pct"] is None
    True
    """
    equity_estimate = None
    equity_pct = None
    ltv_pct = None
    if cad_value and mortgage_debt and mortgage_debt > 0:
        equity_estimate = cad_value - mortgage_debt
        equity_pct = (equity_estimate / cad_value) * 100 if cad_value > 0 else 0
        ltv_pct = (mortgage_debt / cad_value) * 100
    return {"equity_estimate": equity_estimate,
            "equity_pct": equity_pct,
            "ltv_pct": ltv_pct}


if __name__ == "__main__":
    # Self-check: the documented examples plus the negative-debt guard.
    assert compute_leverage_metrics(300_000.0, 150_000.0) == {
        "equity_estimate": 150000.0, "equity_pct": 50.0, "ltv_pct": 50.0}
    assert compute_leverage_metrics(None, 150_000.0) == {
        "equity_estimate": None, "equity_pct": None, "ltv_pct": None}
    assert compute_leverage_metrics(300_000.0, 0) == {
        "equity_estimate": None, "equity_pct": None, "ltv_pct": None}
    assert compute_leverage_metrics(200_000.0, -5_000.0) == {
        "equity_estimate": None, "equity_pct": None, "ltv_pct": None}
    # Underwater parcel: negative equity survives as a number, not None.
    under = compute_leverage_metrics(100_000.0, 250_000.0)
    assert under["equity_estimate"] == -150_000.0
    assert under["equity_pct"] == -150.0 and under["ltv_pct"] == 250.0
    print("metrics.py: all self-checks passed")
