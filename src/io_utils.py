# src/io_utils.py
"""Atomic file-write helpers.

Every JSON/JSON-geo/text artifact the pipeline produces must go through
these helpers: a crash mid-write previously truncated current_deals.json,
caches, and workbooks (AUDIT finding D2). Write to a temp file in the same
directory, fsync, then os.replace() for atomicity on POSIX and Windows.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


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


# Backwards-friendly aliases used by call sites.
atomic_write_json = write_json
atomic_write_text = write_text


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


if __name__ == "__main__":
    self_check()
    print("io_utils.py: atomic write self-check passed")
