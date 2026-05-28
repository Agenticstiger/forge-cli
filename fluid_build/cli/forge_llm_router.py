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

"""LiteLLM Router singleton + multi-cloud fallback chain.

Today every LLM call goes straight at ``litellm.completion``. A 5xx
on the primary provider kills the run with no fallback. This module
wraps litellm's already-shipped Router (cooldown + retry + fallback)
so an Anthropic 529 (overloaded) automatically tries Bedrock or
Vertex without operator intervention.

Borrowed wholesale from litellm's documented Router pattern:
    https://docs.litellm.ai/docs/routing
    https://docs.litellm.ai/docs/proxy/reliability

We intentionally don't reimplement the resilience logic — the Router
already handles cooldown_time / num_retries / retry_after, plus the
``fallbacks=[{primary: [fallback1, fallback2]}]`` list-of-dict shape.
This file is the thin "should we route at all + which fallbacks" wrapper.

Off by default for non-Claude models: GPT / Gemini / Groq don't have
documented cross-cloud counterparts on the same prompt so a fallback
would silently change the model. Operators can force routing via
``FLUID_LLM_FALLBACK_CHAIN``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("fluid.cli.forge_llm_router")

# Module-level singleton — lazily constructed on first get_router() call.
# Cleared by tests via _reset_for_testing().
_ROUTER_LOCK = threading.Lock()
_ROUTER: Optional[Any] = None
_ROUTER_PRIMARY_MODEL: str = ""


# ---------------------------------------------------------------------------
# Heuristics — which primary models have a sensible multi-cloud fallback
# ---------------------------------------------------------------------------


def _is_claude_family(model: str) -> bool:
    """True when *model* names a Claude variant (Opus / Sonnet / Haiku).

    Matches the bare model id (``claude-sonnet-4-6``) and the
    ``anthropic/...`` litellm-prefixed form. Bedrock / Vertex prefixes
    (e.g. ``bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0``) are
    deliberately NOT matched here — if the primary is already on
    Bedrock or Vertex, the operator picked that backend and routing
    should be opt-in via env var only.
    """
    if not model:
        return False
    lower = model.lower()
    # Strip ``anthropic/`` prefix if present so the bare-id check works.
    if lower.startswith("anthropic/"):
        lower = lower[len("anthropic/") :]
    return "claude" in lower


def should_use_router(model: str) -> bool:
    """Decide whether ``get_router`` should return a configured Router.

    True when:

    * ``FLUID_LLM_FALLBACK_CHAIN`` is set (operator override — always
      route, regardless of what the primary model is); or
    * the primary model is in the Claude family (Opus / Sonnet /
      Haiku) — these have first-class Bedrock + Vertex counterparts
      with identical prompt semantics so a fallback is safe.

    False otherwise — the legacy direct-call path takes over.
    """
    if os.environ.get("FLUID_LLM_FALLBACK_CHAIN", "").strip():
        return True
    return _is_claude_family(model)


# ---------------------------------------------------------------------------
# Default fallback chain — Claude on Anthropic API → Bedrock → Vertex
# ---------------------------------------------------------------------------


# Per-family Bedrock + Vertex counterparts. Keys are family names; values
# are the litellm-prefixed model ids for each backend. Updated against
# the litellm provider docs (anthropic, bedrock, vertex_ai sections).
# These are stable family ids — when a new minor lands we add a new key
# rather than chase the moving target.
_CLAUDE_FAMILY_MAP: Dict[str, Dict[str, str]] = {
    "claude-haiku-4-5": {
        "anthropic": "anthropic/claude-haiku-4-5",
        "bedrock": "bedrock/anthropic.claude-haiku-4-5-v1:0",
        "vertex_ai": "vertex_ai/claude-haiku-4-5@20251001",
    },
    "claude-sonnet-4-5": {
        "anthropic": "anthropic/claude-sonnet-4-5",
        "bedrock": "bedrock/anthropic.claude-sonnet-4-5-v1:0",
        "vertex_ai": "vertex_ai/claude-sonnet-4-5@20250514",
    },
    "claude-sonnet-4-6": {
        "anthropic": "anthropic/claude-sonnet-4-6",
        "bedrock": "bedrock/anthropic.claude-sonnet-4-6-v1:0",
        "vertex_ai": "vertex_ai/claude-sonnet-4-6@latest",
    },
    "claude-sonnet-4-7": {
        "anthropic": "anthropic/claude-sonnet-4-7",
        "bedrock": "bedrock/anthropic.claude-sonnet-4-7-v1:0",
        "vertex_ai": "vertex_ai/claude-sonnet-4-7@latest",
    },
    "claude-opus-4-7": {
        "anthropic": "anthropic/claude-opus-4-7",
        "bedrock": "bedrock/anthropic.claude-opus-4-7-v1:0",
        "vertex_ai": "vertex_ai/claude-opus-4-7@latest",
    },
    # Earlier-gen fallback for the v0.5-vintage default users.
    "claude-3-5-sonnet": {
        "anthropic": "anthropic/claude-3-5-sonnet-latest",
        "bedrock": "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
        "vertex_ai": "vertex_ai/claude-3-5-sonnet@20240620",
    },
}


def _family_for(model: str) -> Optional[str]:
    """Return the canonical family key for *model* (best-effort match)."""
    if not model:
        return None
    lower = model.lower()
    if lower.startswith("anthropic/"):
        lower = lower[len("anthropic/") :]
    # Direct match first — most operators name their model exactly.
    if lower in _CLAUDE_FAMILY_MAP:
        return lower
    # Prefix match — claude-sonnet-4-6-20250514 → claude-sonnet-4-6.
    for family in _CLAUDE_FAMILY_MAP:
        if lower.startswith(family):
            return family
    # Generic family bucketing (sonnet/opus/haiku) → newest known.
    for family in ("claude-opus-4-7", "claude-sonnet-4-7", "claude-haiku-4-5"):
        bare = family.split("-", 2)[-1]  # opus-4-7 / sonnet-4-7 / haiku-4-5
        if bare.split("-")[0] in lower:
            return family
    return None


def _default_model_list_for(primary_model: str) -> List[Dict[str, Any]]:
    """Build a sensible Router ``model_list`` for the Claude family.

    Returns three ``model_name = primary_model`` entries — one each
    for anthropic / bedrock / vertex_ai — so the Router treats them
    as a single logical group with three deployments. Combined with
    a same-name fallbacks=[{primary: [primary]}] this gives free
    cross-cloud failover with no code changes.

    For non-Claude primaries this returns a single anthropic-prefixed
    entry (Router is essentially a pass-through then). In practice
    ``should_use_router`` will already have short-circuited so this
    path doesn't fire.
    """
    family = _family_for(primary_model)
    if family is None or family not in _CLAUDE_FAMILY_MAP:
        # Best-effort single-deployment list — the Router still gives
        # us cooldown + retry semantics even without cross-cloud peers.
        return [
            {
                "model_name": primary_model,
                "litellm_params": {"model": primary_model},
            }
        ]
    entries: List[Dict[str, Any]] = []
    mapping = _CLAUDE_FAMILY_MAP[family]
    # Order matters: the Router tries entries top-to-bottom for the
    # same model_name group. Anthropic native first (cheapest + lowest
    # latency for most operators), then Bedrock, then Vertex.
    for backend in ("anthropic", "bedrock", "vertex_ai"):
        model_id = mapping.get(backend)
        if not model_id:
            continue
        entries.append(
            {
                "model_name": primary_model,
                "litellm_params": {"model": model_id},
            }
        )
    return entries


def _parse_fallback_chain_env(raw: str) -> List[Dict[str, Any]]:
    """Parse ``FLUID_LLM_FALLBACK_CHAIN`` into a Router model_list.

    Format: comma-separated ``<provider>/<model>`` entries. Whitespace
    around entries is stripped. Empty entries are skipped silently.

    Example: ``anthropic/claude-sonnet-4-6,bedrock/anthropic.claude-3-5-sonnet-v1:0``

    Returns a list of ``{model_name, litellm_params}`` dicts. The
    first entry's full id becomes the ``model_name`` for the group so
    ``router.completion(model=<first>, ...)`` correctly addresses the
    group with the remaining entries as fallbacks.
    """
    entries: List[str] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if piece:
            entries.append(piece)
    if not entries:
        return []
    group_name = entries[0]
    return [
        {
            "model_name": group_name,
            "litellm_params": {"model": model_id},
        }
        for model_id in entries
    ]


# ---------------------------------------------------------------------------
# Router builder + singleton accessor
# ---------------------------------------------------------------------------


def _build_router(model_list: List[Dict[str, Any]]) -> Any:
    """Construct a litellm.Router with operational defaults.

    Constants chosen per the litellm docs' "Reliability" page:

    * ``cooldown_time=60`` — cool a failing deployment for 60s before
      retrying it. Long enough to outlast a transient 5xx, short enough
      to recover within a single forge run.
    * ``num_retries=3`` — retry a single deployment up to 3 times
      before falling over to the next group member.
    * ``retry_after=2`` — wait 2s between in-deployment retries.
    * ``set_verbose=False`` — keep litellm's debug logging off; we own
      observability via the structured-events bus.
    """
    import litellm  # type: ignore[import-untyped]

    # ``fallbacks`` is a list-of-dict-of-list: each dict maps the
    # primary model_name to a list of fallback model_names. With all
    # deployments sharing one ``model_name``, the Router treats them
    # as one group and rotates across them on failure — no explicit
    # fallbacks dict needed for the default chain. We still add an
    # empty list so the Router constructor takes the documented path.
    primary_name = model_list[0]["model_name"] if model_list else ""
    fallbacks: List[Dict[str, List[str]]] = []
    # When env var is set the primary name == the first entry's full id.
    # Same-name routing handles the rotation; nothing more to do.

    return litellm.Router(
        model_list=model_list,
        fallbacks=fallbacks,
        cooldown_time=60,
        num_retries=3,
        retry_after=2,
        set_verbose=False,
    )


def get_router(primary_model: str) -> Optional[Any]:
    """Return the cached Router for *primary_model* or ``None``.

    ``None`` means "no routing applicable — caller should fall back
    to the direct ``litellm.completion`` path". The non-None branch
    yields a process-wide singleton; subsequent calls with the same
    primary model return the same instance. Switching primary model
    rebuilds (a typical run uses one).

    Safe for concurrent first-call access: guarded by a module-level
    lock so two coordinator threads spinning up at once don't build
    two routers.
    """
    if not should_use_router(primary_model):
        return None
    global _ROUTER, _ROUTER_PRIMARY_MODEL
    with _ROUTER_LOCK:
        if _ROUTER is not None and _ROUTER_PRIMARY_MODEL == primary_model:
            return _ROUTER
        env_chain = os.environ.get("FLUID_LLM_FALLBACK_CHAIN", "").strip()
        if env_chain:
            model_list = _parse_fallback_chain_env(env_chain)
            if not model_list:
                LOG.warning(
                    "FLUID_LLM_FALLBACK_CHAIN was set but parsed to an empty list; "
                    "falling back to default chain for %s",
                    primary_model,
                )
                model_list = _default_model_list_for(primary_model)
        else:
            model_list = _default_model_list_for(primary_model)
        try:
            _ROUTER = _build_router(model_list)
            _ROUTER_PRIMARY_MODEL = primary_model
        except Exception as exc:  # noqa: BLE001 — never block the run on routing
            LOG.warning(
                "litellm.Router construction failed (%s); falling back to direct litellm.completion",
                exc,
            )
            _ROUTER = None
            _ROUTER_PRIMARY_MODEL = ""
            return None
        return _ROUTER


def _reset_for_testing() -> None:
    """Drop the cached singleton — exposed for tests only.

    Production callers should NOT use this; the singleton is process-wide
    by design.
    """
    global _ROUTER, _ROUTER_PRIMARY_MODEL
    with _ROUTER_LOCK:
        _ROUTER = None
        _ROUTER_PRIMARY_MODEL = ""


__all__ = [
    "_default_model_list_for",
    "_reset_for_testing",
    "get_router",
    "should_use_router",
]
