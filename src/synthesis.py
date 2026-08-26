# src/synthesis.py
"""AGENT-SYN: portfolio rollups, reporting, weekly deal-flow digest.

Ported from lucid-noether analyze_foreclosures.py + generate_ocr_report.py.
These are portfolio-level analytics that run across all CPRs in the pipeline.
"""
from __future__ import annotations
import json
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from .cpr import CPR, CPRColumn
# X7: reuse AGENT-ENR's ISD extraction so portfolio rollups and enrichment can
# never disagree about how an ISD is derived from taxing-entity strings.
# TD8: raw dict rows are converted to CPRs at portfolio_stats entry through
# enrich_from_flat_record, so exactly one record type flows through rollups.
from .enrichment import enrich_from_flat_record, extract_isd


# --- Known DFW cities (ported from lucid-noether) ---

KNOWN_CITIES = sorted([
    "Frisco", "Plano", "McKinney", "Allen", "Wylie", "Princeton", "Prosper",
    "Celina", "Melissa", "Anna", "Royse City", "Farmersville", "Josephine",
    "Lavon", "Murphy", "Fairview", "Lucas", "Parker", "St Paul", "St. Paul",
    "Dallas", "Richardson", "Blue Ridge", "Nevada", "Sachse", "Carrollton",
    "Garland", "Weston", "Addison", "Balch Springs", "Balch", "Coppell",
    "DeSoto", "Duncanville", "Farmers Branch", "Glenn Heights", "Grand Prairie",
    "Hutchins", "Irving", "Lancaster", "Mesquite", "Rowlett", "Seagoville",
    "Wilmer", "Cedar Hill",
], key=len, reverse=True)


def get_city(addr: str) -> str:
    """Extract city from address — ported from lucid-noether, uses comma pattern."""
    if not addr or addr == "Unknown":
        return "Unknown"
    # Try comma-separated: "123 Main St, Dallas, TX 75201"
    parts = [p.strip() for p in addr.split(",")]
    if len(parts) >= 3:
        city = parts[-2]
        if city and "TX" not in city.upper():
            return city
    # Fallback: known cities list
    addr_clean = " ".join(addr.split()).replace(",", " ")
    for c in KNOWN_CITIES:
        if re.search(r'\b' + re.escape(c) + r'\b', addr_clean, re.IGNORECASE):
            return c
    return "Unknown"


def _stats_rows(cprs: list[Any]) -> list[CPR]:
    """TD8 entry normalization for portfolio rollups: exactly one record type
    flows through. CPR objects pass through untouched; raw dict rows are
    converted at entry via enrich_from_flat_record (the flat
    current_deals.json schema). The legacy nested-lucid dict branch is
    retired — nested records carry no flat keys, so a caller holding them
    enriches first (enrich_from_raw_record). Anything else fails loudly.
    """
    rows: list[CPR] = []
    for row in cprs:
        if isinstance(row, CPR):
            rows.append(row)
        elif isinstance(row, dict):
            rows.append(enrich_from_flat_record(row))
        else:
            raise TypeError(
                "portfolio_stats expects CPR objects or flat dict rows, "
                f"got {type(row).__name__}")
    return rows


def _record_isd(cpr: CPR) -> str:
    """X7: the receipted `isd` field wins when populated; otherwise derive
    from the taxing-entity strings (same extraction AGENT-ENR uses at
    ingestion)."""
    isd = cpr.get("isd")
    if not isd or isd == "Unknown":
        isd = extract_isd(cpr.get("taxing_entities") or "")
    return isd


def _value_distribution(values: list[float]) -> dict[str, Any]:
    """total/mean/median/min/max summary over collected values (0-filled)."""
    return {
        "total": sum(values),
        "mean": statistics.mean(values) if values else 0,
        "median": statistics.median(values) if values else 0,
        "min": min(values) if values else 0,
        "max": max(values) if values else 0,
    }


