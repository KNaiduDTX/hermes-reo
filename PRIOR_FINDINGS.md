# PRIOR FINDINGS — lucid-noether Reuse Analysis

**Source:** `/Users/knaidu/Documents/antigravity/lucid-noether` (May 2026)
**Status:** Analyzed Aug 19 2026. Working crawlers with real scraped data.

---

## What the prior attempt built

A 6-step pipeline (subprocess-orchestrated, no DB):
1. Crawl Collin (Blazor SPA) + Dallas (PDF bundles) foreclosure portals
2. Enrich with Census geocoding (tract/block group)
3. OCR/parse scanned notice images for lender/debt/trustee
4. Export GeoJSON point map with investment categories
5. TSP route optimization for drive-by inspections
6. Statistical analysis + investment target selection

**Results:** 335 records scraped (2 Collin, 333 Dallas), 194 with extracted
debt, $93.4M total portfolio debt exposure.

---

## Porting guide: what to reuse, what to fix, what to discard

### ✅ Reuse directly (working code, no changes needed)

#### `foreclosure_crawler.py` — Collin County Blazor SPA scraper
- **Lines 48-62:** `window.setProperties` SignalR interceptor — the critical
  trick. Collin's portal is a Blazor Server app that pushes data via SignalR.
  No HTTP API exists. This interceptor captures the data push.
- **Lines 25-84:** `get_foreclosure_list()` — Playwright async function that
  navigates to the portal, injects the interceptor, polls for data. Returns
  listing array with `entryID`, `propertyAddress`, `lat`, `long`.
- **Lines 86-159:** `scrape_detail_page()` — urllib (not Playwright) fetch of
  `/DetailPage/{entry_id}`. BeautifulSoup parser for MudBlazor grid layout:
  - Finds `h2` headers ("Property Information", "Improvement Information", etc.)
  - Parses `mud-grid` divs with `item-title` class labels
  - Extracts: property_info, improvement_info, jurisdiction_info, owner_info,
    tax_info, scanned_notice_image (base64 JPEG)
- **Rate limiting:** 1.0s sleep between detail page fetches

#### `dallas_crawler.py` — Dallas County PDF scraper
- **Lines 48-100:** `parse_portal_links()` — scrapes `blockquote` elements
  with IDs starting with `display` (accordion monthly blocks). Extracts PDF
  URLs with city names from filenames.
- **Lines 132-186:** `split_pdf_pages()` — splits multi-page PDFs into
  individual notices using regex boundary detection:
  ```python
  new_notice_keywords = [
      r"NOTICE OF [A-Z ]*SALE",
      r"NOTICE OF ACCELERATION",
      r"NOTICE OF FORECLOSURE SALE",
      r"ASSERT AND PROTECT YOUR RIGHTS",
      r"DEED OF TRUST INFORMATION",
      r"APPOINTMENT OF SUBSTITUTE TRUSTEE"
  ]
  ```
  Also detects filing stamps and page headers for boundary detection.

#### `enrich_census.py` — Census geocoder
- **Lines 21-64:** `geocode_coordinates()` — lat/lng → census tract/block
  via `geocoding.geo.census.gov/geocoder/geographies/coordinates`
- **Lines 66-118:** `geocode_address()` — address → lat/lng + census tract
  via `geocoding.geo.census.gov/geocoder/geographies/onelineaddress`
- Retry with exponential backoff (3 attempts, 1s base)
- Rate limited: 0.5s between calls, batch limit of 30

#### `route_optimizer.py` — TSP for drive-by inspections
- **Lines 11-19:** `haversine_distance()` — great-circle distance
- **Lines 21-39:** `solve_tsp_nearest_neighbor()` — greedy NN heuristic
- **Lines 41-73:** `optimize_route_2opt()` — 2-opt improvement (500 iter cap)
- **Lines 107-133:** Dynamic courthouse selection (Collin vs Dallas based on
  target cluster)
- Outputs: JSON route schedule + Markdown report with per-stop timing

### ⚠️ Reuse + fix (working but with known bugs)

#### `dallas_crawler.py` — `parse_notice_text()` (lines 188-342)
**Address extraction (lines 195-223):** Works for most notices but fails on
~14% (42/333 "Unknown"). Fix: tighten the zip-code-anchored regex; handle
multi-line addresses where zip is on a separate line.

**City extraction:** Broken. The `get_city()` function in `analyze_foreclosures.py`
and `export_geojson.py` uses a known-cities list with word-boundary regex, but
many addresses parse as "Court", "Way", "Lane", "Drive" (street suffixes
leaking through). Fix: extract city from the address pattern
`<street>, <city>, TX <zip>` after the comma, not by matching against a
known-cities list.

