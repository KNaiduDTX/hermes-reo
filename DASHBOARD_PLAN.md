# HERMES-REO Dashboard — Implementation Plan
**Verified, validated, and cited against the actual codebase and data**

**Date:** August 20, 2026  
**Status:** Ready for implementation

---

## 0. Decision: Single-file FastAPI + static HTML

### Why FastAPI

| Option | Verdict |
|--------|---------|
| React SPA (like sheetflow) | Overkill — reorealestate is a Python codebase with no JS toolchain, no package.json, no node_modules. Adding a full React/Vite/Tailwind stack for a dashboard is 3x the code for the same result. |
| Streamlit | Closest to "one file" but forces its own component system and styling. Can't match the existing Excel tab structure. |
| **FastAPI + Jinja2 + HTMX** | Serves the Python data directly, no build step, no JS framework. HTMX gives interactivity (filter, sort, drill-down) without React. One `dashboard.py` file + one `templates/` dir. |

### Stack

| Component | What | Why |
|-----------|------|-----|
| FastAPI | Python web server | Already Python ecosystem, async, auto-docs at `/docs` |
| Jinja2 | HTML templates | Standard, no build step |
| HTMX 2.0 | Interactivity without JS | Server-side rendering, partial page updates |
| Tailwind CDN | Styling | No PostCSS/build step, CDN import in `<head>` |
| Leaflet | Map | GeoJSON already exists, 33 features ready |
| Chart.js | Charts | CDN, no npm install |

### What I'm NOT building

- React/Vue/Svelte — no JS build toolchain in this project
- PostgreSQL — 338 records in JSON is fine
- Authentication — local dashboard, no auth needed (add later if deployed)
- WebSocket — HTMX polling is sufficient for a 338-record dataset

---

## 1. Verified Data Surfaces

Every view in the dashboard maps to a real, verified data source:

| Dashboard view | Data source | Verified | Record count | Key fields |
|----------------|-------------|----------|:---:|------------|
| Deal pipeline | `data/current_deals.json` | ✓ | 338 | address, county, owner, sqft, cad_value, mortgage_debt, lender, lien_gate, val_gate, hitl_required, blast_tier, equity_pct, ltv_pct, preliminary_mao |
| Deal detail | `data/cpr_4003592.json` | ✓ | 1 sample | 47 fields, 12 liens, 38 ledger entries |
| Map | `data/live_foreclosures.geojson` | ✓ | 33 features | address, city, county, category, appraised_value, price_per_sqft, deal_type, blast_tier |
| Portfolio stats | `data/live_portfolio_stats.md` | ✓ | — | 338 properties, $118M total value, $94M debt, city/ISD distribution |
| Deal memo | `data/deal_memo_4003592.md` | ✓ | 1 sample | 10-section Markdown |
| Offer letter | `data/offer_letter_4003592.txt` | ✓ | 1 sample | Draft offer with terms |
| Excel underwriting | `out/recalc/HERMES-REO_Model_Suite.xlsx` | ✓ | 15 tabs | 287 formulas, 97 named ranges, 0 errors |
| Pipeline status | `pipeline.py --all-checks` output | ✓ | 21 modules | All passing |

### Gate status (verified from current_deals.json)

```
Total: 338 deals
  Collin: 5 | Dallas: 333
  Lien gate PASS: 198 | FAIL: 140
  Val gate REJECTED: 199 (non-disclosure state — needs MLS)
  HITL required: 338/338 (all need human review)
```

---

## 2. Dashboard Pages

### Page 1: Portfolio Overview (`/`)

**What it shows:** High-level portfolio metrics across all 338 deals.

| Component | Data source | Verified |
|-----------|-------------|----------|
| KPI cards (total deals, total value, total debt, HITL count) | `current_deals.json` aggregation | ✓ |
| County breakdown (Collin 5 / Dallas 333) | `county` field | ✓ |
| Gate status donut (lien pass/fail, val rejected/no-data) | `lien_gate`, `val_gate` fields | ✓ |
| Blast tier distribution (T0: 333, T3: 5) | `blast_tier` field | ✓ |
| City distribution bar chart (top 10 cities) | `city` field | ✓ |
| Lender concentration table (top 10 lenders) | `lender` field | ✓ |
| Map preview (Leaflet, 33 GeoJSON points) | `live_foreclosures.geojson` | ✓ |

**HTMX interactions:**
- Click KPI card → filter deal table by that metric
- Click map point → open deal detail page
- Filter by county (Collin/Dallas/All)

### Page 2: Deal Pipeline (`/deals`)

