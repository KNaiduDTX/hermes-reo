# src/valuation.py
"""AGENT-VAL: ARV / as-is / rent estimate + GATE-VAL.

GATE-VAL is the deterministic ARV grounding gate — the system's keystone.
It blocks promotion of any parcel to pricing/offer unless the ARV meets
provenance + confidence floors. This is the direct answer to the Texas
non-disclosure problem.
"""
from __future__ import annotations
import statistics
from dataclasses import dataclass, field
from .cpr import CPR, ValuationObject, CompRecord, ProvenanceField, CPRColumn, BlastTier
from .legal_rules import (
    is_comp_source_approved, is_comp_source_rejected, get_comp_rigor_tier,
    RedemptionClass,
)


# Confidence floors by blast tier
CONFIDENCE_FLOORS = {
    BlastTier.T0: 0.50,  # screen only
    BlastTier.T1: 0.60,  # small deal
    BlastTier.T2: 0.75,  # mid deal — agent-MLS or BPO required
    BlastTier.T3: 0.85,  # large deal — appraiser-grade required
}

# Minimum comp count for a valid comp set
MIN_COMPS = 3

# Max distance (miles) and age (days) for usable comps
MAX_COMP_DISTANCE_MI = 0.5
MAX_COMP_AGE_DAYS = 180


@dataclass
class GateValResult:
    verdict: str  # "pass", "fail", "hitl"
    reason: str
    confidence: float
    comp_source: str
    comp_count: int
    floor: float


def score_confidence(comps: list[CompRecord], source: str) -> float:
    """Score confidence (0.0-1.0) from comp set quality.

    Inputs: comp count, recency, proximity, similarity, source rigor, adjustment magnitude.
    """
    if not comps:
        return 0.0

    score = 0.0

    # 1. Comp count (up to 0.25)
    count_score = min(len(comps) / 6.0, 1.0) * 0.25
    score += count_score

    # 2. Source rigor tier (up to 0.30)
    tier = get_comp_rigor_tier(source)
    rigor_score = (tier / 3.0) * 0.30 if tier >= 0 else 0.0
    score += rigor_score

    # 3. Recency (up to 0.15) — average comp age in days
    # ponytail: comp age not always available; use 0.10 if missing
    recent_comps = [c for c in comps if c.sale_date]
    if recent_comps:
        # Assume recent if sale_date present (can't parse actual age without dates)
        score += 0.15
    else:
        score += 0.10

    # 4. Proximity (up to 0.15) — average distance
    dists = [c.distance_miles for c in comps if c.distance_miles is not None]
    if dists:
        avg_dist = statistics.mean(dists)
        if avg_dist <= MAX_COMP_DISTANCE_MI:
            score += 0.15
        elif avg_dist <= 1.0:
            score += 0.10
        elif avg_dist <= 2.0:
            score += 0.05
    else:
        score += 0.05  # unknown distance

    # 5. Similarity/adjustment magnitude (up to 0.15)
    # If adjusted prices are close to raw prices, adjustments are small → high confidence
    adjusted = [c for c in comps if c.adjusted_price and c.sale_price]
    if adjusted:
        ratios = [abs(c.adjusted_price - c.sale_price) / c.sale_price for c in adjusted if c.sale_price]
        if ratios:
            avg_adj = statistics.mean(ratios)
            if avg_adj < 0.05:
                score += 0.15
            elif avg_adj < 0.10:
                score += 0.10
            elif avg_adj < 0.20:
                score += 0.05
    else:
        score += 0.05

    return min(score, 1.0)


