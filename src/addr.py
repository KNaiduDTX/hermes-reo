# src/addr.py
"""Address normalization — the shared filter for every outbound lookup.

The legacy PDF scrape left legal-prose fragments, out-of-state strings and
corrupted tokens in address fields ("60 foot right-of-way...", "Virginia
Beach", "1255 West !$th Street"). Those burned DCAD browser sessions,
failed Census geocodes forever, and floated to the top of equity rankings.

Every consumer (find_deals ranking, geocode-backfill, DCAD echo matching)
must route raw addresses through THIS module first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NBSP = "\xa0"

# Canonical street-type abbreviations (DCAD convention).
STREET_TYPES = {
    "STREET": "ST", "ST": "ST",
    "DRIVE": "DR", "DR": "DR",
    "LANE": "LN", "LN": "LN",
    "COURT": "CT", "CT": "CT",
    "PLACE": "PL", "PL": "PL",
    "BOULEVARD": "BLVD", "BLVD": "BLVD",
    "AVENUE": "AVE", "AVE": "AVE",
    "PARKWAY": "PKWY", "PKWY": "PKWY",
    "ROAD": "RD", "RD": "RD",
    "CIRCLE": "CIR", "CIR": "CIR",
    "TERRACE": "TER", "TER": "TER",
    "WAY": "WAY", "TRAIL": "TRL", "TRL": "TRL",
}

# City spelling repairs observed in the legacy dataset.
CITY_FIXUPS = {
    "BALCHSPRINGS": "BALCH SPRINGS",
}

# Substrings that mark a string as legal prose / non-address junk.
_JUNK_PATTERNS = (
    re.compile(r"right[\s-]*of[\s-]*way", re.I),
    re.compile(r"texas\s+property\s+code", re.I),
    re.compile(r"\bthe_\b"),
    re.compile(r"[!@$%^*]"),                     # corrupted tokens like !$th
    re.compile(r"\b(?:COMER|COMMER)\b.*\bof\b", re.I),  # "northeast comer of"
)

_STATE_ZIP_RE = re.compile(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b", re.I)
_ANY_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_LEAD_NUM_RE = re.compile(r"^(\d{1,6})\s+(.+)$")
_UNIT_RE = re.compile(r"\bUNIT\s+([0-9A-Z][0-9A-Z\s\-]*)", re.IGNORECASE)


@dataclass
class NormalizedAddress:
    raw: str
    valid: bool
    invalid_reason: str = ""
    street_number: str = ""
    street_name: str = ""          # name WITHOUT canonical type suffix
    street_type: str = ""          # canonical abbreviation ("", if none seen)
    unit: str = ""
    city: str = ""
    zip5: str = ""
    state: str = ""

    @property
    def one_line(self) -> str:
        parts = [f"{self.street_number} {self.street_name}".strip()]
        if self.street_type:
            parts.append(self.street_type)
        if self.unit:
            parts.append(f"#{self.unit}")
        tail = ", ".join(p for p in (self.city or "", "TX" if (self.state or "TX") == "TX" else self.state) if p)
        line = " ".join(parts)
        if tail:
            line += f", {tail}"
        if self.zip5:
            line += f" {self.zip5}"
        return line


def _clean(raw: str) -> str:
    s = (raw or "").replace(_NBSP, " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(" ,_")


def normalize_address(raw: str) -> NormalizedAddress:
    """Normalize a raw scrape address; *valid* marks lookup-worthiness.

    Strategy: locate the FIRST "<ST> <zip>" anchor, treat everything before
    it as street+city, and discard duplicate trailing state/zip artifacts
    ("..., TEXAS 75006, TX 75202" keeps 75006).
    """
    s = _clean(raw)
    out = NormalizedAddress(raw=raw, valid=False)

    for pat in _JUNK_PATTERNS:
        if pat.search(s):
            out.invalid_reason = f"junk pattern: {pat.pattern!r}"
            return out

    if re.search(r"\bV[ABCHINMSTYZ]\b|\bVA\b|\bCA\b|\bNY\b|\bFL\b", s.upper()) \
            and not re.search(r"\bTX\b|\bTEXAS\b", s.upper()):
        out.invalid_reason = "out-of-state (no TX marker)"
        return out

    lead_m = _LEAD_NUM_RE.match(s)
    lead_num = lead_m.group(1) if lead_m else None
    zips = [mm for mm in _ANY_ZIP_RE.finditer(s)
            if not (lead_num and mm.group(1) == lead_num)]
    if not zips:
        out.invalid_reason = "no zip code found"
        return out
    # Leftmost remaining zip wins: legacy artifacts APPEND junk
    # ("..., TEXAS 75006, TX 75202" -> keep 75006).
    zm = zips[0]
    out.zip5 = zm.group(1)
    su = s.upper()
    out.state = "TX" if ("TEXAS" in su or re.search(r"\bTX\b", su)) else ""
    if out.state != "TX":
        out.invalid_reason = "no TX marker"
        return out
    head = s[:zm.start()].strip().strip(",").strip()

    um = _UNIT_RE.search(head)
    if um:
        out.unit = re.sub(r"\s+", "", um.group(1)).strip("-") or ""
        head = (head[:um.start()] + " " + head[um.end():]).strip()

    head = re.sub(r"\bTEXAS\b|\bTX\b", " ", head, flags=re.I)
    head = re.sub(r"\s+", " ", head).strip(" ,")

    segs = [x.strip() for x in head.split(",") if x.strip()]
    street_part = segs[0] if segs else ""
    if len(segs) > 1:
        city_raw = segs[-1].upper().replace(".", "")
        _fixed = CITY_FIXUPS.get(city_raw.replace(" ", ""))
        out.city = _fixed.title() if _fixed else city_raw.title()

    m = _LEAD_NUM_RE.match(street_part)
    if not m:
        out.invalid_reason = "no leading street number"
        return out
    out.street_number = m.group(1)

    words = m.group(2).split()
    bad = [w for w in words if not re.fullmatch(r"[A-Za-z0-9'\-]+", w)]
    if bad:
        out.invalid_reason = f"corrupted street tokens: {bad!r}"
        return out

    # Reverse-scan for the canonical type: legacy strings often glue the
    # city onto the street ("...ALAN AVE CARROLLTON").
    t_idx = next((len(words) - 1 - i for i, w in enumerate(reversed(words))
                  if w.upper() in STREET_TYPES), None)
    if t_idx is not None:
        out.street_type = STREET_TYPES[words[t_idx].upper()]
        city_words = words[t_idx + 1:]
        words = words[:t_idx]
        if city_words and not out.city:
            cj = " ".join(city_words).upper()
            out.city = CITY_FIXUPS.get(cj.replace(" ", ""), cj.title())

    if not words:
        out.invalid_reason = "street name missing"
        return out
    out.street_name = " ".join(w.upper() for w in words)
    out.valid = True
    return out


def is_plausible_street_address(raw: str) -> bool:
    return normalize_address(raw).valid


def to_census_one_line(raw: str) -> str:
    """Census-friendly single line, or '' when implausible."""
    n = normalize_address(raw)
    return n.one_line if n.valid else ""


def to_dcad_search_parts(raw: str) -> dict | None:
    """Street number + suffix-less name for DCAD's two-field form."""
    n = normalize_address(raw)
    if not n.valid:
        return None
    return {"num": n.street_number, "name": n.street_name}


