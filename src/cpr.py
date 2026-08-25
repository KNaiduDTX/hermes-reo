# src/cpr.py
"""Canonical Property Record (CPR) — append-only record per parcel.

Every field carries {value, source, retrieved_at, confidence}. Nothing
overwrites; corrections are new receipts appended to the provenance ledger.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Optional
from enum import Enum
import hashlib
import json


class BlastTier(int, Enum):
    T0 = 0  # screen only, no money
    T1 = 1  # small (low assignment / small cash)
    T2 = 2  # mid (agent-MLS or BPO comps required)
    T3 = 3  # large / auction cash / hold (appraiser-grade + attorney)


class CPRColumn(str, Enum):
    """Kanban board columns — each card = one parcel = one CPR."""
    INBOX = "INBOX"
    TRIAGE = "TRIAGE"
    ENRICHED = "ENRICHED"
    VALUED = "VALUED"
    UNDERWRITTEN = "UNDERWRITTEN"
    PRICED = "PRICED"
    OFFER_OUT = "OFFER-OUT"
    UNDER_CONTRACT = "UNDER-CONTRACT"
    OPTION_PERIOD = "OPTION-PERIOD"
    TITLE_ESCROW = "TITLE/ESCROW"
    CLEAR_TO_CLOSE = "CLEAR-TO-CLOSE"
    CLOSED = "CLOSED"
    REHAB_HOLD = "REHAB/HOLD"
    REDEMPTION_WATCH = "REDEMPTION-WATCH"
    DISPOSED = "DISPOSED"
    DEAD = "DEAD"


DEAD_REASON_CODES = [
    "over_mao", "title_fatal", "low_confidence_comp", "redemption_risk",
    "lost_at_auction", "seller_unresponsive", "stale_notice",
    "identity_mismatch", "disclosure_gap",
]


@dataclass
class ProvenanceField:
    """A single field value with provenance metadata."""
    value: Any
    source: str
    retrieved_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    confidence: float = 0.0  # 0.0-1.0

    def to_dict(self) -> dict:
        return {"value": self.value, "source": self.source,
                "retrieved_at": self.retrieved_at, "confidence": self.confidence}


@dataclass
class ProvenanceEntry:
    """An entry in the append-only provenance ledger."""
    field_name: str
    old_value: Any
    new_value: Any
    source: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    prev_hash: str = ""
    entry_hash: str = ""

    def compute_hash(self) -> str:
        """Compute hash of this entry (tamper-evident chain)."""
        payload = f"{self.field_name}|{self.old_value}|{self.new_value}|{self.source}|{self.timestamp}|{self.prev_hash}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class CompRecord:
    """A single comparable sale."""
    address: str
    sale_price: Optional[float] = None
    sale_date: Optional[str] = None
    distance_miles: Optional[float] = None
    sqft: Optional[int] = None
    beds: Optional[int] = None
    baths: Optional[float] = None
    source: str = ""
    adjusted_price: Optional[float] = None  # after sqft/bed/bath/condition adjustments


@dataclass
class ValuationObject:
    """ARV / as-is / rent estimate with comp set + provenance + confidence."""
    as_is_value: Optional[ProvenanceField] = None
    arv: Optional[ProvenanceField] = None
    rent_estimate: Optional[ProvenanceField] = None
    comp_set: list[CompRecord] = field(default_factory=list)
    comp_source: str = ""  # mls_agent, appraiser_bpo, title_feed, vendor_avm
    gate_val_verdict: str = ""  # pass, fail, hitl
    gate_val_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "as_is_value": self.as_is_value.to_dict() if self.as_is_value else None,
            "arv": self.arv.to_dict() if self.arv else None,
            "rent_estimate": self.rent_estimate.to_dict() if self.rent_estimate else None,
            "comp_set": [c.__dict__ for c in self.comp_set],
            "comp_source": self.comp_source,
            "gate_val_verdict": self.gate_val_verdict,
            "gate_val_reason": self.gate_val_reason,
        }


@dataclass
class LienRecord:
    """A single encumbrance on the property."""
    lien_type: str  # mortgage, hoa, municipal, irs_federal, judgment, tax
    amount: Optional[float] = None
    recording_date: Optional[str] = None
    lender: Optional[str] = None
    priority: int = 0  # 1 = senior (first), 2 = junior, etc.
    survives_foreclosure: bool = False
    survives_tax_sale: bool = False


@dataclass
class CPR:
    """Canonical Property Record — one per parcel, append-only."""
    # Identity
    parcel_id: str = ""  # APN / parcel number
    county: str = ""
    situs_address: str = ""
    legal_description: str = ""
    gis_lat: Optional[float] = None
    gis_lng: Optional[float] = None

    # Ownership
    owner_name: Optional[ProvenanceField] = None
    owner_mailing_address: Optional[ProvenanceField] = None
    ownership_length_years: Optional[ProvenanceField] = None
    absentee: Optional[ProvenanceField] = None
    deceased_estate: Optional[ProvenanceField] = None

    # Characteristics
    land_sqft: Optional[ProvenanceField] = None
    improvement_sqft: Optional[ProvenanceField] = None
    beds: Optional[ProvenanceField] = None
    baths: Optional[ProvenanceField] = None
    year_built: Optional[ProvenanceField] = None
    property_type: Optional[ProvenanceField] = None
    homestead: Optional[ProvenanceField] = None
    agricultural: Optional[ProvenanceField] = None

    # CAD valuation (baseline only — not market)
    cad_appraised_value: Optional[ProvenanceField] = None
    cad_tax_year: Optional[ProvenanceField] = None

    # Tax
    tax_delinquent: Optional[ProvenanceField] = None
    tax_delinquent_amount: Optional[ProvenanceField] = None
    tax_suit_case_number: Optional[ProvenanceField] = None

    # Encumbrances
    liens: list[LienRecord] = field(default_factory=list)
    lien_priority_ranking: list[str] = field(default_factory=list)

    # Distress state
    channel: str = ""  # foreclosure_notice, tax_sale, tax_suit, etc.
    deal_type: str = ""
    nod_status: Optional[ProvenanceField] = None
    sale_date: Optional[ProvenanceField] = None
    opening_bid: Optional[ProvenanceField] = None
    redemption_class: Optional[ProvenanceField] = None
    redemption_expiry: Optional[ProvenanceField] = None
    struck_off: Optional[ProvenanceField] = None

    # Valuation (from AGENT-VAL)
    valuation: Optional[ValuationObject] = None

    # Pipeline state
    blast_tier: BlastTier = BlastTier.T0
    column: CPRColumn = CPRColumn.INBOX
    dead_reason: str = ""

    # Provenance ledger (append-only)
    ledger: list[ProvenanceEntry] = field(default_factory=list)

    def _ledger_hash(self) -> str:
        return self.ledger[-1].entry_hash if self.ledger else ""

    def set_field(self, field_name: str, value: Any, source: str, confidence: float = 0.0):
        """Set a field and append to the provenance ledger."""
        old = getattr(self, field_name, None)
        old_val = old.value if isinstance(old, ProvenanceField) else old

        pf = ProvenanceField(value=value, source=source, confidence=confidence)
        setattr(self, field_name, pf)

        entry = ProvenanceEntry(
            field_name=field_name, old_value=old_val, new_value=value,
            source=source, prev_hash=self._ledger_hash(),
        )
        entry.entry_hash = entry.compute_hash()
        self.ledger.append(entry)

    def add_lien(self, lien: LienRecord, source: str):
        """Add a lien to the encumbrance stack."""
        self.liens.append(lien)
        entry = ProvenanceEntry(
            field_name=f"lien:{lien.lien_type}", old_value=None,
            new_value=lien.amount, source=source, prev_hash=self._ledger_hash(),
        )
        entry.entry_hash = entry.compute_hash()
        self.ledger.append(entry)

    def move_column(self, new_column: CPRColumn, reason: str = ""):
        """Move to a new Kanban column (receipted)."""
        old = self.column
        self.column = new_column
        if new_column == CPRColumn.DEAD and reason:
            self.dead_reason = reason
        entry = ProvenanceEntry(
            field_name="column", old_value=old.value, new_value=new_column.value,
            source="pipeline", prev_hash=self._ledger_hash(),
        )
        entry.entry_hash = entry.compute_hash()
        self.ledger.append(entry)

    def dedupe_key(self) -> str:
        """Stable dedupe key: county + parcel_id + channel + sale_date."""
        sale = self.sale_date.value if self.sale_date else ""
        return f"{self.county}|{self.parcel_id}|{self.channel}|{sale}"

    def to_dict(self) -> dict:
        """Serialize to dict (for JSON export / DB storage)."""
        d = {}
        for k, v in self.__dict__.items():
            if k == "ledger":
                d[k] = [e.__dict__ for e in v]
            elif k == "liens":
                d[k] = [l.__dict__ for l in v]
            elif k == "valuation":
                d[k] = v.to_dict() if v else None
            elif isinstance(v, ProvenanceField):
                d[k] = v.to_dict()
            elif isinstance(v, (CPRColumn, BlastTier)):
                d[k] = v.value
            else:
                d[k] = v
        return d


if __name__ == "__main__":
    # Self-check: CPR append-only + provenance chain
    cpr = CPR(parcel_id="R123456", county="collin", situs_address="100 Test St, Frisco, TX 75035")
    cpr.set_field("owner_name", "John Doe", "collin_cad", 0.9)
    assert cpr.owner_name.value == "John Doe"
    assert len(cpr.ledger) == 1
    assert cpr.ledger[0].entry_hash != ""

    # Append a correction — old value preserved
    cpr.set_field("owner_name", "John & Jane Doe", "collin_clerk", 0.95)
    assert cpr.owner_name.value == "John & Jane Doe"
    assert len(cpr.ledger) == 2
    assert cpr.ledger[1].old_value == "John Doe"
    assert cpr.ledger[1].prev_hash == cpr.ledger[0].entry_hash  # chain intact

    # Lien addition
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=350_000, priority=1,
                            survives_foreclosure=False, survives_tax_sale=False),
                 "collin_clerk")
    assert len(cpr.liens) == 1
    assert len(cpr.ledger) == 3

    # Column move (receipted)
    cpr.move_column(CPRColumn.ENRICHED)
    assert cpr.column == CPRColumn.ENRICHED
    assert len(cpr.ledger) == 4

    # Dedupe key
    cpr.channel = "foreclosure_notice"
    cpr.set_field("sale_date", "2026-09-01", "collin_foreclosure", 1.0)
    key = cpr.dedupe_key()
    assert "collin|R123456|foreclosure_notice|2026-09-01" == key

    # DEAD with reason code
    cpr.move_column(CPRColumn.DEAD, reason="over_mao")
    assert cpr.dead_reason == "over_mao"

    print("cpr.py: all self-checks passed")