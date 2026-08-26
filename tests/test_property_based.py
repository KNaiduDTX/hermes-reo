"""FN10 pilot — property-based tests (hypothesis) over the shared contracts.

Pinned invariants, all verified against current behavior before pinning:

addr.normalize_address
  - Idempotence: re-normalizing ``normalize_address(x).one_line`` stays valid
    and yields identical components (and one_line is a fixed point). The
    composite strategy draws plausible street numbers / names / types from
    STREET_TYPES keys / cities / zips. The "Balch Springs" fix-up city is
    pinned to the comma joiner because the glued-city code path returns the
    CITY_FIXUPS value un-title-cased on first pass; that one-hop case drift
    is a known quirk, not something these tests enshrine.
  - Plausibility gate agreement: ``is_plausible_street_address`` is true
    exactly when ``to_census_one_line`` returns a non-empty line.

dates.parse_date_any
  - ISO round trip: for generated dates, the ISO form parses back equal.
  - datetime passthrough and impossible numeric forms.

pricing
  - Clamp invariant: ``compute_bid_ceiling`` is >= 0 for every input (AUDIT C2
    "clamped at $0") and never exceeds MAO or available cash. NOTE: the bare
    70%-rule helper ``underwriting.mao()`` is intentionally UNclamped (rehab
    may exceed ARV x factor), so the money-path clamp lives here.
  - Ladder rung order opening <= target <= walk <= min(mao, ceiling) when the
    equity backs the MAO (ceiling >= mao); with a starved ceiling only target
    may exceed the capped walk, so the walk-cap invariant gets its own test.
  - batch_price: dead deals carry offer None + stamped dead_reason; alive
    deals offer the ladder target; every priced row has mao/bid_ceiling >= 0;
    unvalued CPRs are skipped outright.

All settings use max_examples=50, deadline=None to keep CI fast. No network.
"""
from __future__ import annotations

import copy
import math
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from src.addr import (
    CITY_FIXUPS,
    STREET_TYPES,
    is_plausible_street_address,
    normalize_address,
    to_census_one_line,
)
from src.cpr import BlastTier, CompRecord, CPR, LienRecord
from src.dates import parse_date_any
from src.pricing import (
    BATCH_ROW_FIELDS,
    DEAD_BID_CEILING_NONPOSITIVE,
    batch_price,
    build_offer_ladder,
    compute_bid_ceiling,
)
from src.valuation import gate_val


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

# Street numbers stay <= 4 digits so they can never collide with the zip
# anchor: a street number equal to the zip leaves no zip token and the
# address is (correctly) rejected as "no zip code found".
STREET_NUMBERS = st.integers(min_value=1, max_value=9999).map(str)

# Name words: alphanumeric plus ' and - (the charset normalize accepts), and
# free of junk-pattern triggers ([!@$%^*], right-of-way, COMER/COMMER ... of,
# THE_, Texas Property Code) and of out-of-state tokens (VA..VZ, CA, NY, FL).
NAME_WORDS = st.sampled_from([
    "OAK", "MAPLE", "CEDAR", "BREEDLOVE", "PLAINSMAN", "LAUDERDALE", "ALAN",
    "SHEILAH", "BECKLEY", "ZANG", "LAMAR", "BOLTON", "WYNNEWOOD", "OCONNOR",
    "SMITH", "JONES", "MILLER", "DAVIS", "PARKER", "GRAYSON", "ELLIS",
    "AKARD", "ERVAY", "HARDIN", "KAVANAGH", "MIMOSA", "O'BRIEN",
    "SMITH-HARDIN", "MILL", "WAY", "PARK",
])
STREET_NAMES = st.lists(NAME_WORDS, min_size=1, max_size=3).map(" ".join)
TYPE_WORDS = st.sampled_from(sorted(STREET_TYPES))
CITIES = st.sampled_from([
    "Dallas", "Addison", "Frisco", "Plano", "Carrollton", "Balch Springs",
    "Richardson", "Garland", "Mesquite", "McKinney", "Allen", "Denton",
    "Fort Worth", "Arlington", "Irving", "Grand Prairie", "Rockwall",
    "Wylie", "Sachse", "Rowlett",
])
ZIPS = st.integers(min_value=10000, max_value=99999)


