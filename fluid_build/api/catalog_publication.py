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

"""Canonical publication payload.

Every catalog backend (DataHub / OpenMetadata / Unity / Glue /
Snowflake Horizon / future) consumes the same
:class:`CatalogPublicationPayload`. The payload is built **once per
publish** from the source FLUID contract and threaded to every backend
in the active plan; each backend's translator then projects whatever
slice of the canonical model its target catalog actually persists.

Why this layer exists:

* Before, each registrar re-parsed the raw FLUID dict. Adding a new
  field to the contract required touching every backend.
* Spec renderings (fluid YAML, ODPS, per-asset ODCS) were
  DataHub-only — even though Glue's ``Parameters`` map, Unity's
  ``properties`` map, OpenMetadata's ``extension`` field, and
  Snowflake Horizon's ``comment`` can all carry them. Centralising
  the renderers means *every* backend gets the same provenance trail
  for free.
* Lineage (``contract.consumes[]``) was DataHub-only too. With a
  canonical :class:`LineageEdge` list, any backend that knows how to
  express "this asset reads from upstream X" can wire it.
* Capabilities (declared on :class:`CatalogBackendSpec`) explicitly
  describe what each backend supports — DATA_PRODUCT, DOMAIN,
  LINEAGE, etc. — so the framework / docs can answer "which backends
  surface my domain?" without spelunking through each
  ``register_payload`` implementation.

The payload is **immutable** (``frozen=True`` dataclasses) so backends
running in parallel can share an instance without defensive copies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Leaf payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerPayload:
    """Canonical product / asset ownership."""

    team: str
    email: str = ""


@dataclass(frozen=True)
class ColumnPayload:
    """One column of an asset's schema. ``native_type`` preserves the
    SQL / Avro / Parquet type literally as the contract author wrote
    it (for display + round-trip); semantic typing is the backend
    translator's job — it maps ``native_type`` to e.g. DataHub's
    ``SchemaFieldDataType`` union or Glue's column type."""

    name: str
    native_type: str
    description: str = ""
    required: bool = False
    classifications: Tuple[str, ...] = ()  # e.g. ("pii", "email")


@dataclass(frozen=True)
class LineageEdge:
    """One upstream edge: ``this_asset`` is built from
    ``upstream_product_id.upstream_expose_id``. Each backend translates
    this into its native lineage shape (DataHub UpstreamLineage, Unity
    table lineage, OpenLineage events, …).

    ``upstream_platform`` is optional because cross-platform consumes
    are rare; when absent, the translator typically defaults to the
    consuming asset's own platform (matches the DataHub registrar's
    historical behaviour).
    """

    upstream_product_id: str
    upstream_expose_id: str
    upstream_platform: Optional[str] = None
    transformation_type: str = "TRANSFORMED"


@dataclass(frozen=True)
class AssetPayload:
    """One expose of a data product — the physical asset that backs it.

    ``odcs_yaml`` is the pre-rendered Open Data Contract Standard v3.1
    document scoped to *this* asset (i.e. what
    ``DataMeshManagerProvider`` would PUT to
    ``/api/datacontracts/{product_id}.{expose_id}``). Done once at
    payload build time so backends don't each re-import the heavy
    ``OdcsProvider``.
    """

    asset_id: str
    platform: str
    location: Mapping[str, Any] = field(default_factory=dict)
    schema: Tuple[ColumnPayload, ...] = ()
    odcs_yaml: Optional[str] = None
    upstreams: Tuple[LineageEdge, ...] = ()


@dataclass(frozen=True)
class SpecBundle:
    """Pre-rendered standards-bearing specs that travel with the
    product. Both fields are optional — rendering failures degrade
    to ``None`` rather than aborting publish, and absence is honest
    about "this backend has no spec to attach"."""

    fluid_yaml: Optional[str] = None
    odps_yaml: Optional[str] = None


@dataclass(frozen=True)
class ProductPayload:
    """Canonical data product metadata. Backends that support a
    first-class "data product" entity (DataHub DataProduct, DMM
    /api/dataproducts) hydrate from these fields directly; backends
    that don't (Glue, Unity) still get the metadata as
    customProperties / comments / etc."""

    product_id: str
    name: str
    description: str = ""
    domain: str = ""
    layer: str = ""  # Bronze/Silver/Gold/Platinum
    product_type: str = ""  # SDP/ADP/CDP
    version: str = ""
    owner: Optional[OwnerPayload] = None
    tags: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Top-level payload + builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogPublicationPayload:
    """The canonical "publish this data product" shape every backend
    consumes via :meth:`CatalogRegistrar.register_payload`.

    ``contract`` is the source FLUID dict, kept around as an escape
    hatch for backends that need fields we haven't promoted to the
    canonical model yet. Prefer the typed fields — anything frequently
    consumed should graduate out of ``contract``.
    """

    product: ProductPayload
    assets: Tuple[AssetPayload, ...]
    specs: SpecBundle = field(default_factory=SpecBundle)
    classifications: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    contract: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_contract(
        cls,
        contract: Mapping[str, Any],
        classifications: Optional[Mapping[str, List[str]]] = None,
    ) -> "CatalogPublicationPayload":
        """Build the canonical payload from a raw FLUID contract dict.

        Renders the FLUID YAML, the ODPS v1.0.0 spec, and one ODCS
        v3.1 contract per expose — all best-effort: if a renderer
        explodes (typically because the contract is malformed enough
        that conversion fails), we log a warning and leave that spec
        as ``None`` rather than aborting.
        """
        classifications = classifications or {}
        product = _build_product(contract)
        assets = tuple(_build_asset(contract, expose) for expose in contract.get("exposes") or [])
        specs = SpecBundle(
            fluid_yaml=_render_fluid_yaml(contract),
            odps_yaml=_render_odps_yaml(contract),
        )
        # Normalise classifications to immutable tuples so the payload
        # stays hashable / shareable across threads.
        norm_classifications = {str(col): tuple(labels) for col, labels in classifications.items()}
        return cls(
            product=product,
            assets=assets,
            specs=specs,
            classifications=norm_classifications,
            contract=dict(contract),
        )


