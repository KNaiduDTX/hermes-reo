# HERMES-REO

**Distressed real estate deal engine for Collin / Dallas / Grayson County, TX.**
HERMES-REO sources foreclosure and tax-deed notices from county portals, enriches
each notice into a Canonical Property Record with lien stacks and property
characteristics, gates deals against fake-ARV and missing-lien risks (with a
human-in-the-loop comp-intake step before any valuation passes), underwrites 8
deal types, prices offers with seller-specific playbooks, and outputs deal memos,
offer drafts, portfolio analytics, an Excel model suite, and a FastAPI dashboard.

## Quickstart

```bash
make setup                      # venv + pinned deps + Playwright chromium
make ci-local                   # exact CI-parity gates (lint → tests → checks → fixture → build)
make fixture                    # offline end-to-end pipeline run into data/fixtures/
make desk                      # single command: Diligence Desk at http://127.0.0.1:8000
                               #   (legacy portfolio SPA mounted at /lab/)
uvicorn server:app             # legacy portfolio SPA alone at http://127.0.0.1:8123
```

Daily operation (scraping counties, HITL comp intake, offer generation) is
documented in [`USER_GUIDE.md`](USER_GUIDE.md).

## Commands

All developer workflow goes through the Makefile (`make help`):

| Command | What it does |
|---------|--------------|
| `make setup` | Create venv + install pinned deps + Playwright browser |
| `make lock` | Freeze full transitive pins to `requirements-lock.txt` |
| `make lint` | Fast ruff (correctness errors only: E9,F63,F7,F82,B006,B017) |
| `make lint-full` | Full ruff with style rules |
| `make test` | pytest, excluding live/recalc tiers (no network needed) |
| `make test-live` | Live-portal tier (hits real county sites) |
| `make coverage` | pytest + coverage ratchet (CI parity; ONLY-INCREASES floor) |
| `make checks` | All module self-checks (offline, nonzero exit on fail) |
| `make fixture` | Offline end-to-end pipeline into `data/fixtures/` |
| `make build` | Rebuild the Excel workbook |
| `make recalc` | LibreOffice headless recalc + verification (local-only) |
| `make verify` | lint + tests + checks + fixture + build (CI parity) |
| `make ci-local` | Exact CI parity: the gates CI runs (recalc stays local) |
| `make all` | verify + recalc |
| `make clean` | Remove pytest/ruff caches |

## Documentation map

Freshness labels tell you how much to trust each document:

| Document | Status |
|----------|--------|
| [`USER_GUIDE.md`](USER_GUIDE.md) | **Current** — operational guide (start here) |
| [`OPPORTUNITY_MAP.md`](OPPORTUNITY_MAP.md) | **Current** — roadmap / what comes next |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | **Current** — setup, gates, hooks, PR flow |
| [`docs/EXIT_CODES.md`](docs/EXIT_CODES.md) | **Current** — pipeline exit-code reference |
| [`SOURCE_REGISTRY.md`](SOURCE_REGISTRY.md) | Reference — county endpoints, live-checked 2026-08-19 |
| [`AUDIT_REPORT.md`](AUDIT_REPORT.md) | Remediation history — audit of Aug 24, 2026, findings fixed |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | Historical — original build plan (delivered) |
| [`PRIOR_FINDINGS.md`](PRIOR_FINDINGS.md) | Historical — prior-project reuse analysis |
| [`LEGAL_RULES.md`](LEGAL_RULES.md) | **Attorney-pending** — engineering artifact, not legal advice |
| [`LEGAL_VERIFICATION_PACKAGE.md`](LEGAL_VERIFICATION_PACKAGE.md) | **Attorney-pending** — generated sign-off package (all rows unverified) |
| [`docs/CI_SETUP.md`](docs/CI_SETUP.md) | CI — remote repo, Actions, branch protection, tagging |
| [`docs/adr/`](docs/adr/) | Decisions — architecture decision records |
| [`docs/archive/`](docs/archive/) | Historical — dated snapshots, superseded (see banners) |

Anything in `docs/archive/` is preserved as-of-its-date history: read the status
banner at the top before citing it.

## Project layout

```
reorealestate/
├── src/               # domain library: identity, CPRs, gates, valuation,
│                      #   underwriting, pricing, negotiation, adapters, ...
├── tests/             # pytest suite (~685 tests)
├── data/              # gitignored — scraped public-record data + derived outputs
├── out/               # gitignored — Excel workbook + recalculated artifacts
├── docs/              # CI_SETUP, EXIT_CODES, adr/, archive/
├── scripts/           # utility generators
├── static/            # dashboard SPA (plain ES6, no build step)
├── pipeline.py        # main runner: scrape → enrich → gate → underwrite → memo
├── find_deals.py      # batch deal finder / ranker over the dataset
├── build_workbook.py  # Excel model-suite generator
├── server.py          # FastAPI dashboard (read APIs + draft-only actions)
└── Makefile           # all developer entry points (see Commands above)
```

## Data privacy

`data/` and `out/` are gitignored **because they contain public-record PII**
(owner names, addresses, lien instruments scraped from county portals) plus all
derived deal artifacts. Never commit, attach, or share their contents; code and
documentation only. The ignore rules in [`.gitignore`](.gitignore) enforce this —
do not override them.
