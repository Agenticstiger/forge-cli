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

"""Coding-agent provider — delegate forge's LLM call to a local agent CLI.

When forge runs in a plain terminal (no MCP sampling channel), it can still
author *without an API key of its own* by shelling out to an AI coding-agent
CLI the user already has installed:

* **Claude Code** — ``claude -p`` (truly keyless: the user's subscription
  OAuth pays; forge never sees a key)
* **OpenAI Codex** — ``codex exec`` (reuses ``CODEX_API_KEY``)
* **Cursor** — ``cursor-agent -p`` (reuses ``CURSOR_API_KEY``)
* **Amazon Kiro** — ``kiro --no-interactive`` (reuses ``KIRO_API_KEY``)

This is the standalone-terminal sibling of
:class:`~fluid_build.cli.forge_copilot_llm_providers.MCPSamplingProvider`,
which serves the in-IDE case (forge driven by the host's ``forge_run`` tool).
Both reuse the same :class:`LlmProvider` seam, so selection is purely by
provider name (``claude-code``/``codex``/``cursor``/``kiro``) — there is no
separate backend switch.

Drive mode here is **envelope**: the agent is asked to emit forge's response
JSON envelope on stdout — schema-constrained where the CLI supports it
(Claude's ``--json-schema``, Codex's ``--output-schema``), or instruction +
self-healing repair otherwise — which the runtime then parses, validates, and
repairs through the existing
:func:`~fluid_build.cli.forge_copilot_runtime.generate_copilot_artifacts`
loop, unchanged. (The opt-in *agentic* mode, where the agent writes
``contract.fluid.yaml`` into the workspace with its own tools, is a separate
entry point built on the same per-agent argv table.)

Transport-abstracted by design: :func:`_run_agent` is the only subprocess
seam, so an ACP (Agent Client Protocol) transport can drop in later without
touching prompt composition or output parsing. ``_run_agent`` is also the
single monkeypatch point for unit tests — no real CLI is ever spawned in CI.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fluid_build.cli.forge_copilot_llm_providers import (
    CopilotGenerationError,
    LlmConfig,
    LlmProvider,
    _cumulative_usage,
)

LOG = logging.getLogger(__name__)

# Prompts above this size go via stdin instead of argv to stay under ARG_MAX
# (~256 KB total argv+env on macOS). Claude already uses stdin; the argv agents
# (codex/cursor/kiro) accept the final prompt positionally and fall back to
# stdin only when oversized.
_ARGV_MAX_CHARS = 100_000
# Delimiter between the system and user prompt for agents with no system-prompt
# flag — we fold both into a single prompt string.
_PROMPT_DELIM = "\n\n---\n\n"
# Strip ANSI escape sequences from agent stdout. Verified live: kiro-cli
# decorates its output with colour/cursor codes (e.g. ``\x1b[38;5;141m> ``)
# that would otherwise corrupt JSON extraction.
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


# ---------------------------------------------------------------------------
# Per-call cost — mirrors forge_copilot_llm_litellm's thread-local so the
# call_llm cost bridge can pull an accurate usd_override for Claude Code.
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def _reset_agent_cost_state() -> None:
    _thread_local.last_cost_usd = None


def _set_agent_cost(usd: Optional[float]) -> None:
    _thread_local.last_cost_usd = usd


def get_last_agent_cost_usd() -> Optional[float]:
    """USD cost of the most recent coding-agent call on this thread, if known.

    Claude Code's ``--output-format json`` reports ``total_cost_usd``; the
    other agents don't surface cost, so this stays ``None`` (the cost summary
    shows them as metered-by-the-agent rather than a misleading ``$0``).
    """
    return getattr(_thread_local, "last_cost_usd", None)


# ---------------------------------------------------------------------------
# Per-agent specification — one row per agent, the only thing that differs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """How to drive one agent's headless CLI in envelope mode."""

    name: str  # canonical provider name, e.g. "claude-code"
    binary: str  # CLI binary resolved via shutil.which, e.g. "claude"
    keyless: bool  # True only for claude-code (subscription OAuth)
    api_key_env: Optional[str]  # required env var when not keyless
    prompt_transport: str  # "stdin" | "argv"
    system_prompt_mode: str  # "flag" | "prepend"
    system_prompt_flag: str  # e.g. "--append-system-prompt" (when mode == "flag")
    schema_mode: str  # "inline" | "file" | "none"
    schema_flag: str  # "--json-schema" | "--output-schema" | ""
    base_args: Tuple[str, ...]  # subcommand + non-interactive flags
    model_flag: str  # "--model" | "-m" | "" (passthrough only when explicit)
    output_mode: str  # "claude-json" | "result-json" | "raw"
    cost_field: Optional[str]  # top-level JSON cost field, e.g. "total_cost_usd"
    auth_markers: Tuple[str, ...]  # stderr substrings -> not_authenticated
    login_hint: str  # actionable suggestion for missing-key / not-authenticated


