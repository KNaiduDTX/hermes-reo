# HERMES-REO — developer workflow entry points.
# `make help` lists targets. CI (.github/workflows/ci.yml) runs the same gates.

PY       := .venv/bin/python
SOFFICE  := /Applications/LibreOffice.app/Contents/MacOS/soffice
XLSX     := out/HERMES-REO_Model_Suite.xlsx

.PHONY: help setup lock lint lint-full test test-live checks fixture build recalc verify all clean

help:
	@echo "make setup    - create venv + install pinned deps + playwright browser"
	@echo "make lock     - freeze full transitive pins to requirements-lock.txt"
	@echo "make lint     - fast ruff (correctness errors only)"
	@echo "make lint-full- full ruff with style rules"
	@echo "make test     - pytest, excluding live/recalc (no network needed)"
	@echo "make test-live- pytest live-portal tier (hits real county sites)"
	@echo "make checks   - run all module self-checks (offline, exits nonzero on fail)"
	@echo "make fixture  - offline end-to-end pipeline into data/fixtures/"
	@echo "make build    - rebuild the Excel workbook"
	@echo "make recalc   - LibreOffice headless recalc + recalculated verification"
	@echo "make verify   - lint + tests + checks + fixture + build (CI parity)"
	@echo "make all      - verify + recalc"

setup:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -r requirements.txt ruff
	.venv/bin/playwright install chromium

lock:
	.venv/bin/pip freeze > requirements-lock.txt

lint:
	.venv/bin/ruff check --select E9,F63,F7,F82,B006,B017 --src .

lint-full:
	.venv/bin/ruff check --src .

test:
	$(PY) -m pytest -m "not live and not recalc"

test-live:
	$(PY) -m pytest -m live

checks:
	$(PY) pipeline.py --checks-only

fixture:
	$(PY) pipeline.py --fixture

build:
	$(PY) build_workbook.py

recalc: build
	$(SOFFICE) --headless --calc --convert-to xlsx $(XLSX) --outdir out/recalc/
	$(PY) -m pytest -m recalc

verify: lint test checks fixture build
	@echo "== verify: ALL GATES GREEN =="

all: verify recalc

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__
