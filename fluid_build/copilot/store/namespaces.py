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

"""Namespace helpers for the staged copilot store."""

from __future__ import annotations

from typing import Iterable, List

# LLM cache namespaces match the ``stage`` attribute of each
# concrete :class:`BaseStageAgent` subclass (see
# ``BaseStageAgent.cache_namespace``).  Only ``BaseStageAgent``
# subclasses (logical / builder / readme / transformation /
# validator) write to ``llm/<stage>``; ``ContractForgeAgent`` is
# a deterministic helper with its own ``stage`` field but no
# LLM cache namespace, so it is intentionally not listed here.
# ``llm/scaffold``, ``llm/conceptual`` and ``llm/modeler`` were
# retired in Sprint A (no production callers).
LLM_NAMESPACES = (
    "llm/logical",
    "llm/builder",
    "llm/transformation",
    "llm/readme",
    "llm/validator",
)
MEMORY_NAMESPACES = (
    "memory/project",
    "memory/team",
    "memory/personal",
    "memory/episodic",
    "memory/semantic",
)
DISCOVERY_NAMESPACES = ("discovery",)
SKILL_NAMESPACES = ("skills",)
HISTORY_NAMESPACE = "history"
AUDIT_NAMESPACE = "audit"


def all_namespaces() -> List[str]:
    return list(
        LLM_NAMESPACES
        + MEMORY_NAMESPACES
        + DISCOVERY_NAMESPACES
        + SKILL_NAMESPACES
        + (HISTORY_NAMESPACE, AUDIT_NAMESPACE)
    )


def normalize_namespace(value: str) -> str:
    return "/".join(segment for segment in str(value or "").strip().split("/") if segment)


def namespace_root(value: str) -> str:
    normalized = normalize_namespace(value)
    return normalized.split("/", 1)[0] if normalized else ""


def matches_namespace(candidate: str, requested: Iterable[str]) -> bool:
    normalized = normalize_namespace(candidate)
    requested_roots = {normalize_namespace(item) for item in requested}
    return normalized in requested_roots or namespace_root(normalized) in requested_roots
