# src/enrichment.py
"""AGENT-ENR: enrichment pipeline — assemble CPR from source leads.

Resolves identity across CAD ↔ clerk ↔ tax office, pulls characteristics,
recorded instruments, tax status, and skip-traces owner contact.

FIXED: Now captures ALL available fields from source data, including:
- Land area, stories, pool, deed number, commercial flag
- Both tax years (2025 + 2026)
- Census demographics (tract, block_group, block, county_fips, state_fips)
- Full OCR data (original_principal, unpaid_principal_balance, all_extracted_balances)
- Jurisdiction/taxing entities → ISD extraction
- Lat/lng as provenance fields
- Dallas: taxing entities, census, lat/lng from geocoding
"""
from __future__ import annotations
import re
from .cpr import CPR, ProvenanceField, LienRecord, CPRColumn
from .adapters.base import NormalizedLead


def _parse_int(val) -> int | None:
    if val is None or val == "Unknown":
        return None
    digits = re.sub(r'[^\d]', '', str(val))
    return int(digits) if digits else None


def _parse_float(val) -> float | None:
    if val is None or val == "Unknown":
        return None
    cleaned = re.sub(r'[^\d.]', '', str(val))
    return float(cleaned) if cleaned else None


def _parse_bool(val) -> bool:
    if val is None:
        return False
    return str(val).lower() in ("true", "y", "yes", "1")


def _extract_isd(taxing_entities: str) -> str:
    """Extract ISD (school district) from taxing entities string."""
    if not taxing_entities:
        return "Unknown"
    m = re.search(r'([A-Za-z0-9\s]+ISD)\b', taxing_entities)
    return m.group(1).strip() if m else "Unknown"


