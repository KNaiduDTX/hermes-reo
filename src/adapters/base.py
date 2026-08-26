# src/adapters/base.py
"""Source adapter interface — all external I/O sits behind typed adapters.

Swap a source without touching underwriting logic.

Two-tier convention (TD4)
-------------------------
TIER 1 — SCRAPER ADAPTERS (classes, here in ``src/adapters/``)
    Subclass :class:`SourceAdapter` and implement
    ``fetch(**params) -> list[NormalizedLead]``; expose ``provenance()``
    returning a :class:`SourceProvenance`-shaped record (``.source``,
    ``.retrieved_at``, ``.source_rigor_tier``). Signatures must stay
    keyword-callable — every parameter defaulted or a ``**kwargs`` sink,
    never positional-only or required-positional — so callers can probe and
    drive any adapter uniformly. Members: CollinForeclosureAdapter,
    CollinClerkAdapter, DallasForeclosureAdapter, GraysonTaxAdapter,
    DallasCADAdapter. Grandfathered payload deviations, each pinned with an
    explicit reason in ``tests/test_adapter_contract.py``:

    - ``DallasCADAdapter.fetch -> dict``: legacy single-address DCAD detail
      lookup consumed by ``src/enrichment.py`` and
      ``dallas_cad_bulk.batch_cross_reference()``; a property lookup, not a
      lead producer.
    - ``CollinClerkAdapter.fetch -> list[RecordedInstrument]``: title/lien
      search channel (GATE-LEGAL-LIEN) consumed via
      ``search_encumbrances()``/``build_lien_stack()``; not the lead funnel.

TIER 2 — SERVICE MODULES (plain function modules)
    ``census``, ``dallas_cad_bulk``, ``geojson_export``, ``notice_ocr``,
    ``route_optimizer`` are plain function namespaces. They consume/produce
    CPRs and the shared dataclasses below but must NOT pretend subclassing:
    no shim class inheriting SourceAdapter just to borrow the interface. A
    service that needs provenance constructs ``SourceProvenance(...)``
    directly (see ``dallas_cad_bulk``).

``tests/test_adapter_contract.py`` discovers every SourceAdapter subclass
via ``__subclasses__()`` and enforces this contract, including the tier-2
negative assertion.
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def normalize_address_for_key(address: str) -> str:
    """Canonical form of an address for identity comparison (S10).

    Case-folded, ZIP+4 folded to bare ZIP, punctuation stripped, whitespace
    collapsed — so "100 Test St, Frisco, TX 75035" and
    "100  TEST  ST FRISCO TX 75035-1234" normalize identically.
    """
    cleaned = str(address or "").upper().strip()
    cleaned = re.sub(r"\b(\d{5})-\d{4}\b", r"\1", cleaned)  # fold ZIP+4
    cleaned = re.sub(r"[^A-Z0-9 ]+", " ", cleaned)
    return " ".join(cleaned.split())


def lead_identity_component(parcel_id: str, situs_address: str) -> str:
    """Stable per-parcel identity component for a dedupe key (S10).

    Uses parcel_id when present; otherwise falls back to an addr-prefixed
    hash of the normalized address. The old unconditional parcel_id slot made
    every provisional/empty-parcel lead with the same channel+sale_date
    collapse into one, silently dropping distinct properties.
    """
    pid = str(parcel_id or "").strip()
    if pid:
        return pid
    normalized = normalize_address_for_key(situs_address)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"addr:{digest}"


@dataclass
class NormalizedLead:
    """A normalized lead from any source, before CPR enrichment."""
    county: str          # collin, dallas, grayson
    parcel_id: str       # APN or entry_id (may be provisional)
    channel: str         # foreclosure_notice, tax_sale, tax_suit, cad, clerk, tax_office
    situs_address: str
    sale_date: Optional[str] = None
    owner_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    raw_data: dict = field(default_factory=dict)  # source-specific fields

    def dedupe_key(self) -> str:
        """county | parcel-or-addr-hash | channel | sale_date.

        S10: parcel_id when present, else an addr-prefixed hash of the
        normalized address so empty-parcel leads no longer collide.
        """
        sale = self.sale_date or ""
        identity = lead_identity_component(self.parcel_id, self.situs_address)
        return f"{self.county}|{identity}|{self.channel}|{sale}"


@dataclass
class SourceProvenance:
    """Provenance metadata for a source adapter."""
    source: str
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_rigor_tier: int = 0  # 0=vendor_avm, 1=title_feed, 2=appraiser, 3=mls_agent


class SourceAdapter:
    """Base class for Tier-1 scraper adapters (see module docstring).

    Contract: implement ``fetch(**params) -> list[NormalizedLead]`` with a
    keyword-callable signature (all params defaulted or a ``**kwargs``
    sink), and return a SourceProvenance-shaped record from provenance().
    The base provides provenance(). Tier-2 service modules (census,
    dallas_cad_bulk, geojson_export, notice_ocr, route_optimizer) do NOT
    subclass this.
    """
    channel: str = ""
    county: str = ""

    def fetch(self, **params: Any) -> list[NormalizedLead]:
        """Fetch leads from the source. Subclasses must implement."""
        raise NotImplementedError

    def provenance(self) -> SourceProvenance:
        """Return provenance metadata for this adapter."""
        return SourceProvenance(source=f"{self.county}_{self.channel}")


def dedupe_leads(leads: list[NormalizedLead]) -> list[NormalizedLead]:
    """Dedupe leads by stable key: county + parcel + channel + sale_date."""
    seen = set()
    result = []
    for lead in leads:
        key = lead.dedupe_key()
        if key not in seen:
            seen.add(key)
            result.append(lead)
    return result


if __name__ == "__main__":
    # Self-check: dedupe
    leads = [
        NormalizedLead(county="collin", parcel_id="R1", channel="foreclosure_notice",
                       situs_address="100 Test St", sale_date="2026-09-01"),
        NormalizedLead(county="collin", parcel_id="R1", channel="foreclosure_notice",
                       situs_address="100 Test St", sale_date="2026-09-01"),  # dup
        NormalizedLead(county="collin", parcel_id="R2", channel="foreclosure_notice",
                       situs_address="200 Test St", sale_date="2026-09-01"),
    ]
    deduped = dedupe_leads(leads)
    assert len(deduped) == 2, f"Expected 2, got {len(deduped)}"
    assert deduped[0].dedupe_key() == "collin|R1|foreclosure_notice|2026-09-01"

    # Self-check (S10): addr-hash fallback when parcel_id is absent — distinct
    # properties must no longer collide into one key.
    a = NormalizedLead(county="dallas", parcel_id="", channel="tax_sale",
                       situs_address="15806 Breedlove Place, Addison, TX 75001",
                       sale_date="2026-09-01")
    a_dup = NormalizedLead(county="dallas", parcel_id="   ", channel="tax_sale",
                           situs_address="15806  BREEDLOVE PLACE, ADDISON, TX 75001-6334",
                           sale_date="2026-09-01")  # same address, messy form
    b = NormalizedLead(county="dallas", parcel_id="", channel="tax_sale",
                       situs_address="42 Other St, Denison, TX 75020",
                       sale_date="2026-09-01")
    key_a, key_adup, key_b = a.dedupe_key(), a_dup.dedupe_key(), b.dedupe_key()
    assert key_a.startswith("dallas|addr:"), key_a
    assert key_a == key_adup, "normalized-address fallback failed to match messy duplicate"
    assert key_a != key_b, "distinct addresses collapsed under empty parcel_id"
    kept = dedupe_leads([a, a_dup, b])
    assert len(kept) == 2, f"fallback dedupe expected 2 survivors, got {len(kept)}"
    # normalization is deterministic and punctuation-insensitive
    assert normalize_address_for_key("100 Test St., Frisco, TX!") == \
           normalize_address_for_key("100 test st frisco tx")

    print("base.py: all self-checks passed (incl. S10 addr-fallback dedupe key)")