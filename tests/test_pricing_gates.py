"""Financial-gate regression tests: the audit's Critical money-answer bugs.

C2  negative bid ceiling -> negative purchase -> fake profit
H1  underwriting defaulting purchase to MAO
H3  walk-away exceeding MAO
F12 optimistic_only mislabeling never-pencils deals
M6  bid ceiling subtracting liens that do not survive the deal type
"""
import pytest

from src.cpr import CPR, LienRecord
from src.pricing import (
    PENCIL_BASE,
    PENCIL_NEVER,
    PENCIL_OPTIMISTIC_ONLY,
    build_offer_ladder,
    compute_bid_ceiling,
    price_deal,
)
from src.underwriting import flip_pnl, route_underwrite


LIEN = LienRecord(lien_type="tax", amount=100_000.0,
                  survives_foreclosure=True, survives_tax_sale=True)


def test_ceiling_clamps_at_zero_with_surviving_liens():
    """AUDIT C2: ARV 200k / rehab 50k / 100k surviving lien used to give -10k."""
    ceiling = compute_bid_ceiling(arv=200_000, rehab=50_000, factor=0.70,
                                  mao_val=140_000, surviving_liens=[LIEN])
    assert ceiling == 0


def test_ceiling_never_exceeds_mao():
    ceiling = compute_bid_ceiling(arv=500_000, rehab=20_000, factor=0.70,
                                  mao_val=150_000)
    assert ceiling <= 150_000


# --- AUDIT M6: deal-type-aware lien survival selection ---

FC_ONLY = LienRecord(lien_type="irs_federal", amount=100_000.0,
                     survives_foreclosure=True, survives_tax_sale=False)
TAX_ONLY = LienRecord(lien_type="tax", amount=100_000.0,
                      survives_foreclosure=False, survives_tax_sale=True)

# Raw equity before liens: 200k * 0.70 - 50k rehab = 90k.
_ARV, _REHAB, _FACTOR, _MAO = 200_000.0, 50_000.0, 0.70, 140_000.0


def test_unknown_or_empty_deal_type_keeps_legacy_union():
    """Backward compat: without a deal type either survivor nets out."""
    assert compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                               mao_val=_MAO, surviving_liens=[FC_ONLY]) == 0
    assert compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                               mao_val=_MAO, surviving_liens=[TAX_ONLY]) == 0


def test_taxdeed_ceiling_ignores_foreclosure_only_survivor():
    """AUDIT M6: a tax-deed bid does not pay off liens that die at tax sale.

    The IRS lien (foreclosure-only survivor) must be excluded from a
    DT-TAXDEED ceiling: raw equity 90k stays intact instead of clamping
    to 0 under the legacy union.
    """
    ceiling = compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                                  mao_val=_MAO, surviving_liens=[FC_ONLY],
                                  deal_type="DT-TAXDEED")
    assert ceiling == 90_000
    # Case-insensitive matching.
    assert compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                               mao_val=_MAO, surviving_liens=[FC_ONLY],
                               deal_type="dt-taxdeed") == 90_000


def test_auction_ceiling_ignores_tax_sale_only_survivor():
    """AUDIT M6: an auction bid does not pay off tax-sale-only survivors."""
    ceiling = compute_bid_ceiling(arv=_ARV, rehab=_REHAB, factor=_FACTOR,
                                  mao_val=_MAO, surviving_liens=[TAX_ONLY],
                                  deal_type="DT-AUCTION")
    assert ceiling == 90_000


