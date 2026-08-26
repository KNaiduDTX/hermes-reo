# tests/test_registry_urls.py
"""TD3 — SOURCE-REGISTRY is the single URL authority for the adapters.

Every adapter module-level *_URL constant must equal the .url of the
registry endpoint it is bound to in src.source_registry.ADAPTER_URL_BINDINGS.
The adapters derive their constants via require_endpoint(...) at import, so
these tests fail only when someone reintroduces a hardcoded portal literal,
deletes/renames a registry entry, or lets the binding table go stale.

Census geocoder URLs (src/adapters/census.py) are deliberately OUT of scope:
they are federal endpoints, not county portal hosts.
"""
import importlib
import re
from pathlib import Path

import pytest

from src.source_registry import (
    ADAPTER_URL_BINDINGS,
    REGISTRY,
    assert_registry_sync,
    get_endpoint,
)

# The five adapter modules whose portal URLs are registry-bound (TD3 scope).
BOUND_ADAPTER_MODULES = sorted({m for m, _, _, _ in ADAPTER_URL_BINDINGS})

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)


def test_binding_table_covers_all_bound_constants():
    """Each (module, constant) pair is declared exactly once."""
    seen = [(m, c) for m, c, _, _ in ADAPTER_URL_BINDINGS]
    assert len(seen) == len(set(seen)), "duplicate (module, constant) binding"


def test_registry_has_no_duplicate_county_channel_pairs():
    """get_endpoint() returns the FIRST match, so duplicates would hide one URL."""
    keys = [(e.county, e.channel) for e in REGISTRY]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"duplicate (county, channel) registry entries: {dupes}"


@pytest.mark.parametrize("module_name,const_name,county,channel", ADAPTER_URL_BINDINGS)
def test_adapter_constant_matches_registry(module_name, const_name, county, channel):
    """Every adapter URL constant equals its bound registry endpoint url."""
    endpoint = get_endpoint(county, channel)
    assert endpoint is not None, (
        f"binding {module_name}.{const_name} references missing registry "
        f"endpoint ({county!r}, {channel!r})"
    )
    module = importlib.import_module(module_name)
    value = getattr(module, const_name, None)
    assert value is not None, f"{module_name}.{const_name} is not defined"
    assert value == endpoint.url, (
        f"{module_name}.{const_name} {value!r} drifted from registry "
        f"({county}, {channel}) url {endpoint.url!r}"
    )


@pytest.mark.parametrize("module_name,const_name,county,channel", ADAPTER_URL_BINDINGS)
def test_bound_urls_are_absolute_https(module_name, const_name, county, channel):
    """Bound endpoints must carry absolute https URLs (no empty/tbd stubs)."""
    endpoint = get_endpoint(county, channel)
    assert endpoint is not None
    assert endpoint.url.startswith("https://"), (
        f"({county}, {channel}) bound to non-https/stub url {endpoint.url!r}"
    )


def test_assert_registry_sync_passes():
    """The aggregate helper reports NO drift across the whole binding table."""
    # Raises AssertionError listing every drifted/unresolvable binding.
    assert_registry_sync()


@pytest.mark.parametrize("module_name", BOUND_ADAPTER_MODULES)
def test_no_hardcoded_portal_url_literals_in_adapters(module_name):
    """Adapter modules carry zero http(s) scheme literals (TD3).

    The single copy of every portal URL lives in src/source_registry.py;
    census.py's FEDERAL geocoder endpoints are out of scope by design.
    """
    source_path = _SRC_DIR.joinpath(*module_name.split(".")[1:]).with_suffix(".py")
    assert source_path.exists(), f"source file missing: {source_path}"
    matches = [
        f"line {i}: {line.strip()[:120]}"
        for i, line in enumerate(source_path.read_text(encoding="utf-8").splitlines(), 1)
        if _SCHEME_RE.search(line)
    ]
    assert not matches, (
        f"{module_name} reintroduced hardcoded URL literal(s) — derive them "
        f"from src.source_registry instead:\n  " + "\n  ".join(matches)
    )
