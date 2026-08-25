# src/disambiguate.py
"""DCAD mismatch disambiguation — human-in-the-loop rescue for echo-gate failures.

The R2 fail-closed echo gate (src/adapters/dallas_cad.py) withholds property
fields whenever the DCAD detail page echoes back a different address than the
one queried. The legacy scrape left 34 such mismatches in data/dcad_cache.json;
many are false failures: the bulk flow auto-clicked the FIRST detail link on
the results grid, and junk row text ("link to view the details of your
account.", "2026 Appraisal Notice") got captured as the displayed address.

This module turns those mismatches into a reviewable workflow:

- build_queue():       ranked list of mismatched cache entries (newest first)
- purge_mismatches():  drop them from the cache so bulk re-attempts them
- export:              data/dcad_disambiguation_queue.{json,md} review table
- --resolve (LIVE):    re-run the DCAD address search but collect EVERY
                       candidate detail link, print them numbered, let a human
                       pick with --choice N, then apply the SAME echo gate
                       against the ORIGINAL query before touching the cache.

Offline by default: only --resolve performs browser/network work. Everything
else is local file surgery on the JSON cache.

Reuse note (task 4): the live flow imports dallas_cad's private helpers
(_apply_echo_gate / _extract_displayed_address / _parse_dcad_detail) so gating
and parsing stay byte-identical to bulk; only the search loop itself is
duplicated here in a compact form, because it must STOP at the results page
instead of auto-navigating to the first detail link.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup

from .addr import normalize_address, to_dcad_search_parts
from .dates import parse_date_any
from .io_utils import write_json, write_text

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CACHE_PATH = os.path.join(REPO_ROOT, "data", "dcad_cache.json")
CACHE_ENV_VAR = "HERMES_DCAD_CACHE"

QUEUE_JSON_NAME = "dcad_disambiguation_queue.json"
QUEUE_MD_NAME = "dcad_disambiguation_queue.md"

DCAD_BASE_URL = "https://www.dallascad.org"
LIST_DEFAULT_LIMIT = 10
_DIAGNOSTIC_TRUNC = 90
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Same link family the bulk flow auto-clicks (first match); here we keep ALL.
_DETAIL_HREF_RE = re.compile(r"Detail|PropertyDetail|PCD|AcctDetail", re.IGNORECASE)


# --------------------------------------------------------------------------
# Cache plumbing
# --------------------------------------------------------------------------

def resolve_cache_path(cli_override: str | None = None) -> str:
    """Cache file precedence: --cache arg > HERMES_DCAD_CACHE > repo default."""
    if cli_override:
        return os.path.abspath(cli_override)
    env = os.environ.get(CACHE_ENV_VAR)
    if env:
        return os.path.abspath(env)
    return DEFAULT_CACHE_PATH


def load_cache(path: str) -> dict:
    """Tolerant load mirroring dallas_cad_bulk semantics: bad file => {}."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        print(
            f"warning: DCAD cache {path} unreadable ({type(exc).__name__}: {exc})"
            " — treating as empty",
            file=sys.stderr,
        )
        return {}
    if not isinstance(data, dict):
        print(f"warning: DCAD cache {path} malformed — treating as empty",
              file=sys.stderr)
        return {}
    return data


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Task 1/2: queue building + purging (pure functions over the cache dict)
# --------------------------------------------------------------------------