@st.composite
def _plausible_addresses(draw):
    """Plausible North-Texas addresses with case/spacing/zip4/joiner drift."""
    num = draw(STREET_NUMBERS)
    name = draw(STREET_NAMES)
    type_word = draw(TYPE_WORDS)
    city = draw(CITIES)
    z5 = str(draw(ZIPS))
    z4 = draw(st.one_of(st.none(), st.integers(min_value=1000, max_value=9999)))
    state = draw(st.sampled_from(["TX", "TEXAS", "tx", "texas"]))
    # Glued joiner exercises the "...ALAN AVE CARROLLTON" legacy form; the
    # CITY_FIXUPS city must keep the comma (see module docstring).
    joiner = ", " if city == "Balch Springs" else draw(st.sampled_from([", ", " "]))
    inner_sep = "  " if draw(st.booleans()) else " "
    raw = f"{num}{inner_sep}{name} {type_word}{joiner}{city}, {state} {z5}"
    if z4 is not None:
        raw += f"-{z4}"
    render = draw(st.sampled_from([str.upper, str.lower, str.title]))
    return render(raw), {
        "street_number": num,
        "street_name": name.upper(),
        "street_type": STREET_TYPES[type_word],
        "zip5": z5,
        # Both parse paths agree once the fix-up city keeps its comma:
        # comma path title-cases CITY_FIXUPS values, glued path title-cases
        # the joined city words.
        "city": CITY_FIXUPS.get(
            city.upper().replace(" ", ""), city.upper()).title(),
    }


def _lien_strategy():
    return st.builds(
        LienRecord,
        lien_type=st.sampled_from(
            ["mortgage", "hoa", "irs_federal", "judgment", "tax", "municipal"]),
        amount=st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False),
        survives_foreclosure=st.booleans(),
        survives_tax_sale=st.booleans(),
    )


def _ceiling_inputs():
    """(arv, rehab, factor, mao_val, cash, liens, deal_type) tuples."""
    return st.tuples(
        st.floats(min_value=0.0, max_value=2_000_000.0, allow_nan=False),
        st.floats(min_value=0.0, max_value=500_000.0, allow_nan=False),
        st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        st.floats(min_value=0.0, max_value=2_000_000.0, allow_nan=False),
        st.floats(min_value=0.0, max_value=5_000_000.0, allow_nan=False),
        st.lists(_lien_strategy(), max_size=4),
        st.sampled_from(["DT-AUCTION", "DT-TAXDEED", "", "dt-auction"]),
    )


def _ladder_rungs():
    """(mao, ceiling) in whole $100 steps with ceiling >= mao >= 0."""
    return st.tuples(
        st.integers(min_value=0, max_value=50_000),
        st.integers(min_value=0, max_value=10_000),
    ).map(lambda t: (t[0] * 100.0, (t[0] + t[1]) * 100.0))


def _arbitrary_caps():
    """(mao, ceiling) in whole $100 steps, ceiling possibly far below mao."""
    return st.tuples(
        st.integers(min_value=0, max_value=50_000),
        st.integers(min_value=0, max_value=60_000),
    ).map(lambda t: (t[0] * 100.0, t[1] * 100.0))


@st.composite
def _valued_cprs(draw):
    """GATE-VAL-passed CPRs built from generated fresh/close comps (n>=3).

    Shapes are constrained to the deterministic-pass region of the confidence
    score: mls_agent rigor (+0.30), n>=3 comps (+>=0.125), ages 5-40 days
    (+0.15), avg distance <= 0.35 mi (+0.15), adjustments < 5% (+0.15) —
    worst case 0.875 >= the T1 floor 0.60, so verdict == "pass" always.
    """
    n = draw(st.integers(min_value=3, max_value=6))
    ages = draw(st.lists(st.integers(min_value=5, max_value=40),
                         min_size=n, max_size=n))
    prices = draw(st.lists(st.integers(min_value=50_000, max_value=900_000),
                           min_size=n, max_size=n))
    ratios = draw(st.lists(st.floats(min_value=-0.02, max_value=0.02,
                                     allow_nan=False),
                           min_size=n, max_size=n))
    dists = draw(st.lists(st.floats(min_value=0.10, max_value=0.35,
                                    allow_nan=False),
                          min_size=n, max_size=n))
    force_dead = draw(st.booleans())
    today = datetime.now(timezone.utc).date()
    parcel_id = f"PBT-{uuid4().hex[:10]}"
    cpr = CPR(parcel_id=parcel_id, county="collin",
              situs_address=f"1 Property Based St {parcel_id}")
    comps = [
        CompRecord(address=f"{120 + i} GATE CT",
                   sale_price=float(prices[i]),
                   adjusted_price=prices[i] * (1.0 + ratios[i]),
                   distance_miles=dists[i],
                   source="mls_agent",
                   sale_date=(today - timedelta(days=ages[i])).isoformat())
        for i in range(n)
    ]
    gate, val_obj = gate_val(cpr, "mls_agent", comps, BlastTier.T1)
    assert gate.verdict == "pass", gate.reason
    cpr.valuation = val_obj
    if force_dead:
        cpr.add_lien(LienRecord(lien_type="irs_federal", amount=10_000_000.0,
                                survives_foreclosure=True), "property-based")
    return cpr, force_dead


