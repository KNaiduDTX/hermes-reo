"""Offline pins for the SOURCE-REGISTRY lookup helpers.

Covers src/source_registry.py exactly as shipped: get_endpoints (filter),
get_endpoint (single pair), get_live_endpoints / get_unverified_endpoints
(status partition) and the REGISTRY record invariants they rest on.

Discovered-contract notes (documented instead of changing src, per the
work order):

1. The work order asked for "get_counties" coverage, but the shipped
   module exposes NO such helper — county enumeration today is
   ``sorted({e.county for e in REGISTRY})`` and per-county access is
   ``get_endpoints(county=...)``. Rather than invent an API in src,
   test_registry_county_partition() below pins the partition properties
   any future get_counties() must satisfy.
   (require_endpoint/assert_registry_sync were added by parallel work;
   they are exercised in tests/test_registry_urls.py, not here.)
2. Unknown lookups never raise: get_endpoint returns None for an unknown
   (county, channel) pair and get_endpoints returns [] when a filter
   matches nothing. Both halves are pinned as-is.
3. get_endpoint returns the FIRST match; REGISTRY-wide uniqueness of
   (county, channel) pairs is what makes that deterministic, so it is
   pinned as a registry invariant too.
"""
from __future__ import annotations

from src.source_registry import (
    REGISTRY,
    SourceEndpoint,
    get_endpoint,
    get_endpoints,
    get_live_endpoints,
    get_unverified_endpoints,
)

# Documented vocabularies (SourceEndpoint comments + SOURCE_REGISTRY.md)
LIVE_STATUSES = ("200", "403_browser")
UNVERIFIED_STATUSES = ("000_timeout", "tbd")
ACCESS_VOCAB = {"http", "browser", "pdf", "api", "tbd"}
COUNTIES = ["collin", "dallas", "grayson"]


def _pairs(endpoints) -> set:
    return {(e.county, e.channel) for e in endpoints}


# ------------------------------------------------------------ known pairs ----

def test_known_county_channel_pairs_resolve_to_documented_urls():
    collin_foreclosure = get_endpoint("collin", "foreclosure_notice")
    assert collin_foreclosure is not None
    assert collin_foreclosure.url == \
        "https://apps2.collincountytx.gov/ForeclosureNotices"
    assert collin_foreclosure.http_status == "200"
    assert collin_foreclosure.access == "browser"

    dallas_sheriff = get_endpoint("dallas", "tax_sale")
    assert dallas_sheriff is not None
    assert dallas_sheriff.url == \
        "https://www.dallascounty.org/departments/tax/sheriff-sales.php"
    assert dallas_sheriff.notes  # documented RealAuction hand-off

    grayson_cad = get_endpoint("grayson", "cad")
    assert grayson_cad is not None
    assert grayson_cad.url == "https://esearch.graysonappraisal.org"
    assert grayson_cad.access == "browser"


def test_get_endpoint_agrees_with_get_endpoints_filter_for_every_pair():
    for endpoint in REGISTRY:
        found = get_endpoint(endpoint.county, endpoint.channel)
        assert found is endpoint  # first match IS the unique record
        assert found in get_endpoints(county=endpoint.county,
                                      channel=endpoint.channel)


# ----------------------------------------------------------- unknown pairs ---

def test_unknown_pairs_return_none_per_contract():
    # Note 2: None, not a raise — pinned deliberately.
    assert get_endpoint("grayson", "clerk") is None      # channel not listed
    assert get_endpoint("tarrant", "tax_sale") is None   # county not covered
    assert get_endpoint("", "") is None                  # nothing matches ""
    assert get_endpoint("collin", "nonexistent_channel") is None


def test_get_endpoints_filter_with_no_matches_returns_empty_list():
    assert get_endpoints(county="tarrant") == []
    assert get_endpoints(channel="deed_scrape") == []
    assert get_endpoints(county="tarrant", channel="tax_sale") == []


