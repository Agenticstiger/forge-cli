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

"""The FLUID LLM runtime — a cli-free lower tier below both ``cli`` and ``copilot``.

This subpackage holds the LLM-provider surface (provider adapters, the litellm
backend, the coding-agent bridge, provider plugins, config resolution, the model
catalog, the router, and the response schema). It used to live under
``fluid_build/cli/`` (``forge_copilot_llm_*`` / ``_llm_*`` modules), which made
``copilot`` — which genuinely depends on this runtime to call an LLM — carry a
``copilot -> cli`` import edge (a real cycle: ``cli`` also imports this surface).

Relocating the runtime here breaks that cycle honestly: ``copilot`` imports
``fluid_build.llm.*`` (a lower tier), and the old ``cli`` module paths remain as
thin ``sys.modules`` re-export shims so the ~40 cli importers and the test
patch-targets on ``fluid_build.cli.forge_copilot_llm_providers.*`` keep resolving
to the very same module objects (object identity ⇒ zero test churn).

This package must stay free of ``fluid_build.cli`` upstreams — the
``[tool.importlinter]`` "copilot must not depend on cli" contract enforces it
transitively (copilot imports this package). Keep ``__init__`` import-free so the
``fluid --help`` cold path never pulls httpx / litellm through a bare
``import fluid_build.llm``.
"""

from __future__ import annotations
