# SOURCE-REGISTRY v1 — County Data Source Directory

**Version:** 1.0
**Last verified:** 2026-08-19 (live HTTP checks)
**Scope:** Collin, Dallas, Grayson County, TX

> All endpoints verified via `curl -sL -o /dev/null -w "%{http_code}"` on Aug 19 2026.
> Sources returning 403 to curl are live BIS portals that require browser automation.

---

## Collin County

### Foreclosure Notices (Trustee Sales)
| Field | Value |
|-------|-------|
| **channel** | `foreclosure_notice` |
| **url** | https://apps2.collincountytx.gov/ForeclosureNotices |
| **http_status** | ✅ 200 |
| **access** | Web app (HTML scraping) |
| **data** | Trustee sale notices, filterable by sale date / city |
| **cadence** | Monthly (keyed to first Tuesday), refresh weekly |
| **notes** | §51.002(f-1) lets county remove notices after sale date — scrape-and-persist promptly |

### Collin CAD (Property Search)
| Field | Value |
|-------|-------|
| **channel** | `cad` |
| **url** | https://esearch.collincad.org |
| **http_status** | ⚠️ 403 to curl (live BIS portal, requires browser) |
| **fallback_url** | https://www.collincad.org (✅ 200) |
| **access** | Browser automation (Playwright) |
| **data** | Property characteristics, assessed/appraised value, ownership, exemptions |
| **notes** | Same BIS vendor as Grayson CAD; 403 is bot-detection, not down |

### Collin County Clerk (Official Records)
| Field | Value |
|-------|-------|
| **channel** | `clerk` |
| **url** | https://collin.tx.publicsearch.us |
| **http_status** | ✅ 200 |
| **access** | Web search (HTML scraping) |
| **data** | Deeds, deeds of trust, lis pendens, liens, notices |
| **notes** | Public records search; use for encumbrance stack + lien-priority ranking |

### Collin Tax Assessor (Properties for Sale)
| Field | Value |
|-------|-------|
| **channel** | `tax_sale` |
| **url** | https://www.collincountytx.gov/tax-assessor/properties-for-sale |
| **http_status** | ✅ 200 |
| **access** | Web page (HTML scraping) |
| **data** | Constable/tax sale properties, struck-off properties |
| **buyer_eligibility** | Written Statement ($10) from Tax Assessor — confirmed live on page |
| **notes** | Contact constable for the city where property is located for specific sale info |

### Collin Sales & Auctions Overview
| Field | Value |
|-------|-------|
| **channel** | `foreclosure_notice` (overview) |
| **url** | https://www.collincountytx.gov/government/sales-and-auctions |
| **http_status** | ✅ 200 |
| **access** | Web page |
| **data** | Overview of all county sales (foreclosures, constable/sheriff, surplus) |
| **sale_location** | Collin County Courthouse, 2100 Bloomdale Rd, McKinney, TX 75071 |
| **notes** | County staff do not conduct trustee sales; trustees run the auction |

### Collin District Clerk (Tax-Suit Case Search)
| Field | Value |
|-------|-------|
| **channel** | `tax_suit` |
| **url** | TBD — verify in WS-00 (district clerk case search portal) |
| **http_status** | Not yet verified |
| **access** | TBD |
| **data** | Tax-foreclosure suit filings (upstream signal, 6-18 months before sale) |
| **notes** | This is the upstream edge — earliest public warning of tax distress |

---

## Dallas County

### County Clerk Foreclosure Notices
| Field | Value |
|-------|-------|
| **channel** | `foreclosure_notice` |
| **url** | https://www.dallascounty.org/government/county-clerk/recording/foreclosures.php |
| **http_status** | ✅ 200 |
| **access** | Web page with PDF listings by city |
| **data** | Trustee sale notices, filterable by sale date or city |
| **sale_location** | North side of George Allen Courts Building |
| **cadence** | Monthly (first Tuesday), listings posted as PDFs by city |
| **notes** | PDFs named by city (e.g., Addison_4.pdf, Balch-Springs_1.pdf); parse PDF for structured data |

### Dallas CAD (Property Search)
| Field | Value |
|-------|-------|
| **channel** | `cad` |
| **url** | https://www.dallascad.org |
| **http_status** | ✅ 200 |
| **access** | Web search (SearchAddr / SearchAcct) |
| **data** | Property characteristics, assessed/appraised value, ownership, exemptions |

