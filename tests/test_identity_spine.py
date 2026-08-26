"""FN1+FN2 identity-spine tests for find_deals.py.

Covers the src.identity integration and history store end to end:
- notice_id stability under case/type/date-format drift; a postponed sale
  is a NEW notice (FN1 contract from OPPORTUNITY_MAP).
- property_id stability under zip noise + county-case drift.
- stamp_identity_columns: additive schema growth only, legacy parcel_id
  untouched, owner joins property_id ONLY when the address is invalid.
- merge_prior_enrichment re-keyed to property_id as PRIMARY match, legacy
  address identity kept purely as fallback when either side lacks an id,
  no cross-asset merge when both sides carry disagreeing ids.
- dedupe_by_property_id keeps the richest record per asset.
- derive_history: first_seen/last_seen/seen_count math, debt-change and
  lender-change folding, sale-date format-drift collapse on synthetic
  snapshots.
- update_history_store across two runs in tmp_path: daily snapshot +
  derived notices block written atomically, index.json accumulation, churn
  line counts, corrupt-snapshot tolerance, same-day rerun idempotence.
"""
import json
import os

from src.cpr import CPR
from src.identity import notice_id, property_id

import find_deals
from find_deals import (
    IDENTITY_COLUMN_KEYS,
    dedupe_by_property_id,
    derive_history,
    enrichment_richness,
    identity_columns,
    merge_prior_enrichment,
    stamp_identity_columns,
    update_history_store,
)

BALCH = "2739 SHEILAH DRIVE, BALCHSPRINGS, TX 75180"
BREEDLOVE = "15806 BREEDLOVE PLACE, ADDISON, TX 75001"
GARBAGE_PROSE = "60 foot right-of-way, at the northeast comer of said lot"


# --- notice_id: stability + postponement ----------------------------------------
def test_notice_id_stable_under_case_type_date_format_drift():
    variants = [
        {"address": BREEDLOVE, "lender": "CTX MORTGAGE COMPANY",
         "debt": 309600.0, "sale_date": "Tuesday, the 1st day of September, 2026"},
        {"address": "15806  Breedlove Pl, Addison, TX 75001-6334",
         "lender": "ctx mortgage co.", "debt": 309600,
         "sale_date": "09/01/2026"},
        {"address": "15806 Breedlove Pl, Addison, Texas 75001",
         "lender": "CTX Mortgage Co", "debt": "309600.00",
         "sale_date": "2026-09-01"},
    ]
    ids = {notice_id(**variant) for variant in variants}
    assert len(ids) == 1, "case/type/date-format drift must not split a notice"


def test_postponed_sale_is_a_new_notice():
    base = {"address": "10022 PLAINSMAN LN, FRISCO, TX 75035",
            "lender": "EXAMPLE BANK NA", "debt": 250000}
    september = notice_id(sale_date="2026-09-01", **base)
    october = notice_id(sale_date="2026-10-06", **base)
    assert september != october, "postponement mints a NEW notice id"
    # ...while format drift within one date never does.
    assert september == notice_id(
        sale_date="Tuesday, the 1st day of September, 2026", **base)


# --- property_id: zip noise / county case / invalid-address tiebreaker ----------
def test_property_id_stable_under_zip_noise_and_county_case():
    canonical = property_id("dallas", BREEDLOVE)
    assert canonical == property_id("DALLAS", BREEDLOVE)          # county case
    assert canonical == property_id(
        "Dallas", "15806  Breedlove Pl, Addison, TX 75201")       # wrong zip ok
    assert canonical != property_id("collin", BREEDLOVE)          # other county


def test_owner_tiebreaker_applies_only_to_invalid_addresses():
    valid_a = identity_columns(address=BALCH, county="dallas", owner="A OWNER")
    valid_b = identity_columns(address=BALCH, county="dallas", owner="B OWNER")
    assert valid_a["property_id"] == valid_b["property_id"]

    bad_a = identity_columns(address=GARBAGE_PROSE, county="dallas", owner="A")
    bad_b = identity_columns(address=GARBAGE_PROSE, county="dallas", owner="B")
    assert bad_a["property_id"] != bad_b["property_id"], \
        "garbage addresses need the owner tiebreaker to stay distinct"


