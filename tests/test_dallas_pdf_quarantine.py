"""RL2/RL3: Dallas foreclosure portal shape marker + PDF quarantine tests.

Offline only: no portal is contacted (urllib.request.urlopen and time.sleep
are scripted), no real county PDFs are needed — corrupt/truncated fixtures
are synthetic bytes.

Covers:
  RL2  parse_portal_links raises DallasPortalShapeError when #temp-region-7
       or the blockquote[id^=display] accordions are missing; an intact
       structure with zero .pdf links is still a legitimate empty month;
       export_portal_health reports counters without raising.
  RL3  cached-PDF validation (size > 1KB AND %PDF header), quarantine of
       poisoned cache entries to data/quarantine-style dirs, re-download,
       and per-PDF failure tallying into {"pdf_failures": [...]}.
"""
from __future__ import annotations

import os
import urllib.request

import pytest

from src.adapters import dallas_foreclosure as df


# --------------------------------------------------------------- fixtures --

VALID_PDF_BYTES = b"%PDF-1.4\n" + b"\x00" * 4096          # header + bulk
JUNK_PDF_BYTES = b"<html>error page saved as pdf</html>" * 64   # >1KB, wrong header
TRUNCATED_PDF_BYTES = b"%PDF-1.4\ntrunc"                  # right header, <=1KB

PORTAL_INTACT = (
    b"<html><body><strong>September:</strong><div id='temp-region-7'>"
    b"<blockquote id='display1'><a href='/f/Dallas_x.pdf'>Dallas_x.pdf</a>"
    b"</blockquote>"
    b"<blockquote id='display2'><a href='/f/Garland_x.pdf'>Garland_x.pdf</a>"
    b"</blockquote></div></body></html>"
)
PORTAL_NO_REGION = b"<html><body><p>redesigned</p></body></html>"
PORTAL_NO_ACCORDIONS = (
    b"<html><body><div id='temp-region-7'><p>moved</p></div></body></html>")
PORTAL_INTACT_EMPTY = (
    b"<html><body><div id='temp-region-7'>"
    b"<blockquote id='display1'><p>No postings</p></blockquote>"
    b"</div></body></html>")


def _item(city="Dallas", filename=None):
    filename = filename or f"{city}_x.pdf"
    return {"url": f"{df.BASE_URL}/f/{filename}", "filename": filename,
            "city": city, "month": "September"}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _no_network_no_waiting(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=15: (_ for _ in ()).throw(
                            AssertionError("unexpected network access")))
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)


# ------------------------------------------------------------- RL2: shape --

def test_missing_temp_region_raises_shape_error():
    with pytest.raises(df.DallasPortalShapeError):
        df.parse_portal_links(PORTAL_NO_REGION)


def test_zero_display_accordions_raise_shape_error():
    with pytest.raises(df.DallasPortalShapeError):
        df.parse_portal_links(PORTAL_NO_ACCORDIONS)


def test_intact_but_empty_month_is_not_a_shape_change():
    assert df.parse_portal_links(PORTAL_INTACT_EMPTY) == []


def test_intact_portal_links_parse():
    links = df.parse_portal_links(PORTAL_INTACT)
    assert [l["city"] for l in links] == ["Dallas", "Garland"]
    assert all(l["url"].startswith(df.BASE_URL) for l in links)


def test_adapter_fetch_raises_through_on_shape_change(monkeypatch, tmp_path):
    monkeypatch.setattr(df, "fetch_portal_html", lambda: PORTAL_NO_REGION)
    adapter = df.DallasForeclosureAdapter()
    with pytest.raises(df.DallasPortalShapeError):
        adapter.fetch(temp_dir=str(tmp_path / "pdfs"))


def test_export_portal_health_zeroes_on_shape_change(monkeypatch, tmp_path):
    monkeypatch.setattr(df, "fetch_portal_html", lambda: PORTAL_NO_REGION)
    health = df.export_portal_health(temp_dir=str(tmp_path))
    assert health == {"links_found": 0, "pdfs_downloaded": 0, "parsed_ok": 0}


def test_export_portal_health_counts_without_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(df, "fetch_portal_html", lambda: PORTAL_INTACT)

    def fake_download(url, local_dir, **kw):
        path = os.path.join(local_dir, url.rsplit("/", 1)[-1])
        from src.io_utils import write_bytes
        return write_bytes(path, VALID_PDF_BYTES)

    class _FakePage:
        def extract_text(self):
            return "NOTICE OF SALE\nnothing usable"

    monkeypatch.setattr(df, "download_pdf", fake_download)
    # Garland's PDF is corrupt for parsing; Dallas parses fine.
    monkeypatch.setattr(
        df, "split_pdf_pages",
        lambda p: [[_FakePage()]] if p.endswith("Dallas_x.pdf")
        else (_ for _ in ()).throw(ValueError("corrupt")))

    health = df.export_portal_health(limit=2, temp_dir=str(tmp_path))
    assert health == {"links_found": 2, "pdfs_downloaded": 2, "parsed_ok": 1}


