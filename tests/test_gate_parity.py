"""Audit TEST-5: the coverage ratchet floor lived in three manually-synced
copies (ci.yml, Makefile, docs/CI_SETUP.md). A coverage-eating change could
pass make ci-local (bare pytest) and fail CI post-push. TEST-1 fixed the
parity leg; this meta-test pins the NUMBERS so they can never silently drift.
Also pins: the Makefile verify target really runs the coverage leg (CI parity
is enforced, not just claimed)."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _read(name):
    return (REPO / name).read_text(encoding="utf-8")


def test_ci_and_makefile_coverage_floors_are_identical():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    ci_floors = re.findall(r"--cov-fail-under=(\d+)", ci)
    mk_floors = re.findall(r"--cov-fail-under=(\d+)", mk)
    assert ci_floors, "ci.yml lost its coverage ratchet"
    assert mk_floors, "Makefile lost its coverage ratchet"
    assert len(set(ci_floors)) == 1, "multiple ratchet numbers in ci.yml"
    assert len(set(mk_floors)) == 1, "multiple ratchet numbers in Makefile"
    assert ci_floors[0] == mk_floors[0], (
        f"coverage floor drift: ci.yml={ci_floors[0]} Makefile={mk_floors[0]}")


def test_verify_target_actually_enforces_the_ratchet():
    """TEST-1 regression guard: verify must invoke the instrumented pytest
    (the coverage target), not bare pytest."""
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    verify = re.search(r"^verify:.*$", mk, re.M).group(0)
    assert "coverage" in verify, "verify no longer runs the coverage ratchet"
