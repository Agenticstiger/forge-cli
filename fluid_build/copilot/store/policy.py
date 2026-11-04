# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Store policy helpers."""

from __future__ import annotations

from typing import Optional

DEFAULT_LLM_TTL_SECONDS = 60 * 60 * 24 * 7


def default_ttl_for_namespace(namespace: str) -> Optional[int]:
    """Return the default TTL for a namespace."""
    if namespace.startswith("llm/"):
        return DEFAULT_LLM_TTL_SECONDS
    return None