def test_export_portal_health_zeroes_on_network_outage(monkeypatch, tmp_path):
    def boom():
        raise OSError("connection refused")

    monkeypatch.setattr(df, "fetch_portal_html", boom)
    assert df.export_portal_health(temp_dir=str(tmp_path))["links_found"] == 0


# ------------------------------------------- RL3: cached-PDF validation ----

def test_is_valid_pdf_matrix(tmp_path):
    good = tmp_path / "good.pdf"
    good.write_bytes(VALID_PDF_BYTES)
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(JUNK_PDF_BYTES)
    tiny = tmp_path / "tiny.pdf"
    tiny.write_bytes(TRUNCATED_PDF_BYTES)
    missing = tmp_path / "missing.pdf"

    assert df.is_valid_pdf(str(good)) is True
    assert df.is_valid_pdf(str(junk)) is False      # wrong magic
    assert df.is_valid_pdf(str(tiny)) is False      # <= 1KB
    assert df.is_valid_pdf(str(missing)) is False   # absent -> never raise


def test_quarantine_moves_corrupt_bytes_and_suffixes_collisions(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    qdir = tmp_path / "quarantine"
    first = cache / "poisoned.pdf"
    first.write_bytes(JUNK_PDF_BYTES)

    dest1 = df.quarantine_pdf(str(first), str(qdir))
    assert not first.exists(), "corrupt bytes must leave the cache"
    moved1 = qdir / "poisoned.pdf"
    assert os.path.abspath(dest1) == os.path.abspath(str(moved1))
    assert moved1.read_bytes() == JUNK_PDF_BYTES, "bytes preserved for forensics"

    # A second poison event keeps history instead of overwriting.
    first.write_bytes(TRUNCATED_PDF_BYTES)
    dest2 = df.quarantine_pdf(str(first), str(qdir))
    assert dest2.endswith(".quarantined-1")
    assert os.path.exists(dest2)


def test_download_pdf_revalidates_and_heals_poisoned_cache(
        monkeypatch, tmp_path):
    cache = str(tmp_path)
    qdir = str(tmp_path / "quarantine")
    poisoned = os.path.join(cache, "bundle.pdf")
    with open(poisoned, "wb") as f:
        f.write(JUNK_PDF_BYTES)

    served: list[str] = []

    def fake_urlopen(req, timeout=15):
        served.append(req.full_url)
        return _FakeResp(VALID_PDF_BYTES)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    qlog: list[dict] = []
    healed = df.download_pdf(f"{df.BASE_URL}/f/bundle.pdf", cache,
                             quarantine_dir=qdir, quarantine_log=qlog)

    assert served == [f"{df.BASE_URL}/f/bundle.pdf"], "poison forces re-download"
    assert healed == poisoned
    assert df.is_valid_pdf(healed), "healed cache entry must be a valid PDF"
    assert len(qlog) == 1
    assert qlog[0]["path"] == poisoned
    assert os.path.dirname(os.path.abspath(qlog[0]["quarantined_to"])) == \
        os.path.abspath(qdir)
    # the corrupt original survives in quarantine...
    quarantined_files = os.listdir(qdir)
    assert quarantined_files and any(n.startswith("bundle.pdf")
                                     for n in quarantined_files)
    # ...and no longer poisons the cache dir.
    with open(poisoned, "rb") as f:
        assert f.read(len(b"%PDF-")) == b"%PDF-"


def test_download_pdf_cache_hit_skips_network(monkeypatch, tmp_path):
    cache = str(tmp_path)
    cached = os.path.join(cache, "bundle.pdf")
    with open(cached, "wb") as f:
        f.write(VALID_PDF_BYTES)

    hit = df.download_pdf(f"{df.BASE_URL}/f/bundle.pdf", cache,
                          quarantine_dir=str(tmp_path / "q"))
    assert hit == cached            # valid cache entry reused untouched


def test_download_pdf_network_failure_returns_none(monkeypatch, tmp_path):
    def refused(req, timeout=15):
        raise OSError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert df.download_pdf(f"{df.BASE_URL}/f/bundle.pdf",
                           str(tmp_path)) is None


def test_download_pdf_traversal_refusal_still_holds(tmp_path):
    # Decoded name keeps backslash separators -> refused before any write.
    with pytest.raises(ValueError):
        df.download_pdf(f"{df.BASE_URL}/f/%2e%2e%5C%2e%2e%5Cpwned.pdf",
                        str(tmp_path))
    assert not os.listdir(str(tmp_path)), "nothing may be written on refusal"


def test_download_pdf_basename_neutralized_url_degrades_to_download(
        monkeypatch, tmp_path):
    # %2e%2e%2f decodes to '../' which basename strips: the P1 guard's
    # verdict is "safe name", so the attempt degrades to a normal (failing,
    # offline) download instead of raising.
    def refused(req, timeout=15):
        raise OSError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert df.download_pdf(f"{df.BASE_URL}/f/%2e%2e%2fpwned.pdf",
                           str(tmp_path)) is None
    assert not os.listdir(str(tmp_path))


# --------------------------------------- RL3: per-PDF failure tallying -----

def test_process_dallas_pdf_tallies_failed_download(monkeypatch, tmp_path):
    monkeypatch.setattr(df, "download_pdf", lambda url, d, **kw: None)
    sink: list[dict] = []
    records = df.process_dallas_pdf(_item(), str(tmp_path),
                                    failure_sink=sink)
    assert records == []
    assert len(sink) == 1
    assert sink[0]["stage"] == "download"
    assert sink[0]["filename"] == "Dallas_x.pdf"


def test_corrupt_real_bytes_fail_at_split_and_are_tallied(
        monkeypatch, tmp_path):
    # End-to-end with REAL pypdf failure: a >1KB file with a %PDF header but
    # a truncated body makes PdfReader raise deterministically.
    corrupt = tmp_path / "already-cached.pdf"
    corrupt.write_bytes(b"%PDF-1.4\n" + b"garbage" * 300)  # valid-sized poison
    monkeypatch.setattr(df, "download_pdf",
                        lambda url, d, **kw: str(corrupt))
    records, report = df.process_pdf_batch([_item()], str(tmp_path),
                                           quarantine_dir=str(tmp_path / "q"))
    assert records == []
    assert len(report["pdf_failures"]) == 1
    failure = report["pdf_failures"][0]
    assert failure["stage"] == "process"
    assert "PdfStreamError" in failure["error"] or "EOF" in failure["error"], \
        f"expected pypdf stream failure, got: {failure['error']}"


def test_process_batch_survives_sibling_failure(monkeypatch, tmp_path):
    real_split = df.split_pdf_pages
    calls: list[str] = []

    def flaky_download(url, d, **kw):
        name = url.rsplit("/", 1)[-1]
        calls.append(name)
        if name == "bad.pdf":
            return None                      # download dies for this one
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(VALID_PDF_BYTES)
        return path

    class _FakePage:
        def __init__(self, t):
            self._t = t

        def extract_text(self):
            return self._t

    def fake_split(path):
        if path.endswith("garbled.pdf"):
            raise ValueError("xref table mangled")   # parse dies for this one
        return [[_FakePage("NOTICE OF SALE\nnothing usable")]]

    monkeypatch.setattr(df, "download_pdf", flaky_download)
    monkeypatch.setattr(df, "split_pdf_pages", fake_split)

    records, report = df.process_pdf_batch(
        [_item("Dallas"), _item("Garland", filename="bad.pdf"),
         _item("Mesquite", filename="garbled.pdf")],
        str(tmp_path), quarantine_dir=str(tmp_path / "q"))

    # healthy item parsed exactly once despite two sibling failures
    assert len(records) == 1
    assert records[0]["entry_id"] == "DAL_September_Dallas_1"
    stages = {f["filename"]: f["stage"] for f in report["pdf_failures"]}
    assert stages == {"bad.pdf": "download", "garbled.pdf": "process"}
    assert report["pdf_quarantined"] == []
    assert set(calls) == {"Dallas_x.pdf", "bad.pdf", "garbled.pdf"}, \
        "every item must be attempted independently"


def test_process_batch_propagates_quarantine_log(monkeypatch, tmp_path):
    cache = str(tmp_path)
    poisoned = os.path.join(cache, "poisoned.pdf")
    with open(poisoned, "wb") as f:
        f.write(JUNK_PDF_BYTES)                     # >1KB, wrong header
    real_split = df.split_pdf_pages

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=15: _FakeResp(VALID_PDF_BYTES))

    class _FakePage:
        def extract_text(self):
            return "NOTICE OF SALE"

    monkeypatch.setattr(df, "split_pdf_pages",
                        lambda p: [[_FakePage()]])
    try:
        records, report = df.process_pdf_batch(
            [_item("Dallas", filename="poisoned.pdf")], cache,
            quarantine_dir=str(tmp_path / "quarantine"))
    finally:
        pass
    assert len(records) == 1                        # healed and parsed anyway
    assert len(report["pdf_quarantined"]) == 1
    assert report["pdf_quarantined"][0]["path"] == poisoned
    assert report["pdf_failures"] == []


def test_fetch_with_report_carries_failure_report(monkeypatch, tmp_path):
    monkeypatch.setattr(df, "fetch_portal_html", lambda: PORTAL_INTACT)

    def failing_download(url, d, **kw):
        return None                                  # every download fails

    monkeypatch.setattr(df, "download_pdf", failing_download)
    adapter = df.DallasForeclosureAdapter()
    leads, report = adapter.fetch_with_report(limit=2, temp_dir=str(tmp_path))
    assert leads == []
    assert report["links_found"] == 2
    assert len(report["pdf_failures"]) == 2
    assert all(f["stage"] == "download" for f in report["pdf_failures"])
    # fetch() itself stays a plain lead list over the same pipeline
    monkeypatch.setattr(df, "fetch_portal_html", lambda: PORTAL_INTACT)
    assert adapter.fetch(limit=1, temp_dir=str(tmp_path)) == []
