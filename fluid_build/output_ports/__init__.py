# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Output-port runtime adapters.

The ``output_ports`` namespace is reserved for runtime adapters that
serve a FLUID data product's ``exposes`` blocks to consumers in
different protocols (MCP, gRPC, REST, GraphQL, …). The first adapter
is :mod:`fluid_build.output_ports.mcp` — a Model Context Protocol
stdio server that lets AI agents (Claude Code, Cursor, internal
copilots) discover and consume one expose at a time.

Distinct from ``fluid_build.providers``: providers BUILD data products
(plan + apply against BigQuery / Snowflake / etc.); output-port
adapters SERVE the already-built artefacts to downstream consumers.
"""
