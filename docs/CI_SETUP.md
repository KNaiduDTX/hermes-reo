# CI Setup — Remote Repository

HERMES-REO's gates already run locally via `make ci-local` (exact parity with the
GitHub Actions workflow `.github/workflows/ci.yml`, job `verify`: lint → pytest →
module self-checks → offline fixture pipeline → workbook build). This doc covers
wiring the repo to a GitHub remote so Actions runs them on every push/PR.

## 1. Create the GitHub repository

Option A — `gh` CLI (creates private repo, adds remote, pushes in one step):

```bash
gh auth status                      # confirm you are logged in
gh repo create hermes-reo --private --source . --remote origin --push
```

Option B — web UI:

1. Create an empty private repo at https://github.com/new (no README/.gitignore/license — the repo already has history).
2. Add the remote:

```bash
git remote add origin git@github.com:<owner>/hermes-reo.git   # or https://github.com/<owner>/hermes-reo.git
git remote -v                                                 # verify origin is set
```

## 2. Push and verify

```bash
git push -u origin main
```

## 3. Enable Actions

Actions are enabled by default when `.github/workflows/ci.yml` lands on the
default branch (`main`). Confirm at the repo's **Actions** tab: the `ci`
workflow should show a green `verify` job for your first push. If disabled:
**Settings → Actions → General → Allow all actions**.

The job pins Python 3.11 and installs `requirements.txt` + `ruff`, mirroring
`make setup`.

## 4. Branch protection (recommended)

After the first CI run registers its check name:

1. **Settings → Branches → Add branch ruleset** (or classic branch protection rule) for `main`.
2. Enable **Require a pull request before merging**.
3. Enable **Require status checks to pass** and select **`verify`** (shown as `ci / verify`).

CLI alternative:

```bash
gh api repos/<owner>/hermes-reo/branches/main/protection -X PUT \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks.strict=true \
  -f "required_status_checks.contexts[]=verify" \
  -F enforce_admins=true -F required_pull_request_reviews=null \
  -F restrictions=null
```

## 5. Local hooks vs CI

Repo-local git hooks are already wired via `core.hooksPath=.githooks` and enforce
gates before code ever reaches the remote:

- **pre-commit**: ruff correctness (`E9,F63,F7,F82,B006,B017`) + no naive `datetime.utcnow()` under `src/`.
- **pre-push**: full default pytest set (`pytest -q`, same exclusions as CI).

So the normal loop needs no manual gate-running: hooks catch issues at commit/push,
and CI confirms on the remote. Run `make ci-local` explicitly before pushing large
changes or when bypassing hooks (`--no-verify`).

## 6. Tagging convention

Tags are **annotated** and cut only from `main` after a green `make ci-local`
(or a green CI run), never from dirty trees:

```bash
# format: vMAJOR.MINOR.PATCH[-qualifier]
git tag -a v0.4.0 -m "Release: valuation + underwriting tier"
git tag -a v0.3.0-data-quality -m "Data-quality milestone: address normalization + DCAD echo gate"
git push origin v0.3.0-data-quality          # tags do not ride `git push` by default
```

Conventions:

- `vX.Y.Z` — clean releases.
- `vX.Y.Z-<qualifier>` — milestone snapshots (e.g. `-data-quality`, `-live-portals`).
- Never re-tag or force-move a pushed tag; delete and bump instead.

> Note: no tag has been created yet as part of this change — `v0.3.0-data-quality`
> is reserved as the next candidate once the current work is committed and green.

## 7. Coverage ratchet (FN6)

The `verify` job's pytest step now runs instrumented and enforces a floor:

```
pytest --cov=src --cov=server --cov-report=term-missing --cov-fail-under=36
```

Rules of the ratchet:

- **ONLY-INCREASES.** `--cov-fail-under` starts one point above the measured
  baseline (~35% across `src/` + `server.py`). Never lower it; raise it as tests
  land. The number lives in exactly two places that must stay in sync:
  `.github/workflows/ci.yml` (job `verify`) and the Makefile `coverage` target.
- Local mirror: `make coverage` (same flags, same number).
- Tooling pin: `pytest-cov==7.1.0` in `requirements.txt` (matches the installed
  workspace version) so CI and local measure identically. Bump the pin and the
  gate in the same change only if the measurement is re-baselined first.
- Comparison semantics: coverage.py compares against the total rounded per the
  default precision (whole numbers), so a true 35.5–36.49% reads as "36" and
  passes; anything under 35.5% fails the job.

After adding tests, check whether the floor can rise:

```bash
make coverage        # read the TOTAL line, then raise both fail-under numbers
```

Branch protection from §4 stays as-is: required check is `verify` only. The
ratchet rides inside `verify`, so no extra check name is needed.

## 8. Weekly live-portal smoke (RL5)

Job `weekly-live` in `.github/workflows/ci.yml` runs the real-county-site tier
(`test_live_portals.py`: Collin Clerk search + lien stack, DCAD property parse,
Grayson registry consistency) so portal markup breakage is caught by CI instead
of human memory.

- **Triggers:** weekly cron (`0 13 * * 1`, Mondays ~8am Dallas time) plus manual
  **Run workflow** (`workflow_dispatch`) for on-demand checks. It also runs on
  push/schedule like any job, but its `if:` gates execution to those two events,
  so ordinary PRs and pushes are unaffected and merges never depend on county
  sites being up.
- **Environment:** installs `requirements.txt` plus Playwright's browser via
  `playwright install chromium --with-deps`.
- **Retargeting without code edits:** set repo Variables (**Settings → Secrets
  and variables → Actions → Variables**) `HERMES_TEST_PARTY` /
  `HERMES_TEST_ADDRESS`; they pass through to the job, which falls back to the
  same defaults as `test_live_portals.py` when unset.
- **Local equivalent:** `make test-live`. Note it names the module explicitly
  (`pytest test_live_portals.py -m live`): pyproject.toml [tool.pytest.ini_options] sets testpaths=tests
  while the live tier lives at repo root, so bare `pytest -m live` collects
  nothing and exits 5. The same explicit invocation is used in CI.
- **On red:** a failed `weekly-live` run means an adapter or portal surface
  drifted (HTTP shape, selectors, endpoints). Check `SOURCE_REGISTRY.md` first,
  then the adapter named in the failing test. It does not block merges, but fix
  before the next release cut (§6).
