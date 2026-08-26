"""TD4 adapter-contract tests: enforce the two-tier adapter convention.

Tier 1 — every ``SourceAdapter`` subclass discovered via
``SourceAdapter.__subclasses__()`` must:

1. expose a keyword-callable ``fetch()`` — every parameter defaulted or a
   ``**kwargs`` sink, never positional-only, never ``*args``, never
   required-positional — so any adapter can be probed and driven
   uniformly through the base's ``fetch(**params)`` interface;
2. return a SourceProvenance-shaped object from its ``provenance()`` —
   duck-checked for ``.source`` (non-empty str), ``.retrieved_at``
   (ISO string) and ``.source_rigor_tier`` (int 0..3);
3. annotate the canonical lead-funnel payload
   ``list[NormalizedLead]`` on ``fetch`` — unless listed in
   FETCH_SHAPE_EXCEPTIONS with an explicit reason.

Known, grandfathered payload deviations (each with its reason below):
DallasCADAdapter.fetch -> dict and CollinClerkAdapter.fetch ->
list[RecordedInstrument]. If one of these adapters is ever wrapped to the
canonical shape, its registry entry becomes stale and the staleness guard
below fails until the entry is removed.

Tier 2 — service modules (census, dallas_cad_bulk, geojson_export,
notice_ocr, route_optimizer) are plain function modules and must NOT
define SourceAdapter subclasses locally (no pretend-subclassing shims).

All five scraper modules are imported up front so subclass discovery sees
every adapter. Everything here is offline: fetch() itself is never called;
network only happens inside fetch().
"""
from __future__ import annotations

import importlib
import inspect
from datetime import datetime

import pytest

import src.adapters as adapters_pkg
from src.adapters import SERVICE_MODULES, SCRAPER_ADAPTERS

# Importing each scraper module registers its adapter class on
# SourceAdapter.__subclasses__(); keeping them bound here documents that
# purpose and lets the identity checks below compare lazy package exports
# against their defining modules.
from src.adapters import (
    collin_clerk,
    collin_foreclosure,
    dallas_cad,
    dallas_foreclosure,
    grayson_tax,
)
from src.adapters.base import SourceAdapter, SourceProvenance

SCRAPER_MODULES = (
    collin_foreclosure,
    collin_clerk,
    dallas_foreclosure,
    dallas_cad,
    grayson_tax,
)

#: Roster pinned by TD4; discovery must find all of them (guards against a
#: silent rename/delete leaving an adapter outside the contract).
EXPECTED_ADAPTER_NAMES = frozenset({
    "CollinForeclosureAdapter",
    "CollinClerkAdapter",
    "DallasForeclosureAdapter",
    "DallasCADAdapter",
    "GraysonTaxAdapter",
})

#: Tier-2 roster as documented in base.py / adapters/__init__.py docstrings.
DOCUMENTED_SERVICE_MODULES = (
    "census",
    "dallas_cad_bulk",
    "geojson_export",
    "notice_ocr",
    "route_optimizer",
)

#: Adapter class name -> (return-annotation fragment, reason). The ONLY
#: allowed departures from the canonical list[NormalizedLead] payload.
FETCH_SHAPE_EXCEPTIONS = {
    "DallasCADAdapter": (
        "dict",
        "Legacy single-address DCAD detail lookup: fetch() returns the raw "
        "property-characteristics dict consumed by src/enrichment.py and "
        "dallas_cad_bulk.batch_cross_reference(); it is a property lookup, "
        "not a lead producer. Wrapping it into list[NormalizedLead] means "
        "editing dallas_cad.py and its call sites — not a contract-test "
        "concern.",
    ),
    "CollinClerkAdapter": (
        "RecordedInstrument",
        "Title/lien search channel (GATE-LEGAL-LIEN): fetch() returns "
        "official-records instruments consumed via search_encumbrances()/"
        "build_lien_stack(), not NormalizedLead funnel output.",
    ),
}


def _discover_adapters():
    """All SourceAdapter subclasses visible right now (BFS, depth-safe).

    Requires the scraper modules to be imported first (done above); walks
    recursively so an intermediate ABC introduced later is still traversed.
    """
    found = set()
    stack = [SourceAdapter]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub not in found:
                found.add(sub)
                stack.append(sub)
    return sorted(found, key=lambda c: c.__name__)


ADAPTERS = _discover_adapters()


