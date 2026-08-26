# src/adapters/__init__.py
"""Two-tier adapter package for HERMES-REO external data access.

TIER 1 — SCRAPER ADAPTERS (classes)
    Subclass ``base.SourceAdapter``; implement
    ``fetch(**params) -> list[NormalizedLead]`` and expose a
    SourceProvenance-shaped ``provenance()``. See the two-tier convention
    in ``base.py`` for the contract and the grandfathered payload
    deviations (DallasCADAdapter -> dict, CollinClerkAdapter ->
    RecordedInstrument), both pinned by ``tests/test_adapter_contract.py``.

TIER 2 — SERVICE MODULES (plain function modules)
    ``census``, ``dallas_cad_bulk``, ``geojson_export``, ``notice_ocr``,
    ``route_optimizer`` are plain function namespaces and must NOT pretend
    subclassing of ``SourceAdapter``.

Imports here are lazy (PEP 562): importing ``src.adapters`` loads only the
dependency-free ``base`` vocabulary; scraper classes and service submodules
resolve on first attribute access, so e.g.
``import src.adapters.census`` stays free of Playwright/pypdf imports.
"""
from __future__ import annotations

import importlib

from .base import (
    NormalizedLead,
    SourceAdapter,
    SourceProvenance,
    dedupe_leads,
    lead_identity_component,
    normalize_address_for_key,
)

# Tier 1 — SCRAPER adapter class names exported by this package. The
# symbols themselves resolve lazily via __getattr__ below so that merely
# importing the package never drags in Playwright/pypdf/bs4.
SCRAPER_ADAPTERS = (
    "CollinForeclosureAdapter",
    "CollinClerkAdapter",
    "DallasForeclosureAdapter",
    "DallasCADAdapter",
    "GraysonTaxAdapter",
)

# Tier 2 — SERVICE function modules: plain-function namespaces, never
# SourceAdapter subclasses. Keep in sync with base.py's module docstring;
# tests/test_adapter_contract.py pins this roster.
SERVICE_MODULES = (
    "census",
    "dallas_cad_bulk",
    "geojson_export",
    "notice_ocr",
    "route_optimizer",
)

# scraper class name -> defining submodule
_SCRAPER_HOME = {
    "CollinForeclosureAdapter": "collin_foreclosure",
    "CollinClerkAdapter": "collin_clerk",
    "DallasForeclosureAdapter": "dallas_foreclosure",
    "DallasCADAdapter": "dallas_cad",
    "GraysonTaxAdapter": "grayson_tax",
}

__all__ = [
    # shared base vocabulary
    "NormalizedLead",
    "SourceAdapter",
    "SourceProvenance",
    "dedupe_leads",
    "lead_identity_component",
    "normalize_address_for_key",
    # tier registries (both tiers named explicitly)
    "SCRAPER_ADAPTERS",
    "SERVICE_MODULES",
]
__all__ += list(SCRAPER_ADAPTERS) + list(SERVICE_MODULES)


def __getattr__(name):
    # PEP 562 lazy export: scraper classes from their home modules, tier-2
    # service modules as submodules themselves.
    if name in _SCRAPER_HOME:
        module = importlib.import_module("." + _SCRAPER_HOME[name], __name__)
        return getattr(module, name)
    if name in SERVICE_MODULES:
        return importlib.import_module("." + name, __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_SCRAPER_HOME) | set(SERVICE_MODULES))
