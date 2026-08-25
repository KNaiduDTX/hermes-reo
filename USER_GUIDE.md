# HERMES-REO — User Guide
**Complete operational guide for the distressed RE deal engine**

---

## Table of Contents
1. [Prerequisites & Setup](#1-prerequisites--setup)
2. [Daily Workflow: Sourcing Deals](#2-daily-workflow-sourcing-deals)
3. [Underwriting in Excel](#3-underwriting-in-excel)
4. [Lien Search & Title Diligence](#4-lien-search--title-diligence)
5. [Offer Generation & Negotiation](#5-offer-generation--negotiation)
6. [Portfolio Analytics](#6-portfolio-analytics)
7. [Batch Operations](#7-batch-operations)
8. [Testing & Verification](#8-testing--verification)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Prerequisites & Setup

### What you need installed

```bash
# Python 3.9+ with venv
cd ~/Documents/Hardline/Projects/reorealestate
source .venv/bin/activate

# Playwright (for county portal scrapers)
playwright install chromium

# Tesseract OCR (for scanned foreclosure notices)
brew install tesseract

# LibreOffice (for Excel recalculation)
# Already installed at /Applications/LibreOffice.app/
```

### Verify the system is healthy

```bash
# Run all 21 module self-checks
python3 pipeline.py --all-checks

# Expected output: 21 ✓ marks + "All checks passed"
```

```bash
# Verify Excel workbook recalculates correctly
python3 test_recalc.py

# Expected: "Self-test: PASS" + "All 8 deal types: computed"
```

```bash
# Verify live county portals are reachable
python3 test_live_portals.py

# Expected: "Results: 3 passed, 0 failed"
```

If all three pass, the system is ready.

---

## 2. Daily Workflow: Sourcing Deals

### Scrape live foreclosure notices

```bash
# Collin County — scrape 5 fresh foreclosure notices
python3 pipeline.py --county collin --limit 5

# What happens:
# 1. Scrapes Collin County Blazor SPA for foreclosure notices
# 2. Enriches each into a 36-field Canonical Property Record (CPR)
# 3. Searches Collin County Clerk for liens by owner name
# 4. Builds lien stack with surviving-lien flags
# 5. Runs GATE-LEGAL-LIEN (title/lien priority check)
# 6. Runs GATE-VAL (rejects assessed-value-only ARVs)
# 7. Routes to HITL if GATE-VAL fails (needs MLS comps)
```

```bash
# Dallas County — scrape 5 foreclosure notices
python3 pipeline.py --county dallas --limit 5

# Same pipeline, but uses Dallas PDF scraper + DCAD cross-reference
# instead of Collin clerk lien search
```

### What you'll see in the output

```
--- Parcel 1/5: 1001 Faringdon Dr Anna, TX 75409 ---
  Enriched: 1001 Faringdon Dr Anna, TX 75409 | Column: ENRICHED
  Clerk liens: 19 merged (total: 20)
  GATE-LEGAL-LIEN: pass — Lien stack verified: 20 liens, no surviving-lien issues
  GATE-VAL: fail (assessed-value-only — HITL required for real ARV)
  → Deal routed to HITL for MLS comp pull
```

**GATE-VAL failing is correct behavior.** Texas is a non-disclosure state — the system refuses to produce an ARV from assessed value alone. You must provide MLS comps (see Section 3).

### Fixture run (no network needed)

```bash
# Run the full pipeline on a verified test parcel (entry_id 4003592)
python3 pipeline.py --fixture

# This runs every step end-to-end:
# - Enrichment (36 fields)
# - Clerk lien search (31 instruments → 12 liens)
# - GATE-LEGAL-LIEN (hitl — tax lien surviving foreclosure)
# - GATE-VAL (pass — sample MLS comps provided)
# - Underwriting (flip: $135K profit, 24.3% ROI)
# - Pricing (MAO $475K, offer ladder $403K→$475K→$498K)
# - Reviewer (8 fake-signal checks)
# - Deal memo (10-section Markdown)
# - Negotiation (offer letter generated)
#
# Outputs saved to:
#   data/deal_memo_4003592.md
#   data/offer_letter_4003592.txt
#   data/cpr_4003592.json
#   data/foreclosures.geojson
```

---

## 3. Underwriting in Excel

### Open the workbook

```bash
# The formula version (open this in Excel or LibreOffice)
open out/HERMES-REO_Model_Suite.xlsx

# The recalculated version (has computed values — for reading results)
open out/recalc/HERMES-REO_Model_Suite.xlsx
```

### The Assumptions tab is your control panel

All 97 named ranges live on the **Assumptions** tab. Change any yellow cell and every deal model recalculates.

**Key assumptions to set for your deal:**

| Cell | Named range | What it controls | Default |
|------|------------|------------------|---------|
| B4 | `county` | Which tax rate applies | collin |
| B7 | `prop_addr` | Property address | — |
| B9 | `purchase_price` | What you're paying | $300,000 |
| B10 | `arv` | After-repair value | $752,000 |
| B12 | `rehab_budget` | Repair cost estimate | $50,000 |
| B13 | `hold_months` | Time to flip | 6 |
| B30 | `hm_rate` | Hard money interest rate | 9.95% |
| B31 | `hm_points` | Hard money points | 2.75% |
| B34 | `dscr_rate` | DSCR loan rate | 6.10% |
| B40 | `mao_factor` | MAO rule (70% = 0.70) | 0.70 |

### Comp & ARV tab — enter your comps

1. Enter 3-6 comparable sales in rows 5-10 (address, sold price, sale date, sqft, beds, baths, lot size, condition adjustment, distance, DOM)
2. Set **comp_source** (B18) to `mls_agent`, `appraiser_bpo`, `title_feed`, `vendor_avm`, `assessed_only`, or `zestimate_only`
3. The **GATE-VAL** cell (B22) will show PASS or FAIL
4. If GATE-VAL = PASS, the ARV flows to all 8 deal models
5. If GATE-VAL = FAIL, all downstream outputs show "GATE-VAL FAIL"

### Deal model tabs — read the results

| Tab | What to look at | Key cells |
|-----|----------------|-----------|
| **Fix-and-Flip** | B42 (net profit), B44 (ROI), B58 (XIRR) | Should be positive |
| **BRRRR** | B14 (DSCR), B20 (% recycled), B21 (infinite return?) | DSCR ≥ 1.25 to refi |
| **Rental DCF** | B29 (cap rate), B31 (DSCR), B34 (levered IRR) | Cap rate > DSCR rate |
| **Tax-Deed** | B17 (guaranteed return), B18 (annualized yield) | Compare to hurdle rate |
| **Wholesale** | B11 (MAO), B13 (spread) | Check compliance gate |
| **Auction Bid** | B13 (bid ceiling) | Maximum you should bid |
| **Subject-To** | B11 (equity captured) | Due-on-sale risk flag |
| **Sensitivity** | B16 (scenario toggle) | Switch Bull/Base/Bear |

### The recalculation test (Definition of Done)

1. Open `out/HERMES-REO_Model_Suite.xlsx` in Excel
2. Go to **Comp & ARV** tab, change ARV from $752,000 to $700,000
3. Go to **Fix-and-Flip** tab — net profit (B42) should drop
4. Go to **Sensitivity** tab — the 5×4 grid should recalculate
5. Go to **Checks** tab — should still show "PASS — formula-driven"

If any output doesn't change, the model is broken. Every headline output must trace to the Assumptions tab via formulas.

### Rebuilding the workbook

If you change the build script:

```bash
# Rebuild from scratch
python3 build_workbook.py

# Recalculate with LibreOffice (computes formula values)
/Applications/LibreOffice.app/Contents/MacOS/soffice --headless --calc \
  --convert-to xlsx out/HERMES-REO_Model_Suite.xlsx --outdir out/recalc/

# Verify zero errors
python3 -c "
from openpyxl import load_workbook
wb = load_workbook('out/recalc/HERMES-REO_Model_Suite.xlsx', data_only=True)
errors = [(n,c.coordinate,c.value) for n in wb.sheetnames for row in wb[n].iter_rows() for c in row if isinstance(c.value,str) and c.value.startswith('#') and any(e in c.value for e in ['#REF','#NAME','#VALUE','#DIV','#N/A'])]
print(f'Errors: {len(errors)}')
"
```

---

## 4. Lien Search & Title Diligence

### Collin County Clerk — search by owner name

```bash
# Standalone search (not part of pipeline)
python3 -c "
from src.adapters.collin_clerk import CollinClerkAdapter, build_lien_stack
adapter = CollinClerkAdapter()
instruments = adapter.fetch(party_name='ISLAM MAZHAR')
print(f'Instruments: {len(instruments)}')
for i in instruments[:5]:
    print(f'  {i.doc_type:30s} | {i.recorded_date} | {i.doc_number}')

liens = build_lien_stack(instruments)
print(f'Lien stack: {len(liens)} encumbrances')
for l in liens:
    flag = 'SURVIVES FC' if l.survives_foreclosure else ''
    print(f'  #{l.priority} {l.lien_type:15s} | {l.recording_date} | {flag}')
"
```

### What the lien stack tells you

| Lien type | Survives foreclosure? | Survives tax sale? | Action |
|-----------|:---:|:---:|--------|
| mortgage | No | No | Cleared by foreclosure/tax sale |
| tax (state) | **Yes** | No | Must pay to clear title |
| irs_federal | **Yes** | **Yes** | 120-day redemption right |
| hoa | **Yes** | **Yes** | Must negotiate/pay |
| municipal | **Yes** | **Yes** | Must pay |
| child_support | **Yes** | **Yes** | Must pay |
| lis_pendens | No | No | Pending litigation — investigate |

### Dallas CAD — cross-reference property data

```bash
# Single address lookup
python3 -c "
from src.adapters.dallas_cad import DallasCADAdapter
adapter = DallasCADAdapter()
result = adapter.fetch(address='15806 BREEDLOVE PLACE, ADDISON, TX 75001')
print(f'Status: {result.get(\"status\")}')
print(f'Year built: {result.get(\"year_built\")}')
print(f'Sqft: {result.get(\"improvement_sqft\")}')
print(f'Beds: {result.get(\"beds\")}')
print(f'Baths: {result.get(\"baths\")}')
print(f'Market value: \${result.get(\"cad_appraised_value\")}')
"
```

```bash
# Bulk DCAD enrichment (50 addresses, ~2min, cached to data/dcad_cache.json)
python3 pipeline.py --bulk-dcad
```

---

## 5. Offer Generation & Negotiation

### How it works in the pipeline

The pipeline automatically generates an offer at step 8:

```
[8] NEGOTIATION — Seller playbook + offer generation
  Deal type: DT-AUCTION
  Posture: last chance before sale
  Offer price: $403,800
  Earnest money: $4,038 | Option fee: $404
  Option period: 7d | Closing: 7d
  Cash offer: True | And/or assigns: False
  Key message: The auction is days away. Cash now is better than hoping...
  Saved: data/offer_letter_4003592.txt
```

### Seller postures by deal type

| Deal type | Posture | Framing | Opening offer |
|-----------|---------|---------|:---:|
| DT-PREFORE | Speed & certainty | "Close in 7 days, cash, no repairs" | 80% of MAO |
| DT-TAXDEED | Problem-solving | "Take this tax burden off your hands" | 75% of MAO |
| DT-AUCTION | Last chance | "Cash now before the sale" | 85% of MAO |
| DT-REO | Condition leverage | "As-is, no inspection contingency" | 75% of MAO |
| DT-SHORT | Patience | "We'll wait for lender approval" | 70% of MAO |

### Wholesale compliance gate

Wholesale offers are **auto-blocked** until bilateral disclosure is attached:

```
⚠️ BLOCKED — bilateral written equitable-interest disclosure required
(TX Prop. Code §5.0205 / SB 1577, eff. Jan 2024).
Generate disclosure to BOTH seller and buyer before any contract or marketing.
```

This is by design. The system will not produce a wholesale offer without compliance.

### Offer letter output

The pipeline saves a draft offer letter to `data/offer_letter_4003592.txt`:

```
DRAFT OFFER — 10022 Plainsman Ln Frisco, TX 75035
Parcel: 4003592 | County: Collin
Deal type: DT-AUCTION | Posture: Pre-Auction

Offer price: $403,800
Earnest money: $4,038 (to escrow within 3 days of effective date)
Option fee: $404 (for 7-day option period)
...

Key message: The auction is days away. Cash now is better than hoping
the property doesn't sell at auction. We close in 7 days, no repairs needed.

COMPLIANCE: PENDING — all comms are drafts for human review and send.
```

**All comms are drafts.** The system never sends anything. You review, edit, and send.

---

## 6. Portfolio Analytics

### Batch deal finder

```bash
# Process the full 335-record dataset
python3 find_deals.py

# Outputs:
#   data/current_deals.json — all 335 CPRs with enrichment
#   data/live_foreclosures.geojson — map of all deals
#   data/live_portfolio_stats.md — portfolio summary
```

### Portfolio stats include

- Deal count by county (Collin vs Dallas)
- Distribution by blast tier (T0-T3)
- Lender concentration analysis
- Average/median assessed values
- Geographic distribution by city/ISD

### GeoJSON export

Open `data/live_foreclosures.geojson` in any GIS tool (QGIS, Google Earth, geojson.io) to see all deals on a map with investment categories.

---

## 7. Batch Operations

### Full pipeline run with all integrations

```bash
# 1. Scrape live Collin notices (5 leads)
python3 pipeline.py --county collin --limit 5

# 2. Bulk DCAD enrichment for Dallas records
python3 pipeline.py --bulk-dcad

# 3. Full dataset analysis (335 records)
python3 find_deals.py

# 4. Rebuild Excel workbook
python3 build_workbook.py

# 5. Recalculate in LibreOffice
/Applications/LibreOffice.app/Contents/MacOS/soffice --headless --calc \
  --convert-to xlsx out/HERMES-REO_Model_Suite.xlsx --outdir out/recalc/

# 6. Verify everything
python3 pipeline.py --all-checks
python3 test_recalc.py
python3 test_live_portals.py
```

---

## 8. Testing & Verification

### Three test suites

| Test | Command | What it checks | Runtime |
|------|---------|---------------|---------|
| Self-checks | `python3 pipeline.py --all-checks` | All 21 modules pass `__main__` asserts | ~10s |
| Excel recalc | `python3 test_recalc.py` | All 8 deal types compute, GATE-VAL live, sensitivity works, S&U balances | ~5s |
| Live portals | `python3 test_live_portals.py` | Collin Clerk (31 instruments), DCAD (property data), Grayson (confirmed dead) | ~60s |

### Running all tests

```bash
source .venv/bin/activate
python3 pipeline.py --all-checks && \
python3 test_recalc.py && \
python3 test_live_portals.py && \
echo "✓ ALL TESTS PASS"
```

---

## 9. Troubleshooting

### Playwright browser not found

```bash
# Install Chromium for Playwright
playwright install chromium
```

### Collin Clerk returns 0 results

The Kofile SPA needs time to render. If you get 0 results:
1. The search ran too fast — the SPA didn't finish rendering
2. The owner name doesn't exist in Collin County records
3. Try the search directly in a browser to verify

### DCAD returns SearchError

DCAD's ASP.NET form is fragile. The adapter uses `page.type()` with 50ms delay for proper keyboard events. If it still fails:
1. Check if DCAD is down (visit dallascad.org manually)
2. The address may not exist in DCAD
3. Street suffix must be removed (search "BREEDLOVE" not "BREEDLOVE PLACE")

### Excel shows #NAME? or #N/A

Run the recalculation step — LibreOffice computes formula values:
```bash
/Applications/LibreOffice.app/Contents/MacOS/soffice --headless --calc \
  --convert-to xlsx out/HERMES-REO_Model_Suite.xlsx --outdir out/recalc/
```

If errors persist after recalc, rebuild the workbook:
```bash
python3 build_workbook.py
# Then recalc again
```

### GATE-VAL always fails on real deals

This is correct. Texas is a non-disclosure state. The system rejects assessed-value-only and Zestimate-only ARVs. You must provide MLS comps or a BPO from a licensed agent.

To override (not recommended):
1. Open the Excel workbook
2. Set `comp_source` to `vendor_avm` (will show "WARN — directional only")
3. Set `confidence_score` above the active floor
4. GATE-VAL will show "PASS" but the warning means the ARV is not reliable

### Grayson County returns nothing

The Grayson tax sale portal (`taxsearch.co.grayson.tx.us:8443`) is DNS NXDOMAIN — the subdomain no longer exists. The county website returns 403 on the tax sale page.

**Resolution**: Call the Grayson County Sheriff's office or Tax Assessor-Collector directly to request the tax sale list.

---

## Key Files Reference

| File | What it is | When to use |
|------|-----------|-------------|
| `pipeline.py` | Main pipeline runner | Every time you want to source or analyze deals |
| `build_workbook.py` | Excel workbook generator | When you need to modify the Excel models |
| `find_deals.py` | Batch deal finder | When you want to analyze the full 335-record dataset |
| `test_recalc.py` | Excel verification | After rebuilding the workbook |
| `test_live_portals.py` | Live portal tests | When verifying county portals are still up |
| `SYSTEM_REPORT.md` | Full system report | Reference for capabilities and architecture |
| `out/HERMES-REO_Model_Suite.xlsx` | Excel workbook (formula version) | Open in Excel to underwrite deals |
| `out/recalc/HERMES-REO_Model_Suite.xlsx` | Excel workbook (recalculated) | Read computed values without opening Excel |
| `data/current_deals.json` | Full dataset | Source of truth for all deals |
| `data/deal_memo_*.md` | Deal memos | Review before making offers |
| `data/offer_letter_*.txt` | Draft offer letters | Edit and send to sellers |