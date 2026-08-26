# Contributing to HERMES-REO

Short version: **`make setup` once, `make ci-local` before you push.** Hooks
catch most issues at commit time.

## Setup

```bash
make setup    # creates .venv, installs pinned requirements + ruff + Playwright chromium
```

## Gates

```bash
make verify      # lint + tests + module self-checks + offline fixture + workbook build
make ci-local    # identical gates to the CI job — run this before pushing
```

Rules of thumb:

- Behavior changes need tests in the same change.
- The coverage ratchet (`make coverage`) is **ONLY-INCREASES** — never lower the
  `--cov-fail-under` floor; raise it as tests land.
- `make recalc` needs local LibreOffice and is intentionally outside CI.

## Git hooks (repo-local)

Hooks live in `.githooks/`, wired via `git config core.hooksPath=.githooks` —
no global install needed:

- **pre-commit**: ruff correctness subset (`E9,F63,F7,F82,B006,B017`) plus a
  guard against naive `datetime.utcnow()` under `src/`. Fast; blocks the commit.
- **pre-push**: full default pytest set (same exclusions as CI).

If a hook rejects your commit, fix the issue — don't reach for `--no-verify`.
CI re-runs every gate on the remote anyway, so bypassing only delays failure.
See [`docs/CI_SETUP.md`](docs/CI_SETUP.md) §5 for hook-vs-CI details.

## PR flow

1. Branch from `main`; keep changes focused (one concern per PR).
2. `make ci-local` green locally.
3. Push and open a PR against `main`; CI runs job `verify` on it.
4. Merge after the `verify` check is green (branch protection enforces this).
5. Releases: annotated tags cut from `main` only after a green run — convention
   in [`docs/CI_SETUP.md`](docs/CI_SETUP.md) §6.