def _keyword_signature_violations(func):
    """Parameters of ``func`` that break uniform keyword invocation."""
    violations = []
    for name, param in inspect.signature(func).parameters.items():
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            violations.append(f"'{name}' is positional-only")
        elif param.kind is inspect.Parameter.VAR_POSITIONAL:
            violations.append(f"'*{name}' cannot receive keyword arguments")
        elif (
            param.kind is not inspect.Parameter.VAR_KEYWORD
            and param.default is inspect.Parameter.empty
        ):
            violations.append(f"'{name}' has no default (breaks zero-arg probing)")
    return violations


# ------------------------------------------------------------- discovery --


def test_discovery_finds_expected_roster():
    names = {cls.__name__ for cls in ADAPTERS}
    missing = EXPECTED_ADAPTER_NAMES - names
    assert not missing, (
        f"SourceAdapter subclasses missing from __subclasses__() discovery: "
        f"{sorted(missing)} — were the adapter modules imported?"
    )


def test_every_scraper_module_registers_its_adapter():
    registered = {cls.__name__ for cls in ADAPTERS}
    for module in SCRAPER_MODULES:
        local_adapters = [
            obj.__name__
            for obj in vars(module).values()
            if isinstance(obj, type)
            and obj.__module__ == module.__name__
            and issubclass(obj, SourceAdapter)
            and obj is not SourceAdapter
        ]
        assert local_adapters, f"src.adapters.{module.__name__} defines no SourceAdapter subclass"
        unregistered = [n for n in local_adapters if n not in SCRAPER_ADAPTERS]
        assert not unregistered, (
            f"src.adapters.{module.__name__} defines {unregistered} but they are "
            f"not listed in adapters.SCRAPER_ADAPTERS"
        )
        assert set(local_adapters) <= registered


def test_every_real_adapter_is_listed_in_registry():
    for cls in ADAPTERS:
        if cls.__module__.split(".")[0] == "tests":
            continue  # tolerate test doubles defined inside other test modules
        assert cls.__name__ in SCRAPER_ADAPTERS, (
            f"{cls.__module__}.{cls.__name__} subclasses SourceAdapter but is not "
            f"listed in adapters.SCRAPER_ADAPTERS — register it or fix a rename"
        )
    phantoms = [n for n in SCRAPER_ADAPTERS if n not in {c.__name__ for c in ADAPTERS}]
    assert not phantoms, (
        f"adapters.SCRAPER_ADAPTERS lists classes discovery cannot see: {phantoms}"
    )


# ----------------------------------------------------------- conformance --


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda c: c.__name__)
class TestScraperConformance:
    def test_fetch_exists_and_is_keyword_callable(self, adapter_cls):
        name = adapter_cls.__name__
        assert callable(adapter_cls.fetch), f"{name}.fetch is not callable"
        # fetch must be overridden: the base implementation only raises.
        assert adapter_cls.fetch is not SourceAdapter.fetch, (
            f"{name} does not override fetch()"
        )
        # Bound method: self is consumed, so only real params are checked.
        violations = _keyword_signature_violations(adapter_cls().fetch)
        assert not violations, (
            f"{name}.fetch signature is not **params-compatible: {violations}; "
            f"give every parameter a default (or accept **kwargs)"
        )

    def test_provenance_returns_source_provenance_shape(self, adapter_cls):
        name = adapter_cls.__name__
        prov = adapter_cls().provenance()
        for attr in ("source", "retrieved_at", "source_rigor_tier"):
            assert hasattr(prov, attr), f"{name}.provenance() missing .{attr}"
        assert isinstance(prov.source, str) and prov.source.strip(), (
            f"{name}.provenance().source must be a non-empty str"
        )
        assert isinstance(prov.retrieved_at, str) and prov.retrieved_at.strip(), (
            f"{name}.provenance().retrieved_at must be a non-empty ISO str"
        )
        try:
            datetime.fromisoformat(prov.retrieved_at)
        except ValueError:
            pytest.fail(
                f"{name}.provenance().retrieved_at is not ISO-parseable: "
                f"{prov.retrieved_at!r}"
            )
        tier = prov.source_rigor_tier
        assert isinstance(tier, int) and not isinstance(tier, bool), (
            f"{name}.provenance().source_rigor_tier must be int, got {type(tier)}"
        )
        assert 0 <= tier <= 3, (
            f"{name}.provenance().source_rigor_tier={tier} outside 0..3 "
            f"(0=vendor_avm, 1=title_feed, 2=appraiser, 3=mls_agent)"
        )

    def test_fetch_return_annotation_matches_contract(self, adapter_cls):
        name = adapter_cls.__name__
        annotation = inspect.signature(adapter_cls.fetch).return_annotation
        ann_text = "" if annotation is inspect.Signature.empty else str(annotation)
        if "NormalizedLead" in ann_text:
            if name in FETCH_SHAPE_EXCEPTIONS:
                pytest.fail(
                    f"{name}.fetch now annotates {ann_text!r}: the "
                    f"FETCH_SHAPE_EXCEPTIONS entry is stale — remove it"
                )
            return
        if name in FETCH_SHAPE_EXCEPTIONS:
            fragment, reason = FETCH_SHAPE_EXCEPTIONS[name]
            assert fragment.lower() in ann_text.lower(), (
                f"{name} is registered as returning {fragment!r} but annotates "
                f"{ann_text!r}; update the entry. Reason on file: {reason}"
            )
            assert reason.strip(), f"{name}'s exception entry needs its reason kept"
            return
        pytest.fail(
            f"{name}.fetch annotates {ann_text!r}, not list[NormalizedLead]: "
            f"fix the adapter payload/annotation, or add it to "
            f"FETCH_SHAPE_EXCEPTIONS with an explicit reason"
        )


