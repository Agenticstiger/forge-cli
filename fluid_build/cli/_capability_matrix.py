# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Capability matrix: the templates, providers and build engines this install offers.

Command Center imports ``build_capability_matrix`` through
``fluid_build.cli.forge_copilot_runtime``, which re-exports it from here.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Optional

# The built-in FORGE SCAFFOLDING providers (fluid_build/forge/providers/), the
# fallback when the forge registry cannot be read -- not the runtime provider
# registry (there is no databricks scaffold). See COPILOT_WARNING_ONLY_PROVIDERS.
COPILOT_BUILTIN_PROVIDERS = ("local", "gcp", "aws", "snowflake")

KNOWN_BUILD_ENGINES = {
    # Transformation engines (ADP / CDP role).
    "sql",
    "python",
    "dbt",
    "spark",
    "custom",
    # Ingestion engines (SDP role) — must be advertised so the AI
    # path can pick the right engine for source-aligned products.
    # The build_runners directory ships dispatchers for each.
    "duckdb",
    "dlt",
    "airbyte",
    "meltano",
    "kafka-connect",
    "debezium",
}

PROVIDER_ENGINE_COMPATIBILITY = {
    # Local: every engine works (zero-infra ingestion + local dbt).
    "local": {
        "sql",
        "python",
        "dbt",
        "duckdb",
        "dlt",
        "airbyte",
        "meltano",
        "kafka-connect",
        "debezium",
    },
    # Cloud platforms: transformation engines + ingestion engines that
    # have first-class support there.
    "gcp": {"sql", "python", "dbt", "dlt", "airbyte", "meltano", "kafka-connect"},
    "aws": {"sql", "python", "dbt", "dlt", "airbyte", "meltano", "kafka-connect", "debezium"},
    "snowflake": {"sql", "python", "dbt", "dlt", "airbyte", "meltano"},
}

# build_capability_matrix() iterates every provider + template in the registry. The matrix
# only changes when the process reloads the registries, so it is memoised per process.
_CAPABILITY_MATRIX_CACHE: Optional[Dict[str, Any]] = None
_CAPABILITY_MATRIX_LOCK = threading.Lock()


def clear_capability_matrix_cache() -> None:
    """Drop the process-wide capability matrix cache.

    Tests (and any future ``fluid doctor refresh`` command) can call
    this to force the next ``build_capability_matrix()`` to recompute
    from scratch.
    """
    global _CAPABILITY_MATRIX_CACHE
    with _CAPABILITY_MATRIX_LOCK:
        _CAPABILITY_MATRIX_CACHE = None


def build_capability_matrix() -> Dict[str, Any]:
    """Describe the locally available templates, providers, and supported engines.

    Cached per-process (slice UX-G).  The first call pays the full
    registry-scan cost; subsequent calls return a deep copy of the
    cached dict so callers that mutate the result can't poison the
    cache for the next caller.  See ``clear_capability_matrix_cache``
    for how to invalidate.
    """
    global _CAPABILITY_MATRIX_CACHE
    with _CAPABILITY_MATRIX_LOCK:
        if _CAPABILITY_MATRIX_CACHE is not None:
            return copy.deepcopy(_CAPABILITY_MATRIX_CACHE)

    matrix = _build_capability_matrix_uncached()
    with _CAPABILITY_MATRIX_LOCK:
        # Another thread may have populated the cache between our checks;
        # that's fine — last writer wins, the content is identical.
        _CAPABILITY_MATRIX_CACHE = matrix
    return copy.deepcopy(matrix)


def _build_capability_matrix_uncached() -> Dict[str, Any]:
    """Describe the locally available templates, providers, and supported engines."""
    warnings: List[str] = []
    try:
        from fluid_build.forge.core.registry import provider_registry, template_registry
    except Exception as exc:  # pragma: no cover
        warnings.append(
            "Copilot couldn't inspect the local provider registry "
            f"({exc}). Continuing with built-in provider defaults."
        )
        return {
            "providers": list(COPILOT_BUILTIN_PROVIDERS),
            "templates": {},
            "build_engines": sorted(KNOWN_BUILD_ENGINES),
            "provider_engine_compatibility": {
                provider: sorted(engines)
                for provider, engines in PROVIDER_ENGINE_COMPATIBILITY.items()
            },
            "warnings": warnings,
        }

    try:
        discovered_provider_names = list(provider_registry.list_available())
    except Exception as exc:
        discovered_provider_names = []
        warnings.append(
            "Copilot couldn't list local providers "
            f"({exc}). Continuing with built-in provider defaults."
        )

    verified_provider_names: List[str] = []
    for provider_name in discovered_provider_names:
        try:
            provider = provider_registry.get(provider_name)
        except Exception as exc:  # pragma: no cover
            provider = None
            if provider_name in COPILOT_BUILTIN_PROVIDERS:
                warnings.append(
                    f"Copilot couldn't inspect the {provider_name} provider ({exc}). "
                    "Continuing without blocking the run."
                )
        if provider is None:
            if provider_name in COPILOT_BUILTIN_PROVIDERS:
                warnings.append(
                    f"Copilot couldn't inspect the {provider_name} provider. "
                    "Continuing without blocking the run."
                )
            continue
        verified_provider_names.append(provider_name)

    if verified_provider_names:
        provider_names = [p for p in COPILOT_BUILTIN_PROVIDERS if p in verified_provider_names]
        provider_names.extend(sorted(p for p in verified_provider_names if p not in provider_names))
    else:
        provider_names = list(COPILOT_BUILTIN_PROVIDERS)
        warnings.append(
            "Copilot couldn't verify any local providers, so it's using built-in provider defaults "
            "for planning. You can still review or override the provider later."
        )

    try:
        template_names = template_registry.list_available()
    except Exception as exc:
        template_names = []
        warnings.append(
            "Copilot couldn't list local templates "
            f"({exc}). Continuing with built-in defaults where possible."
        )
    templates: Dict[str, Any] = {}

    for template_name in template_names:
        try:
            template = template_registry.get(template_name)
        except Exception as exc:  # pragma: no cover
            warnings.append(
                f"Copilot couldn't inspect template '{template_name}' ({exc}). "
                "Continuing with the remaining templates."
            )
            continue
        if not template:
            warnings.append(
                f"Copilot couldn't inspect template '{template_name}'. "
                "Continuing with the remaining templates."
            )
            continue
        try:
            metadata = template.get_metadata()
        except Exception as exc:
            warnings.append(
                f"Copilot couldn't read metadata for template '{template_name}' ({exc}). "
                "Continuing with the remaining templates."
            )
            continue
        templates[template_name] = {
            "description": metadata.description,
            "provider_support": [p for p in metadata.provider_support if p in provider_names],
            "use_cases": metadata.use_cases,
            "technologies": metadata.technologies,
        }

    return {
        "providers": provider_names,
        "templates": templates,
        "build_engines": sorted(KNOWN_BUILD_ENGINES),
        "provider_engine_compatibility": {
            provider: sorted(engines) for provider, engines in PROVIDER_ENGINE_COMPATIBILITY.items()
        },
        "warnings": warnings,
    }
