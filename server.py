# server.py
"""HERMES-REO dashboard server — read APIs, deal detail, and draft actions.

A single FastAPI app serving the static dark-theme dashboard under ``static/``
plus thin JSON APIs over the existing pipeline data files:

| Route                      | Serves                                                        |
|----------------------------|---------------------------------------------------------------|
| ``GET /``                  | ``static/index.html`` (portfolio overview UI)                 |
| ``GET /api/deals``         | ``data/current_deals.json`` (fresh read per request)          |
| ``GET /api/geojson``       | ``data/live_foreclosures.geojson`` (fresh read per request)   |
| ``GET /api/stats``         | portfolio stats via ``src.synthesis`` over enriched CPRs      |
| ``GET /api/summary``       | small KPI aggregate {total, counties, with_sqft, ...}         |
| ``GET /api/cpr/{parcel}``  | saved CPR snapshot (``data/cpr_{p}.json``, then fixture)      |
| ``GET /api/memo/{parcel}`` | deal memo markdown (``data/deal_memo_{p}.md``, then fixture)  |
| ``POST /api/generate-offer`` | price + draft offer letter → ``data/drafts/`` (draft-only)  |
| ``POST /api/clerk-search`` | LIVE Collin clerk records search (counts + preview)           |
| ``GET /api/market``        | group aggregates (?groupby=zip|city|isd|county)               |

Design constraints (docs/archive/DASHBOARD_PLAN__2026-08-20.md [archived — plan
delivered] + delivery rules):

* All paths are anchored to the repo root via ``Path(__file__).parent`` so the
  server runs identically from any working directory.
* Heavy analysis imports (``src.synthesis`` / ``src.enrichment`` /
  ``src.hitl`` / ``src.negotiation`` / Playwright adapters) happen lazily
  INSIDE their handlers — a broken optional dependency can never prevent
  startup or static serving.
* A missing/unreadable backing data file raises :class:`DataUnavailableError`,
  converted to HTTP 503 with a clear JSON message. A well-formed request for
  an artifact that does not exist is a clean 404 JSON; a malformed request
  (bad parcel id, unknown groupby, empty party name) is a clean 400. Handlers
  never crash.

Run: ``uvicorn server:app`` (offline-safe; no network access at import time).
The two POST actions are the only non-read routes and both preserve the
draft-only posture: generate-offer stages a letter under ``data/drafts/`` for
human review (never sends), clerk-search only reads public county records.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# --- Repo-anchored paths ----------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
STATIC_DIR = REPO_ROOT / "static"
INDEX_PATH = STATIC_DIR / "index.html"
DATA_DIR = REPO_ROOT / "data"
DEALS_PATH = DATA_DIR / "current_deals.json"
GEOJSON_PATH = DATA_DIR / "live_foreclosures.geojson"
DRAFTS_DIR = DATA_DIR / "drafts"

SUMMARY_KEYS = ("total", "counties", "with_sqft", "with_debt", "geocoded")

# Parcel ids are file-name components: lock them to traversal-proof shapes.
# Anything outside [A-Za-z0-9_-] (slashes, dots, whitespace tricks) is a 400,
# never a filesystem lookup.
PARCEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# /api/market grouping dimensions (F11).
MARKET_GROUPBYS = ("zip", "city", "isd", "county")
_ZIP_TAIL_RE = re.compile(r"(\d{5})(?:-\d{4})?\s*$")

# /api/clerk-search response shaping.
CLERK_PREVIEW_LIMIT = 10
CLERK_MAX_PARTY_LEN = 200
CLERK_POLITENESS = (
    "One sequential search per request against collin.tx.publicsearch.us via "
    "headless Chromium (~10-15s); results settle ~8s after submit and "
    "best-effort pagination walks at most MAX_SEARCH_PAGES=20 pages with 1s "
    "pauses. No retries, no parallel requests, no other endpoints hit."
)


class DataUnavailableError(Exception):
    """A route's backing data file is missing or unreadable → HTTP 503."""

    def __init__(self, path: Path, reason: str = "") -> None:
        self.path = path
        self.reason = reason or "file not found"
        super().__init__(f"{path}: {self.reason}")