### Dallas Tax Office (Sheriff Sales — Online Auction)
| Field | Value |
|-------|-------|
| **channel** | `tax_sale` |
| **url** | https://www.dallascounty.org/departments/tax/sheriff-sales.php |
| **auction_url** | https://dallas.texas.sheriffsaleauctions.com |
| **http_status** | ✅ 200 |
| **platform** | RealAuction.com (online auction) |
| **access** | Register at RealAuction.com; call (877) 361-7325 or email customerservice@realauction.com |
| **data** | Tax foreclosure sheriff sales — conducted as ONLINE AUCTION (not in-person) |
| **notes** | Dallas is the only county with online tax sales; Collin and Grayson are in-person |

### Dallas Public Works (Tax Foreclosure Resales + Struck-Off)
| Field | Value |
|-------|-------|
| **channel** | `struck_off` |
| **url** | https://www.dallascounty.org/departments/pubworks/property-division.php |
| **http_status** | ✅ 200 |
| **access** | Web page |
| **data** | Tax foreclosure resales, struck-off properties (sealed bid / Daily Commercial Record) |

### Dallas Tax Lookup / Payment
| Field | Value |
|-------|-------|
| **channel** | `tax_office` |
| **url** | https://www.dallascounty.org/departments/tax/pay-property-tax.php |
| **http_status** | ✅ 200 |
| **access** | Web lookup |
| **data** | Property tax status, delinquency amounts |

---

## Grayson County

### County Clerk Foreclosure Info
| Field | Value |
|-------|-------|
| **channel** | `foreclosure_notice` |
| **url** | https://www.co.grayson.tx.us/page/cclk.foreclosure |
| **http_status** | ✅ 200 |
| **access** | Web page |
| **data** | Foreclosure auction info — first Tuesday, west door of courthouse |
| **sale_location** | West door of Grayson County courthouse |
| **notes** | County clerk has no involvement in foreclosure process; trustees conduct the sale |

### Grayson CAD (Property Search)
| Field | Value |
|-------|-------|
| **channel** | `cad` |
| **url** | https://esearch.graysonappraisal.org |
| **http_status** | ✅ 200 |
| **access** | Browser automation (BIS portal, same vendor as Collin) |
| **data** | Property characteristics, assessed/appraised value, ownership, exemptions |
| **notes** | Full HTML returned; Kendo UI-based search; works with browser automation |

### Grayson Tax Sheriff Sale
| Field | Value |
|-------|-------|
| **channel** | `tax_sale` |
| **url** | https://taxsearch.co.grayson.tx.us:8443/Sales/ssale |
| **http_status** | ❌ 000 (timeout — needs browser verification) |
| **access** | TBD — verify in WS-00 |
| **data** | Sheriff sale properties |
| **notes** | HTTPS on port 8443 timed out via curl; may need different port, TLS config, or may be down. **Priority verification item for WS-00.** |

### Grayson Tax Payments
| Field | Value |
|-------|-------|
| **channel** | `tax_office` |
| **url** | https://grayson.propertytaxpayments.net |
| **http_status** | ⚠️ 403 to curl (likely works in browser) |
| **access** | Browser automation |
| **data** | Tax payment status |

### Grayson Struck-Off Properties
| Field | Value |
|-------|-------|
| **channel** | `struck_off` |
| **url** | TBD — spec references /Struck-Off-Properties path; verify in WS-00 |
| **http_status** | Not yet verified |
| **access** | TBD |

---

## Vendor Adapters (Paid Enrichment + Comps)

### ATTOM Data Solutions
| Field | Value |
|-------|-------|
| **channel** | `vendor` |
| **url** | https://api.developer.attomdata.com |
| **http_status** | 403 (auth required — API is live) |
| **api_type** | REST API |
| **data** | Foreclosure data, property characteristics, AVM, ownership, deed/mortgage records |
| **coverage** | All 3 counties (national) |
| **rigor_tier** | 0 (vendor AVM — directional only, never sole basis for ARV) |
| **selection_notes** | Top candidate for automated foreclosure + property data enrichment. API docs at developer.attomdata.com. |

### HouseCanary
| Field | Value |
|-------|-------|
| **channel** | `vendor` |
| **url** | https://api.housecanary.com |
| **http_status** | ✅ 200 |
| **api_type** | REST API |
| **data** | AVM, property characteristics, rental comps |
| **coverage** | All 3 counties (national) |
| **rigor_tier** | 0 (vendor AVM — directional only) |
| **selection_notes** | Good for rental estimate + directional AVM. Never sole basis for ARV in non-disclosure state. |

### Regrid
| Field | Value |
|-------|-------|
| **channel** | `vendor` |
| **url** | https://docs.regrid.com |
| **http_status** | ✅ 200 |
| **api_type** | REST API |
| **data** | Parcel boundaries, ownership, land use, tax info |
| **coverage** | All 3 counties (national) |
| **rigor_tier** | N/A (parcel data, not comps) |
| **selection_notes** | Best for parcel geometry + ownership overlay. Not a comp source. |

