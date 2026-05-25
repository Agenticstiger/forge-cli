# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Ordered mapper pipeline.

Order matters:
- ``metadata`` first — establishes the FLUID skeleton (exposes/expects lists)
  and seeds the metadata block.
- ``team`` — populates ``owner``.
- ``schema`` — fills ``exposes[]`` from ODCS SchemaObjects (needed before sla).
- ``servers`` — fills ``expects[]`` from ODCS servers.
- ``sla`` — attaches qos to ``exposes[0]`` (depends on schema).
- ``quality`` — contract-level only (property/object level handled in schema).

On export the same order is fine: each mapper writes its own slice of the
ODCS dict; no inter-mapper dependencies.
"""

from . import metadata, quality, schema, servers, sla, team  # noqa: F401

IMPORT_PIPELINE = [metadata, team, schema, servers, sla, quality]
EXPORT_PIPELINE = [metadata, team, schema, servers, sla, quality]

__all__ = ["IMPORT_PIPELINE", "EXPORT_PIPELINE"]
