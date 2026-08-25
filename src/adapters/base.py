# src/adapters/base.py
"""Source adapter interface — all external I/O sits behind typed adapters.

Swap a source without touching underwriting logic.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


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
        sale = self.sale_date or ""
        return f"{self.county}|{self.parcel_id}|{self.channel}|{sale}"


@dataclass
class SourceProvenance:
    """Provenance metadata for a source adapter."""
    source: str
    retrieved_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    source_rigor_tier: int = 0  # 0=vendor_avm, 1=title_feed, 2=appraiser, 3=mls_agent


class SourceAdapter:
    """Base class for all source adapters.

    Subclasses implement fetch(). The base provides provenance().
    """
    channel: str = ""
    county: str = ""

    def fetch(self, **params) -> list[NormalizedLead]:
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
    print("base.py: all self-checks passed")