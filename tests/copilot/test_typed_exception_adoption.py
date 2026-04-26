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

"""Pin typed-exception adoption at every site that should use them.

V1.3.4 closes the gap between *declaring* the typed exception hierarchy
(landed as F1 in v1.0.1) and *raising* the typed classes at the right
sites. Until this test landed, ``BaseStageAgent`` raised bare
``RuntimeError`` for "no LLM configured" and "provider leak" — both of
which are squarely "stage agent run failures" and so should surface as
``AgentExecutionError`` for the documented ``except AgentExecutionError``
handler path.

The pins below are intentionally **two-layer**:

1. **Behavioural** — the ``BaseStageAgent.call()`` happy path is
   exercised with bad inputs; the test asserts the typed exception is
   raised and that the legacy ``FluidError`` parent still catches it
   (backward-compat contract from F1).
2. **Static** — a source-grep regression that fails if a future PR
   re-introduces ``raise RuntimeError(`` in either of the typed sites.
   Without this guard, a refactor could silently revert the typing.

The static guard is deliberately scoped to the two files we converted
(not "all of copilot") because:

* ``store/factory.py`` and ``store/backends/postgres.py`` raise on
  *configuration* issues that are not "stage execution" failures —
  they fire before any agent is constructed. Typing them as
  ``AgentExecutionError`` would mis-categorise them.
* ``forge_datamodel/from_ddl/snowflake_dumper.py`` IS in the static
  list because dump failures DO belong to ``DDLGenerationError`` —
  they're the from-ddl path's "couldn't even fetch the source DDL"
  surface.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from fluid_build.copilot.agents.base import BaseStageAgent, StageSession
from fluid_build.copilot.agents.errors import (
    AgentExecutionError,
    DDLGenerationError,
    FluidGenerationError,
)
from fluid_build.copilot.schemas.stage_outputs import StructuredOutputModel
from fluid_build.copilot.store.backends.null import NullBackend
from fluid_build.errors import FluidError

# ----------------------------------------------------------------------
# Behavioural — BaseStageAgent.call raises typed exceptions
# ----------------------------------------------------------------------


class _DummyOutput(StructuredOutputModel):
    """Minimal Pydantic shape sufficient to satisfy the type bound."""

    name: str = "dummy"


class TestBaseStageAgentTypedRaises:
    def test_no_llm_config_raises_agent_execution_error(self):
        agent = BaseStageAgent(stage="logical", tier="balanced")
        session = StageSession(store=NullBackend())  # llm_config left as None
        with pytest.raises(AgentExecutionError) as exc_info:
            agent.call(
                session,
                system_prompt="s",
                user_prompt="u",
                output_schema=_DummyOutput,
            )
        # The typed ancestor is the documented catch-all for forge
        # failures; legacy handlers must keep catching this too.
        assert isinstance(exc_info.value, FluidGenerationError)
        assert isinstance(exc_info.value, FluidError)
        assert "No LLM configuration" in str(exc_info.value)


# ----------------------------------------------------------------------
# Static — no bare RuntimeError(...) regressed into the typed sites
# ----------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TYPED_SITES = [
    _REPO_ROOT / "fluid_build" / "copilot" / "agents" / "base.py",
    _REPO_ROOT / "fluid_build" / "forge_datamodel" / "from_ddl" / "snowflake_dumper.py",
]
# ``raise RuntimeError(`` matches the most common regression shape; the
# regex tolerates whitespace variants (e.g. ``raise  RuntimeError(``).
_RAW_RUNTIME_PATTERN = re.compile(r"raise\s+RuntimeError\s*\(")


@pytest.mark.parametrize("path", _TYPED_SITES, ids=lambda p: p.name)
def test_no_bare_runtime_error_in_typed_sites(path: Path) -> None:
    """Source-grep regression — these files must use the typed
    exception hierarchy (``AgentExecutionError`` /
    ``DDLGenerationError``) for *application* failures, not the bare
    ``RuntimeError`` from before V1.3.4. A future PR re-introducing
    ``raise RuntimeError(`` in either file fails this test loudly."""
    text = path.read_text(encoding="utf-8")
    matches = _RAW_RUNTIME_PATTERN.findall(text)
    assert not matches, (
        f"{path.name}: found {len(matches)} bare RuntimeError raise(s); "
        "convert to AgentExecutionError / DDLGenerationError "
        "(see test docstring for the rationale)."
    )


def test_typed_exceptions_actually_imported_in_typed_sites():
    """Inverse pin: the typed sites must actively import the typed
    exception they raise — defends against a refactor that drops the
    raise but leaves the import (which would silently broaden any
    catch downstream)."""
    base = (_REPO_ROOT / "fluid_build" / "copilot" / "agents" / "base.py").read_text(
        encoding="utf-8"
    )
    assert "AgentExecutionError" in base

    dumper = (
        _REPO_ROOT / "fluid_build" / "forge_datamodel" / "from_ddl" / "snowflake_dumper.py"
    ).read_text(encoding="utf-8")
    assert "DDLGenerationError" in dumper


# ----------------------------------------------------------------------
# Cross-check — every typed-exception class is exported from one place
# ----------------------------------------------------------------------


def test_all_typed_exceptions_accessible_via_one_import():
    """One canonical import path — ``fluid_build.copilot.agents.errors``.

    A new typed exception added elsewhere (e.g. as a sibling helper
    file) without re-exporting through this module would split the
    public API and silently break ``from fluid_build.copilot.agents.errors
    import *`` style usage in user scripts."""
    from fluid_build.copilot.agents import errors

    for name in ("FluidGenerationError", "DDLGenerationError", "AgentExecutionError"):
        assert hasattr(errors, name), f"{name} not exported from copilot.agents.errors"