_AGENT_SPECS: Dict[str, AgentSpec] = {
    "claude-code": AgentSpec(
        name="claude-code",
        binary="claude",
        keyless=True,
        api_key_env=None,
        prompt_transport="stdin",
        system_prompt_mode="flag",
        system_prompt_flag="--append-system-prompt",
        schema_mode="inline",
        schema_flag="--json-schema",
        # NOT --bare: bare mode skips OAuth/keychain and would demand
        # ANTHROPIC_API_KEY, defeating the keyless path.
        base_args=("-p", "--output-format", "json"),
        model_flag="--model",
        output_mode="claude-json",
        cost_field="total_cost_usd",
        auth_markers=(
            "not logged in",
            "please run /login",
            "oauth",
            "unauthorized",
            "invalid api key",
            "authentication",
        ),
        login_hint="Run `claude` once and sign in (subscription), or set ANTHROPIC_API_KEY.",
    ),
    "codex": AgentSpec(
        name="codex",
        binary="codex",
        keyless=False,
        api_key_env="CODEX_API_KEY",
        prompt_transport="argv",
        system_prompt_mode="prepend",
        system_prompt_flag="",
        schema_mode="file",
        schema_flag="--output-schema",
        base_args=("exec",),
        model_flag="--model",
        output_mode="raw",
        cost_field=None,
        auth_markers=(
            "not logged in",
            "unauthorized",
            "api key",
            "authentication",
            "401",
            "403",
        ),
        login_hint="Set CODEX_API_KEY — `codex exec` uses API-key auth in headless mode.",
    ),
    "cursor": AgentSpec(
        name="cursor",
        binary="cursor-agent",
        keyless=False,
        api_key_env="CURSOR_API_KEY",
        prompt_transport="argv",
        system_prompt_mode="prepend",
        system_prompt_flag="",
        schema_mode="none",
        schema_flag="",
        base_args=("-p", "--output-format", "json"),
        model_flag="--model",
        output_mode="result-json",
        cost_field=None,
        auth_markers=(
            "not logged in",
            "unauthorized",
            "api key",
            "authentication",
            "login",
        ),
        login_hint="Run `cursor-agent login`, or set CURSOR_API_KEY.",
    ),
    "kiro": AgentSpec(
        name="kiro",
        # The standalone "Kiro CLI" product (binary ``kiro-cli``), distinct from
        # the Kiro IDE. Verified live: the headless agent is
        # ``kiro-cli chat --no-interactive "<prompt>"`` (kiro.dev/docs/cli/headless).
        binary="kiro-cli",
        keyless=False,
        api_key_env="KIRO_API_KEY",
        prompt_transport="argv",
        system_prompt_mode="prepend",
        system_prompt_flag="",
        schema_mode="none",
        schema_flag="",
        base_args=("chat", "--no-interactive"),
        model_flag="",
        output_mode="raw",
        cost_field=None,
        auth_markers=(
            "not logged in",
            "unauthorized",
            "api key",
            "authentication",
            "kiro_api_key",
        ),
        login_hint="Run `kiro-cli login`, or set KIRO_API_KEY (Kiro portal) for headless/CI.",
    ),
}

