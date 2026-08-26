"""RL10 regression tests: inter-process file locking.

Covers src.io_utils.file_lock (fcntl.flock sidecar semantics: LOCK-BUSY on
timeout, release on exception, persistent zero-content "<path>.lock" sidecar)
and the dallas_cad_bulk load -> search -> save critical section that depends
on it. Two-process cases run real subprocesses against a temp cache path.
"""
import json
import os
import subprocess
import sys
import time

import pytest

import src.io_utils as io_utils
from src.io_utils import file_lock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# All locking behavior requires POSIX flock; skip cleanly elsewhere.
pytestmark = pytest.mark.skipif(
    io_utils.fcntl is None, reason="fcntl/flock unavailable on this platform"
)


def _subprocess_env():
    return {**os.environ, "TEST_REPO_ROOT": REPO_ROOT}


def _wait_for_marker(path, proc, limit_sec=15.0):
    """Poll until *path* exists or the child dies; True iff marker seen."""
    deadline = time.monotonic() + limit_sec
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.02)
    return False


# Child process that grabs the lock, signals via marker file, holds ~1.5s.
_CHILD_HOLD_AND_RELEASE = """
import os, sys, time
sys.path.insert(0, os.environ["TEST_REPO_ROOT"])
from src.io_utils import file_lock

target, marker = sys.argv[1], sys.argv[2]
with file_lock(target, timeout=10):
    open(marker, "w").write("held")
    print("HELD", flush=True)
    time.sleep(1.5)
print("RELEASED", flush=True)
"""

# Child process that just acquires and releases the lock successfully.
_CHILD_ACQUIRE = """
import os, sys
sys.path.insert(0, os.environ["TEST_REPO_ROOT"])
from src.io_utils import file_lock

with file_lock(sys.argv[1], timeout=15):
    print("ACQUIRED", flush=True)
"""


def test_two_process_contention_times_out_with_lock_busy(tmp_path):
    """A second PROCESS hitting a held lock gets TimeoutError('LOCK-BUSY ...')."""
    target = str(tmp_path / "shared.json")
    marker = str(tmp_path / "child_holds.marker")
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLD_AND_RELEASE, target, marker],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for_marker(marker, proc), "child never acquired the lock"
        t0 = time.monotonic()
        with pytest.raises(TimeoutError) as excinfo:
            with file_lock(target, timeout=0.5):
                pass  # pragma: no cover - must not be reached
        elapsed = time.monotonic() - t0
    finally:
        out, err = proc.communicate(timeout=30)

    assert proc.returncode == 0, err
    assert str(excinfo.value).startswith("LOCK-BUSY"), excinfo.value
    assert target + ".lock" in str(excinfo.value), "error should name the sidecar"
    assert elapsed >= 0.4, "timeout gave up before its own deadline"
    assert "HELD" in out and "RELEASED" in out
    # Once the holding process exits, the lock hands off cleanly.
    with file_lock(target, timeout=15):
        pass


def test_lock_released_on_exception_and_visible_to_other_process(tmp_path):
    """An exception inside the critical section releases the lock (finally)."""
    target = str(tmp_path / "rmw.json")
    sidecar = target + ".lock"
    with pytest.raises(RuntimeError, match="boom"):
        with file_lock(target, timeout=5):
            assert os.path.exists(sidecar)
            raise RuntimeError("boom mid-critical-section")
    # Same process sees the release immediately ...
    with file_lock(target, timeout=2):
        pass
    # ... and so does a brand-new process (the kernel dropped the flock).
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_ACQUIRE, target],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "ACQUIRED" in result.stdout


def test_nested_acquisition_same_process_times_out_fast(tmp_path):
    """flock is per open-file-description: nesting blocks on itself (docs)."""
    target = str(tmp_path / "nested.json")
    with file_lock(target):
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="LOCK-BUSY"):
            with file_lock(target, timeout=0):
                pass  # pragma: no cover - must not be reached
        assert time.monotonic() - t0 < 2.0


def test_sidecar_created_persists_after_release_and_makes_dirs(tmp_path):
    """Documented sidecar behavior: <path>.lock, zero-content, kept, dirs made."""
    target = tmp_path / "deep" / "dir" / "cache.json"
    sidecar = tmp_path / "deep" / "dir" / "cache.json.lock"
    assert not target.parent.exists()
    with file_lock(str(target)) as held:
        assert held == str(sidecar)
        assert sidecar.exists()
        assert sidecar.read_text() == "", "sidecar must stay zero-content"
    # The sidecar deliberately survives release (see file_lock docstring:
    # unlinking it would let two processes flock different inodes).
    assert sidecar.exists()
    assert target.parent.is_dir(), "missing parent directories are created"


# --- dallas_cad_bulk critical section ---------------------------------------