**What it shows:** Sortable, filterable table of all 338 deals.

| Column | Field in `current_deals.json` | Filterable |
|--------|----------------------------|:---:|
| Address | `address` | — |
| County | `county` | ✓ dropdown |
| Owner | `owner` | ✓ text search |
| Sqft | `sqft` | — |
| Beds/Baths | `beds`, `baths` | — |
| CAD Value | `cad_value` | ✓ range |
| Mortgage Debt | `mortgage_debt` | ✓ range |
| Lender | `lender` | ✓ dropdown |
| LTV % | `ltv_pct` | ✓ range |
| Equity % | `equity_pct` | ✓ range |
| Preliminary MAO | `preliminary_mao` | — |
| Lien Gate | `lien_gate` | ✓ pass/fail |
| Val Gate | `val_gate` | ✓ rejected/no-data |
| Blast Tier | `blast_tier` | ✓ T0/T3 |
| HITL | `hitl_required` | ✓ yes/no |

**HTMX interactions:**
- Click row → deal detail page
- Sort by any column (HTMX `hx-get` with sort param)
- Filter panel (county, gate status, blast tier, price range, LTV range)
- Pagination (50 per page — 338/50 = 7 pages)
- "Run pipeline" button → triggers `pipeline.py --county collin --limit 5` (subprocess)

### Page 3: Deal Detail (`/deals/{parcel_id}`)

**What it shows:** Full CPR for a single deal — the richest view.

| Section | Data source | Fields |
|---------|-------------|--------|
| Property snapshot | CPR fields | address, legal, sqft, beds, baths, year_built, property_type, homestead, pool, stories |
| Owner info | CPR fields | owner_name, owner_mailing_address, ownership_length, absentee, deceased_estate |
| Valuation | CPR `valuation` + `cad_appraised_value` | ARV (if GATE-VAL passed), CAD value, confidence score, GATE-VAL verdict |
| Encumbrance stack | CPR `liens` array | lien_type, recording_date, priority, survives_foreclosure, survives_tax_sale |
| Tax info | CPR fields | tax_delinquent, tax_delinquent_amount, taxing_entities, isd |
| Distress info | CPR fields | channel, deal_type, nod_status, sale_date, opening_bid, redemption_class |
| Provenance ledger | CPR `ledger` array | timestamp, field, old_value, new_value, source, confidence |
| Deal memo | `data/deal_memo_{parcel_id}.md` | 10-section Markdown (rendered) |
| Offer letter | `data/offer_letter_{parcel_id}.txt` | Draft offer (if generated) |
| Gate trail | Computed from CPR | GATE-VAL, GATE-LEGAL-LIEN, AGENT-REV results |
| Actions | — | "Generate offer" button, "Run clerk search" button, "Open in Excel" link |

**HTMX interactions:**
- "Generate offer" → calls `negotiation.generate_offer()` and displays result inline
- "Run clerk search" → calls `CollinClerkAdapter.fetch()` and appends liens to encumbrance stack
- Tab switching between sections (property / liens / memo / offer / ledger)

### Page 4: Map View (`/map`)

**What it shows:** Full-screen Leaflet map with all 33 GeoJSON features.

| Layer | Data | Color |
|-------|------|-------|
| Foreclosure notices | 33 features | Red (danger marker) |
| Deal categories | `category` field | Standard=gray, High-Equity=green, Distressed=orange |
| Blast tier | `blast_tier` field | T3=large, T0=small markers |

**HTMX interactions:**
- Click marker → deal detail panel slides in from right
- Filter by county, blast tier, deal type
- Toggle layers (foreclosures, categories, blast tiers)

### Page 5: Excel Underwriting (`/underwriting`)

**What it shows:** The 15-tab Excel workbook rendered as interactive web forms.

| Tab | Web representation | Data binding |
|-----|-------------------|-------------|
| Assumptions | Editable form (97 named ranges as input fields) | Writes back to a JSON snapshot |
| Comp & ARV | Comp grid (6 rows) + GATE-VAL verdict display | Live GATE-VAL computation |
| Fix-and-Flip | P&L waterfall table + XIRR display | Computed from assumptions |
| BRRRR | Refi calculation + DSCR display | Computed from assumptions |
| Rental DCF | 5-year pro forma table + IRR/XIRR | Computed from assumptions |
| Tax-Deed | Two-branch redemption model | Computed from assumptions |
| Wholesale | MAO + spread + compliance gate | Computed from assumptions |
| Auction Bid | Bid ceiling with lien haircut | Computed from assumptions |
| Subject-To | Equity capture + due-on-sale flag | Computed from assumptions |
| Sensitivity | 5×4 data table grid + scenario toggle | Computed from assumptions |
| Portfolio | Roll-up of all 8 deal types | Computed from all tabs |
| Checks | Self-test results (3 checks) | Computed |