class ApiError(Exception):
    """A well-formed-but-rejected request → clean JSON 4xx (never a traceback).

    ``error`` is a stable machine code (``invalid_parcel_id``,
    ``invalid_groupby``, ...); ``detail`` is human-readable.
    """

    def __init__(self, status_code: int, error: str, detail: str) -> None:
        self.status_code = status_code
        self.error = error
        self.detail = detail
        super().__init__(f"{status_code} {error}: {detail}")


app = FastAPI(
    title="HERMES-REO Dashboard",
    description=(
        "Portfolio dashboard over HERMES-REO pipeline data "
        "(current_deals.json, live_foreclosures.geojson, src.synthesis) with "
        "deal-detail drilldown, market aggregates, and draft-only actions."
    ),
    version="1.1.0",
)


@app.exception_handler(DataUnavailableError)
async def _data_unavailable_handler(request: Request, exc: DataUnavailableError) -> JSONResponse:
    """Missing/broken data must degrade to a clear 503, never a traceback."""
    return JSONResponse(
        status_code=503,
        content={
            "error": "data_unavailable",
            "detail": f"{exc.path.name}: {exc.reason}",
        },
    )


@app.exception_handler(ApiError)
async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Malformed requests answer with a stable JSON error envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "detail": exc.detail},
    )


if STATIC_DIR.is_dir():  # tolerate a deleted static/ tree at boot
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Helpers -----------------------------------------------------------------
def _read_json(path: Path) -> Any:
    """Fresh-read + parse a JSON data file; raise DataUnavailableError on failure."""
    if not path.exists():
        raise DataUnavailableError(path, "data file not found")
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise DataUnavailableError(path, f"unreadable ({exc.__class__.__name__}: {exc})") from exc


def _load_deals() -> list:
    """Load the flat deal records; enforce the expected list-of-dicts shape."""
    deals = _read_json(DEALS_PATH)
    if not isinstance(deals, list):
        raise DataUnavailableError(DEALS_PATH, "expected a JSON list of deal records")
    return deals


def _as_number(val: Any) -> float | None:
    """Coerce a flat-record value to float; None when missing/non-numeric."""
    if isinstance(val, bool):  # bool is an int subclass — never a quantity
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        cleaned = val.replace("$", "").replace(",", "").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _validated_parcel(parcel_id: str) -> str:
    """Return the trimmed parcel id, or raise 400 for traversal-prone shapes.

    Parcel ids become file-name components (``cpr_{id}.json``), so anything
    outside ``[A-Za-z0-9_-]`` is rejected before any filesystem access.
    """
    pid = (parcel_id or "").strip()
    if not PARCEL_ID_RE.match(pid):
        raise ApiError(
            400,
            "invalid_parcel_id",
            f"parcel id {parcel_id!r} must match {PARCEL_ID_RE.pattern} "
            "(letters, digits, dash, underscore)",
        )
    return pid


def _locate(paths: list[Path]) -> Path | None:
    """First existing path among candidates, else None."""
    for path in paths:
        try:
            if path.exists():
                return path
        except OSError:  # unreadable parent dir — treat as absent
            continue
    return None


def _cpr_paths(parcel_id: str) -> list[Path]:
    """CPR snapshot lookup order: production file, then offline fixture."""
    return [
        DATA_DIR / f"cpr_{parcel_id}.json",
        DATA_DIR / "fixtures" / f"fixture_cpr_{parcel_id}.json",
    ]