# --------------------------------------------------------------------------
# addr.normalize_address — idempotence over plausible generated addresses
# --------------------------------------------------------------------------

@given(_plausible_addresses())
@settings(max_examples=50, deadline=None)
def test_renormalize_one_line_stays_valid(case):
    raw, expected = case
    first = normalize_address(raw)
    assert first.valid, (raw, first.invalid_reason)
    second = normalize_address(first.one_line)
    assert second.valid, (first.one_line, second.invalid_reason)


@given(_plausible_addresses())
@settings(max_examples=50, deadline=None)
def test_renormalize_preserves_components_and_is_fixed_point(case):
    raw, _expected = case
    first = normalize_address(raw)
    second = normalize_address(first.one_line)
    assert (second.street_number, second.street_name, second.street_type,
            second.unit, second.city, second.zip5, second.state) == \
           (first.street_number, first.street_name, first.street_type,
            first.unit, first.city, first.zip5, first.state)
    assert second.one_line == first.one_line


@given(_plausible_addresses())
@settings(max_examples=50, deadline=None)
def test_normalization_matches_drawn_oracle(case):
    raw, expected = case
    n = normalize_address(raw)
    assert n.street_number == expected["street_number"]
    assert n.street_name == expected["street_name"]
    assert n.street_type == expected["street_type"]
    assert n.zip5 == expected["zip5"]
    assert n.city == expected["city"]


# --------------------------------------------------------------------------
# addr — plausibility gate agrees with the census renderer
# --------------------------------------------------------------------------

@given(_plausible_addresses())
@settings(max_examples=50, deadline=None)
def test_plausible_implies_census_one_line_nonempty(case):
    raw, _expected = case
    assert is_plausible_street_address(raw)
    assert to_census_one_line(raw) != ""


PRINTABLE_ASCII = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=64)


@given(PRINTABLE_ASCII)
@settings(max_examples=50, deadline=None)
def test_plausibility_gate_agrees_with_census_rendering(noise):
    """Equivalence holds for arbitrary strings, not just plausible ones."""
    assert (to_census_one_line(noise) != "") == is_plausible_street_address(noise)


# --------------------------------------------------------------------------
# dates.parse_date_any — round trips
# --------------------------------------------------------------------------

@given(st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31)))
@settings(max_examples=50, deadline=None)
def test_iso_date_round_trip(d):
    assert parse_date_any(d.isoformat()) == d


@given(st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31)),
       st.integers(min_value=0, max_value=23),
       st.integers(min_value=0, max_value=59),
       st.integers(min_value=0, max_value=59))
@settings(max_examples=50, deadline=None)
def test_datetime_passthrough_round_trip(d, hour, minute, second):
    moment = datetime(d.year, d.month, d.day, hour, minute, second)
    assert parse_date_any(moment) == d


@given(st.integers(min_value=13, max_value=99),
       st.integers(min_value=32, max_value=99),
       st.integers(min_value=1000, max_value=2999))
@settings(max_examples=50, deadline=None)
def test_impossible_numeric_dates_return_none(month, day, year):
    """M/D/Y shapes that cannot be real calendar dates parse to None."""
    assert parse_date_any(f"{month}/{day}/{year}") is None


# --------------------------------------------------------------------------
# pricing — clamps, ladder order, batch dead-deal contract
# --------------------------------------------------------------------------

