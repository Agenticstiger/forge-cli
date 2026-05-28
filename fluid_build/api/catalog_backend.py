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

"""Catalog backend plugin registry.

A *catalog backend* is a metadata catalog the CLI can push FLUID
contracts into (DataHub, OpenMetadata, AWS Glue,
Snowflake Horizon, …). The registry here is the single declaration
point for a backend — once a registrar module under
``fluid_build/build_runners/catalog_registrars/<name>.py`` calls
:func:`register_catalog_backend` with a :class:`CatalogBackendSpec`,
the backend is reachable from:

* the contract-driven acquisition path (``properties.catalog.register:
  [<name>]``) via ``build_runners/_catalog.py``
* the CLI surface (``fluid publish --target <name>``) via
  ``providers/catalogs/__init__.py``

with no edits to either site. Adding a new backend is a single-file
change in the registrar package.

The pattern is import-time self-registration (Django models, Click
commands, pytest plugins use the same shape) rather than entry-point
discovery so the module graph stays static and analysers / type
checkers can see every backend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

if TYPE_CHECKING:
    from .catalog import CatalogRegistrar


class CatalogCapability(str, Enum):
    """What a catalog backend can persist about a data product.

    Used as a declarative contract on :class:`CatalogBackendSpec` so:

    * docs / ``fluid publish --list-catalogs`` can answer "which
      backends surface my domain?" without spelunking each registrar;
    * tests can assert "every backend declaring DATA_PRODUCT actually
      writes a first-class data-product entity";
    * the canonical publish layer can short-circuit work no backend
      will use (e.g. don't render ODCS per asset if no live backend
      has ``PER_ASSET_CONTRACT``).

    Values are the string the user writes in declarations / docs.
    """

    DATA_PRODUCT = (
        "data_product"  # First-class product entity (DataHub DataProduct, DMM /api/dataproducts)
    )
    DOMAIN = "domain"  # Domain entity / tagging (DataHub Domain, OpenMetadata Domain)
    LINEAGE = "lineage"  # Reads contract.consumes[] → emits lineage edges
    PER_ASSET_CONTRACT = "per_asset_contract"  # Attaches ODCS per asset (DMM /api/datacontracts, DataHub custom prop)
    PRODUCT_SPECS = "product_specs"  # Attaches fluid + ODPS at the product level
    CUSTOM_PROPERTIES = "custom_properties"  # Free-form key-value map on entities
    GLOSSARY_TERMS = "glossary_terms"  # Maps classifications → glossary tags
    OWNERSHIP = "ownership"  # First-class ownership aspect


@dataclass(frozen=True)
class CatalogBackendSpec:
    """Declarative description of a single catalog backend.

    Attributes:
        name: Canonical id used by ``--target <name>`` and
            ``properties.catalog.register: [<name>]``. Lowercase
            kebab-case is the convention (``datahub``, ``glue``,
            ``snowflake_horizon``). Aliases come from ``aliases``.
        registrar_factory: Called once per publish with the resolved
            catalog config dict; must return an instance satisfying
            :class:`~fluid_build.api.catalog.CatalogRegistrar`. The
            factory owns all per-target config interpretation — the
            framework just hands it the resolved dict.
        aliases: Additional names that map to this backend. Allows
            ``--target aws-glue`` and ``--target glue`` without
            redeclaration.
        env_vars: Mapping of config-dict keys → ordered list of
            environment variables the framework consults when filling
            that key. First non-empty env var wins; an unset key
            leaves the config dict untouched (preserving any YAML
            value or the registrar's own defaults). Flat keys only —
            nested catalog configs are reserved for the legacy native
            async providers (fluid-command-center, datamesh-manager).
        capabilities: Frozen set of :class:`CatalogCapability` strings
            describing what this backend persists. Empty set means
            "just a dataset emitter, no product / lineage / specs"
            (matches the historical behaviour of Glue / Snowflake
            Horizon before the canonical refactor).
        description: One-line human description shown by
            ``fluid publish --list-catalogs`` and friends.
    """

    name: str
    registrar_factory: Callable[[Dict[str, Any]], "CatalogRegistrar"]
    aliases: Tuple[str, ...] = ()
    env_vars: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    capabilities: FrozenSet[str] = frozenset()
    description: str = ""

    @property
    def all_names(self) -> Tuple[str, ...]:
        return (self.name, *self.aliases)

    def supports(self, capability: str) -> bool:
        """Return ``True`` iff *capability* is declared on this backend.
        Accepts either the bare string (``"lineage"``) or the enum
        value (``CatalogCapability.LINEAGE``); both compare equal
        because :class:`CatalogCapability` is a ``str`` subclass."""
        return capability in self.capabilities


_BACKENDS: Dict[str, CatalogBackendSpec] = {}


def register_catalog_backend(spec: CatalogBackendSpec) -> None:
    """Register *spec* under its canonical name and every alias.

    Idempotent — re-registering the same name overwrites the previous
    spec. This matches how test fixtures occasionally rebind a
    backend; intentional override is preserved.
    """
    for name in spec.all_names:
        _BACKENDS[name] = spec


def get_catalog_backend(name: str) -> Optional[CatalogBackendSpec]:
    """Return the spec registered for *name*, or ``None`` if unknown."""
    return _BACKENDS.get(name)


def all_catalog_backend_names() -> List[str]:
    """Return every registered name (canonical + aliases) for
    enumeration. Order is registration order via ``dict`` insertion
    semantics."""
    return list(_BACKENDS.keys())


def all_catalog_backend_specs() -> List[CatalogBackendSpec]:
    """Return the unique :class:`CatalogBackendSpec` instances in
    registration order, de-duplicated when aliases point at the same
    spec object."""
    seen: List[int] = []
    out: List[CatalogBackendSpec] = []
    for spec in _BACKENDS.values():
        if id(spec) in seen:
            continue
        seen.append(id(spec))
        out.append(spec)
    return out


def apply_env_overrides(name: str, catalog_config: Dict[str, Any]) -> None:
    """Apply the env-var → config-key mapping from the spec in place.

    No-op when *name* isn't registered (preserves the surrounding
    legacy native-provider env-var blocks in ``config_manager`` that
    own their own resolution logic).
    """
    spec = _BACKENDS.get(name)
    if spec is None:
        return
    for config_key, env_names in spec.env_vars.items():
        for env in env_names:
            value = os.environ.get(env)
            if value:
                catalog_config[config_key] = value
                break


__all__ = [
    "CatalogBackendSpec",
    "CatalogCapability",
    "register_catalog_backend",
    "get_catalog_backend",
    "all_catalog_backend_names",
    "all_catalog_backend_specs",
    "apply_env_overrides",
]
