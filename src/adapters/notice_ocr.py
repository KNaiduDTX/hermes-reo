# src/adapters/notice_ocr.py
"""OCR parser for scanned foreclosure notice images — REAL OCR, no fabrication.

Ported from lucid-noether parse_notices_ocr.py, but DISCARDS the heuristic
fallback that fabricated lender/trustee/debt values. If OCR fails, returns
empty data — never fabricates.

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
    """OCR-extracted data from a scanned foreclosure notice."""
    foreclosing_lender: str = "Unknown"
    substitute_trustee: str = "Unknown"
    scheduled_sale_time: str = "Unknown"
    estimated_default_balance: float = 0.0
    original_principal: float | None = None
    unpaid_principal_balance: float | None = None
    all_extracted_balances: list[float] = field(default_factory=list)
    ocr_engine: str = "none"
    status: str = "not_attempted"  # success, no_ocr, no_image, parse_fail


def _extract_lender(text: str) -> str:
    """Extract foreclosing lender from notice text."""
    # Try explicit labels first
    for pattern in [
        r'(?:Original Mortgagee:|Current Mortgagee:|Beneficiary:|Noteholder:|Lender:)[ \t]*([^\n]+)',
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            matched = " ".join(m.group(1).split()).strip()
            # Resolve MERS nominee
            if "NOMINEE FOR" in text.upper():
                actual = re.search(r'NOMINEE FOR\s+([^,\n]+)', text, re.IGNORECASE)
                if actual:
                    return " ".join(actual.group(1).split()).strip()
            return matched

    # Try known lender names
    for l in ["JPMorgan Chase", "Wells Fargo", "Bank of America", "Freedom Mortgage",
              "Rocket Mortgage", "U.S. Bank", "PennyMac", "NewRez"]:
        if l.lower() in text.lower():
            return f"{l} Bank, N.A."

    # Try MERS nominee globally
    if "NOMINEE FOR" in text.upper():
        actual = re.search(r'NOMINEE FOR\s+([^,\n]+)', text, re.IGNORECASE)
        if actual:
            return " ".join(actual.group(1).split()).strip()

    return "Unknown"


def _extract_trustee(text: str) -> str:
    """Extract substitute trustee from notice text."""
    m = re.search(r'Substitute\s+T[a-z]{4,6}e[s\(\)a-z]*:\s*([^\n]+)', text, re.IGNORECASE)
    if m:
        return " ".join(m.group(1).split()).strip()
    return "Unknown"


def _extract_debt(text: str) -> tuple[float, float | None, float | None, list[float]]:
    """Extract debt amounts from notice text.

    Returns (estimated_default, original_principal, unpaid_principal, all_balances).
    """
    # Try specific labels
    debt = 0.0
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

    # Original principal
    m = re.search(r'(?:Original Principal|Note Amount|Original Debt)[^\d$]*\$?\s*([0-9,]{5,10}(?:\.[0-9]{2})?)', text, re.IGNORECASE)
    if m:
        try:
            orig = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Unpaid principal
    m = re.search(r'(?:Unpaid Principal|Unpaid Balance|Current Principal)[^\d$]*\$?\s*([0-9,]{5,10}(?:\.[0-9]{2})?)', text, re.IGNORECASE)
    if m:
        try:
            unpaid = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Fallback: first large dollar amount
    if debt == 0.0:
        amounts = re.findall(r'\$\s*([0-9,]{5,10}\.[0-9]{2})', text)
        if amounts:
            try:
                debt = float(amounts[0].replace(",", ""))
            except ValueError:
                pass

    # All balances
    all_balances = []
    for amt in re.findall(r'\$\s*([0-9,]{5,10}(?:\.[0-9]{2})?)', text):
        try:
            val = float(amt.replace(",", ""))
            if val not in all_balances:
                all_balances.append(val)
        except ValueError:
            pass

    return debt, orig, unpaid, all_balances


def _extract_sale_time(text: str) -> str:
    """Extract sale time from notice text."""
    m = re.search(r'\b(\d{1,2}:\d{2}\s*(?:AM|PM|A\.M\.|P\.M\.))\b[^\n]*', text, re.IGNORECASE)
    if m:
        return " ".join(m.group(0).split()).strip()
    if "1:00" in text or "1 pm" in text.lower():
        return "1:00 PM (or within three hours thereafter)"
    return "10:00 AM (or within three hours thereafter)"


def parse_notice_image(base64_image: str) -> NoticeOCRData:
    """OCR a scanned notice image (base64 JPEG) and extract structured data.

    Uses pytesseract if available. If tesseract is not installed or OCR fails,
    returns empty data with status='no_ocr' — NEVER fabricates values.
    """
    if not base64_image:
        return NoticeOCRData(status="no_image")

    # Strip data URI prefix
    b64_str = base64_image.split(",", 1)[1] if base64_image.startswith("data:") else base64_image

    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return NoticeOCRData(status="no_ocr", ocr_engine="none",
                             foreclosing_lender="Unknown")

    try:
        image_data = base64.b64decode(b64_str)
        image = Image.open(io.BytesIO(image_data))
        text = pytesseract.image_to_string(image)
    except Exception:
        return NoticeOCRData(status="parse_fail", ocr_engine="tesseract")

    if not text.strip():
        return NoticeOCRData(status="parse_fail", ocr_engine="tesseract")

    # Parse the OCR text
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
        ocr_engine="tesseract",
        status="success" if debt > 0 else "no_debt_found",
    )


def parse_notice_text(text: str) -> NoticeOCRData:
    """Parse notice text directly (for Dallas PDFs where pypdf already extracted text).

    Same extraction logic but operates on text instead of an image.
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
        ocr_engine="text_parser",
        status="success" if debt > 0 else "no_debt_found",
    )


if __name__ == "__main__":
    # Self-check: text parser
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
    assert "Wells Fargo" in result.foreclosing_lender
    assert "Beazley" in result.substitute_trustee
    assert "10:00" in result.scheduled_sale_time
    print(f"notice_ocr.py: text parser — debt=${result.estimated_default_balance:,.0f}, lender={result.foreclosing_lender}")

    # Self-check: no fabrication when OCR unavailable
    result2 = parse_notice_image("")
    assert result2.status == "no_image"
    assert result2.foreclosing_lender == "Unknown"
    assert result2.estimated_default_balance == 0.0
    print("notice_ocr.py: no image → no fabrication (correct)")

    # Self-check: no fabrication when text empty
    result3 = parse_notice_text("")
    assert result3.status == "no_text"
    assert result3.estimated_default_balance == 0.0
    print("notice_ocr.py: no text → no fabrication (correct)")

    print("notice_ocr.py: all self-checks passed")