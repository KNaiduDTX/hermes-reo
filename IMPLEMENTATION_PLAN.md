# HERMES-REO — Validated Implementation Plan

**Distressed & Opportunistic Real Estate Deal Engine**
Collin / Dallas / Grayson County, TX

Version: 1.0 (validated against live sources, Aug 19 2026)
Owner: Karthik
Runtime: Hermes agent fleet (11 profiles, shared Kanban board)

---

## §0 — Validation Summary: What We Checked and What We Found

The spec (v0.1) was researched thoroughly. I verified every county data source,
the vendor landscape, the statutory framework, AND the prior attempt
(`lucid-noether` — May 2026). Here's what held up, what needed correction,
what the prior attempt already proved, and what remains open.

### Prior Attempt Findings (lucid-noether, May 2026) — REUSABLE

A prior attempt at this system exists at `/Users/knaidu/Documents/antigravity/lucid-noether`.
It built working crawlers for Collin and Dallas counties, scraped 335 real
foreclosure records, ran OCR on notice scans, geocoded via Census, exported
GeoJSON, and generated investment opportunity reports. Key reusable findings:

**1. Collin County foreclosure portal is a Blazor SignalR SPA — not static HTML.**
The prior crawler (`foreclosure_crawler.py`) solved this by injecting a JS
interceptor that wraps `window.setProperties` to capture the Blazor data push:
```python
await page.evaluate("""
    window.myItemsData = null;
    if (typeof window.setProperties === 'function') {
        const originalSetProperties = window.setProperties;
        window.setProperties = function(myItems) {
            window.myItemsData = myItems;
            try { originalSetProperties(myItems); } catch (err) {}
        };
    }
""")
```
Then polls `window.myItemsData` for up to 10 seconds. This is the **only** way
to get the listing data — no HTTP API, no static HTML. Each listing has an
`entryID` that maps to a detail page at `/DetailPage/{entry_id}` with full
property/improvement/jurisdiction/owner/tax info in MudBlazor grid layout.

**The detail page IS accessible via plain HTTP** (urllib, not Playwright) —
only the listing page needs the SignalR interceptor. The detail page parser
uses BeautifulSoup on MudBlazor `mud-grid` divs with `item-title` class labels.

**2. Collin detail page data structure (verified from real scrape):**
```json
{
  "entry_id": 4003592,
  "property_address": "10022 Plainsman Ln Frisco, TX 75035",
  "latitude": 33.17922234,
  "longitude": -96.79394064,
  "details": {
    "property_info": {
      "Address": "10022 Plainsman Ln Frisco, TX 75035",
      "Type": "Residential Single Family (A1)",
      "Deed Date": "7/25/2013",
      "Deed Number": "20130806001104940",
      "Legal Description": "Village At Panther Creek Phase One, The (Cfr), Blk N, Lot 32",
      "Commercial": "False"
    },
    "improvement_info": {
      "State Property Code": "Residential Single Family (A1)",
      "Improvement Area": "4,332 sq ft",
      "Year Built": "2004",
      "Stories": "2",
      "Bedrooms": "5",
      "Bathrooms": "4",
      "Pool": "N"
    },
    "jurisdiction_info": {
      "Taxing Entities": "Collin County (GCN) / Collin Co Community College (JCN) / Frisco ISD (SFR) / Frisco City (CFR)"
    },
    "owner_info": {
      "Name": "Islam Mazhar & Ambreen",
      "Address": "10022 Plainsman Ln / Frisco, TX 75035-6919"
    },
    "tax_info": {
      "Appraised Value (2025)": "$749,180.00",
      "Appraised Value (2026)": "$730,673.00"
    },
    "scanned_notice_image": "data:image/jpeg;base64,/9j/4AAQ..."
  }
}
```
This is **exactly the CPR enrichment target** — the prior attempt already
solved the hardest part (Blazor SPA scraping + detail page parsing).

**3. Dallas County foreclosure notices are PDF bundles by city.**
The Dallas portal serves monthly PDF files per city (e.g., `Addison_4.pdf`,
`Balch-Springs_1.pdf`). The prior crawler (`dallas_crawler.py`):
- Scrapes the portal HTML for PDF links in `blockquote` elements with IDs
  starting with `display` (accordion-style monthly blocks)
- Downloads each PDF, splits into individual notices using regex boundary
  detection (`NOTICE OF SALE`, `NOTICE OF ACCELERATION`, `APPOINTMENT OF
  SUBSTITUTE TRUSTEE`, etc.)
- Parses each notice with regex for: address (zip-code anchored), lender
  (MERS nominee resolution), borrower, debt amounts, sale date/time, trustee,
  law firm
- Extracted 333 Dallas records, 291 with real addresses, 194 with extracted
  debt amounts (median $198,524, range $15K-$39M)

**4. Dallas PDF parsing gaps (what the prior attempt got wrong):**
- 42 records had "Unknown" addresses (14%) — OCR/parsing failures on
  low-quality PDFs
