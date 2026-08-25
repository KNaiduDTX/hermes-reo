#!/usr/bin/env python3
# find_deals.py — End-to-end run that finds and analyzes ALL current deals.
#
# Loads the full 335-record lucid-noether dataset, enriches each into a CPR,
# runs all gates, and outputs deal analysis + portfolio stats + GeoJSON.
from __future__ import annotations
import json
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.enrichment import enrich_from_raw_record, enrich_lead
from src.adapters.collin_foreclosure import CollinForeclosureAdapter
from src.adapters.geojson_export import cpr_to_geojson
from src.underwriting import mao
from src.pricing import assign_blast_tier
from src.compliance import gate_legal_lien
from src.synthesis import portfolio_stats, render_stats_report, get_city
from src.cpr import CPR
from src.io_utils import write_json, write_text  # atomic crash-safe writers (shared contract)

# F1: dataset location is overridable via env; default keeps the lucid-noether path.
DEFAULT_LUCID_DATA = "/Users/knaidu/Documents/antigravity/lucid-noether/foreclosure_report.json"
LUCID_DATA = os.environ.get("HERMES_LUCID_DATA") or DEFAULT_LUCID_DATA


def load_prior_dataset():
    """Load the 335-record lucid-noether dataset and enrich each into a CPR."""
    print("\n[DATASET] Loading prior 335-record foreclosure dataset...")
    if not os.path.exists(LUCID_DATA):
        # F1: fail loudly but gracefully — no traceback.
        print(f"  ✗ Dataset not found: {LUCID_DATA}")
        print("    Fix: set HERMES_LUCID_DATA to your foreclosure_report.json path,")
        print("    or restore the default lucid-noether checkout, then re-run.")
        sys.exit(1)
    with open(LUCID_DATA) as f:
        records = json.load(f)
    print(f"  ✓ Loaded {len(records)} records ({sum(1 for r in records if r.get('county') == 'Collin')} Collin, {sum(1 for r in records if r.get('county') == 'Dallas')} Dallas)")

    cprs = []
    for i, record in enumerate(records, 1):
        cpr = enrich_from_raw_record(record)
        cprs.append(cpr)
    print(f"  ✓ Enriched {len(cprs)} CPRs")
    return cprs


def scrape_live_collin(limit=5):
    """Live scrape Collin County for current notices."""
    print(f"\n[COLLIN] Live scraping foreclosure portal (limit={limit})...")
    adapter = CollinForeclosureAdapter()
    try:
        leads = adapter.fetch(limit=limit, extract_details=True)
        print(f"  ✓ Sourced {len(leads)} live leads")
        cprs = [enrich_lead(l) for l in leads]
        return cprs
    except Exception as e:
        print(f"  ✗ Collin scrape failed: {e}")
        return []