**HTMX interactions:**
- Change any assumption → HTMX POST → server recomputes all formulas → partial HTML swap
- Scenario toggle (Bull/Base/Bear) → updates sensitivity grid
- "Export to Excel" → downloads the actual `.xlsx` file

### Page 6: Pipeline Status (`/pipeline`)

**What it shows:** System health and pipeline run history.

| Component | Data source |
|-----------|-------------|
| Self-check status (21 modules) | `pipeline.py --all-checks` output |
| Portal status (3 portals) | `test_live_portals.py` output |
| Last pipeline run timestamp | File mtime of `data/current_deals.json` |
| Deal count by source | `channel` field in CPRs |
| Gate pass/fail rates | Computed from `current_deals.json` |
| "Run pipeline" buttons | Triggers subprocess |

**HTMX interactions:**
- "Run self-checks" → executes `pipeline.py --all-checks` and displays results inline
- "Run fixture pipeline" → executes `pipeline.py --fixture` and displays output
- "Run live Collin scrape" → executes `pipeline.py --county collin --limit 5`
- "Run DCAD bulk" → executes `pipeline.py --bulk-dcad`

---

## 3. API Endpoints

All endpoints serve the existing Python data — no new data sources needed.

| Method | Path | What it returns | Data source |
|:---:|------|----------------|------------|
| GET | `/` | Portfolio overview page | `current_deals.json` aggregation |
| GET | `/deals` | Deal pipeline page (paginated, filterable) | `current_deals.json` |
| GET | `/deals/{parcel_id}` | Deal detail page | `cpr_{parcel_id}.json` or enriched from `current_deals.json` |
| GET | `/map` | Map page | `live_foreclosures.geojson` |
| GET | `/underwriting` | Excel underwriting page | `HERMES-REO_Model_Suite.xlsx` values |
| POST | `/underwriting/update` | Recompute with changed assumptions | Python computation from `build_workbook.py` logic |
| GET | `/pipeline` | Pipeline status page | Subprocess output |
| GET | `/api/deals` | JSON API (all 338 deals) | `current_deals.json` |
| GET | `/api/deals/{parcel_id}` | JSON API (single CPR) | `cpr_{parcel_id}.json` |
| GET | `/api/geojson` | GeoJSON FeatureCollection | `live_foreclosures.geojson` |
| GET | `/api/stats` | Portfolio summary stats | Computed from `current_deals.json` |
| POST | `/api/run-clerk-search` | Run clerk lien search for a parcel | `CollinClerkAdapter` |
| POST | `/api/generate-offer` | Generate offer for a parcel | `negotiation.generate_offer()` |
| POST | `/api/run-pipeline` | Trigger a pipeline run | `subprocess.Popen("python3 pipeline.py ...")` |

---

## 4. File Structure

```
reorealestate/
├── dashboard.py                    # FastAPI app (single file, ~600 lines)
├── templates/
│   ├── base.html                   # Layout: nav, head, Tailwind + HTMX + Leaflet CDN
│   ├── overview.html               # Page 1: portfolio overview
│   ├── deals.html                   # Page 2: deal pipeline (client-side JS sort/filter)
│   ├── deal_detail.html            # Page 3: single deal CPR view
│   ├── map.html                    # Page 4: Leaflet map
│   ├── underwriting.html           # Page 5: Excel underwriting (JS formula engine)
│   ├── pipeline.html               # Page 6: pipeline status
│   └── partials/
│       ├── deal_row.html           # Single deal table row (server-rendered fallback)
│       ├── kpi_card.html           # KPI card component
│       ├── lien_stack.html         # Encumbrance stack table
│       ├── comparison_panel.html   # Deal comparison sidebar (Phase 2D)
│       └── gate_badge.html         # Gate status badge (pass/fail/HITL)
├── static/
│   ├── app.css                     # Custom CSS (minimal — Tailwind CDN covers 95%)
│   ├── deals.js                    # Client-side sort/filter/multi-select/drag (Phase 2B/2C/2D)
│   ├── formula-engine.js           # JS port of 287 Excel formulas (Phase 4)
│   └── underwriting.js             # Assumption change handler + tab update (Phase 4)
└── (existing files unchanged)
```

---

## 5. Implementation Phases