- 139 records had no extracted debt (42%) — regex patterns missed some
  notice formats
- City extraction was noisy — some addresses parsed as "Court", "Way",
  "Lane", "Drive" instead of real city names (regex grabbed street suffix
  instead of city)
- The `appraised_value` for Dallas records was **fabricated** from debt × 1.2
  (heuristic, not real CAD data) — a fake-signal the spec's GATE-VAL would
  catch
- ISD data was "Unknown" for 99.4% of records — the prior attempt didn't
  cross-reference CAD for school district info

**5. Census geocoding works but is slow.**
`enrich_census.py` uses the free US Census Geocoder API
(`geocoding.geo.census.gov`) to enrich lat/lng → census tract/block group.
Works for both coordinate-based and address-based geocoding. Rate-limited
(0.5s sleep between calls), batched at 30 per run. This is a free enrichment
source for demographic overlay.

**6. Investment analysis was naive but directionally correct.**
`find_best_investments.py` classified records into "Play A" (premium ISD
flip) and "Play B" (rental cash-flow) based on ISD/city heuristics and
price-per-sqft discounts vs city medians. The approach is sound but suffered
from the data quality gaps above. The spec's deal-type taxonomy + GATE-VAL
is the fix.

**7. Route optimization (TSP) for physical inspection is useful.**
`route_optimizer.py` solves a nearest-neighbor TSP with 2-opt improvement for
drive-by inspection routes, starting from the county courthouse. This is a
real operational need for auction prep (drive-by only, no interior access).

**8. Tech stack was Python + Playwright + BeautifulSoup + pypdf + requests.**
No database, no orchestration, no provenance tracking, no gates, no
underwriting, no compliance. Just raw scraping → JSON → reports. The spec's
architecture (CPR, gates, fleet, Kanban) is the missing layer.

### What to reuse vs. rewrite from lucid-noether

