"""TD9 memo-section + TD9-flatmap/RL7 regression tests.

Locks the decomposed render_memo section pipeline to byte-identical golden
output (captured from the PRE-refactor monolith under a dual frozen clock),
proves every section helper is individually callable and invoked in order,
pins enrich_from_flat_record's declarative _FLAT_FIELD_MAP to the old
hand-rolled behavior (literal expected dicts captured before the refactor),
and covers RL7/S6: details_status="error" records get an error provenance
marker and NO property extraction (never fabricate Unknowns from failed
scrapes).
"""
from datetime import datetime, timezone

import pytest

import src.deal_memo as deal_memo
import src.valuation as valuation
from src.adapters.base import NormalizedLead
from src.cpr import (BlastTier, CPR, CPRColumn, CompRecord, LienRecord,
                     ProvenanceField)
from src.enrichment import (_FLAT_FIELD_MAP, enrich_from_collin_details,
                            enrich_from_dallas_details,
                            enrich_from_flat_record, enrich_from_raw_record)
from src.deal_memo import render_memo
from src.pricing import PricingResult, SensitivityCell
from src.reviewer import ReviewResult
from src.underwriting import UnderwriteResult, flip_pnl

# The instant both module clocks are frozen to: deal_memo renders its header
# stamp + LEGAL-RULES verification date from it, and gate_val scores comp
# recency against it (comps below are dated relative to THIS date).
FROZEN_NOW = datetime(2026, 3, 14, 9, 26, tzinfo=timezone.utc)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW


@pytest.fixture(autouse=True)
def frozen_clocks(monkeypatch):
    """Freeze deal_memo AND valuation clocks so goldens never drift."""
    monkeypatch.setattr(deal_memo, "datetime", _FrozenDateTime)
    monkeypatch.setattr(valuation, "datetime", _FrozenDateTime)


# ---------------------------------------------------------------------------
# Scenario builders (fixture-valued style, synthesized inline)
# ---------------------------------------------------------------------------

def build_fixture_cpr():
    """Fixture CPR: populated characteristics + lien + ProvenanceField(None)."""
    cpr = CPR(parcel_id="R4003592", county="collin",
              situs_address="10022 Plainsman Ln, Frisco, TX 75035",
              deal_type="DT-AUCTION", channel="foreclosure_notice")
    cpr.set_field("improvement_sqft", 4332, "collin_cad", 0.95)
    cpr.set_field("beds", 5, "collin_cad", 0.95)
    cpr.set_field("baths", 4.0, "collin_cad", 0.95)
    cpr.set_field("year_built", 2004, "collin_cad", 0.95)
    cpr.set_field("property_type", "Residential Single Family (A1)", "collin_cad", 0.95)
    cpr.set_field("cad_appraised_value", 730_673, "collin_cad", 0.90)
    cpr.set_field("sale_date", "2026-09-01", "collin_foreclosure", 0.80)
    # ProvenanceField(None) must render an em-dash row, never literal "None"
    cpr.set_field("homestead", None, "collin_cad", 0.0)
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=350_000, lender="Wells Fargo",
                            priority=1, survives_foreclosure=False), "collin_clerk")
    return cpr


def build_fixture_cpr_of_type(deal_type):
    return CPR(parcel_id="R4003592", county="collin",
               situs_address="10022 Plainsman Ln, Frisco, TX 75035",
               deal_type=deal_type, channel="foreclosure_notice")


def build_valued_cpr():
    """Fixture-valued CPR via the tests/test_memo_recommendation.py pattern."""
    from src.valuation import attach_valuation, gate_val
    cpr = CPR(parcel_id="P100", county="dallas",
              situs_address="15806 Breedlove Pl, Addison, TX 75001",
              deal_type="DT-FLIP", channel="foreclosure_notice")
    recent = datetime(2026, 3, 1).date().isoformat()  # 13d before FROZEN_NOW
    comps = [CompRecord(address=f"{i} X St", sale_price=750_000,
                        sale_date=recent, distance_miles=0.2,
                        source="mls_agent", adjusted_price=750_000)
             for i in range(5)]
    gate, val = gate_val(cpr, "mls_agent", comps, BlastTier.T2)
    val.as_is_value = ProvenanceField(value=690_000, source="mls_agent", confidence=0.82)
    val.rent_estimate = ProvenanceField(value=3_950, source="rental_avm", confidence=0.70)
    cpr = attach_valuation(cpr, val, gate)
    cpr.lien_lookup_status = "ok"
    return cpr


