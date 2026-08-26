# src/cpr.py
"""Canonical Property Record (CPR) — append-only record per parcel.

Every field carries {value, source, retrieved_at, confidence}. Nothing
overwrites; corrections are new receipts appended to the provenance ledger.
"""
from __future__ import annotations
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
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

# Outcome of the last title/lien search on this parcel (audit S7: a FAILED
# lookup must never be indistinguishable from clean title).
LIEN_LOOKUP_STATUSES = ("not_run", "ok", "error")


def pv_value(x: Any) -> Any:
    """Return ``x.value`` for a ProvenanceField, else *x* itself.

    Use everywhere a field may or may not be provenance-wrapped so callers
    never compare an object against a sentinel string (audit H7).
    """
    return x.value if isinstance(x, ProvenanceField) else x


@dataclass
class ProvenanceField:
    """A single field value with provenance metadata."""
    value: Any
    source: str
    retrieved_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    confidence: float = 0.0  # 0.0-1.0

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source,
                "retrieved_at": self.retrieved_at, "confidence": self.confidence}


@dataclass
class ProvenanceEntry:
    """An entry in the append-only provenance ledger."""
    field_name: str
    old_value: Any
    new_value: Any
    source: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    prev_hash: str = ""
    entry_hash: str = ""
    note: str = ""  # e.g. "undeclared-field" for county-specific extras (audit D4)

    def compute_hash(self) -> str:
        """Compute hash of this entry (tamper-evident chain)."""
        payload = (f"{self.field_name}|{self.old_value}|{self.new_value}|{self.source}"
                   f"|{self.timestamp}|{self.prev_hash}|{self.note}")
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

    def to_dict(self) -> dict[str, Any]:
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


