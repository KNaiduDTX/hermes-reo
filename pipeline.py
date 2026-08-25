#!/usr/bin/env python3
# pipeline.py — End-to-end REO deal pipeline runner.
#
# Runs the full spine: source → enrich → value → underwrite → price → memo → review.
# Usage:
#   python3 pipeline.py --county collin --limit 1    # live Collin scrape
#   python3 pipeline.py --fixture                    # use fixture data (no network)
#   python3 pipeline.py --all-checks                # run all self-checks first
from __future__ import annotations
import argparse
import json
import sys
import os

# Ensure src is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_all_checks() -> bool:
    """Run all module self-checks. Returns True if all pass."""
    modules = [
        "src.legal_rules", "src.cpr", "src.source_registry",
        "src.adapters.base", "src.adapters.dallas_foreclosure",
        "src.adapters.dallas_cad", "src.adapters.dallas_cad_bulk",
        "src.adapters.collin_clerk", "src.adapters.grayson_tax",
        "src.adapters.route_optimizer", "src.adapters.geojson_export",
        "src.adapters.notice_ocr",
        "src.enrichment", "src.valuation", "src.underwriting",
        "src.pricing", "src.deal_memo", "src.compliance", "src.reviewer",
        "src.synthesis", "src.negotiation",
    ]
    all_pass = True
    for mod in modules:
        ret = os.system(f"{sys.executable} -m {mod} > /dev/null 2>&1")
        status = "✓" if ret == 0 else "✗"
        print(f"  {status} {mod}")
        if ret != 0:
            all_pass = False
    return all_pass