def portfolio_stats(cprs: list[Any]) -> dict[str, Any]:
    """Compute portfolio-level statistics across all CPRs.

    Ported from lucid-noether analyze_foreclosures.py.
    Returns a dict with city/ISD/valuation/price-per-sqft distributions.

    TD8: raw dict rows are normalized at entry (_stats_rows), so exactly one
    record type flows through the accumulation loop.

    TD1: per-record reads go through CPR.get(), the provenance-unwrapping
    accessor over declared fields, instead of pv_value/hasattr walls.
    """
    cpr_rows = _stats_rows(cprs)

    cities = []
    isds = []
    property_types = []
    years_built = []
    appraised = []
    price_per_sqft = []
    corporate_owners = 0
    individual_owners = 0
    debts = []
    lenders = []
    trustees = []

    corp_pattern = re.compile(
        r'\b(LLC|L\.P\.|LP|TRUST|TRUSTEE|CORP|CORPORATION|INC|INCORPORATED|'
        r'PROPERTIES|HOLDINGS|PARTNERS|DEVELOPMENT|ESTATE|ASSOCIATES|FUND|'
        r'CAPITAL|MANAGEMENT|GROUP|BANK|HOMEOWNERS ASSOCIATION|HOA|'
        r'COMMUNITY ASSOCIATION|LTD|LIMITED|LLP|SERIES|VENTURES|'
        r'INVESTMENTS|CO|COMPANY)\b', re.IGNORECASE
    )

    for cpr in cpr_rows:
        owner_name = cpr.get("owner_name", "Unknown")
        val = cpr.get("cad_appraised_value", 0.0)
        sqft = cpr.get("improvement_sqft", 0)
        yr = cpr.get("year_built", 0)
        # Debt from liens; lender/trustee from the receipted fields.
        debt = sum(l.amount or 0 for l in cpr.liens if l.lien_type == "mortgage")
        lender = cpr.liens[0].lender if cpr.liens else "Unknown"
        trustee = cpr.get("substitute_trustee") or "Unknown"
        trustees.append(trustee)

        cities.append(get_city(cpr.situs_address))
        isds.append(_record_isd(cpr))
        property_types.append(cpr.get("property_type", "Unknown"))
        if yr and yr > 0:
            years_built.append(yr)
        if val and val > 0:
            appraised.append(val)
        if val and val > 0 and sqft and sqft > 0:
            price_per_sqft.append(val / sqft)
        if corp_pattern.search(str(owner_name)):
            corporate_owners += 1
        else:
            individual_owners += 1
        if debt and debt > 0:
            debts.append(debt)
        lenders.append(lender)

    total = len(cprs)
    result = {
        "total_properties": total,
        "corporate_owners": corporate_owners,
        "individual_owners": individual_owners,
        "cities": dict(Counter(cities).most_common()),
        "isds": dict(Counter(isds).most_common()),
        "property_types": dict(Counter(property_types).most_common()),
        "appraised": _value_distribution(appraised),
        "price_per_sqft": {
            "mean": statistics.mean(price_per_sqft) if price_per_sqft else 0,
            "median": statistics.median(price_per_sqft) if price_per_sqft else 0,
        },
        "debt": _value_distribution(debts),
        "lender_concentration": dict(Counter(lenders).most_common(10)),
        "trustee_distribution": dict(Counter(trustees).most_common(10)) if trustees else {},
    }

    if years_built:
        result["year_built"] = {
            "mean": statistics.mean(years_built),
            "min": min(years_built),
            "max": max(years_built),
        }

    return result