def uw_win():
    return flip_pnl(arv=750_000, purchase=450_000, rehab=50_000,
                    holding=5_000, financing=10_000, closing_buy=5_000,
                    closing_sell=60_000)


def uw_rental():
    return UnderwriteResult(
        deal_type="DT-FLIP", model="rental", arv=750_000, purchase_price=450_000,
        rehab=50_000, holding_costs=5_000, financing_costs=10_000,
        closing_costs_buy=5_000, closing_costs_sell=60_000,
        net_profit=120_000, roi=0.22, cash_invested=520_000,
        cap_rate=0.065, cash_on_cash=None, dscr=1.28,
    )


def pricing_alive():
    return PricingResult(
        deal_type="DT-AUCTION", mao=475_000, bid_ceiling=470_000,
        offer_ladder={"opening": 404_000, "target": 475_000, "walk_away": 470_000},
        blast_tier=BlastTier.T3,
        pencil_status="base",
        sensitivity=[SensitivityCell(arv_delta=0.0, rehab_delta=0.0,
                                     profit=100_000, margin=0.13)],
    )


def pricing_unknown():
    return PricingResult(deal_type="DT-FLIP", mao=500_000, bid_ceiling=480_000,
                         blast_tier=BlastTier.T2)


REVIEW_PASS = ReviewResult(overall="pass")


# ---------------------------------------------------------------------------
# 1. Golden-output equality — byte-identical to the pre-refactor monolith
#    (captured under the same dual frozen clock).
# ---------------------------------------------------------------------------

GOLDEN_FIXTURE_CPR = '# DEAL MEMO — R4003592 — 10022 Plainsman Ln, Frisco, TX 75035 — Collin County\n**Deal type:** DT-AUCTION | **Channel:** foreclosure_notice | **Date:** 2026-03-14 09:26 UTC\n**Sale date:** 2026-09-01 | **Blast tier:** T0\n\n## Property Snapshot\n| Field | Value | Source | Confidence |\n|-------|-------|--------|------------|\n| Address | 10022 Plainsman Ln, Frisco, TX 75035 | — | — |\n| Legal | — | — | — |\n| Type | Residential Single Family (A1) | collin_cad | 95% |\n| Sqft | 4332 | collin_cad | 95% |\n| Beds | 5 | collin_cad | 95% |\n| Baths | 4.0 | collin_cad | 95% |\n| Year Built | 2004 | collin_cad | 95% |\n| Homestead | — | — | — |\n\n## Distress & Legal State\n- **NOD/NOS status:** N/A\n- **Sale date:** 2026-09-01\n- **Opening/credit bid:** Unknown\n- **Redemption class:** N/A\n\n### Encumbrance Stack\n| Type | Amount | Lender | Priority | Survives FC | Survives Tax Sale |\n|------|--------|--------|----------|-------------|-------------------|\n| mortgage | $350,000 | Wells Fargo | 1 | No | No |\n\n## Valuation\n_Valuation not yet performed — GATE-VAL required._\n\n## Underwrite\n- **Model:** flip\n- **ARV:** $750,000\n- **Purchase:** $450,000\n- **Rehab:** $50,000 (contingency: 20%)\n- **Holding:** $5,000\n- **Financing:** $10,000\n- **Closing (buy+sell):** $65,000\n- **Net profit:** $160,000\n- **ROI:** 30.2%\n\n## Pricing\n- **MAO:** $475,000\n- **Bid ceiling:** $470,000\n- **Blast tier:** T3\n- **Offer ladder:** opening $404,000 → target $475,000 → walk-away $470,000\n- ✓ Pencils at base assumptions.\n\n### Sensitivity Grid (ARV × Rehab)\n| ARV Δ | Rehab Δ | Profit | Margin |\n|-------|---------|--------|--------|\n| +0% | +0% | $100,000 | 13.0% |\n\n## Exit & Disposition Plan\n- **Exit strategy:** flip\n- **Disposition:** fix-and-flip or BRRRR\n\n## Risk Register\n- Auction: no inspection, no title insurance, sight-unseen\n- Occupied property → eviction cost/time (3-day notice)\n\n## Compliance Checklist\n- [ ] Buyer eligibility verified (Written Statement / RealAuction registration)\n- [ ] GATE-LEGAL-LIEN: title/lien priority confirmed\n- [ ] LEGAL-RULES version verified (2026-03-14)\n\n## Recommendation\n**CONDITIONAL — HITL REVIEW** — GATE-VAL not run — ARV unknown\n\n### Gate Trail\n- GATE-VAL: not run\n- GATE-LEGAL-LIEN: lien stack present\n- AGENT-REV: pass\n- LEGAL-RULES: v1.0 (verified_by: spec_research)'