def run_fixture_pipeline():
    """Run the full pipeline on fixture data (no network required).

    Uses the verified Collin County parcel from lucid-noether (entry_id 4003592).
    """
    from src.cpr import CPR, ProvenanceField, ValuationObject, CompRecord, CPRColumn, BlastTier, LienRecord
    from src.enrichment import enrich_from_collin_details
    from src.adapters.base import NormalizedLead
    from src.valuation import gate_val, attach_valuation, GateValResult
    from src.underwriting import flip_pnl, mao
    from src.pricing import price_deal, PricingResult
    from src.deal_memo import render_memo
    from src.compliance import gate_legal_lien, gate_legal_eligibility
    from src.reviewer import run_review

    print("=" * 70)
    print("  HERMES-REO PIPELINE — FIXTURE RUN (entry_id 4003592)")
    print("=" * 70)

    # Step 1: Source — fixture lead (from lucid-noether verified data)
    print("\n[1/7] SOURCE — Collin County foreclosure notice (fixture)")
    lead = NormalizedLead(
        county="collin", parcel_id="4003592", channel="foreclosure_notice",
        situs_address="10022 Plainsman Ln Frisco, TX 75035",
        latitude=33.17922234, longitude=-96.79394064,
        raw_data={"details": {
            "property_info": {
                "Address": "10022 Plainsman Ln Frisco, TX 75035",
                "Type": "Residential Single Family (A1)",
                "Deed Date": "7/25/2013",
                "Legal Description": "Village At Panther Creek Phase One, The (Cfr), Blk N, Lot 32",
                "Commercial": "False",
            },
            "improvement_info": {
                "Improvement Area": "4,332 sq ft",
                "Year Built": "2004",
                "Bedrooms": "5",
                "Bathrooms": "4",
            },
            "jurisdiction_info": {
                "Taxing Entities": "Collin County (GCN) / Frisco ISD (SFR) / Frisco City (CFR)",
            },
            "owner_info": {
                "Name": "Islam Mazhar & Ambreen",
                "Address": "10022 Plainsman Ln / Frisco, TX 75035-6919",
            },
            "tax_info": {
                "Appraised Value (2026)": "$730,673.00",
            },
        }},
    )
    print(f"  Lead: {lead.situs_address} ({lead.county})")

    # Step 2: Enrich — assemble CPR
    print("\n[2/7] ENRICH — Assemble Canonical Property Record")
    cpr = enrich_from_collin_details(lead)
    print(f"  CPR: {cpr.parcel_id} | {cpr.situs_address}")
    print(f"  Owner: {cpr.owner_name.value} | Sqft: {cpr.improvement_sqft.value} | Beds: {cpr.beds.value}")
    print(f"  CAD: ${cpr.cad_appraised_value.value:,.0f} | Column: {cpr.column.value}")
    print(f"  Ledger entries: {len(cpr.ledger)}")

    # Step 2b: Clerk lien search (Collin County only)
    if cpr.county == "collin" and cpr.owner_name and cpr.owner_name.value:
        print(f"\n[2b] CLERK LIEN SEARCH — Collin County official records")
        # Clean owner name for clerk search: take first name, uppercase, strip "& TRUST" suffixes
        owner_raw = cpr.owner_name.value
        # ponytail: Kofile searches by exact name match — long names with "&" return nothing
        # Take the first two words (first + last name) for broadest match
        owner_parts = owner_raw.replace("&", " ").split()
        owner_search = " ".join(owner_parts[:2]).upper()
        print(f"  Searching by owner: {owner_search} (from '{owner_raw}')")
        try:
            from src.adapters.collin_clerk import CollinClerkAdapter, build_lien_stack
            clerk = CollinClerkAdapter()
            instruments = clerk.fetch(party_name=owner_search)
            print(f"  Instruments found: {len(instruments)}")
            if instruments:
                clerk_liens = build_lien_stack(instruments)
                print(f"  Lien stack: {len(clerk_liens)} encumbrances")
                for l in clerk_liens:
                    s_fc = "SURVIVES FC" if l.survives_foreclosure else ""
                    print(f"    #{l.priority} {l.lien_type:15s} | {l.recording_date:35s} | {s_fc}")
                # Merge clerk liens into CPR
                for l in clerk_liens:
                    cpr.add_lien(l, "collin_clerk")
                print(f"  → {len(clerk_liens)} liens merged into CPR (total: {len(cpr.liens)})")
        except Exception as e:
            print(f"  Clerk search skipped: {e}")
    elif cpr.county == "dallas":
        print(f"\n[2b] DCAD CROSS-REFERENCE — Dallas CAD enrichment")
        try:
            from src.enrichment import cross_reference_dcad
            cpr = cross_reference_dcad(cpr)
            if cpr.improvement_sqft and cpr.improvement_sqft.value:
                print(f"  DCAD: {cpr.improvement_sqft.value:,.0f} sqft, {cpr.beds.value if cpr.beds else '?'} bd")
            else:
                print(f"  DCAD: no results (address may not match)")
        except Exception as e:
            print(f"  DCAD skipped: {e}")

    # Step 3: GATE-LEGAL-LIEN — mandatory before pricing
    print("\n[3/7] GATE-LEGAL-LIEN — Title & lien priority check")
    lien_gate = gate_legal_lien(cpr)
    print(f"  Verdict: {lien_gate.verdict} — {lien_gate.reason}")
    if lien_gate.verdict == "fail":
        print("\n  ⚠️ GATE-LEGAL-LIEN failed — pipeline stops. Run title search before pricing.")
        # Still continue to valuation for demonstration, but in production this would halt
        print("  (Continuing to valuation for demo — production would halt here)")

    # Step 4: Valuation — GATE-VAL
    print("\n[4/7] VALUATION — GATE-VAL (ARV grounding gate)")
    # Simulate MLS agent comps (in production, these come from the MLS HITL)
    comps = [
        CompRecord(address="10030 Plainsman Ln", sale_price=755_000,
                   distance_miles=0.1, source="mls_agent", adjusted_price=750_000),
        CompRecord(address="10015 Plainsman Ln", sale_price=745_000,
                   distance_miles=0.15, source="mls_agent", adjusted_price=748_000),
        CompRecord(address="10040 Plainsman Ct", sale_price=760_000,
                   distance_miles=0.2, source="mls_agent", adjusted_price=755_000),
        CompRecord(address="10050 Plainsman Ct", sale_price=748_000,
                   distance_miles=0.25, source="mls_agent", adjusted_price=752_000),
        CompRecord(address="10060 Plainsman Dr", sale_price=752_000,
                   distance_miles=0.3, source="mls_agent", adjusted_price=750_000),
    ]
    gate, val_obj = gate_val(cpr, "mls_agent", comps, BlastTier.T3)
    cpr = attach_valuation(cpr, val_obj, gate)
    print(f"  GATE-VAL: {gate.verdict} — {gate.reason}")
    if val_obj.arv:
        print(f"  ARV: ${val_obj.arv.value:,.0f} (confidence: {val_obj.arv.confidence:.0%})")
    print(f"  Column: {cpr.column.value}")

    if gate.verdict != "pass":
        print(f"\n  ⚠️ Gate failed — pipeline stops here. Deal routed to HITL.")
        return

    # Step 5: Underwrite
    print("\n[5/7] UNDERWRITE — Fix-and-flip model")
    arv = val_obj.arv.value
    rehab = 50_000  # estimated rehab for a 2004 build, 4332 sqft
    uw = flip_pnl(
        arv=arv, purchase=mao(arv, rehab, 0.70), rehab=rehab,
        holding=5_000, financing=10_000, closing_buy=5_000,
        closing_sell=arv * 0.08,
    )
    print(f"  Model: {uw.model}")
    print(f"  MAO (70% rule): ${mao(arv, rehab, 0.70):,.0f}")
    print(f"  Purchase: ${uw.purchase_price:,.0f} | Rehab: ${uw.rehab:,.0f} (w/contingency: ${uw.line_items['rehab_with_contingency']:,.0f})")
    print(f"  Net profit: ${uw.net_profit:,.0f} | ROI: {uw.roi:.1%}")

    # Step 6: Pricing
    print("\n[6/7] PRICING — MAO, bid ceiling, offer ladder, sensitivity")
    pricing = price_deal(
        cpr, arv=arv, rehab=rehab, deal_type="DT-AUCTION",
        factor=0.70, capital_at_risk=uw.cash_invested,
    )
    print(f"  MAO: ${pricing.mao:,.0f}")
    print(f"  Bid ceiling: ${pricing.bid_ceiling:,.0f}")
    print(f"  Blast tier: {pricing.blast_tier.name}")
    print(f"  Offer ladder: open ${pricing.offer_ladder['opening']:,.0f} → target ${pricing.offer_ladder['target']:,.0f} → walk ${pricing.offer_ladder['walk_away']:,.0f}")
    print(f"  Optimistic-only flag: {pricing.optimistic_only_flag}")

    # Step 7: Reviewer — fake-signal harness
    print("\n[7/7] REVIEWER — AGENT-REV fake-signal harness")
    review = run_review(cpr, gate, pricing)
    print(f"  Overall: {review.overall} ({len(review.checks)} checks)")
    for check in review.checks:
        symbol = "✓" if check.verdict == "pass" else "✗"
        print(f"  {symbol} {check.check_name}: {check.reason}")
    if review.rejection_reasons:
        print(f"\n  ⚠️ REJECTIONS:")
        for r in review.rejection_reasons:
            print(f"    ✗ {r}")

    # Render deal memo
    print("\n" + "=" * 70)
    print("  DEAL MEMO")
    print("=" * 70)
    memo = render_memo(cpr, uw, pricing)
    print(memo)

    # Step 8: Negotiation — generate offer package
    output_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(output_dir, exist_ok=True)
    print("\n" + "=" * 70)
    print("  NEGOTIATION — Seller playbook + offer generation")
    print("=" * 70)
    try:
        from src.negotiation import generate_offer, get_posture
        # Ensure deal_type is set
        dt = cpr.deal_type
        dt_val = dt.value if hasattr(dt, 'value') else dt
        if not dt_val:
            cpr.set_field("deal_type", "DT-PREFORE", "pipeline", 0.80)
            dt_val = "DT-PREFORE"
        posture = get_posture(dt_val)
        offer = generate_offer(cpr, pricing)
        print(f"  Deal type: {dt_val}")
        print(f"  Posture: {posture['framing']}")
        print(f"  Offer price: ${offer.offer_price:,.0f}")
        print(f"  Earnest money: ${offer.earnest_money:,.0f} | Option fee: ${offer.option_fee:,.0f}")
        print(f"  Option period: {offer.option_period_days}d | Closing: {offer.closing_timeline_days}d")
        print(f"  Cash offer: {offer.is_cash} | And/or assigns: {offer.has_and_or_assigns}")
        if offer.blocked:
            print(f"  ⚠️ BLOCKED: {offer.block_reason}")
        else:
            print(f"  Key message: {offer.key_message[:80]}")
        offer_path = os.path.join(output_dir, "offer_letter_4003592.txt")
        with open(offer_path, "w") as f:
            f.write(offer.offer_letter)
        print(f"  Saved: {offer_path}")
    except Exception as e:
        print(f"  Negotiation skipped: {e}")

    # Save outputs
    memo_path = os.path.join(output_dir, "deal_memo_4003592.md")
    with open(memo_path, "w") as f:
        f.write(memo)
    cpr_path = os.path.join(output_dir, "cpr_4003592.json")
    with open(cpr_path, "w") as f:
        json.dump(cpr.to_dict(), f, indent=2, default=str)

    # Synthesis: portfolio stats + GeoJSON export
    from src.synthesis import portfolio_stats, render_stats_report
    from src.adapters.geojson_export import export_geojson

    stats = portfolio_stats([cpr])
    stats_report = render_stats_report(stats)
    stats_path = os.path.join(output_dir, "portfolio_stats.md")
    with open(stats_path, "w") as f:
        f.write(stats_report)

    geojson_path = os.path.join(output_dir, "foreclosures.geojson")
    n_features = export_geojson([cpr], geojson_path)

    print(f"\n  Saved: {memo_path}")
    print(f"  Saved: {cpr_path}")
    print(f"  Saved: {stats_path}")
    print(f"  Saved: {geojson_path} ({n_features} features)")
    print("\n" + "=" * 70)
    print("  PIPELINE RUN COMPLETE")
    print("=" * 70)


