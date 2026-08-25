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

from .cpr import CPRColumn, pv_value
# X7: reuse AGENT-ENR's ISD extraction so portfolio rollups and enrichment can
# never disagree about how an ISD is derived from taxing-entity strings.
from .enrichment import extract_isd


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


def _parse_value(val: Any) -> float:
    """Parse a dollar value from a string like '$730,673.00'."""
    if val is None or val == "Unknown":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = re.sub(r'[^\d.]', '', str(val))
    return float(cleaned) if cleaned else 0.0


def portfolio_stats(cprs: list[Any]) -> dict[str, Any]:
    """Compute portfolio-level statistics across all CPRs.

    Ported from lucid-noether analyze_foreclosures.py.
    Returns a dict with city/ISD/valuation/price-per-sqft distributions.
    """
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

    for cpr in cprs:
        # Handle both CPR objects and raw dicts (from lucid-noether JSON)
        if hasattr(cpr, 'situs_address'):
            addr = cpr.situs_address
            city = get_city(addr)
            owner_name = pv_value(cpr.owner_name) if cpr.owner_name else "Unknown"
            val = pv_value(cpr.cad_appraised_value) if cpr.cad_appraised_value else 0.0
            sqft = pv_value(cpr.improvement_sqft) if cpr.improvement_sqft else 0
            yr = pv_value(cpr.year_built) if cpr.year_built else 0
            ptype = pv_value(cpr.property_type) if cpr.property_type else "Unknown"
            # X7 fix: CPR has no `jurisdiction_info` attribute — that field only
            # exists on raw source dicts. ISD comes from the `isd` provenance
            # field when populated, otherwise it is derived from the taxing-
            # entity strings (same extraction AGENT-ENR uses at ingestion).
            taxing = pv_value(getattr(cpr, "taxing_entities", None)) or ""
            isd = pv_value(getattr(cpr, "isd", None))
            if not isd or isd == "Unknown":
                isd = extract_isd(taxing)
            # Debt from liens
            debt = sum(l.amount or 0 for l in cpr.liens if l.lien_type == "mortgage")
            lender = cpr.liens[0].lender if cpr.liens else "Unknown"
            # X7: trustee distribution covers CPR objects too — pull the
            # substitute_trustee provenance field instead of leaving the list
            # empty for every enriched record.
            trustee = pv_value(getattr(cpr, "substitute_trustee", None)) or "Unknown"
            trustees.append(trustee)
        else:
            # Raw dict (lucid-noether format)
            addr = cpr.get("property_address", "")
            city = get_city(addr)
            details = cpr.get("details", {})
            owner_name = details.get("owner_info", {}).get("Name", "Unknown")
            val = _parse_value(details.get("tax_info", {}).get("Appraised Value (2026)", "$0"))
            sqft_str = details.get("improvement_info", {}).get("Improvement Area", "Unknown")
            sqft = _parse_value(sqft_str)
            yr_str = details.get("improvement_info", {}).get("Year Built", "0")
            try:
                yr = int(re.sub(r'[^\d]', '', yr_str)) if yr_str else 0
            except ValueError:
                yr = 0
            ptype = details.get("property_info", {}).get("Type", "Unknown")
            taxing = details.get("jurisdiction_info", {}).get("Taxing Entities", "")
            isd_match = re.search(r'([A-Za-z0-9\s]+ISD)\b', taxing)
            isd = isd_match.group(1).strip() if isd_match else "Unknown"
            ocr = details.get("scanned_notice_ocr_data", {})
            debt = ocr.get("estimated_default_balance", 0.0)
            lender = ocr.get("foreclosing_lender", "Unknown")
            trustee = ocr.get("substitute_trustee", "Unknown")
            trustees.append(trustee)

        cities.append(city)
        isds.append(isd)
        property_types.append(ptype)
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
        "appraised": {
            "total": sum(appraised),
            "mean": statistics.mean(appraised) if appraised else 0,
            "median": statistics.median(appraised) if appraised else 0,
            "min": min(appraised) if appraised else 0,
            "max": max(appraised) if appraised else 0,
        },
        "price_per_sqft": {
            "mean": statistics.mean(price_per_sqft) if price_per_sqft else 0,
            "median": statistics.median(price_per_sqft) if price_per_sqft else 0,
        },
        "debt": {
            "total": sum(debts),
            "mean": statistics.mean(debts) if debts else 0,
            "median": statistics.median(debts) if debts else 0,
            "min": min(debts) if debts else 0,
            "max": max(debts) if debts else 0,
        },
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
    # Self-check: stats on fixture data
    fixture = [
        {"property_address": "10022 Plainsman Ln, Frisco, TX 75035",
         "details": {
             "property_info": {"Type": "Residential Single Family (A1)"},
             "improvement_info": {"Improvement Area": "4,332 sq ft", "Year Built": "2004"},
             "jurisdiction_info": {"Taxing Entities": "Collin County / Frisco ISD / Frisco City"},
             "owner_info": {"Name": "Islam Mazhar & Ambreen"},
             "tax_info": {"Appraised Value (2026)": "$730,673.00"},
             "scanned_notice_ocr_data": {"estimated_default_balance": 350000,
                                         "foreclosing_lender": "Wells Fargo"},
         }},
        {"property_address": "15806 Breedlove Pl, Addison, TX 75001",
         "details": {
             "property_info": {"Type": "Residential Single Family (A1)"},
             "improvement_info": {"Improvement Area": "Unknown", "Year Built": "Unknown"},
             "jurisdiction_info": {"Taxing Entities": "Dallas County / Addison"},
             "owner_info": {"Name": "KERRY P. GRAY"},
             "tax_info": {"Appraised Value (2026)": "$387,000.00"},
             "scanned_notice_ocr_data": {"estimated_default_balance": 309600,
                                         "foreclosing_lender": "CTX Mortgage"},
         }},
    ]

    stats = portfolio_stats(fixture)
    assert stats["total_properties"] == 2
    assert "Frisco" in stats["cities"]
    assert "Addison" in stats["cities"]
    assert stats["appraised"]["total"] == 1_117_673.0
    assert stats["debt"]["total"] == 659_600.0
    assert "Wells Fargo" in stats["lender_concentration"]

    report = render_stats_report(stats)
    assert "PORTFOLIO STATISTICAL SUMMARY" in report
    assert "Frisco" in report
    assert "DEBT EXPOSURE" in report

    # X7: real CPR objects — ISD derives from taxing-entity strings when the
    # `isd` field is missing (the old code read a nonexistent
    # `jurisdiction_info` attribute, so ISD was always Unknown), and the
    # trustee distribution now includes enriched records.
    from .cpr import CPR, ProvenanceField

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

    print("synthesis.py: X7 — CPR ISD derivation + trustee distribution verified")
    print("synthesis.py: all self-checks passed")