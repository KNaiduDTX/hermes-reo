# HERMES-REO — Full Battle Test & Improvement Audit
**Date:** Aug 24, 2026 · **Scope:** all 29 Python modules (~7.9K LOC), Excel workbook, tests, data artifacts, repo hygiene
**Method:** independent execution of every verification claim + 3 parallel deep-code audits (root scripts, core domain, adapters/security). Every Critical/High finding below was verified against actual source; several were runtime-reproduced.

---

## PART 1 — BATTLE TEST RESULTS (what actually holds up)

| Check | Result |
|---|---|
| `py_compile` all 29 modules | ✅ PASS |
| `pipeline.py --all-checks` (21 modules) | ✅ 21/21 pass |
| `pipeline.py --fixture` end-to-end (offline) | ✅ completes; gates, memo, offer, geojson all produced |
| `build_workbook.py` full rebuild | ✅ reproducible; 15 tabs, 97 named ranges |
| `test_recalc.py` vs recalc'd workbook | ✅ passes — **but see T-fraud finding below: it can't fail** |
| Secrets scan (API keys, tokens, passwords) | ✅ clean |
| Dangerous patterns (`eval`/`exec`/`pickle`/`shell=True`/`verify=False`) | ✅ none in src (⚠️ one TLS exception, see S1) |
| Import graph | ✅ clean DAG, no circular imports |
| Mutable default args / O(n²) loops / races | ✅ none found |

**Battle-test surprises discovered during verification:**
1. `--all-checks` does not stop after checking — it falls through into a **live scrape** of the Collin County portal (verification flag = unintended network side effect).
2. Running `--fixture` **overwrites production data**: `data/portfolio_stats.md` now shows "Total properties: 1" (was the 338-record portfolio; survivor is `live_portfolio_stats.md`). Test runs mutate prod artifacts because outputs share paths.
3. Bare `pytest` collects and would execute live-portal scrapes at two real county sites (no skip markers), plus module-level code in `test_recalc.py`.
4. Named ranges: **97 fresh build → 91 after LibreOffice recalc round-trip** (silent loss, never checked).
5. Doc drift everywhere: report claims 335 records / 7,951 LOC / "0 TODO" — reality: 338 records / 7,919 LOC / **14 deferred-work markers hidden under a nonstandard `ponytail:` tag** that the TODO scan doesn't catch.

---

## PART 2 — CRITICAL FINDINGS (wrong money answers)

### C1. Homestead false-positive via `"HS"` substring — wrong redemption class
`src/enrichment.py:90` — `"HS" in taxing.upper()` matches any entity containing the letters HS. **"City of SacHSe"** is a real Collin County city → its properties are flagged homestead → redemption class flips from 180-day/25% premium to **730-day/50%**, poisoning `gate_legal_redemption`, tax-deed yield math, and lockup risk.
**Fix:** token-match the parenthesized exemption code: `re.search(r'\(HS\)|\bHOMESTEAD\b', taxing, re.I)`.

### C2. Negative bid ceiling becomes a negative purchase price → fake profit
`src/pricing.py:32-39` — no `max(0, …)` clamp; docstring says `min(MAO, cash, …)` but MAO isn't in the min(). Verified: ARV $200k / rehab $50k / $100k surviving liens → ceiling **−$10,000**; `price_deal` then uses it as the purchase price and the sensitivity grid shows **+$134k profit on an unbuyable deal**.
**Fix:** clamp to 0, include MAO in the min(), route ceiling ≤ 0 to DEAD.

### C3. Deal memo recommends PROCEED without looking at economics
`src/deal_memo.py:183-189` — PROCEED requires only GATE-VAL pass + not optimistic_only. A deal with **−$80k net profit**, failed legal gates, or offer > bid ceiling still renders "**PROCEED**".
**Fix:** require net_profit > threshold, AGENT-REV pass, and lien-gate pass.

### C4. BRRRR annual cash flow ADDS debt service instead of subtracting
`build_workbook.py:794` — `B24 = B12-B13` where `B13 = PMT(...)*12` is negative (Excel convention) → NOI − (negative) = **NOI + |debt service|**, overstating cash flow by 2× debt service; feeds Cash-on-cash B25. Rental DCF does it right (`H=F+G`, L851).
**Fix:** `=B12+B13`.

