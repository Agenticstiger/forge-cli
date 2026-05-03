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

"""``fluid ai test`` smoke-call implementation — physical extraction.

Lifted from ``cli/ai_setup.py`` (host file was 1608 LOC). ~500 LOC
of AI-test plumbing: config resolution, endpoint validation, token
caps, model preflight, smoke call, error classification, JSON report
emission. Resolves shared constants and host-module symbols
(``BUILTIN_LLM_PROVIDERS``, ``LlmConfig``, ``CopilotGenerationError``,
``RICH_AVAILABLE``, etc.) via the ``_host()`` indirection so test
patches on ``fluid_build.cli.ai_setup.<helper>`` flow through.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import httpx

LOG = logging.getLogger("fluid.cli.ai_setup.test")


# ── Indirection accessors ───────────────────────────────────────────────


def _host():
    """Return the canonical ``cli.ai_setup`` module."""
    from fluid_build.cli import ai_setup as _as

    return _as


# Host-function indirection: tests patch these via
# ``fluid_build.cli.ai_setup.<helper>``. We resolve every host-side
# helper through the host module at call time so the patches flow
# through. Each shim falls back to its own implementation in this
# module when the host module hasn't (yet) re-exported it — that
# makes ``_resolve_ai_test_config`` etc. self-routing during the
# bootstrapping window.
def _load_ai_config(*args, **kwargs):
    return _host()._load_ai_config(*args, **kwargs)


def _load_key_from_keyring(*args, **kwargs):
    return _host()._load_key_from_keyring(*args, **kwargs)


def _resolve_cloud_api_key(*args, **kwargs):
    return _host()._resolve_cloud_api_key(*args, **kwargs)


def _make_cloud_config(*args, **kwargs):
    return _host()._make_cloud_config(*args, **kwargs)


# Module-internal indirection: tests patch
# ``fluid_build.cli.ai_setup._resolve_ai_test_config`` etc. — those
# names point back at THIS module via the host's re-export, so we
# resolve through the host on every call to flow the patch through.
def _via_host(name: str):
    """Return the host-module attribute by name when it differs from
    the local one (because tests patched it). Otherwise None."""
    host_attr = getattr(_host(), name, None)
    here_attr = globals().get(name)
    if host_attr is not None and host_attr is not here_attr:
        return host_attr
    return None


def _bind_constants_from_host() -> None:
    """Pull constants and host classes from the host module into this
    module's globals so bare-name references inside the extracted
    functions resolve via ``LOAD_GLOBAL`` (which bypasses PEP 562's
    ``__getattr__``).

    Called once at module import. The bound objects are stable
    references (constants, classes) — the host doesn't replace them
    after definition.
    """
    host = _host()
    g = globals()
    for name in (
        # Constants
        "_AI_TEST_DEFAULT_TIMEOUT_SECONDS",
        "_AI_TEST_DEFAULT_OUTPUT_TOKENS",
        "_AI_TEST_GEMINI_OUTPUT_TOKENS",
        "_AI_TEST_DISPLAY_LIMIT",
        "_AI_TEST_AUTH_STATUSES",
        "_AI_TEST_EXIT_AUTH",
        "_AI_TEST_EXIT_CONFIG",
        "_AI_TEST_EXIT_NETWORK",
        "_AI_TEST_EXIT_OK",
        "_AI_TEST_EXIT_RESOURCE",
        "_AI_TEST_REPORT_VERSION",
        "_AI_TEST_SYSTEM_PROMPT",
        "_AI_TEST_USER_PROMPT",
        # Host classes / dicts referenced by bare name in extracted bodies.
        "BUILTIN_LLM_PROVIDERS",
        "PROVIDER_DISPLAY_NAMES",
        "PROVIDER_ENV_VARS",
        "LlmConfig",
        "CopilotGenerationError",
        "RICH_AVAILABLE",
        "Panel",
        "Table",
        "cprint",
    ):
        if hasattr(host, name):
            g[name] = getattr(host, name)


_bind_constants_from_host()


def __getattr__(name: str):
    """Expose host-module symbols (``BUILTIN_LLM_PROVIDERS``,
    ``LlmConfig``, ``CopilotGenerationError``, ``RICH_AVAILABLE``,
    ``Panel``, ``Table``, ``cprint``, AI test constants) via lazy
    attribute access — one resolution path for every host symbol.
    """
    forwarded = {
        "BUILTIN_LLM_PROVIDERS",
        "LlmConfig",
        "CopilotGenerationError",
        "RICH_AVAILABLE",
        "Panel",
        "Table",
        "cprint",
        "_AI_TEST_DEFAULT_TIMEOUT_SECONDS",
        "_AI_TEST_DEFAULT_OUTPUT_TOKENS",
        "_AI_TEST_GEMINI_OUTPUT_TOKENS",
        "_AI_TEST_DISPLAY_LIMIT",
        "_AI_TEST_AUTH_STATUSES",
        "_AI_TEST_EXIT_AUTH",
        "_AI_TEST_EXIT_CONFIG",
        "_AI_TEST_EXIT_NETWORK",
        "_AI_TEST_EXIT_OK",
        "_AI_TEST_EXIT_RESOURCE",
        "_AI_TEST_REPORT_VERSION",
        "_AI_TEST_SYSTEM_PROMPT",
        "_AI_TEST_USER_PROMPT",
        "_resolve_cloud_api_key",
    }
    if name in forwarded:
        return getattr(_host(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _coerce_ai_test_timeout(raw: Any) -> int:
    if raw in (None, ""):
        return _AI_TEST_DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _AI_TEST_DEFAULT_TIMEOUT_SECONDS
    return max(1, min(3600, value))


def _normalize_ai_test_provider(value: Any) -> str:
    provider = str(value or "").strip().lower().replace("-", "_")
    return "anthropic" if provider == "claude" else provider


def _safe_ai_test_display(value: Any, *, limit: Optional[int] = None) -> str:
    # Default arg evaluated at function-def time can't see ``__getattr__``;
    # resolve from the host module at call time instead.
    if limit is None:
        limit = _host()._AI_TEST_DISPLAY_LIMIT
    text = str(value or "")
    clean = "".join(ch if ch.isprintable() and ch != "\x1b" else "?" for ch in text)
    if len(clean) > limit:
        return clean[: limit - 1] + "…"
    return clean


def _http_error_name(exc: httpx.HTTPError) -> str:
    return exc.__class__.__name__


def _validate_ai_test_endpoint(provider_name: str, endpoint: str) -> Optional[str]:
    raw = str(endpoint or "")
    parsed = urlsplit(raw)
    if parsed.username or parsed.password:
        return "AI test endpoint URLs must not embed credentials."
    # litellm-routed providers (the default) use a ``litellm://`` sentinel
    # that's not a real URL — litellm owns auth + transport, so there's no
    # plaintext-credential risk to validate against. Accept the sentinel.
    if parsed.scheme == "litellm":
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "AI test endpoint must be an absolute HTTP(S) URL."
    host = (parsed.hostname or "").lower()
    if provider_name == "ollama":
        if parsed.scheme != "http" or host not in {"localhost", "127.0.0.1", "::1"}:
            return "Ollama AI test endpoints must stay on localhost."
        return None
    if parsed.scheme != "https":
        return "Cloud AI test endpoints must use HTTPS to avoid sending API keys over plaintext."
    return None


def _resolve_cloud_api_key(provider: str, saved: Optional[dict] = None) -> Optional[str]:
    if os.environ.get("FLUID_LLM_API_KEY"):
        return os.environ["FLUID_LLM_API_KEY"]
    if provider == "gemini":
        for env_var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            if os.environ.get(env_var):
                return os.environ[env_var]
    env_var = PROVIDER_ENV_VARS.get(provider)
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    if saved and saved.get("api_key"):
        return str(saved["api_key"])
    key = _load_key_from_keyring(provider)
    if key:
        return key
    try:
        from fluid_build.cli.forge_copilot_llm_providers import _resolve_api_key

        return _resolve_api_key(provider, {})
    except Exception:  # noqa: BLE001 - best-effort compatibility with the Forge keyring namespace
        LOG.debug("Could not read Forge LLM keyring namespace for provider=%s", provider)
        return None


def _resolve_ai_test_config(args: Any) -> tuple[Optional[LlmConfig], Optional[str]]:
    provider_override = _normalize_ai_test_provider(getattr(args, "llm_provider", None))
    model_override = getattr(args, "llm_model", None) or os.environ.get("FLUID_LLM_MODEL")
    endpoint_override = getattr(args, "llm_endpoint", None) or os.environ.get("FLUID_LLM_ENDPOINT")
    timeout = _coerce_ai_test_timeout(
        getattr(args, "llm_timeout_seconds", None) or os.environ.get("FLUID_LLM_TIMEOUT_SECONDS")
    )
    saved = _load_ai_config() or {}

    def _build_for_provider(
        pname: str, *, allow_saved: bool = True
    ) -> tuple[Optional[LlmConfig], Optional[str]]:
        provider = BUILTIN_LLM_PROVIDERS.get(pname)
        if not provider:
            return None, f"Unsupported AI provider '{pname}'."
        saved_for_provider = saved if allow_saved and saved.get("provider") == pname else {}
        model = model_override or saved_for_provider.get("model") or provider.default_model
        if pname == "ollama":
            host = saved_for_provider.get("ollama_host")
            if host and not os.environ.get("OLLAMA_HOST"):
                os.environ["OLLAMA_HOST"] = _sanitize_ollama_host(str(host))
            endpoint = endpoint_override or provider.default_endpoint(model, dict(os.environ))
            endpoint_error = _validate_ai_test_endpoint(pname, endpoint)
            if endpoint_error:
                return None, endpoint_error
            return (
                LlmConfig(
                    provider=pname,
                    model=model,
                    endpoint=endpoint,
                    api_key=None,
                    timeout_seconds=timeout,
                ),
                None,
            )
        api_key = _resolve_cloud_api_key(pname, saved_for_provider)
        if not api_key:
            return None, f"No API key found for {PROVIDER_DISPLAY_NAMES.get(pname, pname)}."
        endpoint = endpoint_override or saved_for_provider.get("endpoint")
        config = _make_cloud_config(pname, api_key, model=model, endpoint=endpoint)
        endpoint_error = _validate_ai_test_endpoint(pname, config.endpoint)
        if endpoint_error:
            return None, endpoint_error
        return (
            config,
            None,
        )

    if provider_override:
        config, error = _build_for_provider(provider_override)
        if config:
            config.timeout_seconds = timeout
        return config, error

    if saved.get("provider"):
        config, error = _build_for_provider(str(saved["provider"]))
        if config:
            config.timeout_seconds = timeout
            return config, None
        LOG.debug("Saved AI config was not test-ready: %s", error)

    for pname, env_var in PROVIDER_ENV_VARS.items():
        if os.environ.get(env_var) or (pname == "gemini" and os.environ.get("GEMINI_API_KEY")):
            config, error = _build_for_provider(pname, allow_saved=False)
            if config:
                config.timeout_seconds = timeout
            return config, error

    if os.environ.get("OLLAMA_HOST") or detect_ollama_available(os.environ):
        config, error = _build_for_provider("ollama", allow_saved=False)
        if config:
            config.timeout_seconds = timeout
        return config, error

    return None, "No AI provider configured. Run 'fluid ai setup' or set a provider API key."


def _cap_ai_test_token_budget(config: LlmConfig, payload: dict) -> None:
    provider_name = config.provider
    if provider_name in {"openai", "ollama"}:
        payload["max_tokens"] = _AI_TEST_DEFAULT_OUTPUT_TOKENS
        return
    if provider_name == "anthropic":
        payload["max_tokens"] = min(
            int(payload.get("max_tokens") or _AI_TEST_DEFAULT_OUTPUT_TOKENS),
            _AI_TEST_DEFAULT_OUTPUT_TOKENS,
        )
        return
    if provider_name == "gemini":
        generation_config = payload.setdefault("generationConfig", {})
        if not isinstance(generation_config, dict):
            return
        generation_config["maxOutputTokens"] = _AI_TEST_GEMINI_OUTPUT_TOKENS


def _ai_test_token_budget_label(config: LlmConfig) -> str:
    if config.provider == "gemini":
        return f"{_AI_TEST_GEMINI_OUTPUT_TOKENS} tokens"
    return f"{_AI_TEST_DEFAULT_OUTPUT_TOKENS} tokens"


def _with_freeform_ai_test_payload(provider: Any, config: LlmConfig) -> tuple[dict, dict]:
    old_value = os.environ.get("FLUID_LLM_STRUCTURED_OUTPUTS")
    had_value = "FLUID_LLM_STRUCTURED_OUTPUTS" in os.environ
    os.environ["FLUID_LLM_STRUCTURED_OUTPUTS"] = "0"
    try:
        headers, payload = provider.build_request(
            config, _AI_TEST_SYSTEM_PROMPT, _AI_TEST_USER_PROMPT
        )
    finally:
        if had_value:
            os.environ["FLUID_LLM_STRUCTURED_OUTPUTS"] = old_value or ""
        else:
            os.environ.pop("FLUID_LLM_STRUCTURED_OUTPUTS", None)
    payload = dict(payload)
    _cap_ai_test_token_budget(config, payload)
    return headers, payload


def _check_ai_test_model_availability(
    provider: Any, config: LlmConfig
) -> tuple[str, Optional[list[str]]]:
    try:
        available = provider.list_available_models(config.api_key, dict(os.environ))
    except httpx.HTTPStatusError as exc:
        raise CopilotGenerationError(
            "ai_test_model_preflight_failed",
            f"Could not list {config.provider} models ({exc.response.status_code}).",
            suggestions=[
                "Check the provider API key and account permissions",
                "Try again after confirming network access to the provider",
            ],
        ) from exc
    except httpx.HTTPError as exc:
        raise CopilotGenerationError(
            "ai_test_model_preflight_failed",
            f"Could not list {config.provider} models ({_http_error_name(exc)}).",
            suggestions=[
                "Check network connectivity",
                "For Ollama, start the local Ollama server",
            ],
        ) from exc
    if available is None:
        return "unavailable", None
    if config.model not in available:
        available_preview = ", ".join(_safe_ai_test_display(model) for model in available[:5])
        raise CopilotGenerationError(
            "ai_test_model_unavailable",
            f"Configured {config.provider} model "
            f"'{_safe_ai_test_display(config.model)}' was not returned by the provider.",
            suggestions=[
                f"Available models include: {available_preview or '(none)'}",
                "Run `fluid ai models` to inspect bundled defaults",
                "Re-run `fluid ai setup` or pass `fluid ai test --model <model>`",
            ],
        )
    return "available", available


def _run_ai_smoke_call(provider: Any, config: LlmConfig) -> tuple[str, dict, int]:
    """Issue the diagnostic call. Returns (text, usage, latency_ms)."""
    headers, payload = _with_freeform_ai_test_payload(provider, config)
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=config.timeout_seconds) as client:
            response = client.post(config.endpoint, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise CopilotGenerationError(
            "ai_test_request_failed",
            f"AI test request failed ({exc.response.status_code}) for {config.provider}.",
            suggestions=[
                "Check the selected model and endpoint",
                "Verify the API key has access to the configured model",
            ],
        ) from exc
    except httpx.HTTPError as exc:
        raise CopilotGenerationError(
            "ai_test_network_error",
            f"AI test network error for {config.provider} ({_http_error_name(exc)}).",
            suggestions=[
                "Check network connectivity",
                "For Ollama, start the local Ollama server",
            ],
        ) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)

    try:
        response_json = response.json()
    except ValueError as exc:
        raise CopilotGenerationError(
            "ai_test_response_invalid",
            f"AI test response from {config.provider} could not be parsed as JSON.",
            suggestions=["Try a different model or re-run `fluid ai setup`."],
        ) from exc
    try:
        text = provider.extract_text(response_json).strip()
        usage = provider.extract_usage(response_json)
    except Exception as exc:  # noqa: BLE001 — provider adapters raise heterogeneous errors
        raise CopilotGenerationError(
            "ai_test_response_invalid",
            f"AI test response from {config.provider} did not match the expected schema.",
            suggestions=["Try a different model or re-run `fluid ai setup`."],
        ) from exc
    if "FLUID_OK" not in text:
        raise CopilotGenerationError(
            "ai_test_unexpected_response",
            f"AI test response from {config.provider} was not the expected diagnostic token.",
            suggestions=["Try again, or choose a different model with `fluid ai setup`."],
        )
    return "FLUID_OK", usage, latency_ms


# Map (error_code, http_status) → exit code. Status is None when there's no HTTP response.
_AI_TEST_AUTH_STATUSES = frozenset({401, 403, 407})


def _classify_ai_test_error(error_code: str, status_code: Optional[int]) -> int:
    """Return the parseable exit code for a CopilotGenerationError.

    0 OK / 2 config / 3 auth / 4 resource / 5 network. See AGENTS.md for the contract.
    """
    if error_code in {"ai_test_request_failed", "ai_test_model_preflight_failed"}:
        if status_code is None:
            return _AI_TEST_EXIT_NETWORK
        if status_code in _AI_TEST_AUTH_STATUSES:
            return _AI_TEST_EXIT_AUTH
        if status_code == 429 or status_code >= 500:
            return _AI_TEST_EXIT_NETWORK
        return _AI_TEST_EXIT_RESOURCE
    if error_code == "ai_test_network_error":
        return _AI_TEST_EXIT_NETWORK
    return _AI_TEST_EXIT_RESOURCE


def _http_status_from_cause(exc: CopilotGenerationError) -> Optional[int]:
    cause = exc.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        return cause.response.status_code
    return None


def _new_ai_test_report(
    *,
    ok: bool,
    provider: Optional[str],
    model: Optional[str],
    endpoint: Optional[str],
    availability: Optional[str],
    live_call: Optional[str],
    usage: Optional[dict],
    output_cap: Optional[str],
    latency_ms: Optional[int],
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    error_suggestions: Optional[list] = None,
    exit_code: int = _AI_TEST_EXIT_OK,
) -> dict:
    """Build the stable JSON-output schema. Bumps `_AI_TEST_REPORT_VERSION` on changes."""
    return {
        "schema_version": _AI_TEST_REPORT_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ok": ok,
        "exit_code": exit_code,
        "provider": provider,
        "model": _safe_ai_test_display(model) if model else None,
        "endpoint": _safe_ai_test_display(endpoint) if endpoint else None,
        "model_availability": availability,
        "live_call": live_call,
        "usage": usage,
        "output_cap": output_cap,
        "latency_ms": latency_ms,
        "error": (
            {
                "code": error_code,
                "message": error_message,
                "suggestions": error_suggestions or [],
            }
            if error_code
            else None
        ),
    }


def _resolve_test_call_targets():
    """Resolve the four functions ``run_ai_test`` orchestrates via
    the host module (``fluid_build.cli.ai_setup``) so test patches on
    those host-namespace names flow through. Falls back to this
    module's implementations when the host hasn't (yet) re-bound."""
    host = _host()
    return (
        getattr(host, "_resolve_ai_test_config", _resolve_ai_test_config),
        getattr(host, "_check_ai_test_model_availability", _check_ai_test_model_availability),
        getattr(host, "_run_ai_smoke_call", _run_ai_smoke_call),
        getattr(host, "_new_ai_test_report", _new_ai_test_report),
    )


def run_ai_test(console: Any, args: Any) -> tuple[int, dict]:
    """Run a configured-provider connectivity test.

    Returns ``(exit_code, report)`` where ``exit_code`` is one of
    ``_AI_TEST_EXIT_OK / _CONFIG / _AUTH / _RESOURCE / _NETWORK`` and
    ``report`` is the JSON-output schema dict (see ``_new_ai_test_report``).
    """
    as_json = bool(getattr(args, "json", False))
    (
        _resolve_ai_test_config_fn,
        _check_ai_test_model_availability_fn,
        _run_ai_smoke_call_fn,
        _new_ai_test_report_fn,
    ) = _resolve_test_call_targets()

    config, config_error = _resolve_ai_test_config_fn(args)
    if not config:
        message = config_error or "AI provider is not configured."
        report = _new_ai_test_report_fn(
            ok=False,
            provider=None,
            model=None,
            endpoint=None,
            availability=None,
            live_call=None,
            usage=None,
            output_cap=None,
            latency_ms=None,
            error_code="ai_test_no_provider",
            error_message=message,
            error_suggestions=["Run `fluid ai setup` to configure a provider."],
            exit_code=_AI_TEST_EXIT_CONFIG,
        )
        if as_json:
            sys.stdout.write(json.dumps(report, indent=2) + "\n")
        elif console and RICH_AVAILABLE:
            console.print(
                Panel(
                    f"[yellow]{message}[/yellow]\n\n"
                    "Run [bold cyan]fluid ai setup[/bold cyan] to configure a provider.",
                    title="AI Provider Test",
                    border_style="yellow",
                )
            )
        else:
            from fluid_build.cli.console import cprint

            cprint(f"AI Provider Test: {message}")
        return _AI_TEST_EXIT_CONFIG, report

    provider = BUILTIN_LLM_PROVIDERS[config.provider]
    label = PROVIDER_DISPLAY_NAMES.get(config.provider, config.provider)
    endpoint = config.redacted_endpoint
    output_cap = _ai_test_token_budget_label(config)
    try:
        availability, available_models = _check_ai_test_model_availability_fn(provider, config)
        text, usage, latency_ms = _run_ai_smoke_call_fn(provider, config)
    except CopilotGenerationError as exc:
        exit_code = _classify_ai_test_error(exc.event, _http_status_from_cause(exc))
        report = _new_ai_test_report_fn(
            ok=False,
            provider=config.provider,
            model=config.model,
            endpoint=endpoint,
            availability=None,
            live_call=None,
            usage=None,
            output_cap=output_cap,
            latency_ms=None,
            error_code=exc.event,
            error_message=exc.message,
            error_suggestions=list(exc.suggestions or []),
            exit_code=exit_code,
        )
        if as_json:
            sys.stdout.write(json.dumps(report, indent=2) + "\n")
        elif console and RICH_AVAILABLE:
            suggestions = "\n".join(f"- {s}" for s in exc.suggestions)
            body = f"[red]{exc.message}[/red]"
            if suggestions:
                body += f"\n\n[dim]{suggestions}[/dim]"
            console.print(Panel(body, title="AI Provider Test Failed", border_style="red"))
        else:
            from fluid_build.cli.console import cprint

            cprint(f"AI Provider Test Failed: {exc.message}")
        return exit_code, report

    report = _new_ai_test_report_fn(
        ok=True,
        provider=config.provider,
        model=config.model,
        endpoint=endpoint,
        availability=availability,
        live_call=text,
        usage={
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        },
        output_cap=output_cap,
        latency_ms=latency_ms,
        exit_code=_AI_TEST_EXIT_OK,
    )

    if as_json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return _AI_TEST_EXIT_OK, report

    usage_text = (
        f"{report['usage']['input_tokens']} input / "
        f"{report['usage']['output_tokens']} output / "
        f"{report['usage']['total_tokens']} total"
    )
    latency_text = f"{latency_ms} ms"
    if console and RICH_AVAILABLE:
        table = Table(title="AI Provider Test", border_style="green")
        table.add_column("Check", style="cyan")
        table.add_column("Result", style="green")
        table.add_row("Provider", label)
        table.add_row("Model", _safe_ai_test_display(config.model))
        table.add_row("Endpoint", _safe_ai_test_display(endpoint))
        table.add_row(
            "Model availability",
            (
                "available"
                if availability == "available"
                else "not supported by this provider adapter"
            ),
        )
        table.add_row("Live call", text)
        table.add_row("Latency", latency_text)
        table.add_row("Token usage", usage_text)
        table.add_row("Output cap", output_cap)
        if available_models:
            table.caption = f"Provider returned {len(available_models)} available model(s)."
        console.print(table)
    else:
        from fluid_build.cli.console import cprint

        cprint(f"AI Provider Test: ready ({label}, {_safe_ai_test_display(config.model)})")
        cprint(f"  Endpoint: {_safe_ai_test_display(endpoint)}")
        cprint(f"  Model availability: {availability}")
        cprint(f"  Live call: {text}")
        cprint(f"  Latency: {latency_text}")
        cprint(f"  Token usage: {usage_text}; output cap: {output_cap}")
    return _AI_TEST_EXIT_OK, report


# ---------------------------------------------------------------------------
# CLI registration -- ``fluid ai setup``
# ---------------------------------------------------------------------------
