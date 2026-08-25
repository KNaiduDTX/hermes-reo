"""Atomic binary writer tests (Wave 3: audit P4/D2 follow-up)."""
import json
import os

import pytest

from src.io_utils import write_bytes


def test_write_bytes_roundtrip(tmp_path):
    p = tmp_path / "doc.pdf"
    write_bytes(str(p), b"%PDF-1.4 fake")
    assert p.read_bytes() == b"%PDF-1.4 fake"


def test_write_bytes_crash_preserves_original(tmp_path, monkeypatch):
    """Simulate a crash between temp-write and replace."""
    import src.io_utils as io

    p = tmp_path / "keep.pdf"
    write_bytes(str(p), b"ORIGINAL")

    def _boom(src, dst):
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(io.os, "replace", _boom)
    with pytest.raises(RuntimeError):
        write_bytes(str(p), b"TRUNCATED-DATA")

    assert p.read_bytes() == b"ORIGINAL"
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".atomic-")]
    assert not leftovers, f"temp files leaked: {leftovers}"


def test_notice_ocr_sets_bomb_guard_and_timeout():
    """Audit P4: explicit decompression cap + OCR timeout must be present."""
    import inspect

    import src.adapters.notice_ocr as ocr
    src_text = inspect.getsource(ocr)
    assert "MAX_IMAGE_PIXELS" in src_text
    assert "timeout=" in src_text
