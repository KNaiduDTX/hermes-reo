# src/legal_rules.py
"""LEGAL-RULES v1 — versioned Texas real estate distressed acquisition rules.

Every rule carries verified_by + last_verified. The system treats these as
versioned and re-verifiable, not hardcoded truth. See LEGAL_RULES.md for full
documentation.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional

VERSION = "1.0"
LAST_VERIFIED = "2026-08-19"
VERIFIED_BY = "spec_research"  # pending TX RE attorney confirmation


class County(str, Enum):
    COLLIN = "collin"
    DALLAS = "dallas"
    GRAYSON = "grayson"


class RedemptionClass(str, Enum):
    HOMESTEAD_AG = "homestead_ag"      # 2-year redemption
    OTHER = "other"                     # 180-day redemption


class DealType(str, Enum):
    PREFORE = "DT-PREFORE"
    AUCTION = "DT-AUCTION"
    REO = "DT-REO"
    TAXDEED = "DT-TAXDEED"
    SHORT = "DT-SHORT"
    WHOLESALE = "DT-WHOLESALE"
    ONMARKET = "DT-ONMARKET"
    OFFMARKET = "DT-OFFMARKET"
    # Audit UW-7: these three were previously bare strings (underwriting.py
    # stamped a literal "DT-SUBTO"), so a misspelled deal_type survived
    # enrichment/pricing and only exploded later at route_underwrite.
    SUBTO = "DT-SUBTO"
    RENTAL = "DT-RENTAL"
    BRRRR = "DT-BRRRR"


# --- R1: Mortgage Foreclosure (non-judicial trustee sale) ---

FORECLOSURE_TYPE = "non-judicial"
FORECLOSURE_STATUTE = "Tex. Prop. Code §51.002"

# Timeline: 20-day cure + 21-day sale notice = ~41 days minimum
CURE_NOTICE_DAYS = 20
SALE_NOTICE_DAYS = 21
MIN_DAYS_TO_SALE = CURE_NOTICE_DAYS + SALE_NOTICE_DAYS  # ~41

# Sale mechanics
SALE_DAY = "first_tuesday_of_month"  # moves to first Wednesday if Jan 1 or Jul 4
SALE_TIME_START = "10:00"
SALE_TIME_END = "16:00"
SALE_MUST_BEGIN_WITHIN_HOURS = 3

# Redemption: NONE after mortgage foreclosure
MORTGAGE_REDEMPTION = None  # sale is final

# Post-sale: 3-day notice to vacate before eviction
EVICTION_NOTICE_DAYS = 3


# --- R2: Tax-foreclosure sale (redeemable tax deed) ---

TAX_SALE_TYPE = "judicial"
TAX_SALE_STATUTE = "Tex. Tax Code Ch. 34, §34.21"

# Redemption periods
REDEMPTION_DAYS_OTHER = 180          # non-homestead/ag
REDEMPTION_DAYS_HOMESTEAD = 730       # 2 years (homestead/ag/mineral)

# Redemption premium (one-time statutory penalty on aggregate)
REDEMPTION_PREMIUM_YR1 = 0.25        # 25% if redeemed in year 1
REDEMPTION_PREMIUM_YR2 = 0.50        # 50% if redeemed in year 2 (2-year class only)
# 180-day class caps at 25%

# IRS federal tax lien: 120-day right of redemption
IRS_REDEMPTION_DAYS = 120

# Aggregate basis for premium: bid + recording fee + post-sale taxes/costs


# --- R3: Non-disclosure state ---

NON_DISCLOSURE = True
NON_DISCLOSURE_STATUTES = ["Gov. Code §552.149", "Tax Code §22.27"]

# Approved comp provenance sources (rigor tier 0-3)
COMP_SOURCE_TIER = {
    "mls_agent": 3,       # highest rigor
    "appraiser_bpo": 2,   # high rigor
    "title_feed": 1,      # medium
    "vendor_avm": 0,      # directional only
}
REJECTED_COMP_SOURCES = {"assessed_value", "zestimate", "consumer_avm"}


# --- R4: Wholesaling / assignment legality ---

WHOLESALE_STATUTES = [
    "Occ. Code §1101.0045", "Occ. Code §1101.351",
    "Prop. Code §5.0205", "SB 2212 (2017)", "SB 1577 (eff. Jan 1, 2024)",
]
WHOLESALE_BILATERAL_DISCLOSURE = True  # mandatory since Jan 1, 2024
WHOLESALE_DISCLOSURE_REQUIRED_BEFORE = "contracting"

# Prohibited (makes it unlicensed brokerage — Class A misdemeanor)
WHOLESALE_PROHIBITED = [
    "mls_listing",
    "representing_party",
    "negotiating_for_party",
    "marketing_as_owned",
]


# --- R5: Standard purchase & option mechanics ---

EARNEST_MONEY_DAYS = 3  # deliver to escrow within 3 days of effective date
# Option fee buys unrestricted right to terminate during option period
# If no option fee/period set, no unrestricted termination right


# --- R6: Deal-type risk matrix ---

DEAL_TYPE_RISK = {
    DealType.AUCTION: {
        "redemption": None,
        "title_at_purchase": False,  # no title insurance at sale
        "key_risk": "occupied_eviction",
        "inspection": "drive_by_only",
    },
    DealType.TAXDEED: {
        "redemption": "statutory",   # 180d or 2yr
        "title_at_purchase": False,   # quiet-title needed post-redemption
        "key_risk": "no_clear_title",
        "inspection": "limited_during_redemption",
    },
    DealType.PREFORE: {
        "redemption": None,
        "title_at_purchase": True,
        "key_risk": "speed_41_day_clock",
        "inspection": "possible_with_access",
    },
    DealType.REO: {
        "redemption": None,
        "title_at_purchase": True,
        "key_risk": "near_retail_margin",
        "inspection": "full_inspection_available",
    },
    DealType.WHOLESALE: {
        "redemption": "per_underlying",
        "title_at_purchase": "per_underlying",
        "key_risk": "disclosure_compliance",
        "inspection": "per_underlying",
    },
}


# --- County-specific buyer eligibility ---

BUYER_ELIGIBILITY = {
    County.COLLIN: {
        "requirement": "Written Statement declaring no delinquent tax owed in county",
        "cost": 10,
        "source": "Tax Assessor office",
    },
    County.DALLAS: {
        "requirement": "Register with RealAuction.com (online auction)",
        "cost": None,  # TBD
        "source": "dallas.texas.sheriffsaleauctions.com",
    },
    County.GRAYSON: {
        "requirement": "TBD — verify in WS-00",
        "cost": None,
        "source": "Sheriff's office",
    },
}


# --- Helper functions ---

def get_redemption_class(homestead: bool, agricultural: bool) -> RedemptionClass:
    """Determine redemption class from exemption flags."""
    if homestead or agricultural:
        return RedemptionClass.HOMESTEAD_AG
    return RedemptionClass.OTHER


def get_redemption_days(redemption_class: RedemptionClass) -> int:
    """Return redemption period in days for the given class."""
    if redemption_class == RedemptionClass.HOMESTEAD_AG:
        return REDEMPTION_DAYS_HOMESTEAD
    return REDEMPTION_DAYS_OTHER


def get_redemption_premium(redemption_class: RedemptionClass, year: int) -> float:
    """Return the statutory premium fraction for redemption in a given year.

    Year 1: 25% for both classes.
    Year 2: 50% for homestead/ag, N/A for 180-day class (caps at 25%).
    """
    if year == 1:
        return REDEMPTION_PREMIUM_YR1
    if year == 2 and redemption_class == RedemptionClass.HOMESTEAD_AG:
        return REDEMPTION_PREMIUM_YR2
    # 180-day class: only year 1 applies
    if year == 2 and redemption_class == RedemptionClass.OTHER:
        return REDEMPTION_PREMIUM_YR1  # caps at 25%
    return REDEMPTION_PREMIUM_YR1


def compute_redemption_yield(bid: float, recording_fee: float = 0,
                             post_sale_costs: float = 0,
                             redemption_class: RedemptionClass = RedemptionClass.OTHER,
                             year: int = 1) -> float:
    """Compute the guaranteed redemption yield (lump-sum return).

    premium = aggregate * rate
    aggregate = bid + recording_fee + post_sale_costs
    """
    aggregate = bid + recording_fee + post_sale_costs
    rate = get_redemption_premium(redemption_class, year)
    return aggregate * rate


def is_comp_source_approved(source: str) -> bool:
    """Check if a comp source is in the approved provenance list."""
    return source in COMP_SOURCE_TIER


def is_comp_source_rejected(source: str) -> bool:
    """Check if a comp source is explicitly rejected (fake-signal)."""
    return source in REJECTED_COMP_SOURCES


def get_comp_rigor_tier(source: str) -> int:
    """Return the rigor tier (0-3) for a comp source."""
    return COMP_SOURCE_TIER.get(source, -1)  # -1 = unknown/unapproved


def get_version_info() -> dict:
    """Return version metadata for audit logging."""
    return {
        "version": VERSION,
        "last_verified": LAST_VERIFIED,
        "verified_by": VERIFIED_BY,
    }


if __name__ == "__main__":
    # Self-check: redemption math
    assert get_redemption_days(RedemptionClass.OTHER) == 180
    assert get_redemption_days(RedemptionClass.HOMESTEAD_AG) == 730
    assert get_redemption_premium(RedemptionClass.OTHER, 1) == 0.25
    assert get_redemption_premium(RedemptionClass.HOMESTEAD_AG, 1) == 0.25
    assert get_redemption_premium(RedemptionClass.HOMESTEAD_AG, 2) == 0.50
    assert get_redemption_premium(RedemptionClass.OTHER, 2) == 0.25  # caps

    # Redemption yield: $100k bid, 180-day class, year 1 → $25k premium
    y = compute_redemption_yield(100_000, redemption_class=RedemptionClass.OTHER, year=1)
    assert y == 25_000, f"Expected 25000, got {y}"

    # Homestead, year 2 → $50k premium
    y2 = compute_redemption_yield(100_000, redemption_class=RedemptionClass.HOMESTEAD_AG, year=2)
    assert y2 == 50_000, f"Expected 50000, got {y2}"

    # Comp source checks
    assert is_comp_source_approved("mls_agent")
    assert not is_comp_source_approved("zestimate")
    assert is_comp_source_rejected("assessed_value")
    assert get_comp_rigor_tier("mls_agent") == 3
    assert get_comp_rigor_tier("vendor_avm") == 0

    # Redemption class from flags
    assert get_redemption_class(True, False) == RedemptionClass.HOMESTEAD_AG
    assert get_redemption_class(False, True) == RedemptionClass.HOMESTEAD_AG
    assert get_redemption_class(False, False) == RedemptionClass.OTHER

    print("legal_rules.py: all self-checks passed")