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

BASE_URL = "https://www.dallascounty.org"
PORTAL_URL = f"{BASE_URL}/government/county-clerk/recording/foreclosures.php"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

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
    """
    soup = BeautifulSoup(html, "html.parser")
    region = soup.find(id="temp-region-7")
    if not region:
        return []

    bqs = region.find_all(
        lambda tag: tag.name == "blockquote" and tag.get("id", "").startswith("display")
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


def download_pdf(url: str, local_dir: str) -> str | None:
    """Download a PDF to local_dir. Returns local path or None."""
    os.makedirs(local_dir, exist_ok=True)
    filename = urllib.parse.unquote(url.split("/")[-1])
    local_path = os.path.join(local_dir, filename)
    if os.path.exists(local_path):
        return local_path

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        time.sleep(1.0)  # rate limit
        with urllib.request.urlopen(req, timeout=15) as resp, open(local_path, "wb") as f:
            f.write(resp.read())
        return local_path
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
    Does NOT fabricate appraised values — caller must get from DCAD.
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

    # 2. Lender / Mortgagee
    lender = "Unknown Beneficiary"
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
    else:
        if "NOMINEE FOR" in text_fixed.upper():
            actual = re.search(r'NOMINEE FOR\s+([^,\n]+)', text_fixed, re.IGNORECASE)
            if actual:
                lender = _clean(actual.group(1))
        else:
            for l in ["JPMorgan Chase", "Wells Fargo", "Bank of America",
                       "Freedom Mortgage", "Rocket Mortgage", "U.S. Bank",
                       "PennyMac", "NewRez"]:
                if l.lower() in text_fixed.lower():
                    lender = f"{l} Bank, N.A."
                    break

    # 3. Borrower / Grantor
    borrower = "Unknown"
    grantor_match = re.search(
        r'(?:Grantor\(s\):|Grantor:|Borrower\(s\):|Borrower:)\s*([^\n]+)',
        text_fixed, re.IGNORECASE
    )
    if grantor_match:
        borrower = _clean(grantor_match.group(1))

    # 4. Debt — expanded regex patterns (FIX vs prior attempt)
    debt = 0.0
    # Try specific labels first
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

    # Fallback: first large dollar amount in the notice
    if debt == 0.0:
        amounts = re.findall(r'\$\s*([0-9,]{5,10}\.[0-9]{2})', text_fixed)
        if amounts:
            try:
                debt = float(amounts[0].replace(",", ""))
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

    # 5. Sale date and time
    sale_date = f"First Tuesday of {month} 2026"
    date_match = re.search(
        r'(Tuesday|Wednesday|Thursday|Friday|Monday),\s*the\s*\d+\w*\s*day\s*of\s*[A-Za-z]+,\s*202\d',
        text_fixed, re.IGNORECASE
    )
    if date_match:
        sale_date = _clean(date_match.group(0))

    sale_time = "10:00 AM (or within three hours thereafter)"
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


def process_dallas_pdf(item: dict, temp_dir: str) -> list[dict]:
    """Download, split, and parse a single city PDF."""
    local_path = download_pdf(item["url"], temp_dir)
    if not local_path:
        return []

    notice_groups = split_pdf_pages(local_path)
    records = []

    for idx, page_group in enumerate(notice_groups, 1):
        full_text = ""
        for page in page_group:
            full_text += (page.extract_text() or "") + "\n"

        details = parse_notice_text(full_text, item["city"], item["month"])
        entry_id = f"DAL_{item['month']}_{item['city']}_{idx}"

        records.append({
            "entry_id": entry_id,
            "property_address": details["property_address"],
            "city": details["city"],
            "county": "Dallas",
            "details": {
                "property_info": {
                    "Address": details["property_address"],
                    "Deed Date": details["sale_date"],
                    "Commercial": "True" if details["debt"] >= 1_000_000 else "False",
                },
                "owner_info": {
                    "Name": details["borrower"],
                    "Address": details["property_address"],
                },
                "scanned_notice_ocr_data": {
                    "foreclosing_lender": details["lender"],
                    "substitute_trustee": details["trustee"],
                    "scheduled_sale_time": details["sale_time"],
                    "estimated_default_balance": details["debt"],
                    "all_extracted_balances": details["all_extracted_balances"],
                    "ocr_engine_mode": "pypdf Text Extractor + Regex",
                    "status": "Success" if details["debt"] > 0 else "NoDebt",
                },
            },
        })

    return records


class DallasForeclosureAdapter(SourceAdapter):
    """Dallas County foreclosure-notice portal adapter."""
    channel = "foreclosure_notice"
    county = "dallas"

    def fetch(self, limit: int | None = None, temp_dir: str = "data/dallas_pdfs") -> list[NormalizedLead]:
        """Fetch foreclosure listings from Dallas County PDFs."""
        html = fetch_portal_html()
        pdf_links = parse_portal_links(html)
        if not pdf_links:
            return []
        if limit:
            pdf_links = pdf_links[:limit]

        leads = []
        for item in pdf_links:
            records = process_dallas_pdf(item, temp_dir)
            for r in records:
                leads.append(NormalizedLead(
                    county="dallas",
                    parcel_id=r["entry_id"],
                    channel="foreclosure_notice",
                    situs_address=r["property_address"],
                    sale_date=r["details"]["property_info"].get("Deed Date"),
                    owner_name=r["details"]["owner_info"].get("Name"),
                    raw_data=r,
                ))
        return leads

    def provenance(self) -> SourceProvenance:
        return SourceProvenance(source="dallas_foreclosure_notice", source_rigor_tier=0)


if __name__ == "__main__":
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
    assert "September" in result["sale_date"]

    # Self-check: courthouse address rejection
    bad_sample = """
    Place of Sale of Property: George Allen Courts Building, 600 Commerce Street, Dallas, TX 75202
    Property Address: 4335 Lashley Dr., Dallas, TX 75232
    Original Principal: $349,500.00
    """
    result2 = parse_notice_text(bad_sample, "Dallas", "March")
    assert "4335 Lashley" in result2["property_address"], f"Should extract real address, got: {result2['property_address']}"

    print("dallas_foreclosure.py: all self-checks passed")