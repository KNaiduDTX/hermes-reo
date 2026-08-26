# src/io_utils.py
"""Atomic file-write helpers.

Every JSON/JSON-geo/text artifact the pipeline produces must go through
these helpers: a crash mid-write previously truncated current_deals.json,
caches, and workbooks (AUDIT finding D2). Write to a temp file in the same
directory, fsync, then os.replace() for atomicity on POSIX and Windows.

file_lock() complements the writers with inter-process mutual exclusion
(OPPORTUNITY_MAP RL10): concurrent read-modify-write cycles on one artifact
(e.g. dcad_cache.json) previously dropped each other's updates.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Any

try:
    import fcntl  # POSIX; guarded so importing io_utils stays portable
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None


def _atomic_replace(target_path: str, write_fn) -> str:
    directory = os.path.dirname(os.path.abspath(target_path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp", prefix=".atomic-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target_path


def write_json(path: str, payload: Any, indent: int = 2, default=str) -> str:
    """Atomically write *payload* as JSON to *path*."""
    return _atomic_replace(
        path, lambda f: json.dump(payload, f, indent=indent, default=default)
    )


def write_text(path: str, text: str) -> str:
    """Atomically write *text* to *path*."""
    return _atomic_replace(path, lambda f: f.write(text))


def write_bytes(path: str, data: bytes) -> str:
    """Atomically write binary *data* (e.g. downloaded PDFs) to *path*."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp", prefix=".atomic-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


# Backwards-friendly aliases used by call sites.
atomic_write_json = write_json
atomic_write_text = write_text


@contextmanager
def file_lock(path: Any, timeout: float = 10.0):
    """Exclusive inter-process lock on ``<path>.lock`` via fcntl.flock (RL10).

    Serializes read-modify-write sections (e.g. the DCAD cache's
    load -> search -> save cycle) across processes so concurrent writers
    cannot silently drop each other's updates.

    Contract:
    - Sidecar: the lock lives in a zero-content ``<path> + ".lock"`` file in
      the same directory; missing parent directories are created on demand.
      The sidecar is deliberately NOT unlinked on release: deleting it between
      unlock and close would let two processes flock different inodes and both
      believe they won. A leftover ``.lock`` file is normal and harmless —
      lock state lives in the kernel, not in the file contents.
    - Timeout: acquisition polls ``flock(LOCK_EX | LOCK_NB)`` until *timeout*
      seconds elapse, then raises ``TimeoutError("LOCK-BUSY <sidecar>: ...")``.
      Callers should treat LOCK-BUSY as "another writer holds this resource;
      retry later" — never as data corruption.
    - Release happens on normal exit AND on exception (finally below); the
      kernel additionally releases the lock if the process dies outright.
    - Not re-entrant: nesting file_lock() on the same path within one process
      blocks on itself (flock is per open-file-description). Do not nest.
    - Requires POSIX ``fcntl``; where it is unavailable this raises RuntimeError
      when called (importing io_utils itself stays portable).
    """
    if fcntl is None:  # pragma: no cover - platform-dependent
        raise RuntimeError(
            "file_lock requires fcntl (POSIX); this platform has no flock support"
        )
    target = os.path.abspath(os.fspath(path))
    lock_path = target + ".lock"
    directory = os.path.dirname(lock_path)
    os.makedirs(directory, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + max(0.0, float(timeout))
    delay = 0.01
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"LOCK-BUSY {lock_path}: exclusive lock not acquired "
                        f"within {float(timeout):g}s; another writer holds it"
                    )
                time.sleep(min(delay, remaining))
                delay = min(delay * 2.0, 0.2)
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def self_check() -> None:
    """Self-check: crash-safety semantics of the atomic writers."""
    import tempfile as _tf

    d = _tf.mkdtemp()
    p = os.path.join(d, "x.json")
    write_json(p, {"ok": True})
    with open(p) as f:
        assert json.load(f) == {"ok": True}
    # A failing writer must leave the previous good content intact.
    try:
        def _boom(f):
            f.write('{"partial":')
            raise RuntimeError("simulated crash")
        _atomic_replace(p, _boom)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    leftovers = [n for n in os.listdir(d) if n.startswith(".atomic-")]
    assert not leftovers, f"temp files leaked: {leftovers}"
    with open(p) as f:
        assert json.load(f) == {"ok": True}, "original file corrupted by failed write"
    write_text(os.path.join(d, "t.txt"), "hello")
    assert open(os.path.join(d, "t.txt")).read() == "hello"

    # RL10: file_lock single-process semantics (cross-process contention is
    # covered by tests/test_file_locking.py).
    lock_target = os.path.join(d, "selfcheck.json")
    with file_lock(lock_target, timeout=5.0) as held:
        assert held.endswith(".lock"), held
        assert os.path.exists(held), "sidecar must exist while the lock is held"
        try:
            with file_lock(lock_target, timeout=0.2):
                raise AssertionError("second exclusive acquisition should block")
        except TimeoutError as exc:
            assert str(exc).startswith("LOCK-BUSY"), exc
        else:
            raise AssertionError("expected LOCK-BUSY TimeoutError")
    # Released on exception ...
    try:
        with file_lock(lock_target, timeout=5.0):
            raise ValueError("simulated failure inside the critical section")
    except ValueError:
        pass
    # ... provably released: an immediate re-acquisition succeeds.
    with file_lock(lock_target, timeout=5.0):
        pass
    # Sidecar is deliberately left in place after release (see file_lock docs).
    assert os.path.exists(lock_target + ".lock")


if __name__ == "__main__":
    self_check()
    print("io_utils.py: atomic write + file_lock self-check passed")