def test_dcad_batch_holds_cache_lock_across_search(tmp_path, monkeypatch):
    """The cache stays locked while a batch search is in flight."""
    import src.adapters.dallas_cad_bulk as bulk

    cache_file = tmp_path / "dcad_cache.json"
    monkeypatch.setattr(bulk, "CACHE_PATH", str(cache_file))
    monkeypatch.setattr(bulk, "RATE_LIMIT_SEC", 0.0)
    observed = {}

    async def fake_search(addr):
        # A second exclusive acquisition from this process must time out
        # while batch_cross_reference is inside its locked section.
        try:
            with file_lock(bulk.CACHE_PATH, timeout=0.25):
                observed["locked_during_search"] = False
        except TimeoutError as exc:
            observed["locked_during_search"] = True
            assert "LOCK-BUSY" in str(exc)
        return {"status": "parsed", "beds": "3"}

    monkeypatch.setattr(bulk, "_search_dcad_by_address", fake_search)
    address = "123 MAIN ST, DALLAS, TX 75201"
    result = bulk.batch_cross_reference([address], batch_limit=5)

    assert result[address]["status"] == "parsed"
    assert observed["locked_during_search"] is True, (
        "cache was unlocked during search: concurrent writers could drop entries"
    )
    saved = json.loads(cache_file.read_text())
    assert saved[address]["status"] == "parsed"
    assert saved[address]["retrieved_at"], "save inside the section must stamp entries"


def test_dcad_batch_raises_lock_busy_when_cache_locked_elsewhere(
    tmp_path, monkeypatch
):
    """Contention beyond the scaled timeout surfaces as LOCK-BUSY, loudly."""
    import src.adapters.dallas_cad_bulk as bulk

    monkeypatch.setattr(bulk, "CACHE_PATH", str(tmp_path / "dcad_cache.json"))
    monkeypatch.setattr(bulk, "_cache_lock_timeout", lambda limit: 0.3)
    with file_lock(bulk.CACHE_PATH, timeout=5):
        with pytest.raises(TimeoutError, match="LOCK-BUSY"):
            bulk.batch_cross_reference(["ANY ADDRESS"], batch_limit=1)
    # Lock released: a retry goes through (and finds no data offline-free
    # because the empty cache means nothing to search yet).
    assert bulk._load_cache() == {}


def test_cache_lock_timeout_scales_with_batch_size():
    """Wait grows with the batch so legitimate concurrent runs queue, not fail."""
    import src.adapters.dallas_cad_bulk as bulk

    assert bulk._cache_lock_timeout(1) == bulk.BULK_LOCK_TIMEOUT_FLOOR_SEC
    assert bulk._cache_lock_timeout(50) >= 2 * 50 * bulk.RATE_LIMIT_SEC
    assert bulk._cache_lock_timeout(50) > bulk._cache_lock_timeout(5)


def test_two_processes_do_not_drop_each_others_dcad_entries(tmp_path, monkeypatch):
    """End-to-end lost-update regression: concurrent batches merge, not clobber.

    Pre-RL10, the child's final save would overwrite the parent's entry
    wholesale (last writer wins on a stale in-memory copy).
    """
    import src.adapters.dallas_cad_bulk as bulk

    cache_file = tmp_path / "dcad_cache.json"
    monkeypatch.setattr(bulk, "CACHE_PATH", str(cache_file))
    parent_addr = "100 PARENT LANE, DALLAS, TX 75201"
    child_addr = "200 CHILD ROAD, PLANO, TX 75023"

    bulk._save_cache({parent_addr: {"status": "parsed", "beds": "2",
                                    "retrieved_at": bulk._utc_now_iso()}})

    child_src = """
import os, sys
sys.path.insert(0, os.environ["TEST_REPO_ROOT"])
import src.adapters.dallas_cad_bulk as bulk

async def fake_search(addr):
    return {"status": "parsed", "beds": "4"}

# The parent's monkeypatch does not cross process boundaries: aim the
# child at the same tmp cache explicitly (argv[1]) — never the repo's.
bulk.CACHE_PATH = os.path.abspath(sys.argv[1])
assert bulk.CACHE_PATH.endswith("dcad_cache.json")
bulk.RATE_LIMIT_SEC = 0.0
bulk._search_dcad_by_address = fake_search
open(sys.argv[3], "w").write("started")
bulk.batch_cross_reference([sys.argv[2]], batch_limit=1)
open(sys.argv[4], "w").write("done")
"""
    started = str(tmp_path / "child_started.marker")
    done = str(tmp_path / "child_done.marker")
    proc = subprocess.Popen(
        [sys.executable, "-c", child_src, str(cache_file), child_addr, started, done],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
    )
    try:
        assert _wait_for_marker(started, proc), "child never started"
        # Parent performs its own load -> mutate -> save cycle. Whether the
        # child waits on the sidecar lock or runs first, every writer reloads
        # fresh state under the lock, so both entries must survive.
        with file_lock(bulk.CACHE_PATH, timeout=20):
            cache = bulk._load_cache()
            cache[parent_addr] = {"status": "parsed", "beds": "9",
                                  "retrieved_at": bulk._utc_now_iso()}
            bulk._save_cache(cache)
        assert _wait_for_marker(done, proc, limit_sec=30), "child never finished"
    finally:
        proc.communicate(timeout=60)
    assert proc.returncode == 0

    final = json.loads(cache_file.read_text())
    assert final[parent_addr]["beds"] == "9", final
    assert final[child_addr]["beds"] == "4", final
