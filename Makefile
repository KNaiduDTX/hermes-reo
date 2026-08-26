# HERMES-REO — developer workflow entry points.
# `make help` lists targets. `make ci-local` mirrors the GitHub Actions job (.github/workflows/ci.yml).

PY       := .venv/bin/python
RUFF     := .venv/bin/ruff
SOFFICE  := /Applications/LibreOffice.app/Contents/MacOS/soffice
XLSX     := out/HERMES-REO_Model_Suite.xlsx

.PHONY: help setup lock lint lint-full test test-live coverage checks fixture build recalc verify ci-local all clean

help:
	@echo "make setup    - create venv + install pinned deps + playwright browser"
	@echo "make lock     - freeze full transitive pins to requirements-lock.txt"
	@echo "make lint     - fast ruff (correctness errors only)"
	@echo "make lint-full- full ruff with style rules"
	@echo "make test     - pytest, excluding live/recalc (no network needed)"
	@echo "make test-live- pytest live-portal tier (hits real county sites)"
	@echo "make coverage - pytest + coverage ratchet (CI parity; ONLY-INCREASES)"
	@echo "make checks   - run all module self-checks (offline, exits nonzero on fail)"
	@echo "make fixture  - offline end-to-end pipeline into data/fixtures/"
	@echo "make build    - rebuild the Excel workbook"
	@echo "make recalc   - LibreOffice headless recalc + recalculated verification"
	@echo "make verify   - lint + tests + checks + fixture + build (CI parity)"
	@echo "make ci-local - exact CI parity: the gates CI runs (verify; recalc stays local)"
	@echo "make all      - verify + recalc"

setup:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -r requirements.txt ruff
	.venv/bin/playwright install chromium

lock:
	.venv/bin/pip freeze > requirements-lock.txt

lint:
	$(RUFF) check --select E9,F63,F7,F82,B006,B017 .

lint-full:
	$(RUFF) check .

test:
	$(PY) -m pytest

# Live tier module sits at repo root while pytest.ini sets `testpaths = tests`,
# so bare `pytest -m live` would collect nothing (exit 5) — the module is named
# explicitly. CLI `-m live` overrides the default addopts filter for this run.
test-live:
	$(PY) -m pytest test_live_portals.py -m live

# FN6 ratchet mirror of ci.yml job `verify`: same flags, same number.
# ONLY-INCREASES: never lower --cov-fail-under; raise it as coverage grows.
coverage:
	$(PY) -m pytest --cov=src --cov=server --cov-report=term-missing --cov-fail-under=36

checks:
	$(PY) pipeline.py --checks-only

fixture:
	$(PY) pipeline.py --fixture

build:
	$(PY) build_workbook.py

recalc: build
	$(SOFFICE) --headless -env:UserInstallation=file:///tmp/lo_hermes --calc --convert-to xlsx $(XLSX) --outdir out/recalc/
	$(PY) -m pytest test_recalc.py -m recalc

verify: lint test checks fixture build
	@echo "== verify: ALL GATES GREEN =="

# Exact parity with .github/workflows/ci.yml job `verify`: same gates (lint,
# pytest default set excluding live/recalc, checks, fixture, build), reusing the
# `verify` target as the single source of truth so CI and local can never drift.
# Recalc is intentionally excluded (LibreOffice is local-only): run `make recalc`.
ci-local: verify
	@echo "== ci-local: matches CI job 'verify' — ALL GATES GREEN =="

all: verify recalc

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__