### C5. Weighted ARV median drops comp #6 and averages zeros
`build_workbook.py:473` — `=IF(B13>=3,MEDIAN(K5:K9),...)`: grid spans rows 5–10 (**6 comps**) but median stops at K9; count B13 scans B5:B10. The comment claims non-zero filtering but it's a plain MEDIAN — exactly 3 entered comps yields `MEDIAN(x,y,z,0,0)`. This one cell feeds **every downstream MAO/bid/verdict in the workbook**.
**Fix:** zero-filtered median over K5:K10.

### C6. BRRRR verdict declares "infinite return" before checking DSCR
`build_workbook.py:800` — verdict order: GATE-VAL → cash-recycled ≤ 0 → DSCR ≥ min. A deal with **DSCR 0.63** (< the workbook's own 1.25 minimum) prints *"Refinance-Feasible (infinite return)"* — precisely the optimistic-only signal AGENT-REV exists to catch. The shipped SYSTEM_REPORT even documents both numbers side-by-side without flagging the contradiction.
**Fix:** DSCR check must gate the refi-feasible branches.

### C7. Fabricated financial facts become priority-1 liens
`src/adapters/notice_ocr.py:111-140` + `src/adapters/dallas_foreclosure.py:342-368` — debt fallback takes the FIRST large dollar figure in a notice as `estimated_default_balance` (could be taxes/costs/another paragraph); enrichment converts it into a **priority-1 mortgage lien** feeding equity/LTV/"TOP 10 EQUITY PROSPECTS". Sale date falls back to a **fabricated** "First Tuesday of {month} 2026"; sale time invented as "10:00 AM…" — despite the module docstring claiming "never fabricates". Lender names like "Rocket Mortgage Bank, N.A." (doesn't exist) constructed by substring match.
**Fix:** return None + status="unparsed"; keep candidates in `all_extracted_balances` only; never synthesize dates/times/entity names.

---

## PART 3 — HIGH SEVERITY

### Financial logic
- **H1.** `underwriting.py:198-214` — `route_underwrite` silently defaults purchase price **to MAO** (self-confirming underwrite; mirrored live in pipeline.py) and stamps every result `DT-AUCTION` regardless of deal type.
- **H2.** `valuation.py:64-71,147` — comp recency scoring is fake (any non-empty `sale_date` string earns full points, never parsed); `MAX_COMP_AGE_DAYS`/distance filters defined but **never applied** — stale/far comps flow into the ARV median; GATE-VAL can return "pass" with **no ARV attached at all** (all-None prices).
- **H3.** Two disagreeing offer-ladder tables: `pricing.py:50-55` vs dead `negotiation.py:18-80`; walk-away = **105% of MAO** — the system will pay above its own stated maximum.
- **H4.** `compliance.py:188-197` — `run_all_gates(**kwargs)` fans shared kwargs to every gate → **TypeError crash** on legitimate calls (runtime-verified).
- **H5.** `compliance.py:79-96` — Dallas eligibility checks the wrong requirement (certified funds vs required RealAuction registration); Grayson has no branch → auto-passes a legal requirement documented as "TBD".
- **H6.** `reviewer.py:99-110` + `enrichment.py:139` — stale-notice parser only accepts ISO dates; real enriched formats ("7/25/2013", "Tuesday, the 3rd day of March, 2026") never match → check always reports "current". Collin's *owner-acquisition* deed date is stored as `sale_date` (opposite meaning vs Dallas) → corrupts dedupe key and timelines.
- **H7.** `reviewer.py:148-155` — identity check compares ProvenanceField object ≠ string → always True → false-PASS verified.
- **H8.** Workbook uses ungated `arv` instead of gated `arv_used` in 7 formula sites (Screener MAO, Flip B51, Auction Bid, kill-switch) → MAO computed from a value GATE-VAL never approved.
- **H9.** `build_workbook.py:731` — break-even ARV algebra wrong (`uses + p*s/(1-s)`; correct is `uses/(1-s)` = B50 next line); `:1163-1171` "optimistic-only" flag inspects the −10% row → permanent false alarm.

### Security & scraping integrity
- **S1.** `grayson_tax.py:42-44` — Chromium launched with **`--ignore-certificate-errors`** session-wide → MITM can inject/alter sale records.
- **S2.** `dallas_foreclosure.py:125-128` — **path traversal**: percent-decoded portal filename joined unsanitized into local write path (`%2e%2e%2f` → `../`; leading `/` discards the dir) → arbitrary file write driven by external website content.
- **S3.** `collin_clerk.py:101-127` — clerk results are paginated but **no pagination handling**; fixed 8s sleep parses page 1 only → silently incomplete lien stacks feeding GATE-LEGAL-LIEN (missing lien reads as clean title).
- **S4.** `collin_clerk.py:182,193` — lien priority sorted by **lexicographic MM/DD/YYYY strings** ("9/01/2019" > "10/15/2020"); release matching compares dates the same way → wrong priority order entering a legal gate.
- **S5.** `grayson_tax.py:49` — references `TAX_SALE_HTTP_URL`, which is **defined nowhere** → NameError on every call, swallowed by broad except → the primary source can never work and the bug masquerades as "portal down" for months.
- **S6.** `dallas_cad.py:73-81` + bulk — first DCAD search result followed with **zero parcel verification**, written into CPRs with hardcoded 0.90 confidence → wrong-property attributes presented as high-confidence facts.
- **S7.** Swallowed lookup failures make the legal gate vacuous: pipeline's five broad `except Exception → print("skipped")` blocks mean a FAILED lien search is indistinguishable from CLEAN title; GATE-LEGAL-LIEN passes on an empty stack. Also `pipeline.py:146-149`: a gate FAIL prints "production would halt here" — then continues anyway.
- **S8.** `grayson_tax.py:97-135` — any table/link/div becomes a "property lead" (address = link text, empty parcel id) once pages render → fabricated inventory waiting to happen.

### Data integrity & robustness
- **D1.** Schema mismatch breaks the flagship enrichment loop: `_run_bulk_dcad` expects old nested `details`/`entry_id` schema, but `current_deals.json` is flat (`address`) → every record resolves "Unknown" and is filtered out → **`--bulk-dcad` silently no-ops** against the real dataset. Even if fixed, `find_deals.py:247` regenerates the dataset from scratch on every run, destroying any enrichment written back.
- **D2.** All JSON/XLSX output writes are non-atomic (no tmp+rename): crash mid-write corrupts `current_deals.json` (the 419KB core dataset), caches, or workbooks. `dcad_cache.json` loader has no try/except → one corrupt file bricks all future runs.
- **D3.** Missing data becomes sentinels that flow into decisions: 25+ `"Unknown"` strings, `_parse_value` → 0.0 for missing dollars (skews portfolio stats), `cad_tax_year` hard-pinned "2026" @ confidence 1.0 even when absent.
- **D4.** `cpr.set_field` accepts anything: typo'd field names create silent attributes, confidence never range-checked, direct attribute writes bypass the provenance ledger entirely; lien ledger receipts amounts only (priority/survival unreceipted); no correction/supersede path.
- **D5.** Dataset reality vs claims: **0/338 records have coordinates**, sqft only 5/338, census tract 30/338; three inconsistent artifact generations (`foreclosures.geojson`=1 feature, `live_…geojson`=33, dataset=338).

### Test integrity (false acceptance)
- **T1.** `test_recalc.py` prints an unconditional green "**PASS**" banner, has zero asserts for 4 of 7 checks, discards the S&U balance result, and always exits 0 → CI-blind.
- **T2.** `verify_workbook.py` asserts nothing at all — name says verify, verifies nothing; copy-paste overlap with test_recalc.
- **T3.** `tests/` directory is **empty**; the "3 test suites" are ad-hoc scripts; bare `pytest` executes live scrapes + module-level code.

---

## PART 4 — MEDIUM (selected)

| # | Location | Issue |
|---|---|---|
| M1 | `pipeline.py:34` | Self-checks via `os.system` with output → `/dev/null`: failures give zero diagnostics; no timeout; 21 sequential interpreter spawns |
| M2 | `census.py` retries | Retries 4xx/permanent errors 3× (no jitter), `except: pass` swallows causes, sleeps after final attempt |
| M3 | `collin_foreclosure.py:43,70` | `os.system("playwright install")` mid-library-code; `browser.close()` not in finally → Chromium leaks on timeout |
| M4 | Adapters | Per-address fresh Playwright browser for DCAD single lookups (multi-second startup × hundreds); base64 notice JPEGs held in memory unbounded |
| M5 | OCR/PDF | No memoization by image hash; Dallas PDF text extracted twice per notice; no OCR timeout (pathological scan hangs run) |
| M6 | `pricing.py:36-37` | Bid ceiling mixes foreclosure/tax-sale survival flags regardless of channel |
| M7 | `pricing.py:95-103` | `optimistic_only` returns True for deals losing money in EVERY scenario (mislabels hopeless as optimistic-risk); empty grid vacuously True |
| M8 | `synthesis.py:93` | Reads nonexistent `cpr.jurisdiction_info` attribute → ISD always Unknown for real CPR objects |
| M9 | `find_deals.py:216-225` | TypeError crash formatting LTV when debt present but CAD absent |
| M10 | `find_deals.py:29` | Hardcoded absolute path OUTSIDE the repo (`/Users/knaidu/Documents/antigravity/lucid-noether/...`), unchecked |
| M11 | `build_workbook.py` 20+ sites | `CellIsRule(operator="containsText")` is not a valid Excel pattern → **gate PASS/FAIL color coding likely never renders** |
| M12 | `build_workbook.py:1410` | Portfolio chart plots 8 values against 9 categories (titles_from_data consumes first row) |
| M13 | Dates as text | "2026-06-15" written as strings with date formats; unusable in date math |
| M14 | Route optimizer | Cumulative time double-counts inspection minutes; courthouse selection ignores Grayson |

---

## PART 5 — TECH DEBT & REPO HYGIENE

**Repo hygiene (fix first — everything else depends on it):**
- ❌ **Not a git repository** — zero version control on 7.9K LOC of financial software
- ❌ **No requirements.txt / lockfile** despite populated venv (Python **3.9.6 = past EOL**)
- ❌ No `.gitignore` (data/ holds real owner PII — must be excluded before any git init + push), no linting config, no CI, no pre-commit
- ⚠️ Empty `tests/` and `static/` directories; docs metrics drifted (LOC, record counts, named-range counts)

**Structural debt:**
- `build_workbook.py`: 1,445 lines of module-level imperative code — no functions per tab, no main guard (`import` rebuilds+saves), `ws` rebound ~15×, sheet renames break distant sections
- `pipeline.py`: ~240-line god function `run_fixture_pipeline`; fixture/live paths copy-paste owner-cleaning + clerk + DCAD blocks
- Policy constants scattered outside `legal_rules.py` (the designated home): 70%/65% factors inline, closing costs 8% vs workbook 7.5% (memo ≠ Excel for same deal!), earnest/option terms hardcoded, `EARNEST_MONEY_DAYS` defined but never used
- Adding a 9th deal type touches ≥7 files (several silently defaulting); adding a 4th county fans out across 5+
- Dead code cluster: `DEAL_TYPE_RISK`, `MIN_DAYS_TO_SALE`, 9 unused named ranges, dual rent inputs (`monthly_rent` dead vs `rent_estimate` live — desync risk), negotiation POSTURES table dead, 14 dead imports in find_deals alone
- ~68 `print()` calls in library code, no logging framework; naive deprecated `datetime.utcnow()` throughout; essentially no type hints

---

## PART 6 — PRIORITIZED IMPROVEMENT ROADMAP

### Wave 0 — Stop the bleeding (½ day)
1. `git init` + `.gitignore` (exclude `data/`, `out/`, `.venv/`, `.pycache`) + initial commit; export `requirements.txt` (`pip freeze`)
2. Make `--all-checks` checks-only (add `--live` gating); add `--checks-only` semantics
3. Fixture runs write to `data/fixtures/` namespace — never clobber prod paths
4. Atomic writes helper (`tmp + os.replace`) for all JSON/XLSX outputs
5. Fix test exit codes: asserts + `sys.exit` in test_recalc; delete or fold verify_workbook; `@pytest.mark.live` markers so plain pytest is safe

### Wave 1 — Correct money answers (2–3 days)
6. C1 homestead substring → token match (+ regression test with "Sachse")
7. C2 bid-ceiling clamp + MAO in min(); DEAD routing for ceiling ≤ 0
8. C3 PROCEED requires economics + all gates
9. C4/C5/C6 workbook formulas: cash-flow sign, zero-filtered median K5:K10, DSCR-before-verdict ordering
10. C7 fabrication purge: None + status instead of invented dates/times/lenders/first-dollar debt
11. H1 purchase-price default removal; thread deal_type through models
12. Unify ladder policy into one table consumed by pricing (walk-away ≤ MAO)

### Wave 2 — Integrity of the legal spine (2–3 days)
13. S2 path-traversal sanitize (basename post-unquote + realpath containment check)
14. S1 remove `--ignore-certificate-errors`
15. S3/S4/S5 clerk: pagination support, real date parsing (datetime.date), fix undefined constant, narrow excepts
16. S7 loud-failure semantics: `lien_lookup_status` on CPR; errored lookups force gate verdict "unknown/HITL", never vacuous pass; `--force` flag required to continue past gate failures
17. S6 DCAD echo-back-verify (owner/street/zip match) before writing fields; scale confidence by match quality
18. H4-H7 compliance/reviewer fixes: kwargs split, Grayson → HITL, ISO date normalization at ingestion, ProvenanceField-aware comparisons

### Wave 3 — Structure & efficiency (3–5 days)
19. Split `build_workbook.py` into per-tab builder functions + main(); shared constants module for cross-file policy (closing costs, factors, tiers)
20. Shared adapter session helper: timeouts, jittered backoff on retryable errors only, uniform throttle, honest UA, browser reuse per batch
21. Fix D1 schema mismatch (flat↔nested) + add write-back merge so bulk-DCAD survives regeneration; cache TTL + corruption tolerance
22. Consolidate dedupe key (parcel_id fallback → normalized address hash); fix dataset coordinate gap via census geocode backfill
23. Introduce `logging` (replace 68 prints), timezone-aware timestamps, type hints on public APIs

### Wave 4 — Verification you can trust (ongoing)
24. Real pytest suite: golden-file tests for enrichment→valuation→pricing on fixture CPR; property-based tests on pricing invariants (ceiling ≤ MAO, profit monotonicity)
25. Recalc harness that asserts headline values ±tolerance and named-range counts pre/post LibreOffice round-trip
26. Doc-generation: derive SYSTEM_REPORT metrics (LOC, counts) from the codebase instead of hand-maintaining

---

## Bottom line
The architecture is genuinely good — provenance-chained records, gate-first design, clean import DAG, honest HITL routing, and the GATE-VAL concept is the right answer to non-disclosure states. But the system currently **cannot be trusted for money decisions**: three separate paths produce confidently-wrong financial outputs (fake PROCEED, negative-purchase-profit, infinite-return-below-DSCR), the legal gate can pass vacuously, and the test suite is structurally incapable of failing. The fixes are small and surgical — Wave 0+1 is roughly a week and would move this from "impressive demo" to "defensible tool."

---

# REMEDIATION LOG — Aug 24, 2026
**Method:** persistent goal + 6-agent parallel fix swarm (disjoint file ownership, pre-seeded shared contracts) + adversarial integration reviewer. Baseline commit `58b8bcd` precedes all changes; every diff reviewable wave-by-wave.

## Fixed (verified by 43 unit tests + full gate run)
| Audit ID | Fix |
|---|---|
| C1 | Homestead token match — Sachse false-positive dead (`tests/test_compliance_gates.py`) |
| C2 | Bid ceiling clamped ≥0, MAO in min(), dead-deal routing (`tests/test_pricing_gates.py`) |
| C3 | PROCEED requires economics + reviewer + alive pricing; surviving tax lien ⇒ CONDITIONAL; errored lookup ⇒ DO NOT PROCEED (`tests/test_memo_recommendation.py`) |
| C4/C5/C6 | Workbook: BRRRR cash-flow sign; zero-filtered MEDIAN over K5:L10; DSCR-before-infinite-return verdict (`tests/test_workbook_formulas.py`) |
| C7-fabrication | OCR/PDF adapters emit None — no invented dates/times/lenders/first-dollar liens |
| H1/H3/F12 | purchase required (no self-confirming underwrite); walk-away ≤ MAO & ceiling; three-state pencil status |
| H2/F5/F6 | GATE-VAL filters stale/far comps pre-scoring & pre-ARV; real recency scoring; never passes without ARV |
| H4–H7, X2–X8 | gates kwargs split; Dallas registration/Grayson HITL; ISO-normalized date parsing; identity unwrap; synthesis ISD fix |
| S1–S7 security | TLS restored; path traversal blocked; clerk date-sort + pagination; undefined-constant NameError eliminated; DCAD echo-back verification |
| P1–P10, E-series, F-series, T/L/V | CLI opt-in live (zero-network default), gate halt semantics, fixture/prod namespace separation, atomic writes everywhere, honest asserting test suites |
| Battle-test findings | `--fixture` fully offline via seeded verified liens; prod files untouchable from tests; bare pytest safe |

## Deferred (tracked for Wave 3+)
- build_workbook monolith → per-tab builder functions; logging framework (68 prints); shared constants module (pipeline 8% vs workbook 7.5% closing-cost divergence still open)
- `_run_bulk_dcad` flat-vs-nested schema mismatch (feature still silently no-ops on current dataset); dataset coordinate backfill (0/338 geocoded)
- Sensitivity grid intentionally varies raw arv scenarios (documented agent risk)
- OCR timeout worker; binary-atomic PDF writes; requirements-lock refresh cadence

## Gate status at close
`make verify` = lint ✓ · 43 unit ✓ · 22 module self-checks ✓ · offline fixture ✓ · workbook build ✓ — then `make recalc`: LibreOffice round-trip + 8 recalc assertions ✓ (97 named ranges survive).

## Wave 3 completion (same day) — deferred items cleared
| Deferred item | Status |
|---|---|
| Shared constants module (`src/policy.py`) + cross-file divergence (8% vs 7.5% closing) | ✅ DONE — policy wired through pricing/underwriting/negotiation/valuation/pipeline/workbook seeds; parity guard added |
| build_workbook monolith → per-tab builders + import guard | ✅ DONE — byte-stable output verified via exhaustive openpyxl diff vs baseline |
| bulk-dcad flat-schema mismatch (silent no-op) | ✅ DONE — `enrich_from_flat_record` + merge-on-write + atomic write-back |
| Dataset coordinate backfill tooling | ✅ DONE — opt-in `--geocode-backfill [--dry-run]`, cap 25/run, 296 geocodable identified |
| Logging framework replacing library prints | ✅ DONE — zero prints above `__main__` guards in src/**; runners configure basicConfig |
| OCR timeout worker / binary-atomic PDF writes / lockfile cadence | ⏳ still open (small; next wave) |

**Gate status:** lint ✓ · 55 unit tests ✓ · self-checks ✓ · offline fixture ✓ · workbook ✓ · recalc+8 ✓
| OCR timeout worker / binary-atomic PDF writes | ✅ DONE — pytesseract timeout=30 + PIL bomb guard; write_bytes atomic binary writer for downloads |

---

# TIER A–D COMPLETION — Aug 25, 2026
## A. Code polish ✅
Timezone-aware provenance timestamps (6 sites); M6 deal-type-aware lien-survival selection in bid ceilings (+4 tests); repo-local git hooks (commit: ruff+no-utcnow, push: unit tests) via core.hooksPath; full public-API type hints across six core modules.

## B. Operational runs against live sources ✅ (honest outcomes)
- **bulk-dcad**: 291→130 after new invalid-query guard (161 legacy-scrape artifacts skipped: legal-prose fragments, out-of-state strings). **46 CPRs enriched**, ~91 fields written back atomically; sqft coverage 5→37 (survived regeneration via merge-on-write). 34 ambiguous first-result matches fail-closed as `mismatch` — correct wrong-property protection; cached for future disambiguation.
- **Live DCAD integration fixed during ops**: real detail pages use bare `Address:` label + nbsp + abbreviated street types; extractor adapted, live probe parsed $518k/3bd/2006.
- **geocode-backfill**: 38/338 coordinates recovered (from 0). Census rejects most ALL-CAPS legacy address strings; failed lookups lack negative-caching → follow-up logged.
- **live tier**: pytest -m live 3/3 vs real county portals post-rewrite.
- **fresh scrape**: find_deals regenerated dataset with merge-on-write holding all DCAD enrichment.

## C. Business-gap enablers ✅
- `python -m src.hitl` — MLS/appraiser comp intake CLI; GATE-VAL HITL route now executable end-to-end (fixture CPR verified PASS conf 0.92 → VALUED).
- `LEGAL_VERIFICATION_PACKAGE.md` — 25 constants w/ citations + attorney-notes column for TX counsel sign-off.
- Grayson manual-channel procedure documented in SOURCE_REGISTRY.md.

## D. Bigger builds ✅
- **Dashboard** per DASHBOARD_PLAN.md (now archived at `docs/archive/DASHBOARD_PLAN__2026-08-20.md`; the plan itself is delivered, not pending): FastAPI (`server.py`, /api/deals|geojson|stats|summary) + dark SPA UI (KPIs, filterable table, Leaflet map, stats panel); 9 smoke tests; boots offline.
- **Transaction lifecycle** (`src/transaction.py`, 27 tests): whitelisted state machine over post-offer columns, redemption calendar (180d/730d + IRS 120d), policy-driven escrow schedule.
- **ADR-0001**: PostgreSQL/Dagster deferral reaffirmed with explicit trigger thresholds + migration sketch.

## Remaining known-open (post A–D)
1. Address-quality remediation of legacy scrape artifacts (blocks ~54% DCAD echo matches & most geocodes) — normalize addresses upstream in find_deals ranking too
2. Negative-cache for failed geocodes so batches advance past permanently-failing heads
3. CI remote: workflow ready; needs a git remote + push (user action)
4. Manual disambiguation queue for the 34 mismatch-cached DCAD parcels

**Final gates:** lint ✓ · 98 tests ✓ · self-checks ✓ · fixture ✓ · workbook ✓ · recalc+8 ✓ · live 3/3 ✓

---

# DATA-QUALITY SWARM — Aug 25, 2026 (final four items)
| Item | Resolution |
|---|---|
| Legacy address quality | ✅ `src/addr.py` normalizer (validated against verbatim dataset garbage) + find_deals plausibility gate — 209/335 rows flagged, fake-equity junk excluded from rankings, dataset schema byte-preserved |
| Geocode negative-cache | ✅ attempts/TTL ledger (`data/geocode_failures.json`), `--retry-failed` override, normalized outbound queries; batches now advance past permanently failing heads |
| CI remote | ✅ Workflow validated step-by-step vs current gates; `make ci-local` parity target green end-to-end; `docs/CI_SETUP.md` has exact repo-create/remote/push/protection steps (push itself needs your remote); ruff pinned |
| 34 mismatch parcels | ✅ Queue exported (`data/dcad_disambiguation_queue.{json,md}`), purge utility verified exact, opt-in `--resolve <addr> [--choice N]` candidate-picker CLI; PLUS verifier rescue (canonical types, nbsp, directional prefixes) replaying prior mismatches as passes while wrong-number protection stays decisive |

**Live re-measure after all fixes:** purge + bounded rerun → +2 CPRs enriched (8 fields); remaining re-fetch failures are genuine wrong-first-result ambiguity → queued for `--resolve`. Cache lifetime: 71 attempted / 26 parsed.

**Gates:** lint ✓ · 171 tests ✓ · self-checks ✓ · fixture ✓ · workbook ✓ · recalc ✓ · live 3/3 ✓ · hooks active ✓

## CI LIVE + GEOCODE HARVEST — Aug 26, 2026
- **GitHub remote live**: `KNaiduDTX/hermes-reo` (private); 35 commits pushed; Actions running the full verify pipeline on every push.
- **CI caught real drift immediately**: fastapi/uvicorn missing from requirements (worked locally, broke remotely), plus two environment-portability assumptions in tests (hardcoded .venv path; data/-dependent assertions). All fixed; pipeline green: lint → 171 tests → self-checks → fixture → workbook build in ~25s.
- **Geocode harvest**: address normalization flipped Census success from ~4% to ~90%+ per batch → **115/338 records geocoded** (0 at session start); negative-cache ledger holds 9 repeat-offenders; batches now terminate when only stragglers remain.
- **Map artifact refreshed**: live_foreclosures.geojson 30 features, all with real coordinates.

---

# WAVE 1 EXECUTED — Aug 26, 2026 (18 of 43 opportunities)
F1 HITL loop closure (end-to-end verified: comps→GATE-VAL pass→$138K profit→$402K offer) · RL1-RL3 adapter fail-fast/quarantine/shape-markers · RL4 run summaries + EXIT_CODES · RL5 weekly live smoke job · RL9 DCAD yield probe · RL10 file locking · RL11 census cache unify · F5-lite batch_price+CSV · F6 complete underwriting router (all 11 deal-type keys) · F7 coordinate leak fixed (119 features) · F9 digest wired · F10 grayson dispatch · F11 market analytics endpoint · FN3 cad_2025 root cause + defenses · FN4-lite address columns · FN5 money-path contracts pinned (132 tests) · FN6 coverage ratchet (55% vs 36 floor) · FN4 columns.
**Suite: 487 tests green (was 171). Coverage 55%. CI ✓ on push.**
**Remaining:** Wave 2 (TD1-TD9 tech-debt cluster, RL6/7/8), Wave 3 (FN1 identity spine, FN2 history, F2 comps platform), Final (FN8/FN9/FN10/FN11, F2 detail, TD10).