def test_provenance_defaults_are_source_provenance():
    # The shared default provenance() must itself satisfy the duck-shape.
    prov = SourceProvenance(source="unit_check")
    assert prov.source == "unit_check"
    datetime.fromisoformat(prov.retrieved_at)  # tz-aware ISO by default
    assert isinstance(prov.source_rigor_tier, int)


# ---------------------------------------------------------------- tier 2 --


@pytest.mark.parametrize("module_name", DOCUMENTED_SERVICE_MODULES)
def test_service_module_defines_no_source_adapter_subclasses(module_name):
    module = importlib.import_module(f"src.adapters.{module_name}")
    offenders = [
        obj.__qualname__
        for obj in vars(module).values()
        if isinstance(obj, type)
        and obj.__module__ == module.__name__  # ignore transitively imported names
        and issubclass(obj, SourceAdapter)
    ]
    assert not offenders, (
        f"src.adapters.{module_name} is a Tier-2 service module but defines "
        f"SourceAdapter subclasses {offenders} — plain functions only; construct "
        f"SourceProvenance directly instead of pretending subclassing"
    )


def test_service_registry_matches_documentation():
    assert tuple(SERVICE_MODULES) == DOCUMENTED_SERVICE_MODULES


# ------------------------------------------------------------ __all__ -----


def test_all_is_defined_unique_and_resolvable():
    exported = adapters_pkg.__all__
    assert exported, "adapters/__init__.py must export __all__"
    duplicates = [n for n in set(exported) if exported.count(n) > 1]
    assert not duplicates, f"duplicate names in adapters.__all__: {duplicates}"
    unresolved = [n for n in exported if not hasattr(adapters_pkg, n)]
    assert not unresolved, f"adapters.__all__ names that do not resolve: {unresolved}"


def test_all_names_both_tiers_explicitly():
    for name in SCRAPER_ADAPTERS:
        assert name in adapters_pkg.__all__, f"Tier-1 adapter {name} missing from __all__"
    for name in SERVICE_MODULES:
        assert name in adapters_pkg.__all__, f"Tier-2 module {name} missing from __all__"
    assert "SCRAPER_ADAPTERS" in adapters_pkg.__all__
    assert "SERVICE_MODULES" in adapters_pkg.__all__


def test_registry_names_resolve_to_subclasses():
    for name in SCRAPER_ADAPTERS:
        cls = getattr(adapters_pkg, name)
        assert isinstance(cls, type) and issubclass(cls, SourceAdapter), (
            f"adapters.{name} did not resolve to a SourceAdapter subclass"
        )
        assert cls.__name__ in {c.__name__ for c in ADAPTERS}


def test_lazy_exports_are_the_defining_module_objects():
    assert getattr(adapters_pkg, "CollinForeclosureAdapter") is \
        collin_foreclosure.CollinForeclosureAdapter
    assert getattr(adapters_pkg, "CollinClerkAdapter") is collin_clerk.CollinClerkAdapter
    assert getattr(adapters_pkg, "DallasForeclosureAdapter") is \
        dallas_foreclosure.DallasForeclosureAdapter
    assert getattr(adapters_pkg, "DallasCADAdapter") is dallas_cad.DallasCADAdapter
    assert getattr(adapters_pkg, "GraysonTaxAdapter") is grayson_tax.GraysonTaxAdapter
    for name in ("census", "route_optimizer"):
        submodule = importlib.import_module(f"src.adapters.{name}")
        assert getattr(adapters_pkg, name) is submodule


def test_unknown_attribute_still_raises_attribute_error():
    try:
        adapters_pkg.definitely_not_exported  # noqa: B018
    except AttributeError:
        pass
    else:
        pytest.fail("adapters package must raise AttributeError for unknown names")
