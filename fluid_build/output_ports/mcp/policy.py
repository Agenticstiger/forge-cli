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

"""Access-control policy for the consumer-side MCP output-port server.

The output-port server is distinct from the authoring-side ``fluid mcp
serve`` (whose policy lives in :mod:`fluid_build.cli.mcp`). Authoring
mutates filesystem paths and store namespaces; the consumer-side
server queries production data, so its threat model and policy
surface differ:

* No writable filesystem roots — the server is read-only against the
  underlying engine and never writes consumer-supplied paths.
* No store namespaces — there is no logical/sidecar to mutate.
* New surface: ``allow_free_form_sql`` (default OFF) gates the
  free-form ``query_sql`` tool; without it, callers must use the
  predeclared semantic ``query`` tool.
* New surface: ``max_sample_rows`` (default 100) caps the row count
  any single ``sample`` call can return.
* Reused: ``allowed_tools`` / ``denied_tools`` / ``readable_paths``
  carry the same semantics as the authoring server so operators can
  apply consistent policies across both surfaces.

Column-level masking comes from ``expose.policy.authz.columnRestrictions``
inside the contract — it is not a server-side policy field, because the
restrictions belong to the data product, not the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from fluid_build.policy.agent_policy import (
    is_model_allowed as _is_model_allowed,
)
from fluid_build.policy.agent_policy import (
    is_use_case_allowed as _is_use_case_allowed,
)


@dataclass(frozen=True)
class OutputPortPolicy:
    """Consumer-side MCP server policy.

    ``allowed_tools = None`` means "all tools allowed"; an empty tuple
    means "no tools allowed" — useful when an operator wants the
    server to advertise itself but reject every call (e.g. during a
    change freeze).

    Defaults are conservative: every option that could widen the
    server's surface is OFF until explicitly enabled.
    """

    read_only: bool = True
    """Reject any tool that mutates the underlying engine.

    All Phase-1 tools are read-only by design; this flag is reserved
    for forward-compat with future write tools and for symmetry with
    the authoring server. Default ``True`` so that any future write
    tool stays disabled until the operator opts in.
    """

    allowed_tools: Optional[Tuple[str, ...]] = None
    """Allowlist of tool names; ``None`` allows every advertised tool.

    Tools not in the allowlist are also hidden from ``tools/list`` so
    upstream agents (Claude Code, Cursor) do not advertise calls
    doomed to fail.
    """

    denied_tools: Tuple[str, ...] = ()
    """Blocklist of tool names. Evaluated before ``allowed_tools`` so
    denial wins."""

    readable_paths: Tuple[Path, ...] = field(default_factory=lambda: (Path.cwd().resolve(),))
    """Filesystem roots the server may read from.

    Today the only path-based read is the contract YAML itself; the
    consumer-side server never opens caller-supplied paths. The field
    exists to keep the policy shape symmetric with authoring and to
    leave room for future tools (e.g. an OpenAPI doc resolver).
    """

    allow_free_form_sql: bool = False
    """Permit the optional ``query_sql`` tool to execute caller-
    supplied SQL.

    Default OFF because free-form SQL bypasses the semantic-layer
    safety net (predeclared measures + dimensions). When ON, the
    query compiler still calls
    :func:`fluid_build.providers._sql_safety.validate_sql_expression_allowlist`
    on every untrusted string, so the surface is bounded but wider.

    Operators should leave this OFF for LLM-driven agents and only
    enable it for trusted internal copilots that have to handle
    ad-hoc analyst questions.
    """

    max_sample_rows: int = 100
    """Hard cap for the ``sample`` tool's row count.

    A consumer can request fewer rows but never more. Defends against
    a curious agent that asks for ``limit: 100_000_000`` against a
    petabyte-scale lake. Default ``100`` is enough for "show me what
    this looks like" without exposing meaningful data volumes.
    """

    expose_id: Optional[str] = None
    """The exposeId this server is bound to.

    Phase-1 servers are single-expose. Phase-2 will widen this to
    ``Optional[Tuple[str, ...]]`` so one server can multiplex many
    exposes; the field is named in singular form today and migrates
    cleanly.
    """

    contract_path: Optional[Path] = None
    """Absolute path to the FLUID contract this server was started
    against.

    Used for audit logging and for the ``describe`` tool's
    ``contract_path`` reference. The path is resolved at startup, so
    a working-directory change later does not invalidate it.
    """

    # ------------------------------------------------------------------
    # NEW in v0.7.4 — agentPolicy runtime enforcement (model + use-case
    # gates). These fields make the previously dead-code helpers in
    # :mod:`fluid_build.policy.agent_policy` load-bearing: the gateway
    # populates them from ``expose.policy.agentPolicy`` (with optional
    # CLI overrides) and ``check_tool_call`` evaluates every request.
    # ------------------------------------------------------------------

    allowed_models: Optional[Tuple[str, ...]] = None
    """Allowlist of caller model ids; ``None`` means no allowlist."""

    denied_models: Tuple[str, ...] = ()
    """Denylist of caller model ids. Evaluated before allowed_models so
    denial wins even when a model also appears in the allowlist."""

    allowed_use_cases: Optional[Tuple[str, ...]] = None
    """Allowlist of declared use cases; ``None`` means no allowlist."""

    denied_use_cases: Tuple[str, ...] = ()
    """Denylist of declared use cases. Evaluated before
    allowed_use_cases so denial wins."""

    policy_source: str = "default"
    """Where the policy came from — ``contract``, ``cli``, or
    ``default``. Surfaced on audit events so operators can tell
    whether the runtime gate reflected the contract author's intent
    or an operational override."""

    def is_tool_allowed(self, tool: str) -> bool:
        """True iff ``tool`` is currently dispatchable by this policy."""
        if tool in self.denied_tools:
            return False
        if self.allowed_tools is None:
            return True
        return tool in self.allowed_tools

    def is_model_allowed(self, model_id: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Evaluate the model gate. Returns ``(allowed, deny_reason)``.

        Fail-closed when ``model_id`` is missing AND the contract (or an
        operator flag) declared something to enforce — without identity
        the gate cannot make a decision and silently allowing would
        defeat the agentPolicy contract.

        When NEITHER an allowlist nor a denylist exists the gate is inert
        and a missing identity passes, mirroring
        :meth:`is_use_case_allowed`. Model identity is not part of the MCP
        spec: the ``initialize`` request's ``Implementation`` object
        carries ``{name, version}`` only, and the gateway reads the model
        from a NON-STANDARD ``model`` field the gateway itself invented.
        Denying unconditionally therefore refused every call — ``describe``
        included — from every spec-compliant client (Claude Code, Cursor,
        the MCP Inspector the docs tell you to use), on contracts with no
        ``agentPolicy`` block at all, while ``doctor`` and ``tools/list``
        still reported the server healthy. A contract that declares a
        model allowlist or denylist still hard-denies an unidentified
        caller: the denylist is included in that test because otherwise a
        denied model would slip the gate simply by omitting the field.
        """
        if not model_id:
            if self.allowed_models is None and not self.denied_models:
                return True, None
            return False, "missing-model-identity"
        if model_id in self.denied_models:
            return False, "in-deniedModels"
        if self.allowed_models is not None and model_id not in self.allowed_models:
            return False, "not-in-allowedModels"
        return True, None

    def is_use_case_allowed(self, use_case: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Evaluate the use-case gate. Returns ``(allowed, deny_reason)``.

        Use-case is optional in the protocol; when absent we let the
        request through (model gate is the harder constraint).
        Operators who want a strict use-case check should set
        ``allowed_use_cases`` to a non-empty tuple AND configure
        clients to declare a use case at ``initialize``.
        """
        if not use_case:
            # No declared use-case + no allowlist = pass; but if an
            # allowlist exists, missing use-case is a hard deny so
            # the operator can't accidentally bypass the gate.
            if self.allowed_use_cases is not None:
                return False, "missing-use-case-with-allowlist"
            return True, None
        if use_case in self.denied_use_cases:
            return False, "in-deniedUseCases"
        if self.allowed_use_cases is not None and use_case not in self.allowed_use_cases:
            return False, "not-in-allowedUseCases"
        return True, None

    def check_tool_call(
        self,
        *,
        tool: str,
        model_id: Optional[str],
        use_case: Optional[str],
    ) -> Tuple[bool, Optional[str]]:
        """Composite gate evaluated on every ``tools/call`` request.

        Precedence (first deny wins): tool denylist > tool allowlist
        > model denylist > use-case denylist > model allowlist >
        use-case allowlist. Tool gating is checked first because the
        toolset is bounded; model/use-case gates exist to enforce
        agentPolicy on top of an already-allowed tool surface.
        """
        if not self.is_tool_allowed(tool):
            return False, "tool-not-allowed"
        ok, reason = self.is_model_allowed(model_id)
        if not ok:
            return False, reason
        ok, reason = self.is_use_case_allowed(use_case)
        if not ok:
            return False, reason
        return True, None

    @classmethod
    def from_contract_and_flags(
        cls,
        *,
        expose: Mapping[str, Any],
        contract_path: Optional[Path] = None,
        # Existing tool-level overrides (preserved verbatim from the
        # cherry-picked CLI surface)
        read_only: bool = True,
        allowed_tools: Optional[Tuple[str, ...]] = None,
        denied_tools: Tuple[str, ...] = (),
        readable_paths: Optional[Tuple[Path, ...]] = None,
        allow_free_form_sql: bool = False,
        max_sample_rows: int = 100,
        # NEW in v0.7.4 — model + use-case overrides. CLI values
        # win over contract values when both are set; the policy_source
        # field records which won.
        cli_allowed_models: Optional[Tuple[str, ...]] = None,
        cli_denied_models: Optional[Tuple[str, ...]] = None,
        cli_allowed_use_cases: Optional[Tuple[str, ...]] = None,
        cli_denied_use_cases: Optional[Tuple[str, ...]] = None,
    ) -> "OutputPortPolicy":
        """Build a policy from an expose's ``agentPolicy`` block,
        optionally overridden by CLI flags.

        Reads ``expose.policy.agentPolicy`` for the four model/
        use-case fields. CLI flags, if provided, replace the contract
        values entirely (not merged) — the operator's override is
        meant to be intentional.
        """
        agent_policy = (expose.get("policy") or {}).get("agentPolicy") or {}

        contract_allowed_models = _maybe_tuple(agent_policy.get("allowedModels"))
        contract_denied_models = _coerce_tuple(agent_policy.get("deniedModels"))
        contract_allowed_use_cases = _maybe_tuple(agent_policy.get("allowedUseCases"))
        contract_denied_use_cases = _coerce_tuple(agent_policy.get("deniedUseCases"))

        used_cli = any(
            v is not None
            for v in (
                cli_allowed_models,
                cli_denied_models,
                cli_allowed_use_cases,
                cli_denied_use_cases,
            )
        )
        used_contract = bool(agent_policy)
        if used_cli:
            policy_source = "cli"
        elif used_contract:
            policy_source = "contract"
        else:
            policy_source = "default"

        if readable_paths is None:
            readable_paths = (Path.cwd().resolve(),)

        expose_id = expose.get("exposeId")
        return cls(
            read_only=read_only,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            readable_paths=readable_paths,
            allow_free_form_sql=allow_free_form_sql,
            max_sample_rows=max_sample_rows,
            expose_id=expose_id if isinstance(expose_id, str) else None,
            contract_path=contract_path,
            allowed_models=(
                cli_allowed_models if cli_allowed_models is not None else contract_allowed_models
            ),
            denied_models=(
                cli_denied_models if cli_denied_models is not None else contract_denied_models
            ),
            allowed_use_cases=(
                cli_allowed_use_cases
                if cli_allowed_use_cases is not None
                else contract_allowed_use_cases
            ),
            denied_use_cases=(
                cli_denied_use_cases
                if cli_denied_use_cases is not None
                else contract_denied_use_cases
            ),
            policy_source=policy_source,
        )


def _maybe_tuple(value: Any) -> Optional[Tuple[str, ...]]:
    """Convert a list-or-None to a tuple-or-None, preserving the
    None vs empty distinction (None = no allowlist; () = empty
    allowlist that denies all)."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return None


def _coerce_tuple(value: Any) -> Tuple[str, ...]:
    """Convert a list to a tuple; treat None and non-lists as empty."""
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


# Cross-check: keep the OutputPortPolicy gates symmetric with the
# free-standing helpers in fluid_build.policy.agent_policy. We import
# them above to make the linkage visible to grep — the runtime can
# delegate to either form interchangeably and any future change to
# the helpers' precedence ripples here automatically.
assert _is_model_allowed is not None
assert _is_use_case_allowed is not None
