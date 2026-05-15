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

"""``fluid ai setup`` Ollama path — physical extraction.

Lifted from ``cli/ai_setup.py`` (host file was 1747 LOC). The
Ollama-specific helpers are coherent: model polling, model query,
the interactive setup flow, and the ``LlmConfig`` builder. ~200 LOC
together.

Resolves host-module dependencies via the
``module-attribute-access`` indirection pattern so test patches on
``fluid_build.cli.ai_setup.<helper>`` (e.g. ``_save_ai_config``,
``_sanitize_ollama_host``) flow through to this module.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

LOG = logging.getLogger("fluid.cli.ai_setup.ollama")


# ── Indirection accessors ───────────────────────────────────────────────


def _host():
    """Return the canonical ``cli.ai_setup`` module."""
    from fluid_build.cli import ai_setup as _as

    return _as


def _sanitize_ollama_host(host: str) -> str:
    return _host()._sanitize_ollama_host(host)


def _save_ai_config(*args, **kwargs):
    return _host()._save_ai_config(*args, **kwargs)


def _prompt_for_api_key_loop(*args, **kwargs):
    return _host()._prompt_for_api_key_loop(*args, **kwargs)


# ── Public functions ────────────────────────────────────────────────────


def _poll_for_ollama(
    host: str, console: Any, *, timeout_s: int = 60, interval_s: float = 2.0
) -> list:
    """Poll Ollama's ``/api/tags`` endpoint until models appear or timeout.

    Designed for the Ollama poll-or-fallback path: the user goes off
    to install + start ``ollama serve`` in another terminal; we keep
    checking. Returns the model list (possibly empty if timeout hits).
    """
    import time

    deadline = time.time() + max(1, int(timeout_s))
    spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    tick = 0
    while time.time() < deadline:
        models = _query_ollama_models(host)
        if models:
            console.print(f"[green]✓ Ollama is up — found {len(models)} model(s).[/green]")
            return models
        char = spinner_chars[tick % len(spinner_chars)]
        try:
            console.print(
                f"[dim]{char} Waiting for Ollama at {host}… "
                f"({int(deadline - time.time())}s left, Ctrl-C to give up)[/dim]"
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            time.sleep(interval_s)
        except KeyboardInterrupt:
            console.print("[yellow]Polling cancelled.[/yellow]")
            return []
        tick += 1
    console.print(f"[yellow]Polling timed out after {timeout_s}s.[/yellow]")
    return []


def _query_ollama_models(host: str) -> list:
    """Return a list of model names available on the local Ollama
    instance.

    Returns an empty list if Ollama is unreachable or has no models.
    """
    try:
        import httpx
    except ImportError:
        LOG.debug("httpx not installed — cannot query Ollama models")
        return []

    try:
        resp = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return [m["name"] for m in data.get("models", []) if m.get("name")]
    except httpx.ConnectError:
        LOG.debug("Ollama is not running at %s", host)
        return []
    except Exception:  # noqa: BLE001
        LOG.debug("Could not query Ollama models at %s", host, exc_info=True)
        return []


def _setup_ollama(console: Any) -> Optional[Any]:
    """Handle the Ollama setup path with model discovery.

    Phase 0.5 — when Ollama isn't reachable, instead of dead-ending
    with "install and try again", offer 4 paths:

    * Poll for ``ollama serve`` to come up (the user can install +
      start it without restarting the forge run)
    * Switch to a cloud provider via ``--rescue``-style picker
    * Skip AI entirely (returns to top of forge with no LLM)
    * Print install instructions and quit
    """
    from fluid_build.cli.forge_ui import ask_numbered_choice

    host_mod = _host()
    BUILTIN_LLM_PROVIDERS = host_mod.BUILTIN_LLM_PROVIDERS
    LlmConfig = host_mod.LlmConfig

    provider = BUILTIN_LLM_PROVIDERS["ollama"]
    host = _sanitize_ollama_host(os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    os.environ["OLLAMA_HOST"] = host

    available_models = _query_ollama_models(host)
    if not available_models:
        console.print(
            "[yellow]Couldn't reach Ollama at "
            f"[bold]{host}[/bold] (no models installed or daemon not running)."
            "[/yellow]\n"
            "[dim]Install: [bold cyan]https://ollama.com[/bold cyan]  ·  "
            "Then: [bold]ollama pull llama3.1[/bold][/dim]"
        )
        choice = ask_numbered_choice(
            console,
            "What now?",
            [
                ("poll", "Wait for Ollama to come up (I'll install/start it now)"),
                ("cloud", "Use a cloud provider instead"),
                ("skip", "Skip AI for now"),
            ],
            default=1,
        )
        if choice == "poll":
            available_models = _poll_for_ollama(host, console)
            if not available_models:
                console.print(
                    "[yellow]Still no Ollama — falling back to the cloud picker.[/yellow]"
                )
                return _prompt_for_api_key_loop(console, allow_browser=False)
        elif choice == "cloud":
            return _prompt_for_api_key_loop(console, allow_browser=False)
        else:
            return None

    if len(available_models) == 1:
        model = available_models[0]
        console.print(f"[green]Using Ollama model:[/green] {model}")
    else:
        model = ask_numbered_choice(
            console,
            "Which local model should Forge use?",
            [(m, m) for m in available_models],
            default=1,
        )

    env = dict(os.environ)
    LOG.info("AI setup: selected ollama model=%s", model)
    _save_ai_config("ollama", model, ollama_host=host)
    return LlmConfig(
        provider="ollama",
        model=model,
        endpoint=provider.default_endpoint(model, env),
        api_key=None,
    )


def _make_ollama_config(*, model: Optional[str] = None) -> Any:
    """Build a fully-formed ``LlmConfig`` for local Ollama.

    Reads ``os.environ`` once and defaults the model to the provider's
    built-in default when *model* is ``None`` or empty. Callers that
    need ``OLLAMA_HOST`` respected should set it on ``os.environ``
    before calling.
    """
    host_mod = _host()
    BUILTIN_LLM_PROVIDERS = host_mod.BUILTIN_LLM_PROVIDERS
    LlmConfig = host_mod.LlmConfig

    provider = BUILTIN_LLM_PROVIDERS["ollama"]
    env = dict(os.environ)
    resolved_model = model or provider.default_model
    return LlmConfig(
        provider="ollama",
        model=resolved_model,
        endpoint=provider.default_endpoint(resolved_model, env),
        api_key=None,
    )


__all__ = [
    "_make_ollama_config",
    "_poll_for_ollama",
    "_query_ollama_models",
    "_setup_ollama",
]