def _memo_paths(parcel_id: str) -> list[Path]:
    """Deal-memo lookup order: production file, then offline fixture."""
    return [
        DATA_DIR / f"deal_memo_{parcel_id}.md",
        DATA_DIR / "fixtures" / f"fixture_deal_memo_{parcel_id}.md",
    ]


def _read_text_file(path: Path) -> str:
    """Fresh-read a text artifact; raise DataUnavailableError on I/O failure."""
    if not path.exists():
        raise DataUnavailableError(path, "data file not found")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DataUnavailableError(path, f"unreadable ({exc.__class__.__name__}: {exc})") from exc


def _body_dict(payload: Any) -> dict:
    """The request body when it is a JSON object; {} otherwise.

    Callers turn a missing required key into a clean 400 — FastAPI never
    sees a pydantic validation error for these routes.
    """
    return payload if isinstance(payload, dict) else {}


# --- Static UI ----------------------------------------------------------------
@app.get("/", include_in_schema=False, summary="Serve the dashboard UI")
def index() -> FileResponse:
    if not INDEX_PATH.exists():
        raise DataUnavailableError(INDEX_PATH, "dashboard UI missing (static/index.html)")
    return FileResponse(INDEX_PATH, media_type="text/html")


# --- JSON APIs ----------------------------------------------------------------
@app.get("/api/deals", summary="All current deals (fresh read per request)")
def api_deals() -> JSONResponse:
    return JSONResponse(content=_load_deals())


@app.get("/api/geojson", summary="Geocoded foreclosure notices FeatureCollection")
def api_geojson() -> JSONResponse:
    geo = _read_json(GEOJSON_PATH)
    if not isinstance(geo, dict):
        raise DataUnavailableError(GEOJSON_PATH, "expected a GeoJSON object")
    return JSONResponse(content=geo)


@app.get("/api/stats", summary="Portfolio statistical summary (markdown report)")
def api_stats() -> JSONResponse:
    """Rendered portfolio stats over current_deals records.

    Pipeline: flat records → ``enrich_from_flat_record`` CPRs →
    ``portfolio_stats`` → ``render_stats_report``. Imports are lazy so a
    missing optional dependency degrades to 503 without touching static
    serving or the other endpoints. Individually malformed records are
    skipped (counted in ``skipped``) instead of failing the whole report.
    """
    deals = _load_deals()
    try:
        from src.enrichment import enrich_from_flat_record
        from src.synthesis import portfolio_stats, render_stats_report
    except Exception as exc:  # lazy-import guard: deps must not break serving
        raise DataUnavailableError(
            REPO_ROOT / "src", f"analysis modules unavailable ({exc.__class__.__name__}: {exc})"
        ) from exc

    cprs = []
    skipped = 0
    for record in deals:
        if not isinstance(record, dict):
            skipped += 1
            continue
        try:
            cprs.append(enrich_from_flat_record(record))
        except Exception:  # one bad row must not sink the portfolio report
            skipped += 1

    stats = portfolio_stats(cprs)
    report = render_stats_report(stats)
    return JSONResponse(
        content={"report": report, "stats": stats, "enriched": len(cprs), "skipped": skipped}
    )