def _parse_attempted_at(value) -> datetime | None:
    """Full-precision timestamp for ordering; None when unknowable.

    parse_date_any() deliberately truncates to a date, which would flatten a
    same-day scrape session into ties — so ISO timestamps are ordered at
    second/microsecond precision here, falling back to parse_date_any for
    non-ISO strings.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        parsed = parse_date_any(s)
        if parsed is not None:
            dt = datetime(parsed.year, parsed.month, parsed.day,
                          tzinfo=timezone.utc)
    if dt is None:
        return None
    if dt.tzinfo is None:  # naive timestamp: interpret as UTC, stay comparable
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_queue(cache: dict) -> list[dict]:
    """Mismatched cache entries as review rows, newest attempt first.

    Row shape: {key, queried_address (the key itself), displayed_diagnostic,
    attempted_at}. Entries without a usable timestamp sort last (key asc).
    """
    rows = []
    for key, entry in cache.items():
        if not isinstance(entry, dict) or entry.get("status") != "mismatch":
            continue
        rows.append({
            "key": key,
            "queried_address": key,
            "displayed_diagnostic": entry.get("displayed_address"),
            "attempted_at": entry.get("retrieved_at"),
            "_dt": _parse_attempted_at(entry.get("retrieved_at")),
        })
    # Stable two-pass: key ascending breaks ties within equal timestamps and
    # keeps undated rows deterministic.
    rows.sort(key=lambda r: r["key"] or "")
    rows.sort(key=lambda r: r["_dt"] or _EPOCH, reverse=True)
    for row in rows:
        del row["_dt"]
    return rows


def purge_mismatches(cache: dict) -> tuple[dict, int]:
    """Drop status=='mismatch' entries so bulk re-attempts them; pure."""
    kept = {
        key: entry
        for key, entry in cache.items()
        if not (isinstance(entry, dict) and entry.get("status") == "mismatch")
    }
    return kept, len(cache) - len(kept)


def age_days(attempted_at, today: date | None = None) -> int | None:
    """Whole days since the attempt was made; None when unknowable."""
    parsed = parse_date_any(attempted_at)
    if parsed is None:
        return None
    return ((today or date.today()) - parsed).days


def format_age(days: int | None) -> str:
    return "?" if days is None else f"{days}d"


# --------------------------------------------------------------------------
# Task 1: export (JSON machine list + MD human table)
# --------------------------------------------------------------------------

def _collapse(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).replace("\xa0", " ").strip()


def _truncate(text, width: int = _DIAGNOSTIC_TRUNC) -> str:
    t = _collapse(text)
    if width and len(t) > width:
        return t[: width - 1] + "…"
    return t


def _md_cell(text, width: int | None = None) -> str:
    cell = _truncate(text, width) if width else _collapse(text)
    return cell.replace("|", "\\|").replace("`", "'")


def render_queue_markdown(rows: list[dict], source_cache: str,
                          generated_at: str | None = None) -> str:
    stamp = generated_at or _utc_now_iso()
    lines = [
        "# DCAD Disambiguation Queue",
        "",
        f"- Generated: {stamp}",
        f"- Source cache: `{_md_cell(source_cache)}`",
        f"- Mismatched entries: {len(rows)} (newest attempt first)",
        "",
        'Rescue workflow: `python -m src.disambiguate --resolve "<query>" '
        '[--choice N]`',
        "",
        "| # | Query | Diagnostic | Age |",
        "| --- | --- | --- | --- |",
    ]
    for i, row in enumerate(rows, 1):
        query = _md_cell(row["queried_address"])
        diag = _md_cell(row["displayed_diagnostic"], width=_DIAGNOSTIC_TRUNC)
        age = format_age(age_days(row["attempted_at"]))
        lines.append(f"| {i} | `{query}` | {diag} | {age} |")
    return "\n".join(lines) + "\n"


def export_queue(cache: dict, out_dir: str,
                 generated_at: str | None = None) -> tuple[str, str]:
    """Write dcad_disambiguation_queue.{json,md}; returns both paths."""
    rows = build_queue(cache)
    json_path = write_json(os.path.join(out_dir, QUEUE_JSON_NAME), rows)
    md_path = write_text(
        os.path.join(out_dir, QUEUE_MD_NAME),
        render_queue_markdown(rows, out_dir, generated_at=generated_at),
    )
    return json_path, md_path


# --------------------------------------------------------------------------
# Task 3/4: LIVE candidate collection (opt-in via --resolve only)
# --------------------------------------------------------------------------

def _absolutize_dcad_href(href: str) -> str:
    href = href.strip()
    if href.startswith("http"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return DCAD_BASE_URL + href


def _candidate_label(anchor) -> str:
    """Nearby row text for a results-grid link (cells joined with ' | ')."""
    tr = anchor.find_parent("tr")
    if tr is not None:
        cells = [
            _collapse(td.get_text(" ", strip=True))
            for td in tr.find_all(["td", "th"])
        ]
        cells = [c for c in cells if c]
        if cells:
            return _truncate(" | ".join(cells), 200)
    return _truncate(anchor.get_text(" ", strip=True), 200)


def collect_candidates(results_soup) -> list[dict]:
    """ALL detail links on a DCAD results page, deduped by URL, numbered."""
    candidates: list[dict] = []
    seen: set[str] = set()
    for anchor in results_soup.find_all("a", href=True):
        href = anchor.get("href") or ""
        if not _DETAIL_HREF_RE.search(href):
            continue
        url = _absolutize_dcad_href(href)
        # Stay on-property: --choice navigates within one browser session, so
        # a foreign absolute URL would be useless (and off-domain).
        if not url.startswith(DCAD_BASE_URL + "/") and url != DCAD_BASE_URL:
            continue
        if url in seen:
            continue
        seen.add(url)
        candidates.append({"url": url, "row_text": _candidate_label(anchor)})
    for choice, cand in enumerate(candidates, 1):
        cand["choice"] = choice
    return candidates


async def _resolve_live(query: str, choice: int | None) -> dict:
    """LIVE: run the DCAD search, stop at the results page, list candidates.

    Unlike dallas_cad._search_dcad_by_address this never auto-clicks the first
    detail link. With --choice N it navigates to that candidate, parses it and
    applies the SAME echo gate (_apply_echo_gate) using the ORIGINAL query.
    Never touches the cache — callers decide persistence from the gate result.
    """
    try:
        # Lazy import: keeps every offline mode runnable without a browser stack.
        from playwright.async_api import async_playwright

        from .adapters.dallas_cad import (
            DCAD_URL,
            _apply_echo_gate,
            _extract_displayed_address,
            _parse_dcad_detail,
        )
    except ImportError as exc:  # pragma: no cover - environment-dependent
        return {"stage": "error",
                "message": f"browser tooling unavailable: {exc}"}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            # Compact duplication of the verified bulk search loop (see module
            # docstring): identical form fields/events, different stopping point.
            await page.goto(DCAD_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(2000)

            parts = to_dcad_search_parts(query)
            if not parts:  # defensive; cmd_resolve pre-checks too
                reason = normalize_address(query).invalid_reason or "implausible"
                return {"stage": "error",
                        "message": f"query not lookup-worthy ({reason}): {query!r}"}

            # page.type() — proper keyboard events for ASP.NET validation
            await page.click("#txtAddrNum")
            await page.type("#txtAddrNum", parts["num"], delay=50)
            await page.click("#txtStName")
            await page.type("#txtStName", parts["name"], delay=50)
            await page.wait_for_timeout(500)

            await page.click('input[name="cmdSubmit"]')
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(3000)

            if "SearchError" in page.url:
                return {"stage": "error",
                        "message": "DCAD search error — ASP.NET form rejected "
                                   "submission"}

            soup = BeautifulSoup(await page.content(), "html.parser")
            candidates = collect_candidates(soup)
            if choice is None:
                return {"stage": "candidates", "candidates": candidates,
                        "results_url": page.url}
            if not 1 <= choice <= len(candidates):
                return {"stage": "error", "candidates": candidates,
                        "results_url": page.url,
                        "message": f"--choice {choice} is out of range "
                                   f"1..{len(candidates)}"}

            picked = candidates[choice - 1]
            await page.goto(picked["url"], timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=10000)
            await page.wait_for_timeout(2000)

            detail_soup = BeautifulSoup(await page.content(), "html.parser")
            displayed = _extract_displayed_address(detail_soup)
            gated = _apply_echo_gate(_parse_dcad_detail(detail_soup),
                                     query, displayed)
            return {"stage": "resolved", "gated": gated,
                    "displayed_address": displayed, "picked": picked,
                    "candidates_total": len(candidates)}
        except Exception as exc:  # noqa: BLE001 - report, never crash the CLI
            return {"stage": "error", "message": f"{type(exc).__name__}: {exc}"}
        finally:
            await browser.close()


def cmd_resolve(query: str, choice: int | None, cache_path: str) -> int:
    """LIVE driver: prints candidates, applies the gate, persists on success."""
    parts = to_dcad_search_parts(query)
    if not parts:
        reason = normalize_address(query).invalid_reason or \
            "not a plausible street address"
        print(f"FAIL: refusing to search DCAD with this query ({reason}):")
        print(f"  {query!r}")
        print("Normalize or correct the address first (src.addr.normalize_address).")
        return 1

    state = asyncio.run(_resolve_live(query, choice))

    if state["stage"] == "error":
        print(f"FAIL: {state['message']}")
        for cand in state.get("candidates") or []:
            print(f"  {cand['choice']}. {_truncate(cand['row_text'])}\n"
                  f"      {cand['url']}")
        print("Cache left untouched.")
        return 1

    candidates = state.get("candidates") or []
    print(f"{len(candidates)} candidate detail link(s) on results page "
          f"{state.get('results_url', '')}:")
    for cand in candidates:
        print(f"  {cand['choice']}. {_truncate(cand['row_text'])}\n"
              f"      {cand['url']}")

    if state["stage"] == "candidates":
        if not candidates:
            print("No candidate detail links found — nothing to disambiguate.")
            print("Cache left untouched.")
            return 1
        print("\nRe-run with --choice N to fetch one; the echo gate still "
              "applies before any cache write.")
        return 0

    gated = state["gated"]
    picked = state["picked"]
    print(f"\nChosen candidate #{picked['choice']}: {picked['url']}")
    print(f"Displayed situs: {state['displayed_address']!r}")

    if gated.get("status") != "parsed":
        print("ECHO GATE FAILED — property fields withheld, cache untouched.")
        print(f"  gate: {gated.get('error')}")
        print(f"  expected query tokens vs displayed: "
              f"{gated.get('displayed_address')!r}")
        return 1

    cache = load_cache(cache_path)
    replaced = query in cache
    entry = dict(gated)
    entry["retrieved_at"] = _utc_now_iso()  # same shape as bulk-written entries
    cache[query] = entry
    write_json(cache_path, cache)
    fields = ", ".join(
        f"{k}={v!r}" for k, v in sorted(entry.items())
        if k not in ("status", "match_quality", "retrieved_at")
    )
    print(f"CACHE {'UPDATED' if replaced else 'CREATED'} in {cache_path}")
    print(f"  status=parsed match_quality={entry.get('match_quality')!r}")
    print(f"  fields: {fields or '(none extracted)'}")
    return 0


# --------------------------------------------------------------------------
# Offline CLI commands
# --------------------------------------------------------------------------

def cmd_list(cache_path: str, limit: int) -> int:
    rows = build_queue(load_cache(cache_path))
    head = rows[: max(limit, 0)]
    scope = f", showing first {len(head)}" if head and len(head) < len(rows) else ""
    print(f"{len(rows)} mismatched entr{'y' if len(rows) == 1 else 'ies'} in "
          f"{cache_path} (newest first{scope})")
    for i, row in enumerate(head, 1):
        print(f"{i:>3}. [{row['attempted_at'] or 'no timestamp'}] "
              f"{row['queried_address']}")
        if row["displayed_diagnostic"]:
            print(f"     diagnostic: {_truncate(row['displayed_diagnostic'])}")
    return 0


def cmd_show(cache_path: str, key: str) -> int:
    cache = load_cache(cache_path)
    entry = cache.get(key)
    if entry is None:
        print(f"no cached entry for key {key!r}", file=sys.stderr)
        hints = [k for k in cache if key.lower() in str(k).lower()]
        if hints:
            preview = ", ".join(repr(h) for h in hints[:3])
            more = "" if len(hints) <= 3 else f" (+{len(hints) - 3} more)"
            print(f"keys containing that substring: {preview}{more}",
                  file=sys.stderr)
        return 1
    print(json.dumps({"key": key, **entry}, indent=2))
    return 0


def cmd_purge_mismatches(cache_path: str) -> int:
    cache = load_cache(cache_path)
    kept, purged = purge_mismatches(cache)
    if purged:
        write_json(cache_path, kept)
    print(f"purged {purged} mismatched entr"
          f"{'y' if purged == 1 else 'ies'}; {len(kept)} kept -> {cache_path}")
    return 0


def cmd_export(cache_path: str) -> int:
    cache = load_cache(cache_path)
    out_dir = os.path.dirname(cache_path) or "."
    json_path, md_path = export_queue(cache, out_dir=out_dir)
    print(f"exported {len(build_queue(cache))} mismatched entrie(s):")
    print(f"  {json_path}")
    print(f"  {md_path}")
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.disambiguate",
        description=(
            "DCAD mismatch disambiguation. Offline by default; only --resolve "
            "performs live browser work."
        ),
    )
    parser.add_argument(
        "--cache", metavar="PATH", default=None,
        help=f"cache file override (default: data/dcad_cache.json, env "
             f"{CACHE_ENV_VAR})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true",
                      help="print the head of the mismatch queue")
    mode.add_argument("--show", metavar="KEY",
                      help="print one cached entry exactly as stored")
    mode.add_argument("--export", action="store_true",
                      help=f"write {QUEUE_JSON_NAME} and {QUEUE_MD_NAME} next "
                           f"to the cache file")
    mode.add_argument("--purge-mismatches", action="store_true",
                      dest="purge_mismatches",
                      help="remove mismatch entries from the cache so bulk "
                           "re-attempts them")
    mode.add_argument("--resolve", metavar="QUERY_ADDRESS",
                      help="LIVE: search DCAD and list ALL candidate detail "
                           "links for this original query string")
    parser.add_argument("--choice", type=int, default=None, metavar="N",
                        help="with --resolve: fetch candidate N and apply the "
                             "echo gate before any cache write")
    parser.add_argument("--limit", type=int, default=LIST_DEFAULT_LIMIT,
                        metavar="N",
                        help=f"rows shown by --list (default "
                             f"{LIST_DEFAULT_LIMIT})")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.choice is not None and not args.resolve:
        parser.error("--choice requires --resolve")

    if not any([args.list, args.show, args.export, args.purge_mismatches,
                args.resolve]):
        parser.print_help()
        return 0

    cache_path = resolve_cache_path(args.cache)
    if args.list:
        return cmd_list(cache_path, args.limit)
    if args.show is not None:
        return cmd_show(cache_path, args.show)
    if args.export:
        return cmd_export(cache_path)
    if args.purge_mismatches:
        return cmd_purge_mismatches(cache_path)
    return cmd_resolve(args.resolve, args.choice, cache_path)


# --------------------------------------------------------------------------
# Self-check (pure functions only — no network, no filesystem writes)
# --------------------------------------------------------------------------

def self_check() -> None:
    cache = {
        "newer, DALLAS, TX": {"status": "mismatch",
                              "displayed_address": "junk row text",
                              "retrieved_at": "2026-08-25T23:00:00+00:00"},
        "older, DALLAS, TX": {"status": "mismatch", "displayed_address": None,
                              "retrieved_at": "2026-08-01T10:00:00+00:00"},
        "undated, DALLAS, TX": {"status": "mismatch"},
        "good, DALLAS, TX": {"status": "parsed", "beds": "3",
                             "retrieved_at": "2026-08-25T22:00:00+00:00"},
        "negative, DALLAS, TX": {"status": "no_data_found",
                                 "retrieved_at": "2026-08-25T22:00:00+00:00"},
    }
    queue = build_queue(cache)
    assert [r["key"] for r in queue] == ["newer, DALLAS, TX",
                                         "older, DALLAS, TX",
                                         "undated, DALLAS, TX"], queue
    assert queue[0]["queried_address"] == "newer, DALLAS, TX"
    assert queue[0]["displayed_diagnostic"] == "junk row text"

    kept, purged = purge_mismatches(cache)
    assert purged == 3 and len(kept) == 2
    assert len(cache) == 5, "purge_mismatches must be pure"

    html = ('<tr><td><a href="Detail.aspx?ID=1">Select</a></td>'
            '<td>14160 DALLAS PKWY</td></tr>'
            '<a href="/Other.aspx?ID=9">skip</a>')
    cands = collect_candidates(BeautifulSoup(html, "html.parser"))
    assert len(cands) == 1 and cands[0]["choice"] == 1
    assert cands[0]["url"].startswith(DCAD_BASE_URL)
    assert "14160 DALLAS PKWY" in cands[0]["row_text"]

    md = render_queue_markdown(queue, "cache.json",
                               generated_at="2026-08-26T00:00:00+00:00")
    assert md.count("\n| ") == len(queue) + 2, "header + separator + rows"
    assert "| ? |" in md, "undated row must show unknown age"


if __name__ == "__main__":
    self_check()
    sys.exit(main())