GOLDEN_VALUED_CPR = '# DEAL MEMO — P100 — 15806 Breedlove Pl, Addison, TX 75001 — Dallas County\n**Deal type:** DT-FLIP | **Channel:** foreclosure_notice | **Date:** 2026-03-14 09:26 UTC\n**Sale date:** N/A | **Blast tier:** T0\n\n## Property Snapshot\n| Field | Value | Source | Confidence |\n|-------|-------|--------|------------|\n| Address | 15806 Breedlove Pl, Addison, TX 75001 | — | — |\n| Legal | — | — | — |\n| Type | — | — | — |\n| Sqft | — | — | — |\n| Beds | — | — | — |\n| Baths | — | — | — |\n| Year Built | — | — | — |\n| Homestead | — | — | — |\n\n## Distress & Legal State\n- **NOD/NOS status:** N/A\n- **Sale date:** N/A\n- **Opening/credit bid:** Unknown\n- **Redemption class:** N/A\n\n### Encumbrance Stack\n_No liens recorded — GATE-LEGAL-LIEN required before bidding._\n\n## Valuation\n- **ARV:** $750,000 (source: mls_agent, confidence: 96%)\n- **As-is value:** $690,000\n- **Rent estimate:** $3,950/mo\n- **Comp source:** mls_agent\n- **GATE-VAL verdict:** pass — Confidence 0.96 >= floor 0.75\n- **Comp count:** 5\n\n## Underwrite\n- **Model:** rental\n- **ARV:** $750,000\n- **Purchase:** $450,000\n- **Rehab:** $50,000 (contingency: 20%)\n- **Holding:** $5,000\n- **Financing:** $10,000\n- **Closing (buy+sell):** $65,000\n- **Net profit:** $120,000\n- **ROI:** 22.0%\n- **Cap rate:** 6.50%\n- **Cash-on-cash:** — (equity basis unknown for financed deal)\n- **DSCR:** 1.28\n\n## Pricing\n- **MAO:** $500,000\n- **Bid ceiling:** $480,000\n- **Blast tier:** T2\n- ℹ️ **Pencil status unknown:** sensitivity grid is empty.\n\n## Exit & Disposition Plan\n- **Exit strategy:** rental\n- **Disposition:** fix-and-flip or BRRRR\n\n## Risk Register\n- No lien search performed — title risk unknown\n\n## Compliance Checklist\n- [ ] Buyer eligibility verified (Written Statement / RealAuction registration)\n- [ ] GATE-LEGAL-LIEN: title/lien priority confirmed\n- [ ] LEGAL-RULES version verified (2026-03-14)\n\n## Recommendation\n**CONDITIONAL — HITL REVIEW** — Pencil status unknown — sensitivity grid is empty\n\n### Gate Trail\n- GATE-VAL: pass\n- GATE-LEGAL-LIEN: not run\n- AGENT-REV: pass\n- LEGAL-RULES: v1.0 (verified_by: spec_research)'


def test_golden_output_fixture_style_cpr():
    """Winning fixture deal renders byte-identical to the pre-refactor memo."""
    memo = render_memo(build_fixture_cpr(), uw_win(), pricing_alive(), REVIEW_PASS)
    assert memo == GOLDEN_FIXTURE_CPR


def test_golden_output_valued_cpr():
    """Valued-CPR (gate_val pattern) + rental model renders byte-identically."""
    memo = render_memo(build_valued_cpr(), uw_rental(), pricing_unknown(), REVIEW_PASS)
    assert memo == GOLDEN_VALUED_CPR


# ---------------------------------------------------------------------------
# 2. Section helpers: individually callable, and called in declared order
# ---------------------------------------------------------------------------

SECTION_HELPERS = [
    "_render_header", "_render_snapshot", "_render_distress",
    "_render_valuation", "_render_underwrite", "_render_pricing",
    "_render_exit", "_render_risks", "_render_compliance",
    "_render_recommendation",
]


