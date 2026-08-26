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


def _homestead_from_taxing(taxing: str) -> bool:
    """AUDIT C1 fix: token-match the parenthesized exemption code or the
    whole word HOMESTEAD. The old substring test ('HS' in taxing.upper())
    flagged e.g. "City of Sachse" (contains the letters h+s) as homestead,
    silently flipping the redemption class from 180-day/25% to 730-day/50%.
    """
    return bool(re.search(r"\(HS\)|\bHOMESTEAD\b", taxing or "", re.IGNORECASE))


def extract_isd(taxing_entities: str) -> str:
    """Extract ISD (school district) from taxing entities string."""
    if not taxing_entities:
        return "Unknown"
    m = re.search(r'([A-Za-z0-9\s]+ISD)\b', taxing_entities)
    return m.group(1).strip() if m else "Unknown"


# Backwards-compatible alias for the original private name.
_extract_isd = extract_isd


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

    # Homestead/ag flags from jurisdiction info.
    # C1 fix: token-match the parenthesized exemption code or the whole word
    # HOMESTEAD. Substring matching ("HS" in ...) flagged e.g. "City of
    # Sachse" (contains the letters h+s) as homestead, silently flipping the
    # tax-deed redemption class from 180-day/25% to 730-day/50%.
    cpr.set_field(
        "homestead",
        _homestead_from_taxing(taxing),
        source, 0.70,
    )

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

    # None-safe: adapters now correctly emit None (never fabricated zeros) — audit C5/C7
    debt = ocr.get("estimated_default_balance") or 0
    if ocr.get("status") == "Success" or debt > 0:
        cpr.set_field("foreclosing_lender", ocr.get("foreclosing_lender", "Unknown"), source, base_confidence)
        cpr.set_field("substitute_trustee", ocr.get("substitute_trustee", "Unknown"), source, base_confidence)
        cpr.set_field("scheduled_sale_time", ocr.get("scheduled_sale_time", "Unknown"), source, base_confidence)
        cpr.set_field("original_principal", ocr.get("original_principal"), source, base_confidence)
        cpr.set_field("unpaid_principal_balance", ocr.get("unpaid_principal_balance"), source, base_confidence)
        cpr.set_field("all_extracted_balances", ocr.get("all_extracted_balances", []), source, base_confidence)

        # Lien: the foreclosing mortgage (None-safe — audit C5)
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
    # X5 fix: Collin's "Deed Date" is the OWNER-ACQUISITION date, not the
    # foreclosure-sale date. Storing it in sale_date corrupted dedupe keys and
    # made stale-notice checks compare the wrong event (audit H6). It is now
    # kept in its own provenance field, owner_deed_date; Collin sale_date
    # stays unset unless a true foreclosure-sale date ever surfaces. Dallas's
    # prose date IS the trustee-sale date, so Dallas keeps sale_date.
    deed_date = prop.get("Deed Date", "Unknown")
    if (getattr(cpr, "county", "") or "").lower() == "collin":
        cpr.set_field("owner_deed_date", deed_date, source, base_confidence * 0.8)
    else:
        cpr.set_field("sale_date", deed_date, source, base_confidence * 0.8)

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

    # RL7/S6: a flagged-failed detail scrape is an error payload, NOT data —
    # receipt the failure and skip property extraction entirely instead of
    # fabricating "Unknown" characteristics from .get() defaults.
    if _details_scrape_failed(lead.raw_data):
        _mark_detail_scrape_error(cpr, lead.raw_data, "collin_foreclosure_detail")
    else:
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

    # RL7/S6: same failed-scrape guard as the Collin path — mark, don't
    # extract (city/GIS above come from the lead/filename, not the payload).
    if _details_scrape_failed(raw):
        _mark_detail_scrape_error(cpr, raw, "dallas_foreclosure_pdf")
    else:
        _enrich_common(cpr, details, "dallas_foreclosure_pdf", 0.70)

        # DCAD cross-reference: if no improvement data, flag for on-demand lookup.
        # DCAD requires Playwright (browser automation) — slow — so it is never
        # run during bulk enrichment; call cross_reference_dcad(cpr) explicitly.
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
    # RL7/S6: legacy dataset rows can carry the details_status flag too —
    # never extract characteristics out of a failed-scrape error payload.
    if _details_scrape_failed(record):
        _mark_detail_scrape_error(cpr, record, source)
    else:
        _enrich_common(cpr, details, source, confidence)

    cpr.move_column(CPRColumn.ENRICHED)
    return cpr