@given(_ceiling_inputs())
@settings(max_examples=50, deadline=None)
def test_bid_ceiling_clamped_at_zero(inputs):
    """AUDIT C2 clamp: the bid ceiling never goes negative, any inputs."""
    arv, rehab, factor, mao_val, cash, liens, deal_type = inputs
    ceiling = compute_bid_ceiling(arv, rehab, factor, mao_val,
                                  cash_available=cash,
                                  surviving_liens=liens, deal_type=deal_type)
    assert ceiling >= 0.0
    assert math.isfinite(ceiling)


@given(_ceiling_inputs())
@settings(max_examples=50, deadline=None)
def test_bid_ceiling_capped_by_mao_and_cash(inputs):
    arv, rehab, factor, mao_val, cash, liens, deal_type = inputs
    ceiling = compute_bid_ceiling(arv, rehab, factor, mao_val,
                                  cash_available=cash,
                                  surviving_liens=liens, deal_type=deal_type)
    assert ceiling <= mao_val
    assert ceiling <= cash


POSTURES = ["standard", "pre-foreclosure", "tax-delinquent", "unknown-posture"]


@given(_ladder_rungs(), st.sampled_from(POSTURES))
@settings(max_examples=50, deadline=None)
def test_offer_ladder_monotonic_when_equity_backs_mao(rungs, posture):
    """opening <= target <= walk_away <= min(mao, ceiling) when ceiling >= mao."""
    mao_val, ceiling = rungs
    ladder = build_offer_ladder(mao_val, posture, bid_ceiling_val=ceiling)
    assert ladder["opening"] >= 0.0
    assert ladder["target"] >= 0.0
    assert ladder["walk_away"] >= 0.0
    assert ladder["opening"] <= ladder["target"]
    assert ladder["target"] <= ladder["walk_away"]
    assert ladder["walk_away"] <= min(mao_val, ceiling) + 1e-9


@given(_arbitrary_caps())
@settings(max_examples=50, deadline=None)
def test_walk_away_never_exceeds_min_of_mao_and_ceiling(rungs):
    """H3 cap holds even when the ceiling starves the ladder below MAO."""
    mao_val, ceiling = rungs
    ladder = build_offer_ladder(mao_val, "standard", bid_ceiling_val=ceiling)
    assert ladder["walk_away"] >= 0.0
    assert ladder["walk_away"] <= min(mao_val, ceiling) + 1e-9
    assert ladder["opening"] >= 0.0 and ladder["opening"] <= ladder["target"]


@given(_valued_cprs())
@settings(max_examples=50, deadline=None)
def test_batch_price_dead_deal_has_no_offer(valued):
    """Dead rows: offer None + stamped reason; alive rows offer the target."""
    cpr, forced_dead = valued
    rows = batch_price([cpr])
    assert len(rows) == 1
    row = rows[0]
    assert tuple(row.keys()) == BATCH_ROW_FIELDS
    assert row["parcel_id"] == cpr.parcel_id
    assert row["mao"] >= 0.0          # gated ARVs are positive -> MAO >= 0
    assert row["bid_ceiling"] >= 0.0  # the C2 clamp survives batching
    if forced_dead:
        assert row["bid_ceiling"] == 0.0
        assert row["offer"] is None
        assert row["dead_reason"] == DEAD_BID_CEILING_NONPOSITIVE
    else:
        assert row["dead_reason"] is None
        assert row["offer"] is not None
        assert row["offer"] == build_offer_ladder(row["mao"])["target"]


@given(_valued_cprs())
@settings(max_examples=50, deadline=None)
def test_batch_price_skips_unvalued_cprs(valued):
    """Only GATE-VAL-valued CPRs reach the investor pack."""
    cpr, _forced_dead = valued
    unvalued = CPR(parcel_id=f"SKIP-{uuid4().hex[:10]}", county="collin")
    hitl_only = CPR(parcel_id=f"HITL-{uuid4().hex[:10]}", county="collin")
    # Deep-copy: sharing the ValuationObject would flip cpr's verdict too.
    hitl_only.valuation = copy.deepcopy(cpr.valuation)
    hitl_only.valuation.gate_val_verdict = "hitl"
    rows = batch_price([unvalued, hitl_only, cpr])
    ids = [r["parcel_id"] for r in rows]
    assert ids == [cpr.parcel_id]
    assert unvalued.parcel_id not in ids
    assert hitl_only.parcel_id not in ids
