"""Deal-memo recommendation gate tests (audit C3 + surviving-lien follow-up)."""
from src.cpr import BlastTier, CPR, CompRecord, LienRecord
from src.deal_memo import recommend_deal, render_memo
from src.pricing import price_deal
from src.underwriting import flip_pnl


def _winning_cpr():
    cpr = CPR(parcel_id="P", county="collin", situs_address="1 Main St")
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=20)).isoformat()
    comps = [CompRecord(address=f"{i} X St", sale_price=750_000,
                        sale_date=recent, distance_miles=0.2,
                        source="mls_agent", adjusted_price=750_000)
             for i in range(5)]
    from src.valuation import attach_valuation, gate_val
    gate, val = gate_val(cpr, "mls_agent", comps, BlastTier.T2)
    cpr = attach_valuation(cpr, val, gate)
    return cpr


def _pricing(arv=750_000):
    return price_deal(_winning_cpr(), arv=arv, rehab=50_000,
                      deal_type="DT-FLIP", factor=0.70)


def test_losing_deal_never_proceeds():
    """AUDIT C3: a deal losing $80k used to render PROCEED."""
    cpr = _winning_cpr()
    uw = flip_pnl(arv=400_000, purchase=380_000, rehab=100_000,
                  holding=10_000, financing=5_000, closing_buy=5_000,
                  closing_sell=32_000)
    pricing = _pricing()
    verdict, blockers, _ = recommend_deal(cpr, uw, pricing, review=None)
    assert verdict == "DO NOT PROCEED"
    assert any("net profit" in b.lower() for b in blockers)


def test_surviving_tax_lien_caps_at_conditional():
    """A surviving tax lien must downgrade PROCEED even on a perfect deal."""
    cpr = _winning_cpr()
    cpr.lien_lookup_status = "ok"
    cpr.add_lien(LienRecord(lien_type="tax", amount=9_412.0,
                            recording_date="11/11/2014",
                            survives_foreclosure=True), "test")
    uw = flip_pnl(arv=700_000, purchase=400_000, rehab=50_000,
                  holding=5_000, financing=10_000, closing_buy=5_000,
                  closing_sell=56_000)
    pricing = _pricing(arv=700_000)
    verdict, _, cautions = recommend_deal(cpr, uw, pricing, review=None)
    assert verdict == "CONDITIONAL"
    assert any("surviving" in c_.lower() for c_ in cautions)


def test_errored_lookup_blocks_proceed():
    cpr = _winning_cpr()
    cpr.lien_lookup_status = "error"
    uw = flip_pnl(arv=700_000, purchase=400_000, rehab=50_000,
                  holding=5_000, financing=10_000, closing_buy=5_000,
                  closing_sell=56_000)
    pricing = _pricing(arv=700_000)
    verdict, blockers, _ = recommend_deal(cpr, uw, pricing, review=None)
    assert verdict == "DO NOT PROCEED"


def test_memo_renders_recommendation():
    cpr = _winning_cpr()
    uw = flip_pnl(arv=700_000, purchase=400_000, rehab=50_000,
                  holding=5_000, financing=10_000, closing_buy=5_000,
                  closing_sell=56_000)
    memo = render_memo(cpr, uw, _pricing(arv=700_000), review=None)
    assert "## Recommendation" in memo
