# server.py
"""HERMES-REO dashboard server — DASHBOARD_PLAN.md Phase-1 core (read-only).

A single FastAPI app serving the static dark-theme dashboard under ``static/``
plus thin JSON APIs over the existing pipeline data files:

| Route           | Serves                                                       |
|-----------------|--------------------------------------------------------------|
| ``GET /``       | ``static/index.html`` (portfolio overview UI)                 |
| ``GET /api/deals``   | ``data/current_deals.json`` (fresh read per request)      |
| ``GET /api/geojson`` | ``data/live_foreclosures.geojson`` (fresh read per request) |
| ``GET /api/stats``   | portfolio stats via ``src.synthesis`` over enriched CPRs    |
| ``GET /api/summary`` | small KPI aggregate {total, counties, with_sqft, ...}       |

Design constraints (DASHBOARD_PLAN.md + delivery rules):

* All paths are anchored to the repo root via ``Path(__file__).parent`` so the
  server runs identically from any working directory.
* Heavy analysis imports (``src.synthesis`` / ``src.enrichment``) happen
  lazily INSIDE the ``/api/stats`` handler — a broken optional dependency can
  never prevent startup or static serving.
* A missing/unreadable backing data file raises :class:`DataUnavailableError`,
  converted to HTTP 503 with a clear JSON message. Handlers never crash.

Run: ``uvicorn server:app`` (offline-safe; no network access at import time).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# --- Repo-anchored paths ----------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
STATIC_DIR = REPO_ROOT / "static"
INDEX_PATH = STATIC_DIR / "index.html"
DATA_DIR = REPO_ROOT / "data"
DEALS_PATH = DATA_DIR / "current_deals.json"
GEOJSON_PATH = DATA_DIR / "live_foreclosures.geojson"

SUMMARY_KEYS = ("total", "counties", "with_sqft", "with_debt", "geocoded")


class DataUnavailableError(Exception):
    """A route's backing data file is missing or unreadable → HTTP 503."""

    def __init__(self, path: Path, reason: str = "") -> None:
        self.path = path
        self.reason = reason or "file not found"
        super().__init__(f"{path}: {self.reason}")


app = FastAPI(
    title="HERMES-REO Dashboard",
    description=(
        "Read-only portfolio dashboard over HERMES-REO pipeline data "
        "(current_deals.json, live_foreclosures.geojson, src.synthesis)."
    ),
    version="1.0.0",
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8123)