def normalize_deal_identity(county, address) -> tuple[str, str]:
    """Canonical merge key for current_deals.json rows: (county.lower(),
    upper-collapsed address). Shared by find_deals.py's merge-on-write and
    pipeline.py's DCAD write-back so both match records identically.
    """
    return (
        str(county or "").strip().lower(),
        " ".join(str(address or "").upper().split()),
    )


def _clean_flat_value(val):
    """Normalize a flat current_deals.json value: None for missing data.

    Flat rows use Python Nones, empty strings and the literal "Unknown" as
    placeholders — none of those may reach a ProvenanceField.
    """
    if val is None:
        return None
    if isinstance(val, str):
        stripped = val.strip()
        if not stripped or stripped.lower() in ("unknown", "n/a", "none"):
            return None
        return stripped
    return val


# ---------------------------------------------------------------------------
# RL7/S6: failed detail scrapes. collin_foreclosure flags them by writing
# raw_data["details_status"]="error" (the payload itself is {"error": ...},
# NOT parsed data). Consumers must receipt the failure on the CPR and skip
# property-field extraction — an error payload's .get() defaults would
# otherwise enrich to ENRICHED full of fabricated "Unknown"s.
DETAILS_STATUS_ERROR = "error"


def _details_scrape_failed(raw: object) -> bool:
    """True when the record/lead carries the details_status="error" flag."""
    return isinstance(raw, dict) and \
        str(raw.get("details_status") or "").lower() == DETAILS_STATUS_ERROR


def _mark_detail_scrape_error(cpr: CPR, raw: dict, source: str) -> None:
    """Receipt the failed scrape as a provenance error marker (RL7).

    The ledger entry makes "detail data unavailable" explicit — downstream
    readers see the failure instead of mistaking skipped fields for real
    Unknowns. The underlying scrape message rides along when present.
    """
    err = "detail scrape failed"
    details = raw.get("details") if isinstance(raw, dict) else None
    if isinstance(details, dict) and details.get("error"):
        err = f"detail scrape failed: {details['error']}"
    elif isinstance(raw, dict) and raw.get("details_error"):
        err = f"detail scrape failed: {raw['details_error']}"
    cpr.set_field("detail_scrape_error", err, source, 1.0)


# TD9-flatmap: declarative flat-row mapping. One table, one loop — each entry
# is (record_key, cpr_field, parser, confidence, extra):
#   record_key  key read off the flat current_deals.json row
#   cpr_field   provenance-managed CPR attribute to receive it
#   parser      cleaner/converter applied to the cleaned value; None =
#               passthrough string
#   confidence  provenance strength recorded on the receipt
#   extra       optional modifier tag:
#                 "gate_on_clean" — gate on the cleaned row value instead of
#                   the parsed one (pool: "N"/"False" must still land False)
#                 "wrap_str" — store str(value) (census fields stay strings)
_FLAT_SOURCE_CONFIDENCE = 0.70  # flat-row provenance strength

