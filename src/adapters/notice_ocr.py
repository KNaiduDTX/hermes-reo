# src/adapters/notice_ocr.py
"""OCR parser for scanned foreclosure notice images — REAL OCR only.

Ported from lucid-noether parse_notices_ocr.py. All heuristic fabrication
has been removed (finding E2) — and this docstring is now TRUE:

- estimated_default_balance comes ONLY from labeled amounts; when no label
  matches it is None (the unlabeled first-dollar guess is gone).
  all_extracted_balances stays as the candidate list for human review.
- scheduled_sale_time is ONLY a time printed in the notice; there is no
  invented "10:00 AM" default — it is None when absent.
- foreclosing_lender / substitute_trustee are returned VERBATIM from the
  notice text. Lenders are never guessed from brand mentions and legal
  suffixes like "N.A." are never appended.
- status is "partial" when primaries are missing but some real data was
  extracted, so downstream can tell "nothing found" from "found some".

Collin County detail pages embed scanned notice JPEGs as base64 data URIs.
Dallas County notices are PDFs with extractable text (no OCR needed).

Uses pytesseract if available; otherwise returns empty data with a flag.
"""
from __future__ import annotations
import base64
import io
import re
from dataclasses import dataclass, field


@dataclass
class NoticeOCRData:
    """OCR-extracted data from a scanned foreclosure notice.

    Every value field may be None: None means "not stated in the notice",
    never a fabricated default.
    """
    foreclosing_lender: str | None = None
    substitute_trustee: str | None = None
    scheduled_sale_time: str | None = None
    estimated_default_balance: float | None = None
    original_principal: float | None = None
    unpaid_principal_balance: float | None = None
    all_extracted_balances: list[float] = field(default_factory=list)
    ocr_engine: str = "none"
    status: str = "not_attempted"
    # success | partial | no_debt_found | no_ocr | no_image | parse_fail | no_text


def _extract_lender(text: str) -> str | None:
    """Extract foreclosing lender VERBATIM from notice text.

    Only explicit mortgagee/beneficiary labels and MERS nominee statements
    count. Returns None when no lender is stated. NEVER guesses a lender
    from brand-name mentions and NEVER appends invented legal suffixes
    ("Bank, N.A." etc.) — finding E2.
    """
    m = re.search(
        r'(?:Original Mortgagee:|Current Mortgagee:|Beneficiary:|Noteholder:|Lender:)[ \t]*([^\n]+)',
        text, re.IGNORECASE
    )
    nominee = re.search(r'NOMINEE FOR\s+([^,\n]+)', text, re.IGNORECASE)

    if m:
        matched = " ".join(m.group(1).split()).strip()
        # Resolve MERS nominee to the actual beneficiary, verbatim from text
        if nominee:
            resolved = " ".join(nominee.group(1).split()).strip()
            if resolved:
                return resolved
        return matched or None

    if nominee:
        resolved = " ".join(nominee.group(1).split()).strip()
        if resolved:
            return resolved

    return None


def _extract_trustee(text: str) -> str | None:
    """Extract substitute trustee VERBATIM from notice text (None if absent)."""
    m = re.search(r'Substitute\s+T[a-z]{4,6}e[s\(\)a-z]*:\s*([^\n]+)', text, re.IGNORECASE)
    if m:
        cleaned = " ".join(m.group(1).split()).strip()
        return cleaned or None
    return None