def test_section_helpers_called_in_order(monkeypatch):
    calls = []
    originals = {name: getattr(deal_memo, name) for name in SECTION_HELPERS}

    def wrapped(name, fn):
        def inner(lines, *args, **kwargs):
            calls.append(name)
            return fn(lines, *args, **kwargs)
        return inner

    for name, fn in originals.items():
        monkeypatch.setattr(deal_memo, name, wrapped(name, fn))

    render_memo(build_fixture_cpr(), uw_win(), pricing_alive(), REVIEW_PASS)
    assert calls == SECTION_HELPERS


def test_each_section_helper_callable_standalone():
    cpr = build_fixture_cpr()

    lines = []
    deal_memo._render_header(lines, cpr, now="2026-03-14 09:26 UTC",
                             sale="2026-09-01")
    assert lines[0] == ("# DEAL MEMO — R4003592 — 10022 Plainsman Ln, Frisco, "
                        "TX 75035 — Collin County")

    lines = []
    deal_memo._render_snapshot(lines, cpr)
    assert lines[0] == "## Property Snapshot"
    assert "| Homestead | — | — | — |" in lines

    lines = []
    deal_memo._render_distress(lines, cpr, sale="2026-09-01")
    assert "- **Sale date:** 2026-09-01" in lines
    assert "| mortgage | $350,000 | Wells Fargo | 1 | No | No |" in lines

    bare = CPR(parcel_id="B", county="tarrant", situs_address="1 B St")
    lines = []
    deal_memo._render_distress(lines, bare, sale="N/A")
    assert any("No liens recorded" in ln for ln in lines)

    lines = []
    deal_memo._render_valuation(lines, bare)
    assert "_Valuation not yet performed — GATE-VAL required._" in lines

    lines = []
    deal_memo._render_valuation(lines, build_valued_cpr())
    assert any(ln.startswith("- **ARV:** $750,000") for ln in lines)
    assert "- **Comp count:** 5" in lines

    lines = []
    deal_memo._render_underwrite(lines, uw_rental())
    assert "- **Cap rate:** 6.50%" in lines
    assert "- **Cash-on-cash:** — (equity basis unknown for financed deal)" in lines
    assert "- **DSCR:** 1.28" in lines

    lines = []
    deal_memo._render_pricing(lines, pricing_alive())
    assert "- **MAO:** $475,000" in lines
    assert ("- **Offer ladder:** opening $404,000 → target $475,000 → "
            "walk-away $470,000") in lines
    assert "### Sensitivity Grid (ARV × Rehab)" in lines

    lines = []
    deal_memo._render_exit(lines, build_fixture_cpr_of_type("DT-TAXDEED"), uw_win())
    assert "- **Disposition:** hold through redemption → quiet-title → sell/refi" in lines

    lines = []
    deal_memo._render_risks(lines, bare, pricing_unknown())
    assert "- No lien search performed — title risk unknown" in lines

    lines = []
    deal_memo._render_compliance(lines, build_fixture_cpr_of_type("DT-WHOLESALE"))
    assert any("§5.0205" in ln for ln in lines)
    assert "- [ ] LEGAL-RULES version verified (2026-03-14)" in lines

    lines = []
    deal_memo._render_recommendation(lines, build_fixture_cpr(), uw_win(),
                                     pricing_alive(), None)
    assert lines[0] == "## Recommendation"
    assert "**CONDITIONAL — HITL REVIEW**" in lines[1]
    assert "- AGENT-REV: not run" in lines


def test_render_exit_wholesale_branch():
    lines = []
    deal_memo._render_exit(lines, build_fixture_cpr_of_type("DT-WHOLESALE"), uw_win())
    assert ("- **Disposition:** assign contract to end buyer "
            "(requires bilateral disclosure)") in lines


# ---------------------------------------------------------------------------
# 3. Flat-map parity probe — literal expected dicts captured from the OLD
#    hand-rolled enrich_from_flat_record before the TD9 refactor.
# ---------------------------------------------------------------------------

