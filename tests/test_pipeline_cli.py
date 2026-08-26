"""CLI-contract smoke tests for pipeline.py (audit P1/P4 + opt-in live).

These run the real CLI as a subprocess. They must stay fast and fully
offline: --checks-only and bare invocation are network-free by contract.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable  # environment-portable: works in CI and local venv
PIPELINE = os.path.join(REPO, "pipeline.py")


def _run(*args, timeout=300):
    return subprocess.run([PY, PIPELINE, *args], capture_output=True,
                          text=True, timeout=timeout, cwd=REPO)


def test_checks_only_is_network_free_and_green():
    r = _run("--checks-only")
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "All checks passed" in r.stdout


def test_bare_invocation_hints_and_exits_zero():
    """AUDIT battle-test: bare runs used to fire live scrapes at counties."""
    r = _run()
    assert r.returncode == 0
    assert "--live" in r.stdout or "--fixture" in r.stdout


def test_all_checks_without_mode_does_not_scrape():
    """AUDIT battle-test: --all-checks used to fall through into a live run."""
    r = _run("--all-checks")
    assert r.returncode == 0
    assert "LIVE RUN" not in r.stdout