_FLAT_FIELD_MAP = [
    # --- characteristics ---------------------------------------------------
    ("sqft", "improvement_sqft", _parse_int, _FLAT_SOURCE_CONFIDENCE, None),
    ("beds", "beds", _parse_int, _FLAT_SOURCE_CONFIDENCE, None),
    ("baths", "baths", _parse_float, _FLAT_SOURCE_CONFIDENCE, None),
    ("year_built", "year_built", _parse_int, _FLAT_SOURCE_CONFIDENCE, None),
    ("stories", "stories", _parse_int, _FLAT_SOURCE_CONFIDENCE, None),
    ("pool", "pool", _parse_bool, _FLAT_SOURCE_CONFIDENCE, "gate_on_clean"),
    # --- owner / property context ------------------------------------------
    ("owner", "owner_name", None, _FLAT_SOURCE_CONFIDENCE, None),
    ("property_type", "property_type", None, _FLAT_SOURCE_CONFIDENCE, None),
    # --- CAD valuations (baseline provenance fields, not market ARV) -------
    ("cad_value", "cad_appraised_value", _parse_float,
     _FLAT_SOURCE_CONFIDENCE, None),
    ("cad_value_2025", "cad_appraised_value_2025", _parse_float,
     _FLAT_SOURCE_CONFIDENCE, None),
    # --- jurisdiction -------------------------------------------------------
    ("city", "city", None, _FLAT_SOURCE_CONFIDENCE, None),
    # --- census context carried by earlier census/geocode passes ------------
    ("census_tract", "census_tract", None, 0.95, "wrap_str"),
    ("county_fips", "county_fips", None, 0.95, "wrap_str"),
    # --- trustee-sale date (X5-adjudicated upstream; owner-deed dates never
    #     surface here) — reduced confidence like the details-based paths ----
    ("sale_date", "sale_date", None, _FLAT_SOURCE_CONFIDENCE * 0.8, None),
]