def coerce_blast_tier_like(value, default: "BlastTier") -> "BlastTier":
    """Best-effort BlastTier coercion shared by from_dict callers."""
    if value is None:
        return default
    try:
        return BlastTier(int(value))
    except (TypeError, ValueError):
        if isinstance(value, str):
            cleaned = value.strip().upper().split(".")[-1]
            try:
                return BlastTier[cleaned]
            except KeyError:
                pass
    return default


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

    # Distress detail (audit TD1: declared so tooling sees them; set_field
    # still routes writes through the receipted provenance path)
    taxing_entities: Optional[ProvenanceField] = None
    deed_number: Optional[ProvenanceField] = None
    owner_deed_date: Optional[ProvenanceField] = None
    commercial: Optional[ProvenanceField] = None
    stories: Optional[ProvenanceField] = None
    pool: Optional[ProvenanceField] = None
    isd: Optional[ProvenanceField] = None
    mapsco: Optional[ProvenanceField] = None
    census_tract: Optional[ProvenanceField] = None
    census_block: Optional[ProvenanceField] = None
    census_block_group: Optional[ProvenanceField] = None
    county_fips: Optional[ProvenanceField] = None
    state_fips: Optional[ProvenanceField] = None
    city: Optional[ProvenanceField] = None
    cad_appraised_value_2025: Optional[ProvenanceField] = None
    foreclosing_lender: Optional[ProvenanceField] = None
    substitute_trustee: Optional[ProvenanceField] = None
    scheduled_sale_time: Optional[ProvenanceField] = None
    original_principal: Optional[ProvenanceField] = None
    unpaid_principal_balance: Optional[ProvenanceField] = None
    all_extracted_balances: Optional[ProvenanceField] = None
    dcad_cross_ref_needed: Optional[ProvenanceField] = None

    # Operational scratch (plain values, not provenance-managed)
    dcad_xref_error: str = ""
    dcad_xref_status: str = ""
    lien_lookup_error: str = ""

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
    # Backing store for the managed `lien_lookup_status` attribute below.
    _lien_lookup_status: str = "not_run"

    # Provenance ledger (append-only)
    ledger: list[ProvenanceEntry] = field(default_factory=list)

    @property
    def lien_lookup_status(self) -> str:
        """Outcome of the last title/lien search: not_run | ok | error (audit S7)."""
        return self._lien_lookup_status

    @lien_lookup_status.setter
    def lien_lookup_status(self, status: str) -> None:
        if status not in LIEN_LOOKUP_STATUSES:
            raise ValueError(
                f"lien_lookup_status must be one of {LIEN_LOOKUP_STATUSES}, got {status!r}"
            )
        self._lien_lookup_status = status

    def _ledger_hash(self) -> str:
        return self.ledger[-1].entry_hash if self.ledger else ""

    def pv_value(self, x: Any) -> Any:
        """Unwrap a possibly-provenance field (instance form of module pv_value)."""
        return pv_value(x)

    @staticmethod
    def _validate_confidence(confidence: float, context: str) -> None:
        """Loud failure on out-of-range confidence (audit D3/D4).

        NaN fails the comparison chain and is rejected too.
        """
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(
                f"confidence must be within 0..1 for {context}, got {confidence!r}"
            )

    def set_field(self, field_name: str, value: Any, source: str,
                  confidence: float = 0.0) -> None:
        """Set a field and append to the provenance ledger.

        Confidence outside 0..1 raises ValueError. Undeclared field names are
        still allowed (county enrichers attach extras like ``deed_number`` or
        ``isd``), but their receipts carry note="undeclared-field" so silent
        attribute typos are at least visible in the ledger (audit D4).
        """
        self._validate_confidence(confidence, f"set_field({field_name!r})")

        old = getattr(self, field_name, None)
        old_val = pv_value(old)

        pf = ProvenanceField(value=value, source=source, confidence=confidence)
        setattr(self, field_name, pf)

        declared = {f.name for f in dataclasses.fields(self.__class__)}
        entry = ProvenanceEntry(
            field_name=field_name, old_value=old_val, new_value=value,
            source=source, prev_hash=self._ledger_hash(),
            note="" if field_name in declared else "undeclared-field",
        )
        entry.entry_hash = entry.compute_hash()
        self.ledger.append(entry)

    def add_lien(self, lien: LienRecord, source: str,
                 confidence: float = 0.0) -> None:
        """Add a lien to the encumbrance stack.

        The ledger receipt records the amount alongside lien_type and both
        survival flags (audit D4: priority/survival were previously
        unreceipted, so a corrupted flag left no trail). Confidence is
        validated like set_field's.
        """
        self._validate_confidence(confidence, f"add_lien({lien.lien_type!r})")
        self.liens.append(lien)
        entry = ProvenanceEntry(
            field_name=f"lien:{lien.lien_type}", old_value=None,
            new_value={
                "amount": lien.amount,
                "lien_type": lien.lien_type,
                "survives_foreclosure": lien.survives_foreclosure,
                "survives_tax_sale": lien.survives_tax_sale,
            },
            source=source, prev_hash=self._ledger_hash(),
        )
        entry.entry_hash = entry.compute_hash()
        self.ledger.append(entry)

    def move_column(self, new_column: CPRColumn, reason: str = "") -> None:
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
        sale = pv_value(self.sale_date) if self.sale_date else ""
        return f"{self.county}|{self.parcel_id}|{self.channel}|{sale}"

    def get(self, name: str, default: Any = None) -> Any:
        """TD1 typed accessor: provenance-unwrapped value or *default*.

        Reads declared fields (ProvenanceField values are unwrapped) and
        falls back to *default* for anything unset/unknown — replacing the
        hasattr walls in consumers (find_deals/synthesis/geojson_export).
        """
        if not hasattr(self, name):
            return default
        val = getattr(self, name)
        if val is None:
            return default
        return pv_value(val)


    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict (for JSON export / DB storage)."""
        d = {"lien_lookup_status": self.lien_lookup_status}
        for k, v in self.__dict__.items():
            if k.startswith("_"):
                continue  # managed-attribute backing stores are exposed above
            elif k == "ledger":
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

    @classmethod
    def from_dict(cls, raw: dict, path_label: str = "cpr",
                  include_valuation: bool = False,
                  default_blast_tier: "BlastTier" = BlastTier.T0,
                  on_warn=None) -> "CPR":
        """TD5: single inverse of to_dict() (absorbs the former 82-line
        4-pass loader buried in src/hitl.py).

        Reconstructs identity, declared/provenance fields, ledger (replayed
        verbatim), liens (receipted via add_lien, chaining onto restored
        history) and pipeline state. With include_valuation=True the stored
        valuation object is rebuilt too; HITL flows pass False because the
        human run replaces it wholesale.
        """
        def _warn(msg: str) -> None:
            (on_warn or (lambda m: None))(msg)

        if not isinstance(raw, dict):
            raise ValueError(f"{path_label}: expected a CPR dict at top level, "
                             f"got {type(raw).__name__}")
        cpr = cls()
        declared = {f.name for f in dataclasses.fields(cls)}

        def _pf(d):
            if d is None:
                return None
            return ProvenanceField(value=d.get("value"), source=str(d.get("source") or ""),
                                   retrieved_at=str(d.get("retrieved_at") or ""),
                                   confidence=float(d.get("confidence") or 0.0))

        skip = ("valuation", "liens", "ledger", "column", "blast_tier",
                "lien_lookup_status")
        for key, val in raw.items():
            if key in skip:
                continue
            if key == "lien_priority_ranking":
                cpr.lien_priority_ranking = [str(x) for x in (val or [])]
            elif isinstance(val, dict) and {"value", "source"} <= set(val):
                setattr(cpr, key, _pf(val))
            elif key in declared or not isinstance(val, (dict, list)):
                try:
                    setattr(cpr, key, val)
                except (AttributeError, TypeError) as exc:
                    _warn(f"{path_label}: could not restore field {key!r}: {exc}")

        for entry in raw.get("ledger") or []:
            if not isinstance(entry, dict):
                continue
            e = ProvenanceEntry(
                field_name=str(entry.get("field_name") or ""),
                old_value=entry.get("old_value"), new_value=entry.get("new_value"),
                source=str(entry.get("source") or ""),
                timestamp=str(entry.get("timestamp") or ""),
                prev_hash=str(entry.get("prev_hash") or ""),
                note=str(entry.get("note") or ""))
            e.entry_hash = str(entry.get("entry_hash") or "")
            cpr.ledger.append(e)

        for ld in raw.get("liens") or []:
            if not isinstance(ld, dict):
                continue
            cpr.add_lien(LienRecord(
                lien_type=str(ld.get("lien_type") or "unknown"),
                amount=(float(ld["amount"]) if ld.get("amount") is not None else None),
                recording_date=ld.get("recording_date"), lender=ld.get("lender"),
                priority=int(ld.get("priority") or 0),
                survives_foreclosure=bool(ld.get("survives_foreclosure")),
                survives_tax_sale=bool(ld.get("survives_tax_sale"))),
                source="cpr_rehydrate", confidence=0.0)

        cpr.blast_tier = coerce_blast_tier_like(raw.get("blast_tier"), default_blast_tier)
        col = raw.get("column")
        if col is not None:
            try:
                cpr.column = CPRColumn(str(col))
            except ValueError:
                _warn(f"{path_label}: unrecognized column {col!r}; keeping {cpr.column.value}")
        status = raw.get("lien_lookup_status")
        if status is not None:
            try:
                cpr.lien_lookup_status = str(status)
            except ValueError:
                _warn(f"{path_label}: invalid lien_lookup_status {status!r}")
        if raw.get("dead_reason"):
            cpr.dead_reason = str(raw["dead_reason"])

        if include_valuation:
            vd = raw.get("valuation")
            if isinstance(vd, dict):
                comps = [CompRecord(**{k: v for k, v in cd.items()
                                        if k in CompRecord.__dataclass_fields__})
                         for cd in (vd.get("comp_set") or []) if isinstance(cd, dict)]
                cpr.valuation = ValuationObject(
                    as_is_value=_pf(vd.get("as_is_value")),
                    arv=_pf(vd.get("arv")),
                    rent_estimate=_pf(vd.get("rent_estimate")),
                    comp_set=comps,
                    comp_source=str(vd.get("comp_source") or ""),
                    gate_val_verdict=str(vd.get("gate_val_verdict") or ""),
                    gate_val_reason=str(vd.get("gate_val_reason") or ""))
        return cpr


if __name__ == "__main__":
    import math

    # Self-check: CPR append-only + provenance chain
    cpr = CPR(parcel_id="R123456", county="collin", situs_address="100 Test St, Frisco, TX 75035")
    cpr.set_field("owner_name", "John Doe", "collin_cad", 0.9)
    assert cpr.owner_name.value == "John Doe"
    assert len(cpr.ledger) == 1
    assert cpr.ledger[0].entry_hash != ""
    assert cpr.ledger[0].note == ""  # declared field — no note

    # Append a correction — old value preserved
    cpr.set_field("owner_name", "John & Jane Doe", "collin_clerk", 0.95)
    assert cpr.owner_name.value == "John & Jane Doe"
    assert len(cpr.ledger) == 2
    assert cpr.ledger[1].old_value == "John Doe"
    assert cpr.ledger[1].prev_hash == cpr.ledger[0].entry_hash  # chain intact

    # D3: confidence must be within 0..1 — loud failure otherwise
    for bad in (1.5, -0.1, math.nan):
        try:
            cpr.set_field("owner_name", "X", "test", bad)
            raise AssertionError(f"expected ValueError for confidence={bad!r}")
        except ValueError:
            pass
    assert cpr.owner_name.value == "John & Jane Doe"  # rejected writes left no trace
    assert len(cpr.ledger) == 2

    # D4-light: undeclared names allowed but receipted with a note (enrichers rely
    # on this). deed_number is a DECLARED field as of TD1 — use an unknown name.
    cpr.set_field("some_future_field", "x", "collin_clerk", 0.9)
    assert cpr.some_future_field.value == "x"
    assert cpr.ledger[-1].note == "undeclared-field"

    # TD1: declared fields receipt WITHOUT the undeclared note.
    cpr.set_field("deed_number", "20130806001104940", "collin_clerk", 0.9)
    assert cpr.deed_number.value == "20130806001104940"
    assert cpr.ledger[-1].note != "undeclared-field"

    # pv_value unwraps provenance fields, passes anything else through
    assert pv_value(cpr.owner_name) == "John & Jane Doe"
    assert pv_value("plain") == "plain"
    assert pv_value(None) is None

    # Lien addition — receipt now carries lien_type + survival flags alongside amount
    cpr.add_lien(LienRecord(lien_type="mortgage", amount=350_000, priority=1,
                            survives_foreclosure=False, survives_tax_sale=False),
                 "collin_clerk", confidence=0.9)
    assert len(cpr.liens) == 1
    assert len(cpr.ledger) == 5  # 2 owner writes + undeclared + declared + lien receipt
    lien_receipt = cpr.ledger[-1]
    assert lien_receipt.field_name == "lien:mortgage"
    assert lien_receipt.new_value == {"amount": 350_000, "lien_type": "mortgage",
                                      "survives_foreclosure": False,
                                      "survives_tax_sale": False}
    try:
        cpr.add_lien(LienRecord(lien_type="hoa", amount=1_000), "test", confidence=2.0)
        raise AssertionError("expected ValueError for add_lien confidence=2.0")
    except ValueError:
        pass
    assert len(cpr.liens) == 1 and len(cpr.ledger) == 5

    # Managed attribute lien_lookup_status: default + validated values (S7)
    assert cpr.lien_lookup_status == "not_run"
    cpr.lien_lookup_status = "ok"
    assert cpr.lien_lookup_status == "ok" and cpr._lien_lookup_status == "ok"
    try:
        cpr.lien_lookup_status = "clean"
        raise AssertionError("expected ValueError for invalid lien_lookup_status")
    except ValueError:
        pass

    # Column move (receipted)
    cpr.move_column(CPRColumn.ENRICHED)
    assert cpr.column == CPRColumn.ENRICHED
    assert len(cpr.ledger) == 6  # + column-move receipt

    # Dedupe key
    cpr.channel = "foreclosure_notice"
    cpr.set_field("sale_date", "2026-09-01", "collin_foreclosure", 1.0)
    key = cpr.dedupe_key()
    assert "collin|R123456|foreclosure_notice|2026-09-01" == key

    # DEAD with reason code
    cpr.move_column(CPRColumn.DEAD, reason="over_mao")
    assert cpr.dead_reason == "over_mao"

    # Serialization exposes the managed attribute, not its backing store
    d = cpr.to_dict()
    assert d["lien_lookup_status"] == "ok"  # last value set above
    assert "_lien_lookup_status" not in d
    assert not any(k.startswith("_") for k in d)

    print("cpr.py: all self-checks passed")