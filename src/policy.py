# src/policy.py
"""Single source of truth for decision-policy constants (AUDIT Wave 3).

Every module that prices a deal must import its thresholds from here.
Before this module existed the same facts lived in two+ places and drifted:
pipeline priced seller closing costs at 8% while the Excel model used 7.5%,
so a memo and its workbook disagreed about the same property (audit P7/X9).

Values are pinned to the shipped Assumptions-tab defaults so the workbook's
computed outputs are unchanged by this refactor — only divergence dies.
"""
from __future__ import annotations

# --- Offer / MAO policy -------------------------------------------------
MAO_FACTOR_DEFAULT = 0.70          # 70% rule
REHAB_CONTINGENCY_PCT = 0.20       # industry standard 10–25% by scope

# --- Wholesale (audit UW-3) ----------------------------------------------
# Seller price for wholesale = (ARV x 0.65) - rehab - fee, matching the
# workbook Wholesale tab (Assumptions!assignment_fee default = $10,000, "DFW
# market — $5-15k typical"). The engine previously quoted the 70% rule here,
# so a memo and the workbook disagreed about the same deal.
WHOLESALE_FACTOR_DEFAULT = 0.65
WHOLESALE_ASSIGNMENT_FEE = 10_000.0

# --- Closing costs (fraction of price) ----------------------------------
CLOSING_BUY_PCT = 0.025            # TX title promulgated + escrow + recording
CLOSING_SELL_PCT = 0.075           # ~5-6% commission + title + concessions

# --- Holding costs -------------------------------------------------------
HOLDING_UTILITIES_MONTHLY = 200.0  # utilities/upkeep $/mo during hold

# --- Rehab heuristics ----------------------------------------------------
SIGHT_UNSEEN_REHAB_MULT = 1.5      # sight-unseen bids marked up 50%

# --- Blast-radius tiers (capital at risk, USD) ---------------------------
BLAST_TIER_T2_MAX = 25_000         # <= this → T1 small
BLAST_TIER_T3_MAX = 150_000        # <= this → T2 mid; above → T3 large

# --- Offer terms (negotiation) --------------------------------------------
EARNEST_MONEY_PCT = 0.01           # 1% of offer price
EARNEST_MONEY_MIN = 1_000
OPTION_FEE_PCT = 0.001             # ~0.1% of offer price
OPTION_FEE_MIN = 200
OPTION_PERIOD_DAYS = 7
OPTION_PERIOD_DAYS_EXTENDED = 14
CLOSING_TIMELINE_DAYS = 7
CLOSING_TIMELINE_DAYS_MAX = 45

# --- GATE-VAL floors (valuation) -----------------------------------------
CONFIDENCE_FLOOR_T0 = 0.50         # screen only
CONFIDENCE_FLOOR_T1 = 0.60         # small deal
CONFIDENCE_FLOOR_T2 = 0.75         # mid — agent-MLS or BPO required
CONFIDENCE_FLOOR_T3 = 0.85         # large/appraiser-grade
MIN_COMPS = 3
MAX_COMP_DISTANCE_MI = 0.5
MAX_COMP_AGE_DAYS = 180


def self_check() -> None:
    """Guard against accidental value drift from the audited defaults."""
    assert MAO_FACTOR_DEFAULT == 0.70
    assert REHAB_CONTINGENCY_PCT == 0.20
    assert WHOLESALE_FACTOR_DEFAULT < MAO_FACTOR_DEFAULT
    assert 5_000 <= WHOLESALE_ASSIGNMENT_FEE <= 15_000
    assert CLOSING_SELL_PCT == 0.075 and CLOSING_BUY_PCT == 0.025
    assert BLAST_TIER_T2_MAX < BLAST_TIER_T3_MAX
    assert EARNEST_MONEY_PCT > OPTION_FEE_PCT
    assert CONFIDENCE_FLOOR_T0 < CONFIDENCE_FLOOR_T1 < \
        CONFIDENCE_FLOOR_T2 < CONFIDENCE_FLOOR_T3


if __name__ == "__main__":
    self_check()
    print("policy.py: constant sanity checks passed")
