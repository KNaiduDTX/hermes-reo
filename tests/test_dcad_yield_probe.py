"""RL9/FN3: DCAD parse-yield probe + per-year value extraction tests.

Offline only — BeautifulSoup HTML fixtures, no browser, no network.

Covers:
  RL9  HERMES_DCAD_STATS opt-in exposes parse_stats {detail_link_found,
       extracted_fields_count, echo_quality, derived_source} on search
       results; disabled by default with zero shape change.
  FN3  labeled per-year values emit BOTH raw strings (cad_value_{YYYY}_raw)
       and parsed floats (cad_value_{YYYY}); a pair carrying the legacy
       flat 25/24 uplift is flagged derived_source="2026" so enrichment can
       stop copying fabricated year pairs.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from src.adapters import dallas_cad as dc
from src.adapters.dallas_cad import (
    DCAD_STATS_ENV,
    LEGACY_UPLIFT_DEN,
    LEGACY_UPLIFT_NUM,
    _parse_dcad_detail,
    attach_parse_stats,
    build_parse_stats,
    dcad_stats_enabled,
    detect_derived_year,
)

QUERY = "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334"
SHOWN_EXACT = "15806 Breedlove Place, Addison, TX 75001-6334"
SHOWN_MISMATCH = "999 OTHER ST, DENISON, TX 75020"

DETAIL_HTML = """
<html><body><table>
  <tr><td>Year Built</td><td>2004</td></tr>
  <tr><td>Living Area</td><td>2,100 sqft</td></tr>
  <tr><td># Bedrooms</td><td>3</td></tr>
  <tr><td># Baths (Full/Half)</td><td>2 / 1</td></tr>
  <tr><td>Market Value:</td><td>$730,673</td></tr>
  <tr><td>Appraised Value (2025)</td><td>: $749,180</td></tr>
  <tr><td>Appraised Value (2026)</td><td>: $780,437.50</td></tr>
