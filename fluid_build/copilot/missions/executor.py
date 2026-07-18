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

"""The EXECUTE seam — Protocols the runner depends on, wired by ``cli``.

``fluid_build.copilot`` must not import ``fluid_build.cli`` (import-linter
contract "copilot must not depend on cli", ``allow_indirect_imports =
false``, so function-local imports do not evade it). The rationale is in
pyproject.toml: PR #391 severed a ``copilot ⇄ cli`` cycle by relocating
the LLM runtime to the cli-free ``fluid_build.llm`` tier, and the
contract keeps it severed.

The mission runner genuinely needs three things that legitimately live in
``cli``: the inner agent loop, the seed-context builder, and the
provenance-stamping contract writer. Rather than reach up a tier, the
runner declares **what it needs** as Protocols here and ``cli/mission.py``
— which may import both layers — supplies the implementations.

This also matches the RFC's own framing: ``MissionRunner`` is a thin
outer loop that "composes exclusively from existing machinery". Owning
the *loop* while injecting the *machinery* is what makes that literally
true rather than aspirational, and it is why the unit tests can drive a
full mission with no LLM, no TTY, and no clock.

Nothing in this module imports ``cli``, and there are deliberately **no
production defaults** for :class:`MissionRuntime`'s members. A fallback
contract writer that skipped the provenance envelope, or an executor
that quietly no-op'd, would make tests pass against behaviour production
never runs — the exact divergence this seam exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

if TYPE_CHECKING:  # pragma: no cover — typing only
    from fluid_build.copilot.missions.planner import MissionStep
    from fluid_build.copilot.missions.spec import MissionSpec


class MissionStepExecutor(Protocol):
    """Runs one planned step and returns the proposed contract.

    Implementations return the full proposed contract dict, or ``None``
    when the step produced nothing usable. They **must not write** — the
    runner owns the write, because every proposed change has to pass the
    fail-closed destructive gate first.
    """

    def __call__(
        self,
        step: "MissionStep",
        contract: Dict[str, Any],
        *,
        spec: "MissionSpec",
        llm_config: Any,
        workspace_root: Path,
        console: Any = None,
    ) -> Optional[Dict[str, Any]]:  # pragma: no cover — Protocol
        ...


class ContractWriter(Protocol):
    """Serialises an approved contract to *path*.

    Called only after the destructive gate approves. Production wires
    ``cli.forge_contract_factory.write_contract``, which stamps the
    provenance envelope.
    """

    def __call__(
        self, contract: Dict[str, Any], path: Path, *, command: str
    ) -> None:  # pragma: no cover — Protocol
        ...


class JsonObjectParser(Protocol):
    """Extracts a JSON object from a raw LLM response.

    Production wires ``cli.forge_copilot_contract_helpers.
    extract_json_object`` (via ``forge_copilot_runtime``) so the planner
    tolerates code fences and preamble exactly like every other
    LLM-response reader in the codebase. Raises ``ValueError`` when the
    text holds no JSON object.
    """

    def __call__(self, text: str) -> Dict[str, Any]:  # pragma: no cover — Protocol
        ...


class MissionRuntime:
    """The cli-supplied collaborators a mission run needs.

    Constructed by ``cli/mission.py::build_mission_runtime`` in
    production and by fakes in tests. All three members are required —
    see the module docstring on why there are no defaults.
    """

    __slots__ = ("execute", "write_contract", "parse_json")

    def __init__(
        self,
        *,
        execute: MissionStepExecutor,
        write_contract: ContractWriter,
        parse_json: JsonObjectParser,
    ) -> None:
        self.execute = execute
        self.write_contract = write_contract
        self.parse_json = parse_json


__all__ = [
    "ContractWriter",
    "JsonObjectParser",
    "MissionRuntime",
    "MissionStepExecutor",
]
