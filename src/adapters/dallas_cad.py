# src/adapters/dallas_cad.py
"""Dallas CAD (DCAD) adapter — cross-reference property characteristics.

DCAD is an ASP.NET form-based search at dallascad.org. Requires Playwright
to submit the address search form and parse the results page.

Used to fill the gap in Dallas foreclosure data: the PDF notices only have
address + debt, not sqft/beds/baths/year built. DCAD provides those.

RL9 yield probe: with HERMES_DCAD_STATS=1 in the environment, results carry a
"parse_stats" payload {detail_link_found, extracted_fields_count, echo_quality,
derived_source} so regex decay (endless cache-miss re-searches) becomes
measurable. Without the env var, behavior and result shape are unchanged.

FN3 root cause — degenerate cad_value_2025: 196/198 paired rows in
data/current_deals.json carry cad_value / cad_value_2025 == exactly 25/24
(+4.1667%). Nothing in THIS repository derives one year from the other:
enrichment.py reads both years verbatim from tax_info, and this adapter's
parser historically captured a SINGLE unlabeled "Market Value" — it had no
per-year extraction at all, so no row enriched through this codebase can
legitimately acquire a second year. The uniform ratio is therefore a legacy
harvest artifact: one year was synthesized upstream from the certified prior
year with a flat 25/24 uplift instead of scraping both notice values.
Defenses implemented here:
  * cad_value_{YYYY}_raw strings are emitted whenever a detail page labels a
    per-year value ("Appraised Value (2025): $749,180"), alongside parsed
    floats under cad_value_{YYYY};
  * any pair whose newer figure equals the older times exactly 25/24 (to
    within display rounding) is flagged derived_source="2026" in the parsed
    result and mirrored into parse_stats — the newer notice year is the one
    assessments generate from the prior certified roll, so analytics can
    never mistake such a pair for two independent observations. (The ratio
    proves derivation; the direction is inferred from assessment practice,
    not observable from the pair alone.)
"""
from __future__ import annotations
import asyncio
import os
import re
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from ..addr import STREET_TYPES, normalize_address
from .base import SourceAdapter, SourceProvenance

DCAD_URL = "https://www.dallascad.org/SearchAddr.aspx"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# RL9: parse-yield probe switch (opt-in via environment).
DCAD_STATS_ENV = "HERMES_DCAD_STATS"

# FN3: the legacy uplift signature between paired CAD years (25/24 == +1/24).
LEGACY_UPLIFT_NUM = 25
LEGACY_UPLIFT_DEN = 24
# Display-rounding tolerance in dollars: harvests round the synthesized year
# to whole dollars (or cents), so the cross-year identity can only be checked
# to +-half a dollar after dividing back (plus float slop). An independent
# appraisal landing within $0.50 of EXACTLY 25/24 of its prior year is a
# coincidence this flag is willing to accept — the marker records suspicion
# for provenance triage, it never deletes data.
_DERIVED_DISPLAY_TOLERANCE_USD = 0.5 + 1e-6