def _extract_debt(text: str) -> tuple[float | None, float | None, float | None, list[float]]:
    """Extract debt amounts from notice text.

    Returns (estimated_default, original_principal, unpaid_principal,
    all_balances). estimated_default comes ONLY from labeled amounts — the
    old fallback that promoted the first large dollar figure in the notice
    (which could be taxes, costs, or another paragraph's figure) is removed;
    it stays None when no labeled amount matches. all_extracted_balances
    keeps every dollar figure found, as review candidates only.
    """
    debt = None
    orig = None
    unpaid = None

    for pattern in [
        r'(?:Original Principal:|Principal Debt:|Debt Amount:|Unpaid Principal Balance:|Current Principal Balance:)\s*\D*([0-9,]{5,10}\.[0-9]{2})',
        r'(?:Original Principal|Note Amount|Original Debt|Unpaid Principal|Unpaid Balance|Current Principal)[^\d$]*\$?\s*([0-9,]{5,10}(?:\.[0-9]{2})?)',
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                debt = float(m.group(1).replace(",", ""))
                break
            except ValueError:
                pass

    # Original principal (labeled)
    m = re.search(r'(?:Original Principal|Note Amount|Original Debt)[^\d$]*\$?\s*([0-9,]{5,10}(?:\.[0-9]{2})?)', text, re.IGNORECASE)
    if m:
        try:
            orig = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Unpaid principal (labeled)
    m = re.search(r'(?:Unpaid Principal|Unpaid Balance|Current Principal)[^\d$]*\$?\s*([0-9,]{5,10}(?:\.[0-9]{2})?)', text, re.IGNORECASE)
    if m:
        try:
            unpaid = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # All dollar figures — candidates for review, NOT a default balance.
    all_balances = []
    for amt in re.findall(r'\$\s*([0-9,]{5,10}(?:\.[0-9]{2})?)', text):
        try:
            val = float(amt.replace(",", ""))
            if val not in all_balances:
                all_balances.append(val)
        except ValueError:
            pass

    return debt, orig, unpaid, all_balances


def _extract_sale_time(text: str) -> str | None:
    """Extract sale time VERBATIM from notice text (None if absent).

    No invented default times — finding E2.
    """
    m = re.search(r'\b(\d{1,2}:\d{2}\s*(?:AM|PM|A\.M\.|P\.M\.))\b[^\n]*', text, re.IGNORECASE)
    if m:
        return " ".join(m.group(0).split()).strip()
    return None


def _classify_status(debt, lender, trustee, orig, unpaid, all_balances) -> str:
    """Status for parsed notice data.

    success       — the primary estimated_default_balance was extracted.
    partial       — no balance, but at least one verbatim field was extracted.
    no_debt_found — nothing usable extracted (legacy status name kept).
    """
    if debt is not None:
        return "success"
    has_any = any([
        bool(lender),
        bool(trustee),
        orig is not None,
        unpaid is not None,
        bool(all_balances),
    ])
    return "partial" if has_any else "no_debt_found"


def parse_notice_image(base64_image: str) -> NoticeOCRData:
    """OCR a scanned notice image (base64 JPEG) and extract structured data.

    Uses pytesseract if available. If tesseract is not installed or OCR
    fails, returns empty data with status='no_ocr' — values stay None,
    never fabricated.
    """
    if not base64_image:
        return NoticeOCRData(status="no_image")

    # Strip data URI prefix
    b64_str = base64_image.split(",", 1)[1] if base64_image.startswith("data:") else base64_image

    try:
        import pytesseract
        from PIL import Image
        # Decompression-bomb guard (audit P4): refuse absurdly large scans
        # instead of letting a hostile image exhaust memory.
        Image.MAX_IMAGE_PIXELS = 30_000_000  # ~30 MP
    except ImportError:
        return NoticeOCRData(status="no_ocr", ocr_engine="none")

    try:
        image_data = base64.b64decode(b64_str)
        with Image.open(io.BytesIO(image_data)) as image:
            # timeout (audit P4): a pathological scan can no longer hang the
            # pipeline — pytesseract raises RuntimeError on expiry.
            text = pytesseract.image_to_string(image, timeout=30)
    except Exception:
        return NoticeOCRData(status="parse_fail", ocr_engine="tesseract")

    if not text.strip():
        return NoticeOCRData(status="parse_fail", ocr_engine="tesseract")

    return parse_notice_text(text, ocr_engine="tesseract")


def parse_notice_text(text: str, ocr_engine: str = "text_parser") -> NoticeOCRData:
    """Parse notice text directly (for Dallas PDFs where pypdf already extracted text).

    Same extraction logic but operates on text instead of an image. All
    values are verbatim-or-None; see module docstring (finding E2).
    """
    if not text.strip():
        return NoticeOCRData(status="no_text")

    lender = _extract_lender(text)
    trustee = _extract_trustee(text)
    sale_time = _extract_sale_time(text)
    debt, orig, unpaid, all_balances = _extract_debt(text)

    return NoticeOCRData(
        foreclosing_lender=lender,
        substitute_trustee=trustee,
        scheduled_sale_time=sale_time,
        estimated_default_balance=debt,
        original_principal=orig,
        unpaid_principal_balance=unpaid,
        all_extracted_balances=all_balances,
        ocr_engine=ocr_engine,
        status=_classify_status(debt, lender, trustee, orig, unpaid, all_balances),
    )


if __name__ == "__main__":
    import dataclasses

    # Self-check: full labeled notice parses verbatim
    sample = """
    NOTICE OF SUBSTITUTE TRUSTEE'S SALE
    PROPERTY ADDRESS: 123 Test St, Dallas, TX 75201
    Grantor(s): John Doe
    Original Principal: $250,000.00
    Current Mortgagee: Wells Fargo Bank, N.A.
    Substitute Trustee: John Beazley
    Tuesday, the 1st day of September, 2026 at 10:00 AM
    """
    result = parse_notice_text(sample)
    assert result.status == "success"
    assert result.estimated_default_balance == 250_000.0
    assert result.foreclosing_lender == "Wells Fargo Bank, N.A."  # verbatim, suffix came from the notice itself
    assert result.substitute_trustee == "John Beazley"
    assert result.scheduled_sale_time == "10:00 AM"
    print(f"notice_ocr.py: PASS — labeled fields verbatim: debt=${result.estimated_default_balance:,.0f}, "
          f"lender={result.foreclosing_lender!r}, time={result.scheduled_sale_time!r}")

    # Self-check (E2): sparse notice — ALL keys kept, unknown values are None.
    sparse_text = "NOTICE OF SALE\nBoilerplate only. Filing fee $7,500.00.\n"
    sparse = parse_notice_text(sparse_text)
    d = dataclasses.asdict(sparse)
    required_keys = {"foreclosing_lender", "substitute_trustee", "scheduled_sale_time",
                     "estimated_default_balance", "original_principal",
                     "unpaid_principal_balance", "all_extracted_balances",
                     "ocr_engine", "status"}
    assert required_keys <= set(d.keys()), f"E2 FAIL: key shape changed: {sorted(d)}"
    assert d["foreclosing_lender"] is None
    assert d["scheduled_sale_time"] is None
    assert d["estimated_default_balance"] is None
    assert d["all_extracted_balances"] == [7500.0]  # candidate list preserved, NOT promoted
    assert d["status"] == "partial"
    print("notice_ocr.py: PASS — OCR dict keeps all keys with None values where unstated; "
          "$7,500.00 stayed a candidate, never became estimated_default_balance")

    # Self-check (E2): lender never invented from brand mention / suffix never appended
    brand = parse_notice_text(
        "NOTICE OF SALE\nServicing systems by Rocket Mortgage and PennyMac.\n"
        "Sale at 1:00 PM\n$120,000.00 costs\n"
    )
    assert brand.foreclosing_lender is None, f"Invented lender: {brand.foreclosing_lender!r}"
    assert brand.scheduled_sale_time == "1:00 PM"
    assert "N.A." not in str(brand.foreclosing_lender)
    assert brand.status == "partial"
    print("notice_ocr.py: PASS — no invented 'Rocket Mortgage Bank, N.A.' style lenders; "
          "sale time verbatim ('1:00 PM'), no default time invented")

    # Self-check (E2): MERS nominee resolves verbatim (capture stops at the
    # first line/comma delimiter, as before — still verbatim from the notice)
    mers = parse_notice_text("Lender: MERS\nNOMINEE FOR JPMORGAN CHASE BANK, N.A.")
    assert mers.foreclosing_lender == "JPMORGAN CHASE BANK"
    print(f"notice_ocr.py: PASS — MERS nominee resolved verbatim: {mers.foreclosing_lender!r}")

    # Self-check: nothing-at-all -> legacy no_debt_found, still all keys
    empty = parse_notice_text("no data here")
    de = dataclasses.asdict(empty)
    assert required_keys <= set(de.keys())
    assert empty.status == "no_debt_found" and empty.estimated_default_balance is None
    print("notice_ocr.py: PASS — nothing-extractable text → status='no_debt_found', keys intact")

    # Self-check: no fabrication when OCR unavailable
    result2 = parse_notice_image("")
    assert result2.status == "no_image"
    assert result2.foreclosing_lender is None
    assert result2.estimated_default_balance is None
    print("notice_ocr.py: PASS — no image → no fabrication (correct)")

    # Self-check: no fabrication when text empty
    result3 = parse_notice_text("")
    assert result3.status == "no_text"
    assert result3.estimated_default_balance is None
    print("notice_ocr.py: PASS — no text → no fabrication (correct)")

    print("notice_ocr.py: all self-checks passed")