@app.get("/api/summary", summary="Small KPI aggregate for the dashboard header")
def api_summary() -> JSONResponse:
    """``{total, counties, with_sqft, with_debt, geocoded}`` (+ provenance hint).

    ``geocoded`` counts deal rows carrying usable latitude+longitude. The
    shipped flat dataset carries no coordinate columns at all (coordinates
    live in live_foreclosures.geojson), so when zero rows are coordinate-
    capable the geocoded feature count from the GeoJSON is reported instead
    and ``geocoded_source`` says which surface produced the number.
    """
    deals = _load_deals()

    counties: dict[str, int] = {}
    with_sqft = 0
    with_debt = 0
    geocoded_rows = 0
    coord_capable = 0

    for record in deals:
        if not isinstance(record, dict):
            continue
        county = str(record.get("county") or "").strip().lower() or "unknown"
        counties[county] = counties.get(county, 0) + 1

        sqft = _as_number(record.get("sqft"))
        if sqft is not None and sqft > 0:
            with_sqft += 1

        debt = _as_number(record.get("mortgage_debt"))
        if debt is not None and debt > 0:
            with_debt += 1

        lat = _as_number(record.get("latitude"))
        lng = _as_number(record.get("longitude"))
        if lat is not None or lng is not None:
            coord_capable += 1
        if lat and lng:
            geocoded_rows += 1

    geocoded = geocoded_rows
    geocoded_source = "deals"
    if coord_capable == 0 and GEOJSON_PATH.exists():
        try:
            geo = _read_json(GEOJSON_PATH)
            features = geo.get("features", []) if isinstance(geo, dict) else []
            geocoded = len(features)
            geocoded_source = "geojson"
        except DataUnavailableError:
            geocoded_source = "unavailable"

    return JSONResponse(
        content={
            "total": len(deals),
            "counties": dict(sorted(counties.items(), key=lambda kv: -kv[1])),
            "with_sqft": with_sqft,
            "with_debt": with_debt,
            "geocoded": geocoded,
            "geocoded_source": geocoded_source,
        }
    )


# --- F3: deal-detail APIs -----------------------------------------------------
@app.get("/api/cpr/{parcel_id}", summary="Saved CPR snapshot for one parcel")
def api_cpr(parcel_id: str) -> JSONResponse:
    """Return the saved CPR dict (``CPR.to_dict()`` shape) for one parcel.

    Lookup order: ``data/cpr_{p}.json`` then
    ``data/fixtures/fixture_cpr_{p}.json``. A well-formed id with no file on
    disk is a clean 404 JSON; a traversal-shaped id is a 400; a file that
    exists but cannot be parsed is 503 via :class:`DataUnavailableError`.
    Provenance-wrapped fields are passed through verbatim as
    ``{value, source, retrieved_at, confidence}`` objects — the UI unwraps.
    """
    pid = _validated_parcel(parcel_id)
    path = _locate(_cpr_paths(pid))
    if path is None:
        raise ApiError(
            404,
            "cpr_not_found",
            f"no CPR snapshot for parcel {pid!r} "
            f"(looked for cpr_{pid}.json and fixtures/fixture_cpr_{pid}.json)",
        )
    cpr = _read_json(path)
    if not isinstance(cpr, dict):
        raise DataUnavailableError(path, "expected a CPR JSON object at top level")
    return JSONResponse(content=cpr)


@app.get("/api/memo/{parcel_id}", summary="Rendered deal-memo markdown for one parcel")
def api_memo(parcel_id: str) -> JSONResponse:
    """Return the raw deal-memo markdown for one parcel.

    Lookup order: ``data/deal_memo_{p}.md`` then
    ``data/fixtures/fixture_deal_memo_{p}.md``. Missing file → 404 JSON;
    unreadable file (permissions, directory-in-place) → 503 via the shared
    handler — this endpoint is 503-safe by construction, never a traceback.
    """
    pid = _validated_parcel(parcel_id)
    path = _locate(_memo_paths(pid))
    if path is None:
        raise ApiError(
            404,
            "memo_not_found",
            f"no deal memo for parcel {pid!r} "
            f"(looked for deal_memo_{pid}.md and fixtures/fixture_deal_memo_{pid}.md)",
        )
    text = _read_text_file(path)
    return JSONResponse(
        content={"parcel_id": pid, "source": path.name, "content": text}
    )