# FN3: labeled per-year value lines on DCAD detail pages, e.g.
# "Appraised Value (2025): $749,180" / "Market Value (2026): $387,000".
# Cents are captured too — truncating "$780,395.83" to 780395 used to break
# the derived-pair identity check below. The plain "Market Value:" capture
# (no year) stays untouched below.
_YEAR_VALUE_RE = re.compile(
    r"(?:Appraised|Market)\s+Value\s*\((20\d{2})\)\s*:?\s*\$?\s*"
    r"([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_DERIVED_PAIR_YEARS = ("2025", "2026")


# --- R2: echo-back verification ---------------------------------------------
# DCAD search results must be verified against the queried address before any
# property characteristics are attached: the ASP.NET grid sometimes echoes back
# a *different* property (same street name, different number/city), and
# silently attaching its sqft/beds/baths corrupts underwriting inputs.

_ECHO_COMPONENTS = ("street_number", "street_name_first", "zip5")

# Zip lives in the tail segment ("..., ADDISON, TX 75001-6334"); anchor at end
# so we never grab a year or account number instead.
_ZIP5_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")

# Detail-page labels that introduce the displayed situs address, most-specific first.
_DISPLAYED_ADDRESS_LABELS = (
    re.compile(r"Situs\s+Address\b[:\s]*(.+)", re.IGNORECASE),
    re.compile(r"Property\s+Address\b[:\s]*(.+)", re.IGNORECASE),
)
# Live DCAD detail pages label the situs with a bare "Address:" line whose
# VALUE sits on its own following line, using non-breaking spaces and an
# ABBREVIATED street type ("15806\xa0BREEDLOVE\xa0PL") — no city/zip there.
_ADDRESS_LABEL_ONLY_RE = re.compile(r"^Address\s*:?\s*$", re.IGNORECASE)
_FALLBACK_ADDR_LINE_RE = re.compile(
    r"^\d+\s+[A-Za-z].*\bTX\b.*\d{5}", re.IGNORECASE
)
# Situs line without city/zip: street number + alpha tokens (e.g. "15806 BREEDLOVE PL")
_SITUS_LINE_RE = re.compile(r"^\d{1,6}\s+[A-Za-z]{3,}[A-Za-z ]*$")


# --- R2b: echo RESCUE path (abbreviation / city-glue variants) ---------------
# Why same-property pairs used to fail this gate: of the 34 rows sitting in
# data/dcad_cache.json under status="mismatch" when the fail-closed echo gate
# landed, ALL 34 recorded displayed text that was page-link boilerplate or a
# notice header rather than a situs line, while the QUERY side carried legacy-
# scrape artifacts — 13 keys glue the city onto the street segment ("FRAZIER
# ST DALLAS", "HOLLOW OAK ROAD LANCASTER", "BALCHSPRINGS"), 9 repeat the
# state/zip as a "TEXAS 75216, TX 75202" tail, and 5 carry OCR/nbsp token
# damage (families overlap). The pure-abbreviation family comes from the live
# detail page itself, whose situs renders as "15806\xa0BREEDLOVE\xa0PL":
# abbreviated street type, non-breaking-space separators, NO zip. The old
# raw-token compare took the first post-number token verbatim as the entire
# name component, so a directional prefix ("N." / "NORTH" vs a bare situs
# line) masked the real street name ("N Cedar Hill Rd" vs "Cedar Hill Rd"),
# any single-token wobble (an OCR-damaged name like BERKLEY/BARKLEY) dropped
# the vote below 2-of-3, and street type was never comparable — leaving
# nothing to substitute when the rendered line carried no zip. Identical
# properties therefore scored mismatch; each loss mode is fixed below.
#
# Rescue rules (BOTH sides pass through src.addr normalization first):
#   * nbsp/whitespace runs collapse before any tokenizing;
#   * canonical street types compare equal (PLACE == PL, DRIVE == DR);
#   * street_name_first is the first ALPHA token >2 chars excluding
#     directional prefixes (N/S/E/W/NORTH/SOUTH/EAST/WEST), so a leading
#     directional difference degrades quality to "partial", never "mismatch";
#   * when zip5 is missing on one side, canonical street-type equality may
#     substitute for zip in the 2-of-3 vote (component "street_type");
#   * comparison stays city-independent — city strings are never compared;
#   * a present-and-CONFLICTING street number is decisive: the grid echoed a
#     different property, so extra agreement elsewhere can never outvote it.
# A conflicting directional prefix or any absent base component caps quality
# at "partial"; "exact" still requires number+name+zip present and equal on
# both sides with agreeing directional prefixes.

_DIRECTIONAL_PREFIXES = {"N", "S", "E", "W", "NORTH", "SOUTH", "EAST", "WEST"}
_LEAD_NUM_NAME_RE = re.compile(r"^(\d+)\s+(.+)$")


def _echo_text(address: str) -> str:
    """Collapse nbsp + whitespace runs so token splits survive HTML rendering."""
    return re.sub(r"\s+", " ", str(address or "").replace("\xa0", " ")).strip()


def _first_name_token(words: list[str]) -> str | None:
    """First ALPHA token >2 chars, skipping directional prefixes ("N", "NORTH")."""
    for word in words:
        tok = word.upper().rstrip(".")
        if not tok.isalpha() or len(tok) <= 2 or tok in _DIRECTIONAL_PREFIXES:
            continue
        return tok
    return None


def _directional_prefix(words: list[str]) -> str | None:
    """Leading directional prefix of the street name, canonically uppercased."""
    if words:
        tok = words[0].upper().rstrip(".")
        if tok in _DIRECTIONAL_PREFIXES:
            return tok
    return None


def _canonical_street_type(words: list[str]) -> str | None:
    """Rightmost recognized street type, canonically abbreviated (PLACE->PL)."""
    for word in reversed(words):
        tok = word.upper().rstrip(".")
        if tok in STREET_TYPES:
            return STREET_TYPES[tok]
    return None


def _echo_tokens(address: str) -> dict | None:
    """Normalization-backed tokens for echo comparison (rescue path).

    Both sides route through src.addr.normalize_address so abbreviations,
    whitespace and city-glued segments compare canonically. When the strict
    normalizer rejects the string (zip-less situs lines, scrape garbage) we
    fall back to cleaned raw-token scanning so a partially readable address
    still yields comparable components instead of failing closed.

    Returns {'street_number', 'street_name_first', 'zip5', 'street_type',
    'directional'}; a component is None when absent/unreadable. Returns None
    only for an empty address — all-None components still tokenize but can
    never match, preserving the old garbage-in mismatch behavior.
    """
    cleaned = _echo_text(address)
    if not cleaned:
        return None
    tokens = {
        "street_number": None,
        "street_name_first": None,
        "zip5": None,
        "street_type": None,
        "directional": None,
    }
    norm = normalize_address(cleaned)
    if norm.valid:
        words = norm.street_name.split()
        tokens["street_number"] = norm.street_number or None
        tokens["street_name_first"] = _first_name_token(words)
        tokens["street_type"] = norm.street_type or None
        tokens["zip5"] = norm.zip5 or None
        tokens["directional"] = _directional_prefix(words)
        return tokens
    lead = cleaned.split(",")[0].strip()
    m = _LEAD_NUM_NAME_RE.match(lead)
    if m:
        words = m.group(2).split()
        tokens["street_number"] = m.group(1)
        tokens["street_name_first"] = _first_name_token(words)
        tokens["street_type"] = _canonical_street_type(words)
        tokens["directional"] = _directional_prefix(words)
    zip_match = _ZIP5_RE.search(cleaned.upper())
    if zip_match:
        tokens["zip5"] = zip_match.group(1)
    return tokens


def _address_tokens(address: str) -> dict | None:
    """Split an address into the tokens used for echo comparison.

    Returns {'street_number', 'street_name_first', 'zip5'}; a component is None
    when absent from the input. Returns None for an empty address.
    """
    if not address or not str(address).strip():
        return None
    raw = str(address).strip()
    first_segment = raw.split(",")[0].strip()
    m = re.match(r"^(\d+)\s+(\S+)", first_segment)
    zip_match = _ZIP5_RE.search(raw.upper())
    return {
        "street_number": m.group(1) if m else None,
        "street_name_first": m.group(2).upper() if m else None,
        "zip5": zip_match.group(1) if zip_match else None,
    }


def _extract_displayed_address(soup) -> str:
    """Best-effort extraction of the address displayed on a DCAD page.

    Adapted to live dallascad.org detail pages (probed Aug 2026): the situs
    renders as a bare "Address:" label with its value on the next line,
    using non-breaking spaces and an abbreviated street type.
    """
    if soup is None:
        return ""
    text = soup.get_text("\n").replace("\xa0", " ")
    lines = [ln.strip() for ln in text.splitlines()]

    for idx, ln in enumerate(lines):
        for label_re in _DISPLAYED_ADDRESS_LABELS:
            m = label_re.search(ln)
            if m and m.group(1).strip():
                return m.group(1).strip().strip(":").strip()
        if _ADDRESS_LABEL_ONLY_RE.match(ln):
            # Value lives on one of the next few non-empty lines.
            for nxt in lines[idx + 1: idx + 4]:
                if not nxt:
                    continue
                if _SITUS_LINE_RE.match(nxt) or _FALLBACK_ADDR_LINE_RE.match(nxt):
                    return nxt
                break  # first non-empty line after "Address:" wasn't a situs

    for line in lines:
        if _FALLBACK_ADDR_LINE_RE.match(line) or _SITUS_LINE_RE.match(line):
            return line
    return ""


def _verify_echo(query_address: str, displayed_address: str) -> tuple[bool, str]:
    """Compare the queried address against the detail-page displayed address.

    Normalization-backed token compare over street-number, street-name-first
    and zip5 (R2b rescue: canonical street type may substitute for zip when
    zip is unreadable on one side): at least 2 components must match.
    Returns (ok, match_quality) where match_quality is 'exact' when number,
    name and zip are all present and equal on both sides AND the directional
    prefixes agree; 'partial' for any rescued or incomplete-but-passing
    compare; 'mismatch' when fewer than 2 components match or the street
    numbers conflict outright; 'unverifiable'
    when either side carries no address text at all. City strings are never
    compared (city-independent).
    """
    query = _echo_tokens(query_address)
    shown = _echo_tokens(displayed_address)
    if not query or not shown:
        return False, "unverifiable"
    # Decisive negative (R2b guard): a readable street-number conflict means
    # the grid echoed a different property — street-type/zip agreement can
    # never outvote it ("wrong number stays mismatch").
    if (query["street_number"] and shown["street_number"]
            and query["street_number"] != shown["street_number"]):
        return False, "mismatch"
    matches = 0
    exact_so_far = True
    for component in _ECHO_COMPONENTS:
        qv, dv = query[component], shown[component]
        if qv is None or dv is None:
            exact_so_far = False  # component unverifiable on one side
            continue
        if qv == dv:
            matches += 1
        else:
            exact_so_far = False
    # R2b rescue: zip unreadable on one side -> canonical street-type equality
    # may cast the deciding vote; it never overrides a readable-zip verdict.
    if query["zip5"] is None or shown["zip5"] is None:
        qt, dt = query["street_type"], shown["street_type"]
        if qt is not None and dt is not None and qt == dt:
            matches += 1
    if matches < 2:
        return False, "mismatch"
    if exact_so_far and query["directional"] == shown["directional"]:
        return True, "exact"
    return True, "partial"


def _apply_echo_gate(parsed: dict, query_address: str, displayed_address: str) -> dict:
    """Gate parsed DCAD fields behind R2 echo verification.

    On mismatch the returned payload carries ONLY diagnostic status — no
    property fields ever leak from a wrong property. On success the parsed
    fields gain match_quality ('exact'|'partial').
    """
    ok, quality = _verify_echo(query_address, displayed_address)
    if not ok:
        return {
            "status": "mismatch",
            "error": (
                f"DCAD detail page failed address echo-back verification "
                f"(quality={quality}); property fields withheld"
            ),
            "displayed_address": displayed_address or None,
        }
    gated = dict(parsed)
    gated["match_quality"] = quality
    return gated


# --- RL9: parse-yield probe --------------------------------------------------
# Keys of a parsed payload that describe the parse itself rather than a
# property field — excluded from extracted_fields_count.
_STATS_NON_FIELD_KEYS = frozenset({"status", "match_quality", "derived_source"})

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def dcad_stats_enabled(env: str | None = None) -> bool:
    """True when HERMES_DCAD_STATS opts in (conventionally =1)."""
    value = os.environ.get(DCAD_STATS_ENV, "") if env is None else env
    return value.strip().lower() in _TRUTHY_ENV_VALUES


def detect_derived_year(value_2025, value_2026) -> str | None:
    """FN3: flag the derived side of a 25/24-shaped CAD year pair.

    A pair whose 2026 figure equals 2025 * 25/24 to within display rounding
    (half a dollar) is not two independent appraisals — one figure was
    synthesized from the other with the legacy flat uplift (see module
    docstring). Returns "2026" for such pairs (the newer notice year is
    generated from the prior certified roll), else None. Missing,
    non-positive, or unparseable years are never flagged.
    """
    if value_2025 is None or value_2026 is None:
        return None
    try:
        v25 = float(value_2025)
        v26 = float(value_2026)
    except (TypeError, ValueError):
        return None
    if v25 <= 0 or v26 <= 0:
        return None
    expected_2026 = v25 * LEGACY_UPLIFT_NUM / LEGACY_UPLIFT_DEN
    if abs(v26 - expected_2026) <= _DERIVED_DISPLAY_TOLERANCE_USD:
        return "2026"
    return None


def build_parse_stats(detail_link_found: bool, parsed: dict,
                      echo_quality: str | None) -> dict:
    """RL9: assemble the parse-yield probe payload.

    detail_link_found       — did the results grid expose a detail link?
    extracted_fields_count  — non-None property fields parsed from the page
                              (regex decay shows up as this trending to 0
                              while searches keep succeeding). FN3 raw-string
                              mirrors ("*_raw") are provenance duplicates of
                              their parsed counterpart and are NOT counted.
    echo_quality            — 'exact' | 'partial' | 'mismatch' | 'unverifiable'
    derived_source          — FN3 marker when a CAD year pair carries the
                              legacy 25/24 uplift signature
    """
    fields = sum(
        1 for key, val in parsed.items()
        if key not in _STATS_NON_FIELD_KEYS
        and not key.endswith("_raw")
        and val is not None
    )
    return {
        "detail_link_found": bool(detail_link_found),
        "extracted_fields_count": fields,
        "echo_quality": echo_quality,
        "derived_source": parsed.get("derived_source"),
    }


def attach_parse_stats(result: dict, parsed: dict, query_address: str,
                       displayed_address: str, detail_link_found: bool) -> dict:
    """Attach parse_stats onto *result* when HERMES_DCAD_STATS is enabled.

    No-op (returns *result* untouched) when the probe is disabled, so the
    default result shape stays byte-identical to pre-RL9 behavior.
    """
    if not dcad_stats_enabled():
        return result
    # Re-run the pure echo compare for its quality label; deterministic and
    # cheap, and keeps _apply_echo_gate's contract untouched.
    _, quality = _verify_echo(query_address, displayed_address)
    result["parse_stats"] = build_parse_stats(detail_link_found, parsed, quality)
    return result


async def _search_dcad_by_address(address: str) -> dict:
    """Search DCAD by property address using Playwright.

    Verified live Aug 20 2026:
    - Search URL: https://www.dallascad.org/SearchAddr.aspx
    - Form fields: #txtAddrNum (street number), #txtStName (street name, no suffix)
    - Submit button: input[name="cmdSubmit"] (value="Search")
    - ASP.NET form requires page.type() for proper keyboard events
    - Results page has a grid with a link to property detail
    - Detail page has property characteristics in a table

    R2: results are gated by detail-page echo-back verification — see
    _verify_echo / _apply_echo_gate.
    """
    m = re.match(r'^(\d+)\s+(.+)', address.split(',')[0].strip())
    if not m:
        return {"error": "Cannot parse street number from address"}

    street_num = m.group(1)
    street_name = m.group(2).strip()
    # Remove street suffix (DCAD says: "Do not enter the street type such as Street, Drive or Lane")
    street_name = re.sub(r'\s+(STREET|ST|DRIVE|DR|LANE|LN|COURT|CT|WAY|BOULEVARD|BLVD|AVENUE|AVE|PARKWAY|PKWY|PLACE|PL|ROAD|RD)$', '', street_name, flags=re.IGNORECASE)

    detail_link_found = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto("https://www.dallascad.org/SearchAddr.aspx", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(2000)

            # Use page.type() — sends proper keyboard events for ASP.NET validation
            await page.click("#txtAddrNum")
            await page.type("#txtAddrNum", street_num, delay=50)
            await page.click("#txtStName")
            await page.type("#txtStName", street_name, delay=50)
            await page.wait_for_timeout(500)

            # Click the submit button
            await page.click('input[name="cmdSubmit"]')
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(3000)

            # Check if we're on the results page or error page
            url = page.url
            if "SearchError" in url:
                result = {"error": "DCAD search error — ASP.NET form rejected submission", "status": "error"}
            else:
                html = await page.content()
                soup = BeautifulSoup(html, "html.parser")

                # Look for property detail link in the results grid
                detail_link = soup.find("a", href=re.compile(r"Detail|PropertyDetail|PCD|AcctDetail", re.IGNORECASE))
                detail_link_found = detail_link is not None
                if detail_link:
                    href = detail_link.get("href", "")
                    # Handle relative URLs — DCAD uses relative paths without leading /
                    if not href.startswith("http"):
                        if not href.startswith("/"):
                            href = "/" + href
                        href = f"https://www.dallascad.org{href}"
                    await page.goto(href, timeout=15000)
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    soup = BeautifulSoup(html, "html.parser")

                # R2: verify the page we landed on is actually the queried
                # property before trusting any parsed characteristics.
                displayed = _extract_displayed_address(soup)
                parsed = _parse_dcad_detail(soup)
                result = _apply_echo_gate(parsed, address, displayed)
                # RL9: opt-in parse-yield probe; no-op unless HERMES_DCAD_STATS=1.
                result = attach_parse_stats(result, parsed, address, displayed,
                                            detail_link_found)

        except Exception as e:
            result = {"error": str(e)}
        finally:
            await browser.close()

    return result


def _parse_dcad_detail(soup: BeautifulSoup) -> dict:
    """Parse property characteristics from a DCAD detail page.

    Verified live Aug 20 2026 — DCAD detail page structure:
    - "Main Improvement" section has a table with label/value pairs in td cells
    - Labels: Year Built, Living Area, # Bedrooms, # Baths (Full/Half), # Stories, Pool, Building Class
    - "Value" section has Market Value, Improvement, Land values
    - Fields are in 2-column or 3-column table rows (label, value, label2, value2)
    """
    result = {"status": "parsed"}
    text = soup.get_text()

    # Extract fields using regex on the full text — more robust than table parsing
    # since DCAD uses complex multi-column table layouts

    patterns = {
        "year_built": r'Year Built\s+(\d{4})',
        "improvement_sqft": r'Living Area\s+([\d,]+)\s*sqft',
        "beds": r'# Bedrooms\s+(\d+)',
        "baths": r'# Baths \(Full/Half\)\s*(\d+)\s*/\s*(\d+)',
        "stories": r'# Stories\s+([A-Z\s]+?)(?=\t|\n|$)',
        "pool": r'Pool \(Y/N\)\s*([YN])',
        "building_class": r'Building Class\s+([A-Z\s]+?)(?=\t|\n)',
        "construction_type": r'Construction Type\s+([A-Z\s]+?)(?=\t|\n)',
        "cad_appraised_value": r'Market Value:\s*\$?([\d,]+)',
        "improvement_value": r'Improvement:\s*\$?([\d,]+)',
        "land_value": r'\+\s*\$?([\d,]+)\s*$',
    }

    for field, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            val = m.group(1).strip()
            if field == "baths":
                result[field] = f"{m.group(1)}.{m.group(2)}"  # 3.1 format
            elif field in ("cad_appraised_value", "improvement_value"):
                result[field] = val.replace(",", "")
            else:
                result[field] = val

    # FN3: labeled per-year values — keep the RAW string (auditable provenance:
    # exactly what the page claimed for that year) alongside the parsed float.
    # The legacy unlabeled "Market Value:" capture above is left untouched for
    # backward compatibility.
    year_raw: dict = {}
    year_float: dict = {}
    for m in _YEAR_VALUE_RE.finditer(text):
        year = m.group(1)
        if year in year_raw:
            continue
        year_raw[year] = " ".join(m.group(0).split())
        try:
            year_float[year] = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
    for year in sorted(year_raw):
        result[f"cad_value_{year}_raw"] = year_raw[year]
        if year in year_float:
            result[f"cad_value_{year}"] = year_float[year]

    # FN3: an EXACTLY 25/24 pair is a derived figure, not two observations.
    derived = detect_derived_year(result.get(f"cad_value_{_DERIVED_PAIR_YEARS[0]}"),
                                  result.get(f"cad_value_{_DERIVED_PAIR_YEARS[1]}"))
    if derived:
        result["derived_source"] = derived

    # Also try to extract owner name
    owner_match = re.search(r'Owner \(Current \d+\)\n(.+?)(?=\n)', text)
    if owner_match:
        result["owner"] = owner_match.group(1).strip()

    if len(result) <= 1:
        result["status"] = "no_data_found"

    return result


class DallasCADAdapter(SourceAdapter):
    """Dallas CAD (DCAD) property search adapter."""
    channel = "cad"
    county = "dallas"

    def fetch(self, address: str = "") -> dict:
        """Search DCAD for a single address. Returns property characteristics."""
        if not address:
            return {"error": "No address provided"}
        return asyncio.run(_search_dcad_by_address(address))

    def provenance(self) -> SourceProvenance:
        return SourceProvenance(source="dallas_cad", source_rigor_tier=1)


if __name__ == "__main__":
    # Self-check: address parsing
    m = re.match(r'^(\d+)\s+(.+)', "15806 BREEDLOVE PLACE")
    assert m is not None
    assert m.group(1) == "15806"
    assert m.group(2) == "BREEDLOVE PLACE"

    m2 = re.match(r'^(\d+)\s+(.+)', "Place of Sale: George Allen")
    assert m2 is None  # Not a real address

    # Self-check (R2): token extraction
    tokens = _address_tokens("15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334")
    assert tokens == {"street_number": "15806", "street_name_first": "BREEDLOVE",
                      "zip5": "75001"}, tokens
    assert _address_tokens("no leading number, TX")["street_number"] is None

    # Self-check (R2): echo verification decisions
    query = "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334"
    ok, q = _verify_echo(query, "15806 Breedlove Place, Addison, TX 75001-6334")
    assert ok and q == "exact", (ok, q)
    # zip missing on detail page -> only 2 comparables, both match -> partial pass
    ok, q = _verify_echo(query, "15806 BREEDLOVE PLACE")
    assert ok and q == "partial", (ok, q)
    # wrong zip -> 2 of 3 still match -> partial pass per policy
    ok, q = _verify_echo(query, "15806 BREEDLOVE PLACE, DALLAS, TX 75201")
    assert ok and q == "partial", (ok, q)
    # wrong street number AND wrong zip -> fewer than 2 matches -> mismatch
    ok, q = _verify_echo(query, "999 MAIN ST, DALLAS, TX 75201")
    assert not ok and q == "mismatch", (ok, q)
    # same number, different street name AND zip -> mismatch
    ok, q = _verify_echo(query, "15806 MAIN ST, DALLAS, TX 75201")
    assert not ok and q == "mismatch", (ok, q)
    # blank display -> unverifiable -> fail loud
    assert _verify_echo(query, "")[0] is False

    # Self-check (R2b): echo rescue — normalization-backed compare
    # nbsp-abbreviated live situs ("15806\xa0BREEDLOVE\xa0PL") now passes
    ok, q = _verify_echo(query, "15806\xa0Breedlove\xa0Pl")
    assert ok and q == "partial", (ok, q)
    # directional-prefix difference ignored -> partial, never mismatch
    ok, q = _verify_echo("1425 N CEDAR HILL RD, CEDAR HILL, TX 75104",
                         "1425 Cedar Hill Rd")
    assert ok and q == "partial", (ok, q)
    ok, q = _verify_echo("1425 N CEDAR HILL RD, CEDAR HILL, TX 75104",
                         "1425 Cedar Hill Rd, Cedar Hill, TX 75104")
    assert ok and q == "partial", (ok, q)  # prefix conflict caps at partial
    # missing zip rescued by canonical street-type equality (name token damaged)
    ok, q = _verify_echo("2002 BERKLEY AVENUE DALLAS, TX 75224", "2002 Barkley Ave")
    assert ok and q == "partial", (ok, q)
    # ...but street type never overrides a readable, differing zip
    ok, q = _verify_echo("2002 BERKLEY AVENUE DALLAS, TX 75224",
                         "2002 Barkley Ave, Dallas, TX 75201")
    assert not ok and q == "mismatch", (ok, q)

    # Self-check (R2): the gate strips property fields on mismatch
    parsed_ok = {"status": "parsed", "year_built": "2004", "improvement_sqft": "2,100"}
    gated = _apply_echo_gate(parsed_ok, query, "15806 Breedlove Pl, Addison, TX 75001")
    assert gated["status"] == "parsed" and gated["year_built"] == "2004"
    assert gated["match_quality"] in ("exact", "partial")
    gated_bad = _apply_echo_gate(dict(parsed_ok), query, "42 OTHER ST, DENISON, TX 75020")
    assert gated_bad["status"] == "mismatch"
    for leak in ("year_built", "improvement_sqft", "beds", "baths",
                 "cad_appraised_value", "match_quality"):
        assert leak not in gated_bad, f"property field {leak} leaked past gate"

    # Self-check (FN3): labeled per-year values emit raw strings + floats.
    detail_html = """
    <html><body><table>
      <tr><td>Year Built</td><td>2004</td></tr>
      <tr><td>Living Area</td><td>2,100 sqft</td></tr>
      <tr><td># Bedrooms</td><td>3</td></tr>
      <tr><td>Market Value:</td><td>$730,673</td></tr>
      <tr><td>Appraised Value (2025)</td><td>: $749,180</td></tr>
      <tr><td>Appraised Value (2026)</td><td>: $780,437.50</td></tr>
    </table></body></html>
    """
    parsed_years = _parse_dcad_detail(BeautifulSoup(detail_html, "html.parser"))
    assert parsed_years["cad_appraised_value"] == "730673"  # legacy capture intact
    assert parsed_years["cad_value_2025_raw"] == "Appraised Value (2025): $749,180"
    assert parsed_years["cad_value_2025"] == 749180.0
    assert parsed_years["cad_value_2026_raw"].startswith("Appraised Value (2026)")
    assert parsed_years["cad_value_2026"] == 780437.5, \
        f"cents must survive the year-value parse: {parsed_years['cad_value_2026']!r}"
    assert "derived_source" not in parsed_years, \
        "a non-25/24 pair must never be flagged as derived"

    # Self-check (FN3): a cent-ROUNDED legacy 25/24 signature is still flagged.
    uplift_html = detail_html.replace("$780,437.50", "$780,395.83")  # 749180*25/24 -> 780395.8333
    parsed_uplift = _parse_dcad_detail(BeautifulSoup(uplift_html, "html.parser"))
    assert parsed_uplift.get("derived_source") == "2026", parsed_uplift.get("derived_source")
    # ...and so is one rounded to whole dollars ($780,396).
    dollar_html = detail_html.replace("$780,437.50", "$780,396")
    parsed_dollar = _parse_dcad_detail(BeautifulSoup(dollar_html, "html.parser"))
    assert parsed_dollar.get("derived_source") == "2026", parsed_dollar.get("derived_source")

    # Self-check (FN3): unlabeled page gains none of the new keys (back-compat).
    plain_html = "<html><body>Market Value: $200,000 Year Built 1990</body></html>"
    parsed_plain = _parse_dcad_detail(BeautifulSoup(plain_html, "html.parser"))
    for key in parsed_plain:
        assert not key.startswith("cad_value_2"), key
    assert "derived_source" not in parsed_plain

    # Self-check (RL9): env-gated stats — off by default, no shape change.
    import os as _os
    _saved = _os.environ.pop(DCAD_STATS_ENV, None)
    try:
        assert dcad_stats_enabled() is False
        res_off = attach_parse_stats({"status": "parsed"}, parsed_ok, query,
                                     query, True)
        assert "parse_stats" not in res_off, "stats leaked while probe disabled"
        _os.environ[DCAD_STATS_ENV] = "1"
        assert dcad_stats_enabled() is True
        ok_q = _verify_echo(query, "15806 Breedlove Pl, Addison, TX 75001")[1]
        res_on = attach_parse_stats({"status": "parsed"}, parsed_ok, query,
                                    "15806 Breedlove Pl, Addison, TX 75001", True)
        stats = res_on["parse_stats"]
        assert set(stats) == {"detail_link_found", "extracted_fields_count",
                              "echo_quality", "derived_source"}, set(stats)
        assert stats["detail_link_found"] is True
        assert stats["echo_quality"] == ok_q
        assert stats["extracted_fields_count"] == 2  # year_built + improvement_sqft
        assert stats["derived_source"] is None

        # derived_source mirrors into parse_stats (FN3 x RL9)
        res_derived = attach_parse_stats(
            {"status": "parsed"},
            {"status": "parsed", "cad_value_2025": 749180.0,
             "cad_value_2026": 780395.8333333334, "derived_source": "2026"},
            query, query, False)
        assert res_derived["parse_stats"]["derived_source"] == "2026"

        # FN3 raw-string mirrors are provenance duplicates: they must not
        # inflate extracted_fields_count (RL9 yield stays a decay signal).
        res_raw = attach_parse_stats(
            {"status": "parsed"},
            {"status": "parsed", "year_built": "2004",
             "cad_value_2025": 749180.0, "cad_value_2025_raw":
                 "Appraised Value (2025): $749,180"},
            query, query, True)
        assert res_raw["parse_stats"]["extracted_fields_count"] == 2

        # a mismatch still reports its quality through the probe
        res_mm = attach_parse_stats(
            {"status": "mismatch", "error": "gate"},
            {"status": "parsed"}, query, "999 OTHER ST, DALLAS, TX 75201", True)
        assert res_mm["parse_stats"]["echo_quality"] == "mismatch"

        # only explicit opt-in values enable; "0"/"" stay off
        for off in ("0", "", "false", "no"):
            _os.environ[DCAD_STATS_ENV] = off
            assert dcad_stats_enabled() is False, off
    finally:
        if _saved is None:
            _os.environ.pop(DCAD_STATS_ENV, None)
        else:
            _os.environ[DCAD_STATS_ENV] = _saved

    print("dallas_cad.py: address parsing self-check passed")
    print("dallas_cad.py: R2 echo-back verification self-check passed")
    print("dallas_cad.py: R2b echo rescue (abbreviation/city-glue) self-check passed")
    print("dallas_cad.py: FN3 per-year raw strings + exact-25/24 derived flag self-check passed")
    print("dallas_cad.py: RL9 HERMES_DCAD_STATS parse-yield probe self-check passed")
    print("dallas_cad.py: live DCAD search requires browser (Playwright) — skipping live test")