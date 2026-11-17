# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The AWS Glue Schema Registry as a source of stream schemas.

The sibling of :class:`~.confluent_schema_reg.ConfluentSchemaRegistryConnector`'s
``stream_block``: seed a build's ``properties.stream`` from a Glue schema, read with
``glue:GetSchemaVersion`` as the caller's IAM identity (no secret). boto3 comes from
the ``[aws]`` extra and is imported on first use (``schema_registry/clients.py``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class GlueSchemaRegistryImporter:
    """Read schemas from one Glue registry (``default-registry`` when none is named)."""

    def __init__(
        self,
        registry_name: Optional[str] = None,
        *,
        region: Optional[str] = None,
        client: Any = None,
    ) -> None:
        from fluid_build.schema_registry.clients import GlueRegistry

        self.registry_name = registry_name
        self._registry = GlueRegistry(region=region, client=client)

    def schemas(self) -> List[str]:
        """The schema names in the registry."""
        from fluid_build.schema_registry.reference import GLUE_DEFAULT_REGISTRY

        return self._registry.schemas(self.registry_name or GLUE_DEFAULT_REGISTRY)

    def stream_block(self, schema_name: str, version: Any = "latest") -> Dict[str, Any]:
        """A ``properties.stream`` block seeded from ``schema_name``."""
        from fluid_build.schema_registry.importer import ref_for, stream_from_subject

        ref = ref_for("glue", schema_name, registry_name=self.registry_name, version=version)
        return stream_from_subject(ref, client=self._registry)