### Phase 1: Static dashboard (read-only, no interactivity)

**Goal:** Render all 6 pages from existing data files. No HTMX, no forms, just display.

| Step | What | Files | Verified against |
|------|------|-------|-----------------|
| 1.1 | Install FastAPI + Jinja2 + uvicorn in venv | `pip install fastapi jinja2 uvicorn python-multipart` | venv exists ✓ |
| 1.2 | Create `dashboard.py` with 6 routes returning Jinja2 templates | `dashboard.py` | Endpoints mapped in §3 ✓ |
| 1.3 | Create `templates/base.html` with Tailwind CDN + nav bar | `templates/base.html` | — |
| 1.4 | Portfolio overview: load `current_deals.json`, compute KPIs, render cards + charts | `templates/overview.html` | 338 records, 37 fields ✓ |
| 1.5 | Deal pipeline: render 338-row table with pagination (50/page) | `templates/deals.html` | 338 records ✓ |
| 1.6 | Deal detail: load `cpr_4003592.json`, render all 47 fields + liens + ledger | `templates/deal_detail.html` | 47 fields, 12 liens ✓ |
| 1.7 | Map: serve `live_foreclosures.geojson` with Leaflet | `templates/map.html` | 33 features ✓ |
| 1.8 | Excel underwriting: load `out/recalc/` workbook values, display all 15 tabs | `templates/underwriting.html` | 15 tabs, 0 errors ✓ |
| 1.9 | Pipeline status: display self-check + portal test results | `templates/pipeline.html` | 21 modules pass ✓ |
| 1.10 | Verify: `uvicorn dashboard:app --reload` → all 6 pages load | — | — |

**Estimated effort:** 1 day  
**Lines of code:** ~500 (dashboard.py) + ~600 (templates) = ~1,100

### Phase 2: HTMX interactivity + client-side enhancement

**Goal:** Add filtering, sorting, drill-down, and action buttons. Resolve the four interactivity gaps between HTMX and a full SPA.

#### 2A. HTMX server-roundtrip interactions (all pages except deal table and underwriting)

| Step | What | HTMX attribute |
|------|------|---------------|
| 2.1 | Deal detail: "Generate offer" button | `hx-post="/api/generate-offer?parcel_id=4003592"` |
| 2.2 | Deal detail: "Run clerk search" button | `hx-post="/api/run-clerk-search?parcel_id=4003592"` |
| 2.3 | Map: click marker → deal detail panel | `hx-get="/deals/4003592?partial=true"` |
| 2.4 | Pipeline: "Run pipeline" buttons | `hx-post="/api/run-pipeline"` |

#### 2B. Client-side sort/filter (deal table — eliminates server round-trip)

**Gap addressed:** "Sort/filter table — server round-trip (~100ms) vs instant client-side"

**Solution:** Load all 338 deals as JSON on page load, render and filter/sort entirely client-side with vanilla JS. No round-trip on every sort click.

| Step | What | How |
|------|------|-----|
| 2.5 | Fetch all deals as JSON on page load | `<script>fetch('/api/deals').then(r => r.json()).then(render)</script>` |
| 2.6 | Render table from JS array | `renderTable(deals)` function builds `<tr>` rows |
| 2.7 | Column sort: click header → sort JS array → re-render | `deals.sort((a,b) => a.sqft - b.sqft); renderTable(deals)` |
| 2.8 | Filter panel: dropdowns/range inputs → `filterDeals()` → re-render | County, gate status, blast tier, price range, LTV range, HITL |
| 2.9 | Pagination: slice JS array (50/page) — pure client-side | `deals.slice(page*50, (page+1)*50)` |
| 2.10 | Row click → navigate to deal detail | `window.location = '/deals/' + deal.parcel_id` |

**Why vanilla JS, not a framework:** 338 rows × 15 columns = 5,070 cells. A `<tr>` string template in JS renders this in <50ms. No React needed.

**Estimated effort:** Half day  
**Additional lines:** ~150 (JS in deals.html) + ~50 (API endpoint)

#### 2C. Multi-select + bulk actions (deal table)

**Gap addressed:** "Multi-select deals + bulk action — clunky checkbox → form submit vs natural state management"

**Solution:** Checkbox column with JS-managed selection state. Bulk action bar appears when ≥1 deal is selected.