# ---------------------------------------------------------------------------
# Builders + renderers (module-private)
# ---------------------------------------------------------------------------


def _build_product(contract: Mapping[str, Any]) -> ProductPayload:
    metadata = contract.get("metadata") or {}
    owner_raw = metadata.get("owner") or {}
    owner = None
    if isinstance(owner_raw, Mapping):
        team = str(owner_raw.get("team") or owner_raw.get("name") or "")
        email = str(owner_raw.get("email") or "")
        if team or email:
            owner = OwnerPayload(team=team or "unknown", email=email)
    return ProductPayload(
        product_id=str(contract.get("id") or contract.get("name") or "unknown"),
        name=str(contract.get("name") or contract.get("id") or ""),
        description=str(contract.get("description") or ""),
        domain=str(contract.get("domain") or "").strip(),
        layer=str(metadata.get("layer") or ""),
        product_type=str(metadata.get("productType") or ""),
        version=str(contract.get("version") or ""),
        owner=owner,
        tags=tuple(str(t) for t in contract.get("tags") or []),
    )


def _build_asset(contract: Mapping[str, Any], expose: Mapping[str, Any]) -> AssetPayload:
    if not isinstance(expose, Mapping):
        # Defensive — schema validation should catch this earlier, but
        # the canonical layer must never raise on bad input. Return a
        # placeholder so the rest of the payload still builds.
        return AssetPayload(asset_id="<malformed>", platform="forge")
    expose_id = str(expose.get("exposeId") or expose.get("name") or expose.get("id") or "")
    binding = expose.get("binding") or {}
    platform = str((binding.get("platform") if isinstance(binding, Mapping) else None) or "forge")
    location = dict((binding.get("location") or {})) if isinstance(binding, Mapping) else {}
    contract_spec = expose.get("contract") or {}
    schema_cols = (
        contract_spec.get("schema") if isinstance(contract_spec, Mapping) else None
    ) or []
    schema = tuple(_build_column(col) for col in schema_cols if isinstance(col, Mapping))
    upstreams = tuple(
        LineageEdge(
            upstream_product_id=str(ref.get("productId") or ""),
            upstream_expose_id=str(ref.get("exposeId") or ""),
            upstream_platform=ref.get("platform"),
        )
        for ref in (contract.get("consumes") or [])
        if isinstance(ref, Mapping) and ref.get("productId") and ref.get("exposeId")
    )
    return AssetPayload(
        asset_id=expose_id,
        platform=platform,
        location=location,
        schema=schema,
        odcs_yaml=_render_odcs_yaml(contract, expose_id),
        upstreams=upstreams,
    )


def _build_column(col: Mapping[str, Any]) -> ColumnPayload:
    return ColumnPayload(
        name=str(col.get("name") or ""),
        native_type=str(col.get("type") or "string"),
        description=str(col.get("description") or ""),
        required=bool(col.get("required") or False),
    )


def _render_fluid_yaml(contract: Mapping[str, Any]) -> Optional[str]:
    try:
        import yaml as _yaml

        # ``dict(contract)`` defends against ``Mapping`` subclasses that
        # PyYAML's default representer doesn't know about.
        return _yaml.safe_dump(dict(contract), sort_keys=False)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Failed to render FLUID YAML for canonical payload: %s", exc)
        return None


def _render_odps_yaml(contract: Mapping[str, Any]) -> Optional[str]:
    try:
        import yaml as _yaml

        from fluid_build.providers.odps_standard import OdpsStandardProvider

        odps_dict = OdpsStandardProvider().render(dict(contract))
        return _yaml.safe_dump(odps_dict, sort_keys=False)
    except Exception as exc:  # noqa: BLE001
        LOG.warning(
            "Failed to render ODPS spec for canonical payload (continuing): %s",
            exc,
        )
        return None


def _render_odcs_yaml(contract: Mapping[str, Any], expose_id: str) -> Optional[str]:
    if not expose_id:
        return None
    try:
        import yaml as _yaml

        from fluid_build.providers.odcs import OdcsProvider

        odcs_dict = OdcsProvider().render(dict(contract), expose_id=expose_id)
        return _yaml.safe_dump(odcs_dict, sort_keys=False)
    except Exception as exc:  # noqa: BLE001
        LOG.warning(
            "Failed to render ODCS for expose %s (continuing without it): %s",
            expose_id,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Convenience constructors for callers that only have a CatalogAsset
# ---------------------------------------------------------------------------


def payload_from_single_asset(
    contract: Mapping[str, Any],
    classifications: Optional[Mapping[str, List[str]]] = None,
) -> CatalogPublicationPayload:
    """Alias of :meth:`CatalogPublicationPayload.from_contract` for
    clarity at call sites that wrap a single
    :class:`~fluid_build.providers.catalogs.base.CatalogAsset` (the
    legacy publish.py path)."""
    return CatalogPublicationPayload.from_contract(contract, classifications)


__all__ = [
    "OwnerPayload",
    "ColumnPayload",
    "LineageEdge",
    "AssetPayload",
    "SpecBundle",
    "ProductPayload",
    "CatalogPublicationPayload",
    "payload_from_single_asset",
]
