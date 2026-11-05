# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Stable cache and namespace key derivation helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonicalize_params(params: Optional[Mapping[str, Any]]) -> str:
    """Serialize *params* deterministically for hashing."""
    if not params:
        return "{}"
    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)


def generate_cache_key(
    model: str,
    prompt: str,
    params: Optional[Mapping[str, Any]] = None,
    capability_matrix: Optional[Mapping[str, Any]] = None,
) -> str:
    """Stable cache key extended with provider capability metadata.

    Hash shape::

        sha256( model
              ‖ sha256(prompt)
              ‖ sha256(canonical(params))
              ‖ sha256(canonical(capability_matrix)) )

    The fourth segment is what makes flipping a capability flag
    (extended-thinking budget, structured-output mode, prompt-cache
    placement) invalidate the cache cleanly. Two runs with identical
    model + prompt + params but different capability matrices will
    NOT collide on the same cached response — which is the property
    we need so a cached "no-thinking" answer doesn't leak into a
    later "with-thinking" run, and vice versa.

    Both new arguments are optional. ``params=None`` and
    ``capability_matrix=None`` hash to the same canonical-empty
    sentinel — i.e. callers that don't care about either get a
    deterministic key that only varies on model + prompt.
    """
    prompt_hash = _sha256_text(prompt or "")
    params_hash = _sha256_text(canonicalize_params(params))
    cap_hash = _sha256_text(canonicalize_params(capability_matrix))
    return _sha256_text(f"{model or ''}{prompt_hash}{params_hash}{cap_hash}")


def workspace_fingerprint(path: Path) -> str:
    """Return a stable workspace fingerprint for project-scoped namespaces."""
    return _sha256_text(str(path.resolve()))


def discovery_key(path: Path, mtime_ns: int, size: int) -> str:
    """Stable key for discovery artifacts keyed by path and file identity."""
    return _sha256_text(f"{path.resolve()}:{mtime_ns}:{size}")
