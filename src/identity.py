# src/identity.py
"""Stable identity spine (OPPORTUNITY_MAP FN1).

Two id layers replace positional parcel ids (which collide 134x across 335
rows because Dallas mints DAL_{month}_{city}_{idx}):

- notice_id(raw): one foreclosure NOTICE (a lender action against a
  property on a date) — sha1[:12] over normalized address + lender +
  debt + sale-date. Same property re-noticed next month => NEW notice_id.
- property_id(county, raw_address, owner=""): the durable ASSET — county
  + normalized address (+ owner tiebreaker when addresses tie). Stable
  across postponements and re-scrapes.

Fallbacks are explicit: callers pass whatever they have; missing debt or
dates simply omit those hash components rather than fabricating values.
"""
from __future__ import annotations

import hashlib
import re

try:
    from .addr import normalize_address
    from .dates import parse_date_any
except ImportError:  # direct-script execution
    from addr import normalize_address
    from dates import parse_date_any


def _sha(text: str, n: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def _norm_component(value) -> str:
    s = str(value or "").strip().upper()
    return re.sub(r"\s+", " ", s)



_LENDER_SUFFIX_RE = re.compile(
    r"\b(COMPANY|CO|CORP(?:ORATION)?|INCORPORATED|INC|LLC|L\.L\.C\.?"
    r"|N\.?A\.?|NATIONAL ASSOCIATION|ASSOCIATION|LT[DP]|TRUST)\b\.?", re.I)


def _lender_norm(lender: str) -> str:
    """Collapse legal-suffix/case noise so entity variants hash together."""
    s = _norm_component(lender)
    prev = None
    while prev != s:
        prev = s
        s = _LENDER_SUFFIX_RE.sub(" ", s)
        s = re.sub(r"\s+", " ", s).strip(" .,&")
    return s


def notice_id(*, address: str, lender: str = "", debt=None, sale_date: str = "") -> str:
    """Stable id for ONE foreclosure notice."""
    n = normalize_address(address)
    addr_component = (f"{n.street_number} {n.street_name} {n.street_type}".strip()
                      if n.valid else _norm_component(address))
    parts = ["A:" + addr_component]
    if lender:
        parts.append("L:" + _lender_norm(lender))
    if debt not in (None, "", 0):
        try:
            parts.append(f"D:{float(debt):.2f}")
        except (TypeError, ValueError):
            pass
    if sale_date:
        d = parse_date_any(sale_date)
        if d:
            parts.append("S:" + d.isoformat())
    return "N" + _sha("|".join(parts))


def property_id(county: str, address: str, owner: str = "") -> str:
    """Durable id for the physical ASSET across notices/years."""
    n = normalize_address(address)
    if n.valid:
        # zip is deliberately NOT hashed: scrape zip noise must not split one
        # asset into two ids; county+canonical-geo is the durable core.
        geo = f"{n.street_number} {n.street_name} {n.street_type}".strip()
        base = f"{_norm_component(county)}|{geo}"
    else:
        base = f"{_norm_component(county)}|RAW:{_norm_component(address)}"
    if owner:
        base += "|O:" + _norm_component(owner)
    return "P" + _sha(base)


def legacy_backfill_key(old_parcel_id: str, address: str, sale_month_hint: str = "") -> str:
    """Mapping key used by the one-time old->new id backfill (address+month)."""
    return f"{_norm_component(address)}|{_norm_component(sale_month_hint)}"


def self_check() -> None:
    a = notice_id(address="15806 BREEDLOVE PLACE, ADDISON, TX 75001",
                  lender="CTX MORTGAGE COMPANY", debt=309600.0,
                  sale_date="Tuesday, the 1st day of September, 2026")
    b = notice_id(address="15806  Breedlove Pl, Addison, TX 75001-6334",
                  lender="ctx mortgage co.", debt=309600,
                  sale_date="2026-09-01")
    assert a == b, "normalization must absorb case/type/zip4/date-format drift"

    c = notice_id(address="15806 BREEDLOVE PLACE, ADDISON, TX 75001",
                  lender="CTX MORTGAGE COMPANY", debt=309600.0,
                  sale_date="2026-10-06")  # postponed to October
    assert a != c, "a postponed sale is a NEW notice"

    p1 = property_id("dallas", "15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334")
    p2 = property_id("DALLAS", "15806  Breedlove Pl, Addison, TX 75201")  # wrong zip tolerated
    assert p1 == p2, "same asset regardless of zip noise"
    assert property_id("collin", "10022 PLAINSMAN LN, FRISCO, TX 75035") != p1

    # Missing debt/date must not crash — components just drop out.
    assert notice_id(address="1000 X ST, DALLAS, TX 75201")

    l1 = notice_id(address="1000 X ST, DALLAS, TX 75201",
                   lender="WELLS FARGO BANK, N.A.")
    l2 = notice_id(address="1000 X ST, DALLAS, TX 75201",
                   lender="wells fargo bank")
    assert l1 == l2, "legal-suffix variants must collapse"


if __name__ == "__main__":
    self_check()
    print("identity.py: spine self-check passed")
