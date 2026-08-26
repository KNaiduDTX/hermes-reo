# src/adapters/dallas_foreclosure.py
"""Dallas County foreclosure-notice adapter — PORTED from lucid-noether.

Dallas serves monthly PDF bundles by city. The portal HTML has accordion-style
blockquote elements with PDF links. Each PDF contains multiple notices that
must be split and parsed.

Source: /Users/knaidu/Documents/antigravity/lucid-noether/dallas_crawler.py

FIXES applied vs prior attempt:
- City extraction: use comma-separated address pattern, not street-suffix matching
- Debt extraction: expanded regex patterns for more notice formats
- No fabricated appraised values — caller must cross-reference DCAD separately
- Path traversal (P1): percent-decoded PDF filenames are sanitized and
  realpath-contained under local_dir; traversal attempts raise ValueError
- No fabrication (E2): estimated_default_balance/sale_date/sale_time fall
  back to None when not stated in the notice; lender is verbatim only —
  no invented legal suffixes, no brand-name guessing
- Shape-change marker (RL2): a portal redesign that drops #temp-region-7
  or the blockquote[id^=display] accordions raises DallasPortalShapeError
  instead of silently returning [] (a silent [] exits 0 and reads as
  "zero foreclosures this month"); export_portal_health() reports
  {links_found, pdfs_downloaded, parsed_ok} without raising
- PDF quarantine (RL3): cached PDFs are validated (size > 1KB AND %PDF
  header) before reuse; poisoned caches are moved to data/quarantine/ and
  re-downloaded, and per-PDF split/process failures are tallied into the
  fetch report {"pdf_failures": [...]} instead of crashing the county loop
"""
from __future__ import annotations
import os
import re
import time
import urllib.request
import urllib.parse
from bs4 import BeautifulSoup
import pypdf

from .base import SourceAdapter, NormalizedLead, SourceProvenance
from ..source_registry import assert_registry_sync, require_endpoint

# TD3: derived from SOURCE-REGISTRY at import — no hardcoded portal literals.
# PORTAL_URL is the full foreclosures.php page; BASE_URL is the county site
# root that relative PDF hrefs resolve against.
PORTAL_URL = require_endpoint("dallas", "foreclosure_notice").url
BASE_URL = require_endpoint("dallas", "county_site").url
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# RL2: raised when the portal HTML no longer matches the structure this
# parser was built against. Callers must treat it as "marker moved or
# redesign", never as an empty foreclosure month.
class DallasPortalShapeError(Exception):
    """Dallas foreclosure portal HTML lost its expected parse anchors."""


# RL3: cached-PDF validation floor and quarantine destination. A file that
# fails EITHER check (truncated pre-fix download, HTML error page saved as
# .pdf, crash mid-write) is moved aside and re-downloaded — it used to be
# handed to PdfReader forever, crashing the whole county loop.
QUARANTINE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "quarantine")
MIN_PDF_BYTES = 1024  # "size > 1KB"
_PDF_HEADER = b"%PDF-"

# Known DFW cities for address parsing (sorted by length for longest-match-first)
KNOWN_CITIES = sorted([
    "Balch Springs", "Farmers Branch", "Glenn Heights", "Grand Prairie",
    "Addison", "Allen", "Balch", "Carrollton", "Coppell", "Dallas",
    "DeSoto", "Duncanville", "Frisco", "Garland", "Hutchins", "Irving",
    "Lancaster", "Mesquite", "Plano", "Richardson", "Rowlett", "Sachse",
    "Seagoville", "Wilmer", "Cedar Hill", "Desoto", "Duncanville",
    "Lancaster", "Mesquite", "Seagoville",
], key=len, reverse=True)


def _clean(text: str) -> str:
    if not text:
        return "Unknown"
    return " ".join(text.split()).strip()


def _fix_zip(text: str) -> str:
    """Fix OCR typos like 7S001 -> 75001."""
    return re.sub(r'\b(7)[sS](\d{3})\b', r'\g<1>5\g<2>', text)


def _extract_city(address: str, filename_city: str = "") -> str:
    """Extract city from address using comma-separated pattern.

    FIX vs prior attempt: the old code matched against a known-cities list
    using word boundaries, which leaked street suffixes ("Court", "Way",
    "Lane", "Drive"). This version extracts from the address pattern
    "<street>, <city>, TX <zip>" after the comma.
    """
    if not address or address == "Unknown":
        return filename_city or "Unknown"

    # Try comma-separated: "123 Main St, Dallas, TX 75201"
    parts = [p.strip() for p in address.split(",")]
    if len(parts) >= 3:
        # Second-to-last part before TX is the city
        city_candidate = parts[-2]
        if city_candidate and "TX" not in city_candidate.upper():
            return city_candidate

    # Fallback: search for known city names in the address
    addr_clean = " ".join(address.split()).replace(",", " ")
    for c in KNOWN_CITIES:
        if re.search(r'\b' + re.escape(c) + r'\b', addr_clean, re.IGNORECASE):
            return c

    return filename_city or "Unknown"


