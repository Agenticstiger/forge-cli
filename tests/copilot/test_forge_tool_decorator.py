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

"""Unit tests for :func:`fluid_build.cli.forge_tool.forge_tool`.

The decorator is the world-class replacement for the hand-written
``TOOL_REGISTRY`` dict entries — these tests pin its contracts:

* args-schema is the source of truth, JSON Schema derives from it,
* ``workspace_root`` is dispatcher-injected and never exposed to the LLM,
* dispatch returns structured success/failure results so the agent loop
  can route corrective feedback,
* the legacy dict shape is preserved for the existing
  ``dispatch_tool_call`` consumer,
* the langchain ``BaseTool`` is built lazily (no eager dep on
  langchain-core for users on the legacy path).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from fluid_build.cli.forge_tool import (
    FORGE_TOOL_REGISTRY,
    ForgeTool,
    ToolDispatchResult,
    dispatch_forge_tool,
    forge_tool,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Each test gets a fresh registry — autouse so the decorator's
    side effect doesn't leak across the suite. Save/restore so we
    don't wipe out the production tools registered by
    ``forge_copilot_tools`` import (under pytest-randomly other test
    modules can share-load the same module and depend on those tools
    being registered)."""
    saved = dict(FORGE_TOOL_REGISTRY)
    FORGE_TOOL_REGISTRY.clear()
    yield
    FORGE_TOOL_REGISTRY.clear()
    FORGE_TOOL_REGISTRY.update(saved)


class EchoArgs(BaseModel):
    text: str = Field(description="Text to echo back.")


class WorkspaceArgs(BaseModel):
    relative_path: str = Field(default=".", description="Path under the workspace root.")


class TestForgeToolBasics:
    def test_decorator_registers_tool_under_function_name(self) -> None:
        @forge_tool(args_schema=EchoArgs)
        def echo(args: EchoArgs):
            """Echo the input text back."""
            return {"echoed": args.text}

        assert "echo" in FORGE_TOOL_REGISTRY
        tool = FORGE_TOOL_REGISTRY["echo"]
        assert isinstance(tool, ForgeTool)
        assert tool.name == "echo"
        # Description defaults to the docstring's first line.
        assert tool.description == "Echo the input text back."

    def test_explicit_name_and_description_override_defaults(self) -> None:
        @forge_tool(
            name="say_hello",
            description="Custom description.",
            args_schema=EchoArgs,
        )
        def _impl(args):
            return args.text

        tool = FORGE_TOOL_REGISTRY["say_hello"]
        assert tool.name == "say_hello"
        assert tool.description == "Custom description."

    def test_input_schema_is_derived_from_args_model(self) -> None:
        @forge_tool(args_schema=EchoArgs)
        def echo(args):
            return args.text

        schema = FORGE_TOOL_REGISTRY["echo"].input_schema
        assert schema["type"] == "object"
        assert "text" in schema["properties"]
        assert schema["required"] == ["text"]
        # additionalProperties must be False — the LLM shouldn't be
        # able to sneak extra fields past validation.
        assert schema["additionalProperties"] is False

    def test_register_false_skips_registration(self) -> None:
        @forge_tool(args_schema=EchoArgs, register=False)
        def echo(args):
            return args.text

        assert "echo" not in FORGE_TOOL_REGISTRY
        # But the decorator still returns a usable ForgeTool.
        assert isinstance(echo, ForgeTool)

    def test_non_pydantic_args_schema_is_rejected_at_decoration(self) -> None:
        with pytest.raises(TypeError, match="Pydantic BaseModel"):

            @forge_tool(args_schema=dict)  # type: ignore[arg-type]
            def bad(args):
                return args