</table></body></html>
"""


def soup_of(html: str):
    return BeautifulSoup(html, "html.parser")


# ------------------------------------------------- RL9: env-gated probe -----

def test_probe_disabled_by_default(monkeypatch):
    monkeypatch.delenv(DCAD_STATS_ENV, raising=False)
    assert dcad_stats_enabled() is False


def test_probe_env_matrix(monkeypatch):
    for on in ("1", "true", "TRUE", "Yes", "on"):
        monkeypatch.setenv(DCAD_STATS_ENV, on)
        assert dcad_stats_enabled() is True, on
    for off in ("0", "", "false", "no", "off", "maybe"):
        monkeypatch.setenv(DCAD_STATS_ENV, off)
        assert dcad_stats_enabled() is False, off
    # explicit-arg path never touches os.environ
    assert dcad_stats_enabled(env="1") is True
    assert dcad_stats_enabled(env="") is False


def test_disabled_probe_leaves_result_untouched(monkeypatch):
    monkeypatch.delenv(DCAD_STATS_ENV, raising=False)
    result = {"status": "parsed"}
    parsed = {"status": "parsed", "year_built": "2004"}
    out = attach_parse_stats(result, parsed, QUERY, SHOWN_EXACT, True)
    assert out is result                       # same object, unmodified
    assert "parse_stats" not in out


def test_enabled_probe_payload_shape_and_values(monkeypatch):
    monkeypatch.setenv(DCAD_STATS_ENV, "1")
    parsed = {"status": "parsed", "year_built": "2004",
              "improvement_sqft": "2100"}
    out = attach_parse_stats({"status": "parsed"}, parsed, QUERY,
                             SHOWN_EXACT, detail_link_found=True)
    stats = out["parse_stats"]
    assert set(stats) == {"detail_link_found", "extracted_fields_count",
                          "echo_quality", "derived_source"}
    assert stats["detail_link_found"] is True
    assert stats["echo_quality"] == "exact"
    assert stats["extracted_fields_count"] == 2
    assert stats["derived_source"] is None


def test_probe_reports_mismatch_quality_on_gated_result(monkeypatch):
    monkeypatch.setenv(DCAD_STATS_ENV, "1")
    gated = dc._apply_echo_gate({"status": "parsed", "beds": "3"},
                                QUERY, SHOWN_MISMATCH)
    assert gated["status"] == "mismatch"
    out = attach_parse_stats(gated, {"status": "parsed"}, QUERY,
                             SHOWN_MISMATCH, True)
    assert out["parse_stats"]["echo_quality"] == "mismatch"


def test_extracted_fields_count_excludes_meta_and_raw_mirrors(monkeypatch):
    parsed = {
        "status": "parsed",            # meta
        "match_quality": "partial",    # meta
        "derived_source": "2026",      # meta
        "beds": "3",
        "baths": None,                 # unparsed -> not counted
        "cad_value_2025": 749180.0,
        "cad_value_2025_raw": "Appraised Value (2025): $749,180",  # mirror
        "owner": "DOE JOHN",
    }
    stats = build_parse_stats(True, parsed, "partial")
    assert stats["extracted_fields_count"] == 3   # beds, cad float, owner
    assert stats["derived_source"] == "2026"      # mirrored into the probe
    # ...and the count is stable whether or not the raw string was captured
    del parsed["cad_value_2025_raw"]
    assert build_parse_stats(True, parsed, "partial")[
        "extracted_fields_count"] == 3


# ----------------------------- FN3: per-year values and derived flag -------

def test_per_year_values_emit_raw_strings_and_parsed_floats():
    parsed = _parse_dcad_detail(soup_of(DETAIL_HTML))
    assert parsed["cad_appraised_value"] == "730673"      # legacy capture
    assert parsed["cad_value_2025_raw"] == \
        "Appraised Value (2025): $749,180"
    assert parsed["cad_value_2025"] == 749180.0
    assert parsed["cad_value_2026_raw"].startswith("Appraised Value (2026)")
    assert parsed["cad_value_2026"] == 780437.5           # cents survive
    assert "derived_source" not in parsed                 # distinct pair


def test_unlabeled_page_gains_no_year_keys():
    parsed = _parse_dcad_detail(
        soup_of("<html><body>Market Value: $200,000 Year Built 1990</body>"
                "</html>"))
    assert all(not k.startswith("cad_value_2") for k in parsed)
    assert "derived_source" not in parsed
    assert parsed["status"] == "parsed"


def test_market_value_with_year_label_is_also_captured():
    html = ("<html><body><tr><td>Market Value (2026)</td>"
            "<td>: $387,000</td></tr></body></html>")
    parsed = _parse_dcad_detail(soup_of(html))
    assert parsed["cad_value_2026"] == 387000.0
    assert parsed["cad_value_2026_raw"].startswith("Market Value (2026)")


def test_detect_derived_year_signature_matrix():
    v25 = 749180.0
    exact_uplift = v25 * LEGACY_UPLIFT_NUM / LEGACY_UPLIFT_DEN
    assert detect_derived_year(v25, exact_uplift) == "2026"
    # cent-rounded display of the synthesized figure still trips it
    assert detect_derived_year(v25, round(exact_uplift, 2)) == "2026"
    # dollar-rounded harvest figure too ($0.17 away from exact)
    assert detect_derived_year(v25, round(exact_uplift)) == "2026"
    # genuinely different ratio never flags
    assert detect_derived_year(v25, 780437.50) is None
    assert detect_derived_year(200000.0, 300000.0) is None
    # degenerate inputs are never flagged
    assert detect_derived_year(None, exact_uplift) is None
    assert detect_derived_year(v25, None) is None
    assert detect_derived_year("abc", exact_uplift) is None
    assert detect_derived_year(0, 0) is None
    assert detect_derived_year(-100.0, -104.17) is None


def test_exact_ratio_pair_parsed_from_html_is_flagged():
    uplift_html = DETAIL_HTML.replace("$780,437.50", "$780,395.83")
    parsed = _parse_dcad_detail(soup_of(uplift_html))
    assert parsed["cad_value_2026"] == 780395.83
    assert parsed["derived_source"] == "2026"


def test_derived_pair_flag_flows_through_the_probe(monkeypatch):
    monkeypatch.setenv(DCAD_STATS_ENV, "1")
    parsed = _parse_dcad_detail(
        soup_of(DETAIL_HTML.replace("$780,437.50", "$780,395.83")))
    out = attach_parse_stats({"status": "parsed"}, parsed, QUERY,
                             SHOWN_EXACT, True)
    assert out["parse_stats"]["derived_source"] == "2026"
    # the raw provenance strings stay on the PARSED payload for auditability
    assert parsed["cad_value_2025_raw"] == "Appraised Value (2025): $749,180"


def test_parse_stats_absent_without_env_even_for_derived_pairs(monkeypatch):
    monkeypatch.delenv(DCAD_STATS_ENV, raising=False)
    parsed = {"status": "parsed", "cad_value_2025": 749180.0,
              "cad_value_2026": 780395.8333333334, "derived_source": "2026"}
    out = attach_parse_stats({"status": "parsed"}, dict(parsed), QUERY,
                             SHOWN_EXACT, True)
    assert "parse_stats" not in out          # no shape change when opted out


def test_module_policy_constants_consistent():
    # The uplift signature is the documented 25/24 (+1/24) ratio.
    assert LEGACY_UPLIFT_NUM > LEGACY_UPLIFT_DEN
    assert abs(LEGACY_UPLIFT_NUM / LEGACY_UPLIFT_DEN - 1.0416667) < 1e-6
