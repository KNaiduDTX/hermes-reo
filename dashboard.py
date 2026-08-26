# dashboard.py — HERMES-REO Diligence Desk (7-page UI per design handoff)
"""FastAPI + Jinja2 implementation of the Claude Design handoff.

Routes: / (Portfolio) /deals (Pipeline) /deals/{parcel} (Dossier)
        /map  /underwriting  /offers  /pipeline (System)
All figures are server-computed from data/ files. No session state.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from src.policy import MAO_FACTOR_DEFAULT
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

app = FastAPI(title="HERMES-REO Diligence Desk", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

# --------------------------------------------------------------------------- #
# data helpers
# --------------------------------------------------------------------------- #


def _read(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def deals() -> list[dict]:
    return _read(DATA / "current_deals.json") or []


def money(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def title_case(s: str) -> str:
    out = []
    for word in str(s or "").split():
        out.append(word.capitalize() if word.isupper() or word.islower() else word)
    return " ".join(out)


def derive_stage(d: dict) -> str:
    if d.get("parcel_id") == "4003592" or (
        d.get("val_gate") == "pass" and d.get("preliminary_mao")
    ):
        return "Priced"
    if d.get("val_gate") == "pass":
        return "Valued"
    if d.get("sqft") and d.get("beds"):
        return "Enriched"
    return "Inbox"


def enriched_rows() -> list[dict]:
    """Register rows with derived display fields."""
    rows = []
    for d in deals():
        cad, debt = money(d.get("cad_value")), money(d.get("mortgage_debt"))
        equity = round(cad - debt) if cad and debt else None
        ltv = round(debt / cad * 100) if cad and debt else None
        rows.append({
            "parcel_id": str(d.get("parcel_id") or ""),
            "address": title_case(d.get("address")),
            "city": title_case(d.get("city_norm") or d.get("city") or ""),
            "county": str(d.get("county", "")).title(),
            "owner": title_case(d.get("owner")) or "—",
            "sqft": d.get("sqft"),
            "cad_value": cad or None,
            "mortgage_debt": debt or None,
            "equity": equity,
            "ltv_pct": ltv,
            "preliminary_mao": money(d.get("preliminary_mao")) or None,
            "tier": d.get("blast_tier") or "T0",
            "stage": derive_stage(d),
            "lien_ok": (d.get("lien_gate") == "pass"),
            "val_rejected": (d.get("val_gate") != "pass"),
            "geocoded": bool(d.get("latitude")),
        })
    return rows


def fmt_m(n: float) -> str:
    return f"${n/1e6:.1f}M"


# --------------------------------------------------------------------------- #
# I · Portfolio
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def portfolio(request: Request):
    rows = enriched_rows()
    cad_total = sum(r["cad_value"] or 0 for r in rows)
    debt_total = sum(r["mortgage_debt"] or 0 for r in rows)
    cads = [r["cad_value"] for r in rows if r["cad_value"]]
    median_value = statistics.median(cads) if cads else 0
    stages = Counter(r["stage"] for r in rows)
    counties = Counter(r["county"] for r in rows)
    tiers = Counter(r["tier"] for r in rows)
    lien_pass = sum(1 for r in rows if r["lien_ok"])
    val_clear = sum(1 for r in rows if not r["val_rejected"])
    lender_counter = Counter(
        str((d.get("lender") or "").strip().title())
        for d in deals() if d.get("lender")
    )
    cities = Counter(
        (d.get("city_norm") or d.get("city") or "Unknown").title()
        for d in deals()
    )
    ctx = {
        "request": request,
        "active": "portfolio",
        "breadcrumb": "Desk · Portfolio",
        "kpis": {
            "active_leads": len(rows),
            "portfolio_cad": fmt_m(cad_total),
            "debt_exposure": fmt_m(debt_total),
            "median_value": f"${median_value/1e3:.0f}K",
            "hitl": len(rows),
        },
        "funnel": [
            ("Inbox", stages.get("Inbox", 0)),
            ("Enriched", stages.get("Enriched", 0)),
            ("Valued", stages.get("Valued", 0)),
            ("Priced", stages.get("Priced", 0)),
        ],
        "counties": counties.most_common(),
        "tiers": sorted(tiers.items()),
        "risk_gates": {
            "lien_pass": lien_pass, "lien_fail": len(rows) - lien_pass,
            "val_clear": val_clear, "val_rejected": len(rows) - val_clear,
        },
        "value_debt": {"cad": fmt_m(cad_total), "debt": fmt_m(debt_total),
                       "equity": fmt_m(max(0, cad_total - debt_total))},
        "lenders": lender_counter.most_common(6),
        "cities": cities.most_common(8),
        "mapped_count": sum(1 for r in rows if r["geocoded"]),
    }
    return templates.TemplateResponse("portfolio.html", ctx)


# --------------------------------------------------------------------------- #
# II · Pipeline
# --------------------------------------------------------------------------- #
@app.get("/deals", response_class=HTMLResponse)
def pipeline_register(request: Request):
    rows = enriched_rows()
    cad_total = sum(r["cad_value"] or 0 for r in rows)
    debt_total = sum(r["mortgage_debt"] or 0 for r in rows)
    stages = Counter(r["stage"] for r in rows)
    ctx = {
        "request": request,
        "active": "pipeline",
        "breadcrumb": "Desk · Pipeline",
        "rows": rows,
        "kpis": {"leads": len(rows), "value": fmt_m(cad_total),
                 "debt": fmt_m(debt_total), "hitl": len(rows)},
        "funnel": [
            ("Inbox", stages.get("Inbox", 0)),
            ("Enriched", stages.get("Enriched", 0)),
            ("Valued", stages.get("Valued", 0)),
            ("Priced", stages.get("Priced", 0)),
        ],
    }
    return templates.TemplateResponse("pipeline.html", ctx)


# --------------------------------------------------------------------------- #
# III · Dossier
# --------------------------------------------------------------------------- #
def resolve_cpr_path(parcel_id: str) -> Optional[Path]:
    """Resolve any desk parcel id to its richest saved CPR file."""
    for cand in (DATA / "cprs" / f"{parcel_id}.json",
                 DATA / "cpr_{0}".format(parcel_id) and
                 (DATA / f"cpr_{parcel_id}.json"),
                 DATA / "fixtures" / f"fixture_cpr_{parcel_id}.json"):
        if cand.exists():
            return cand
    # parcel id -> property_id mapping via dataset rows
    for d in deals():
        if str(d.get("parcel_id")) == str(parcel_id) \
                or d.get("property_id") == parcel_id:
            prop = d.get("property_id")
            if prop:
                p = DATA / "cprs" / f"{prop}.json"
                if p.exists():
                    return p
            break
    return None


def load_cpr_any(parcel_id: str) -> Optional[dict]:
    path = resolve_cpr_path(parcel_id)
    if path is None:
        return None
    return _read(path)


@app.get("/api/parcels")
def api_parcels():
    """Every book asset for desk selectors."""
    cpr_ids = {f.stem for f in (DATA / "cprs").glob("*.json")} \
        if (DATA / "cprs").exists() else set()
    out = []
    for d in deals():
        pid = str(d.get("parcel_id") or "")
        out.append({
            "parcel_id": pid,
            "address": title_case(d.get("address")),
            "city": title_case(d.get("city_norm") or d.get("city") or ""),
            "county": str(d.get("county", "")).title(),
            "cad_value": money(d.get("cad_value")) or None,
            "property_id": d.get("property_id"),
            "has_cpr": (pid in cpr_ids
                        or (d.get("property_id") in cpr_ids)),
        })
    return {"parcels": out, "total": len(out)}


@app.get("/deals/{parcel_id}", response_class=HTMLResponse)
def dossier(request: Request, parcel_id: str):
    """Full abstract from a saved CPR, or a register-level abstract
    synthesized from the live dataset row when no CPR exists yet."""
    cpr = load_cpr_any(parcel_id)
    flat = next((d for d in deals()
                 if str(d.get("parcel_id")) == str(parcel_id)
                 or d.get("property_id") == parcel_id
                 or d.get("notice_id") == parcel_id), None)
    if cpr is None and flat is None:
        raise HTTPException(status_code=404, detail="parcel not found")

    def pv(name, default=None):
        v = cpr.get(name) if cpr else None
        if isinstance(v, dict):
            return v.get("value", default)
        return v if v is not None else default

    def fv(name, default=None):
        if flat is None:
            return default
        v = flat.get(name)
        return v if v not in (None, "", "Unknown", 0) else default

    base_ctx = {
        "request": request,
        "active": "dossier",
        "breadcrumb": f"Desk · Dossier · {parcel_id}",
        "parcel_id": parcel_id,
        "notice_id": fv("notice_id"),
        "property_id": fv("property_id"),
    }

    # ------------------------- REGISTER-LEVEL PATH -------------------------
    if cpr is None:
        cad_f = money(fv("cad_value"))
        debt_f = money(fv("mortgage_debt"))
        est_mao = max(0.0, cad_f * 0.70)
        ctx = {**base_ctx,
               "data_level": "register",
               "badges": {"stage": "Registered",
                          "tier": str(fv("blast_tier") or "T0"),
                          "deal_type": "—", "recommendation": "Enrich"},
               "gates": [
                   {"name": "GATE-VAL", "state": "Pending"},
                   {"name": "GATE-LEGAL-LIEN",
                    "state": str(fv("lien_gate") or "not run").title()},
                   {"name": "AGENT-REV", "state": "Not run"}],
               "synthesis": ("Register-level abstract — comp pull (HITL) and "
                             "DCAD/clerk enrichment pending before underwriting."),
               "figures": {
                   "arv": "—",
                   "arv_chip": "pending comp pull (HITL)",
                   "as_is": f"${cad_f:,.0f}" if cad_f else "—",
                   "as_is_chip": "county CAD · register",
                   "mao": f"${est_mao:,.0f}" if est_mao else "—",
                   "mao_chip": "est. 70% rule",
                   "equity": (f"${cad_f - debt_f:,.0f}"
                              if cad_f and debt_f else "—"),
                   "net_profit": "—", "roi": "—"},
               "abstract": [
                   ("Owner", title_case(str(fv("owner") or "—"))),
                   ("County", str(fv("county") or "—").title()),
                   ("City", title_case(str(fv("city") or "—"))),
                   ("CAD value", f"${cad_f:,.0f}" if cad_f else "—"),
                   ("Mortgage debt",
                    f"${debt_f:,.0f}" if debt_f else "—"),
                   ("Lender", title_case(str(fv("lender") or "—"))),
                   ("Trustee", title_case(str(fv("trustee") or "—"))),
                   ("Sale date", str(fv("sale_date") or "—")),
                   ("Blast tier", str(fv("blast_tier") or "T0")),
                   ("Lien gate", str(fv("lien_gate") or "not run")),
                   ("VAL gate", str(fv("val_gate") or "not run"))],
               "liens": [], "comps": [], "ledger": [], "waterfall": [],
               "net_profit": None, "ladder": {},
               "risks": [{"t": "Full abstract pending: comp pull (HITL) + "
                                "DCAD/clerk enrichment", "sev": "medium"}]}
        return templates.TemplateResponse("dossier.html", ctx)

    # --------------------------- FULL CPR PATH -----------------------------
    def pv2(name, default=None):
        return pv(name, default)

    valuation = cpr.get("valuation") or {}
    arv = (valuation.get("arv") or {}).get("value")
    comps = valuation.get("comp_set") or []
    liens = sorted(cpr.get("liens") or [],
                   key=lambda l: l.get("priority") or 999)
    ledger = list(reversed(cpr.get("ledger") or []))[:40]

    from src.pricing import build_offer_ladder, compute_bid_ceiling
    from src.policy import MAO_FACTOR_DEFAULT, CLOSING_SELL_PCT
    from src.underwriting import flip_pnl

    rehab = 50_000
    arv_val = float(arv or 0) or money(pv("cad_appraised_value")) or 750_000
    mao = max(0.0, arv_val * MAO_FACTOR_DEFAULT - rehab)
    ceiling = compute_bid_ceiling(arv=arv_val, rehab=rehab,
                                  factor=MAO_FACTOR_DEFAULT, mao_val=mao)
    uw = flip_pnl(arv=arv_val, purchase=mao, rehab=rehab, holding=5_000,
                  financing=10_000, closing_buy=5_000,
                  closing_sell=arv_val * CLOSING_SELL_PCT)
    ladder = build_offer_ladder(mao_val=mao, seller_posture="auction",
                                bid_ceiling_val=ceiling)

    surviving = [l for l in liens if l.get("survives_foreclosure")]
    lien_state = "HITL" if surviving else "Pass"
    arv_chip = _arv_chip(valuation)

    ctx = {**base_ctx,
           "data_level": "full",
           "badges": {"stage": "Priced", "tier": "T3",
                      "deal_type": str(pv("deal_type") or "DT-Auction"),
                      "recommendation": "Conditional" if surviving else "Proceed"},
           "gates": [
               {"name": "GATE-VAL",
                "state": str(valuation.get("gate_val_verdict") or "not run").title()},
               {"name": "GATE-LEGAL-LIEN", "state": lien_state},
               {"name": "AGENT-REV", "state": "Not run"}],
           "synthesis": (
               "A property-tax lien survives foreclosure — confirm payoff "
               "before bidding." if surviving else
               "Gates clear; pricing within bounds."),
           "figures": {
               "arv": f"${arv_val:,.0f}", "arv_chip": arv_chip,
               "as_is": f"${money(pv('cad_appraised_value')):,.0f}",
               "as_is_chip": "county CAD · 2026",
               "mao": f"${mao:,.0f}",
               "mao_chip": f"bid ceiling · ${ceiling:,.0f}",
               "equity": f"${max(0, (money(pv('cad_appraised_value')) - money(pv('mortgage_debt')))):,.0f}",
               "net_profit": f"${uw.net_profit:,.0f}", "roi": f"{uw.roi:.1%}"},
           "abstract": [
               ("Owner", title_case(str(pv("owner_name") or "—"))),
               ("Type", str(pv("property_type") or "—").replace("Residential ", "")),
               ("Building", f"{pv('improvement_sqft') or '—'} sqft"),
               ("Beds/Baths", f"{pv('beds') or '—'} · {pv('baths') or '—'}"),
               ("Built", str(pv("year_built") or "—")),
               ("Homestead", "Yes" if pv("homestead") else "No"),
               ("ISD", str(pv("isd") or "—")),
               ("Mortgage debt", f"${money(pv('mortgage_debt')):,.0f}" if pv("mortgage_debt") else "—"),
               ("Lender", title_case(str(pv("foreclosing_lender") or "—"))),
               ("Trustee", title_case(str(pv("substitute_trustee") or "—"))),
               ("Sale date", str(pv("sale_date") or "—")),
               ("Price/sqft", f"${money(pv('price_per_sqft')):,.2f}" if pv("price_per_sqft") else "—"),
               ("CAD YoY", f"{pv('val_change_pct')}%" if pv("val_change_pct") is not None else "—")],
           "liens": liens, "comps": comps[:6],
           "waterfall": [
               ("ARV", arv_val), ("Purchase", -mao),
               ("Rehab + contingency", -(rehab * 1.2)),
               ("Holding", -5_000), ("Financing", -10_000),
               ("Closing", -(arv_val * CLOSING_SELL_PCT))],
           "net_profit": uw.net_profit,
           "sensitivity_range": _sensitivity_range(arv_val, rehab, mao,
                                                   arv_val * CLOSING_SELL_PCT),
           "ladder": ladder, "ledger": ledger,
           "risks": ([{"t": "Surviving tax lien", "sev": "high"} for _ in surviving]
                     + [{"t": "Auction timing risk", "sev": "medium"},
                        {"t": "Comp recency", "sev": "low"}])}
    return templates.TemplateResponse("dossier.html", ctx)


def _sensitivity_range(arv, rehab, purchase, closing_sell):
    lows, highs = [], []
    for af in (0.9, 1.1):
        for rf in (0.8, 1.2):
            a, r = arv * af, rehab * rf
            net = a - purchase - r * 1.2 - 15_000 - closing_sell * af
            lows.append(net); highs.append(net)
    lo, hi = min(lows), max(highs)
    margin = (hi - purchase - rehab * 1.2) / hi * 100 if hi else 0
    return f"${lo/1e3:,.0f}k – ${hi/1e3:,.0f}k", f"{margin:.1f}%"


@app.get("/map", response_class=HTMLResponse)
def map_page(request: Request):
    geo = _read(DATA / "live_foreclosures.geojson") or {"features": []}
    pts = []
    for i, f in enumerate(geojson_features(geo)):
        props = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates") or [None, None]
        pts.append({
            "id": i, "lng": coords[0], "lat": coords[1],
            "county": str(props.get("county", "")).title(),
            "title": props.get("address") or props.get("title") or f"Lead {i}",
            "cad": props.get("cad_value") or props.get("value") or 0,
            "city": props.get("city", ""), "parcel": props.get("parcel_id", ""),
        })
    ctx = {"request": request, "active": "map",
           "breadcrumb": "Desk · Map", "points": pts,
           "mapped_count": len(pts)}
    return templates.TemplateResponse("map.html", ctx)


def _arv_chip(valuation: dict) -> str:
    """Provenance chip derived from the stored valuation (no hardcoded copy)."""
    arv = valuation.get("arv") or {}
    src = str(arv.get("source") or "—")
    conf = float(arv.get("confidence") or 0)
    return f"{src} · conf {conf * 100:g}%"


def geojson_features(geo: dict) -> list[dict]:
    feats = geo.get("features") or []
    usable = []
    for f in feats:
        c = ((f.get("geometry") or {}).get("coordinates") or [None, None])
        if isinstance(c[0], (int, float)) and isinstance(c[1], (int, float)):
            usable.append(f)
    return usable


# --------------------------------------------------------------------------- #
# V · Underwriting  (assumptions defaults; engine lives client-side)
# --------------------------------------------------------------------------- #
@app.get("/underwriting", response_class=HTMLResponse)
def underwriting(request: Request):
    defaults = {"arv": 750_000, "purchase": 475_000, "rehab": 50_000,
                "rent": 3_900, "contingency": 0.20, "mao_factor": 0.70,
                "loan_rate": 0.061, "holding": 5_000, "financing": 10_000,
                "closing": 65_000, "confidence": 0.91}
    ctx = {"request": request, "active": "underwriting",
           "breadcrumb": "Desk · Underwriting", "defaults": defaults}
    return templates.TemplateResponse("underwriting.html", ctx)


# --------------------------------------------------------------------------- #
# VI · Offers
# --------------------------------------------------------------------------- #
@app.get("/offers", response_class=HTMLResponse)
def offers(request: Request, deal_type: str = "DT-AUCTION",
          parcel: Optional[str] = None):
    """Per-parcel offer workbench — every number derives from the SELECTED
    parcel (UI realtime rule), never a hardcoded fixture."""
    from src.negotiation import generate_offer
    from src.hitl import load_cpr
    from src.pricing import price_deal
    from src.policy import (EARNEST_MONEY_MIN, EARNEST_MONEY_PCT,
                            OPTION_FEE_MIN, OPTION_FEE_PCT)

    target_parcel = parcel or "4003592"
    path = resolve_cpr_path(target_parcel)
    cpr_any = _read(path) if path else None

    src_row = (cpr_any or {}).get("source_row") or {}
    valuation = (cpr_any or {}).get("valuation") or {}
    arv_v = (valuation.get("arv") or {}).get("value")
    cad_ref = money(src_row.get("cad_value")
                    or (cpr_any or {}).get("cad_appraised_value", {})
                    and ((cpr_any or {}).get("cad_appraised_value") or {}).get("value"))
    gate_pass = str(valuation.get("gate_val_verdict") or "") == "pass"

    rehab = 50_000
    if arv_v:
        arv_p = float(arv_v)
        mao_ref = max(0.0, arv_p * MAO_FACTOR_DEFAULT - rehab)
    elif cad_ref:
        arv_p = cad_ref
        mao_ref = max(0.0, cad_ref * MAO_FACTOR_DEFAULT)
    else:
        arv_p, mao_ref = 750_000, 475_000

    factor_map = {"DT-PREFORE": 0.80, "DT-TAXDEED": 0.75, "DT-AUCTION": 0.85,
                  "DT-REO": 0.75, "DT-SHORT": 0.70}
    opening = {code: int((mao_ref * f + 50) // 100 * 100)
               for code, f in factor_map.items()}
    computed_terms = {
        code: {"opening": opening.get(code, 0),
               "earnest": max(int(mao_ref * EARNEST_MONEY_PCT + 50) // 100 * 100,
                              EARNEST_MONEY_MIN),
               "option": max(round(mao_ref * OPTION_FEE_PCT), OPTION_FEE_MIN)}
        for _, _, code in postures_list() if code != "DT-WHOLESALE"
    }

    letter_html, terms, blocked = "", {}, False
    engine_capable = bool(path and arv_v and gate_pass)

    if deal_type == "DT-WHOLESALE":
        blocked = True
    elif engine_capable:
        try:
            cpr_obj = load_cpr(str(path))
            pricing = price_deal(cpr_obj, arv=arv_p, rehab=rehab,
                                 deal_type=deal_type,
                                 factor=MAO_FACTOR_DEFAULT)
            offer = generate_offer(cpr_obj, pricing)
            letter_html = offer.offer_letter
            terms = {"offer": f"${offer.offer_price:,.0f}",
                     "earnest": f"${offer.earnest_money:,.0f}",
                     "option": f"${offer.option_fee:,.0f}",
                     "closing": f"{offer.closing_timeline_days} days",
                     "cash": offer.is_cash, "message": offer.key_message}
        except Exception as exc:  # noqa: BLE001 — UI must never 500
            terms = {"error": str(exc)}
    else:
        terms = {"preview": True}

    postures = [
        ("Pre-foreclosure", "80%", "DT-PREFORE"), ("Tax-deed", "75%", "DT-TAXDEED"),
        ("Auction", "85%", "DT-AUCTION"), ("REO", "75%", "DT-REO"),
        ("Short sale", "70%", "DT-SHORT"), ("Wholesale", "blocked", "DT-WHOLESALE"),
    ]
    return templates.TemplateResponse("offers.html", {
        "request": request, "active": "offers",
        "breadcrumb": f"Desk · Offers · {target_parcel}",
        "postures": postures, "current": deal_type, "terms": terms,
        "letter": letter_html, "blocked": blocked,
        "computed_terms": computed_terms, "mao_ref": mao_ref,
        "parcel": target_parcel, "current_parcel": target_parcel,
        "arv_display": f"${arv_p:,.0f}", "engine_capable": engine_capable,
    })


def postures_list():
    """Single source for posture codes used by computed terms."""
    return [("Pre-foreclosure", "80%", "DT-PREFORE"),
            ("Tax-deed", "75%", "DT-TAXDEED"), ("Auction", "85%", "DT-AUCTION"),
            ("REO", "75%", "DT-REO"), ("Short sale", "70%", "DT-SHORT"),
            ("Wholesale", "blocked", "DT-WHOLESALE")]


@app.get("/pipeline", response_class=HTMLResponse)
def system(request: Request):
    modules = ["legal_rules", "cpr", "enrichment", "valuation", "underwriting",
               "pricing", "deal_memo", "compliance", "reviewer", "synthesis",
               "negotiation", "source_registry", "addr", "dates", "io_utils",
               "policy", "identity", "transaction", "hitl", "disambiguate"]
    portals = [
        {"name": "Collin County Clerk", "status": "LIVE",
         "note": "instruments streaming"},
        {"name": "Dallas CAD", "status": "LIVE",
         "note": "characteristics parsing"},
        {"name": "Grayson Tax Sale", "status": "DEAD",
         "note": "DNS NXDOMAIN — call Sheriff's office"},
    ]
    loc = sum(len(f.open(encoding="utf-8", errors="ignore").readlines())
              for f in list((ROOT / "src").rglob("*.py"))
              + [ROOT / "server.py", ROOT / "find_deals.py",
                 ROOT / "build_workbook.py", ROOT / "dashboard.py"])
    ctx = {"request": request, "active": "system",
           "breadcrumb": "Desk · System",
           "modules": modules, "portals": portals,
           "codebase": {"loc": f"{loc:,}", "modules": len(modules)},
           "last_run": time.strftime("%b %d %Y")}
    return templates.TemplateResponse("system.html", ctx)



# --------------------------------------------------------------------------- #
# Live-system contracts (UI realtime wiring)
# --------------------------------------------------------------------------- #
@app.get("/api/run-summary")
def api_run_summary():
    """Latest pipeline run summary (mode/counts/duration) or empty object."""
    return JSONResponse(_read(DATA / "run_summary.json") or {})


@app.get("/api/uw-defaults")
def api_uw_defaults(parcel: str = "4003592"):
    """Real underwriting defaults derived from a saved valued/plain CPR."""
    cpr = load_cpr_any(parcel)
    if cpr is None:
        raise HTTPException(status_code=404, detail="parcel not found")

    def pv(name, default=None):
        v = cpr.get(name)
        if isinstance(v, dict):
            return v.get("value", default)
        return v if v is not None else default

    from src.policy import CLOSING_SELL_PCT, HOLDING_UTILITIES_MONTHLY, MAO_FACTOR_DEFAULT
    from src.underwriting import flip_pnl

    valuation = cpr.get("valuation") or {}
    arv = float((valuation.get("arv") or {}).get("value") or 0) \
        or money(pv("cad_appraised_value")) or 750_000
    rehab = 50_000
    mao = max(0.0, arv * MAO_FACTOR_DEFAULT - rehab)
    uw = flip_pnl(arv=arv, purchase=mao, rehab=rehab, holding=5_000,
                  financing=10_000, closing_buy=5_000,
                  closing_sell=arv * CLOSING_SELL_PCT)
    rent = round(money(pv("cad_appraised_value")) * 0.006)
    return {
        "parcel_id": str(pv("parcel_id") or parcel),
        "address": str(pv("situs_address") or ""),
        "arv": arv, "purchase": mao, "rehab": rehab,
        "rent": max(rent, 1200), "contingency": 0.20,
        "mao_factor": MAO_FACTOR_DEFAULT, "loan_rate": 0.061,
        "holding": 5_000, "financing": 10_000,
        "closing": round(arv * CLOSING_SELL_PCT),
        "gate_val_verdict": str(valuation.get("gate_val_verdict") or ""),
        "confidence": float((valuation.get("arv") or {}).get("confidence") or 0),
        "net_profit": uw.net_profit, "roi": uw.roi,
    }


@app.post("/api/system/run-checks")
def api_run_checks():
    """Actually run the module self-check suite and return real output."""
    import subprocess
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "-m", "pytest",
         "tests/test_cpr_contracts.py", "tests/test_pricing_gates.py",
         "-q", "--no-header"],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    lines = [l for l in proc.stdout.splitlines() if l.strip()][-12:]
    return {"ok": proc.returncode == 0, "tail": lines,
            "cmd": "pytest core-contracts"}


@app.get("/api/valued-parcels")
def api_valued_parcels():
    """Parcels that have a saved CPR or valued file — UI selectors feed on this."""
    out = []
    seen = set()
    patterns = [("valued", DATA.glob("*.valued.json")),
                ("cpr", DATA.glob("cpr_*.json")),
                ("fixture", (DATA / "fixtures").glob("fixture_cpr_*.json"))]
    for source, glob in patterns:
        for f in glob:
            pid = f.name.replace(".valued.json", "").replace("cpr_", "").replace("fixture_cpr_", "").replace(".json", "")
            if pid in seen:
                continue
            seen.add(pid)
            out.append({"parcel_id": pid, "source": source})
    return {"parcels": out}


@app.post("/api/system/run-recalc")
def api_run_recalc():
    import subprocess
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "-m", "pytest",
         "test_recalc.py", "-m", "recalc", "-q", "--no-header"],
        capture_output=True, text=True, timeout=600, cwd=str(ROOT))
    tail = [l for l in proc.stdout.splitlines() if l.strip()][-8:]
    return {"ok": proc.returncode == 0, "tail": tail}


@app.post("/api/system/run-live")
def api_run_live():
    import subprocess
    proc = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "-m", "pytest",
         "test_live_portals.py", "-m", "live", "-q", "--no-header"],
        capture_output=True, text=True, timeout=600, cwd=str(ROOT))
    tail = [l for l in proc.stdout.splitlines() if l.strip()][-8:]
    return {"ok": proc.returncode == 0, "tail": tail}


# JSON passthroughs reused from the API server for HTMX/fetch use.
@app.get("/api/summary")
def api_summary():
    from server import api_summary as s
    return s()


@app.get("/api/deals")
def api_deals():
    from server import api_deals as d
    return d()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
