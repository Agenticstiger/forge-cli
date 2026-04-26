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

"""V2 conceptual agent wrapper.

.. note::

    **Internal-composition agent.** :class:`ConceptualAgent` wraps
    :class:`fluid_build.copilot.agents.modeler_agent.ModelerAgent`'s
    conceptual sub-stage. It is composed inside
    :class:`fluid_build.copilot.agents.logical_agent.LogicalAgent`
    when the staged pipeline runs.

    Direct use is supported for v1.3 orchestrators that want to
    drive the conceptual stage in isolation (e.g. an interactive
    UI that proposes the entity model before user approval).
    Most production code should prefer
    :class:`fluid_build.copilot.agents.coordinator.StageCoordinator`
    methods (``from_intent`` / ``from_tables`` / ``from_catalog``)
    — those run the full staged pipeline including conceptual.
"""

from __future__ import annotations

from fluid_build.copilot.agents.modeler_agent import ModelerAgent
from fluid_build.copilot.schemas.intent import BusinessIntent
from fluid_build.forge_datamodel.from_ddl.parser import TableDefinition


class ConceptualAgent:
    """Conceptual stage wrapper around :class:`ModelerAgent`.

    Direct use is **discouraged** in v1.5+ — see module docstring
    for the recommended entry points. The class is retained for
    backwards compatibility with v1.3 orchestrators that explicitly
    drive the conceptual stage.
    """

    def __init__(self) -> None:
        self._modeler = ModelerAgent()

    def from_tables(self, *, name: str, tables: list[TableDefinition]):
        return self._modeler._conceptual_from_tables(name=name, tables=tables)

    def from_intent(self, intent: BusinessIntent):
        return self._modeler._conceptual_from_intent(intent)