def analyze_deal(cpr: CPR) -> dict:
    """Analyze a single deal — everything we know, what's blocked."""
    addr = cpr.situs_address
    # F3: CPR may or may not carry a parsed city field — fall back to get_city().
    city_pf = getattr(cpr, "city", None)
    city = (city_pf.value if city_pf else None) or get_city(addr)
    sqft = cpr.improvement_sqft.value if cpr.improvement_sqft else None
    beds = cpr.beds.value if cpr.beds else None
    baths = cpr.baths.value if cpr.baths else None
    yr_built = cpr.year_built.value if cpr.year_built else None
    stories = cpr.stories.value if cpr.stories else None
    pool = cpr.pool.value if cpr.pool else None
    cad_val = cpr.cad_appraised_value.value if cpr.cad_appraised_value else None
    cad_val_2025 = cpr.cad_appraised_value_2025.value if hasattr(cpr, 'cad_appraised_value_2025') and cpr.cad_appraised_value_2025 else None
    owner = cpr.owner_name.value if cpr.owner_name else "Unknown"
    ptype = cpr.property_type.value if cpr.property_type else "Unknown"
    sale_date = cpr.sale_date.value if cpr.sale_date else "Unknown"
    isd = cpr.isd.value if hasattr(cpr, 'isd') and cpr.isd else "Unknown"
    lender = cpr.foreclosing_lender.value if hasattr(cpr, 'foreclosing_lender') and cpr.foreclosing_lender else "Unknown"
    trustee = cpr.substitute_trustee.value if hasattr(cpr, 'substitute_trustee') and cpr.substitute_trustee else "Unknown"
    sale_time = cpr.scheduled_sale_time.value if hasattr(cpr, 'scheduled_sale_time') and cpr.scheduled_sale_time else "Unknown"
    orig_principal = cpr.original_principal.value if hasattr(cpr, 'original_principal') and cpr.original_principal else None
    unpaid_principal = cpr.unpaid_principal_balance.value if hasattr(cpr, 'unpaid_principal_balance') and cpr.unpaid_principal_balance else None
    all_balances = cpr.all_extracted_balances.value if hasattr(cpr, 'all_extracted_balances') and cpr.all_extracted_balances else []
    census_tract = cpr.census_tract.value if hasattr(cpr, 'census_tract') and cpr.census_tract else None
    county_fips = cpr.county_fips.value if hasattr(cpr, 'county_fips') and cpr.county_fips else None
    commercial = cpr.commercial.value if hasattr(cpr, 'commercial') and cpr.commercial else False
    deed_number = cpr.deed_number.value if hasattr(cpr, 'deed_number') and cpr.deed_number else None

    # Debt from liens
    mortgage_debt = sum(l.amount or 0 for l in cpr.liens if l.lien_type == "mortgage")

    # Lien gate
    lien_gate = gate_legal_lien(cpr)

    # GATE-VAL — CAD-only, will fail (non-disclosure)
    val_status = "REJECTED (assessed-value-only — non-disclosure state)"
    if not cad_val:
        val_status = "NO DATA"

    # Preliminary MAO using 70% rule on CAD (directional only)
    preliminary_mao = None
    rough_rehab = 0
    if cad_val and sqft:
        rough_rehab = sqft * (20 if (yr_built and yr_built < 2010) else 10)
        preliminary_mao = mao(cad_val, rough_rehab, 0.70)

    # Equity estimate: CAD - debt
    equity_estimate = None
    equity_pct = None
    if cad_val and mortgage_debt and mortgage_debt > 0:
        equity_estimate = cad_val - mortgage_debt
        equity_pct = (equity_estimate / cad_val) * 100 if cad_val > 0 else 0

    # LTV
    ltv = None
    if cad_val and mortgage_debt and mortgage_debt > 0:
        ltv = (mortgage_debt / cad_val) * 100

    # Valuation change 2025→2026
    val_change = None
    if cad_val and cad_val_2025 and cad_val_2025 > 0:
        val_change = ((cad_val - cad_val_2025) / cad_val_2025) * 100

    # Blast tier
    capital = preliminary_mao or 0
    tier = assign_blast_tier(capital)

    return {
        "address": addr, "city": city, "county": cpr.county,
        "parcel_id": cpr.parcel_id, "owner": owner, "property_type": ptype,
        "sqft": sqft, "beds": beds, "baths": baths, "year_built": yr_built,
        "stories": stories, "pool": pool, "commercial": commercial,
        "cad_value": cad_val, "cad_value_2025": cad_val_2025,
        "price_per_sqft": (cad_val / sqft) if (cad_val and sqft) else None,
        "val_change_pct": val_change,
        "isd": isd, "deed_number": deed_number,
        "mortgage_debt": mortgage_debt if mortgage_debt > 0 else None,
        "lender": lender if lender != "Unknown" else None,
        "trustee": trustee if trustee != "Unknown" else None,
        "sale_date": sale_date, "sale_time": sale_time,
        "original_principal": orig_principal,
        "unpaid_principal": unpaid_principal,
        "all_balances": all_balances,
        "census_tract": census_tract, "county_fips": county_fips,
        "equity_estimate": equity_estimate,
        "equity_pct": equity_pct,
        "ltv_pct": ltv,
        "preliminary_mao": preliminary_mao,
        "rough_rehab": rough_rehab if rough_rehab > 0 else None,
        "blast_tier": tier.name,
        "lien_gate": lien_gate.verdict,
        "val_gate": val_status,
        "hitl_required": "MLS/appraiser comp pull for real ARV",
    }


