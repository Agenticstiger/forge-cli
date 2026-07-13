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

"""Vector / embeddings output port — a RAG-consumption peer of the MCP port.

Where :mod:`fluid_build.output_ports.mcp` *serves* an already-built expose to
an agent at runtime, the vector output port *emits the target* a
retrieval-augmented-generation pipeline writes into: a pgvector embeddings
table + ANN index for a data product's ``ai-embeddable`` columns, plus the
provenance manifest a retriever needs.

It consumes the ``ai-embeddable`` column label the built-in ai_ready agent
(:mod:`fluid_build.copilot.agents.ai_ready_agent`, PR #405) stamps on
embedding-friendly text columns — so the two features compose: ai_ready
decides *what* is safe to embed, this port decides *where* the embeddings
land and *how* they are indexed.

The public surface is a pure, credential-free emitter (mirrors the confluent
IaC plugin's contract-in / artefact-out shape). The heavier follow-ups —
actual embedding generation, LanceDB / Qdrant targets, a live round-trip —
are layered on top of this seam, not baked into it.
"""

from __future__ import annotations

from .emitter import (
    AI_EMBEDDABLE_LABEL,
    VECTOR_PLATFORM,
    VectorPortArtifacts,
    VectorTarget,
    build_rag_manifest,
    compile_pgvector_ddl,
    compile_vector_port,
    embeddable_columns,
    iter_vector_exposes,
    resolve_table_name,
    validate_vector_binding,
)

__all__ = [
    "AI_EMBEDDABLE_LABEL",
    "VECTOR_PLATFORM",
    "VectorPortArtifacts",
    "VectorTarget",
    "build_rag_manifest",
    "compile_pgvector_ddl",
    "compile_vector_port",
    "embeddable_columns",
    "iter_vector_exposes",
    "resolve_table_name",
    "validate_vector_binding",
]