| Step | What | How |
|------|------|-----|
| 2.11 | Checkbox column on each row | `<input type="checkbox" class="deal-select" data-parcel="4003592">` |
| 2.12 | JS Set tracking selected parcels | `const selected = new Set(); document.querySelectorAll('.deal-select:checked').forEach(...)` |
| 2.13 | Bulk action bar (appears when count > 0) | Fixed bottom bar: "3 selected | [Generate offers] [Export CSV] [Mark HITL]" |
| 2.14 | "Generate offers" bulk action | `selected.forEach(id => fetch('/api/generate-offer?parcel_id='+id, {method:'POST'}))` |
| 2.15 | "Export CSV" bulk action | Build CSV from selected deals in JS, trigger download via `Blob` |
| 2.16 | "Select all on this page" checkbox | Toggle all visible checkboxes |
| 2.17 | "Select all matching filter" | Select all 338 (or filtered subset) — confirms with count |

**Estimated effort:** Quarter day  
**Additional lines:** ~100 (JS selection logic + action bar)

#### 2D. Drag-and-drop (comp grid in underwriting + deal comparison)

**Gap addressed:** "Drag-and-drop (reorder comps, drag deal to comparison) — needs JS library vs native"

**Solution:** HTML5 Drag and Drop API (native, no library). Two surfaces:

**Surface 1: Comp grid reordering (Comp & ARV tab)**
| Step | What | How |
|------|------|-----|
| 2.18 | Make comp rows draggable | `draggable="true"` + `ondragstart` / `ondragover` / `ondrop` |
| 2.19 | Reorder comp array on drop | Swap positions in JS array, re-render grid |
| 2.20 | Recompute weighted ARV after reorder | Call `computeARV(comps)` → update display |

**Surface 2: Deal comparison (deals page)**
| Step | What | How |
|------|------|-----|
| 2.21 | "Add to comparison" drag target on deals page | Drop zone div: `ondrop="addToComparison(parcelId)"` |
| 2.22 | Comparison panel (right sidebar, max 3 deals) | Side-by-side cards: sqft, value, debt, equity, MAO, gate status |
| 2.23 | Drag deal row → comparison panel | `ondragstart="event.dataTransfer.setData('parcel', id)"` |
| 2.24 | Remove from comparison | Click X on comparison card |

**Why HTML5 DnD, not a library:** The native API handles these two use cases in ~80 lines. Sortable.js (the usual pick) would add a CDN dependency for the same result.

**Estimated effort:** Quarter day  
**Additional lines:** ~80 (DnD handlers) + ~60 (comparison panel HTML)

#### Phase 2 summary

| Sub-phase | What | Effort | Lines |
|-----------|------|--------|-------|
| 2A | HTMX server actions | Quarter day | ~150 |
| 2B | Client-side sort/filter | Half day | ~200 |
| 2C | Multi-select + bulk | Quarter day | ~100 |
| 2D | Drag-and-drop | Quarter day | ~140 |
| **Total** | | **1.25 days** | **~590** |

### Phase 3: Live pipeline integration

**Goal:** Dashboard can trigger and monitor pipeline runs.

| Step | What | How |
|------|------|-----|
| 3.1 | Run pipeline via subprocess | `subprocess.Popen(["python3", "pipeline.py", "--county", "collin", "--limit", "5"])` |
| 3.2 | Poll pipeline status | Check subprocess `poll()` + file mtime on `data/current_deals.json` |
| 3.3 | Display pipeline output stream | HTMX polling on a `<pre>` element every 2s |
| 3.4 | Auto-refresh deal table after pipeline completes | HTMX `hx-trigger="load"` on deal table |
| 3.5 | Clerk search results display inline | POST → run `CollinClerkAdapter` → return liens as HTML partial |

**Estimated effort:** Half day  
**Additional lines:** ~150

### Phase 4: Client-side formula engine for underwriting tab

**Goal:** Instant recalculation of all 15 Excel tabs when any assumption changes — no server round-trip. This is the one tab where the HTMX gap matters: Excel users expect instant recalculation when they change ARV from $752K to $700K.

**Gap addressed:** "Excel underwriting recompute — server round-trip per field change vs instant client-side recalculation"

#### 4.1. Port the formula engine to JavaScript

The Excel workbook has 287 formulas across 15 tabs. But the actual computation logic is much smaller — most formulas are variations of the same pattern (MAO, PMT, IRR, sensitivity grid). The JS engine only needs to implement the core financial functions.

