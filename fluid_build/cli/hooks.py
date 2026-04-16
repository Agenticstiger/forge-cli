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

# fluid_build/cli/hooks.py
"""Lifecycle hook integration for FLUID CLI plan/apply commands.

Provides thin wrappers that call provider lifecycle hooks at the
correct points.  Hook failures are logged but never break the
core plan/apply flow.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

# Import hook helpers — SDK preferred, fallback via providers.base
try:
    from fluid_provider_sdk import (  # type: ignore[import-untyped]
        CostEstimate,
        has_hook,
        invoke_hook,
    )
except ImportError:
    from fluid_build.providers.base import (  # type: ignore[attr-defined]
        CostEstimate,
        has_hook,
        invoke_hook,
    )


def _log(logger: logging.Logger, tag: str, **kv: Any) -> None:
    import json

    try:
        logger.info(json.dumps({"event": tag, **kv}))
    except Exception:
        logger.info("%s: %s", tag, kv)


# ---------------------------------------------------------------------------
# Plan hooks
# ---------------------------------------------------------------------------


def run_pre_plan(provider: Any, contract: Dict[str, Any], logger: logging.Logger) -> Dict[str, Any]:
    """Invoke ``pre_plan`` hook if the provider implements it."""
    if not has_hook(provider, "pre_plan"):
        return contract
    _log(logger, "hook_pre_plan", provider=getattr(provider, "name", "?"))
    result = invoke_hook(provider, "pre_plan", contract)
    return result if isinstance(result, dict) else contract


def run_post_plan(
    provider: Any, actions: List[Dict[str, Any]], logger: logging.Logger
) -> List[Dict[str, Any]]:
    """Invoke ``post_plan`` hook if the provider implements it."""
    if not has_hook(provider, "post_plan"):
        return actions
    _log(
        logger, "hook_post_plan", provider=getattr(provider, "name", "?"), action_count=len(actions)
    )
    result = invoke_hook(provider, "post_plan", actions)
    return result if isinstance(result, list) else actions


# ---------------------------------------------------------------------------
# Apply hooks
# ---------------------------------------------------------------------------


def run_pre_apply(
    provider: Any, actions: List[Dict[str, Any]], logger: logging.Logger
) -> List[Dict[str, Any]]:
    """Invoke ``pre_apply`` hook if the provider implements it."""
    if not has_hook(provider, "pre_apply"):
        return actions
    _log(
        logger, "hook_pre_apply", provider=getattr(provider, "name", "?"), action_count=len(actions)
    )
    result = invoke_hook(provider, "pre_apply", actions)
    return result if isinstance(result, list) else actions


def run_post_apply(provider: Any, result: Any, logger: logging.Logger) -> None:
    """Invoke ``post_apply`` hook if the provider implements it."""
    if not has_hook(provider, "post_apply"):
        return
    _log(logger, "hook_post_apply", provider=getattr(provider, "name", "?"))
    invoke_hook(provider, "post_apply", result)


def run_on_error(provider: Any, error: Exception, phase: str, logger: logging.Logger) -> None:
    """Invoke ``on_error`` hook if the provider implements it."""
    if not has_hook(provider, "on_error"):
        return
    _log(
        logger,
        "hook_on_error",
        provider=getattr(provider, "name", "?"),
        phase=phase,
        error=str(error),
    )
    invoke_hook(provider, "on_error", error, {"phase": phase})


# ---------------------------------------------------------------------------
# Advanced hooks
# ---------------------------------------------------------------------------


def run_estimate_cost(
    provider: Any, actions: List[Dict[str, Any]], logger: logging.Logger
) -> Optional[CostEstimate]:
    """Invoke ``estimate_cost`` hook if the provider implements it."""
    if not has_hook(provider, "estimate_cost"):
        return None
    _log(
        logger,
        "hook_estimate_cost",
        provider=getattr(provider, "name", "?"),
        action_count=len(actions),
    )
    return invoke_hook(provider, "estimate_cost", actions)


def run_validate_sovereignty(
    provider: Any, contract: Dict[str, Any], logger: logging.Logger
) -> List[str]:
    """Invoke ``validate_sovereignty`` hook if the provider implements it."""
    if not has_hook(provider, "validate_sovereignty"):
        return []
    _log(logger, "hook_validate_sovereignty", provider=getattr(provider, "name", "?"))
    result = invoke_hook(provider, "validate_sovereignty", contract)
    return result if isinstance(result, list) else []