def _enrich_common(cpr: CPR, details: dict, source: str, base_confidence: float):
    """Enrich fields common to both Collin and Dallas records."""
    prop = details.get("property_info", {})
    imp = details.get("improvement_info", {})
    jur = details.get("jurisdiction_info", {})
    owner = details.get("owner_info", {})
    tax = details.get("tax_info", {})
    ocr = details.get("scanned_notice_ocr_data", {})
    census = details.get("census_demographics", {})

    # Property info — ALL fields
    cpr.set_field("property_type", prop.get("Type", "Unknown"), source, base_confidence)
    cpr.set_field("legal_description", prop.get("Legal Description", "Unknown"), source, base_confidence)
    cpr.set_field("deed_number", prop.get("Deed Number", "Unknown"), source, base_confidence)
    cpr.set_field("commercial", _parse_bool(prop.get("Commercial", "False")), source, base_confidence)

    # Improvement info — ALL fields
    cpr.set_field("improvement_sqft", _parse_int(imp.get("Improvement Area")), source, base_confidence)
    cpr.set_field("land_sqft", _parse_int(imp.get("Land Area")), source, base_confidence)
    cpr.set_field("beds", _parse_int(imp.get("Bedrooms")), source, base_confidence)
    cpr.set_field("baths", _parse_float(imp.get("Bathrooms")), source, base_confidence)
    cpr.set_field("year_built", _parse_int(imp.get("Year Built")), source, base_confidence)
    cpr.set_field("stories", _parse_int(imp.get("Stories")), source, base_confidence)
    cpr.set_field("pool", _parse_bool(imp.get("Pool", "N")), source, base_confidence)

    # Jurisdiction — taxing entities + ISD
    taxing = jur.get("Taxing Entities", "Unknown")
    cpr.set_field("taxing_entities", taxing, source, base_confidence)
    cpr.set_field("isd", _extract_isd(taxing), source, base_confidence)

    # Owner info
    cpr.set_field("owner_name", owner.get("Name", "Unknown"), source, base_confidence)
    cpr.set_field("owner_mailing_address", owner.get("Address", "Unknown"), source, base_confidence)

    # Tax info — BOTH years
    cpr.set_field("cad_appraised_value", _parse_float(tax.get("Appraised Value (2026)")), source, base_confidence)
    cpr.set_field("cad_appraised_value_2025", _parse_float(tax.get("Appraised Value (2025)")), source, base_confidence)
    cpr.set_field("cad_tax_year", "2026", source, 1.0)

    # Homestead/ag flags from jurisdiction info
    cpr.set_field("homestead", "HS" in taxing.upper() or "HOMESTEAD" in taxing.upper(), source, 0.70)

    # Census demographics — ALL fields
    if census.get("status") == "Success":
        cpr.set_field("census_tract", census.get("census_tract"), source, 0.95)
        cpr.set_field("census_block_group", census.get("block_group"), source, 0.95)
        cpr.set_field("census_block", census.get("block"), source, 0.95)
        cpr.set_field("county_fips", census.get("county_fips"), source, 0.95)
        cpr.set_field("state_fips", census.get("state_fips"), source, 0.95)

    # OCR data — ALL fields (not just debt)
    # If no pre-existing OCR data but we have a scanned image, run OCR now
    if not ocr and details.get("scanned_notice_image"):
        from .adapters.notice_ocr import parse_notice_image
        ocr_result = parse_notice_image(details["scanned_notice_image"])
        if ocr_result.status in ("success", "no_debt_found"):
            ocr = {
                "foreclosing_lender": ocr_result.foreclosing_lender,
                "substitute_trustee": ocr_result.substitute_trustee,
                "scheduled_sale_time": ocr_result.scheduled_sale_time,
                "estimated_default_balance": ocr_result.estimated_default_balance,
                "original_principal": ocr_result.original_principal,
                "unpaid_principal_balance": ocr_result.unpaid_principal_balance,
                "all_extracted_balances": ocr_result.all_extracted_balances,
                "status": "Success" if ocr_result.status == "success" else "NoDebt",
            }

    if ocr.get("status") == "Success" or ocr.get("estimated_default_balance", 0) > 0:
        cpr.set_field("foreclosing_lender", ocr.get("foreclosing_lender", "Unknown"), source, base_confidence)
        cpr.set_field("substitute_trustee", ocr.get("substitute_trustee", "Unknown"), source, base_confidence)
        cpr.set_field("scheduled_sale_time", ocr.get("scheduled_sale_time", "Unknown"), source, base_confidence)
        cpr.set_field("original_principal", ocr.get("original_principal"), source, base_confidence)
        cpr.set_field("unpaid_principal_balance", ocr.get("unpaid_principal_balance"), source, base_confidence)
        cpr.set_field("all_extracted_balances", ocr.get("all_extracted_balances", []), source, base_confidence)

        # Lien: the foreclosing mortgage
        debt = ocr.get("estimated_default_balance", 0)
        if debt > 0:
            cpr.add_lien(LienRecord(
                lien_type="mortgage",
                amount=debt,
                lender=ocr.get("foreclosing_lender", "Unknown"),
                priority=1,
                survives_foreclosure=False,
                survives_tax_sale=False,
            ), f"{source}_ocr")

    # Distress state
    cpr.deal_type = "DT-AUCTION"
    cpr.set_field("sale_date", prop.get("Deed Date", "Unknown"), source, base_confidence * 0.8)

    return cpr


def enrich_from_collin_details(lead: NormalizedLead) -> CPR:
    """Enrich a CPR from Collin County foreclosure detail page data."""
    raw = lead.raw_data.get("details", {})
    prop = raw.get("property_info", {})

    cpr = CPR(
        parcel_id=lead.parcel_id,
        county="collin",
        situs_address=prop.get("Address", lead.situs_address),
        channel="foreclosure_notice",
    )

    # GIS coordinates as provenance fields
    if lead.latitude and lead.longitude:
        cpr.gis_lat = lead.latitude
        cpr.gis_lng = lead.longitude
        cpr.set_field("gis_lat", lead.latitude, "collin_foreclosure_detail", 0.95)
        cpr.set_field("gis_lng", lead.longitude, "collin_foreclosure_detail", 0.95)

    _enrich_common(cpr, raw, "collin_foreclosure_detail", 0.95)

    # Collin-specific: Mapsco, Deed Date, scanned notice image
    cpr.set_field("mapsco", raw.get("property_info", {}).get("Mapsco", "Unknown"),
                  "collin_foreclosure_detail", 0.50)

    cpr.move_column(CPRColumn.ENRICHED)
    return cpr


