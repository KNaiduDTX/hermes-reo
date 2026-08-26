# Exit codes — HERMES-REO `pipeline.py`

Executable source of truth: `EXIT_CODES` / `exit_code_for()` in `pipeline.py`
(search for "Exit-code catalog"). This page is referenced from
`pipeline.py --help`; any code added there must be cataloged here in the same
change.

| Code | Name | Meaning | Raised by |
|-----:|------|---------|-----------|
| 0 | ok | Run completed — including honest degradations: fixture gate demo, zero leads above floor, Grayson known-dead endpoints without `--force-dead-errors` | every mode |
| 1 | usage / data-missing | Bad invocation, failed module self-checks (`--checks-only`), or an input/dataset file absent or unreadable (e.g. `--resume-valued` file missing or unparseable) | `--checks-only`, `--all-checks`, `--resume-valued` |
| 2 | gate-fail | GATE-LEGAL-LIEN verdict fail/hitl across a live batch (not lookup-caused); also `--resume-valued` given a CPR whose carried GATE-VAL verdict is not `pass` | `--live`, `--resume-valued` |
| 3 | lookup-error | Gate block caused by an ERRORED lien lookup (`CLERK-LOOKUP-FAILED`) — a failed search is indistinguishable from clean title, so strictly worse than 2 | `--live` |
| 4 | min-leads | `--live` scrape returned fewer leads than the `--min-leads` floor; loud `PORTAL-SHAPE-CHANGED` banner printed because a near-empty pull usually means the portal HTML changed, not an empty market | `--live --min-leads N` |

## Notes

- Suppression valves: `--allow-gate-fail-demo` turns a 2/3 into 0 loudly (demo
  only); `--force-dead-errors` turns Grayson's swallowed endpoint degradation
  into the real exception instead of a graceful zero-lead continuation.
- Regardless of exit code, every data mode (fixture / live / bulk-dcad /
  geocode / resume) finishes by writing `data/run_summary.json` via
  `RunSummary` in `pipeline.py` — `{mode, started_at, duration_s, counts per
  source, flags}` — so a nonzero exit always carries its counts.
- Exit code of a mode is also observable from the shell as usual:
  `python3 pipeline.py --live --county collin --min-leads 5 || echo rc=$?`.