# --- F4: draft actions ----------------------------------------------------------
def _run_clerk_search(party_name: str) -> dict:
    """LIVE Collin County Clerk official-records search (Playwright).

    Isolated in a module-level function so tests can monkeypatch it — importing
    ``src.adapters.collin_clerk`` pulls in Playwright/bs4, which must stay a
    runtime concern, never an import-time one. Returns raw instruments plus
    their lien stack (``build_lien_stack``).
    """
    from src.adapters.collin_clerk import CollinClerkAdapter, build_lien_stack

    adapter = CollinClerkAdapter()
    instruments = list(adapter.fetch(party_name=party_name) or [])
    liens = build_lien_stack(instruments)
    return {"instruments": instruments, "liens": list(liens or [])}


def _instrument_dict(inst: Any) -> dict:
    """Shape a RecordedInstrument-like object for JSON preview (getattr-safe)."""
    return {
        "doc_number": getattr(inst, "doc_number", None),
        "doc_type": getattr(inst, "doc_type", None),
        "recorded_date": getattr(inst, "recorded_date", None),
        "parties": list(getattr(inst, "parties", None) or []),
        "legal_description": getattr(inst, "legal_description", None),
        "book": getattr(inst, "book", None),
        "volume": getattr(inst, "volume", None),
        "page": getattr(inst, "page", None),
    }


def _cpr_plain(val: Any) -> Any:
    """Unwrap a ProvenanceField-like object to its plain value."""
    return getattr(val, "value", val)


def _parties_of_record(cpr: Any) -> list[dict]:
    """Counterparty identities from the saved CPR (owner + lienholders).

    ``generate_offer``'s rendered letter is deliberately template-generic; this
    composes the party names on top so a staged draft identifies who is on the
    other side without anyone reopening the CPR JSON. Never fabricates: absent
    fields are simply omitted.
    """
    parties: list[dict] = []
    owner = _cpr_plain(getattr(cpr, "owner_name", None))
    if owner is not None and str(owner).strip():
        parties.append({"role": "owner_of_record", "name": str(owner).strip()})
    lenders: list[str] = []
    for lien in getattr(cpr, "liens", None) or []:
        lender = _cpr_plain(getattr(lien, "lender", None))
        name = str(lender or "").strip()
        if name and name.lower() != "unknown" and name not in lenders:
            lenders.append(name)
    if lenders:
        parties.append({"role": "lienholders", "name": "; ".join(lenders)})
    return parties


def _compose_offer_letter(cpr: Any, offer_letter: str) -> str:
    """The generated offer letter prefixed with the parties-of-record block."""
    parties = _parties_of_record(cpr)
    if not parties:
        return offer_letter
    lines = ["PARTIES OF RECORD (from saved CPR — verify before any human send)"]
    lines += [f"  {p['role'].replace('_', ' ')}: {p['name']}" for p in parties]
    return "\n".join(lines) + "\n\n" + ("-" * 72) + "\n\n" + offer_letter