FLAT_COMPLETE = {
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

# Literal expected dicts (value/source/confidence per field), transcribed
# from the pre-refactor implementation's output on these exact rows.
EXPECTED_FLAT_COMPLETE = {
    "baths": {"confidence": 0.7, "source": "dallas_source", "value": 2.0},
    "beds": {"confidence": 0.7, "source": "dallas_source", "value": 3},
    "cad_appraised_value": {"confidence": 0.7, "source": "dallas_source", "value": 387000.0},
    "cad_appraised_value_2025": {"confidence": 0.7, "source": "dallas_source", "value": 371520.0},
    "census_tract": {"confidence": 0.95, "source": "dallas_source", "value": "013628"},
    "city": {"confidence": 0.7, "source": "dallas_source", "value": "Addison"},
    "county_fips": {"confidence": 0.95, "source": "dallas_source", "value": "113"},
    "foreclosing_lender": {"confidence": 0.7, "source": "dallas_source", "value": "CTX MORTGAGE COMPANY"},
    "gis_lat": {"confidence": 0.9, "source": "dallas_source", "value": 32.964465474806},
    "gis_lng": {"confidence": 0.9, "source": "dallas_source", "value": -96.825153337622},
    "improvement_sqft": {"confidence": 0.7, "source": "dallas_source", "value": 1826},
    "owner_name": {"confidence": 0.7, "source": "dallas_source", "value": "KERRY P. GRAY SEPARATED"},
    "property_type": {"confidence": 0.7, "source": "dallas_source", "value": "Residential Single Family (A1)"},
    "sale_date": {"confidence": 0.5599999999999999, "source": "dallas_source",
                  "value": "Tuesday, the 3rd day of March, 2026"},
    "stories": {"confidence": 0.7, "source": "dallas_source", "value": 1},
    "year_built": {"confidence": 0.7, "source": "dallas_source", "value": 1983},
}
EXPECTED_FLAT_COMPLETE_LIENS = [
    {"amount": 309600.0, "lender": "CTX MORTGAGE COMPANY", "lien_type": "mortgage",
     "priority": 1, "survives_foreclosure": False, "survives_tax_sale": False},
]

EXPECTED_STRINGLY_COLLIN = {
    "baths": {"confidence": 0.7, "source": "collin_source", "value": 4.0},
    "beds": {"confidence": 0.7, "source": "collin_source", "value": 5},
    "cad_appraised_value": {"confidence": 0.7, "source": "collin_source", "value": 730673.0},
    "cad_appraised_value_2025": {"confidence": 0.7, "source": "collin_source", "value": 749180.0},
    "census_tract": {"confidence": 0.95, "source": "collin_source", "value": "030520"},
    "city": {"confidence": 0.7, "source": "collin_source", "value": "Frisco"},
    "county_fips": {"confidence": 0.95, "source": "collin_source", "value": "085"},
    "foreclosing_lender": {"confidence": 0.7, "source": "collin_source",
                           "value": "Specialized Loan Servicing LLC"},
    "gis_lat": {"confidence": 0.9, "source": "collin_source", "value": 33.17922234},
    "gis_lng": {"confidence": 0.9, "source": "collin_source", "value": -96.79394064},
    "improvement_sqft": {"confidence": 0.7, "source": "collin_source", "value": 4332},
    "owner_name": {"confidence": 0.7, "source": "collin_source",
                   "value": "Islam Mazhar & Ambreen"},
    # pool="N" parses to False yet MUST still land (gate_on_clean semantics)
    "pool": {"confidence": 0.7, "source": "collin_source", "value": False},
    "sale_date": {"confidence": 0.5599999999999999, "source": "collin_source",
                  "value": "7/25/2013"},
    "stories": {"confidence": 0.7, "source": "collin_source", "value": 2},
    "year_built": {"confidence": 0.7, "source": "collin_source", "value": 2004},
}
EXPECTED_STRINGLY_COLLIN_LIENS = [
    {"amount": 314189.39, "lender": "Specialized Loan Servicing LLC",
     "lien_type": "mortgage", "priority": 1,
     "survives_foreclosure": False, "survives_tax_sale": False},
]


def _snapshot(cpr):
    fields = {}
    for name, val in sorted(vars(cpr).items()):
        if isinstance(val, ProvenanceField):
            fields[name] = {"value": val.value, "source": val.source,
                            "confidence": val.confidence}
    gis_lat = cpr.gis_lat
    if isinstance(gis_lat, ProvenanceField):
        gis_lat = gis_lat.value
    gis_lng = cpr.gis_lng
    if isinstance(gis_lng, ProvenanceField):
        gis_lng = gis_lng.value
    return {
        "fields": fields,
        "parcel_id": cpr.parcel_id,
        "county": cpr.county,
        "situs_address": cpr.situs_address,
        "gis_lat": gis_lat,
        "gis_lng": gis_lng,
        "deal_type": cpr.deal_type,
        "column": cpr.column.value,
        "liens": [dict(lien_type=l.lien_type, amount=l.amount, lender=l.lender,
                       priority=l.priority,
                       survives_foreclosure=l.survives_foreclosure,
                       survives_tax_sale=l.survives_tax_sale)
                  for l in cpr.liens],
    }


def test_flat_map_complete_dallas_parity_with_old_behavior():
    snap = _snapshot(enrich_from_flat_record(FLAT_COMPLETE))
    assert snap["fields"] == EXPECTED_FLAT_COMPLETE
    assert snap["liens"] == EXPECTED_FLAT_COMPLETE_LIENS
    assert snap["parcel_id"] == "DAL_March_Addison_1"
    assert snap["county"] == "dallas"
    assert snap["situs_address"] == "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334"
    assert snap["gis_lat"] == 32.964465474806
    assert snap["deal_type"] == "DT-AUCTION"
    assert snap["column"] == "ENRICHED"


def test_flat_map_stringly_typed_collin_parity_with_old_behavior():
    rec = {
        "address": " 10022 Plainsman Ln Frisco TX 75035 ",
        "city": "Frisco ", "county": "COLLIN",
        "owner": "Islam Mazhar & Ambreen",
        "sqft": "4,332 sq ft", "beds": "5", "baths": "4",
        "year_built": "2004", "stories": "2", "pool": "N",
        "cad_value": "$730,673.00", "cad_value_2025": "$749,180.00",
        "census_tract": "030520", "county_fips": "085",
        "sale_date": "7/25/2013",
        "mortgage_debt": 314189.39, "lender": "Specialized Loan Servicing LLC",
        "latitude": "33.17922234", "longitude": "-96.79394064",
    }
    snap = _snapshot(enrich_from_flat_record(rec))
    assert snap["fields"] == EXPECTED_STRINGLY_COLLIN
    assert snap["liens"] == EXPECTED_STRINGLY_COLLIN_LIENS
    assert snap["county"] == "collin"
    assert snap["situs_address"] == "10022 Plainsman Ln Frisco TX 75035"


def test_flat_map_sparse_placeholders_parity_with_old_behavior():
    rec = {
        "address": "123 Nowhere Ave", "county": "Dallas",
        "city": "", "sqft": None, "beds": "Unknown", "baths": "",
        "year_built": None, "cad_value": None, "cad_value_2025": "Unknown",
        "mortgage_debt": None, "lender": None,
        "latitude": 0, "longitude": None, "parcel_id": "",
    }
    snap = _snapshot(enrich_from_flat_record(rec))
    # Old behavior: ONLY the Dallas DCAD flag survives placeholder junk.
    assert snap["fields"] == {
        "dcad_cross_ref_needed": {"confidence": 1.0, "source": "enrichment",
                                  "value": True},
    }
    assert snap["liens"] == []
    assert snap["parcel_id"] == "" and snap["county"] == "dallas"
    assert snap["situs_address"] == "123 Nowhere Ave"


PREVIOUSLY_HANDLED_FLAT_KEYS = {
    "sqft", "beds", "baths", "year_built", "stories", "pool",
    "owner", "property_type", "cad_value", "cad_value_2025", "city",
    "census_tract", "county_fips", "sale_date",
}


def test_flat_map_covers_every_previously_handled_key():
    mapped_keys = {entry[0] for entry in _FLAT_FIELD_MAP}
    missing = PREVIOUSLY_HANDLED_FLAT_KEYS - mapped_keys
    assert not missing, f"_FLAT_FIELD_MAP lost keys: {sorted(missing)}"
    for key, cpr_field, parser, confidence, extra in _FLAT_FIELD_MAP:
        assert isinstance(key, str) and isinstance(cpr_field, str)
        assert parser is None or callable(parser)
        assert 0.0 <= confidence <= 1.0
        assert extra in (None, "gate_on_clean", "wrap_str")


# ---------------------------------------------------------------------------
# 4. RL7/S6 — details_status="error" consumes the collin_foreclosure flag:
#    error marker ON, property extraction OFF.
# ---------------------------------------------------------------------------

PROPERTY_KEYS_FLAT = (
    "improvement_sqft", "beds", "baths", "year_built", "stories", "pool",
    "owner_name", "property_type", "cad_appraised_value",
    "cad_appraised_value_2025", "city", "census_tract", "county_fips",
    "sale_date", "foreclosing_lender", "gis_lat", "gis_lng",
)


def test_details_status_error_skips_flat_property_extraction():
    rec = dict(FLAT_COMPLETE, details_status="error",
               details={"error": "HTTP 500 fetching DetailPage"})
    cpr = enrich_from_flat_record(rec)
    assert cpr.get("detail_scrape_error") == \
        "detail scrape failed: HTTP 500 fetching DetailPage"
    assert any(e.field_name == "detail_scrape_error" for e in cpr.ledger)
    for name in PROPERTY_KEYS_FLAT:
        assert cpr.get(name) is None, f"{name} fabricated from a failed scrape"
    assert cpr.liens == [], "no lien may be synthesized from a failed scrape"
    assert cpr.deal_type == "DT-AUCTION"
    assert cpr.column == CPRColumn.ENRICHED


def test_details_status_ok_still_extracts_flat_fields():
    rec = dict(FLAT_COMPLETE, details_status="ok")
    cpr = enrich_from_flat_record(rec)
    assert cpr.get("detail_scrape_error") is None
    assert cpr.improvement_sqft.value == 1826


def _collin_error_lead():
    return NormalizedLead(
        county="collin", parcel_id="4003592", channel="foreclosure_notice",
        situs_address="10022 Plainsman Ln Frisco, TX 75035",
        latitude=33.17922234, longitude=-96.79394064,
        raw_data={"entry_id": 4003592, "details_status": "error",
                  "details": {"error": "connection refused"}},
    )


def test_details_status_error_skips_collin_property_extraction():
    cpr = enrich_from_collin_details(_collin_error_lead())
    assert cpr.get("detail_scrape_error") == \
        "detail scrape failed: connection refused"
    for name in ("property_type", "deed_number",
                 "commercial", "improvement_sqft", "land_sqft", "beds",
                 "baths", "year_built", "stories", "pool", "taxing_entities",
                 "isd", "owner_name", "owner_mailing_address",
                 "cad_appraised_value", "homestead", "mapsco"):
        assert cpr.get(name) is None, f"{name} fabricated from a failed scrape"
    # legal_description is a plain CPR constructor default, never enriched.
    assert cpr.legal_description == ""
    assert cpr.liens == []
    # Listing-level GIS comes from the SignalR push, NOT the detail page — kept.
    assert cpr.gis_lat.value == 33.17922234
    assert cpr.column == CPRColumn.ENRICHED


def _dallas_error_lead():
    return NormalizedLead(
        county="dallas", parcel_id="DAL_X", channel="foreclosure_notice",
        situs_address="15806 Breedlove Pl, Addison, TX 75001",
        raw_data={"details_status": "error", "city": "Addison",
                  "details": {"error": "PDF parse failed"}},
    )


def test_details_status_error_skips_dallas_property_extraction():
    cpr = enrich_from_dallas_details(_dallas_error_lead())
    assert cpr.get("detail_scrape_error") == "detail scrape failed: PDF parse failed"
    for name in ("owner_name", "taxing_entities", "isd", "improvement_sqft",
                 "cad_appraised_value", "foreclosing_lender"):
        assert cpr.get(name) is None
    # The DCAD flag keys off improvement data inside the payload — a failed
    # scrape must not fabricate one either.
    assert cpr.get("dcad_cross_ref_needed") is None
    # Filename-derived city is lead-level metadata, not detail-page data — kept.
    assert cpr.city.value == "Addison"
    assert cpr.column == CPRColumn.ENRICHED


def test_details_status_error_skips_raw_record_extraction():
    record = {
        "entry_id": "DAL_March_Addison_1",
        "property_address": "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334",
        "county": "Dallas", "city": "Addison",
        "latitude": 32.96, "longitude": -96.82,
        "details_status": "error",
        "details_error": "HTTP 403",
        "details": {},
    }
    cpr = enrich_from_raw_record(record)
    assert cpr.get("detail_scrape_error") == "detail scrape failed: HTTP 403"
    for name in ("owner_name", "improvement_sqft", "cad_appraised_value",
                 "homestead", "foreclosing_lender"):
        assert cpr.get(name) is None
    assert cpr.city.value == "Addison"
    assert cpr.column == CPRColumn.ENRICHED
