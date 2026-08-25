"""Crash-safety regression tests for the atomic writers (audit D2)."""
import json
import os

from src.io_utils import write_json, write_text


def test_write_json_roundtrip(tmp_path):
    p = tmp_path / "x.json"
    write_json(str(p), {"ok": True})
    assert json.loads(p.read_text()) == {"ok": True}


def test_failed_write_preserves_original(tmp_path):
    p = tmp_path / "keep.json"
    write_json(str(p), {"v": 1})

    import src.io_utils as io

    def _boom(f):
        f.write('{"partial":')
        raise RuntimeError("simulated crash")

    try:
        io._atomic_replace(str(p), _boom)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected the simulated crash to raise")

    assert json.loads(p.read_text()) == {"v": 1}
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".atomic-")]
    assert not leftovers, f"temp files leaked: {leftovers}"


def test_write_text_creates_missing_dirs(tmp_path):
    p = tmp_path / "deep" / "nested" / "f.txt"
    write_text(str(p), "hello")
    assert p.read_text() == "hello"
