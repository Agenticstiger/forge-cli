# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Backwards-compat shim for the staged-LLM "thinking" status panel.

The renderer moved to the tier-0 shared leaf :mod:`fluid_build._agent_progress`
so ``fluid_build.copilot`` can wrap its staged LLM calls in the panel without
inducing a ``copilot -> cli`` edge (enforced by the ``[tool.importlinter]``
contracts). This module re-exports the whole surface so the existing
``from ...cli.progress import AgentStatus`` sites keep working unchanged.
"""

from __future__ import annotations

from fluid_build._agent_progress import AgentStatus, _status_disabled

__all__ = ["AgentStatus", "_status_disabled"]