def test_get_endpoints_filters_by_county_channel_and_both():
    all_endpoints = get_endpoints()
    assert len(all_endpoints) == len(REGISTRY)  # no filter -> whole registry

    collin = get_endpoints(county="collin")
    assert {e.county for e in collin} == {"collin"}
    assert _pairs(collin) == {
        ("collin", "foreclosure_notice"), ("collin", "cad"),
        ("collin", "clerk"), ("collin", "tax_sale"), ("collin", "tax_suit"),
        # R-3 alt-channel spike rows (unverified feasibility entries)
        ("collin", "probate"), ("collin", "eviction_docket"),
        ("collin", "code_violation"),
    }

    cad_rows = get_endpoints(channel="cad")
    assert _pairs(cad_rows) == {("collin", "cad"), ("dallas", "cad"),
                                ("grayson", "cad")}

    combined = get_endpoints(county="dallas", channel="tax_sale")
    assert [(e.county, e.channel) for e in combined] == [("dallas", "tax_sale")]


# --------------------------------------------------------- live-status gate --

def test_live_filter_keeps_only_verified_200_or_browser_403():
    live = get_live_endpoints()
    assert len(live) >= 10
    for e in live:
        assert e.http_status in LIVE_STATUSES
    pairs = _pairs(live)
    # known-live spot checks across every county...
    assert ("collin", "foreclosure_notice") in pairs
    assert ("dallas", "cad") in pairs
    assert ("grayson", "foreclosure_notice") in pairs
    # ...and the three documented dead/unverified exclusions
    assert ("grayson", "tax_sale") not in pairs     # 000_timeout (:8443 NXDOMAIN)
    assert ("collin", "tax_suit") not in pairs      # tbd
    assert ("grayson", "struck_off") not in pairs   # tbd


def test_unverified_complement_and_status_partition_of_the_registry():
    unverified = get_unverified_endpoints()
    assert _pairs(unverified) == {("collin", "tax_suit"),
                                  ("grayson", "struck_off"),
                                  ("grayson", "tax_sale"),
                                  # R-3 alt-channel spike rows: honest tbd
                                  # until a live check lands.
                                  ("collin", "probate"),
                                  ("collin", "eviction_docket"),
                                  ("collin", "code_violation"),
                                  ("dallas", "probate"),
                                  ("dallas", "eviction_docket"),
                                  ("grayson", "tax_suit")}

    live_pairs = _pairs(get_live_endpoints())
    unverified_pairs = _pairs(unverified)
    all_pairs = _pairs(REGISTRY)
    assert live_pairs.isdisjoint(unverified_pairs)
    assert live_pairs | unverified_pairs == all_pairs
    assert len(get_live_endpoints()) + len(unverified) == len(REGISTRY)


# ------------------------------------------------------- REGISTRY invariants -

def test_county_channel_pairs_are_unique_across_registry():
    pairs = [(e.county, e.channel) for e in REGISTRY]
    assert len(pairs) == len(set(pairs)), \
        "duplicate (county, channel) would make get_endpoint nondeterministic"


def test_registry_records_use_documented_vocabularies():
    assert {e.county for e in REGISTRY} == set(COUNTIES)
    for e in REGISTRY:
        assert e.http_status in LIVE_STATUSES + UNVERIFIED_STATUSES
        assert e.access in ACCESS_VOCAB
        if e.http_status in LIVE_STATUSES:
            assert e.url, f"live endpoint {e.county}/{e.channel} lacks url"
            assert e.access != "tbd"
        if not e.url:  # placeholder rows are exactly the tbd ones
            assert e.http_status == "tbd" and e.access == "tbd"


def test_registry_county_partition_properties_any_get_counties_must_hold():
    # Note 1: stand-in contract for the not-shipped get_counties helper —
    # counties partition REGISTRY exactly, and each slice matches the filter.
    seen: list[str] = []
    for county in sorted({e.county for e in REGISTRY}):
        slice_pairs = _pairs(get_endpoints(county=county))
        assert slice_pairs == _pairs(
            [e for e in REGISTRY if e.county == county])
        assert slice_pairs <= _pairs(REGISTRY)
        assert not seen or slice_pairs.isdisjoint(_pairs(
            get_endpoints(county=seen[-1])))
        seen.append(county)
    union: set = set()
    for county in seen:
        union |= _pairs(get_endpoints(county=county))
    assert union == _pairs(REGISTRY)


def test_sourceendpoint_record_shape_is_positional_with_defaulted_notes():
    # The dataclass is constructed positionally throughout REGISTRY;
    # pin the field order the registry relies on.
    ep = SourceEndpoint("county", "channel", "https://example.test",
                        "200", "http")
    assert (ep.county, ep.channel, ep.url) == \
        ("county", "channel", "https://example.test")
    assert ep.http_status == "200" and ep.access == "http"
    assert ep.notes == ""  # only defaulted field
