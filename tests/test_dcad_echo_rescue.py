# tests/test_dcad_echo_rescue.py
"""Matrix tests for the DCAD echo-match rescue path (R2b).

Covers the abbreviation / city-glue families that historically failed the
fail-closed echo gate even when both sides described the same property:
abbreviated street types (PLACE vs PL), non-breaking-space separators from
the live detail page ("15806\xa0BREEDLOVE\xa0PL"), directional prefixes that
masked the first name token, and situs lines rendered without a zip.
"""
from src.adapters.dallas_cad import (
    _address_tokens,
    _apply_echo_gate,
    _echo_tokens,
    _verify_echo,
)

QUERY = "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334"


# --- matrix: canonical street types -----------------------------------------

def test_place_vs_pl_canonical_type_match_exact():
    """Full trio present + canonical types (PLACE == PL) -> exact."""
    assert _verify_echo(QUERY, "15806 Breedlove Pl, Addison, TX 75001") == (
        True,
        "exact",
    )


def test_place_vs_spelled_out_display_exact():
    """Display spells the type out; query abbreviated — still exact."""
    ok, quality = _verify_echo(
        "1432 ILLINOIS AVENUE, LANCASTER, TX 75134-4633",
        "1432 Illinois Avenue, Lancaster, TX 75134",
    )
    assert ok is True and quality == "exact"


# --- matrix: nbsp separators -------------------------------------------------

def test_breedlove_with_nbsp_rescued_as_partial():
    """Live situs render '15806\xa0BREEDLOVE\xa0PL' no longer fails the split."""
    ok, quality = _verify_echo(QUERY, "15806\xa0BREEDLOVE\xa0PL")
    assert ok is True and quality == "partial"


def test_nbsp_collapsed_in_echo_tokens():
    tokens = _echo_tokens("707\xa0LISA\xa0LN")
    assert tokens["street_number"] == "707"
    assert tokens["street_name_first"] == "LISA"
    assert tokens["street_type"] == "LN"  # LANE -> LN canonical


# --- matrix: directional prefix differences ----------------------------------

def test_directional_prefix_difference_partial_not_mismatch():
    ok, quality = _verify_echo(
        "1425 N Cedar Hill Rd, Cedar Hill, TX 75104", "1425 Cedar Hill Rd"
    )
    assert ok is True and quality == "partial"


def test_directional_prefix_conflict_caps_full_trio_at_partial():
    """Even with number+name+zip all equal, N vs none stays partial."""
    ok, quality = _verify_echo(
        "1425 N CEDAR HILL RD, CEDAR HILL, TX 75104",
        "1425 Cedar Hill Rd, Cedar Hill, TX 75104",
    )
    assert ok is True and quality == "partial"


def test_first_name_token_skips_directionals_and_short_tokens():
    tokens = _echo_tokens("641 NORTH BECKLEY STREET, DESOTO, TEXAS 75217")
    assert tokens["directional"] == "NORTH"
    assert tokens["street_name_first"] == "BECKLEY"
    assert tokens["street_type"] == "ST"


# --- matrix: wrong property must keep failing --------------------------------

def test_wrong_number_still_mismatch():
    ok, quality = _verify_echo(QUERY, "999 Breedlove Pl, Addison, TX 75001")
    assert ok is False and quality == "mismatch"


def test_wrong_number_cannot_be_rescued_by_type_substitution():
    """Zip-less display + agreeing name/type still loses to a wrong number."""
    ok, quality = _verify_echo(QUERY, "999\xa0Breedlove\xa0Pl")
    assert ok is False and quality == "mismatch"


def test_different_street_name_and_zip_still_mismatch():
    ok, quality = _verify_echo(QUERY, "15806 Main St, Dallas, TX 75201")
    assert ok is False and quality == "mismatch"


# --- matrix: missing-zip rescue via street-type substitution -----------------

def test_missing_zip_rescued_via_type_match():
    """Number matches, name token damaged, zip absent -> type casts the vote."""
    ok, quality = _verify_echo(
        "2002 BERKLEY AVENUE DALLAS, TX 75224", "2002 Barkley Ave"
    )
    assert ok is True and quality == "partial"


def test_type_substitution_never_overrides_readable_zip():
    """Same shape but both zips readable and different -> mismatch stands."""
    ok, quality = _verify_echo(
        "2002 BERKLEY AVENUE DALLAS, TX 75224", "2002 Barkley Ave, Dallas, TX 75201"
    )
    assert ok is False and quality == "mismatch"


# --- contract preservation ----------------------------------------------------

def test_unverifiable_empty_case_intact():
    assert _verify_echo(QUERY, "") == (False, "unverifiable")
    assert _verify_echo("", QUERY) == (False, "unverifiable")


def test_city_strings_never_compared():
    """City-glued query vs properly-cased display with clean city -> exact."""
    ok, quality = _verify_echo(
        "2739 SHEILAH DRIVE, BALCHSPRINGS, TX 75180",
        "2739 Sheilah Dr, Balch Springs, TX 75180",
    )
    assert ok is True and quality == "exact"


def test_legacy_address_tokens_shape_unchanged():
    """The pre-rescue raw tokenizer keeps its exact contract."""
    assert _address_tokens(QUERY) == {
        "street_number": "15806",
        "street_name_first": "BREEDLOVE",
        "zip5": "75001",
    }


# --- prior mismatch pairs, replayed through the rescue path -------------------

def test_prior_cache_mismatch_pair_now_passes_partial():
    """Cache key '544 NORTH PALOMINO DRIVE LANCASTER...' vs live nbsp situs."""
    ok, quality = _verify_echo(
        "544 NORTH PALOMINO DRIVE LANCASTER, TX 75146",
        "544\xa0N.\xa0Palomino\xa0Dr",
    )
    assert ok is True and quality == "partial"


def test_gate_passes_rescued_fields_and_still_blocks_wrong_property():
    parsed = {"status": "parsed", "year_built": "2004", "improvement_sqft": "2,100"}

    rescued = _apply_echo_gate(
        dict(parsed), QUERY, "\xa015806\xa0Breedlove\xa0Pl"
    )
    assert rescued["status"] == "parsed"
    assert rescued["match_quality"] == "partial"
    assert rescued["year_built"] == "2004"

    blocked = _apply_echo_gate(dict(parsed), QUERY, "42 OTHER ST, DENISON, TX 75020")
    assert blocked["status"] == "mismatch"
    for leak in ("year_built", "improvement_sqft", "match_quality"):
        assert leak not in blocked, f"property field {leak} leaked past gate"