def test_price_deal_passes_deal_type_to_ceiling():
    """price_deal threads deal_type through to compute_bid_ceiling (M6).

    Same CPR, same numbers: the foreclosure-only IRS survivor is ignored
    for DT-TAXDEED (alive at a 90k ceiling) but honored for DT-AUCTION
    (ceiling swamped -> dead).
    """
    cpr = CPR(parcel_id="M6", county="collin", situs_address="1 Main St")
    cpr.add_lien(FC_ONLY, "test")

    priced_td = price_deal(cpr, arv=_ARV, rehab=_REHAB,
                           deal_type="DT-TAXDEED")
    assert priced_td.bid_ceiling == 90_000
    assert priced_td.dead_reason is None

    priced_auc = price_deal(cpr, arv=_ARV, rehab=_REHAB,
                            deal_type="DT-AUCTION")
    assert priced_auc.bid_ceiling == 0
    assert priced_auc.dead_reason is not None


def test_route_underwrite_requires_purchase():
    """AUDIT H1: silently pricing a deal at its own MAO is self-confirming."""
    cpr = CPR(parcel_id="P", county="collin", situs_address="1 Main St")
    with pytest.raises(ValueError):
        route_underwrite(cpr, arv=300_000, rehab=30_000)


def test_walk_away_capped_at_mao():
    """AUDIT H3: ladder used to offer walk-away = 105% of the stated maximum."""
    ladder = build_offer_ladder(mao_val=300_000, bid_ceiling_val=400_000,
                                seller_posture="standard")
    assert ladder["walk_away"] <= 300_000 + 1e-6
    assert ladder["opening"] <= ladder["target"] <= ladder["walk_away"]


def test_walk_away_capped_at_bid_ceiling():
    ladder = build_offer_ladder(mao_val=300_000, bid_ceiling_val=200_000,
                                seller_posture="standard")
    assert ladder["walk_away"] <= 200_000 + 1e-6


def test_flip_still_pencils_on_sane_inputs():
    uw = flip_pnl(arv=300_000, purchase=180_000, rehab=30_000,
                  holding=5_000, financing=10_000, closing_buy=5_000,
                  closing_sell=24_000)
    assert uw.net_profit > 0


# --- F5-lite: batch pricing over valued CPRs -------------------------------

def _valued_cpr(parcel_id: str, county: str = "collin"):
    """Synthesize a GATE-VAL-passed CPR: 3 fresh, close, adjusted MLS comps."""
    from datetime import datetime, timedelta, timezone

    from src.cpr import BlastTier, CompRecord
    from src.valuation import attach_valuation, gate_val

    cpr = CPR(parcel_id=parcel_id, county=county,
              situs_address=f"1 Batch St {parcel_id}")
    today = datetime.now(timezone.utc).date()
    comps = [
        CompRecord(address=f"{100 + i} Neighbor Ct",
                   sale_price=300_000 + i * 10_000,
                   adjusted_price=301_000 + i * 10_000,
                   distance_miles=0.2 + 0.05 * i,
                   source="mls_agent",
                   sale_date=(today - timedelta(days=20 + i)).isoformat())
        for i in range(3)
    ]
    gate, val_obj = gate_val(cpr, "mls_agent", comps, BlastTier.T2)
    assert gate.verdict == "pass", gate.reason
    attach_valuation(cpr, val_obj, gate)
    return cpr


# Median of the synthesized comp set's adjusted prices (301k/311k/321k).
BATCH_ARV = 311_000.0


def test_batch_price_prices_only_valued_cprs():
    from src.pricing import BATCH_ROW_FIELDS, batch_price

    alive = _valued_cpr("B1")
    dead = _valued_cpr("D1")
    dead.add_lien(LienRecord(lien_type="irs_federal", amount=500_000,
                             survives_foreclosure=True), "test")
    never_valued = CPR(parcel_id="S1", county="collin")  # no valuation at all
    hitl_only = _valued_cpr("H1")
    hitl_only.valuation.gate_val_verdict = "hitl"  # GATE-VAL did NOT pass

    rows = batch_price([alive, never_valued, hitl_only, dead])
    # Non-valued CPRs are skipped outright — no rows, no fabricated numbers.
    assert [r["parcel_id"] for r in rows] == ["B1", "D1"]
    assert all(tuple(r.keys()) == BATCH_ROW_FIELDS for r in rows)

    b1 = rows[0]
    assert b1["mao"] == pytest.approx(BATCH_ARV * 0.70)  # rehab defaults to 0
    assert b1["bid_ceiling"] == pytest.approx(b1["mao"])  # no liens, inf cash
    assert b1["dead_reason"] is None
    assert b1["offer"] is not None
    assert b1["pencil_status"] == PENCIL_BASE

    d1 = rows[1]
    assert d1["bid_ceiling"] == 0
    assert d1["offer"] is None
    assert d1["dead_reason"] is not None