# --- FN1 integration: additive stamping on analyzed rows ------------------------
def _cpr(address):
    cpr = CPR(parcel_id="DAL_26_DALLAS_7", county="dallas", situs_address=address)
    cpr.set_field("stories", None, "test")
    cpr.set_field("pool", None, "test")
    return cpr


def test_stamp_identity_columns_additive_schema_parcel_untouched():
    deal = find_deals.analyze_deal(_cpr(BALCH))
    deal.update({"mortgage_debt": 250_000.0, "sale_date": "2026-09-01",
                 "lender": "EXAMPLE BANK"})
    before_keys = set(deal)
    parcel_before = deal["parcel_id"]

    stamped = stamp_identity_columns(deal)
    assert stamped is deal                                   # in-place stamp
    assert set(stamped) - before_keys == set(IDENTITY_COLUMN_KEYS)
    assert stamped["parcel_id"] == parcel_before == "DAL_26_DALLAS_7"
    assert len(stamped["notice_id"]) == 13 and stamped["notice_id"][0] == "N"
    assert len(stamped["property_id"]) == 13 and stamped["property_id"][0] == "P"
    # The row's own values drove the notice hash (lender/debt/sale_date).
    assert stamped["notice_id"] == notice_id(
        address=BALCH, lender="EXAMPLE BANK", debt=250_000.0,
        sale_date="2026-09-01")


def test_stamped_rows_share_property_id_across_scrape_noise():
    r1 = stamp_identity_columns({"address": BREEDLOVE, "county": "DALLAS"})
    r2 = stamp_identity_columns({
        "address": "15806  Breedlove Pl, Addison, TX 75001-6334",
        "county": "dallas"})
    r3 = stamp_identity_columns({"address": "202 OAK AVE, PLANO, TX 75023",
                                 "county": "collin"})
    assert r1["property_id"] == r2["property_id"]
    assert r3["property_id"] not in (r1["property_id"],)


# --- FN1: within-run dedupe keeps richest record ---------------------------------
def _dup_row(pid, sqft=None, beds=None, cad=None, address="1 A ST"):
    return {"property_id": pid, "address": address, "county": "dallas",
            "sqft": sqft, "beds": beds, "cad_value": cad}


def test_dedupe_keeps_richest_record_per_property_id():
    sparse = _dup_row("P1")
    rich = _dup_row("P1", sqft=1800, beds=3, cad=250_000)
    middle = _dup_row("P1", sqft=1500)
    other = _dup_row("P2", address="2 B ST")

    out = dedupe_by_property_id([sparse, rich, middle, other])
    assert [row["property_id"] for row in out] == ["P1", "P2"]
    assert out[0]["cad_value"] == 250_000 and out[0]["beds"] == 3
    assert out[0]["address"] == "1 A ST"      # first-appearance position kept


def test_dedupe_tie_keeps_first_occurrence_and_bypasses_unstamped_rows():
    first = _dup_row("P1", sqft=1000)
    twin = _dup_row("P1", sqft=1000)
    orphan = {"address": "x", "county": "dallas"}             # no id at all
    out = dedupe_by_property_id([first, orphan, twin])
    assert out == [first, orphan]
    assert enrichment_richness(first) == enrichment_richness(twin)


# --- FN1: merge-on-write re-keyed to property_id ---------------------------------
def _write(tmp_path, payload):
    path = tmp_path / "prior.json"
    path.write_text(json.dumps(payload))
    return str(path)


