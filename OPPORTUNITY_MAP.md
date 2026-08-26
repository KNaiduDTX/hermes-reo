# HERMES-REO — Opportunity Map
**Generated:** Aug 26, 2026 · **Method:** six-lens read-only analysis swarm (architecture, reliability, functionality, testing, data, DX) over ~15,900 LOC, grounded in empirical coverage (35% total, nine modules at 0%) and radon complexity artifacts, synthesized across ~60 raw findings into 43 deduplicated opportunities.
**Status of prior remediation:** all original audit Criticals/Highs remain fixed; this map covers what comes *next*.

---

## Executive Summary

The sourcing→gates→underwrite→memo→offer spine runs end-to-end, and the pure financial core + provenance ledger are genuinely well-built. Value now leaks at four seams, each verified by spot-checks:

1. **Silent failure** — scrape collapses masquerade as clean data (`collin_clerk.py:156` swallows transport errors into `[]`, passing GATE-LEGAL-LIEN on unverified title; `dallas_foreclosure.py:99-101` returns `[]` on portal redesign; zero-leads exits 0).
2. **Broken identity** — positional parcel_ids collide (**134 duplicate ids** across 335 rows) and merge-on-write destroys postponement history every run.
3. **Built-but-unwired capability** — HITL `.valued.json` has zero consumers so GATE-VAL blocks 100% of deals today (196 REJECTED / 139 NO DATA); the transaction machine, route optimizer, TSP planner and weekly digest all sit idle.
4. **Fabricated analytics inputs** — 196/198 paired rows carry an exactly-uniform 25/24 cad_2026/cad_2025 ratio, so any market analytics would be invented until repaired.

---

## TRACK 1 — EXTEND FUNCTIONALITY

| # | Opportunity | Impact | Effort | First step |
|---|---|---|---|---|
| F1 | **Close the HITL loop**: consume `.valued.json` via `--resume-valued` — pipeline.py:505 hardcodes assessed-value exit while hitl.py already writes valued CPRs that nothing consumes; price_deal/render_memo/generate_offer sit idle downstream | High | S | Add `--resume-valued <file>` loading via hitl.load_cpr, running steps 5–8 |
| F2 | **Comps platform** ⭐ strategic: reusable comp DB (`data/comps/{property_id}.json`) + batch HITL intake — amortize one comp pull across a subdivision instead of evaporating into single CPR files | High | L | Persist accepted CompRecords keyed by address+sale_date; `--comps-dir` batch loop |
| F3 | **Deal-detail page** + GET /api/cpr/{parcel} — DASHBOARD_PLAN drilldown unbuilt while rich artifacts exist (47-field CPRs, provenance ledger invisible in any UI) | High | M | Reuse `_read_json`+DataUnavailable pattern; fifth index.html view |
| F4 | Dashboard POST actions (generate-offer, clerk-search) — negotiation.generate_offer is live but unreachable from UI | High | S | POST /api/generate-offer writing drafts to data/ (draft-only posture preserved) |
| F5 | **Batch underwriting pass + CSV/XLSX investor pack** — price_deal never runs in batch; find_deals emits only directional MAO off CAD guesses | High | M | Loop price_deal over valued CPRs; parameterize workbook builders w/ assumptions dict |
| F6 | **Complete the underwriting router** — only 4 of 8 DealTypes route to real models; SHORT/SUBTO silently get flip economics; wholesale drops closing costs | High | M | Mirror Excel tab formulas into short_sale/subject_to functions |
| F7 | Fix 115-vs-30 coordinate leak — flat-record path bypasses coordinate mapping; expose idle TSP route planner | Medium | S | Build GeoJSON from enrich_from_flat_record CPRs |
| F8 | Surface transaction state machine + statutory redemption calendar (built, tested, imported by nobody) | Medium | S | `transaction status` CLI + dashboard card |
| F9 | Wire orphaned `weekly_digest` as notification surface ("new this week" vs carried-forward) | Medium | S | Call from find_deals.main(); GET /api/digest |
| F10 | County-expansion kit: wire GraysonTaxSaleAdapter (implemented, imported nowhere) + add-a-county recipe | Medium | M | Register in dispatch w/ graceful dead-endpoint degradation |
| F11 | /api/market?groupby=zip\|isd\|city aggregations (geojson_export already computes-then-discards city $/sqft) | Medium | M | Group-by endpoint + dashboard filters |

## TRACK 2 — IMPROVE RELIABILITY

