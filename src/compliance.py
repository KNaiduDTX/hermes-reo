# src/compliance.py
"""AGENT-LEGAL: compliance gates (spec §12).

GATE-LEGAL-WHOLESALE: bilateral disclosure + licensing boundary
GATE-LEGAL-ELIGIBILITY: auction buyer eligibility
GATE-LEGAL-REDEMPTION: tax-deed redemption compliance
GATE-LEGAL-LIEN: title & lien-priority (mandatory before pricing/bidding)
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, date
from .cpr import CPR, CPRColumn, LienRecord
from .legal_rules import (
    County, DealType, RedemptionClass,
    get_redemption_class, get_redemption_days, get_redemption_premium,
    BUYER_ELIGIBILITY, WHOLESALE_BILATERAL_DISCLOSURE,
    WHOLESALE_DISCLOSURE_REQUIRED_BEFORE, WHOLESALE_PROHIBITED,
    IRS_REDEMPTION_DAYS,
)


@dataclass
class GateResult:
    gate_name: str
    verdict: str   # "pass", "fail", "hitl"
    reason: str
    details: dict


# --- GATE-LEGAL-WHOLESALE ---

def gate_legal_wholesale(cpr: CPR, has_disclosure: bool = False,
                         action: str = "assign") -> GateResult:
    """GATE-LEGAL-WHOLESALE: wholesaling disclosure & licensing boundary.

    Blocks any assignment/wholesale marketing or contract until bilateral
    written equitable-interest disclosure is generated and attached.
    Hard-blocks fleet actions that constitute brokerage.
    """
    if cpr.deal_type != DealType.WHOLESALE.value:
        return GateResult("GATE-LEGAL-WHOLESALE", "pass",
                           "Not a wholesale deal — gate N/A", {})

    if not has_disclosure:
        return GateResult("GATE-LEGAL-WHOLESALE", "fail",
                          f"Bilateral disclosure required before {WHOLESALE_DISCLOSURE_REQUIRED_BEFORE} "
                          f"(Prop. Code §5.0205 / SB 1577)",
                          {"required": True, "provided": False})

    # Check for prohibited brokerage actions
    if action in WHOLESALE_PROHIBITED:
        return GateResult("GATE-LEGAL-WHOLESALE", "fail",
                          f"Prohibited action: {action} constitutes unlicensed brokerage "
                          f"(Occ. Code §1101.351)",
                          {"action": action, "prohibited": True})

    return GateResult("GATE-LEGAL-WHOLESALE", "pass",
                      "Bilateral disclosure attached, action is compliant",
                      {"required": True, "provided": True, "action": action})


# --- GATE-LEGAL-ELIGIBILITY ---

def gate_legal_eligibility(cpr: CPR, has_written_statement: bool = False,
                           has_certified_funds: bool = False,
                           has_realauction_registration: bool = False) -> GateResult:
    """GATE-LEGAL-ELIGIBILITY: auction buyer eligibility.

    County-specific pre-bid requirements. Blocks staging a bid until satisfied.

    H5 fix: Dallas's actual requirement is RealAuction.com registration
    (``has_realauction_registration``) — certified funds alone never
    satisfied it. Grayson's requirements are documented as TBD, so any
    Grayson parcel routes to HITL for attorney verification and NEVER
    auto-passes a requirement nobody has verified.
    """
    county = County(cpr.county) if cpr.county in [c.value for c in County] else None
    if not county:
        return GateResult("GATE-LEGAL-ELIGIBILITY", "fail",
                          f"Unknown county: {cpr.county}", {})

    req = BUYER_ELIGIBILITY.get(county, {})
    missing = []

    if county == County.COLLIN:
        if not has_written_statement:
            missing.append("Written Statement ($10, Tax Assessor)")
    elif county == County.DALLAS:
        if not has_realauction_registration:
            missing.append("RealAuction.com registration (online auction)")
        if not has_certified_funds:
            missing.append("Certified funds for online auction")
    elif county == County.GRAYSON:
        return GateResult(
            "GATE-LEGAL-ELIGIBILITY", "hitl",
            "TBD-attorney-verification: Grayson County buyer-eligibility requirements are "
            f"undocumented ({req.get('requirement', 'verify in WS-00')}) — attorney must "
            "verify before any bid is staged",
            {"county": county.value, "requirements": req,
             "has_written_statement": has_written_statement,
             "has_certified_funds": has_certified_funds},
        )

    if missing:
        return GateResult("GATE-LEGAL-ELIGIBILITY", "fail",
                          f"Missing requirements: {', '.join(missing)}",
                          {"county": county.value, "missing": missing})

    return GateResult("GATE-LEGAL-ELIGIBILITY", "pass",
                      "All buyer eligibility requirements met",
                      {"county": county.value, "requirements": req})


# --- GATE-LEGAL-REDEMPTION ---

def gate_legal_redemption(cpr: CPR, deed_recording_date: str = "",
                          making_improvements: bool = False) -> GateResult:
    """GATE-LEGAL-REDEMPTION: tax-deed redemption compliance.

    Enforces redemption-class determination, premium math, no-irreversible-
    improvement rule, and quiet-title requirement post-expiry.
    """
    if cpr.deal_type != DealType.TAXDEED.value:
        return GateResult("GATE-LEGAL-REDEMPTION", "pass",
                          "Not a tax-deed deal — gate N/A", {})

    homestead = cpr.homestead.value if cpr.homestead else False
    ag = cpr.agricultural.value if cpr.agricultural else False
    redemption_class = get_redemption_class(homestead, ag)
    redemption_days = get_redemption_days(redemption_class)

    issues = []

    # No irreversible improvements during redemption
    if making_improvements:
        issues.append("Irreversible improvements during redemption period — prohibited")

    # Quiet-title required post-redemption
    if not deed_recording_date:
        issues.append("Deed recording date missing — cannot compute redemption expiry")

    if issues:
        return GateResult("GATE-LEGAL-REDEMPTION", "fail",
                          "; ".join(issues),
                          {"redemption_class": redemption_class.value,
                           "redemption_days": redemption_days})

    return GateResult("GATE-LEGAL-REDEMPTION", "pass",
                      f"Redemption class: {redemption_class.value}, "
                      f"period: {redemption_days} days, quiet-title required post-expiry",
                      {"redemption_class": redemption_class.value,
                       "redemption_days": redemption_days,
                       "premium_yr1": get_redemption_premium(redemption_class, 1),
                       "premium_yr2": get_redemption_premium(redemption_class, 2),
                       "irs_redemption_days": IRS_REDEMPTION_DAYS})


# --- GATE-LEGAL-LIEN ---

def gate_legal_lien(cpr: CPR) -> GateResult:
    """GATE-LEGAL-LIEN: title & lien-priority (mandatory before pricing/bidding).

    FIRST checks cpr.lien_lookup_status (audit S7): a FAILED lien search makes
    the stack untrustworthy, so the verdict is hitl / "lien-lookup-FAILED"
    regardless of what happens to be in it — an errored lookup must never be
    indistinguishable from clean title.

    Then requires the encumbrance stack + priority ranking. Flags senior liens
    surviving a junior foreclosure, property-tax liens surviving a mortgage
    foreclosure, and on tax deeds IRS 120-day / HOA / municipal survival.
    Routes to attorney/title HITL for anything non-trivial.
    """
    # Contract: errored lookup -> HITL regardless of stack contents
    lookup_status = getattr(cpr, "lien_lookup_status", "not_run")
    if lookup_status == "error":
        return GateResult("GATE-LEGAL-LIEN", "hitl",
                          "lien-lookup-FAILED — the title/lien search errored; the "
                          "encumbrance stack is incomplete or untrustworthy. Re-run the "
                          "search clean before pricing or bidding.",
                          {"lien_lookup_status": lookup_status,
                           "lien_count": len(cpr.liens)})

    if not cpr.liens:
        return GateResult("GATE-LEGAL-LIEN", "fail",
                          "No lien search performed — mandatory before pricing/bidding",
                          {"lien_count": 0})

    # Audit CPR-3: a stack carried with lookup_status='not_run' (e.g. a Dallas
    # live parcel whose only lien is self-derived from OCR'd debt) used to fall
    # through to a clean 'pass' below — zero verification, clean-title verdict.
    # An unverified stack routes to title-search HITL, never to pricing.
    if lookup_status != "ok":
        return GateResult("GATE-LEGAL-LIEN", "hitl",
                          "title-search-pending — encumbrance stack present but the "
                          "lien lookup has not been completed; the stack is unverified. "
                          "Run the clerk/title search before pricing or bidding.",
                          {"lien_lookup_status": lookup_status,
                           "lien_count": len(cpr.liens)})

    surviving = [l for l in cpr.liens if l.survives_foreclosure or l.survives_tax_sale]
    senior_surviving = [l for l in cpr.liens if l.priority == 1 and l.survives_foreclosure]

    issues = []
    if senior_surviving and cpr.deal_type == DealType.AUCTION.value:
        issues.append(f"Senior lien surviving foreclosure: {senior_surviving[0].lien_type} "
                       f"(${senior_surviving[0].amount or 0:,.0f})")
    if any(l.lien_type == "tax" and l.survives_foreclosure for l in cpr.liens):
        issues.append("Property-tax lien surviving mortgage foreclosure")
    if any(l.lien_type == "irs_federal" for l in cpr.liens):
        issues.append(f"IRS federal tax lien — {IRS_REDEMPTION_DAYS}-day right of redemption")
    if any(l.lien_type == "hoa" and l.survives_tax_sale for l in cpr.liens):
        issues.append("HOA lien surviving tax sale")
    if any(l.lien_type == "municipal" and l.survives_tax_sale for l in cpr.liens):
        issues.append("Municipal lien surviving tax sale")

    if issues:
        return GateResult("GATE-LEGAL-LIEN", "hitl",
                          "; ".join(issues),
                          {"lien_count": len(cpr.liens),
                           "surviving_count": len(surviving),
                           "issues": issues})

    return GateResult("GATE-LEGAL-LIEN", "pass",
                      f"Lien stack verified: {len(cpr.liens)} liens, no surviving-lien issues",
                      {"lien_count": len(cpr.liens), "surviving_count": 0})


# --- Run all gates ---

# Gate names accepted as keys of run_all_gates' gates_kwargs mapping.
GATE_NAMES = (
    "GATE-LEGAL-LIEN",
    "GATE-LEGAL-ELIGIBILITY",
    "GATE-LEGAL-WHOLESALE",
    "GATE-LEGAL-REDEMPTION",
)


def run_all_gates(cpr: CPR, gates_kwargs: dict | None = None) -> list[GateResult]:
    """Run all applicable compliance gates on a CPR.

    X2 fix: per-gate keyword arguments are passed EXPLICITLY via one
    ``gates_kwargs`` dict mapping gate name -> that gate's kwarg dict, instead
    of fanning shared **kwargs into every gate (which crashed with TypeError
    on any legitimate call). Works with zero arguments; gates absent from the
    mapping run with their defaults. Unknown gate names raise ValueError —
    this repo prefers loud failure over silently dropped arguments::

        run_all_gates(cpr, gates_kwargs={
            "GATE-LEGAL-ELIGIBILITY": {"has_written_statement": True},
            "GATE-LEGAL-WHOLESALE": {"has_disclosure": True},
        })
    """
    gates_kwargs = gates_kwargs or {}
    unknown = set(gates_kwargs) - set(GATE_NAMES)
    if unknown:
        raise ValueError(
            f"Unknown gate name(s) {sorted(unknown)} in gates_kwargs; "
            f"valid gates: {list(GATE_NAMES)}"
        )

    results = [
        gate_legal_lien(cpr, **gates_kwargs.get("GATE-LEGAL-LIEN", {})),
        gate_legal_eligibility(cpr, **gates_kwargs.get("GATE-LEGAL-ELIGIBILITY", {})),
    ]
    if cpr.deal_type == DealType.WHOLESALE.value:
        results.append(gate_legal_wholesale(
            cpr, **gates_kwargs.get("GATE-LEGAL-WHOLESALE", {})))
    if cpr.deal_type == DealType.TAXDEED.value:
        results.append(gate_legal_redemption(
            cpr, **gates_kwargs.get("GATE-LEGAL-REDEMPTION", {})))
    return results


if __name__ == "__main__":
    # Self-check: GATE-LEGAL-LIEN
    cpr = CPR(parcel_id="test", county="collin", deal_type="DT-AUCTION")
    r = gate_legal_lien(cpr)
    assert r.verdict == "fail"
    assert "No lien search" in r.reason

    cpr.add_lien(LienRecord(lien_type="mortgage", amount=300_000, priority=1,
                            survives_foreclosure=False), "test")
    # Audit CPR-3: a not_run lookup with a stack in hand must NEVER pass —
    # it routes to title-search HITL until a real lookup completes.
    r_unverified = gate_legal_lien(cpr)
    assert r_unverified.verdict == "hitl"
    assert "title-search-pending" in r_unverified.reason
    cpr.lien_lookup_status = "ok"
    r2 = gate_legal_lien(cpr)
    assert r2.verdict == "pass"

    # Senior lien surviving
    cpr2 = CPR(parcel_id="test2", county="collin", deal_type="DT-AUCTION")
    cpr2.add_lien(LienRecord(lien_type="mortgage", amount=300_000, priority=1,
                             survives_foreclosure=True), "test")
    cpr2.lien_lookup_status = "ok"  # CPR-3: stack-content checks need a completed lookup
    r3 = gate_legal_lien(cpr2)
    assert r3.verdict == "hitl"
    assert "Senior lien" in r3.reason

    # GATE-LEGAL-WHOLESALE
    cpr3 = CPR(parcel_id="test3", county="collin", deal_type="DT-WHOLESALE")
    r4 = gate_legal_wholesale(cpr3, has_disclosure=False)
    assert r4.verdict == "fail"
    assert "disclosure" in r4.reason.lower()

    r5 = gate_legal_wholesale(cpr3, has_disclosure=True, action="assign")
    assert r5.verdict == "pass"

    r6 = gate_legal_wholesale(cpr3, has_disclosure=True, action="mls_listing")
    assert r6.verdict == "fail"
    assert "Prohibited" in r6.reason

    # GATE-LEGAL-ELIGIBILITY
    cpr4 = CPR(parcel_id="test4", county="collin", deal_type="DT-AUCTION")
    r7 = gate_legal_eligibility(cpr4, has_written_statement=False)
    assert r7.verdict == "fail"
    assert "Written Statement" in r7.reason

    r8 = gate_legal_eligibility(cpr4, has_written_statement=True)
    assert r8.verdict == "pass"

    # H5: Dallas requires RealAuction registration — certified funds alone
    # never satisfied it.
    cpr_dal = CPR(parcel_id="test4d", county="dallas", deal_type="DT-AUCTION")
    r_dal1 = gate_legal_eligibility(cpr_dal, has_certified_funds=True)
    assert r_dal1.verdict == "fail"
    assert "RealAuction" in r_dal1.reason

    r_dal2 = gate_legal_eligibility(cpr_dal, has_certified_funds=True,
                                    has_realauction_registration=True)
    assert r_dal2.verdict == "pass"

    # H5: Grayson requirements are TBD -> HITL for attorney verification,
    # NEVER auto-pass, no matter what flags are claimed.
    cpr_gray = CPR(parcel_id="test4g", county="grayson", deal_type="DT-AUCTION")
    r_gray = gate_legal_eligibility(cpr_gray, has_written_statement=True,
                                    has_certified_funds=True,
                                    has_realauction_registration=True)
    assert r_gray.verdict == "hitl"
    assert "TBD-attorney-verification" in r_gray.reason
    print("compliance.py: H5 — Dallas RealAuction required; Grayson -> HITL (TBD-attorney-verification)")

    # Contract: lien_lookup_status=error -> HITL regardless of stack contents
    cpr_err = CPR(parcel_id="test_err", county="collin", deal_type="DT-AUCTION")
    assert cpr_err.lien_lookup_status == "not_run"
    cpr_err.add_lien(LienRecord(lien_type="mortgage", amount=300_000, priority=1,
                                survives_foreclosure=False), "clerk")  # would pass alone
    cpr_err.lien_lookup_status = "error"
    r_err = gate_legal_lien(cpr_err)
    assert r_err.verdict == "hitl"
    assert "lien-lookup-FAILED" in r_err.reason
    try:
        cpr_err.lien_lookup_status = "garbage"
        raise AssertionError("expected ValueError for invalid lien_lookup_status")
    except ValueError:
        pass
    print("compliance.py: S7 — errored lien lookup forces HITL (lien-lookup-FAILED)")

    # GATE-LEGAL-REDEMPTION
    cpr5 = CPR(parcel_id="test5", county="dallas", deal_type="DT-TAXDEED")
    cpr5.set_field("homestead", False, "test", 1.0)
    r9 = gate_legal_redemption(cpr5, deed_recording_date="2026-01-01")
    assert r9.verdict == "pass"
    assert "180" in r9.reason

    r10 = gate_legal_redemption(cpr5, making_improvements=True)
    assert r10.verdict == "fail"
    assert "Irreversible" in r10.reason

    # X2: run_all_gates with explicit per-gate kwargs — zero args must work
    cpr6 = CPR(parcel_id="test6", county="collin", deal_type="DT-AUCTION")
    results_default = run_all_gates(cpr6)  # no TypeError on zero args anymore
    assert [g.gate_name for g in results_default] == \
        ["GATE-LEGAL-LIEN", "GATE-LEGAL-ELIGIBILITY"]
    assert all(g.verdict in ("pass", "fail", "hitl") for g in results_default)

    # Per-gate kwargs land on exactly their gate
    cpr7 = CPR(parcel_id="test7", county="collin", deal_type="DT-WHOLESALE")
    results_kw = run_all_gates(cpr7, gates_kwargs={
        "GATE-LEGAL-LIEN": {},
        "GATE-LEGAL-ELIGIBILITY": {"has_written_statement": True},
        "GATE-LEGAL-WHOLESALE": {"has_disclosure": False, "action": "assign"},
    })
    by_name = {g.gate_name: g for g in results_kw}
    assert set(by_name) == {"GATE-LEGAL-LIEN", "GATE-LEGAL-ELIGIBILITY",
                            "GATE-LEGAL-WHOLESALE"}
    assert by_name["GATE-LEGAL-ELIGIBILITY"].verdict == "pass"   # statement supplied
    assert by_name["GATE-LEGAL-WHOLESALE"].verdict == "fail"     # disclosure missing
    assert "disclosure" in by_name["GATE-LEGAL-WHOLESALE"].reason.lower()

    # Unknown gate names fail loudly instead of fanning bad kwargs around
    try:
        run_all_gates(cpr6, gates_kwargs={"GATE-LEGAL-TYPO": {}})
        raise AssertionError("expected ValueError for unknown gate name")
    except ValueError:
        pass
    print("compliance.py: X2 — run_all_gates(gates_kwargs=...) explicit per-gate params")

    print("compliance.py: all self-checks passed")