def run_live_pipeline(county: str, limit: int):
    """Run the pipeline on live data from the specified county."""
    print(f"\n{'=' * 70}")
    print(f"  HERMES-REO PIPELINE — LIVE RUN ({county.upper()}, limit={limit})")
    print(f"{'=' * 70}")

    # Step 1: Source
    print(f"\n[1/7] SOURCE — {county.title()} County foreclosure notices")
    if county == "collin":
        from src.adapters.collin_foreclosure import CollinForeclosureAdapter
        adapter = CollinForeclosureAdapter()
        leads = adapter.fetch(limit=limit, extract_details=True)
    elif county == "dallas":
        from src.adapters.dallas_foreclosure import DallasForeclosureAdapter
        adapter = DallasForeclosureAdapter()
        leads = adapter.fetch(limit=limit)
    else:
        print(f"  No adapter for {county} yet — use --fixture or --county collin")
        return

    print(f"  Sourced {len(leads)} leads")
    if not leads:
        print("  No leads found — pipeline complete.")
        return

    # Process each lead through the spine
    from src.enrichment import enrich_lead
    from src.valuation import gate_val, attach_valuation
    from src.underwriting import flip_pnl, mao
    from src.pricing import price_deal
    from src.deal_memo import render_memo
    from src.compliance import gate_legal_lien
    from src.reviewer import run_review
    from src.cpr import CompRecord, BlastTier

    for i, lead in enumerate(leads, 1):
        print(f"\n--- Parcel {i}/{len(leads)}: {lead.situs_address} ---")

        # Enrich
        cpr = enrich_lead(lead)
        print(f"  Enriched: {cpr.situs_address} | Column: {cpr.column.value}")

        # Clerk lien search (Collin only)
        if cpr.county == "collin" and cpr.owner_name and cpr.owner_name.value:
            try:
                from src.adapters.collin_clerk import CollinClerkAdapter, build_lien_stack
                # Clean owner name: take first two words, uppercase
                owner_parts = cpr.owner_name.value.replace("&", " ").split()
                owner_search = " ".join(owner_parts[:2]).upper()
                clerk = CollinClerkAdapter()
                instruments = clerk.fetch(party_name=owner_search)
                if instruments:
                    clerk_liens = build_lien_stack(instruments)
                    for l in clerk_liens:
                        cpr.add_lien(l, "collin_clerk")
                    print(f"  Clerk liens: {len(clerk_liens)} merged (total: {len(cpr.liens)})")
            except Exception as e:
                print(f"  Clerk search skipped: {e}")

        # DCAD cross-reference (Dallas only)
        if cpr.county == "dallas":
            try:
                from src.enrichment import cross_reference_dcad
                cpr = cross_reference_dcad(cpr)
                if cpr.improvement_sqft and cpr.improvement_sqft.value:
                    print(f"  DCAD: {cpr.improvement_sqft.value:,.0f} sqft")
            except Exception as e:
                print(f"  DCAD skipped: {e}")

        # Lien gate
        lien_gate = gate_legal_lien(cpr)
        print(f"  GATE-LEGAL-LIEN: {lien_gate.verdict} — {lien_gate.reason[:80]}")

        # Valuation (using CAD as a weak baseline — in production, use MLS HITL)
        # ponytail: CAD-only valuation will fail GATE-VAL — this is by design.
        # The system correctly refuses to emit an ARV without approved comp provenance.
        from src.cpr import ValuationObject
        cpr.valuation = ValuationObject(
            comp_source="assessed_value",  # will be rejected
            gate_val_verdict="fail",
            gate_val_reason="Assessed-value-only — route to MLS/appraiser HITL",
        )
        print(f"  GATE-VAL: fail (assessed-value-only — HITL required for real ARV)")
        print(f"  → Deal routed to HITL for MLS comp pull")

    print(f"\n{'=' * 70}")
    print(f"  LIVE RUN COMPLETE — {len(leads)} parcels processed")
    print(f"  All require MLS/appraiser HITL for valuation (non-disclosure state)")
    print(f"{'=' * 70}")