def enrich_from_dallas_details(lead: NormalizedLead) -> CPR:
    """Enrich a CPR from Dallas County foreclosure PDF data.

    Dallas PDFs provide: address, sale date, borrower, lender, debt, trustee,
    sale time, and sometimes tax info and census demographics (from prior run).
    Does NOT provide: improvement details (sqft, beds, baths, year built) —
    those require DCAD cross-reference.
    """
    raw = lead.raw_data
    details = raw.get("details", {})
    prop = details.get("property_info", {})

    cpr = CPR(
        parcel_id=lead.parcel_id,
        county="dallas",
        situs_address=prop.get("Address", lead.situs_address),
        channel="foreclosure_notice",
    )

    # GIS coordinates (may be 0.0 if not geocoded yet)
    if lead.latitude and lead.longitude and lead.latitude != 0:
        cpr.gis_lat = lead.latitude
        cpr.gis_lng = lead.longitude
        cpr.set_field("gis_lat", lead.latitude, "dallas_census_geocoder", 0.90)
        cpr.set_field("gis_lng", lead.longitude, "dallas_census_geocoder", 0.90)

    # City from lead (parsed from PDF filename)
    if lead.raw_data.get("city"):
        cpr.set_field("city", lead.raw_data.get("city"), "dallas_foreclosure_pdf", 0.70)

    _enrich_common(cpr, details, "dallas_foreclosure_pdf", 0.70)

    # DCAD cross-reference: if no improvement data, try DCAD
    # ponytail: DCAD requires Playwright (browser) — slow, so only run on-demand
    # not during bulk enrichment. Call cross_reference_dcad(cpr) explicitly.
    imp = details.get("improvement_info", {})
    if imp.get("Improvement Area") in (None, "Unknown", "") and cpr.situs_address != "Unknown":
        cpr.set_field("dcad_cross_ref_needed", True, "enrichment", 1.0)

    cpr.move_column(CPRColumn.ENRICHED)
    return cpr


def cross_reference_dcad(cpr: CPR) -> CPR:
    """Cross-reference a Dallas CPR with DCAD for property characteristics.

    Call this on-demand for individual deals, not during bulk enrichment.
    Requires Playwright (browser automation).
    """
    if cpr.county != "dallas" or cpr.situs_address == "Unknown":
        return cpr

    from .dallas_cad import DallasCADAdapter
    adapter = DallasCADAdapter()
    result = adapter.fetch(address=cpr.situs_address)

    if result.get("status") in ("parsed",) and "error" not in result:
        if result.get("improvement_sqft"):
            cpr.set_field("improvement_sqft", _parse_int(result["improvement_sqft"]),
                          "dallas_cad", 0.90)
        if result.get("beds"):
            cpr.set_field("beds", _parse_int(result["beds"]), "dallas_cad", 0.90)
        if result.get("baths"):
            cpr.set_field("baths", _parse_float(result["baths"]), "dallas_cad", 0.90)
        if result.get("year_built"):
            cpr.set_field("year_built", _parse_int(result["year_built"]), "dallas_cad", 0.90)
        if result.get("cad_appraised_value"):
            cpr.set_field("cad_appraised_value", _parse_float(result["cad_appraised_value"]),
                          "dallas_cad", 0.90)
        cpr.set_field("dcad_cross_ref_needed", False, "dallas_cad", 1.0)

    return cpr