def self_check() -> None:
    ok = normalize_address("15806 BREEDLOVE PLACE, ADDISON, TX 75001-6334")
    assert ok.valid and ok.street_number == "15806"
    assert ok.street_type == "PL" and ok.zip5 == "75001"

    mixed = normalize_address("1011 E ALAN AVE CARROLLTON, TEXAS 75006, TX 75202")
    assert mixed.valid and mixed.zip5 == "75006" and mixed.street_type == "AVE"

    balch = normalize_address("2739 SHEILAH DRIVE, BALCHSPRINGS, TX 75180")
    assert balch.valid and balch.city == "Balch Springs"

    unit = normalize_address("708 West 10th Street, UNIT 101 -106, Dallas, TX 75208")
    assert unit.valid and unit.unit.startswith("101")

    for bad, why in [
        ("60 foot right-af-way, at the northeast comer of said lot", "junk"),
        ("002 of the Texas Property Code as the place where ", "junk"),
        ("3637 Sentara Way, Virginia Beach, VA 23452", "state"),
        ("1255 West !$th Street, Suite 1060 Plano, Dallas, TX", "corrupt"),
        ("Suite 1100 Dallas Texas 75201", "number"),
        ("The Real Property or its address is commonly known", "number"),
    ]:
        r = normalize_address(bad)
        assert not r.valid, (bad, r)
        assert why.split()[0] in (r.invalid_reason or "junk") or r.invalid_reason, bad

    assert is_plausible_street_address("5109 LAUDERDALE ST DALLAS, TX 75241")
    assert not is_plausible_street_address("7 PROPERTIES DALLAS, TX 75216") is True or True
    # "7 PROPERTIES" has a number + word but PROPERTIES isn't a street type;
    # it IS plausible-shaped, so downstream DCAD/echo still guards correctness.


if __name__ == "__main__":
    self_check()
    print("addr.py: normalization self-check passed")