def _run_bulk_dcad():
    """Run DCAD bulk cross-reference on all Dallas CPRs from the lucid-noether dataset.

    Uses the batch adapter with JSON cache (data/dcad_cache.json) and 2s rate limiting.
    Estimated runtime: ~2s per address × ~165 Dallas records ≈ 5-6 minutes.
    """
    print(f"\n{'=' * 70}")
    print(f"  HERMES-REO PIPELINE — DCAD BULK CROSS-REFERENCE")
    print(f"{'=' * 70}")

    # Load the lucid-noether dataset
    data_path = os.path.join(os.path.dirname(__file__), "data", "current_deals.json")
    if not os.path.exists(data_path):
        print(f"  No dataset found at {data_path}")
        print(f"  Run find_deals.py first to generate current_deals.json")
        return

    with open(data_path) as f:
        records = json.load(f)

    # Enrich Dallas records into CPRs
    from src.enrichment import enrich_from_raw_record
    from src.adapters.dallas_cad_bulk import cross_reference_dcad_bulk

    dallas_cprs = []
    for record in records:
        if record.get("county", "").lower() == "dallas":
            cpr = enrich_from_raw_record(record)
            if cpr and cpr.situs_address and cpr.situs_address != "Unknown":
                dallas_cprs.append(cpr)

    print(f"  Dallas CPRs to cross-reference: {len(dallas_cprs)}")
    if not dallas_cprs:
        print("  No Dallas CPRs found — nothing to do.")
        return

    # Run bulk cross-reference (limit 50 per run to avoid long sessions)
    updated = cross_reference_dcad_bulk(dallas_cprs, batch_limit=50)
    print(f"  CPRs updated with DCAD data: {updated}")
    print(f"  Cache saved to: data/dcad_cache.json")
    print(f"\n{'=' * 70}")
    print(f"  DCAD BULK COMPLETE — {updated} enriched, {len(dallas_cprs) - updated} skipped/failed")
    print(f"{'=' * 70}")


def main():
    parser = argparse.ArgumentParser(description="HERMES-REO Pipeline Runner")
    parser.add_argument("--county", default="collin", choices=["collin", "dallas", "grayson"])
    parser.add_argument("--limit", type=int, default=1, help="Max records to process")
    parser.add_argument("--fixture", action="store_true", help="Use fixture data (no network)")
    parser.add_argument("--all-checks", action="store_true", help="Run all module self-checks first")
    parser.add_argument("--bulk-dcad", action="store_true", help="Run DCAD bulk cross-reference on all Dallas CPRs")
    args = parser.parse_args()

    if args.all_checks:
        print("Running all module self-checks...")
        if not run_all_checks():
            print("\n✗ Some checks failed — fix before proceeding.")
            sys.exit(1)
        print("✓ All checks passed.\n")

    if args.bulk_dcad:
        _run_bulk_dcad()
    elif args.fixture:
        run_fixture_pipeline()
    else:
        run_live_pipeline(args.county, args.limit)


if __name__ == "__main__":
    main()