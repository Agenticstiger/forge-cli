# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Ordered mapper pipeline.

Order matters:
- ``metadata`` first — establishes the FLUID skeleton (exposes/expects lists)
  and seeds the metadata block.
- ``team`` — populates ``owner``.
- ``schema`` — fills ``exposes[]`` from ODCS SchemaObjects (needed before sla).
- ``servers`` — fills ``expects[]`` from ODCS servers.
- ``sla`` — attaches qos to ``exposes[0]`` (depends on schema).
- ``quality`` — contract-level, plus the object/property rules derived from
  ``exposes[].contract.dq.rules`` and ``exposes[].contract.quality``. MUST stay
  after ``schema`` on export: it attaches each rule to the SchemaObject and
  property the schema mapper has already built.
- ``extras`` — **last in both directions**. On export it snapshots every FLUID
  block the mappers above did not consume into ``customProperties``; on import
  it splices that snapshot back over their output. Running it last is what
  makes it a deny-list: it sees the finished picture.

On export each mapper writes its own slice of the ODCS dict, with two
dependencies: ``quality`` needs ``schema`` ahead of it, and ``extras`` goes
last. The ``quality`` one is easy to lose because it fails *silently* — move
that mapper up and ``_declared_quality_to_odcs`` finds no ``odcs["schema"]``
to walk, so the author's own dq rules and quality expressions vanish from the
export with no error. A docstring is not a guard, so
``tests/providers/test_odcs_quality_switch_and_order.py`` pins ``quality``
after ``schema`` on export and ``extras`` last on both lists.
"""

from . import extras, metadata, quality, schema, servers, sla, team  # noqa: F401

IMPORT_PIPELINE = [metadata, team, schema, servers, sla, quality, extras]
EXPORT_PIPELINE = [metadata, team, schema, servers, sla, quality, extras]

__all__ = ["IMPORT_PIPELINE", "EXPORT_PIPELINE"]
