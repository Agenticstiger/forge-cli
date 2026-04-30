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

"""``@forge_tool`` decorator — the world-class replacement for the
hand-written ``TOOL_REGISTRY`` entries in :mod:`forge_copilot_tools`.

The legacy registry forces every contributor to write three things for
a single tool:

1. an ``impl`` function with explicit ``workspace_root`` plumbing,
2. a hand-rolled ``input_schema`` JSON-Schema dict that mirrors the
   function signature,
3. a ``_register("name", ...)`` call that wires the two together.

That's about 50 lines of boilerplate per tool plus an easy way to drift
the schema and the impl out of sync. ``@forge_tool`` collapses it to:

.. code-block:: python

    from pydantic import BaseModel, Field
    from fluid_build.cli.forge_tool import forge_tool

    class DiscoverArgs(BaseModel):
        relative_path: str = Field(default=".", description="Path under the workspace root")

    @forge_tool(
        name="discover_workspace",
        description="Scan the workspace for data files...",
        args_schema=DiscoverArgs,
        workspace_root_aware=True,
    )
    def discover_workspace(args: DiscoverArgs, *, workspace_root):
        return _discover_impl(workspace_root, args.relative_path)

The decorator:

* registers the tool in :data:`FORGE_TOOL_REGISTRY` (canonical world-class
  registry) **and** in the legacy ``TOOL_REGISTRY`` dict shape so the
  existing ``dispatch_tool_call`` consumer keeps working unchanged,
* generates the LLM-facing JSON Schema **from the Pydantic args model**
  so the schema and the impl are guaranteed consistent,
* enforces the ``workspace_root`` security boundary — the kwarg is
  injected at dispatch time and is **never** exposed to the LLM
  (``args_schema`` excludes it by construction),
* exposes a langchain-compatible ``BaseTool`` (the
  :class:`langchain_core.tools.BaseTool` returned by ``@tool``) so the
  same registry feeds both the legacy provider path and the new
  langchain-core ChatModel path without re-declaration.

The langchain-core dependency is imported lazily at decorator-call time
so importing this module on the legacy path doesn't force the langchain
extra to be installed. If the user opts into ``[langchain]`` and uses
``@forge_tool``, the import succeeds and they get the BaseTool surface;
if they don't, the legacy dict is still produced and the tool still
works on the legacy dispatcher.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel

__all__ = [
    "FORGE_TOOL_REGISTRY",
    "ForgeTool",
    "ToolDispatchResult",
    "dispatch_forge_tool",
    "forge_tool",
    "register_forge_tool",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class ToolDispatchResult:
    """Result of dispatching a forge tool.

    The agent loop wraps either ``value`` or ``error`` into the
    provider-specific tool-result message format. Errors are typed
    so the loop can route corrective feedback to the LLM (instead
    of bubbling a generic exception out of the run).
    """

    ok: bool
    value: Any = None
    error_type: str = ""
    error_message: str = ""

    @classmethod
    def success(cls, value: Any) -> "ToolDispatchResult":
        return cls(ok=True, value=value)

    @classmethod
    def failure(cls, error_type: str, error_message: str) -> "ToolDispatchResult":
        return cls(ok=False, error_type=error_type, error_message=error_message)


@dataclass
class ForgeTool:
    """Forge-side tool descriptor.

    Holds enough metadata to feed both code paths (legacy hand-rolled
    dispatch + langchain ``BaseTool``) without re-declaring the tool.

    Fields:

    * ``name`` / ``description`` / ``args_schema``: the contract.
    * ``impl``: the underlying function. Always takes the args-model
      instance as a positional arg; ``workspace_root`` (or other
      injected context) comes through as kwargs.
    * ``workspace_root_aware``: if ``True``, dispatch will inject the
      caller's ``workspace_root`` as a kwarg. The args-schema does NOT
      include this field — the LLM cannot supply it.
    * ``_lc_tool``: lazy-built langchain ``BaseTool`` instance. Populated
      only when ``langchain_tool`` is accessed so the langchain extra
      stays optional at import time.
    * ``tags``: optional taxonomy labels (used by the capability matrix
      to scope tool subsets per provider).
    """

    name: str
    description: str
    args_schema: Type[BaseModel]
    impl: Callable[..., Any]
    workspace_root_aware: bool = False
    tags: List[str] = field(default_factory=list)
    _lc_tool: Any = field(default=None, repr=False)

    @property
    def input_schema(self) -> Dict[str, Any]:
        """Return the LLM-facing JSON Schema for the tool's args.

        Derived from the Pydantic args-model — the source of truth.
        """
        schema = self.args_schema.model_json_schema()
        # Tools must reject unknown args by default; the LLM should
        # not be able to sneak extra fields past validation.
        schema.setdefault("additionalProperties", False)
        return schema

    @property
    def legacy_dict(self) -> Dict[str, Any]:
        """Return the dict shape expected by the legacy
        :mod:`forge_copilot_tools` ``TOOL_REGISTRY`` so the existing
        ``dispatch_tool_call`` consumer doesn't have to change.

        The ``impl`` here is the dispatch wrapper — accepting flat
        kwargs (the way the legacy path calls), validating them
        through the Pydantic args-model, and routing through
        :meth:`dispatch`.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "impl": self._legacy_impl,
        }

    @property
    def langchain_tool(self) -> Any:
        """Return the langchain ``BaseTool`` for this forge tool.

        Lazy because langchain-core is an optional extra; importing
        the module shouldn't drag the dep in for users on the legacy
        path. Once built, the BaseTool is cached so repeated access
        is cheap.
        """
        if self._lc_tool is None:
            self._lc_tool = self._build_langchain_tool()
        return self._lc_tool

    def dispatch(
        self,
        arguments: Dict[str, Any],
        *,
        workspace_root: Optional[Any] = None,
    ) -> ToolDispatchResult:
        """Validate ``arguments`` against the args-schema and run the
        tool. Returns a :class:`ToolDispatchResult` so callers can
        branch on success/failure without try/except every call site.
        """
        try:
            args_model = self.args_schema.model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 — Pydantic ValidationError + edge cases
            return ToolDispatchResult.failure(
                error_type="ToolValidationError",
                error_message=f"Tool '{self.name}' got invalid args: {exc}",
            )

        kwargs: Dict[str, Any] = {}
        if self.workspace_root_aware:
            kwargs["workspace_root"] = workspace_root

        try:
            value = self.impl(args_model, **kwargs)
        except Exception as exc:  # noqa: BLE001 — surface as typed result
            return ToolDispatchResult.failure(
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        return ToolDispatchResult.success(value)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _legacy_impl(self, **flat_kwargs: Any) -> Any:
        """Adapter for the legacy registry contract.

        The legacy ``dispatch_tool_call`` calls ``tool["impl"](**kwargs)``
        with ``workspace_root`` mixed into ``kwargs``. We split the
        injected fields out, validate the rest through the args model,
        and run the real impl.
        """
        workspace_root = flat_kwargs.pop("workspace_root", None)
        # Drop any unknown injected kwargs the legacy plumbing might
        # have added. Anything that *should* be a real argument has to
        # be in ``args_schema``.
        result = self.dispatch(flat_kwargs, workspace_root=workspace_root)
        if not result.ok:
            return {
                "error": result.error_type,
                "message": result.error_message,
            }
        return result.value

    def _build_langchain_tool(self) -> Any:
        """Build the langchain ``BaseTool`` instance.

        Uses a thin closure that mirrors :meth:`dispatch` but skips
        ``workspace_root`` injection — the langchain path expects the
        forge dispatcher to scope the tool to a workspace at bind time
        (see :func:`bind_workspace_root_to_lc_tools`), not at call time.
        """
        from langchain_core.tools import StructuredTool

        impl = self.impl
        args_schema = self.args_schema

        def _runnable(**flat_kwargs: Any) -> Any:
            args_model = args_schema.model_validate(flat_kwargs)
            return impl(args_model)

        return StructuredTool.from_function(
            func=_runnable,
            name=self.name,
            description=self.description,
            args_schema=args_schema,
        )


# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------

FORGE_TOOL_REGISTRY: Dict[str, ForgeTool] = {}
"""Canonical registry of every ``@forge_tool``-registered tool.

Keyed by tool name. Both the legacy ``dispatch_tool_call`` path and
the new langchain ChatModel path resolve tools through this registry
so contributors never have to maintain two declarations.
"""


def register_forge_tool(tool: ForgeTool) -> None:
    """Register ``tool`` under its name. Re-registration replaces the
    prior entry — useful for testing where a fixture wants to stub a
    real tool.
    """
    FORGE_TOOL_REGISTRY[tool.name] = tool


def dispatch_forge_tool(
    name: str,
    arguments: Dict[str, Any],
    *,
    workspace_root: Optional[Any] = None,
) -> ToolDispatchResult:
    """Resolve ``name`` in the forge registry and dispatch.

    Returns a structured :class:`ToolDispatchResult` so the agent loop
    can inspect the failure mode (`"ToolValidationError"`, custom
    impl exception class names) and route corrective feedback to the
    LLM, instead of catching a bare ``Exception``.
    """
    tool = FORGE_TOOL_REGISTRY.get(name)
    if tool is None:
        return ToolDispatchResult.failure(
            error_type="UnknownTool",
            error_message=f"Tool '{name}' is not registered",
        )
    return tool.dispatch(arguments, workspace_root=workspace_root)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def forge_tool(
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    args_schema: Type[BaseModel],
    workspace_root_aware: bool = False,
    tags: Optional[List[str]] = None,
    register: bool = True,
) -> Callable[[Callable[..., Any]], ForgeTool]:
    """Register ``func`` as a forge-compatible tool.

    Parameters
    ----------
    name
        Tool name as exposed to the LLM. Defaults to the function name.
    description
        Tool description as exposed to the LLM. Defaults to the
        function's docstring (first line) so single-purpose tools don't
        repeat themselves.
    args_schema
        Pydantic model describing the tool's input arguments. The model
        is the source of truth — JSON Schema is derived from it, and
        the impl receives an instance, not a flat dict. ``workspace_root``
        and other injected context fields must NOT appear in this model.
    workspace_root_aware
        When ``True``, the dispatcher injects ``workspace_root`` as a
        kwarg. Required for any tool that touches the filesystem so the
        path-confinement security model is preserved.
    tags
        Optional taxonomy labels — currently used by the planned
        capability matrix to scope which tools are advertised on which
        providers (e.g. tools that require ``tool_use`` shouldn't be
        sent to providers without that capability).
    register
        When ``True`` (default), register the tool in
        :data:`FORGE_TOOL_REGISTRY` at decoration time. Set to ``False``
        for tools that should be built on demand by tests / fixtures.
    """
    if not (inspect.isclass(args_schema) and issubclass(args_schema, BaseModel)):
        raise TypeError(
            "forge_tool requires a Pydantic BaseModel subclass as args_schema; "
            f"got {args_schema!r}"
        )

    def decorate(func: Callable[..., Any]) -> ForgeTool:
        tool_name = name or func.__name__
        tool_desc = description or _first_docstring_line(func) or tool_name
        tool = ForgeTool(
            name=tool_name,
            description=tool_desc,
            args_schema=args_schema,
            impl=func,
            workspace_root_aware=workspace_root_aware,
            tags=list(tags or []),
        )
        if register:
            register_forge_tool(tool)
        return tool

    return decorate


def _first_docstring_line(func: Callable[..., Any]) -> str:
    doc = inspect.getdoc(func)
    if not doc:
        return ""
    return doc.splitlines()[0].strip()
