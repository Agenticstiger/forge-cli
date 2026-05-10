# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Property-based drift tests for the FLUID → engine-SDK alias tables.

The introspectors in ``dlt/destinations.py`` and ``airbyte/destinations.py``
each carry a small alias table mapping FLUID-canonical field names to the
engine SDK's actual field names (e.g. FLUID's ``account`` → dlt-snowflake's
``host``; FLUID's ``user`` → PyAirbyte's ``username``).

When an upstream SDK renames a field (dlt 1.4 → 1.5, PyAirbyte 0.X), our
alias tables silently break: introspection sets the old name, the SDK
ignores it, and the runner falls back to a misleading "missing credential"
error at production-run time.

These tests close the loop by parametrizing over every alias entry and
asserting the *target* name actually exists in the live SDK. CI catches
the drift the moment a `pip install -U dlt` lands.

Tests are auto-skipped when the SDK isn't installed (some CI tiers don't
ship dlt/PyAirbyte). When installed, drift = test failure.
"""

from __future__ import annotations

import inspect
from typing import Iterable, Tuple

import pytest

from fluid_build.build_runners.airbyte.destinations import (
    _FLUID_TO_PYAIRBYTE_FIELD,
)
from fluid_build.build_runners.dlt.destinations import _FLUID_TO_DLT_FIELD

# ── Helpers ──────────────────────────────────────────────────────────


def _flatten(table: dict) -> Iterable[Tuple[str, str, str]]:
    """Flatten ``{platform: {fluid: sdk}}`` → ``[(platform, fluid, sdk), ...]``."""
    for platform, mapping in table.items():
        for fluid_name, sdk_name in mapping.items():
            yield platform, fluid_name, sdk_name


def _dlt_sdk_fields(platform: str) -> set:
    """Return the live dlt destination credentials field set for ``platform``.

    Mirrors the discovery logic in ``_dlt_introspect`` so any change there
    is reflected here.
    """
    import dlt  # type: ignore[import-not-found]

    dest_factory = getattr(dlt.destinations, platform.lower(), None)
    if dest_factory is None:
        pytest.skip(f"dlt has no destination named {platform!r}")
    spec = dest_factory().spec()
    cred_class = None
    ct = getattr(spec, "credentials_type", None)
    if callable(ct):
        try:
            cred_class = ct()
        except TypeError:
            cred_class = ct
    if cred_class is None or not isinstance(cred_class, type):
        cred_class = getattr(spec, "credentials_class", None)
    if cred_class is None:
        pytest.skip(f"dlt destination {platform!r} has no credentials class")
    if hasattr(cred_class, "__dataclass_fields__"):
        return {f for f in cred_class.__dataclass_fields__ if not f.startswith("_")}
    if hasattr(cred_class, "model_fields"):
        return {f for f in cred_class.model_fields if not f.startswith("_")}
    if hasattr(cred_class, "__fields__"):
        return {f for f in cred_class.__fields__ if not f.startswith("_")}
    pytest.skip(f"Could not introspect fields for dlt {platform!r}")


def _pyairbyte_cache_params(platform: str) -> set:
    """Return the live PyAirbyte cache constructor field set for ``platform``.

    PyAirbyte caches are Pydantic v2 BaseModels with generic ``(**data)``
    constructors, so ``model_fields`` is the canonical source — falling back
    to v1 ``__fields__``, dataclass fields, then signature inspection for
    forward / backward compat.
    """
    import airbyte as ab  # type: ignore[import-not-found]

    cache_cls = getattr(ab.caches, f"{platform.title()}Cache", None)
    if cache_cls is None:
        pytest.skip(f"PyAirbyte has no {platform.title()}Cache class")
    if hasattr(cache_cls, "model_fields"):
        return {f for f in cache_cls.model_fields if not f.startswith("_")}
    if hasattr(cache_cls, "__fields__"):
        return {f for f in cache_cls.__fields__ if not f.startswith("_")}
    if hasattr(cache_cls, "__dataclass_fields__"):
        return {f for f in cache_cls.__dataclass_fields__ if not f.startswith("_")}
    sig = inspect.signature(cache_cls.__init__)
    return {
        name
        for name, param in sig.parameters.items()
        if name not in ("self", "args", "kwargs")
        and param.kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        )
    }


# ── dlt drift ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "platform,fluid_name,dlt_name", list(_flatten(_FLUID_TO_DLT_FIELD))
)
def test_dlt_alias_target_exists_in_sdk(
    platform: str, fluid_name: str, dlt_name: str
):
    """Every (platform, fluid → dlt) alias must point at a real dlt field.

    Silent drift here = production-run "missing credential" errors that
    look like config bugs but are actually SDK rename fallout.
    """
    pytest.importorskip("dlt")
    sdk_fields = _dlt_sdk_fields(platform)
    assert dlt_name in sdk_fields, (
        f"dlt destination {platform!r} no longer has field {dlt_name!r} "
        f"(was the alias for FLUID's {fluid_name!r}). "
        f"Discovered fields: {sorted(sdk_fields)}. "
        f"Update _FLUID_TO_DLT_FIELD[{platform!r}] in "
        f"fluid_build/build_runners/dlt/destinations.py."
    )


# ── PyAirbyte drift ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "platform,fluid_name,pyairbyte_name",
    list(_flatten(_FLUID_TO_PYAIRBYTE_FIELD)),
)
def test_pyairbyte_alias_target_exists_in_cache_init(
    platform: str, fluid_name: str, pyairbyte_name: str
):
    """Every (platform, fluid → PyAirbyte) alias must point at a real
    ``__init__`` kwarg on the corresponding ``<X>Cache`` class."""
    pytest.importorskip("airbyte")
    params = _pyairbyte_cache_params(platform)
    assert pyairbyte_name in params, (
        f"PyAirbyte {platform.title()}Cache no longer accepts kwarg "
        f"{pyairbyte_name!r} (was the alias for FLUID's {fluid_name!r}). "
        f"Discovered params: {sorted(params)}. "
        f"Update _FLUID_TO_PYAIRBYTE_FIELD[{platform!r}] in "
        f"fluid_build/build_runners/airbyte/destinations.py."
    )


# ── Sanity: registries are non-empty ────────────────────────────────


def test_dlt_alias_table_has_entries():
    """Smoke check: the dlt alias table actually has the platforms we
    expect (snowflake at minimum). Empty table = silent regression."""
    assert "snowflake" in _FLUID_TO_DLT_FIELD


def test_pyairbyte_alias_table_has_entries():
    """Smoke check: the PyAirbyte alias table actually has the platforms
    we expect (snowflake at minimum)."""
    assert "snowflake" in _FLUID_TO_PYAIRBYTE_FIELD