def enrich_from_raw_record(record: dict) -> CPR:
    """Enrich a CPR directly from a lucid-noether format record dict.

    Used to load the 335-record prior dataset for full portfolio analysis.
    """
    county = record.get("county", "unknown").lower()
    details = record.get("details", {})
    prop = details.get("property_info", {})

    cpr = CPR(
        parcel_id=str(record.get("entry_id", "")),
        county=county,
        situs_address=prop.get("Address", record.get("property_address", "Unknown")),
        channel="foreclosure_notice",
    )

    # GIS coordinates
    lat = record.get("latitude")
    lng = record.get("longitude")
    if lat and lng and lat != 0:
        cpr.gis_lat = lat
        cpr.gis_lng = lng
        cpr.set_field("gis_lat", lat, f"{county}_source", 0.90)
        cpr.set_field("gis_lng", lng, f"{county}_source", 0.90)

    # City
    if record.get("city"):
        cpr.set_field("city", record.get("city"), f"{county}_source", 0.70)

    source = f"{county}_foreclosure"
    confidence = 0.95 if county == "collin" else 0.70
    _enrich_common(cpr, details, source, confidence)

    cpr.move_column(CPRColumn.ENRICHED)
    return cpr


def enrich_lead(lead: NormalizedLead) -> CPR:
    """Route enrichment based on county/source."""
    if lead.county == "collin" and "details" in lead.raw_data:
        return enrich_from_collin_details(lead)
    elif lead.county == "dallas":
        return enrich_from_dallas_details(lead)
    else:
        cpr = CPR(
            parcel_id=lead.parcel_id,
            county=lead.county,
            situs_address=lead.situs_address,
            channel=lead.channel,
        )
        cpr.set_field("owner_name", lead.owner_name or "Unknown",
                       f"{lead.county}_source", 0.50)
        cpr.move_column(CPRColumn.ENRICHED)
        return cpr