def test_batch_price_skips_rejected_comp_source_despite_pass_verdict():
    """F5-lite fail-closed: 'pass' riding rejected provenance is not priced."""
    from src.pricing import batch_price

    cpr = _valued_cpr("BADSRC")
    cpr.valuation.comp_source = "zestimate"  # rejected fake-signal source
    assert batch_price([cpr]) == []


def test_batch_price_falls_back_to_comp_source_default():
    """F5-lite: an unrecorded source assumes comp_source_default (approved)."""
    from src.pricing import BATCH_ROW_FIELDS, batch_price

    cpr = _valued_cpr("NOSRC")
    cpr.valuation.comp_source = ""  # e.g. partially rehydrated record
    rows = batch_price([cpr], comp_source_default="mls_agent")
    assert [r["parcel_id"] for r in rows] == ["NOSRC"]
    assert all(tuple(r.keys()) == BATCH_ROW_FIELDS for r in rows)
    assert rows[0]["mao"] == pytest.approx(BATCH_ARV * 0.70)


def test_batch_price_respects_per_cpr_deal_type_for_lien_survival():
    """M6 threading: a DT-TAXDEED-valued CPR ignores foreclosure-only liens."""
    from src.pricing import batch_price

    cpr = _valued_cpr("TD1", county="dallas")
    cpr.deal_type = "DT-TAXDEED"
    cpr.add_lien(LienRecord(lien_type="irs_federal", amount=500_000,
                            survives_foreclosure=True,
                            survives_tax_sale=False), "test")

    (row,) = batch_price([cpr])
    assert row["bid_ceiling"] > 0 and row["dead_reason"] is None


def test_to_csv_renders_header_and_rows_with_empty_dead_cells():
    import csv as csv_mod
    import io as io_mod

    from src.pricing import (BATCH_ROW_FIELDS,
                             DEAD_BID_CEILING_NONPOSITIVE,
                             batch_price,
                             to_csv)

    alive = _valued_cpr("C1")
    dead = _valued_cpr("C2")
    dead.add_lien(LienRecord(lien_type="irs_federal", amount=900_000,
                             survives_foreclosure=True), "test")
    rows = batch_price([alive, dead])

    text = to_csv(rows)
    lines = list(csv_mod.reader(io_mod.StringIO(text)))
    assert lines[0] == list(BATCH_ROW_FIELDS)
    assert len(lines) == len(rows) + 1
    by_parcel = {r[0]: r for r in lines[1:]}

    alive_row = by_parcel["C1"]
    assert float(alive_row[BATCH_ROW_FIELDS.index("mao")]) == pytest.approx(
        BATCH_ARV * 0.70)

    dead_row = by_parcel["C2"]
    assert dead_row[BATCH_ROW_FIELDS.index("offer")] == ""  # None -> empty cell
    assert float(dead_row[BATCH_ROW_FIELDS.index("bid_ceiling")]) == 0.0
    assert (dead_row[BATCH_ROW_FIELDS.index("dead_reason")]
            == DEAD_BID_CEILING_NONPOSITIVE)


def test_to_csv_empty_rows_yields_header_only():
    from src.pricing import BATCH_ROW_FIELDS, to_csv

    text = to_csv([])
    assert text.strip() == ",".join(BATCH_ROW_FIELDS)
    assert to_csv(None) == text
