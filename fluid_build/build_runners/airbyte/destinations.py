# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Destination introspection for the airbyte runner (embedded mode).

Replaces ~100 lines of hardcoded per-Cache factories with one ~40-line
introspector that walks PyAirbyte's ``<X>Cache.__init__`` signature to
discover what kwargs the cache class expects — then constructs the
instance from FLUID's resolved credentials.

Adding support for a new PyAirbyte cache = ZERO code here. As soon as
PyAirbyte ships ``MotherDuckCache`` or ``ClickhouseCache``, the
introspector picks it up via ``getattr(ab.caches, f"{platform.title()}Cache")``.

Per /borrow-before-build receipts:
- PyAirbyte's connector + cache classes own their auth-flow logic (each
  has its own JSON-schema / Pydantic config). We don't replicate.
  Docs: https://docs.airbyte.com/using-airbyte/pyairbyte/getting-started
- Pydantic-settings (in fluid_build.build_runners._credentials) handles
  the operator-side layered config so we don't write our own env-var loader.
  Docs: https://docs.pydantic.dev/latest/concepts/pydantic_settings/

Forge-cli-owned surface: the per-platform field-name aliases below for
the (rare) cases where FLUID and PyAirbyte name the same concept differently.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, Optional

from .._credentials import register_engine_introspector

LOG = logging.getLogger("fluid.acquire.airbyte.destinations")


# FLUID-canonical → PyAirbyte cache __init__ arg aliases. Only listed when
# names differ; most are 1:1.
_FLUID_TO_PYAIRBYTE_FIELD: Dict[str, Dict[str, str]] = {
    "snowflake": {
        # PyAirbyte SnowflakeCache uses 'username', 'schema_name'; FLUID's
        # canonical is 'user' (and the binding uses 'schema').
        "user": "username",
        "schema": "schema_name",
    },
    "bigquery": {
        # PyAirbyte BigQueryCache: 'project_name', 'dataset_name'.
        "project_id": "project_name",
        "schema": "dataset_name",
    },
    "postgres": {
        "user": "username",
        "schema": "schema_name",
    },
}


def _kwargs_for_cache_param(
    platform: str,
    param_name: str,
    credentials: Dict[str, Any],
    binding: Dict[str, Any],
) -> Optional[Any]:
    """Resolve the FLUID-side value to plug into a PyAirbyte cache __init__ kwarg.

    Search order:
    1. ``binding.location.<param_name>`` (contract-author override)
    2. FLUID-resolved credentials dict (env / .env / secrets)
    3. Reverse alias lookup (dlt-side name → FLUID name)

    Returns ``None`` when nothing resolves; the cache constructor either
    has its own default OR will raise a clear missing-arg error.
    """
    binding_loc = binding.get("location") or {}
    if param_name in binding_loc and binding_loc[param_name] is not None:
        return binding_loc[param_name]
    if param_name in credentials:
        return credentials[param_name]
    # Reverse alias: which FLUID field maps to this cache param?
    for fluid_name, sdk_name in _FLUID_TO_PYAIRBYTE_FIELD.get(platform.lower(), {}).items():
        if sdk_name == param_name and fluid_name in credentials:
            return credentials[fluid_name]
        # Also check binding for the FLUID name under the SDK name's slot
        if sdk_name == param_name and fluid_name in binding_loc:
            return binding_loc[fluid_name]
    return None


@register_engine_introspector("airbyte")
def _airbyte_introspect(
    *,
    platform: str,
    credentials: Dict[str, Any],
    binding: Dict[str, Any],
    contract: Dict[str, Any],
    product_id: str,
) -> Any:
    """Construct the PyAirbyte cache instance for ``platform`` via signature
    introspection of ``ab.caches.<X>Cache``.

    Returns the constructed cache, or ``None`` when:
    - PyAirbyte isn't installed
    - No ``<X>Cache`` class exists (operator typo or unsupported destination)
    - The cache's ``__init__`` raised (bad / missing args)

    Returning ``None`` lets the runner fall back to a local DuckDB cache
    with a clear warning rather than crashing.
    """
    try:
        import airbyte as ab  # type: ignore[import-untyped]
    except ImportError:
        LOG.warning("PyAirbyte not installed; cannot construct destination cache")
        return None

    cache_cls_name = f"{platform.title()}Cache"
    cache_cls = getattr(ab.caches, cache_cls_name, None)
    if cache_cls is None:
        LOG.warning(
            "PyAirbyte has no cache class %r; falling back to local DuckDB cache",
            cache_cls_name,
        )
        return None

    # Discover constructor kwargs. Pydantic v2 BaseModels (which all current
    # PyAirbyte caches are) have a generic ``__init__(self, **data)``, so
    # ``inspect.signature`` reveals no named params; ``model_fields`` is the
    # canonical source for Pydantic models. Try in order: Pydantic v2 →
    # Pydantic v1 → dataclass → signature fallback for non-Pydantic caches.
    sdk_fields: list[str] = []
    if hasattr(cache_cls, "model_fields"):
        sdk_fields = [f for f in cache_cls.model_fields if not f.startswith("_")]
    elif hasattr(cache_cls, "__fields__"):
        sdk_fields = [f for f in cache_cls.__fields__ if not f.startswith("_")]
    elif hasattr(cache_cls, "__dataclass_fields__"):
        sdk_fields = [f for f in cache_cls.__dataclass_fields__ if not f.startswith("_")]
    else:
        try:
            sig = inspect.signature(cache_cls.__init__)
            sdk_fields = [
                name
                for name, param in sig.parameters.items()
                if name not in ("self", "args", "kwargs")
                and param.kind
                not in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                )
            ]
        except (TypeError, ValueError) as exc:
            LOG.debug("Could not introspect %s: %s", cache_cls_name, exc)
            return None

    kwargs: Dict[str, Any] = {}
    for field_name in sdk_fields:
        value = _kwargs_for_cache_param(platform, field_name, credentials, binding)
        if value is not None and value != "":
            kwargs[field_name] = value

    try:
        return cache_cls(**kwargs)
    except TypeError as exc:
        LOG.error(
            "PyAirbyte %s construction failed (likely missing required field): %s. "
            "Resolved kwargs were: %s. Check FLUID env vars or "
            "binding.location overrides.",
            cache_cls_name,
            exc,
            {
                k: ("<<set>>" if "key" in k or "secret" in k or "password" in k else v)
                for k, v in kwargs.items()
            },
        )
        return None