class TestForgeToolDispatch:
    def test_successful_dispatch_returns_value(self) -> None:
        @forge_tool(args_schema=EchoArgs)
        def echo(args: EchoArgs):
            return {"echoed": args.text}

        result = dispatch_forge_tool("echo", {"text": "hi"})
        assert result.ok
        assert result.value == {"echoed": "hi"}

    def test_unknown_tool_returns_typed_failure(self) -> None:
        result = dispatch_forge_tool("does_not_exist", {})
        assert not result.ok
        assert result.error_type == "UnknownTool"
        assert "does_not_exist" in result.error_message

    def test_validation_error_returns_tool_validation_error(self) -> None:
        @forge_tool(args_schema=EchoArgs)
        def echo(args):
            return args.text

        # Missing required ``text`` arg.
        result = dispatch_forge_tool("echo", {})
        assert not result.ok
        assert result.error_type == "ToolValidationError"

    def test_impl_exception_surfaces_in_failure_result(self) -> None:
        @forge_tool(args_schema=EchoArgs)
        def boom(args):
            raise RuntimeError("kaboom")

        result = dispatch_forge_tool("boom", {"text": "x"})
        assert not result.ok
        assert result.error_type == "RuntimeError"
        assert "kaboom" in result.error_message

    def test_dispatch_result_factories(self) -> None:
        ok = ToolDispatchResult.success({"a": 1})
        assert ok.ok and ok.value == {"a": 1}

        bad = ToolDispatchResult.failure("X", "y")
        assert not bad.ok and bad.error_type == "X" and bad.error_message == "y"


class TestWorkspaceRootInjection:
    def test_workspace_root_is_injected_when_aware(self) -> None:
        captured = {}

        @forge_tool(args_schema=WorkspaceArgs, workspace_root_aware=True)
        def lookup(args: WorkspaceArgs, *, workspace_root):
            captured["ws"] = workspace_root
            captured["path"] = args.relative_path
            return "ok"

        result = dispatch_forge_tool(
            "lookup",
            {"relative_path": "data/"},
            workspace_root=Path("/safe/zone"),
        )
        assert result.ok
        assert captured["ws"] == Path("/safe/zone")
        assert captured["path"] == "data/"

    def test_workspace_root_not_injected_when_not_aware(self) -> None:
        @forge_tool(args_schema=EchoArgs)
        def echo(args):
            return args.text

        # Even though the dispatcher is given a workspace_root, the
        # tool doesn't accept it (no kwarg in signature) — and the
        # dispatcher must not pass it through.
        result = dispatch_forge_tool("echo", {"text": "hi"}, workspace_root=Path("/x"))
        assert result.ok
        assert result.value == "hi"

    def test_workspace_root_is_not_in_input_schema(self) -> None:
        """Security: the LLM must not see ``workspace_root`` in the
        tool schema, otherwise it could try to set it directly.
        """

        @forge_tool(args_schema=WorkspaceArgs, workspace_root_aware=True)
        def lookup(args, *, workspace_root):
            return str(workspace_root)

        schema = FORGE_TOOL_REGISTRY["lookup"].input_schema
        assert "workspace_root" not in schema["properties"]


class TestLegacyCompatibility:
    """The forge tools must remain consumable through the existing
    ``TOOL_REGISTRY`` shape until every consumer migrates."""

    def test_legacy_dict_shape_is_complete(self) -> None:
        @forge_tool(
            name="echo",
            description="Echoes",
            args_schema=EchoArgs,
        )
        def echo(args):
            return args.text

        legacy = FORGE_TOOL_REGISTRY["echo"].legacy_dict
        assert set(legacy.keys()) == {"name", "description", "input_schema", "impl"}
        assert legacy["name"] == "echo"
        assert legacy["description"] == "Echoes"
        assert callable(legacy["impl"])

    def test_legacy_impl_accepts_flat_kwargs_with_workspace_root(self) -> None:
        captured = {}

        @forge_tool(args_schema=WorkspaceArgs, workspace_root_aware=True)
        def lookup(args: WorkspaceArgs, *, workspace_root):
            captured["ws"] = workspace_root
            captured["path"] = args.relative_path
            return "ok"

        legacy_impl = FORGE_TOOL_REGISTRY["lookup"].legacy_dict["impl"]
        result = legacy_impl(relative_path="x/", workspace_root=Path("/safe"))
        assert result == "ok"
        assert captured["ws"] == Path("/safe")
        assert captured["path"] == "x/"

    def test_legacy_impl_returns_typed_error_on_validation_failure(self) -> None:
        @forge_tool(args_schema=EchoArgs)
        def echo(args):
            return args.text

        legacy_impl = FORGE_TOOL_REGISTRY["echo"].legacy_dict["impl"]
        # Missing required arg → returns a typed error dict, matching
        # the existing security-hardened pattern (exception text never
        # round-trips into the LLM context).
        result = legacy_impl()  # no ``text``
        assert isinstance(result, dict)
        assert result["error"] == "ToolValidationError"
