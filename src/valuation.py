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
from datetime import date, datetime, timezone
from .cpr import CPR, ValuationObject, CompRecord, ProvenanceField, CPRColumn, BlastTier
from .dates import parse_date_any
from .legal_rules import (
    is_comp_source_approved, is_comp_source_rejected, get_comp_rigor_tier,
    RedemptionClass,
)
from . import policy


# Confidence floors by blast tier — wired to src.policy scalars
# (single source of truth; values unchanged from the audited defaults)
CONFIDENCE_FLOORS = {
    BlastTier.T0: policy.CONFIDENCE_FLOOR_T0,  # screen only
    BlastTier.T1: policy.CONFIDENCE_FLOOR_T1,  # small deal
    BlastTier.T2: policy.CONFIDENCE_FLOOR_T2,  # mid deal — agent-MLS or BPO required
    BlastTier.T3: policy.CONFIDENCE_FLOOR_T3,  # large deal — appraiser-grade required
}

# Minimum comp count for a valid comp set (applied AFTER filtering)
MIN_COMPS = policy.MIN_COMPS

# Max distance (miles) and age (days) for usable comps. Both are enforced:
# failing comps are dropped before scoring and before the ARV median (H2).
MAX_COMP_DISTANCE_MI = policy.MAX_COMP_DISTANCE_MI
MAX_COMP_AGE_DAYS = policy.MAX_COMP_AGE_DAYS

# Recency credit bands relative to MAX_COMP_AGE_DAYS (audit H2):
RECENCY_FULL_POINTS_DAYS = 90   # age <= this: full recency credit
_RECENCY_PARTIAL_CREDIT = 0.5   # age <= MAX_COMP_AGE_DAYS: half credit
_RECENCY_UNKNOWN_CREDIT = 0.25  # unparsable/missing sale date: small fixed credit


@dataclass
class GateValResult:
    verdict: str  # "pass", "fail", "hitl"
    reason: str
    confidence: float
    comp_source: str
    comp_count: int
    floor: float


def _comp_age_days(comp: CompRecord, as_of: date) -> int | None:
    """Parsed age of a comp's sale in days, or None when unverifiable.

    Age comes only from src.dates.parse_date_any — never from string shape.
    A future-dated close is treated as unverifiable, not as fresh.
    """
    parsed = parse_date_any(comp.sale_date)
    if parsed is None:
        return None
    age = (as_of - parsed).days
    return age if age >= 0 else None


def score_confidence(comps: list[CompRecord], source: str,
                     as_of: date | None = None) -> float:
    """Score confidence (0.0-1.0) from comp set quality.

    Inputs: comp count, recency, proximity, similarity, source rigor, adjustment magnitude.
    """
    if not comps:
        return 0.0

    if as_of is None:
        as_of = datetime.now(timezone.utc).date()

    score = 0.0

    # 1. Comp count (up to 0.25)
    count_score = min(len(comps) / 6.0, 1.0) * 0.25
    score += count_score

    # 2. Source rigor tier (up to 0.30)
    tier = get_comp_rigor_tier(source)
    rigor_score = (tier / 3.0) * 0.30 if tier >= 0 else 0.0
    score += rigor_score

    # 3. Recency (up to 0.15) — scored from PARSED comp age vs MAX_COMP_AGE_DAYS.
    # A non-empty sale_date string no longer earns full points (audit H2):
    #   <=90 days full credit, <=180 partial, older zero, unparsable small fixed credit.
    credits = []
    for c in comps:
        age = _comp_age_days(c, as_of)
        if age is None:
            credits.append(_RECENCY_UNKNOWN_CREDIT)
        elif age <= RECENCY_FULL_POINTS_DAYS:
            credits.append(1.0)
        elif age <= MAX_COMP_AGE_DAYS:
            credits.append(_RECENCY_PARTIAL_CREDIT)
        else:
            credits.append(0.0)
    score += statistics.mean(credits) * 0.15

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


def _filter_usable_comps(comps: list[CompRecord], as_of: date):
    """Drop comps outside MAX_COMP_DISTANCE_MI or older than MAX_COMP_AGE_DAYS.

    Returns (usable, dropped_distance, dropped_stale). Comps with unknown
    distance or unparsable dates are kept — they cannot be proven bad — and
    are penalized in scoring instead (loud data beats silent exclusion).
    """
    usable, dropped_distance, dropped_stale = [], 0, 0
    for c in comps:
        dist = c.distance_miles
        if dist is not None and dist > MAX_COMP_DISTANCE_MI:
            dropped_distance += 1
            continue
        parsed = parse_date_any(c.sale_date)
        if parsed is not None and (as_of - parsed).days > MAX_COMP_AGE_DAYS:
            dropped_stale += 1
            continue
        usable.append(c)
    return usable, dropped_distance, dropped_stale


