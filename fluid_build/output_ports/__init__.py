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