if __name__ == "__main__":
    # Self-check: enrich a Collin lead from fixture data with ALL fields
    fixture = NormalizedLead(
        county="collin", parcel_id="4003592", channel="foreclosure_notice",
        situs_address="10022 Plainsman Ln Frisco, TX 75035",
        latitude=33.17922234, longitude=-96.79394064,
        raw_data={"details": {
            "property_info": {
                "Address": "10022 Plainsman Ln Frisco, TX 75035",
                "Type": "Residential Single Family (A1)",
                "Mapsco": "Unknown",
                "Deed Date": "7/25/2013",
                "Deed Number": "20130806001104940",
                "Legal Description": "Village At Panther Creek Phase One, The (Cfr), Blk N, Lot 32",
                "Commercial": "False",
            },
            "improvement_info": {
                "State Property Code": "Residential Single Family (A1)",
                "Land Area": "Unknown",
                "Improvement Area": "4,332 sq ft",
                "Year Built": "2004",
                "Stories": "2",
                "Bedrooms": "5",
                "Bathrooms": "4",
                "Pool": "N",
            },
            "jurisdiction_info": {
                "Taxing Entities": "Collin County (GCN) / Collin Co Community College (JCN) / Frisco ISD (SFR) / Frisco City (CFR)",
            },
            "owner_info": {
                "Name": "Islam Mazhar & Ambreen",
                "Address": "10022 Plainsman Ln / Frisco, TX 75035-6919",
            },
            "tax_info": {
                "Appraised Value (2025)": "$749,180.00",
                "Appraised Value (2026)": "$730,673.00",
            },
            "census_demographics": {
                "state_fips": "48", "county_fips": "085",
                "census_tract": "030520", "block_group": "1", "block": "1013",
                "status": "Success",
            },
            "scanned_notice_ocr_data": {
                "foreclosing_lender": "Specialized Loan Servicing LLC",
                "substitute_trustee": "Robert LaMont, Substitute Trustee",
                "scheduled_sale_time": "10:00 AM (or within three hours thereafter)",
                "estimated_default_balance": 314189.39,
                "original_principal": 298479.92,
                "unpaid_principal_balance": 314189.39,
                "all_extracted_balances": [298479.92, 314189.39],
                "status": "Success",
            },
        }},
    )

    cpr = enrich_from_collin_details(fixture)
    assert cpr.situs_address == "10022 Plainsman Ln Frisco, TX 75035"
    assert cpr.owner_name.value == "Islam Mazhar & Ambreen"
    assert cpr.improvement_sqft.value == 4332
    assert cpr.beds.value == 5
    assert cpr.baths.value == 4.0
    assert cpr.year_built.value == 2004
    assert cpr.stories.value == 2
    assert cpr.pool.value == False
    assert cpr.cad_appraised_value.value == 730673.0
    assert cpr.cad_appraised_value_2025.value == 749180.0
    assert cpr.isd.value == "Frisco ISD"
    assert cpr.deed_number.value == "20130806001104940"
    assert cpr.commercial.value == False
    assert cpr.census_tract.value == "030520"
    assert cpr.census_block_group.value == "1"
    assert cpr.county_fips.value == "085"
    assert cpr.state_fips.value == "48"
    assert cpr.foreclosing_lender.value == "Specialized Loan Servicing LLC"
    assert cpr.substitute_trustee.value == "Robert LaMont, Substitute Trustee"
    assert cpr.scheduled_sale_time.value == "10:00 AM (or within three hours thereafter)"
    assert cpr.original_principal.value == 298479.92
    assert cpr.unpaid_principal_balance.value == 314189.39
    assert cpr.all_extracted_balances.value == [298479.92, 314189.39]
    assert cpr.gis_lat.value == 33.17922234
    assert cpr.gis_lng.value == -96.79394064
    assert len(cpr.liens) == 1
    assert cpr.liens[0].amount == 314189.39
    assert cpr.liens[0].lender == "Specialized Loan Servicing LLC"
    assert cpr.column == CPRColumn.ENRICHED
    assert len(cpr.ledger) > 20  # many fields set now

    # Test enrich_from_raw_record with Dallas data
    dallas_record = {
        "entry_id": "DAL_March_Addison_1",
        "property_address": "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334",
        "latitude": 32.964465474806,
        "longitude": -96.825153337622,
        "county": "Dallas",
        "city": "Addison",
        "details": {
            "property_info": {"Address": "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334",
                              "Type": "Residential Single Family (A1)",
                              "Deed Date": "Tuesday, the 3rd day of March, 2026",
                              "Commercial": "False"},
            "improvement_info": {"Improvement Area": "Unknown", "Year Built": "Unknown",
                                 "Bedrooms": "Unknown", "Bathrooms": "Unknown"},
            "jurisdiction_info": {"Taxing Entities": "Dallas County / City of Addison"},
            "owner_info": {"Name": "KERRY P. GRAY SEPARATED",
                           "Address": "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334"},
            "tax_info": {"Appraised Value (2025)": "$371,520.00",
                         "Appraised Value (2026)": "$387,000.00"},
            "census_demographics": {"state_fips": "48", "county_fips": "113",
                                    "census_tract": "013628", "block_group": "2",
                                    "block": "2006", "status": "Success"},
            "scanned_notice_ocr_data": {"foreclosing_lender": "CTX MORTGAGE COMPANY",
                                        "substitute_trustee": "John Beazley, Logan Thomas",
                                        "scheduled_sale_time": "10:00 AM or within three hours thereafter.",
                                        "estimated_default_balance": 309600.0,
                                        "original_principal": 309600.0,
                                        "all_extracted_balances": [309600.0],
                                        "status": "Success"},
        },
    }
    cpr2 = enrich_from_raw_record(dallas_record)
    assert cpr2.county == "dallas"
    assert cpr2.situs_address == "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334"
    assert cpr2.owner_name.value == "KERRY P. GRAY SEPARATED"
    assert cpr2.city.value == "Addison"
    assert cpr2.foreclosing_lender.value == "CTX MORTGAGE COMPANY"
    assert cpr2.liens[0].amount == 309600.0
    assert cpr2.census_tract.value == "013628"
    assert cpr2.isd.value == "Unknown"  # Dallas tax entities don't include ISD
    assert cpr2.cad_appraised_value.value == 387000.0

    print(f"enrichment.py: Collin — {len(cpr.ledger)} ledger entries (was ~13, now captures all fields)")
    print(f"enrichment.py: Dallas — {len(cpr2.ledger)} ledger entries")
    print("enrichment.py: all self-checks passed")