def fetch_portal_html() -> bytes:
    """Fetch the Dallas County foreclosure portal HTML."""
    req = urllib.request.Request(PORTAL_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def parse_portal_links(html: bytes) -> list[dict]:
    """Parse the portal HTML for PDF links.

    Ported from lucid-noether dallas_crawler.py lines 48-100.
    Portal has accordion-style blockquotes with IDs starting with "display".

    RL2: missing #temp-region-7 or zero blockquote[id^=display] accordions
    raises DallasPortalShapeError — a redesign must never masquerade as an
    empty month. An intact structure that merely carries no .pdf links yet
    still returns [] (that IS an empty month, not a shape change).
    """
    soup = BeautifulSoup(html, "html.parser")
    region = soup.find(id="temp-region-7")
    if not region:
        raise DallasPortalShapeError(
            "Dallas foreclosure portal shape changed: #temp-region-7 is "
            "missing from the page HTML — parser anchors need re-verification"
        )

    bqs = region.find_all(
        lambda tag: tag.name == "blockquote" and tag.get("id", "").startswith("display")
    )
    if not bqs:
        raise DallasPortalShapeError(
            "Dallas foreclosure portal shape changed: zero "
            "blockquote[id^=display] accordions under #temp-region-7 — "
            "parser anchors need re-verification"
        )

    links = []
    for bq in bqs:
        month_label_el = bq.find_previous(["strong", "h3", "h4", "p"])
        month = _clean(month_label_el.get_text()) if month_label_el else "Unknown"
        if month:
            month = month.split()[0].replace(":", "").strip()

        for a in bq.find_all("a"):
            href = a.get("href", "")
            if not href.lower().endswith(".pdf"):
                continue
            url = f"{BASE_URL}{href}" if href.startswith("/") else href
            filename = a.get_text(strip=True)
            city_name = filename.split("_")[0].split("-")[0].replace("_", " ").strip()
            city_name = city_name.split(".")[0].strip()
            links.append({"month": month, "city": city_name, "filename": filename, "url": url})

    return links


def is_valid_pdf(local_path: str, min_size: int = MIN_PDF_BYTES) -> bool:
    """RL3: a reusable cached PDF must exceed 1KB AND start with a %PDF header.

    Anything else on disk (truncated pre-fix download, an HTML error page
    saved under a .pdf name) is poison for PdfReader. Unreadable/missing
    files are invalid by definition — never raise.
    """
    try:
        if os.path.getsize(local_path) <= min_size:
            return False
        with open(local_path, "rb") as f:
            return f.read(len(_PDF_HEADER)) == _PDF_HEADER
    except OSError:
        return False


def quarantine_pdf(local_path: str, quarantine_dir: str = QUARANTINE_DIR) -> str:
    """Move a poisoned cached PDF into *quarantine_dir* and return its new path.

    The move is atomic within the same filesystem; name collisions in the
    quarantine directory get a .quarantined-N suffix so history is preserved.
    """
    os.makedirs(quarantine_dir, exist_ok=True)
    base = os.path.basename(local_path)
    dest = os.path.join(quarantine_dir, base)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(quarantine_dir, f"{base}.quarantined-{n}")
        n += 1
    os.replace(local_path, dest)
    return dest


def download_pdf(url: str, local_dir: str,
                 quarantine_dir: str | None = None,
                 quarantine_log: list | None = None) -> str | None:
    """Download a PDF to local_dir. Returns local path or None.

    Security (finding P1 — path traversal): the filename comes from external
    portal content and is percent-decoded BEFORE use, so "%2e%2e%2f" style
    URLs previously escaped local_dir. The decoded name is now reduced via
    basename, dot-dot/separator residue is rejected outright, and the final
    target must resolve inside local_dir (realpath containment) — otherwise
    ValueError is raised loudly instead of writing outside the directory.
    Network failures still return None (pre-existing contract).

    RL3 cache validation: an existing file is reused only when it passes
    is_valid_pdf (size > 1KB AND %PDF header); a poisoned cache entry is
    moved to *quarantine_dir* (default QUARANTINE_DIR, i.e. data/quarantine/)
    and re-downloaded. Quarantine moves are appended to *quarantine_log*
    (when given) as {"path", "quarantined_to", "reason"} so callers can
    surface healed-cache events.
    """
    os.makedirs(local_dir, exist_ok=True)

    # Basename AFTER percent-decoding (the decoded form is what the OS sees).
    filename = os.path.basename(urllib.parse.unquote(url.split("/")[-1]))
    if (not filename or filename in (".", "..")
            or "/" in filename or "\\" in filename
            or ".." in filename):
        raise ValueError(
            f"Refusing unsafe PDF filename derived from URL: {url!r} "
            f"(decoded basename {filename!r})"
        )

    local_path = os.path.join(local_dir, filename)

    # Enforce containment even if local_dir itself contains symlinks.
    real_base = os.path.realpath(local_dir)
    real_target = os.path.realpath(local_path)
    if os.path.commonpath([real_target, real_base]) != real_base:
        raise ValueError(
            f"Refusing PDF download escaping {local_dir!r}: resolved target "
            f"{real_target!r} from URL {url!r}"
        )

    if os.path.exists(local_path):
        if is_valid_pdf(local_path):
            return local_path
        quarantined_to = quarantine_pdf(local_path, quarantine_dir or QUARANTINE_DIR)
        if quarantine_log is not None:
            quarantine_log.append({
                "path": local_path,
                "quarantined_to": quarantined_to,
                "reason": (
                    "invalid cached PDF (must be > 1KB and start with a "
                    "%PDF- header)"
                ),
            })
        # Fall through: the poisoned cache entry is gone — re-download fresh.

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        time.sleep(1.0)  # rate limit
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read()
        # Atomic write (audit D2): a crash mid-download previously left a
        # truncated PDF on disk that later parsing would treat as real.
        from ..io_utils import write_bytes
        return write_bytes(local_path, payload)
    except ValueError:
        raise
    except Exception:
        return None


def split_pdf_pages(local_path: str) -> list[list]:
    """Split a multi-page PDF into individual notice groups.

    Ported from lucid-noether dallas_crawler.py lines 132-186.
    Uses regex boundary detection for notice starts.
    """
    reader = pypdf.PdfReader(local_path)
    new_notice_keywords = [
        r"NOTICE OF [A-Z ]*SALE",
        r"NOTICE OF ACCELERATION",
        r"NOTICE OF FORECLOSURE SALE",
        r"ASSERT AND PROTECT YOUR RIGHTS",
        r"DEED OF TRUST INFORMATION",
        r"APPOINTMENT OF SUBSTITUTE TRUSTEE",
    ]
    new_notice_regex = re.compile("|".join(new_notice_keywords), re.IGNORECASE)

    notice_groups = []
    current_group = []

    for page_idx, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        is_new = False

        if page_idx == 0:
            is_new = True
        else:
            has_keyword = bool(new_notice_regex.search(text))
            has_filing_stamp = "FILED" in text and ("PM" in text or "AM" in text)
            first_lines = [l.strip() for l in text.split("\n")[:4] if l.strip()]
            starts_with_header = any(new_notice_regex.search(fl) for fl in first_lines)
            if has_filing_stamp or starts_with_header or (has_keyword and "Page 1" in text):
                is_new = True

        if is_new and current_group:
            notice_groups.append(current_group)
            current_group = []
        current_group.append(page)

    if current_group:
        notice_groups.append(current_group)

    return notice_groups


def _clean_address(raw_addr: str, full_text: str = "") -> str:
    """Clean and validate a parsed address.

    Rejects courthouse/sale-location addresses, PO boxes, and non-addresses.
    Cleans OCR artifacts. Returns "Unknown" if not a real property address.
    """
    if not raw_addr or raw_addr == "Unknown":
        return "Unknown"

    addr = raw_addr.strip()

    # Reject courthouse/sale-location addresses
    for pat in [r"Commerce Street", r"George Allen", r"Courts Building",
                r"courthouse", r"commissioners? court", r"Bloomdale",
                r"designated by the", r"Place of Sale"]:
        if re.search(pat, addr, re.IGNORECASE):
            return "Unknown"

    # Reject PO boxes
    if re.search(r"PO Box|P\.O\. Box", addr, re.IGNORECASE):
        return "Unknown"

    # Reject "STATE OF TEXAS" etc.
    if re.match(r"^(STATE OF|COUNTY OF|CITY OF)\s", addr, re.IGNORECASE):
        return "Unknown"

    # Clean OCR artifacts: "I 4160" → "4160"
    addr = re.sub(r'^I\s+', '', addr)
    # Fix "I 4 I 60" → "4160" (OCR misreads digits with I)
    addr = re.sub(r'^(\d)\s+I\s+(\d{2})', r'\1\2', addr)

    # If no street number, try extracting from "Property Address:" prefix in full text
    if not re.match(r'^\d+\s', addr) and full_text:
        m = re.search(
            r'(?:Property Address|Reported Address|PROPERTY ADDRESS|ADDRESS)\s*[:.]?\s*(\d+\s+[^\n]+)',
            full_text, re.IGNORECASE
        )
        if m:
            addr = _clean(m.group(1).strip())

    # Require a street number at the start
    if not re.match(r'^\d+\s', addr):
        return "Unknown"

    # Clean up
    addr = re.sub(r'\s+', ' ', addr).strip()
    addr = re.sub(r'\s+Property County.*$', '', addr, flags=re.IGNORECASE)

    return addr


def parse_notice_text(text: str, city: str, month: str) -> dict:
    """Parse a single notice's text for structured data.

    Ported from lucid-noether dallas_crawler.py lines 188-342.
    FIXES: improved city extraction, expanded debt regex patterns,
    address cleaning (reject courthouse/PO box/OCR garbage).

    NO FABRICATION (finding E2) — this docstring is now true:
    - debt/estimated_default_balance comes ONLY from labeled amounts;
      unlabeled first-dollar guesses are gone -> falls back to None
      ("all_extracted_balances" stays as the candidate list).
    - sale_date comes ONLY from an explicitly stated sale date in the
      notice — the old "First Tuesday of {month} 2026" invention is gone
      -> None when unparseable/unstated.
    - sale_time comes ONLY from a time printed in the notice -> None.
    - lender is returned verbatim from the notice text; legal suffixes
      like "N.A." are NEVER appended and lenders are never guessed from
      brand-name mentions.
    """
    text_fixed = _fix_zip(text)

    # 1. Address — try "Property Address:" prefix first, then zip-code anchored
    address = "Unknown"

    # Try "Property Address:" / "Reported Address:" prefix
    addr_match = re.search(
        r'(?:Property Address|Reported Address|PROPERTY ADDRESS|ADDRESS)\s*[:.]?\s*([^\n]+)',
        text_fixed, re.IGNORECASE
    )
    if addr_match and len(addr_match.group(1).strip()) > 3:
        address = _clean(addr_match.group(1))

    # If prefix didn't work, try zip-code anchored
    if address == "Unknown":
        zip_match = re.search(r'\b(75\d{3})(?:-\d{4})?\b', text_fixed)
        if zip_match:
            zip_code = zip_match.group(0)
            lines = text_fixed.split("\n")
            for idx, line in enumerate(lines):
                if zip_code in line:
                    combined = line
                    if len(line.strip()) < 20 and idx > 0:
                        combined = lines[idx - 1] + " " + line
                    address = _clean(combined)
                    break
            if address != "Unknown" and zip_code not in address:
                address = f"{address}, TX {zip_code}"

    # Final fallback: street-pattern regex
    if address == "Unknown":
        addr_match = re.search(
            r'(\d+\s+[A-Za-z0-9\s\.\#\-]+(?:PLACE|PL|ROAD|RD|DRIVE|DR|STREET|ST|LANE|LN|COURT|CT|WAY|BOULEVARD|BLVD|AVENUE|AVE|PARKWAY|PKWY)\s*,\s*[^,\n]+)',
            text_fixed, re.IGNORECASE
        )
        if addr_match:
            address = _clean(addr_match.group(1))

    # Apply final address cleaning with full text context
    address = _clean_address(address, text_fixed)

    # FIX: extract city from the parsed address, not from street-suffix matching
    parsed_city = _extract_city(address, city)
    if address != "Unknown" and parsed_city.lower() not in address.lower():
        if "TX" in address and ", TX" in address:
            # Insert city before TX if not present
            address = re.sub(r',\s*TX', f', {parsed_city}, TX', address)

    # 2. Lender / Mortgagee — VERBATIM from the notice only (finding E2).
    # No brand-name guessing, no invented legal suffixes ("N.A." etc.).
    lender = None
    lender_match = re.search(
        r'(?:Original Mortgagee:|Current Mortgagee:|Beneficiary:|Noteholder:|Lender:)[ \t]*([^\n]+)',
        text_fixed, re.IGNORECASE
    )
    if lender_match:
        matched = _clean(lender_match.group(1))
        if "NOMINEE FOR" in text_fixed.upper():
            actual = re.search(r'NOMINEE FOR\s+([^,\n]+)', text_fixed, re.IGNORECASE)
            lender = _clean(actual.group(1)) if actual else matched
        else:
            lender = matched
    elif "NOMINEE FOR" in text_fixed.upper():
        actual = re.search(r'NOMINEE FOR\s+([^,\n]+)', text_fixed, re.IGNORECASE)
        if actual:
            lender = _clean(actual.group(1))

    # 3. Borrower / Grantor
    borrower = "Unknown"
    grantor_match = re.search(
        r'(?:Grantor\(s\):|Grantor:|Borrower\(s\):|Borrower:)\s*([^\n]+)',
        text_fixed, re.IGNORECASE
    )
    if grantor_match:
        borrower = _clean(grantor_match.group(1))

    # 4. Debt — labeled amounts ONLY (finding E2). The old fallback that took
    # the first large dollar figure in the notice (which could be taxes,
    # costs, or another paragraph) is removed: debt stays None when no
    # labeled amount matches. all_extracted_balances keeps every candidate.
    debt = None
    for pattern in [
        r'(?:Original Principal:|Principal Debt:|Debt Amount:|Unpaid Principal Balance:|Current Principal Balance:)\s*\D*([0-9,]{5,10}\.[0-9]{2})',
        r'(?:Original Principal|Note Amount|Original Debt|Unpaid Principal|Unpaid Balance|Current Principal)[^\d$]*\$?\s*([0-9,]{5,10}(?:\.[0-9]{2})?)',
    ]:
        m = re.search(pattern, text_fixed, re.IGNORECASE)
        if m:
            try:
                debt = float(m.group(1).replace(",", ""))
                break
            except ValueError:
                pass

    # All extracted balances
    all_balances = []
    for amt in re.findall(r'\$\s*([0-9,]{5,10}(?:\.[0-9]{2})?)', text_fixed):
        try:
            val = float(amt.replace(",", ""))
            if val not in all_balances:
                all_balances.append(val)
        except ValueError:
            pass

    # 5. Sale date and time — ONLY as explicitly stated in the notice
    # (finding E2). No "First Tuesday of {month}" invention, no default
    # sale time: both fall back to None when absent/unparseable.
    sale_date = None
    date_match = re.search(
        r'(Tuesday|Wednesday|Thursday|Friday|Monday),\s*the\s*\d+\w*\s*day\s*of\s*[A-Za-z]+,\s*202\d',
        text_fixed, re.IGNORECASE
    )
    if date_match:
        sale_date = _clean(date_match.group(0))

    sale_time = None
    time_match = re.search(
        r'\b(\d{1,2}:\d{2}\s*(?:AM|PM|A\.M\.|P\.M\.))\b[^\n]*',
        text_fixed, re.IGNORECASE
    )
    if time_match:
        sale_time = _clean(time_match.group(0))

    # 6. Trustee
    trustee = "Substitute Trustee"
    trustee_match = re.search(
        r'Substitute\s+T[a-z]{4,6}e[s\(\)a-z]*:\s*([^\n]+)',
        text_fixed, re.IGNORECASE
    )
    if trustee_match:
        trustee = _clean(trustee_match.group(1))

    return {
        "property_address": address,
        "city": parsed_city,
        "borrower": borrower,
        "lender": lender,
        "debt": debt,
        "all_extracted_balances": all_balances,
        "sale_date": sale_date,
        "sale_time": sale_time,
        "trustee": trustee,
    }


def _ocr_status(debt, address: str, lender, all_balances: list) -> str:
    """Classify OCR extraction quality (finding E2).

    Success  — the labeled estimated_default_balance was extracted.
    Partial  — no balance, but real primaries (address/lender/candidates) exist.
    NoDebt   — nothing meaningful extracted.
    """
    if debt is not None:
        return "Success"
    has_primary = any([
        address not in (None, "", "Unknown"),
        lender not in (None, ""),
        bool(all_balances),
    ])
    return "Partial" if has_primary else "NoDebt"


def process_dallas_pdf(item: dict, temp_dir: str,
                       quarantine_dir: str | None = None,
                       quarantine_log: list | None = None,
                       failure_sink: list | None = None) -> list[dict]:
    """Download, split, and parse a single city PDF.

    RL3: *quarantine_dir*/*quarantine_log* pass through to download_pdf for
    poisoned-cache handling. When *failure_sink* (a list) is given, a failed
    download is tallied into it as a {"stage": "download", ...} entry instead
    of silently returning []; split/parse exceptions still propagate so the
    caller's batch wrapper can attribute them to the "process" stage.
    """
    local_path = download_pdf(item["url"], temp_dir,
                              quarantine_dir=quarantine_dir,
                              quarantine_log=quarantine_log)
    if not local_path:
        if failure_sink is not None:
            failure_sink.append({
                "url": item.get("url"),
                "filename": item.get("filename"),
                "city": item.get("city"),
                "month": item.get("month"),
                "stage": "download",
                "error": "PDF not retrieved (network failure or refused)",
            })
        return []

    notice_groups = split_pdf_pages(local_path)
    records = []

    for idx, page_group in enumerate(notice_groups, 1):
        full_text = ""
        for page in page_group:
            full_text += (page.extract_text() or "") + "\n"

        details = parse_notice_text(full_text, item["city"], item["month"])
        entry_id = f"DAL_{item['month']}_{item['city']}_{idx}"
        debt = details["debt"]

        records.append({
            "entry_id": entry_id,
            "property_address": details["property_address"],
            "city": details["city"],
            "county": "Dallas",
            "details": {
                "property_info": {
                    "Address": details["property_address"],
                    # Sale date as stated in the notice; may be None.
                    "Deed Date": details["sale_date"],
                    "Commercial": "True" if (debt is not None and debt >= 1_000_000) else "False",
                },
                "owner_info": {
                    "Name": details["borrower"],
                    "Address": details["property_address"],
                },
                # Keys preserved; values may be None when absent from the
                # notice — never fabricated (finding E2).
                "scanned_notice_ocr_data": {
                    "foreclosing_lender": details["lender"],
                    "substitute_trustee": details["trustee"],
                    "scheduled_sale_time": details["sale_time"],
                    "estimated_default_balance": debt,
                    "all_extracted_balances": details["all_extracted_balances"],
                    "ocr_engine_mode": "pypdf Text Extractor + Regex",
                    "status": _ocr_status(
                        debt,
                        details["property_address"],
                        details["lender"],
                        details["all_extracted_balances"],
                    ),
                },
            },
        })

    return records


def process_pdf_batch(items: list[dict], temp_dir: str,
                      quarantine_dir: str | None = None) -> tuple[list[dict], dict]:
    """Process portal PDF items one per try — a bad PDF never kills the run (RL3).

    Previously an unguarded PdfReader exception (corrupt/truncated download)
    aborted the whole county loop mid-batch. Every item now gets its own
    try/except; failures are tallied per PDF. Returns
    (records, report) where report carries:
      {"pdf_failures": [{url, filename, city, month, stage, error}, ...],
       "pdf_quarantined": [{path, quarantined_to, reason}, ...]}
    """
    records: list[dict] = []
    failures: list[dict] = []
    quarantined: list[dict] = []
    for item in items:
        sink: list = []
        qlog: list = []
        try:
            recs = process_dallas_pdf(item, temp_dir,
                                      quarantine_dir=quarantine_dir,
                                      quarantine_log=qlog,
                                      failure_sink=sink)
            records.extend(recs)
        except Exception as exc:
            failures.append({
                "url": item.get("url"),
                "filename": item.get("filename"),
                "city": item.get("city"),
                "month": item.get("month"),
                "stage": "process",
                "error": f"{type(exc).__name__}: {exc}",
            })
        finally:
            failures.extend(sink)
            quarantined.extend(qlog)
    return records, {"pdf_failures": failures, "pdf_quarantined": quarantined}


class DallasForeclosureAdapter(SourceAdapter):
    """Dallas County foreclosure-notice portal adapter."""
    channel = "foreclosure_notice"
    county = "dallas"

    def fetch_with_report(self, limit: int | None = None,
                          temp_dir: str = "data/dallas_pdfs",
                          quarantine_dir: str | None = None
                          ) -> tuple[list[NormalizedLead], dict]:
        """fetch() plus the RL3 per-PDF health report.

        Returns (leads, report) where report is:
          {"links_found": int,
           "pdf_failures": [...],      # per-PDF download/split/process failures
           "pdf_quarantined": [...]}   # poisoned cache entries moved aside

        RL2: a portal redesign raises DallasPortalShapeError out of this
        method (via parse_portal_links) instead of yielding [].
        """
        html = fetch_portal_html()
        pdf_links = parse_portal_links(html)  # raises on shape change (RL2)
        links_found = len(pdf_links)
        if limit:
            pdf_links = pdf_links[:limit]

        records, report = process_pdf_batch(pdf_links, temp_dir,
                                            quarantine_dir=quarantine_dir)
        report["links_found"] = links_found

        leads = [
            NormalizedLead(
                county="dallas",
                parcel_id=r["entry_id"],
                channel="foreclosure_notice",
                situs_address=r["property_address"],
                sale_date=r["details"]["property_info"].get("Deed Date"),
                owner_name=r["details"]["owner_info"].get("Name"),
                raw_data=r,
            )
            for r in records
        ]
        return leads, report

    def fetch(self, limit: int | None = None, temp_dir: str = "data/dallas_pdfs") -> list[NormalizedLead]:
        """Fetch foreclosure listings from Dallas County PDFs.

        Raises DallasPortalShapeError when the portal layout changed (RL2) —
        an empty lead list now always means "intact portal, genuinely no
        notices parsed". Per-PDF processing failures are tallied, not fatal;
        call fetch_with_report() for the {"pdf_failures": [...]} detail.
        """
        leads, _ = self.fetch_with_report(limit=limit, temp_dir=temp_dir)
        return leads

    def provenance(self) -> SourceProvenance:
        return SourceProvenance(source="dallas_foreclosure_notice", source_rigor_tier=0)


def export_portal_health(limit: int | None = None,
                         temp_dir: str = "data/dallas_pdfs",
                         quarantine_dir: str | None = None) -> dict:
    """RL2: probe portal shape + PDF-pipeline health WITHOUT raising.

    Returns {"links_found", "pdfs_downloaded", "parsed_ok"}:
      links_found    — PDF links parsed from the portal HTML (0 on any
                       shape/network failure)
      pdfs_downloaded — PDFs present on disk after download/validation
      parsed_ok      — PDFs that produced at least one notice record
    Intended for run summaries / CI smoke checks so a silent scrape collapse
    is observable as a red counter instead of a clean exit.
    """
    health = {"links_found": 0, "pdfs_downloaded": 0, "parsed_ok": 0}
    try:
        html = fetch_portal_html()
        pdf_links = parse_portal_links(html)
    except Exception:
        # Shape change or network outage — the probe reports, never throws.
        return health
    health["links_found"] = len(pdf_links)
    if limit:
        pdf_links = pdf_links[:limit]
    for item in pdf_links:
        try:
            local_path = download_pdf(item["url"], temp_dir,
                                      quarantine_dir=quarantine_dir)
        except Exception:
            continue
        if not local_path:
            continue
        health["pdfs_downloaded"] += 1
        try:
            if process_dallas_pdf(item, temp_dir, quarantine_dir=quarantine_dir):
                health["parsed_ok"] += 1
        except Exception:
            continue
    return health


if __name__ == "__main__":
    import tempfile
    from src.dates import parse_date_any

    # Self-check (TD3): PORTAL_URL/BASE_URL are registry-derived; assert the
    # whole adapter-constant <-> REGISTRY binding table holds.
    assert_registry_sync()
    print("dallas_foreclosure.py: PASS — portal URLs derived from SOURCE-REGISTRY "
          "(assert_registry_sync)")

    # Self-check: city extraction fix
    assert _extract_city("15806 BREEDLOVE PLACE, ADDISON, TX 75001") == "ADDISON"
    assert _extract_city("1019 INTREPID CT, DESOTO, TX 75115") == "DESOTO"
    assert _extract_city("217 RAVENWOOD LN, GARLAND, TX 75043") == "GARLAND"

    # Self-check: zip fix
    assert _fix_zip("7S001") == "75001"
    assert _fix_zip("7s001") == "75001"

    # Self-check: address cleaning rejects courthouse/PO box/garbage
    assert _clean_address("Place of Sale: George Allen Courts Building, 600 Commerce St") == "Unknown"
    assert _clean_address("PO Box 398196, Dallas, Texas 75339") == "Unknown"
    assert _clean_address("STATE OF TEXAS Dallas, Texas 75339") == "Unknown"
    assert _clean_address("I 4160 Dallas Parkway Dallas, TX 75254") == "4160 Dallas Parkway Dallas, TX 75254"
    assert _clean_address("15806 BREEDLOVE PLACE, ADDISON, TX 75001") == "15806 BREEDLOVE PLACE, ADDISON, TX 75001"

    # Self-check (P1): traversal URLs must raise ValueError, never write.
    tmp_dir = tempfile.mkdtemp(prefix="dallas_pdf_probe_")
    traversal_urls = [
        f"{BASE_URL}/foreclosure/%2e%2e%5c%2e%2e%5cpwned.pdf",   # ..\..\pwned.pdf
        f"{BASE_URL}/foreclosure/..",                             # bare dot-dot segment
        f"{BASE_URL}/foreclosure/%2e%2e",                         # decoded to '..'
    ]
    for bad_url in traversal_urls:
        try:
            download_pdf(bad_url, tmp_dir)
            raise AssertionError(f"P1 FAIL: no ValueError for {bad_url!r}")
        except ValueError:
            pass
    leaked = os.listdir(tmp_dir)
    assert not leaked, f"P1 FAIL: files written during traversal probe: {leaked}"
    print("dallas_foreclosure.py: PASS — traversal probe URL raised ValueError "
          f"({len(traversal_urls)} variants), nothing written")

    # Self-check: notice parser on a sample text
    sample = """
    NOTICE OF SUBSTITUTE TRUSTEE'S SALE
    PROPERTY ADDRESS: 123 Test St, Dallas, TX 75201
    Grantor(s): John Doe
    Original Principal: $250,000.00
    Tuesday, the 1st day of September, 2026
    """
    result = parse_notice_text(sample, "Dallas", "September")
    assert "123 Test St" in result["property_address"], f"Address: {result['property_address']}"
    assert result["borrower"] == "John Doe"
    assert result["debt"] == 250_000.0, f"Debt: {result['debt']}"
    assert result["sale_date"] is not None
    assert parse_date_any(result["sale_date"]) is not None, \
        f"E2 FAIL: stated sale date must be machine-parseable, got {result['sale_date']!r}"
    assert parse_date_any(result["sale_date"]) == parse_date_any("9/01/2026")
    print("dallas_foreclosure.py: PASS — stated sale date parses to a real date:",
          f"{parse_date_any(result['sale_date'])}")

    # Self-check: courthouse address rejection
    bad_sample = """
    Place of Sale of Property: George Allen Courts Building, 600 Commerce Street, Dallas, TX 75202
    Property Address: 4335 Lashley Dr., Dallas, TX 75232
    Original Principal: $349,500.00
    """
    result2 = parse_notice_text(bad_sample, "Dallas", "March")
    assert "4335 Lashley" in result2["property_address"], f"Should extract real address, got: {result2['property_address']}"

    # Self-check (E2): sparse notice — keys preserved, values None, nothing invented.
    sparse = parse_notice_text(
        "NOTICE OF SALE\nSome boilerplate without any labeled figures.\n",
        "Dallas", "September",
    )
    expected_keys = {"property_address", "city", "borrower", "lender", "debt",
                     "all_extracted_balances", "sale_date", "sale_time", "trustee"}
    assert set(sparse.keys()) == expected_keys, f"Key shape changed: {set(sparse.keys())}"
    assert sparse["debt"] is None and sparse["sale_date"] is None and sparse["sale_time"] is None
    assert "First Tuesday" not in str(sparse.values())
    print("dallas_foreclosure.py: PASS — sparse notice keeps all dict keys with None values "
          "(no fabricated sale date/time/debt)")

    # Self-check (E2): lender verbatim — brand mention alone never becomes a lender.
    brand_only = parse_notice_text(
        "NOTICE OF SALE\nThis file serviced by Rocket Mortgage systems.\n"
        "PROPERTY ADDRESS: 9 Test Ln, Dallas, TX 75201\n$99,999.00 filing fee\n",
        "Dallas", "September",
    )
    assert brand_only["lender"] is None, \
        f"E2 FAIL: invented lender from brand mention: {brand_only['lender']!r}"
    assert "N.A." not in str(brand_only["lender"])
    assert brand_only["debt"] is None, "E2 FAIL: unlabeled dollar amount became debt"
    print("dallas_foreclosure.py: PASS — lender stays None on mere brand mentions; "
          "unlabeled dollar amounts never become estimated_default_balance")

    # Self-check (E2): process_dallas_pdf record shape with None values + status.
    class _FakePage:
        def __init__(self, t): self._t = t
        def extract_text(self): return self._t

    real_split = split_pdf_pages
    real_download = download_pdf
    try:
        globals()["split_pdf_pages"] = lambda p: [[_FakePage("NOTICE OF SALE\nnothing usable")]]
        globals()["download_pdf"] = lambda url, d, **kw: "stub.pdf"
        recs = process_dallas_pdf({"url": f"{BASE_URL}/x.pdf", "city": "Dallas",
                                   "month": "September"}, tmp_dir)
    finally:
        globals()["split_pdf_pages"] = real_split
        globals()["download_pdf"] = real_download
    assert len(recs) == 1
    ocr = recs[0]["details"]["scanned_notice_ocr_data"]
    assert {"foreclosing_lender", "substitute_trustee", "scheduled_sale_time",
            "estimated_default_balance", "all_extracted_balances",
            "ocr_engine_mode", "status"} <= set(ocr.keys())
    assert ocr["estimated_default_balance"] is None
    assert ocr["status"] in ("Partial", "NoDebt"), f"Unexpected status {ocr['status']!r}"
    print("dallas_foreclosure.py: PASS — scanned_notice_ocr_data keeps keys, balance None, "
          f"status={ocr['status']!r}")

    # Self-check (RL2): portal shape changes raise instead of returning [].
    try:
        parse_portal_links(b"<html><body>redesigned</body></html>")
        raise AssertionError("RL2 FAIL: missing #temp-region-7 did not raise")
    except DallasPortalShapeError:
        pass
    no_accordions = b"<html><body><div id='temp-region-7'><p>moved</p></div></body></html>"
    try:
        parse_portal_links(no_accordions)
        raise AssertionError("RL2 FAIL: zero blockquote[id^=display] did not raise")
    except DallasPortalShapeError:
        pass
    intact = (b"<html><body><strong>September:</strong><div id='temp-region-7'>"
              b"<blockquote id='display1'><a href='/f/Dallas_x.pdf'>Dallas_x.pdf</a>"
              b"</blockquote></div></body></html>")
    links = parse_portal_links(intact)
    assert len(links) == 1 and links[0]["city"] == "Dallas", links
    empty_month = (b"<html><body><div id='temp-region-7'>"
                   b"<blockquote id='display1'><p>No postings</p></blockquote></div></body></html>")
    assert parse_portal_links(empty_month) == [], \
        "intact structure with zero PDF links is an empty month, not a shape change"
    print("dallas_foreclosure.py: PASS — missing temp-region-7/display accordions "
          "raise DallasPortalShapeError; intact-but-empty still returns []")

    # Self-check (RL3): cached-PDF validation + quarantine + failure tallying.
    pdf_dir = tempfile.mkdtemp(prefix="dallas_pdf_cache_")
    quar_dir = os.path.join(pdf_dir, "quarantine")
    assert is_valid_pdf(os.path.join(pdf_dir, "missing.pdf")) is False
    tiny = os.path.join(pdf_dir, "tiny.pdf")
    with open(tiny, "wb") as f:
        f.write(b"%PDF-1.4 too short")
    assert is_valid_pdf(tiny) is False, "<=1KB cached PDF must be invalid"
    big_junk = os.path.join(pdf_dir, "junk.pdf")
    with open(big_junk, "wb") as f:
        f.write(b"<html>error page saved as pdf</html>" * 40)
    assert is_valid_pdf(big_junk) is False, "non-%PDF header must be invalid"
    real_pdf = os.path.join(pdf_dir, "real.pdf")
    with open(real_pdf, "wb") as f:
        f.write(b"%PDF-1.4\n" + b"x" * 2048)
    assert is_valid_pdf(real_pdf) is True

    poisoned = os.path.join(pdf_dir, "poisoned.pdf")
    with open(poisoned, "wb") as f:
        f.write(b"garbage" * 500)  # >1KB, wrong header — the classic poisoned cache
    qlog = []

    class _FakeResp:
        def __init__(self, payload): self._p = payload
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False

    good_payload = b"%PDF-1.4\n" + b"y" * 4096
    real_urlopen = urllib.request.urlopen
    real_sleep = time.sleep
    try:
        urllib.request.urlopen = lambda req, timeout=15: _FakeResp(good_payload)
        time.sleep = lambda s: None
        healed = download_pdf(f"{BASE_URL}/foreclosure/poisoned.pdf", pdf_dir,
                              quarantine_dir=quar_dir, quarantine_log=qlog)
    finally:
        urllib.request.urlopen = real_urlopen
        time.sleep = real_sleep
    assert healed == os.path.join(pdf_dir, "poisoned.pdf"), healed
    assert is_valid_pdf(healed), "re-download after quarantine must produce a valid PDF"
    assert len(qlog) == 1 and qlog[0]["path"].endswith("poisoned.pdf"), qlog
    assert os.path.exists(qlog[0]["quarantined_to"]), "poisoned file must survive in quarantine"
    assert os.path.dirname(os.path.abspath(qlog[0]["quarantined_to"])) == \
        os.path.abspath(quar_dir), "quarantined file must land in quarantine dir"

    # Valid cache hit must NOT touch the network.
    try:
        urllib.request.urlopen = lambda req, timeout=15: (_ for _ in ()).throw(
            AssertionError("cache hit must not re-download"))
        time.sleep = lambda s: None
        hit = download_pdf(f"{BASE_URL}/foreclosure/real.pdf", pdf_dir,
                           quarantine_dir=quar_dir)
    finally:
        urllib.request.urlopen = real_urlopen
        time.sleep = real_sleep
    assert hit == real_pdf

    # Per-PDF tallying: one good item + one failing item never aborts the batch.
    items = [
        {"url": f"{BASE_URL}/f/a.pdf", "filename": "a.pdf", "city": "Dallas", "month": "September"},
        {"url": f"{BASE_URL}/f/b.pdf", "filename": "b.pdf", "city": "Garland", "month": "September"},
    ]
    calls = {"n": 0}
    def _flaky_download(url, d, **kw):
        calls["n"] += 1
        if "b.pdf" in url:
            return None
        return real_pdf
    real_download2 = download_pdf
    real_split2 = split_pdf_pages
    try:
        globals()["download_pdf"] = _flaky_download
        globals()["split_pdf_pages"] = lambda p: [[_FakePage("NOTICE OF SALE\nnothing usable")]]
        recs2, report = process_pdf_batch(items, pdf_dir, quarantine_dir=quar_dir)
    finally:
        globals()["download_pdf"] = real_download2
        globals()["split_pdf_pages"] = real_split2
    assert len(recs2) == 1, "healthy PDF must survive a sibling's failure"
    assert len(report["pdf_failures"]) == 1
    assert report["pdf_failures"][0]["stage"] == "download"
    assert report["pdf_failures"][0]["filename"] == "b.pdf"
    assert report["pdf_quarantined"] == []
    print("dallas_foreclosure.py: PASS — poisoned cache quarantined+healed, valid cache "
          "hit skips network, per-PDF failures tallied into report")

    print("dallas_foreclosure.py: all self-checks passed")