@app.post("/api/generate-offer", summary="Price a parcel and stage a draft offer letter")
def api_generate_offer(payload: Annotated[Any, Body()] = None) -> JSONResponse:
    """Load a saved CPR, price it, render a draft offer, stage it under data/drafts/.

    Pipeline: locate CPR (as /api/cpr) → ``src.hitl.load_cpr`` → ARV basis =
    the *saved* valuation ARV when present (the human-approved number), else
    CAD appraised value → ``pricing.price_deal`` →
    ``negotiation.generate_offer`` → atomic draft write via
    ``io_utils.write_text`` into ``DRAFTS_DIR`` (default ``data/drafts/``).

    Honest-output rules:

    * No pricing basis (neither saved ARV nor positive CAD value) → 400
      ``unpriceable_deal`` — no fabricated economics.
    * A dead pencil (bid ceiling ≤ 0, no offer ladder) answers 200 with
      ``"dead": true``, the reason, and **no** letter and **no** draft file —
      $0 offers are fake signals.
    * The staged letter is ``generate_offer``'s rendering prefixed with a
      parties-of-record block (owner + lienholders read back from the saved
      CPR, echoed in the response under ``"parties"``) so the draft identifies
      its counterparties; it is always a draft for human review, never sent.

    Rehab basis is the flat record's ``rough_rehab`` for that parcel when
    current_deals.json can supply it, else $0 — both flagged in the response.
    """
    body = _body_dict(payload)
    raw_parcel = body.get("parcel_id")
    if not isinstance(raw_parcel, str):
        raise ApiError(400, "invalid_request", 'JSON body must be {"parcel_id": "<id>"}')
    pid = _validated_parcel(raw_parcel)

    path = _locate(_cpr_paths(pid))
    if path is None:
        raise ApiError(404, "cpr_not_found", f"no CPR snapshot for parcel {pid!r}")

    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise DataUnavailableError(path, "expected a CPR JSON object at top level")

    try:
        from src.hitl import load_cpr
        from src.io_utils import write_text
    except Exception as exc:  # lazy-import guard: deps must not break serving
        raise DataUnavailableError(
            REPO_ROOT / "src", f"analysis modules unavailable ({exc.__class__.__name__}: {exc})"
        ) from exc

    try:
        cpr = load_cpr(str(path))
    except Exception as exc:
        raise DataUnavailableError(
            path, f"CPR failed to load ({exc.__class__.__name__}: {exc})"
        ) from exc

    # ARV basis from the SAVED json (load_cpr deliberately discards stored
    # valuations — its contract is to replace them wholesale), falling back to CAD.
    arv: float | None = None
    arv_source = ""
    saved_val = raw.get("valuation")
    if isinstance(saved_val, dict) and isinstance(saved_val.get("arv"), dict):
        candidate = _as_number(saved_val["arv"].get("value"))
        if candidate is not None and candidate > 0:
            arv, arv_source = candidate, "saved_arv"
    if arv is None and cpr.cad_appraised_value is not None:
        candidate = _as_number(getattr(cpr.cad_appraised_value, "value", None))
        if candidate is not None and candidate > 0:
            arv, arv_source = candidate, "cad_appraised_value"
    if arv is None:
        raise ApiError(
            400,
            "unpriceable_deal",
            f"parcel {pid!r} has neither a saved valuation ARV nor a positive "
            "CAD appraised value to price against",
        )

    rehab, rehab_source = _flat_rough_rehab(pid)

    try:
        from src import policy
        from src.negotiation import generate_offer
        from src.pricing import price_deal
        from src.underwriting import mao
    except Exception as exc:
        raise DataUnavailableError(
            REPO_ROOT / "src", f"analysis modules unavailable ({exc.__class__.__name__}: {exc})"
        ) from exc

    dt_raw = cpr.deal_type
    deal_type = str(dt_raw.value if hasattr(dt_raw, "value") else (dt_raw or "")) or "DT-AUCTION"

    # Capital-at-risk proxy for tiering: the max we would pay (MAO). Documented
    # approximation — this endpoint does not run the full underwriter.
    capital_proxy = max(mao(arv, rehab, policy.MAO_FACTOR_DEFAULT), 0.0)
    pricing = price_deal(
        cpr, arv=arv, rehab=rehab, deal_type=deal_type,
        factor=policy.MAO_FACTOR_DEFAULT, capital_at_risk=capital_proxy,
    )
    pricing_payload = {
        "mao": round(pricing.mao, 2),
        "bid_ceiling": round(pricing.bid_ceiling, 2),
        "offer_ladder": pricing.offer_ladder,
        "pencil_status": pricing.pencil_status,
        "arv_basis": {"value": arv, "source": arv_source},
        "rehab_basis": {"value": rehab, "source": rehab_source},
        "deal_type": deal_type,
    }

    if pricing.dead_reason or not pricing.offer_ladder:
        return JSONResponse(content={
            "parcel_id": pid,
            "dead": True,
            "dead_reason": pricing.dead_reason,
            "pricing": pricing_payload,
            "letter": "",
            "draft_written": False,
            "note": (
                "Deal priced dead (bid ceiling non-positive) — no offer letter "
                "generated, no draft written."
            ),
        })

    offer = generate_offer(cpr, pricing)
    letter_text = _compose_offer_letter(cpr, offer.offer_letter)
    draft_name = f"draft_offer_{pid}.txt"
    draft_path = DRAFTS_DIR / draft_name
    write_text(str(draft_path), letter_text)  # io_utils: temp+fsync+replace
    try:
        draft_display = str(draft_path.relative_to(REPO_ROOT))
    except ValueError:  # DRAFTS_DIR relocated outside the repo (tests/tmp dirs)
        draft_display = str(draft_path)

    return JSONResponse(content={
        "parcel_id": pid,
        "dead": False,
        "deal_type": deal_type,
        "terms": {
            "offer_price": offer.offer_price,
            "earnest_money": offer.earnest_money,
            "option_fee": offer.option_fee,
            "option_period_days": offer.option_period_days,
            "closing_timeline_days": offer.closing_timeline_days,
            "is_cash": offer.is_cash,
            "seller_posture": offer.seller_posture,
            "blocked": offer.blocked,
            "block_reason": offer.block_reason,
            "compliance_status": offer.compliance_status,
        },
        "pricing": pricing_payload,
        "letter": letter_text,
        "parties": _parties_of_record(cpr),
        "draft_path": draft_display,
        "note": "Draft-only posture: letter staged for human review, nothing sent.",
    })