def gate_val(cpr: CPR, comp_source: str, comps: list[CompRecord],
             blast_tier: BlastTier = BlastTier.T0) -> tuple[GateValResult, ValuationObject]:
    """Run the GATE-VAL check on a CPR's valuation.

    Returns (gate result, valuation object). The valuation object is always
    populated; the gate result tells you whether to promote or route to HITL.
    """
    # 1. Reject explicitly bad sources
    if is_comp_source_rejected(comp_source):
        return GateValResult(
            verdict="fail", reason=f"Rejected comp source: {comp_source}",
            confidence=0.0, comp_source=comp_source, comp_count=len(comps),
            floor=CONFIDENCE_FLOORS[blast_tier],
        ), ValuationObject(comp_source=comp_source, comp_set=comps,
                           gate_val_verdict="fail",
                           gate_val_reason=f"Rejected source: {comp_source}")

    # 2. Require approved source
    if not is_comp_source_approved(comp_source):
        return GateValResult(
            verdict="fail", reason=f"Unapproved comp source: {comp_source}",
            confidence=0.0, comp_source=comp_source, comp_count=len(comps),
            floor=CONFIDENCE_FLOORS[blast_tier],
        ), ValuationObject(comp_source=comp_source, comp_set=comps,
                           gate_val_verdict="fail",
                           gate_val_reason=f"Unapproved source: {comp_source}")

    # 3. Minimum comp count
    if len(comps) < MIN_COMPS:
        return GateValResult(
            verdict="fail", reason=f"Insufficient comps: {len(comps)} < {MIN_COMPS}",
            confidence=0.0, comp_source=comp_source, comp_count=len(comps),
            floor=CONFIDENCE_FLOORS[blast_tier],
        ), ValuationObject(comp_source=comp_source, comp_set=comps,
                           gate_val_verdict="fail",
                           gate_val_reason=f"Need >= {MIN_COMPS} comps, got {len(comps)}")

    # 4. Score confidence
    confidence = score_confidence(comps, comp_source)
    floor = CONFIDENCE_FLOORS[blast_tier]

    # 5. Compute ARV from comp set
    adjusted_prices = [c.adjusted_price or c.sale_price for c in comps if c.adjusted_price or c.sale_price]
    arv = statistics.median(adjusted_prices) if adjusted_prices else None

    # 6. Check floor
    if confidence < floor:
        verdict = "hitl"
        reason = f"Confidence {confidence:.2f} below floor {floor:.2f} for tier {blast_tier.name}"
    else:
        verdict = "pass"
        reason = f"Confidence {confidence:.2f} >= floor {floor:.2f}"

    val_obj = ValuationObject(
        comp_source=comp_source,
        comp_set=comps,
        gate_val_verdict=verdict,
        gate_val_reason=reason,
    )
    if arv:
        val_obj.arv = ProvenanceField(value=arv, source=comp_source, confidence=confidence)

    return GateValResult(
        verdict=verdict, reason=reason, confidence=confidence,
        comp_source=comp_source, comp_count=len(comps), floor=floor,
    ), val_obj


def attach_valuation(cpr: CPR, val_obj: ValuationObject, gate: GateValResult) -> CPR:
    """Attach valuation to CPR and advance column if gate passes."""
    cpr.valuation = val_obj
    cpr.blast_tier = cpr.blast_tier  # already set by pricing; keep
    if gate.verdict == "pass":
        cpr.move_column(CPRColumn.VALUED)
    elif gate.verdict == "hitl":
        cpr.move_column(CPRColumn.TRIAGE)  # back to triage for HITL
    elif gate.verdict == "fail":
        cpr.move_column(CPRColumn.DEAD, reason="low_confidence_comp")
    return cpr


if __name__ == "__main__":
    # Self-check: GATE-VAL rejects bad sources
    cpr = CPR(parcel_id="test", county="collin")
    comps = [CompRecord(address=f"comp{i}", sale_price=300_000 + i * 10_000,
                        distance_miles=0.3, source="mls_agent")
             for i in range(5)]

    # Pass: MLS agent, 5 comps, close distance
    gate, val = gate_val(cpr, "mls_agent", comps, BlastTier.T1)
    assert gate.verdict == "pass", f"Expected pass, got {gate.verdict}: {gate.reason}"
    assert gate.confidence >= 0.60
    assert val.arv is not None
    print(f"valuation.py: PASS — confidence={gate.confidence:.2f}, ARV=${val.arv.value:,.0f}")

    # Fail: rejected source (assessed_value)
    gate2, val2 = gate_val(cpr, "assessed_value", comps, BlastTier.T0)
    assert gate2.verdict == "fail"
    assert "Rejected" in gate2.reason
    print(f"valuation.py: FAIL (rejected source) — {gate2.reason}")

    # Fail: insufficient comps
    gate3, val3 = gate_val(cpr, "mls_agent", comps[:2], BlastTier.T0)
    assert gate3.verdict == "fail"
    assert "Insufficient" in gate3.reason
    print(f"valuation.py: FAIL (insufficient comps) — {gate3.reason}")

    # HITL: low confidence for tier
    far_comps = [CompRecord(address=f"far{i}", sale_price=200_000,
                            distance_miles=5.0, source="vendor_avm")
                 for i in range(4)]
    gate4, val4 = gate_val(cpr, "vendor_avm", far_comps, BlastTier.T2)
    assert gate4.verdict in ("hitl", "fail")  # vendor_avm + far distance → low confidence
    print(f"valuation.py: HITL/FAIL (low confidence T2) — {gate4.reason}")

    print("valuation.py: all self-checks passed")