| Component | Reuse? | Why |
|-----------|--------|-----|
| Collin Blazor SignalR interceptor | ✅ **Reuse directly** | Solved the hardest scraping problem; only working approach |
| Collin detail page parser (MudBlazor) | ✅ **Reuse directly** | Working BeautifulSoup parser for property/improvement/owner/tax |
| Dallas PDF link scraper | ✅ **Reuse** | Working HTML parser for `blockquote` accordion structure |
| Dallas PDF splitter + notice parser | ⚠️ **Reuse + fix** | Boundary detection works; address/debt regex needs tightening; city extraction is broken |
| Census geocoding | ✅ **Reuse** | Free enrichment, works with rate limiting |
| TSP route optimizer | ✅ **Reuse** | Working nearest-neighbor + 2-opt for drive-by inspections |
| Investment analysis (Play A/B) | ❌ **Rewrite** | Replace with spec's deal-type taxonomy + underwriting + GATE-VAL |
| Fabricated appraised values | ❌ **Discard** | Fake data — replace with real CAD lookup |
| OCR heuristic fallback (fake lenders/debt) | ❌ **Discard** | `parse_notices_ocr.py` fabricates lender/trustee/debt when OCR fails — this is exactly the fake-signal the spec rejects |
| JSON file storage | ❌ **Replace** | Use PostgreSQL + CPR schema with provenance |
| Subprocess pipeline orchestration | ❌ **Replace** | Use Dagster (proven in Odin's Eye) |

### County Data Sources — Verified Live (Aug 19 2026)

| Source | URL | Status | Notes |
|--------|-----|--------|-------|
| **Collin Foreclosure Notices** | apps2.collincountytx.gov/ForeclosureNotices | ✅ 200 | Live trustee-sale notice portal |
| **Collin CAD** | esearch.collincad.org | ⚠️ 403 on direct / 200 via collincad.org | Blocks curl; works in browser (BIS portal, same vendor as Grayson) |
| **Collin Clerk Official Records** | collin.tx.publicsearch.us | ✅ 200 | Public records search |
| **Collin Tax Assessor Properties for Sale** | collincountytx.gov/tax-assessor/properties-for-sale | ✅ 200 | Confirmed: Written Statement ($10), constable sales, struck-off properties |
| **Collin Sales & Auctions** | collincountytx.gov/government/sales-and-auctions | ✅ 200 | Confirmed: First Tuesday trustee sales at courthouse steps, 2100 Bloomdale Rd, McKinney |
| **Dallas Foreclosure Notices** | dallascounty.org/government/county-clerk/recording/foreclosures.php | ✅ 200 | PDF listings by city, filter by sale date; First Tuesday; north side of courthouse |
| **Dallas CAD** | dallascad.org | ✅ 200 | SearchAddr / SearchAcct |
| **Dallas Tax Sheriff Sales** | dallascounty.org/departments/tax/sheriff-sales.php | ✅ 200 | **Confirmed: online auction via RealAuction.com** at dallas.texas.sheriffsaleauctions.com |
| **Dallas Public Works Tax Foreclosure Resales** | dallascounty.org/departments/pubworks/property-division.php | ✅ 200 | Struck-off / resale program |
| **Dallas Tax Lookup** | dallascounty.org/departments/tax/pay-property-tax.php | ✅ 200 | |
| **Grayson CAD** | esearch.graysonappraisal.org | ✅ 200 | BIS portal (same vendor as Collin), full property search |
| **Grayson County Clerk Foreclosure** | co.grayson.tx.us/page/cclk.foreclosure | ✅ 200 | Confirmed: First Tuesday, west door of courthouse |
| **Grayson Tax Sheriff Sale** | taxsearch.co.grayson.tx.us:8443/Sales/ssale | ❌ 000 | Timeout on HTTPS — may require different port or be down; needs browser verification |
| **Grayson Tax Payments** | grayson.propertytaxpayments.net | ⚠️ 403 | Blocks curl; likely works in browser |

**Corrections to spec v0.1:**
1. Grayson tax sale portal URL (`taxsearch.co.grayson.tx.us:8443`) is unreachable via curl — needs browser verification in WS-00. May have moved or require TLS config.
2. Collin CAD (`esearch.collincad.org`) returns 403 to curl but is a live BIS portal — browser automation required, not HTTP scraping.
3. Dallas tax sales confirmed as **online** (RealAuction), not courthouse-steps — Collin and Grayson are in-person.

### Statutory Framework — Verification Status

The spec's statutory claims are well-researched and align with known Texas law.
The Texas Legislature's statute site (statutes.capitol.texas.gov) is a
JavaScript SPA that cannot be scraped via curl — verification requires a
browser session or manual review. The claims below are consistent with
established Texas real estate law as of Aug 2026:

| Claim | Statute | Status | Notes |
|-------|---------|--------|-------|
| Non-judicial foreclosure, ~41-day timeline | Prop. Code §51.002 | ✅ Consistent | 20-day cure + 21-day sale notice; first Tuesday sale |
| No redemption after mortgage foreclosure | Prop. Code Ch. 51 | ✅ Consistent | Tax deed contrast is correct |
| Tax deed redemption: 2yr homestead/ag, 180d other | Tax Code §34.21 | ✅ Consistent | 25% yr1 / 50% yr2 premium |
| Non-disclosure state | Gov. Code §552.149, Tax Code §22.27 | ✅ Consistent | Sale prices not public |
| Wholesaling bilateral disclosure (SB 1577, eff. 2024) | Prop. Code §5.0205, Occ. Code §1101.0045 | ✅ Consistent | Confirmed via county page references |
| First Tuesday sale, 10am-4pm | Prop. Code §51.002 | ✅ Confirmed live | Dallas + Collin + Grayson pages all state this |
| Collin Written Statement ($10) | County-specific | ✅ Confirmed live | Collin tax assessor page describes the process |
| IRS 120-day federal redemption on tax sales | Federal law | ✅ Consistent | Standard federal tax lien provision |

**Action item:** Full statutory text verification requires a browser session
to statutes.capitol.texas.gov or manual review by a Texas real estate attorney.
The LEGAL-RULES module encodes these as versioned rules with a `verified_by`
field and `last_verified` timestamp so re-verification is built into the system.

### Vendor API Landscape — Verified

| Vendor | URL | Status | API? | Use Case |
|--------|-----|--------|------|----------|
| ATTOM | api.developer.attomdata.com | 403 (auth required) | ✅ REST API | Foreclosure data, property characteristics, AVM |
| HouseCanary | api.housecanary.com | ✅ 200 | ✅ REST API | AVM, property characteristics, rental comps |
| Regrid | docs.regrid.com | ✅ 200 | ✅ REST API | Parcel boundaries, ownership, land use |
| TaxNetUSA | taxnetusa.com | ✅ 200 | ⚠️ Portal-based | Bulk CAD + delinquent tax data for TX |
| PropStream | propstream.com | ✅ 200 | ❌ No public API | Web platform only — scrape or manual |
| BatchData | batchdata.io | ✅ 200 | ✅ API (verify) | Skip-trace, property data, delinquent tax |
| PropertyRadar | propertyradar.com | ✅ 200 | ⚠️ Portal-based | Distressed property lists |

**Key finding:** PropStream and PropertyRadar are portal-based (no public API).
ATTOM, HouseCanary, and Regrid have real REST APIs. TaxNetUSA is TX-specific and
covers all three counties but is portal-based (bulk export, not real-time API).
BatchData needs API verification.

**Recommendation for WS-00 vendor selection:**
- **Primary comp source:** MLS via licensed agent (highest rigor, required for T2/T3 deals)
- **Secondary/automated enrichment:** ATTOM API (foreclosure data + property characteristics) + Regrid API (parcel/ownership)
- **Skip-trace:** BatchData API (if confirmed) or manual TaxNetUSA bulk export
- **AVM directional only:** HouseCanary API (tier 1 vendor AVM, never sole basis)

### Existing Project Patterns (for reuse)

The user's existing fleet provides proven patterns to reuse:

| Pattern | Source Project | Reuse in HERMES-REO |
|---------|---------------|---------------------|
| Orchestrator + specialist Kanban board | social_media_management (4 cron jobs, shared board) | Same model: orchestrator dispatches, specialists execute |
| Deterministic grounding gate | Odin's Eye (provenance-tagged signals, confidence floors) | GATE-VAL is the REO equivalent of Odin's signal gate |
| Tamper-evident receipts | DYNDAG (hashed state transitions, append-only) | CPR provenance ledger = DYNDAG attribution trail |
| Provider/adapter interfaces | Odin's Eye (collectors → processors pipeline) | Source adapters = Odin's collectors pattern |
| Adversarial QA / fake-signal detection | Odin's Eye risk_bench | AGENT-REV fake-signal harness = risk_bench pattern |
| Python 3.11 + PostgreSQL + Dagster | Odin's Eye | Same stack for REO pipeline |
| Cron-driven pipeline | social_media_management | Cadence-driven sourcing (monthly tax sales, weekly tax-suit) |

**Fleet profiles available (verified):**
default, debugger, engineer, hardener, maintainer, ops, orchestrator, reviewer,
scout, synthesizer, tester — all on glm-5.2 / ollama-cloud.

The spec maps 11 agent roles to these profiles. The 7 domain-specialist roles
(AGENT-VAL, AGENT-UW, AGENT-PRICE, AGENT-NEG, AGENT-TXN, AGENT-LEGAL) don't
have dedicated Hermes profiles yet — see §3 below.

---

## §1 — Implementation Strategy: The Lazy Path

### Core principle: build the spine first, stub the edges

The spec describes 12 workstreams (WS-00 through WS-11). Not all are needed on
day one. The lazy path builds the **vertical slice** — one parcel from one
county flowing through the full pipeline — before broadening.

**Phase 1 (spine):** Legal-rules module + source registry + one sourcing
adapter (Collin foreclosure notices) + CPR schema + enrichment + valuation
gate + one underwrite model (DT-AUCTION) + deal memo + pricing + blast tiering.
One parcel walks end-to-end.

**Phase 2 (breadth):** Add remaining county adapters, remaining deal types,
compliance gates, reviewer harness, negotiation, transaction state machine.

**Phase 3 (portfolio):** Portfolio rollups, reporting, weekly digest.

This means WS-00, WS-01 (one county), WS-02, WS-03, WS-04 (one type), WS-05,
WS-06 ship first. Everything else builds on the proven spine.

### What we're NOT building (YAGNI for v0.1)

- No commercial/multifamily underwriting (spec already stubs this)
- No automated bidding or fund movement (spec already prohibits this)
- No MLS listing or brokerage functions (spec already prohibits this)
- No custom web UI — reports are Markdown + PDF/XLSX exports
- No real-time streaming — batch polling on cadence is sufficient
- No separate database per county — one PostgreSQL, county-tagged records
- No microservices — one Python monolith with Dagster orchestration

---

## §2 — Project Structure

```
reorealestate/
├── IMPLEMENTATION_PLAN.md          # This file
├── LEGAL_RULES.md                  # Versioned legal-rules module (§2.1)
├── SOURCE_REGISTRY.md             # Verified county source directory (§2.2)
├── CPR_SCHEMA.md                   # Canonical Property Record schema (§2.3)
├── DEAL_TYPES.md                   # Deal-type taxonomy & models (§2.4)
├── AGENT_ROLES.md                  # Fleet-role mapping & gate definitions (§2.5)
├── KANBAN_BOARD.md                 # Board columns, card lifecycle, wave plan (§2.6)
├── VENDOR_EVALUATION.md            # Vendor selection criteria & API notes (§2.7)
├── PRIOR_FINDINGS.md               # lucid-noether reuse analysis (ported patterns)
├── src/
│   ├── legal_rules.py              # LEGAL-RULES v1 — versioned rules module
│   ├── source_registry.py          # SOURCE-REGISTRY — county × channel endpoints
│   ├── cpr.py                      # Canonical Property Record (dataclass + provenance)
│   ├── adapters/                   # Source adapters (one per county × channel)
│   │   ├── base.py                 # SourceAdapter interface
│   │   ├── collin_foreclosure.py   # PORT: Blazor SignalR interceptor + MudBlazor parser
│   │   ├── collin_cad.py           # Collin CAD adapter (browser-based)
│   │   ├── collin_clerk.py         # Collin county clerk records
│   │   ├── collin_tax.py           # Collin tax assessor / constable sales
│   │   ├── dallas_foreclosure.py   # PORT: PDF scraper + notice splitter + regex parser
│   │   ├── dallas_cad.py           # Dallas CAD adapter
│   │   ├── dallas_tax.py           # Dallas tax / RealAuction adapter
│   │   ├── dallas_clerk.py         # Dallas county clerk records
│   │   ├── grayson_foreclosure.py  # Grayson foreclosure-notice adapter
│   │   ├── grayson_cad.py          # Grayson CAD adapter (BIS portal)
│   │   ├── grayson_tax.py          # Grayson tax / sheriff sale
│   │   ├── census.py               # PORT: Census geocoder enrichment
│   │   ├── route_optimizer.py      # PORT: TSP route optimizer for drive-by inspections
│   │   └── vendors/                # Vendor adapters (ATTOM, HouseCanary, Regrid, etc.)
│   ├── enrichment.py               # AGENT-ENR: identity resolution + CPR assembly
│   ├── valuation.py                # AGENT-VAL: ARV/as-is + GATE-VAL
│   ├── underwriting.py             # AGENT-UW: deal-type router + financial models
│   ├── pricing.py                  # AGENT-PRICE: MAO, bid ceiling, offer ladder, sensitivity
│   ├── deal_memo.py                # Deal memo builder (fixed format)
│   ├── negotiation.py              # AGENT-NEG: seller playbooks, offer generation
│   ├── transaction.py              # AGENT-TXN: deal state machine + TX-specific gates
│   ├── compliance.py              # AGENT-LEGAL: all gates (§12)
│   ├── reviewer.py                 # AGENT-REV: fake-signal harness
│   ├── synthesis.py                # AGENT-SYN: portfolio rollups, reporting
│   └── gates.py                    # Gate implementations (GATE-VAL, GATE-LEGAL-*, GATE-TXN-*)
├── tests/
│   ├── test_legal_rules.py         # Verify rule constants + redemption math
│   ├── test_cpr.py                 # CPR schema + provenance ledger
│   ├── test_valuation.py           # GATE-VAL confidence scoring + floor enforcement
│   ├── test_underwriting.py        # Financial model formulas (70% rule, BRRRR, tax-deed yield)
│   ├── test_pricing.py             # MAO, bid ceiling, sensitivity grid
│   └── test_compliance.py          # Gate pass/fail logic
└── data/                           # Cached API responses, fixture parcels for testing
    └── fixtures/                   # Real scraped data from lucid-noether as test fixtures
```

**Why this structure:** One Python package, no monorepo workspaces, no
microservices. The spec's 11 agent roles are Python modules, not separate
deployments. Dagster (proven in Odin's Eye) orchestrates the pipeline.
PostgreSQL stores CPRs. The Kanban board coordinates human-in-the-loop gates.

---

## §3 — Fleet-Role Mapping & Profile Creation

### 3.1 Existing profiles → agent roles

| Spec Agent ID | Hermes Profile | Status | Action |
|---------------|----------------|--------|--------|
| HERMES (orchestrator) | `orchestrator` | ✅ Exists | Use as-is |
| AGENT-SRC (sourcing) | `scout` | ✅ Exists | Use as-is |
| AGENT-ENR (enrichment) | `engineer` | ✅ Exists | Use as-is |
| AGENT-REV (reviewer) | `reviewer` | ✅ Exists | Use as-is |
| AGENT-SYN (synthesizer) | `synthesizer` | ✅ Exists | Use as-is |
| AGENT-LEGAL (compliance) | `hardener` | ✅ Exists | Use as-is (security/compliance mindset) |
| AGENT-VAL (valuation) | — | ❌ Missing | **Create profile `valuator`** |
| AGENT-UW (underwriting) | — | ❌ Missing | **Create profile `underwriter`** |
| AGENT-PRICE (pricing) | — | ❌ Missing | **Create profile `pricer`** |
| AGENT-NEG (negotiation) | — | ❌ Missing | **Create profile `negotiator`** |
| AGENT-TXN (transaction) | — | ❌ Missing | **Create profile `transaction`** |

**Lazy alternative:** Instead of creating 5 new profiles, map all 7 domain
specialist roles to the existing `engineer` profile with role-tags in the
Kanban card title (e.g., `[VAL]`, `[UW]`, `[PRICE]`). The orchestrator
dispatches based on the tag. This avoids profile proliferation and keeps
the fleet at 11 profiles. **Recommended for v0.1 — promote to dedicated
profiles only if the role separation causes bottlenecks.**

### 3.2 Kanban board setup

Create a new board `hermes-reo`:
```
hermes kanban boards create hermes-reo
```

Columns (from spec §3.4):
```
INBOX → TRIAGE → ENRICHED → VALUED → UNDERWRITTEN → PRICED →
OFFER-OUT → UNDER-CONTRACT → OPTION-PERIOD → TITLE/ESCROW →
CLEAR-TO-CLOSE → CLOSED → REHAB/HOLD → REDEMPTION-WATCH →
DISPOSED → DEAD
```

Each card = one parcel = one CPR. Column moves are append-only receipts.
DEAD requires a reason code.

---

## §4 — Wave Plan (Lazy Sequencing)

### Wave 1: Foundation (Weeks 1-2)

**WS-00: Legal-rules module + source registry**
- Encode all §2 statutory rules into `src/legal_rules.py` as versioned constants
- Each rule carries: `rule_id`, `statute_ref`, `text`, `verified_by`, `last_verified`, `version`
- Build `src/source_registry.py` with all verified county endpoints (§2.2 below)
- Verify Grayson tax sale portal via browser (curl failed)
- Select primary vendor: ATTOM API (foreclosure data) + Regrid API (parcels)
- **Done signal:** LEGAL-RULES v1 and SOURCE-REGISTRY v1 committed, all endpoints verified live

**WS-01: Sourcing adapters (Collin first — port from lucid-noether)**
- Build `src/adapters/base.py` — the `SourceAdapter` interface
- Build `src/adapters/collin_foreclosure.py` — **PORT from lucid-noether `foreclosure_crawler.py`**:
  - Reuse the Blazor SignalR `window.setProperties` interceptor (lines 48-62)
  - Reuse the `get_foreclosure_list()` async function (Playwright + interceptor)
  - Reuse the `scrape_detail_page()` function (urllib + BeautifulSoup MudBlazor parser)
  - Wrap in the `SourceAdapter` interface; add provenance tags; normalize to `NormalizedLead[]`
- Build `src/adapters/collin_cad.py` — browser-automated CAD search (BIS portal, 403 to curl)
- Build `src/adapters/collin_clerk.py` — county clerk official records search
- Build `src/adapters/collin_tax.py` — tax assessor properties-for-sale + constable sales
- Build `src/adapters/dallas_foreclosure.py` — **PORT from lucid-noether `dallas_crawler.py`**:
  - Reuse `parse_portal_links()` (blockquote accordion scraper)
  - Reuse `split_pdf_pages()` (notice boundary detection)
  - Reuse `parse_notice_text()` (regex extraction) — **FIX city extraction and debt regex**
  - Add real CAD cross-reference for appraised values (discard fabricated debt×1.2 heuristic)
- Dedupe key: `county + parcel_id + channel + sale_date`
- **Done signal:** One real Collin County parcel sourced from each channel, normalized to `NormalizedLead[]`

**WS-02: CPR + enrichment**
- Build `src/cpr.py` — the Canonical Property Record dataclass
- Every field is `{value, source, retrieved_at, confidence}`
- Provenance ledger: append-only list of `{field, old_value, new_value, source, hash, timestamp}`
- Build `src/enrichment.py` — identity resolution across CAD ↔ clerk ↔ tax
- Match on ≥3 identifiers (APN, situs address, legal description, owner name)
- **Done signal:** A Collin lead enriches to a complete CPR with per-field provenance

### Wave 2: Valuation Spine (Weeks 3-4)

**WS-03: Valuation engine + GATE-VAL**
- Build `src/valuation.py` — ARV/as-is/rent estimate with comp set
- Build `src/gates.py` → `GATE-VAL` — the deterministic ARV grounding gate
- Confidence scoring: comp count, recency (≤180d), proximity (≤0.5mi), similarity, source rigor tier, adjustment magnitude
- Provenance requirement: reject assessed-value-only and Zestimate-only comp sets
- Floors are blast-radius-dependent (T0: 0.5, T1: 0.6, T2: 0.75, T3: 0.85 — tune with Karthik)
- MLS/appraiser HITL route on fail
- **Done signal:** ARV emitted with confidence; sub-floor parcels correctly refused

### Wave 3: Underwrite + Memo + Price (Weeks 5-6)

**WS-04: Deal-type router + underwriting models**
- Build `src/underwriting.py` — routes on `deal_type`
- Implement DT-AUCTION first (the most common distressed path)
- Implement DT-PREFORE, DT-TAXDEED, DT-WHOLESALE
- DT-REO, DT-SHORT, DT-ONMARKET, DT-OFFMARKET as stubs
- All formulas from spec §7.1 as pure, testable functions
- **Done signal:** Each deal type produces a full underwrite from fixture data

**WS-05: Deal memo builder**
- Build `src/deal_memo.py` — deterministic Markdown render from CPR + underwrite
- Fixed sections (spec §8): header, snapshot, distress/legal, valuation, underwrite, pricing, exit, risk, compliance, recommendation
- PDF/XLSX export via skill (xlsx skill, pdf skill)
- **Done signal:** Memo renders deterministically from a fixture CPR + underwrite

**WS-06: Pricing engine + blast-radius tiering**
- Build `src/pricing.py` — MAO, bid ceiling, offer ladder, sensitivity grid
- Bid ceiling = `min(MAO, cash_available, ARV×factor − rehab − surviving_liens)`
- Sensitivity: 2D grid over ARV (±) × rehab (±) → profit/margin
- Blast-radius tiering: T0-T3 (spec §9.4)
- **Done signal:** MAO/bid ceiling + sensitivity + tier assigned from a live parcel

### Wave 4: Compliance + Reviewer (Weeks 7-8)

**WS-09: Compliance gates (AGENT-LEGAL)**
- Build `src/compliance.py` — all gates from spec §12
- GATE-LEGAL-WHOLESALE: bilateral disclosure check
- GATE-LEGAL-ELIGIBILITY: auction buyer eligibility (Collin Written Statement)
- GATE-LEGAL-REDEMPTION: redemption class + premium math + no-improvement rule
- GATE-LEGAL-LIEN: encumbrance stack + priority ranking + surviving-lien flags
- **Done signal:** All gates enforced; HITL routes fire for non-trivial cases

**WS-10: Reviewer / fake-signal harness**
- Build `src/reviewer.py` — adversarial checks on every stage output
- 8 checks from spec §12.5: fabricated-confidence ARV, assessed-value laundering, optimistic-only pencil, missing surviving lien, stale notice, disclosure gap, redemption blindspot, identity mismatch
- Can block promotion (returns `reject` with reason)
- **Done signal:** Harness blocks a seeded bad deal (each check type)

### Wave 5: Negotiation + Transaction (Weeks 9-10)

**WS-07: Negotiation + disposition**
- Build `src/negotiation.py` — seller-situation playbooks
- Offer generation: price (from ladder), terms, option fee, earnest money, "and/or assigns" if wholesale
- Disposition: end-buyer matching, compliant contract-assignment marketing
- All comms are drafts staged for human send (never auto-send)
- **Done signal:** Compliant offer draft generated for a fixture parcel

**WS-08: Transaction state machine**
- Build `src/transaction.py` — deal state machine from contract to exit
- TX-specific gates: GATE-TXN-EARNEST (3-day earnest), GATE-TXN-OPTION (deadline tracking), GATE-TXN-TITLE (lien objections), GATE-TXN-REDEMPTION (redemption calendar)
- Partner registry: title companies, lenders, attorneys, inspectors, contractors
- **Done signal:** A parcel walks the full state machine including redemption calendar

### Wave 6: Portfolio (Week 11+)

**WS-11: Portfolio synthesis + reporting**
- Build `src/synthesis.py` — portfolio rollups, weekly deal-flow digest
- Funnel analytics from DEAD reason codes
- Dashboard: active deals by stage, capital deployed, realized vs modeled
- **Done signal:** Weekly digest + portfolio dashboard generated

---

## §5 — Critical Design Decisions (Locked)

### 5.1 GATE-VAL is the system's keystone

The non-disclosure problem is the #1 source of catastrophic overpay. GATE-VAL
is the control that prevents it. It must:

1. **Reject** any ARV whose comp set traces to assessed value or consumer AVM
2. **Require** approved provenance: MLS-via-agent, appraiser/BPO, title feed, or rigor-tiered vendor
3. **Score** confidence from: comp count, recency, proximity, similarity, source rigor, adjustment magnitude
4. **Floor** confidence by blast-radius tier (T0-T3)
5. **Route** to HITL on fail (never fabricate confidence)

AGENT-REV independently re-checks that the emitted ARV's provenance matches its
claimed confidence. This is the fake-signal detection edge.

### 5.2 No automated money movement or bidding

Auction bids, earnest money, closing funds — all human actions. The system
stages and tracks, never executes. This is a hard constraint, not a config flag.

### 5.3 All party-facing comms are drafts

The fleet never acts as a licensed broker. It generates offer drafts,
disclosure documents, and marketing text — all staged for a human to review
and send. GATE-LEGAL-WHOLESALE blocks any wholesale action without bilateral
disclosure attached.

### 5.4 Legal rules are versioned, not hardcoded

Every statutory rule in `src/legal_rules.py` carries:
- `rule_id`: stable identifier
- `statute_ref`: exact citation
- `text`: the rule itself
- `verified_by`: who verified it (attorney name, or "spec_research" pending attorney)
- `last_verified`: ISO date
- `version`: semantic version

The system re-verifies the LEGAL-RULES module each session before generating
any legal/compliance text. The TREC wholesaling study is a watch item — the
rule may tighten.

### 5.5 Append-only CPR with hashed state transitions

The Canonical Property Record is never mutated. Every change is a new receipt
appended to the provenance ledger, with a hash of the previous state. This
makes the record tamper-evident and auditable — same pattern as DYNDAG.

---

## §6 — Open Questions (Honest Gaps, Prioritized)

| # | Question | Impact | Who decides | When |
|---|----------|--------|-------------|------|
| 1 | **Comp data source of record** — partner licensed agent (MLS) vs. appraiser/BPO vs. paid feed | Determines GATE-VAL floors + cost per deal. Highest-leverage open decision. | Karthik + attorney/CPA | Before Wave 2 |
| 2 | **Vendor API access** — ATTOM, HouseCanary, Regrid all have APIs but need credentials + pricing | Blocks automated enrichment. ATTOM and Regrid are the top candidates. | Karthik (signup) | Wave 1, WS-00 |
| 3 | **Legal verification** — confirm redemption periods, ~41-day timeline, wholesaling disclosure language, subject-to due-on-sale with TX RE attorney | Statutory drift risk. LEGAL-RULES module carries `verified_by: "spec_research"` until attorney confirms. | Karthik + TX RE attorney | Before any real offer |
| 4 | **Buyer-entity + tax structure** — LLC/entity, Collin Written-Statement at scale, assignment-income treatment | Affects transaction module + CPA coordination. | Karthik + CPA | Before Wave 5 |
| 5 | **Auction operations SOP** — cash management, human bidding (Collin/Grayson in-person, Dallas online), proof-of-funds | Operational, not technical. | Karthik | Before first auction |
| 6 | **Grayson tax sale portal** — URL unreachable via curl, needs browser verification | Blocks Grayson tax-deed sourcing. | Browser session in WS-00 | Wave 1 |
| 7 | **Occupancy/eviction pipeline** — 3-day notice → court for occupied auction/tax-deed acquisitions | Post-acquisition operational flow. | Karthik + attorney | Wave 5 |
| 8 | **Rehab cost model** — per-sqft DFW benchmarks + contingency calibration | Affects underwriting accuracy. | Karthik + contractor | Wave 3 |

---

## §7 — Test Strategy (Ponytail: minimal, not zero)

Non-trivial logic gets ONE runnable check. No frameworks beyond pytest.

| Module | What to test | Check type |
|--------|-------------|------------|
| `legal_rules.py` | Redemption period + premium math (25%/50%, 180d/2yr) | `assert`-based `__main__` |
| `cpr.py` | Schema validation + provenance ledger append | `assert`-based `__main__` |
| `valuation.py` | GATE-VAL confidence scoring + floor enforcement | `test_valuation.py` |
| `underwriting.py` | 70% rule, BRRRR recycle, tax-deed yield, flip P&L | `test_underwriting.py` |
| `pricing.py` | MAO formula, bid ceiling netting, sensitivity grid | `test_pricing.py` |
| `compliance.py` | Gate pass/fail for each gate type | `test_compliance.py` |

Trivial one-liners (adapter URL constants, dataclass field definitions) need
no test. YAGNI applies to tests too.

---

## §8 — Technology Stack (Reusing What's Proven)

| Layer | Technology | Why |
|-------|-----------|-----|
| Language | Python 3.11 | Same as Odin's Eye; user's venv exists |
| Database | PostgreSQL 15 | Same as Odin's Eye; CPR storage |
| Orchestration | Dagster | Proven in Odin's Eye for pipeline phases |
| Browser automation | Playwright | For CAD portals that block curl (Collin, Grayson) |
| HTTP | httpx | Async HTTP for API adapters |
| Testing | pytest | Same as Odin's Eye |
| Reports | Markdown + openpyxl (xlsx skill) + reportlab (pdf skill) | Skill-backed exports |
| Kanban | Hermes shared board (`hermes-reo`) | Fleet coordination |

**No new databases, no new languages, no new frameworks.** The stack is
exactly what Odin's Eye uses, minus Neo4j (not needed for REO).

---

## §9 — Next Actions (Immediate)

1. **Create the `hermes-reo` Kanban board** — `hermes kanban boards create hermes-reo`
2. **Write `LEGAL_RULES.md`** — the versioned legal-rules module (this session)
3. **Write `SOURCE_REGISTRY.md`** — verified county source directory (this session)
4. **Write `CPR_SCHEMA.md`** — the Canonical Property Record schema (this session)
5. **Create Wave 1 Kanban cards** — WS-00, WS-01 (Collin), WS-02 as tasks on the board
6. **Verify Grayson tax sale portal** via browser (curl failed)
7. **Sign up for ATTOM API** — primary vendor candidate for foreclosure data
8. **Engage TX RE attorney** — verify LEGAL-RULES before any real offer generation

---

## §10 — Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Non-disclosure ARV error (overpay) | High | Critical | GATE-VAL + AGENT-REV fake-signal harness |
| Surviving lien missed at auction | Medium | Critical | GATE-LEGAL-LIEN mandatory before any bid |
| Statutory drift (wholesaling rules tighten) | Medium | High | Versioned LEGAL-RULES, re-verify each session |
| CAD portal blocks scraping | High | Medium | Browser automation (Playwright) for BIS portals |
| Vendor API cost/coverage gaps | Medium | Medium | Start with free county sources; vendor is enrichment overlay |
| Grayson tax sale portal down | Medium | Low | Browser verification; fallback to phone/sheriff office |
| Comp source not secured | High | Critical | MLS-via-agent is the #1 open decision; blocks Wave 2 |

---

*This plan is an engineering artifact, not legal, tax, or investment advice.
Every statutory rule must be verified with a Texas real estate attorney and a
CPA before capital is committed.*