def main():
    print("=" * 70)
    print("  HERMES-REO — FULL DEAL FINDER")
    print(f"  Run time: {datetime.utcnow().isoformat()}")
    print("=" * 70)

    # Load prior dataset (335 records with full enrichment)
    cprs = load_prior_dataset()

    # Also live scrape Collin for current notices
    live_cprs = scrape_live_collin(limit=5)

    # Merge: prior dataset + live (dedupe by parcel_id)
    seen_ids = {c.parcel_id for c in cprs}
    for c in live_cprs:
        if c.parcel_id not in seen_ids:
            cprs.append(c)
            seen_ids.add(c.parcel_id)

    print(f"\nTotal CPRs: {len(cprs)} ({len(cprs) - len(live_cprs)} from prior dataset + {len(live_cprs)} live)")

    # Analyze each deal
    print("\n" + "=" * 70)
    print("  DEAL ANALYSIS")
    print("=" * 70)

    deals = []
    for i, cpr in enumerate(cprs, 1):
        deal = analyze_deal(cpr)
        deals.append(deal)

    # Print summary table for all deals with debt
    deals_with_debt = [d for d in deals if d["mortgage_debt"]]
    deals_with_debt.sort(key=lambda d: d["mortgage_debt"], reverse=True)

    print(f"\n  Total deals: {len(deals)}")
    print(f"  With extracted debt: {len(deals_with_debt)}")
    print(f"  With property details (sqft): {sum(1 for d in deals if d['sqft'])}")
    print(f"  With census demographics: {sum(1 for d in deals if d['census_tract'])}")

    # Print top 20 deals by debt
    print(f"\n  TOP 20 DEALS BY DEBT (full data):")
    print(f"  {'#':>3} {'Address':<45} {'City':<15} {'Debt':>12} {'CAD':>12} {'LTV':>6} {'Equity':>12} {'Lender'}")
    print(f"  {'':>3} {'':45} {'':15} {'':12} {'':12} {'':6} {'':12} {'':30}")
    for i, d in enumerate(deals_with_debt[:20], 1):
        addr_short = d["address"][:43] if d["address"] else "Unknown"
        city_short = (d["city"] or "Unknown")[:13]
        debt_str = f"${d['mortgage_debt']:>10,.0f}" if d["mortgage_debt"] else "       N/A"
        cad_str = f"${d['cad_value']:>10,.0f}" if d["cad_value"] else "       N/A"
        ltv_str = f"{d['ltv_pct']:>5.0f}%" if d["ltv_pct"] else "   N/A"
        eq_str = f"${d['equity_estimate']:>10,.0f}" if d["equity_estimate"] is not None else "       N/A"
        lender_short = (d["lender"] or "Unknown")[:28]
        print(f"  {i:>3} {addr_short:<45} {city_short:<15} {debt_str} {cad_str} {ltv_str} {eq_str} {lender_short}")

    # Print deals with full property details
    deals_with_details = [d for d in deals if d["sqft"]]
    print(f"\n  DEALS WITH FULL PROPERTY DETAILS ({len(deals_with_details)}):")
    for i, d in enumerate(deals_with_details[:10], 1):
        print(f"\n  {i}. {d['address']}")
        print(f"     {d['county'].title()} | {d['city']} | {d['property_type']}")
        print(f"     {d['sqft']:,} sqft | {d['beds']} bed/{d['baths']} bath | Built {d['year_built']} | {d['stories']} stories | Pool: {d['pool']}")
        if d["cad_value"]:
            pps = f" (${d['price_per_sqft']:.0f}/sqft)" if d["price_per_sqft"] else ""
            print(f"     CAD: ${d['cad_value']:,.0f}{pps}" + (f" (2025: ${d['cad_value_2025']:,.0f}, change: {d['val_change_pct']:+.1f}%)" if d['val_change_pct'] is not None else ""))
        if d["mortgage_debt"] and d.get("ltv_pct") is not None:
            lender_str = d.get("lender") or "Unknown"
            print(f"     Debt: ${d['mortgage_debt']:,.0f} ({lender_str}) | LTV: {d['ltv_pct']:.0f}%")
            if d.get("equity_estimate") is not None and d.get("equity_pct") is not None:
                eq = d["equity_estimate"]
                eq_str = f"${eq:,.0f}" if eq >= 0 else f"-${abs(eq):,.0f} (UNDERWATER)"
                print(f"     Equity: {eq_str} ({d['equity_pct']:.0f}%)")
        if d["isd"] != "Unknown":
            print(f"     ISD: {d['isd']}")
        if d["census_tract"]:
            print(f"     Census: tract {d['census_tract']}, FIPS {d['county_fips']}")
        if d["preliminary_mao"]:
            print(f"     Preliminary MAO (70% rule): ${d['preliminary_mao']:,.0f} (rehab est: ${d['rough_rehab']:,.0f})")
        print(f"     Trustee: {d['trustee']} | Sale: {d['sale_time']}")
        print(f"     Blast tier: {d['blast_tier']} | Lien gate: {d['lien_gate']} | VAL gate: {d['val_gate']}")

    # Portfolio summary
    print("\n" + "=" * 70)
    print("  PORTFOLIO SUMMARY")
    print("=" * 70)
    stats = portfolio_stats(cprs)
    report = render_stats_report(stats)
    print(report)

    # Save outputs
    output_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(output_dir, exist_ok=True)

    deals_path = write_json(os.path.join(output_dir, "current_deals.json"), deals, default=str)

    stats_path = write_text(os.path.join(output_dir, "live_portfolio_stats.md"), report)

    geojson_payload = cpr_to_geojson(cprs)
    geojson_path = write_json(os.path.join(output_dir, "live_foreclosures.geojson"),
                              geojson_payload, default=str)
    n_features = len(geojson_payload["features"])

    print(f"\n  Saved: {deals_path} ({len(deals)} deals)")
    print(f"  Saved: {stats_path}")
    print(f"  Saved: {geojson_path} ({n_features} features)")

    # Final summary
    print("\n" + "=" * 70)
    print("  DEAL FLOW SUMMARY")
    print("=" * 70)
    total = len(deals)
    with_equity = sum(1 for d in deals if d["equity_estimate"] and d["equity_estimate"] > 0)
    with_debt = sum(1 for d in deals if d["mortgage_debt"])
    underwater = sum(1 for d in deals if d["equity_estimate"] is not None and d["equity_estimate"] < 0)
    has_sqft = sum(1 for d in deals if d["sqft"])
    has_census = sum(1 for d in deals if d["census_tract"])
    has_lender = sum(1 for d in deals if d["lender"])
    has_trustee = sum(1 for d in deals if d["trustee"])
    has_orig_principal = sum(1 for d in deals if d["original_principal"])

    print(f"  Total deals: {total}")
    print(f"  With property details (sqft/beds/baths): {has_sqft}")
    print(f"  With extracted debt: {with_debt}")
    print(f"  With foreclosing lender: {has_lender}")
    print(f"  With substitute trustee: {has_trustee}")
    print(f"  With original principal: {has_orig_principal}")
    print(f"  With census demographics: {has_census}")
    print(f"  With positive equity (CAD - debt): {with_equity}")
    print(f"  Underwater (debt > CAD): {underwater}")
    print(f"  All require MLS/appraiser HITL for real ARV (non-disclosure state)")

    # Top equity prospects
    equity_deals = [d for d in deals if d["equity_estimate"] and d["equity_estimate"] > 0]
    if equity_deals:
        equity_deals.sort(key=lambda d: d["equity_estimate"], reverse=True)
        print(f"\n  TOP 10 EQUITY PROSPECTS (CAD-based, needs MLS verification):")
        for i, d in enumerate(equity_deals[:10], 1):
            print(f"  {i:>2}. {d['address'][:40]:<42} equity ~${d['equity_estimate']:>10,.0f} ({d['equity_pct']:>3.0f}%) | debt ${d['mortgage_debt']:>10,.0f} | {d['lender'] or 'Unknown'}")

    print("\n" + "=" * 70)
    print("  RUN COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()