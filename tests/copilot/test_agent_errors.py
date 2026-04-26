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

"""Pin the typed exception hierarchy for forge-stage failures.

The plan (gap closure, plan item #18 under "Deferred to v1.1+ / Schemas,
validation, parsing, resilience") requires three plan-named exceptions at
``fluid_build/copilot/agents/errors.py``:

* ``FluidGenerationError``  — any forge-pipeline failure
* ``DDLGenerationError``    — from-ddl parse/convert failures
* ``AgentExecutionError``   — stage agent run failures

The design decision (deliberate, made during the fast+medium+big sweep)
is that these MUST inherit from the repo-wide ``fluid_build.errors.FluidError``
so every existing ``except FluidError:`` handler keeps working. Without
that guarantee, introducing the plan-named vocabulary would silently
bypass the error-reporting paths the rest of the CLI already has.

This file pins:

1. All three classes exist and are importable from the plan's path.
2. The parent-child chain is exactly as the plan names it
   (``DDLGenerationError`` and ``AgentExecutionError`` are both
   ``FluidGenerationError`` subclasses — NOT siblings).
3. Every one of them is still catchable via ``FluidError`` — so legacy
   handlers are not bypassed.
4. The base-class features (``message``, ``context``, ``original_error``,
   ``suggestions``) survive the inheritance so error-wrapping helpers
   continue to work.
"""

from __future__ import annotations

import pytest

from fluid_build.copilot.agents.errors import (
    AgentExecutionError,
    DDLGenerationError,
    FluidGenerationError,
)
from fluid_build.errors import FluidError

# ---------------------------------------------------------------------------
# Hierarchy
# ---------------------------------------------------------------------------


def test_fluid_generation_error_inherits_from_fluid_error() -> None:
    assert issubclass(FluidGenerationError, FluidError)


def test_ddl_generation_error_inherits_from_fluid_generation_error() -> None:
    assert issubclass(DDLGenerationError, FluidGenerationError)
    # Transitively also a FluidError — the legacy-handler guarantee.
    assert issubclass(DDLGenerationError, FluidError)


def test_agent_execution_error_inherits_from_fluid_generation_error() -> None:
    assert issubclass(AgentExecutionError, FluidGenerationError)
    assert issubclass(AgentExecutionError, FluidError)


def test_ddl_and_agent_errors_are_siblings_not_ancestors() -> None:
    """Neither derived class should be an ancestor of the other — they
    describe orthogonal failure modes and must be catchable separately."""
    assert not issubclass(DDLGenerationError, AgentExecutionError)
    assert not issubclass(AgentExecutionError, DDLGenerationError)


# ---------------------------------------------------------------------------
# Catchability — the whole point of inheriting from FluidError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls",
    [FluidGenerationError, DDLGenerationError, AgentExecutionError],
)
def test_every_plan_named_error_is_catchable_as_fluid_error(exc_cls) -> None:
    """Legacy ``except FluidError:`` handlers must keep catching the new
    plan-named errors — this is the backward-compatibility contract."""
    with pytest.raises(FluidError):
        raise exc_cls("boom")


def test_ddl_error_caught_as_fluid_generation_error() -> None:
    with pytest.raises(FluidGenerationError):
        raise DDLGenerationError("bad SQL")


def test_agent_error_caught_as_fluid_generation_error() -> None:
    with pytest.raises(FluidGenerationError):
        raise AgentExecutionError("provider 500")


# ---------------------------------------------------------------------------
# Base-class features must survive (context, original_error, suggestions)
# ---------------------------------------------------------------------------


def test_base_class_context_preserved_on_plan_named_errors() -> None:
    """``FluidError.context`` / ``original_error`` / ``suggestions`` must
    remain usable — a lot of the CLI's error-reporting formats on these
    attributes and silently degrading them would break diagnostics."""
    original = ValueError("root cause")
    exc = AgentExecutionError(
        "agent call failed",
        context={"stage": "logical", "attempt": 3},
        original_error=original,
        suggestions=["retry with --tiered=false"],
    )
    assert exc.message == "agent call failed"
    assert exc.context == {"stage": "logical", "attempt": 3}
    assert exc.original_error is original
    assert exc.suggestions == ["retry with --tiered=false"]
    # __str__ surfaces the "caused by" line from FluidError.
    rendered = str(exc)
    assert "agent call failed" in rendered
    assert "root cause" in rendered


def test_wrap_error_helper_works_with_plan_named_errors() -> None:
    """``fluid_build.errors.wrap_error`` accepts any FluidError subclass —
    confirm the plan-named ones round-trip through it."""
    from fluid_build.errors import wrap_error

    original = RuntimeError("parser crash")
    wrapped = wrap_error(
        original,
        "failed to parse CREATE TABLE",
        error_class=DDLGenerationError,
        context={"file": "schema.sql"},
    )
    assert isinstance(wrapped, DDLGenerationError)
    assert isinstance(wrapped, FluidError)
    assert wrapped.original_error is original
    assert wrapped.context == {"file": "schema.sql"}