def enrich_from_flat_record(record: dict) -> CPR:
    """Enrich a CPR from a FLAT current_deals.json record (AUDIT D1 fix).

    current_deals.json rows are flat dicts written by find_deals.analyze_deal
    (address / city / county / parcel_id / sqft / beds / ...). The legacy
    enrich_from_raw_record expects the OLD nested lucid-noether schema
    (details/entry_id/property_address), so routing flat rows through it
    resolved every field to "Unknown" and --bulk-dcad silently no-op'd.

    Mapping: parcel_id → identity, county lowercased, address → situs; every
    previously-handled field rides the declarative _FLAT_FIELD_MAP above
    (cleaned → parsed → gated → set_field receipt). mortgage_debt → estimated
    mortgage lien via the same pattern as the OCR path in _enrich_common
    (ONLY when a positive number), lat/lng when both nonzero.

    None-safety contract: fields the record lacks are never set, so the
    attribute stays None — callers use ``cpr.get(name, default)`` or keep the
    ``cpr.f.value if cpr.f else ...`` idiom. Records flagged
    details_status="error" (failed scrapes, RL7/S6) get the error marker and
    NO property extraction at all — nothing from an error payload may become
    a fabricated characteristic or lien.
    """
    county = str(record.get("county") or "unknown").lower()
    source = f"{county}_source"
    confidence = _FLAT_SOURCE_CONFIDENCE

    cpr = CPR(
        parcel_id=str(record.get("parcel_id") or ""),
        county=county,
        situs_address=_clean_flat_value(record.get("address")) or "Unknown",
        channel="foreclosure_notice",
    )

    # RL7/S6: a flagged-failed scrape is an error payload, not data — mark it
    # and skip every property field below rather than fabricating Unknowns.
    if _details_scrape_failed(record):
        _mark_detail_scrape_error(cpr, record, source)
        cpr.deal_type = "DT-AUCTION"
        cpr.move_column(CPRColumn.ENRICHED)
        return cpr

    # Characteristics / owner / CAD / census / sale date — the mapped table.
    for key, cpr_field, parser, field_confidence, extra in _FLAT_FIELD_MAP:
        cleaned = _clean_flat_value(record.get(key))
        if cleaned is None:
            continue
        value = parser(cleaned) if parser is not None else cleaned
        if extra == "wrap_str":
            value = str(value)
        usable = bool(cleaned) if extra == "gate_on_clean" else bool(value)
        if usable:
            cpr.set_field(cpr_field, value, source, field_confidence)

    # Estimated lien from the flat mortgage_debt — SAME pattern as the OCR
    # path in _enrich_common: only when it parses to a positive number, and
    # never fabricated otherwise. The lender rides along when present.
    debt = record.get("mortgage_debt")
    if isinstance(debt, (int, float)) and not isinstance(debt, bool) and debt > 0:
        lender = _clean_flat_value(record.get("lender")) or "Unknown"
        cpr.set_field("foreclosing_lender", lender, source, confidence)
        cpr.add_lien(LienRecord(
            lien_type="mortgage",
            amount=float(debt),
            lender=lender,
            priority=1,
            survives_foreclosure=False,
            survives_tax_sale=False,
        ), source)

    # Geocoded coordinates — only when both parse and are nonzero.
    lat_raw, lng_raw = record.get("latitude"), record.get("longitude")
    try:
        lat = float(lat_raw) if lat_raw not in (None, "") else None
        lng = float(lng_raw) if lng_raw not in (None, "") else None
    except (TypeError, ValueError):
        lat = lng = None
    if lat and lng and lat != 0 and lng != 0:
        cpr.gis_lat = lat
        cpr.gis_lng = lng
        cpr.set_field("gis_lat", lat, source, 0.90)
        cpr.set_field("gis_lng", lng, source, 0.90)

    # Distress state + DCAD flag (mirrors enrich_from_dallas_details).
    cpr.deal_type = "DT-AUCTION"
    sqft = cpr.improvement_sqft.value if cpr.improvement_sqft else 0
    if county == "dallas" and not sqft and cpr.situs_address != "Unknown":
        cpr.set_field("dcad_cross_ref_needed", True, "enrichment", 1.0)

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

    # X5: Collin "Deed Date" is the owner-acquisition date — owner_deed_date,
    # NOT sale_date (sale_date stays unset so dedupe/timelines stay honest).
    assert cpr.owner_deed_date.value == "7/25/2013"
    assert cpr.sale_date is None

    # C1 regression: homestead exemption code must be token-matched.
    # "City of Sachse" contains the letters h+s — substring matching flagged
    # Sachse properties homestead, flipping redemption to 730-day/50%.
    def _homestead_for(taxing: str) -> bool:
        probe = CPR(parcel_id="probe", county="collin", situs_address="1 Probe St")
        _enrich_common(probe, {
            "property_info": {},
            "jurisdiction_info": {"Taxing Entities": taxing},
        }, "self_check", 0.9)
        return probe.homestead.value

    assert _homestead_for(
        "Collin County (GCN) / City of Sachse (CFR) / Wylie ISD (SFR)") is False, \
        "Sachse must NOT be flagged homestead"
    assert _homestead_for("Collin County (GCN) / 1234 Smith HS Rd (CFR)") is False, \
        "bare HS letters must NOT be flagged homestead"
    assert _homestead_for("Collin County (GCN) / General Exemption (HS) (AG)") is True, \
        "(HS) exemption code must be flagged homestead"
    assert _homestead_for("Tarrant County / HOMESTEAD EXEMPTION ON FILE") is True, \
        "word HOMESTEAD must be flagged homestead"
    assert cpr.homestead.value is False  # Frisco fixture has no exemption code

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
    # X5: Dallas's prose date IS the trustee-sale date — stays in sale_date.
    assert cpr2.sale_date.value == "Tuesday, the 3rd day of March, 2026"
    # TD1: owner_deed_date is now a declared field; Dallas leaves it unset (None)
    assert cpr2.get("owner_deed_date") is None  # never fabricated for Dallas

    # ---- D1 regression: FLAT current_deals.json rows -----------------------
    # Complete Dallas row — every field must land on the CPR.
    flat_complete = {
        "address": "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334",
        "city": "Addison", "county": "dallas",
        "parcel_id": "DAL_March_Addison_1",
        "owner": "KERRY P. GRAY SEPARATED",
        "property_type": "Residential Single Family (A1)",
        "sqft": 1826, "beds": 3, "baths": 2.0, "year_built": 1983,
        "stories": 1, "pool": False,
        "cad_value": 387000.0, "cad_value_2025": 371520.0,
        "mortgage_debt": 309600.0, "lender": "CTX MORTGAGE COMPANY",
        "sale_date": "Tuesday, the 3rd day of March, 2026",
        "latitude": 32.964465474806, "longitude": -96.825153337622,
        "census_tract": "013628", "county_fips": "113",
    }
    fc = enrich_from_flat_record(flat_complete)
    assert fc.parcel_id == "DAL_March_Addison_1"
    assert fc.county == "dallas" and fc.situs_address.startswith("15806 BREEDLOVE")
    assert fc.improvement_sqft.value == 1826
    assert fc.beds.value == 3 and fc.baths.value == 2.0
    assert fc.year_built.value == 1983 and fc.stories.value == 1
    assert fc.cad_appraised_value.value == 387000.0
    assert fc.cad_appraised_value_2025.value == 371520.0
    assert fc.city.value == "Addison"
    assert fc.owner_name.value == "KERRY P. GRAY SEPARATED"
    assert fc.foreclosing_lender.value == "CTX MORTGAGE COMPANY"
    assert len(fc.liens) == 1 and fc.liens[0].amount == 309600.0
    assert fc.liens[0].lender == "CTX MORTGAGE COMPANY"
    assert fc.gis_lat.value == 32.964465474806
    assert fc.gis_lng.value == -96.825153337622
    assert fc.census_tract.value == "013628" and fc.county_fips.value == "113"
    assert fc.sale_date.value == "Tuesday, the 3rd day of March, 2026"
    assert fc.improvement_sqft.source == "dallas_source"
    assert fc.improvement_sqft.confidence == 0.70  # dallas source-field confidence
    assert fc.column == CPRColumn.ENRICHED
    assert fc.get("dcad_cross_ref_needed") is None  # sqft present → no flag (TD1 declared)

    # Sparse row with placeholder junk — must stay None-safe (no fabrication).
    flat_sparse = {
        "address": "123 Nowhere Ave", "county": "Dallas",  # county case-insensitive
        "city": "", "sqft": None, "beds": "Unknown", "baths": "",
        "year_built": None, "cad_value": None, "cad_value_2025": "Unknown",
        "mortgage_debt": None, "lender": None,
        "latitude": 0, "longitude": None, "parcel_id": "",
    }
    fs = enrich_from_flat_record(flat_sparse)
    assert fs.county == "dallas" and fs.parcel_id == ""
    assert fs.situs_address == "123 Nowhere Ave"
    for unset in ("improvement_sqft", "beds", "baths", "year_built", "stories",
                  "city", "owner_name", "cad_appraised_value",
                  "cad_appraised_value_2025", "sale_date"):
        # Undeclared CPR extras (stories/city/...) don't even exist until set:
        # None-safety means absent OR None, never a fabricated placeholder.
        assert getattr(fs, unset, None) is None, f"{unset} should be unset (None-safe)"
    assert len(fs.liens) == 0, "must never fabricate a lien without positive debt"
    assert fs.gis_lat is None and fs.gis_lng is None  # zero lat/lng never set
    assert fs.dcad_cross_ref_needed.value is True  # Dallas + no sqft → flag
    assert fs.column == CPRColumn.ENRICHED

    # normalize_deal_identity: shared merge key (find_deals merge-on-write /
    # pipeline DCAD write-back must agree on identity).
    assert normalize_deal_identity("Dallas", "15806  Breedlove   Place") == \
        normalize_deal_identity("dallas", "15806 BREEDLOVE PLACE")

    print(f"enrichment.py: Collin — {len(cpr.ledger)} ledger entries (was ~13, now captures all fields)")
    print(f"enrichment.py: Dallas — {len(cpr2.ledger)} ledger entries")
    print(f"enrichment.py: flat rows — complete={len(fc.ledger)} entries, "
          f"sparse None-safe (no fabricated fields/lien), D1 mapping OK")
    print("enrichment.py: all self-checks passed")