def gate_val(cpr: CPR, comp_source: str, comps: list[CompRecord],
             blast_tier: BlastTier = BlastTier.T0) -> tuple[GateValResult, ValuationObject]:
    """Run the GATE-VAL check on a CPR's valuation.

    Returns (gate result, valuation object). The valuation object is always
    populated; the gate result tells you whether to promote or route to HITL.

    H2 semantics: stale/far comps are filtered BEFORE scoring and BEFORE the
    ARV median; a filtered set below MIN_COMPS fails; an empty priced set is
    verdict "hitl" / reason "no-priced-comps" — GATE-VAL never passes without
    a grounded ARV.
    """
    today = datetime.now(timezone.utc).date()

    def _result(verdict: str, reason: str, confidence: float, used: list[CompRecord]):
        val_obj = ValuationObject(
            comp_source=comp_source,
            comp_set=used,
            gate_val_verdict=verdict,
            gate_val_reason=reason,
        )
        return GateValResult(
            verdict=verdict, reason=reason, confidence=confidence,
            comp_source=comp_source, comp_count=len(used), floor=CONFIDENCE_FLOORS[blast_tier],
        ), val_obj

    # 1. Reject explicitly bad sources
    if is_comp_source_rejected(comp_source):
        return _result("fail", f"Rejected comp source: {comp_source}", 0.0, list(comps))

    # 2. Require approved source
    if not is_comp_source_approved(comp_source):
        return _result("fail", f"Unapproved comp source: {comp_source}", 0.0, list(comps))

    # 3. Filter unusable comps BEFORE scoring AND before the ARV median (H2)
    usable, dropped_distance, dropped_stale = _filter_usable_comps(comps, today)
    dropped = dropped_distance + dropped_stale
    drop_note = ""
    if dropped:
        drop_note = (
            f"; filtered {dropped} comp(s) "
            f"({dropped_distance} beyond {MAX_COMP_DISTANCE_MI} mi, "
            f"{dropped_stale} older than {MAX_COMP_AGE_DAYS} d)"
        )

    # 4. Minimum comp count applies to the FILTERED set
    if len(usable) < MIN_COMPS:
        return _result(
            "fail",
            f"Insufficient usable comps after filtering: {len(usable)} < {MIN_COMPS}{drop_note}",
            0.0, usable,
        )

    # 5. Score confidence on the usable set only
    confidence = score_confidence(usable, comp_source)
    floor = CONFIDENCE_FLOORS[blast_tier]

    # 6. Compute ARV from the usable comp set
    priced = [(c.adjusted_price or c.sale_price) for c in usable
              if c.adjusted_price or c.sale_price]
    arv = statistics.median(priced) if priced else None

    # Mixed adjusted/raw sets blend unadjusted values into the median — warn loudly
    has_adjusted = any(c.adjusted_price for c in usable)
    has_raw_only = any(not c.adjusted_price and c.sale_price for c in usable)
    mixed_note = ("; WARNING: mixed adjusted/raw comp set — ARV median blends "
                  "unadjusted sale prices with adjusted values"
                  ) if (has_adjusted and has_raw_only) else ""

    # 7. Verdict — never pass without a grounded ARV
    if arv is None:
        verdict, reason = "hitl", "no-priced-comps — no usable priced comps; ARV cannot be grounded"
    elif confidence < floor:
        verdict = "hitl"
        reason = f"Confidence {confidence:.2f} below floor {floor:.2f} for tier {blast_tier.name}"
    else:
        verdict = "pass"
        reason = f"Confidence {confidence:.2f} >= floor {floor:.2f}"

    reason = reason + drop_note + mixed_note

    val_obj = ValuationObject(
        comp_source=comp_source,
        comp_set=usable,
        gate_val_verdict=verdict,
        gate_val_reason=reason,
    )
    if arv is not None:
        val_obj.arv = ProvenanceField(value=arv, source=comp_source, confidence=confidence)

    return GateValResult(
        verdict=verdict, reason=reason, confidence=confidence,
        comp_source=comp_source, comp_count=len(usable), floor=floor,
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
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    cpr = CPR(parcel_id="test", county="collin")

    def _dated(days_ago: int) -> str:
        return (today - timedelta(days=days_ago)).isoformat()

    # Pass: MLS agent, 5 fresh (<90d), close, adjusted comps
    comps = [CompRecord(address=f"comp{i}", sale_price=300_000 + i * 10_000,
                        distance_miles=0.3, source="mls_agent",
                        sale_date=_dated(20 + i), adjusted_price=305_000 + i * 10_000)
             for i in range(5)]
    gate, val = gate_val(cpr, "mls_agent", comps, BlastTier.T1)
    assert gate.verdict == "pass", f"Expected pass, got {gate.verdict}: {gate.reason}"
    assert gate.confidence >= 0.60
    assert val.arv is not None
    print(f"valuation.py: PASS — confidence={gate.confidence:.2f}, ARV=${val.arv.value:,.0f}")

    # Recency is parsed, not faked: same comps dated >180d ago are ALL filtered
    stale = [CompRecord(address=f"stale{i}", sale_price=300_000 + i * 10_000,
                        distance_miles=0.3, source="mls_agent",
                        sale_date=_dated(400), adjusted_price=300_000)
             for i in range(5)]
    gate_s, val_s = gate_val(cpr, "mls_agent", stale, BlastTier.T1)
    assert gate_s.verdict == "fail", gate_s.reason
    assert "filtered 5" in gate_s.reason and "older than 180" in gate_s.reason
    print(f"valuation.py: FAIL (stale comps filtered pre-scoring/ARV) — {gate_s.reason}")

    # Fresh but far comps are also filtered before the median
    far = [CompRecord(address=f"far{i}", sale_price=200_000, distance_miles=5.0,
                      source="vendor_avm", sale_date=_dated(30)) for i in range(4)]
    gate_f, val_f = gate_val(cpr, "vendor_avm", far, BlastTier.T2)
    assert gate_f.verdict == "fail", gate_f.reason
    assert "filtered 4" in gate_f.reason
    print(f"valuation.py: FAIL (far comps filtered) — {gate_f.reason}")

    # Filtered set below MIN_COMPS fails even if leftovers look good
    mixed_age = ([CompRecord(address=f"fresh{i}", sale_price=400_000, distance_miles=0.2,
                             source="mls_agent", sale_date=_dated(10)) for i in range(2)] +
                 [CompRecord(address="old1", sale_price=900_000, distance_miles=0.2,
                             source="mls_agent", sale_date=_dated(365))])
    gate_m, _ = gate_val(cpr, "mls_agent", mixed_age, BlastTier.T0)
    assert gate_m.verdict == "fail", gate_m.reason
    assert "Insufficient usable comps after filtering: 2 < 3" in gate_m.reason
    print(f"valuation.py: FAIL (filtered set below MIN_COMPS) — {gate_m.reason}")

    # HITL: fresh/close comps that carry NO price at all — never pass with arv None
    unpriced = [CompRecord(address=f"np{i}", sale_price=None, distance_miles=0.2,
                           source="mls_agent", sale_date=_dated(15)) for i in range(3)]
    gate_u, val_u = gate_val(cpr, "mls_agent", unpriced, BlastTier.T0)
    assert gate_u.verdict == "hitl" and "no-priced-comps" in gate_u.reason
    assert val_u.arv is None
    print(f"valuation.py: HITL (no-priced-comps) — {gate_u.reason}")

    # Mixed adjusted/raw set passes only with a loud warning in the reason
    mixed_src = ([CompRecord(address=f"adj{i}", sale_price=400_000, adjusted_price=410_000,
                             distance_miles=0.2, source="mls_agent", sale_date=_dated(15))
                  for i in range(3)] +
                 [CompRecord(address="rawonly", sale_price=420_000, distance_miles=0.2,
                             source="mls_agent", sale_date=_dated(15))])
    gate_x, val_x = gate_val(cpr, "mls_agent", mixed_src, BlastTier.T1)
    assert "WARNING: mixed adjusted/raw comp set" in gate_x.reason or \
           "WARNING: mixed adjusted/raw comp set" in val_x.gate_val_reason
    print("valuation.py: WARNING (mixed adjusted/raw noted) — OK")

    # Fail: rejected source (assessed_value)
    gate2, val2 = gate_val(cpr, "assessed_value", comps, BlastTier.T0)
    assert gate2.verdict == "fail"
    assert "Rejected" in gate2.reason
    print(f"valuation.py: FAIL (rejected source) — {gate2.reason}")

    # Fail: insufficient comps
    gate3, val3 = gate_val(cpr, "mls_agent", comps[:2], BlastTier.T0)
    assert gate3.verdict == "fail"
    assert "Insufficient usable comps after filtering" in gate3.reason
    print(f"valuation.py: FAIL (insufficient comps) — {gate3.reason}")

    # HITL: fresh/close vendor_avm comps still score low for a T2 floor
    avm = [CompRecord(address=f"avm{i}", sale_price=200_000, distance_miles=0.3,
                      source="vendor_avm", sale_date=_dated(30)) for i in range(4)]
    gate4, val4 = gate_val(cpr, "vendor_avm", avm, BlastTier.T2)
    assert gate4.verdict == "hitl", gate4.reason
    assert "below floor" in gate4.reason
    print(f"valuation.py: HITL (low confidence T2) — {gate4.reason}")

    print("valuation.py: all self-checks passed")