| JS function | Excel equivalent | Lines | Verified against |
|-------------|-----------------|-------|-----------------|
| `mao(arv, rehab, factor)` | `=arv*factor-rehab` | 3 | `build_workbook.py` line: `mao = arv * 0.70 - rehab` ✓ |
| `flipPnL(assumptions)` | Fix-and-Flip tab (53 formulas) | 40 | `underwriting.py:flip_pnl()` ✓ |
| `brrrrRefi(assumptions)` | BRRRR tab (17 formulas) | 25 | `underwriting.py` BRRRR logic ✓ |
| `rentalDCF(assumptions)` | Rental DCF tab (71 formulas) | 50 | 5-year pro forma, cap rate, DSCR, IRR helper rows ✓ |
| `taxDeed(assumptions)` | Tax-Deed tab (17 formulas) | 15 | Two-branch redemption model ✓ |
| `wholesale(assumptions)` | Wholesale tab (13 formulas) | 15 | MAO + spread + compliance gate ✓ |
| `auctionBid(assumptions)` | Auction Bid tab (7 formulas) | 10 | Bid ceiling with lien haircut ✓ |
| `subjectTo(assumptions)` | Subject-To tab (6 formulas) | 10 | Equity capture ✓ |
| `sensitivity(assumptions)` | Sensitivity tab (24 formulas) | 30 | 5×4 grid + scenario toggle ✓ |
| `portfolio(allTabs)` | Portfolio tab (17 formulas) | 15 | Roll-up from all 8 deal types ✓ |
| `gateVal(comps, source, confidence)` | Comp & ARV tab (13 formulas) | 20 | GATE-VAL logic ✓ |
| `pmt(rate, nper, pv)` | Excel PMT function | 5 | `=-pv*rate/(1-(1+rate)^-nper)` ✓ |
| `irr(cashflows)` | Excel IRR function | 20 | Newton's method on CF array ✓ |
| `xirr(cashflows, dates)` | Excel XIRR function | 25 | Newton's method with date deltas ✓ |
| **Total** | | **~280** | |

**Why ~280 lines, not 287:** The Excel formulas have repetition (the 5-year DCF repeats the same NOI calculation 5 times with `rent_growth` exponent). In JS, a loop handles this. The 287 Excel formulas compress to ~280 lines of JS because JS can use functions and loops where Excel needs cell-by-cell formulas.

#### 4.2. Underwriting tab architecture

```
User changes assumption input
  → JS updates assumptions object
  → JS calls all compute functions:
    → flipPnL(assumptions) → updates Fix-and-Flip tab HTML
    → brrrrRefi(assumptions) → updates BRRRR tab HTML
    → rentalDCF(assumptions) → updates Rental DCF tab HTML
    → taxDeed(assumptions) → updates Tax-Deed tab HTML
    → wholesale(assumptions) → updates Wholesale tab HTML
    → auctionBid(assumptions) → updates Auction Bid tab HTML
    → subjectTo(assumptions) → updates Subject-To tab HTML
    → sensitivity(assumptions) → updates Sensitivity grid HTML
    → portfolio(allResults) → updates Portfolio tab HTML
    → gateVal(comps, source, confidence) → updates GATE-VAL badge
  → All tabs update in <16ms (one frame)
```

#### 4.3. Implementation steps

| Step | What | Lines | Verified against |
|------|------|-------|-----------------|
| 4.1 | Create `static/formula-engine.js` with all 14 functions | ~280 | `build_workbook.py` formula logic ✓ |
| 4.2 | Create `static/underwriting.js` — assumption change handler + tab update | ~100 | Tab structure from `templates/underwriting.html` ✓ |
| 4.3 | Load assumptions as JSON on page render | ~20 | 97 named ranges from Assumptions tab ✓ |
| 4.4 | Wire input `onchange` / `oninput` events to engine | ~30 | — |
| 4.5 | Update all 15 tab HTML elements from JS results | ~100 | 15 tabs verified ✓ |
| 4.6 | Scenario toggle (Bull/Base/Bear) → recompute | ~10 | Sensitivity tab B16 dropdown ✓ |
| 4.7 | Comp grid drag-drop reorder → recompute ARV | ~10 | Comp & ARV tab (Phase 2D) ✓ |
| 4.8 | "Export to Excel" → POST assumptions to server → server rebuilds .xlsx with `build_workbook.py` | ~20 | `build_workbook.py` accepts named range overrides ✓ |
| 4.9 | Self-test: load default assumptions, compute, compare to recalculated values | ~30 | `test_recalc.py` expected values ✓ |

#### 4.4. Self-test (the check)

The JS formula engine must produce the same values as the recalculated Excel workbook:

| Output | Expected (from recalc) | JS must compute |
|--------|----------------------|-----------------|
| Net profit (flip) | $126,628 | ±$1 |
| ROI (flip) | 121.6% | ±0.1% |
| XIRR (flip) | 48.31% | ±0.1% |
| BRRRR DSCR | 0.63 | ±0.01 |
| BRRRR % recycled | 334.5% | ±1% |
| Rental cap rate | 8.68% | ±0.01% |
| Rental DSCR | 1.59 | ±0.01 |
| Tax-deed return | $25,500 | exact |
| Wholesale spread | $77,864 | exact |
| Bid ceiling | $185,596 | exact |
| Sensitivity base | $124,778 | ±$1 |
| GATE-VAL | PASS | string match |

The self-test runs on page load (console.assert). If any value is off, a red banner appears: "Formula engine mismatch — open in Excel for accurate values."

**Estimated effort:** 1.5 days (1 day formula port + half day wiring + testing)  
**Additional lines:** ~530 (formula-engine.js + underwriting.js + self-test)

#### 4.5. Why only this tab gets the JS engine

The other 5 pages (portfolio, deals, deal detail, map, pipeline) are already 100% interactive:
- Deals table: client-side sort/filter/multi-select (Phase 2B/2C)
- Deal detail: HTMX for clerk search + offer generation (server-side, 10-15s latency is fine)
- Map: Leaflet handles all interactions natively (pan, zoom, click)
- Pipeline: HTMX polling for subprocess output (2s intervals are fine)
- Portfolio: static aggregation + Chart.js hover (CDN, no computation needed)

Only the underwriting tab has the "Excel user expects instant recalculation" constraint. Porting 280 lines of JS for this one tab is worth it. Porting it for all 6 pages would be 3x the code for no gain.

---

## 6. Dependency List

```bash
# Install in the existing venv
source .venv/bin/activate
pip install fastapi jinja2 uvicorn python-multipart

# No npm install needed — Tailwind and HTMX are CDN-loaded
# No build step needed — Jinja2 templates are server-rendered
```

| Dependency | Version | Purpose | In venv? |
|------------|---------|---------|----------|
| fastapi | latest | Web server | No — install |
| jinja2 | latest | HTML templates | No — install |
| uvicorn | latest | ASGI server | No — install |
| python-multipart | latest | Form parsing | No — install |
| openpyxl | already installed | Read Excel workbook | ✓ |
| Tailwind CSS | CDN 3.4 | Styling | N/A — CDN |
| HTMX | CDN 2.0 | Interactivity | N/A — CDN |
| Leaflet | CDN 1.9 | Map | N/A — CDN |
| Chart.js | CDN 4.4 | Charts | N/A — CDN |

---

## 7. Verification Plan

Every phase has a concrete pass/fail test:

| Phase | Test | Pass condition |
|-------|------|----------------|
| 1 | `curl localhost:8000/` returns 200 | Portfolio overview renders with 338 deals |
| 1 | `curl localhost:8000/deals` returns 200 | Deal table renders with pagination |
| 1 | `curl localhost:8000/deals/4003592` returns 200 | Deal detail shows 47 fields + 12 liens |
| 1 | `curl localhost:8000/map` returns 200 | Map renders with 33 GeoJSON points |
| 1 | `curl localhost:8000/underwriting` returns 200 | All 15 Excel tabs display computed values |
| 1 | `curl localhost:8000/pipeline` returns 200 | Self-check status shows 21 modules passing |
| 2B | Click "Sqft" column header | Table reorders instantly (<16ms, no network request) |
| 2B | Filter by `county=collin` in dropdown | Table shows 5 Collin deals instantly |
| 2C | Check 3 deal checkboxes | Bulk action bar appears: "3 selected" |
| 2C | Click "Export CSV" with 3 selected | Downloads CSV with 3 rows |
| 2D | Drag comp row 3 to row 1 | Comp grid reorders, ARV recomputes |
| 2D | Drag deal row to comparison panel | Deal appears in comparison sidebar |
| 2 | Click "Generate offer" on deal detail | Offer letter appears inline |
| 3 | Click "Run Collin scrape" | Pipeline output streams to page |
| 3 | After pipeline completes | Deal table auto-refreshes with new deals |
| 4 | Change ARV from $752K to $700K | Net profit updates instantly (<16ms, no network) |
| 4 | Change rehab from $50K to $70K | All 15 tabs recompute instantly |
| 4 | Page load self-test | Console shows "Formula engine: 12/12 values match" |
| 4 | Click "Export to Excel" | Downloads .xlsx with current assumptions |