### TaxNetUSA
| Field | Value |
|-------|-------|
| **channel** | `vendor` |
| **url** | https://www.taxnetusa.com |
| **http_status** | ✅ 200 |
| **api_type** | Portal-based (bulk export, not real-time API) |
| **data** | Bulk CAD + delinquent tax data for TX counties |
| **coverage** | All 3 counties (TX-specific) |
| **rigor_tier** | N/A (data source, not comps) |
| **selection_notes** | TX-specific depth; good for delinquent-tax bulk lists. No real-time API — bulk export workflow. |

### PropStream
| Field | Value |
|-------|-------|
| **channel** | `vendor` |
| **url** | https://www.propstream.com |
| **http_status** | ✅ 200 |
| **api_type** | ❌ No public API (web platform only) |
| **data** | Property data, AVM, comps, skip-trace |
| **selection_notes** | Portal-only; cannot automate without scraping. Skip for v0.1. |

### BatchData
| Field | Value |
|-------|-------|
| **channel** | `vendor` |
| **url** | https://www.batchdata.io |
| **http_status** | ✅ 200 |
| **api_type** | API (verify details in WS-00) |
| **data** | Skip-trace, property data, delinquent tax |
| **selection_notes** | Potential skip-trace source. Verify API access + pricing in WS-00. |

### PropertyRadar
| Field | Value |
|-------|-------|
| **channel** | `vendor` |
| **url** | https://www.propertyradar.com |
| **http_status** | ✅ 200 |
| **api_type** | Portal-based (no public API confirmed) |
| **data** | Distressed property lists |
| **selection_notes** | Portal-only; good for manual lead list building. Skip for v0.1 automation. |

---

## Access Method Summary

| Source type | Access method | Notes |
|------------|--------------|-------|
| County web pages (HTML) | `httpx` async HTTP + HTML parsing | Most county pages return 200 to curl |
| BIS CAD portals (Collin, Grayson) | Playwright browser automation | 403 to curl; need full browser with JS |
| Dallas foreclosure PDFs | `httpx` download + PDF parsing | PDFs by city, parse for structured data |
| Dallas RealAuction | Browser automation or API (verify) | Online auction platform |
| Vendor REST APIs (ATTOM, HouseCanary, Regrid) | `httpx` with API key | Need credentials + pricing |
| Portal-only vendors (PropStream, PropertyRadar) | Manual / skip for v0.1 | No public API |

---

## Verification Status Legend

- ✅ 200 — live, accessible via HTTP
- ⚠️ 403 — live but blocks curl; requires browser automation
- ❌ 000 — unreachable via curl; needs browser verification
- TBD — not yet verified; priority for WS-00

---

## Grayson County — Manual Channel

**Added:** 2026-08-25 · **Trigger:** adapter probes (`src/adapters/grayson_tax.py`, Aug 2026) confirmed the automated tax-sale channels dead.

### Confirmed-dead portals (per adapter probes)

| Portal | URL | Probe result |
|--------|-----|--------------|
| Tax sale search (`tax_sale`) | https://taxsearch.co.grayson.tx.us:8443/Sales/ssale | ❌ **DNS NXDOMAIN** — dead subdomain; HTTP and HTTPS both resolve nowhere |
| County tax page (`tax_sale` fallback) | https://www.co.grayson.tx.us/page/tax.sale | ⚠️ **403 Forbidden** |
| Sheriff's office page | https://www.co.grayson.tx.us/page/gcso.home | Loads, but carries no sale listings |
| Tax Assessor-Collector portal | grayson.propertytaxpayments.net | Payments only — no sale lists |

### Manual procedure (only current path to Grayson leads)

1. **Contact Grayson County Sheriff's Office** — request the current sheriff/constable sale list (judicial tax sales; sales occur first Tuesday at the courthouse, west door per county clerk).
2. **Contact Grayson County Tax Assessor-Collector** — request the delinquent-tax suit / struck-off property list (upstream of sale).
3. **Hand-enter each lead** into the pipeline as a CPR (INBOX column), with `source` provenance recording which office supplied it.
4. **Future home:** a manual lead-entry path (CLI/form) so step 3 stops being hand-edited JSON — tracked in OPPORTUNITY_MAP.md roadmap (SYSTEM_REPORT §11 is archived) ("Grayson County: no data source").

> Note: `co.grayson.tx.us` returns 403 to curl but renders in a browser — the county site remains manually browsable even though automation is blocked.