def _flat_rough_rehab(parcel_id: str) -> tuple[float, str]:
    """Rehab estimate for a parcel from the flat deals file, else ($0, 'unknown')."""
    try:
        deals = _load_deals()
    except DataUnavailableError:
        return 0.0, "unknown"
    for record in deals:
        if not isinstance(record, dict):
            continue
        if str(record.get("parcel_id") or "").strip() != parcel_id:
            continue
        rehab = _as_number(record.get("rough_rehab"))
        if rehab is not None and rehab > 0:
            return rehab, "rough_rehab(current_deals)"
        break
    return 0.0, "unknown"


@app.post("/api/clerk-search", summary="LIVE Collin County Clerk records search")
def api_clerk_search(payload: Annotated[Any, Body()] = None) -> JSONResponse:
    """Search official county records by party name; returns counts + preview.

    LIVE call: drives ``CollinClerkAdapter.fetch`` (headless Chromium against
    collin.tx.publicsearch.us), typically 10–15 s. Politeness posture is
    documented in ``CLERK_POLITENESS`` and echoed in every response. Clean
    paths: empty/oversized party name → 400; Playwright missing, browser
    failure, or any transport error → 503 ``clerk_unavailable`` (the search is
    best-effort — it must never crash the server or fabricate results).
    """
    body = _body_dict(payload)
    party = body.get("party_name")
    if not isinstance(party, str) or not party.strip():
        raise ApiError(400, "invalid_request", 'JSON body must be {"party_name": "<name>"}')
    party = party.strip()
    if len(party) > CLERK_MAX_PARTY_LEN:
        raise ApiError(
            400, "invalid_request",
            f"party_name exceeds {CLERK_MAX_PARTY_LEN} characters",
        )

    try:
        result = _run_clerk_search(party)
    except Exception as exc:
        raise ApiError(
            503,
            "clerk_unavailable",
            f"Collin clerk search failed ({exc.__class__.__name__}: {exc})",
        ) from exc

    instruments = result.get("instruments") or []
    liens = result.get("liens") or []
    preview = [_instrument_dict(i) for i in instruments[:CLERK_PREVIEW_LIMIT]]
    return JSONResponse(content={
        "party_name": party,
        "instrument_count": len(instruments),
        "encumbrance_count": len(liens),
        "preview_truncated": len(instruments) > len(preview),
        "preview": preview,
        "politeness": CLERK_POLITENESS,
    })