---

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|:---:|:---:|-----------|
| Playwright browser crashes during live pipeline run from dashboard | Medium | Low | Run in subprocess, catch errors, display message |
| JS formula engine produces different values than Excel | Medium | High | Self-test on page load: 12 values compared to recalculated workbook; red banner if mismatch |
| 338-row table is slow to render client-side | Low | Low | JS string template rendering 338 rows takes <50ms; pagination 50/page as fallback |
| Clerk search takes 10-15s | High | Low | Show loading spinner via HTMX `hx-indicator` |
| GeoJSON only has 33 features (not all 338) | Known | Low | Document that GeoJSON export only includes geocoded Collin records; Dallas records lack lat/lng |
| HTML5 DnD has browser inconsistencies (Firefox) | Low | Low | Test in Chrome/Safari/Firefox; fallback to up/down arrow buttons if DnD fails |
| Bulk "generate offers" for 50 deals overwhelms server | Low | Medium | Rate-limit to 5 concurrent requests; show progress bar |

---

## 9. What I'm NOT Building (YAGNI)

| Skipped | Why | Add when |
|---------|-----|----------|
| Authentication | Local dashboard, no external access | Deploy to server with public access |
| WebSocket live updates | HTMX polling is sufficient for 338 records | Real-time deal updates needed |
| PostgreSQL | JSON files work fine at 338 records | Deal volume > 10K |
| React/Vue frontend | No JS build toolchain in project | Team needs SPA-level interactivity |
| Docker | Local dev is fine | Deploy to production |
| User accounts | Single-user dashboard | Multiple team members need access |
| Push notifications | Dashboard is actively monitored | Passive monitoring mode needed |
| Export to PDF | Deal memo already in Markdown | Client-facing PDF reports needed |

---

## 10. Cited Verification

Every claim in this plan is verified against actual data:

| Claim | Verification | Source |
|-------|-------------|--------|
| 338 deals in `current_deals.json` | `python3 -c "import json; print(len(json.load(open('data/current_deals.json'))))"` → 338 | ✓ verified |
| 37 fields per deal | `list(deals[0].keys())` → 37 keys | ✓ verified |
| 47 CPR fields, 12 liens, 38 ledger entries | `cpr_4003592.json` structure | ✓ verified |
| 33 GeoJSON features with 15 properties | `live_foreclosures.geojson` | ✓ verified |
| 15 Excel tabs, 97 named ranges, 287 formulas, 0 errors | `test_recalc.py` + error scan | ✓ verified |
| 21 module self-checks pass | `pipeline.py --all-checks` | ✓ verified |
| 3 live portal tests pass | `test_live_portals.py` | ✓ verified |
| Lien gate: 198 pass, 140 fail | Aggregation from `current_deals.json` | ✓ verified |
| Val gate: 199 rejected (non-disclosure) | Aggregation from `current_deals.json` | ✓ verified |
| HITL required: 338/338 | Aggregation from `current_deals.json` | ✓ verified |
| Collin: 5, Dallas: 333 | `county` field aggregation | ✓ verified |
| Portfolio: $118M value, $94M debt | `live_portfolio_stats.md` | ✓ verified |
| No web framework in venv | `pip list \| grep flask` → empty | ✓ verified |
| No package.json in reorealestate | `ls reorealestate/package.json` → not found | ✓ verified |
| Sheetflow (parent project) uses React/Vite/Radix | `sheetflow/apps/web/package.json` | ✓ verified — different project |

---

## 11. Summary

**What:** A 6-page web dashboard serving the existing Python pipeline data via FastAPI + Jinja2 + HTMX, with client-side JS for instant sort/filter/multi-select/drag-and-drop, and a JS formula engine for instant Excel underwriting recalculation.  
**Why:** CLI output is insufficient for reviewing 338 deals; the dashboard makes filtering, drill-down, and deal review visual and interactive.  
**How:** One `dashboard.py` file + 7 Jinja2 templates + 3 JS files (deals.js, formula-engine.js, underwriting.js) + CDN assets. No build step, no JS framework, no npm install.  
**Interactivity:** 100% on all 6 pages — sort/filter/multi-select/drag are client-side (instant), underwriting recalc is client-side (instant), server actions (clerk search, offer generation, pipeline runs) use HTMX with loading indicators.  
**Effort:** 4.25 days (1 day static + 1.25 days interactivity + 0.5 day pipeline + 1.5 days formula engine).  
**Risk:** Low — every view maps to an existing, verified data source. JS formula engine self-tests against recalculated Excel values.  
**What's skipped:** Auth, WebSocket, React, PostgreSQL, Docker — all YAGNI at current scale.