def render_stats_report(stats: dict[str, Any]) -> str:
    """Render portfolio stats as Markdown — ported from lucid-noether stats_summary.md format."""
    lines = []
    lines.append("# PORTFOLIO STATISTICAL SUMMARY")
    lines.append(f"Total properties: {stats['total_properties']}")
    lines.append(f"Corporate owners: {stats['corporate_owners']} ({stats['corporate_owners']/max(stats['total_properties'],1)*100:.1f}%)")
    lines.append(f"Individual owners: {stats['individual_owners']} ({stats['individual_owners']/max(stats['total_properties'],1)*100:.1f}%)")

    lines.append("\n## CITIES DISTRIBUTION")
    for city, count in stats["cities"].items():
        pct = count / max(stats["total_properties"], 1) * 100
        lines.append(f"- **{city}**: {count} ({pct:.1f}%)")

    lines.append("\n## ISDS DISTRIBUTION")
    for isd, count in stats["isds"].items():
        pct = count / max(stats["total_properties"], 1) * 100
        lines.append(f"- **{isd}**: {count} ({pct:.1f}%)")

    lines.append("\n## VALUATION")
    v = stats["appraised"]
    lines.append(f"- **Total Portfolio Value**: ${v['total']:,.2f}")
    lines.append(f"- **Mean**: ${v['mean']:,.2f}")
    lines.append(f"- **Median**: ${v['median']:,.2f}")
    lines.append(f"- **Min**: ${v['min']:,.2f}")
    lines.append(f"- **Max**: ${v['max']:,.2f}")

    pps = stats["price_per_sqft"]
    if pps["mean"] > 0:
        lines.append(f"\n## PRICE PER SQFT")
        lines.append(f"- **Mean**: ${pps['mean']:,.2f}/sqft")
        lines.append(f"- **Median**: ${pps['median']:,.2f}/sqft")

    d = stats["debt"]
    if d["total"] > 0:
        lines.append(f"\n## DEBT EXPOSURE")
        lines.append(f"- **Total Portfolio Debt**: ${d['total']:,.2f}")
        lines.append(f"- **Mean Default Balance**: ${d['mean']:,.2f}")
        lines.append(f"- **Median Default Balance**: ${d['median']:,.2f}")
        lines.append(f"- **Min**: ${d['min']:,.2f}")
        lines.append(f"- **Max**: ${d['max']:,.2f}")

    if stats.get("lender_concentration"):
        lines.append(f"\n## FORECLOSING LENDER CONCENTRATION")
        lines.append("| Lender | Filings |")
        lines.append("|--------|---------|")
        for lender, count in stats["lender_concentration"].items():
            lines.append(f"| {lender} | {count} |")

    return "\n".join(lines)


def weekly_digest(cprs: list[Any], dead_reasons: Optional[dict[str, int]] = None) -> str:
    """Generate a weekly deal-flow digest.

    Args:
        cprs: List of CPR objects.
        dead_reasons: Dict of {reason_code: count} for funnel analytics.
    """
    lines = []
    lines.append("# WEEKLY DEAL-FLOW DIGEST")
    lines.append(f"\nGenerated: {datetime.now(timezone.utc).isoformat()}")

    # Active deals by column
    column_counts = {}
    for cpr in cprs:
        col = cpr.column.value if hasattr(cpr, 'column') else "Unknown"
        column_counts[col] = column_counts.get(col, 0) + 1

    lines.append("\n## ACTIVE DEALS BY STAGE")
    for col in CPRColumn:
        count = column_counts.get(col.value, 0)
        if count > 0:
            lines.append(f"- **{col.value}**: {count}")

    # Dead deals funnel
    if dead_reasons:
        lines.append("\n## DEAD DEAL FUNNEL")
        for reason, count in sorted(dead_reasons.items(), key=lambda x: -x[1]):
            lines.append(f"- **{reason}**: {count}")

    # Portfolio stats
    stats = portfolio_stats(cprs)
    lines.append(f"\n## PORTFOLIO SUMMARY")
    lines.append(f"- Total properties tracked: {stats['total_properties']}")
    v = stats["appraised"]
    if v["total"] > 0:
        lines.append(f"- Total appraised value: ${v['total']:,.0f}")
    d = stats["debt"]
    if d["total"] > 0:
        lines.append(f"- Total debt exposure: ${d['total']:,.0f}")

    return "\n".join(lines)