# --- F11: market aggregates -----------------------------------------------------
def compute_market(deals: list, groupby: str, today: date | None = None) -> list[dict]:
    """Aggregate flat deal records per group — pure helper over the rows.

    Entry shape (exactly six keys): ``{group, deals, median_cad, total_debt,
    positive_equity_share, upcoming_sales}``.

    * ``zip`` is extracted from the tail of the address ("... TX 75093-1234"
      → "75093"); rows without a trailing ZIP group under "unknown".
    * ``median_cad`` is the median of parseable ``cad_value`` (None when the
      group has none); ``total_debt`` sums parseable ``mortgage_debt`` (None
      when the group has none — distinguishable from a true $0 sum).
    * ``positive_equity_share`` is share of rows with a parseable
      ``equity_estimate`` that is > 0 (None when no row has one).
    * ``upcoming_sales`` counts rows whose ``sale_date`` parses (via
      ``src.dates.parse_date_any``) to ``today`` or later; unparseable dates
      are never counted as upcoming.
    """
    if today is None:  # tz-aware now → date, matching src/cpr.py conventions
        today = datetime.now(timezone.utc).date()
    try:
        from src.dates import parse_date_any  # lazy: keep boot import-free of src
    except Exception as exc:
        raise DataUnavailableError(
            REPO_ROOT / "src", f"analysis modules unavailable ({exc.__class__.__name__}: {exc})"
        ) from exc

    buckets: dict[str, dict] = {}
    for record in deals:
        if not isinstance(record, dict):
            continue
        if groupby == "zip":
            match = _ZIP_TAIL_RE.search(str(record.get("address") or ""))
            group = match.group(1) if match else "unknown"
        else:
            raw = record.get(groupby)
            group = str(raw).strip() if raw is not None else ""
            group = group or "unknown"
        bucket = buckets.setdefault(group, {
            "deals": 0, "cads": [], "debts": [], "eq_pos": 0, "eq_n": 0, "upcoming": 0,
        })
        bucket["deals"] += 1
        cad = _as_number(record.get("cad_value"))
        if cad is not None:
            bucket["cads"].append(cad)
        debt = _as_number(record.get("mortgage_debt"))
        if debt is not None:
            bucket["debts"].append(debt)
        equity = _as_number(record.get("equity_estimate"))
        if equity is not None:
            bucket["eq_n"] += 1
            if equity > 0:
                bucket["eq_pos"] += 1
        sale_day = parse_date_any(record.get("sale_date"))
        if sale_day is not None and sale_day >= today:
            bucket["upcoming"] += 1

    rows = []
    for group, b in buckets.items():
        rows.append({
            "group": group,
            "deals": b["deals"],
            "median_cad": round(statistics.median(b["cads"]), 2) if b["cads"] else None,
            "total_debt": round(sum(b["debts"]), 2) if b["debts"] else None,
            "positive_equity_share": (
                round(b["eq_pos"] / b["eq_n"], 4) if b["eq_n"] else None
            ),
            "upcoming_sales": b["upcoming"],
        })
    rows.sort(key=lambda r: (-r["deals"], str(r["group"])))
    return rows


@app.get("/api/market", summary="Deal aggregates grouped by zip | city | isd | county")
def api_market(groupby: str = "") -> JSONResponse:
    """/api/market?groupby=zip|city|isd|county → {groupby, groups: [...]}.

    An unknown (or absent) ``groupby`` is a clean 400 listing the valid
    dimensions — never a silent fallback, since a wrong grouping silently
    reads as plausible data.
    """
    key = (groupby or "").strip().lower()
    if key not in MARKET_GROUPBYS:
        raise ApiError(
            400,
            "invalid_groupby",
            f"groupby must be one of {list(MARKET_GROUPBYS)}, got {groupby!r}",
        )
    deals = _load_deals()
    return JSONResponse(content={
        "groupby": key,
        "groups": compute_market(deals, key),
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8123)