| # | Opportunity | Impact | Effort | First step |
|---|---|---|---|---|
| RL1 | **Collin clerk: raise on transport failure** instead of swallowing into `[]` — currently GATE-LEGAL-LIEN can pass on unverifiable title search (worst silent false-negative remaining) | High | S | Let nav/form errors raise; test error→lien_lookup_status='error'→hitl |
| RL2 | **Fail loudly on scrape shrinkage** — portal redesign returns [] and exits 0; enforce --min-leads floor vs prior-run count + PORTAL-SHAPE-CHANGED marker | High | S | Shape-change detection in dallas_foreclosure/collin paths |
| RL3 | Quarantine per-PDF failures; invalidate poisoned cache (unguarded PdfReader crashes whole county loop; truncated pre-fix cached PDFs never heal) | High | S | try/except tallying; synthetic-corrupt-PDF test |
| RL4 | **Operator run summaries** (`data/run_summary.json`) + documented exit-code catalog — zero-leads vs partial degradation both exit 0 today; bulk conflates never-attempted with failed | High | M | Emit structured summary from every mode |
| RL5 | Weekly scheduled live-portal smoke tier in CI (chromium install + `pytest -m live`) — markup breakage currently detected only by human memory | High | S | Cron workflow posting on red |
| RL6 | **Data durability**: `make snapshot` (checksummed archives) + vendored external lucid seed — data/ holds sole copies of hours of scraping, gitignored entirely | High | M | Snapshot target + restore docs |
| RL7 | Consume `details_status='error'` (written but read nowhere — failed scrapes enrich to ENRICHED full of Unknowns) | Medium | S | Route to dead/retry queue in enrich_lead |
| RL8 | Typed source errors (TransientSourceError/ParseError) + shared net-retry helper (census's good policy never generalized; 13 bare excepts blur portal-down vs parser-broke) | Medium | M | src/errors.py + src/net_retry.py |
| RL9 | Make DCAD parse-yield collapse visible (regex decay → endless cache-miss re-searches, unmeasured) | Medium | S | HTML fixture tests + yield % in summaries |
| RL10 | File-lock concurrent writers (dcad_cache, geocode ledger, current_deals RMW paths drop each other's writes) | Medium | M | fcntl.flock helper + regression test |
| RL11 | Unify census negative caches (CLI ledger vs enrich_batch 'Success'-only check never consult each other) | Low | S | One ledger module |

## TRACK 3 — REDUCE TECH DEBT

| # | Opportunity | Impact | Effort | First step |
|---|---|---|---|---|
| TD1 | **Declare CPR's de-facto extra fields** + typed accessor — kills the hasattr walls making analyze_deal cc=62 and portfolio_stats cc=42 | High | M | Optional[ProvenanceField] fields + CPR.get() |
| TD2 | **One verification framework**: port __main__ self-checks to pytest; registry omits addr/dates/io_utils/hitl/transaction/disambiguate/policy so `make checks` passes without them | High | M | pkgutil-derived MODULES + meta-test |
| TD3 | source_registry becomes single URL authority (REGISTRY duplicated independently in ≥6 adapters; get_endpoint read by nothing) | High | S | Adapters consume get_endpoint + sync assertion |
| TD4 | Give SourceAdapter a real contract (+conformance test) — DallasCADAdapter.fetch returns dict not leads; 5 function-modules never subclass | High | M | Decide membership; conformance test |
| TD5 | End split-brain CPR (de)serialization — to_dict exists, its inverse is an 82-line 4-pass loader fused inside hitl.load_cpr (cc=32) | Medium | M | CPR.from_dict classmethod |
| TD6 | Unify the two notice parsers (dallas_foreclosure cc=32 vs notice_ocr duplicate the same no-fabrication rules with different types — rule fixes must land twice or records diverge) | Medium | M | Delegate to shared extraction engine + differential test |
| TD7 | src/coerce.py — six duplicated coercion helpers disagree on failure semantics (_parse_int('12.5')→125!) | Medium | S | to_int/to_money/to_float contract |
| TD8 | Decompose cc≥42 hotspots along existing seams; retire dual-schema branch in synthesis; find_deals utcnow (hook exempts root scripts) | Medium | M | Extract leverage/rehab helpers |
| TD9 | Mechanical extractions: render_memo's ten sections (cc=57), declarative flat-field map (cc=32), geojson two-pass merge (cc=39) | Medium | M | Byte-identical-output asserted refactors |
| TD10 | Type-hint root orchestrators (build_workbook 0%, pipeline 41.7%) + scoped mypy | Medium | M | Annotate main/analyze_deal; [tool.mypy] scoped |

## TRACK 4 — FOUNDATIONS (testing · DX · data)

| # | Opportunity | Impact | Effort | First step |
|---|---|---|---|---|
| FN1 | **Identity spine** ⭐ strategic: stable notice_id + durable property_id unified across merge/cache/comps/disambiguation — positional ids collide 134×, every subsystem mints incompatible keys | High | L | sha1(normalized addr+lender+debt+date) w/ backfill mapping |
| FN2 | **Append-only scrape history** ⭐ (with FN1): postponement/price-drop signal destroyed every run today; zero scraped_at/source_url metadata | High | M | data/history/YYYY-MM-DD.json appends |
| FN3 | **Repair degenerate cad_value_2025** — 196/198 rows exactly 25/24 ratio (one year derived upstream, enrichment.py:99); blocks honest analytics | High | S | Trace uplift; persist raw strings + distinct-ratio assertion |
| FN4 | Persist address-quality + provenance columns (zip5/city normalized, invalid_reason, scraped_at); triageable geocode ledger (currently discards the failed query itself) | High | M | Append columns during regeneration |
| FN5 | **Pin money-path contracts with offline fixture tests** — reviewer's 8 fake-signal checks, parse_notice_text no-fabrication contract, hitl hash-chain rehydration, generate_offer matrix: ALL at 0% coverage | High | M | Port reviewer __main__ asserts first |
| FN6 | Coverage instrumentation + fail-under ratchet in CI (pytest-cov unpinned; no make coverage; nine 0% modules invisible) | High | S | --cov-fail-under slightly above 35, only-increases |
| FN7 | **pyproject.toml + Python 3.11 convergence** — local venv is EOL 3.9.6 vs CI 3.11; ruff select hand-copied ×3 floating above requirements pin; hooksPath bootstrap missing from setup | High | M | pyproject absorbing configs; recreate venv on 3.11 |
| FN8 | README.md doc-router + archive contradictory SYSTEM_REPORT/DASHBOARD_PLAN to docs/archive/ with status banners (~3,300 lines competing unlabeled; report claims 29 modules/7,951 LOC vs ~15,900 today) | High | S | Routing-table README |
| FN9 | Census client + Grayson parser + registry lookup tests (httpx.MockTransport needs no new dep) | Medium | S | Table-driven retry/backoff cases |
| FN10 | Property-based pilot (hypothesis) on pure cores: normalizer idempotence, ladder monotonicity, date round-trips | Medium | M | Generated address-shaped strings |
| FN11 | Dashboard frontend: extract static/app.js + API-payload contract test (747 lines inline; field rename breaks rendering silently) | Medium | M | Python contract test parsing data-keys |

---

## Quick Wins (high impact ≤ M)
1. **F1** Close the HITL loop — converts the #1 gap into a working revenue path over existing plumbing
2. **RL1** Clerk raise-not-swallow — closes the last silent clean-title false-negative
3. **FN3** Repair degenerate cad_value_2025 — unblocks honest analytics before they ship fabricated
4. **TD3** Registry single URL authority — cheapest guard against live-portal config drift
5. **F7** Fix 115-vs-30 coordinate leak — ~4× map coverage from data that already exists

## Strategic Bets
1. **FN1+FN2** Identity & history spine — everything analytical keys off it; unlocks postponement-frequency and price-drop tracking the pipeline currently destroys
2. **F2** Comps platform — scales human comp pulls across all 335 blocked deals
3. **F3+F4+F5** Operational cockpit — deal-detail + actions + batch pricing/export turns the spectator dashboard into the daily driver

## Non-Goals (deliberately deferred — do not re-propose)
PostgreSQL/Dagster (ADR-0001 triggers) · client-side JS formula engine (server recompute instead) · PDF-generation libs (print CSS) · SMTP/webhook alerting (digest artifact suffices) · frontend frameworks/build tooling · paid MLS/RESO feeds now (build the vendor-neutral comps seam only) · auto-send/e-signature (draft-only posture per LEGAL_RULES) · new county adapters before TD4/TD3 land · wholesale scraper rewrite · repo-wide strict mypy/mutation rollout · reviving dead Grayson portals · replacing the Excel workbook path.

---

*Method note: six parallel read-only analyst lenses (architecture, reliability, product, testing, data, DX) each grounded in empirical pytest-coverage (35%, nine 0% modules) and radon complexity artifacts; findings deduplicated by a synthesis pass; key claims spot-checked against source (file:line cited in rationale).*