# Canonical provider names this module serves (used by the factory + the
# keyless-exemption wiring in forge_copilot_llm_providers).
CODING_AGENT_NAMES: Tuple[str, ...] = tuple(_AGENT_SPECS)


def _canonical_agent_name(name: Any) -> Optional[str]:
    """Map a user-supplied provider name to a canonical agent name, or None."""
    n = (str(name) if name is not None else "").strip().lower().replace("_", "-")
    if n in ("claude-code", "claudecode"):
        return "claude-code"
    if n == "codex":
        return "codex"
    if n in ("cursor", "cursor-agent"):
        return "cursor"
    if n == "kiro":
        return "kiro"
    return None


def is_coding_agent(name: Any) -> bool:
    """True when ``name`` selects one of the coding-agent providers."""
    return _canonical_agent_name(name) is not None


# ---------------------------------------------------------------------------
# Subprocess seam — the ONLY place a real CLI is spawned. Tests monkeypatch
# this to return canned (rc, stdout, stderr) tuples.
# ---------------------------------------------------------------------------


def _run_agent(
    argv: List[str],
    *,
    stdin: Optional[str],
    timeout: int,
    cwd: Optional[str],
    env: Dict[str, str],
) -> Tuple[int, str, str]:
    """Run an agent CLI and return ``(returncode, stdout, stderr)``.

    List-form argv only — never ``shell=True``. ``subprocess.run`` raises
    ``TimeoutExpired`` on overrun, which the caller translates to a typed
    error.
    """
    proc = subprocess.run(  # noqa: S603 — list argv, no shell, fixed binary set
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _response_schema() -> Dict[str, Any]:
    # Permissive variant (NOT the OpenAI-hardened one): nested objects keep
    # additionalProperties:true because the contract is free-form.
    from fluid_build.cli.forge_copilot_response_schema import FORGE_RESPONSE_SCHEMA

    return FORGE_RESPONSE_SCHEMA


def _write_temp_json(obj: Any) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(obj, f)
    finally:
        f.close()
    return f.name


def _agent_timeout(config: LlmConfig) -> int:
    raw = os.environ.get("FLUID_FORGE_AGENT_TIMEOUT_SECONDS")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            LOG.debug("invalid FLUID_FORGE_AGENT_TIMEOUT_SECONDS=%r — using config default", raw)
    return int(getattr(config, "timeout_seconds", 120) or 120)


def _scratch_cwd() -> str:
    """Working dir for the agent in envelope mode.

    A scratch dir keeps the agent from auto-loading the user's project
    ``CLAUDE.md`` / ``AGENTS.md`` (determinism + no context bleed). Override
    with ``FLUID_FORGE_AGENT_CWD`` when you *want* the agent to see the repo.
    """
    return os.environ.get("FLUID_FORGE_AGENT_CWD") or tempfile.gettempdir()


def _redact(text: str) -> str:
    if not text:
        return ""
    try:
        from fluid_build.observability.secret_redactor import redact_secret_text

        return redact_secret_text(text)
    except Exception:  # noqa: BLE001 — redaction must never block error surfacing
        return text


def _extract_envelope(spec: AgentSpec, stdout: str) -> str:
    """Pull the response-envelope string out of the agent's stdout wrapper.

    Returns a *string* (never pre-parsed): the runtime's ``extract_json_object``
    + repair loop owns correctness, so a non-JSON answer still flows into the
    self-healing path rather than dying here.
    """
    text = _ANSI_RE.sub("", stdout or "")
    if spec.output_mode == "claude-json":
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return text
        if isinstance(data, dict):
            structured = data.get("structured_output")
            if isinstance(structured, (dict, list)):
                return json.dumps(structured)
            if isinstance(structured, str) and structured.strip():
                return structured
            if isinstance(data.get("result"), str):
                return data["result"]
        return text
    if spec.output_mode == "result-json":
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return text
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            return data["result"]
        return text
    return text  # "raw" — stdout is the final message verbatim


def _record_agent_usage_and_cost(spec: AgentSpec, stdout: str) -> None:
    """Stash Claude's per-call USD + bump the shared usage counters.

    Only Claude Code reports machine-readable cost/usage. Others leave the
    thread-local at ``None`` and the usage counters unchanged (delta 0), which
    the cost bridge records as an unknown-cost call rather than a fake ``$0``.
    """
    if spec.output_mode != "claude-json" or not spec.cost_field:
        return
    try:
        data = json.loads(stdout or "")
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    cost = data.get(spec.cost_field)
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        _set_agent_cost(float(cost))
    usage = data.get("usage")
    if isinstance(usage, dict):
        in_t = int(usage.get("input_tokens", 0) or 0)
        out_t = int(usage.get("output_tokens", 0) or 0)
        if in_t or out_t:
            _cumulative_usage["input_tokens"] += in_t
            _cumulative_usage["output_tokens"] += out_t
            _cumulative_usage["total_tokens"] += in_t + out_t


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class CodingAgentProvider(LlmProvider):
    """Route the forge LLM call through a local coding-agent CLI (envelope mode)."""

    def __init__(self, spec: AgentSpec):
        self._spec = spec
        self.name = spec.name
        # Placeholder default — the agent uses its own configured model unless
        # the user passes --llm-model. invoke_blocking treats model == name as
        # "no explicit model" and omits the model flag.
        self.default_model = spec.name

    # -- abstract-contract satisfiers (the HTTP path never runs here) --------

    def default_endpoint(self, model: str, env: Any) -> str:
        return f"coding-agent://{self._spec.name}"

    def build_request(self, config: LlmConfig, system_prompt: str, user_prompt: str):
        return ({}, {})

    def extract_text(self, response_json: Dict[str, Any]) -> str:
        if isinstance(response_json, dict):
            return str(response_json.get("text", ""))
        return ""

    # -- the real work -------------------------------------------------------

    def invoke_blocking(
        self,
        config: LlmConfig,
        system_prompt: str,
        user_prompt: str,
        *,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        # extra_payload (litellm's response_format) is a wire directive with no
        # meaning for a subprocess — schema is conveyed via the agent's own
        # flags below — so it is intentionally ignored.
        _reset_agent_cost_state()
        # Agentic mode is plumbed (LlmConfig.agent_mode / --forge-agent-mode) but
        # the runtime doesn't yet dispatch the agent-writes-the-file loop, so the
        # envelope path runs instead. Warn loudly rather than silently no-op the
        # opt-in flag. (Wiring the agentic loop is the documented follow-up.)
        if getattr(config, "agent_mode", "envelope") == "agentic":
            LOG.warning(
                "coding_agent_agentic_not_wired: --forge-agent-mode agentic is not "
                "yet dispatched by the runtime; proceeding in envelope mode."
            )
        spec = self._spec

        binary = shutil.which(spec.binary)
        if not binary:
            raise CopilotGenerationError(
                "coding_agent_not_installed",
                f"The '{spec.binary}' CLI for {spec.name} is not on PATH.",
                suggestions=[
                    f"Install {spec.name} and make sure `{spec.binary}` is on PATH.",
                    "Or set an API key and use --llm-provider anthropic/openai/gemini,",
                    "or run a local model with --llm-provider ollama.",
                ],
            )

        # Auth = a stored interactive login OR the agent's key env — we do NOT
        # hard-require the key, because that would block an agent the user
        # logged into. Verified live: a current `kiro-cli login` session runs
        # `chat --no-interactive` fine with NO KIRO_API_KEY (it was an *expired*
        # token that triggered the browser re-auth, not a key requirement).
        # Codex/Cursor likewise accept their login session; the key is the
        # CI/headless-machine fallback. With no auth at all the CLI's own error
        # surfaces below (kiro additionally opens a browser for first-time auth,
        # bounded by the per-call timeout).
        env = dict(os.environ)

        argv: List[str] = [binary, *spec.base_args]
        # Model passthrough only when the user set an explicit, non-placeholder
        # model (placeholder == the provider/agent name).
        if spec.model_flag and config.model and config.model != spec.name:
            argv += [spec.model_flag, str(config.model)]

        combined_prompt = user_prompt
        if spec.system_prompt_mode == "flag":
            argv += [spec.system_prompt_flag, system_prompt]
        else:
            combined_prompt = f"{system_prompt}{_PROMPT_DELIM}{user_prompt}"

        tmp_paths: List[str] = []
        stdin_text: Optional[str] = None
        try:
            if spec.schema_mode == "inline":
                argv += [spec.schema_flag, json.dumps(_response_schema())]
            elif spec.schema_mode == "file":
                schema_path = _write_temp_json(_response_schema())
                tmp_paths.append(schema_path)
                argv += [spec.schema_flag, schema_path]

            if spec.prompt_transport == "stdin":
                stdin_text = combined_prompt
            elif len(combined_prompt) > _ARGV_MAX_CHARS:
                # ARG_MAX guard — hand an oversized prompt to the agent via
                # stdin rather than argv.
                stdin_text = combined_prompt
                LOG.debug("coding_agent_prompt_via_stdin: %d chars", len(combined_prompt))
            else:
                argv.append(combined_prompt)

            timeout = _agent_timeout(config)
            try:
                rc, stdout, stderr = _run_agent(
                    argv,
                    stdin=stdin_text,
                    timeout=timeout,
                    cwd=_scratch_cwd(),
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                raise CopilotGenerationError(
                    "coding_agent_timeout",
                    f"{spec.name} did not finish within {timeout}s.",
                    suggestions=[
                        "Ensure the agent is authenticated (e.g. `kiro-cli login`) or its "
                        "key env is set — an unauthenticated agent may block on interactive login.",
                        "Raise the budget with FLUID_FORGE_AGENT_TIMEOUT_SECONDS.",
                        "Simplify the request, or try a different agent / an API key.",
                    ],
                ) from exc
        finally:
            for path in tmp_paths:
                try:
                    os.unlink(path)
                except OSError:  # pragma: no cover — best-effort cleanup
                    pass

        if rc != 0:
            lowered = (stderr or "").lower()
            if any(marker in lowered for marker in spec.auth_markers):
                raise CopilotGenerationError(
                    "coding_agent_not_authenticated",
                    f"{spec.name} is installed but not authenticated.",
                    suggestions=[spec.login_hint],
                )
            raise CopilotGenerationError(
                "coding_agent_failed",
                f"{spec.name} exited with code {rc}: {_redact(stderr)[:500]}",
                suggestions=[
                    f"Run `{spec.binary}` manually to confirm it works.",
                    "Or fall back to an API key / Ollama.",
                ],
            )

        envelope = _extract_envelope(spec, stdout)
        _record_agent_usage_and_cost(spec, stdout)
        return envelope

    def invoke_streaming(
        self,
        config: LlmConfig,
        system_prompt: str,
        user_prompt: str,
        *,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> Iterator[str]:
        # Streaming is deferred: yield the blocking result as one chunk. The
        # runtime's streaming wrapper concatenates chunks, so this is
        # transparent (same pattern as MCPSamplingProvider).
        yield self.invoke_blocking(config, system_prompt, user_prompt, extra_payload=extra_payload)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDER_CACHE: Dict[str, CodingAgentProvider] = {}


def get_coding_agent_provider(name: str) -> CodingAgentProvider:
    """Resolve a :class:`CodingAgentProvider` by (possibly aliased) name."""
    canonical = _canonical_agent_name(name)
    if canonical is None:
        raise CopilotGenerationError(
            "unknown_coding_agent",
            f"Unknown coding-agent provider: {name!r}",
            suggestions=[f"Valid coding agents: {', '.join(CODING_AGENT_NAMES)}"],
        )
    cached = _PROVIDER_CACHE.get(canonical)
    if cached is None:
        cached = CodingAgentProvider(_AGENT_SPECS[canonical])
        _PROVIDER_CACHE[canonical] = cached
    return cached


__all__ = [
    "AgentSpec",
    "CODING_AGENT_NAMES",
    "CodingAgentProvider",
    "get_coding_agent_provider",
    "get_last_agent_cost_usd",
    "is_coding_agent",
]