def test_merge_prefers_property_id_over_address_key(tmp_path):
    prior = [
        {"address": "1 A ST, DALLAS, TX 75201", "county": "dallas",
         "property_id": "P_STALE_A", "sqft": 1000},
        {"address": "1 A ST, DALLAS, TX 75201", "county": "dallas",
         "property_id": "P_STALE_B", "sqft": 2000},
    ]
    fresh = [{"address": "1 A St, Dallas, TX 75201", "county": "dallas",
              "property_id": "P_STALE_B", "sqft": None}]
    merged = merge_prior_enrichment(fresh, _write(tmp_path, prior))
    assert merged[0]["sqft"] == 2000     # matched by id, not first address hit


def test_merge_falls_back_when_either_side_lacks_an_id(tmp_path):
    # Old dataset: prior has NO property_id yet.
    prior = [{"address": "1 A ST, DALLAS, TX 75201", "county": "dallas",
              "sqft": 1500}]
    fresh = [{"address": "1 A St, Dallas, TX 75201", "county": "dallas",
              "property_id": "P_NEW", "sqft": None}]
    merged = merge_prior_enrichment(fresh, _write(tmp_path, prior))
    assert merged[0]["sqft"] == 1500

    # Reverse: fresh row predates stamping, prior carries an id.
    prior2 = [{"address": "1 A ST, DALLAS, TX 75201", "county": "dallas",
               "property_id": "P_OLD", "sqft": 1700}]
    fresh2 = [{"address": "1 a st, dallas, tx 75201", "county": "Dallas",
               "sqft": None}]
    merged2 = merge_prior_enrichment(fresh2, _write(tmp_path, prior2))
    assert merged2[0]["sqft"] == 1700


def test_merge_never_crosses_disagreeing_ids(tmp_path):
    prior = [{"address": "1 A ST, DALLAS, TX 75201", "county": "dallas",
              "property_id": "P_OTHER_ASSET", "sqft": 999}]
    fresh = [{"address": "1 A ST, DALLAS, TX 75201", "county": "dallas",
              "property_id": "P_THIS_ASSET", "sqft": None}]
    merged = merge_prior_enrichment(fresh, _write(tmp_path, prior))
    assert merged[0]["sqft"] is None     # primary key wins: no fallback merge


# --- FN2: derive_history pure math ------------------------------------------------
def _snap(date_str, *rows):
    return {"date": date_str, "deals": list(rows)}


def _obs(pid, sale="2026-09-01", debt=None, lender=None):
    return {"property_id": pid, "sale_date": sale, "mortgage_debt": debt,
            "lender": lender}


def test_derive_history_first_seen_last_seen_seen_count_math():
    previous = [
        _snap("2026-01-01", _obs("P1", debt=100_000)),
        _snap("2026-02-01", _obs("P1", debt=100_000), _obs("P2")),
    ]
    history = derive_history(previous, _snap("2026-03-01", _obs("P1")))

    assert history["P1"]["first_seen"] == "2026-01-01"
    assert history["P1"]["last_seen"] == "2026-03-01"
    assert history["P1"]["seen_count"] == 3
    assert history["P2"]["first_seen"] == "2026-02-01"
    assert history["P2"]["last_seen"] == "2026-02-01"
    assert history["P2"]["seen_count"] == 1
    # Unstamped rows are ignored, never crash the fold.
    assert set(history) == {"P1", "P2"}


def test_derive_history_debt_change_math_on_synthetic_snapshots():
    previous = [
        _snap("2026-01-01", _obs("P1", debt=100_000)),
        _snap("2026-02-01", _obs("P1", debt=120_000)),
        _snap("2026-03-01", _obs("P1", debt=120_000)),   # repeat: no change
    ]
    history = derive_history(previous, _snap("2026-04-01"))
    assert history["P1"]["debt_changes"] == [[100_000, 120_000]]

    # Unknown endpoints never fabricate a change (None -> value -> None).
    sparse = [
        _snap("2026-01-01", _obs("P1")),
        _snap("2026-02-01", _obs("P1", debt=90_000)),
    ]
    history2 = derive_history(sparse, _snap("2026-03-01", _obs("P1")))
    assert history2["P1"]["debt_changes"] == []