**Debt extraction (lines 263-303):** Works for 58% (194/333). The regex
patterns for "Original Principal", "Unpaid Principal", etc. miss some notice
formats. Fix: add more pattern variants; cross-reference with the scanned
notice image via OCR as fallback.

**Fabricated appraised values (lines 403-404):**
```python
"Appraised Value (2025)": f"${details['debt']*1.2:,.2f}" if details["debt"] > 0 else "$0.00",
"Appraised Value (2026)": f"${details['debt']*1.25:,.2f}" if details["debt"] > 0 else "$0.00",
```
This is **fake data** — multiplying debt by 1.2/1.25 to guess appraised value.
**Discard this entirely.** Replace with real DCAD lookup via the Dallas CAD
adapter.

### ❌ Discard (fake data or wrong approach)

#### `parse_notices_ocr.py` — heuristic fallback fabricates data
When OCR fails (tesseract not available or image unreadable), this script
**fabricates** lender, trustee, and debt values:
```python
# Lines 66-70: If OCR fails, MAKE UP data based on hash of entry_id
lender = LENDERS[entry_hash % len(LENDERS)]
trustee = f"{TEXAS_TRUSTEES[entry_hash % len(TEXAS_TRUSTEES)]}, Substitute Trustee"
sale_time = "10:00 AM..." if (entry_hash % 2 == 0) else "1:00 PM..."
```
And debt is estimated as `val_2026 * 0.8` or a leverage-ratio heuristic. This
is exactly the "confident-but-wrong" fake-signal the spec's GATE-VAL and
AGENT-REV are designed to catch and reject. **Never port this logic.**

#### Investment analysis (`find_best_investments.py`, `export_geojson.py`)
The "Play A / Play B" classification is naive ISD/city heuristics with no
underwriting, no ARV, no comps, no confidence scoring. Replace entirely with
the spec's deal-type taxonomy + underwriting models + GATE-VAL.

#### JSON file storage + subprocess orchestration
Replace with PostgreSQL (CPR storage) + Dagster (pipeline orchestration).

---

## Data structure from prior attempt (for fixture reuse)

### Collin record (from real scrape):
```json
{
  "entry_id": 4003592,
  "property_address": "10022 Plainsman Ln Frisco, TX 75035",
  "latitude": 33.17922234,
  "longitude": -96.79394064,
  "detail_page_url": "https://apps2.collincountytx.gov/ForeclosureNotices/DetailPage/4003592",
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

### Dallas record (from real scrape):
```json
{
  "entry_id": "DAL_March_Addison_1",
  "property_address": "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334",
  "latitude": 0.0,
  "longitude": 0.0,
  "county": "Dallas",
  "city": "Addison",
  "details": {
    "property_info": {
      "Address": "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334",
      "Type": "Residential Single Family (A1)",
      "Deed Date": "Tuesday, the 3rd day of March, 2026",
      "Commercial": "False"
    },
    "improvement_info": {
      "State Property Code": "Residential Single Family (A1)",
      "Improvement Area": "Unknown",
      "Year Built": "Unknown",
      "Bedrooms": "Unknown",
      "Bathrooms": "Unknown"
    },
    "scanned_notice_ocr_data": {
      "foreclosing_lender": "CTX MORTGAGE COMPANY",
      "substitute_trustee": "John Beazley, Logan Thomas, ...",
      "scheduled_sale_time": "10:00 AM or within three hours thereafter.",
      "estimated_default_balance": 309600.0,
      "original_principal": 309600.0,
      "all_extracted_balances": [309600.0],
      "ocr_engine_mode": "pypdf Text Extractor + Regex Boundary Splitter",
      "status": "Success"
    }
  }
}
```

**Key difference:** Collin records have rich property/improvement/owner/tax
data from the detail page. Dallas records only have what's extractable from
the PDF notice — no CAD data, no improvement details. The Dallas adapter must
cross-reference DCAD to fill the gaps.

---

## Prior attempt statistics (from `stats_summary.md`)

- 335 total notices (2 Collin, 333 Dallas)
- 176 unique physical assets
- 93.4% individual owners, 6.6% corporate
- Median appraised value: $248,155
- Mean price/sqft: $149.49 (only 2 records had sqft data — Dallas had none)
- 99% of properties appreciated 2025→2026 (mean +4.08%)
- Top cities: Dallas (124), Mesquite (19), Garland (18), Lancaster (15)
- Total extracted portfolio debt: $93.4M
- Median debt: $198,524

**What this tells us:** The data pipeline works but the Dallas data is
shallow (no CAD enrichment). The Collin data is rich but the prior attempt
only scraped 2 records (likely a test run with `--limit`). The full Collin
portal has many more records available.