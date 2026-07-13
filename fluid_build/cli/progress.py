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