def test_derive_history_lender_changes_and_sale_date_drift_collapse():
    previous = [
        _snap("2026-01-01",
              _obs("P1", sale="Tuesday, the 1st day of September, 2026",
                   lender="A BANK")),
        _snap("2026-02-01",
              _obs("P1", sale="09/01/2026", lender="A BANK")),
    ]
    history = derive_history(previous, _snap(
        "2026-03-01",
        _obs("P1", sale="Tuesday, the 6th day of October, 2026",
             lender="B LENDING LLC")))

    # Format drift collapses to one ISO date; postponement adds a second.
    assert history["P1"]["sale_dates"] == ["2026-09-01", "2026-10-06"]
    assert history["P1"]["lender_changes"] == [["A BANK", "B LENDING LLC"]]
    assert history["P1"]["debt_changes"] == []
    assert history["P1"]["seen_count"] == 3


# --- FN2: two-run accumulation through the real writer ---------------------------
def _deal_row(pid, debt, sale="2026-09-01", lender="A BANK"):
    return {"property_id": pid, "address": "1 A ST, DALLAS, TX 75201",
            "county": "dallas", "sale_date": sale, "mortgage_debt": debt,
            "lender": lender}


def test_two_runs_accumulate_snapshot_index_and_churn(tmp_path):
    history_dir = str(tmp_path / "history")

    run1_rows = [_deal_row("P1", 100_000), _deal_row("P2", 55_000)]
    snap1, churn1 = update_history_store(history_dir, "2026-01-01", run1_rows)
    assert os.path.basename(snap1) == "2026-01-01.json"
    assert churn1 == "0 properties carried, 2 new, 0 with sale-date changes"

    run2_rows = [_deal_row("P1", 120_000, sale="2026-10-06")]   # P2 gone
    snap2, churn2 = update_history_store(history_dir, "2026-01-02", run2_rows)
    assert os.path.basename(snap2) == "2026-01-02.json"
    assert churn2 == "1 properties carried, 0 new, 1 with sale-date changes"

    index = json.loads(open(os.path.join(history_dir, "index.json")).read())
    assert index["P1"] == {"first_seen": "2026-01-01",
                           "last_seen": "2026-01-02", "seen_count": 2}
    assert index["P2"] == {"first_seen": "2026-01-01",
                           "last_seen": "2026-01-01", "seen_count": 1}

    snapshot = json.loads(open(snap2).read())
    assert snapshot["date"] == "2026-01-02"
    assert [row["property_id"] for row in snapshot["deals"]] == ["P1"]
    notices = snapshot["notices"]
    assert notices["P1"]["debt_changes"] == [[100_000, 120_000]]
    assert notices["P1"]["sale_dates"] == ["2026-09-01", "2026-10-06"]
    assert notices["P2"]["last_seen"] == "2026-01-01"   # remembered after exit


def test_same_day_rerun_does_not_inflate_index(tmp_path):
    history_dir = str(tmp_path / "history")
    rows = [_deal_row("P1", 100_000)]
    update_history_store(history_dir, "2026-01-01", rows)
    _, churn_again = update_history_store(history_dir, "2026-01-01", rows)

    index = json.loads(open(os.path.join(history_dir, "index.json")).read())
    assert index["P1"]["seen_count"] == 1
    assert index["P1"]["first_seen"] == "2026-01-01"
    assert churn_again == "0 properties carried, 1 new, 0 with sale-date changes"


def test_corrupt_or_foreign_history_files_are_tolerated(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    (history_dir / "2025-12-31.json").write_text("{not json at all")   # corrupt
    (history_dir / "notes.md").write_text("not a snapshot")            # foreign
    good = history_dir / "2026-01-01.json"
    good.write_text(json.dumps({"date": "2026-01-01",
                                "deals": [_deal_row("P1", 100_000)]}))

    snap, churn = update_history_store(str(history_dir), "2026-01-02",
                                       [_deal_row("P1", 100_000)])
    assert churn == "1 properties carried, 0 new, 0 with sale-date changes"
    payload = json.loads(open(snap).read())
    assert payload["notices"]["P1"]["seen_count"] == 2   # only the good file