if __name__ == "__main__":
    # Self-check: stats over FLAT dict rows (TD8 entry conversion via
    # enrich_from_flat_record — same schema the /api/stats endpoint feeds in),
    # then real CPR objects for the X7 ISD/trustee behavior.
    fixture = [
        {"address": "10022 Plainsman Ln, Frisco, TX 75035",
         "county": "collin",
         "owner": "Islam Mazhar & Ambreen",
         "property_type": "Residential Single Family (A1)",
         "sqft": 4332, "year_built": 2004,
         "cad_value": 730673.00,
         "mortgage_debt": 350000, "lender": "Wells Fargo"},
        {"address": "15806 Breedlove Pl, Addison, TX 75001",
         "county": "dallas",
         "owner": "KERRY P. GRAY",
         "property_type": "Residential Single Family (A1)",
         "cad_value": 387000.00,
         "mortgage_debt": 309600, "lender": "CTX Mortgage"},
    ]

    stats = portfolio_stats(fixture)
    assert stats["total_properties"] == 2
    assert "Frisco" in stats["cities"]
    assert "Addison" in stats["cities"]
    assert stats["appraised"]["total"] == 1_117_673.0
    assert stats["debt"]["total"] == 659_600.0
    assert "Wells Fargo" in stats["lender_concentration"]
    assert "CTX Mortgage" in stats["lender_concentration"]
    # Neither flat row carries isd/taxing entities → honest "Unknown" for both.
    assert stats["isds"] == {"Unknown": 2}, stats["isds"]

    report = render_stats_report(stats)
    assert "PORTFOLIO STATISTICAL SUMMARY" in report
    assert "Frisco" in report
    assert "DEBT EXPOSURE" in report

    # TD8: mixed input lists normalize to a single internal type; garbage
    # input fails loudly instead of silently rolling into an empty branch.
    mixed = portfolio_stats(fixture + [CPR(parcel_id="SYN0", county="collin",
                                           situs_address="9 Elm St, Plano, TX 75023")])
    assert mixed["total_properties"] == 3
    assert mixed["cities"].get("Plano") == 1
    try:
        portfolio_stats([42])  # type: ignore[list-item]
        raise AssertionError("expected TypeError for non-CPR/non-dict row")
    except TypeError:
        pass

    # TD8: the shared leverage kernel (src/metrics.py) — the equity/LTV trio
    # analyze_deal stamps on every row.
    from .metrics import compute_leverage_metrics

    lev = compute_leverage_metrics(300_000.0, 150_000.0)
    assert lev == {"equity_estimate": 150000.0, "equity_pct": 50.0,
                   "ltv_pct": 50.0}, lev
    for cad, debt in ((None, 100.0), (300_000.0, 0), (300_000.0, None)):
        assert compute_leverage_metrics(cad, debt) == {
            "equity_estimate": None, "equity_pct": None, "ltv_pct": None}

    # X7: real CPR objects — ISD derives from taxing-entity strings when the
    # `isd` field is missing, and the trustee distribution includes enriched
    # records.
    def _cpr_with(taxing: str, isd: str | None = None, trustee: str = "") -> CPR:
        c = CPR(parcel_id="SYN1", county="collin",
                situs_address="101 Sachse Rd, Sachse, TX 75048")
        c.set_field("taxing_entities", taxing, "self_check", 0.9)
        if isd is not None:
            c.set_field("isd", isd, "manual_override", 1.0)
        if trustee:
            c.set_field("substitute_trustee", trustee, "ocr", 0.8)
        return c

    stats_cpr = portfolio_stats([_cpr_with(
        "Collin County (GCN) / City of Sachse (CFR) / Wylie ISD (SFR)",
        trustee="Robert LaMont, Substitute Trustee")])
    assert stats_cpr["isds"].get("Wylie ISD") == 1, \
        f"ISD not derived from taxing entities: {stats_cpr['isds']}"
    assert stats_cpr["trustee_distribution"].get(
        "Robert LaMont, Substitute Trustee") == 1, \
        f"trustee distribution missing CPR record: {stats_cpr['trustee_distribution']}"

    # A populated `isd` provenance field wins over re-derivation.
    stats_isd = portfolio_stats([_cpr_with(
        "Collin County (GCN) / Frisco ISD (SFR)", isd="Manual ISD Override")])
    assert stats_isd["isds"] == {"Manual ISD Override": 1}, stats_isd["isds"]

    # No ISD anywhere in the strings -> honest "Unknown", not a crash or guess.
    stats_none = portfolio_stats([_cpr_with("Dallas County / City of Addison")])
    assert stats_none["isds"] == {"Unknown": 1}, stats_none["isds"]

    print("synthesis.py: TD8 — flat-row entry conversion + leverage kernel verified")
    print("synthesis.py: X7 — CPR ISD derivation + trustee distribution verified")
    print("synthesis.py: all self-checks passed")