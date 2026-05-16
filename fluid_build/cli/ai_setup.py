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

"""Unified AI / LLM setup for the FLUID CLI.

Provides four entry-points into the same underlying flow:

* ``fluid ai setup`` -- dedicated interactive command (like ``gh auth login``)
* ``fluid ai test`` -- quick connectivity/model smoke test
* ``run_ai_setup_inline()`` -- compact version triggered when forge starts
  without a configured provider
* ``show_ai_status()`` -- display current config (used by ``fluid doctor``)
"""

from __future__ import annotations

__all__ = [
    "register",
    "run_ai_setup_interactive",
    "run_ai_setup_inline",
    "run_ai_test",
    "set_session_env",
    "show_ai_models",
    "show_ai_status",
]

import json as _json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from fluid_build.cli.forge_banner import print_v2_banner
from fluid_build.cli.forge_copilot_llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    PROVIDER_DISPLAY_NAMES,
    PROVIDER_ENV_VARS,
    CopilotGenerationError,
    LlmConfig,
    build_llm_run_plan,
    check_llm_readiness,
    detect_ollama_available,
    detect_provider_from_api_key,
    get_catalog_default,
    get_catalog_routing_model,
    get_catalog_tier_models,
)
from fluid_build.cli.forge_dialogs import ask_confirmation

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    RICH_AVAILABLE = False

LOG = logging.getLogger("fluid.cli.ai_setup")

# SSRF guard: restrict Ollama host to localhost addresses only.
_LOCALHOST_PREFIXES = (
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
    "http://0.0.0.0",
)


def _sanitize_ollama_host(host: str) -> str:
    """Return *host* if it points to localhost, otherwise fall back to the default."""
    clean = (host or "http://localhost:11434").rstrip("/")
    if not any(clean.lower().startswith(p) for p in _LOCALHOST_PREFIXES):
        LOG.warning("OLLAMA_HOST points to a non-localhost address (%s), ignoring.", clean)
        return "http://localhost:11434"
    return clean


# Keyring key prefix used when persisting API keys.
_KEYRING_PREFIX = "llm_api_key"
_PLAINTEXT_AI_SECRETS_ENV = "FLUID_ALLOW_PLAINTEXT_AI_SECRETS"

# Session-level flag: True once user explicitly skips AI setup.
# Prevents re-prompting within the same process.
_ai_setup_skipped = False

# Key format hints shown when auto-detection fails.
_KEY_FORMAT_HINTS = "Recognised formats: sk-... (OpenAI), sk-ant-... (Anthropic), AIza... (Gemini)"

_AI_TEST_SYSTEM_PROMPT = "You are a FLUID CLI connectivity diagnostic. Reply with exactly FLUID_OK."
_AI_TEST_USER_PROMPT = "Reply with exactly FLUID_OK."
_AI_TEST_DEFAULT_TIMEOUT_SECONDS = 30
_AI_TEST_DEFAULT_OUTPUT_TOKENS = 8
# Gemini Flash thinking-mode requires a higher output budget — even a 5-token
# "FLUID_OK" reply is preceded by reasoning tokens that count against the cap.
# Cloud providers without thinking mode (OpenAI, Anthropic, Ollama) stay at 8.
_AI_TEST_GEMINI_OUTPUT_TOKENS = 256
_AI_TEST_DISPLAY_LIMIT = 160

# Exit codes for `fluid ai test` — see `--json` schema for parseable form.
_AI_TEST_EXIT_OK = 0
_AI_TEST_EXIT_CONFIG = 2
_AI_TEST_EXIT_AUTH = 3
_AI_TEST_EXIT_RESOURCE = 4
_AI_TEST_EXIT_NETWORK = 5
_AI_TEST_REPORT_VERSION = 1


# ---------------------------------------------------------------------------
# Config file — persists provider + model choice across sessions
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path.home() / ".fluid"
_CONFIG_FILE = _CONFIG_DIR / "ai_config.json"


# Storage helpers (config + keyring) — physically extracted to
# ``cli/_ai_setup_storage.py``. ~155 LOC of pure I/O lifted without
# behavior change. Re-exported here under the same names so test
# patches on ``fluid_build.cli.ai_setup.<helper>`` flow through to
# the moved functions via the module-attribute-access indirection.
# Ollama poll/query helpers — extracted; see
# ``cli/_ai_setup_ollama.py`` for the implementation.
from fluid_build.cli._ai_setup_ollama import (  # noqa: E402,F401
    _poll_for_ollama,
    _query_ollama_models,
)
from fluid_build.cli._ai_setup_storage import (  # noqa: E402,F401
    _allow_plaintext_ai_secrets,
    _clear_ai_config,
    _clear_key_from_keyring,
    _load_ai_config,
    _load_key_from_keyring,
    _save_ai_config,
    _save_key_to_keyring,
)


def _validate_api_key(provider: Any, api_key: str) -> Optional[str]:
    """Lightweight API call to verify the key works.

    One litellm completion validates every provider — there's no
    per-provider httpx logic anymore. litellm is a core dep so the
    import always resolves.

    Returns ``None`` on success or a short human-readable error message.
    """
    import litellm  # core dep

    from fluid_build.cli.forge_copilot_llm_litellm import (
        _LITELLM_PREFIX_BY_PROVIDER,
    )

    prefix = _LITELLM_PREFIX_BY_PROVIDER.get(provider.name.lower(), provider.name.lower())
    model = (
        provider.default_model
        if "/" in (provider.default_model or "")
        else f"{prefix}/{provider.default_model}"
    )
    try:
        litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "Say ok"}],
            api_key=api_key,
            max_tokens=4,
            timeout=15,
            num_retries=0,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — translate to friendly text
        msg = str(exc).lower()
        if "401" in msg or "invalid" in msg or "auth" in msg:
            return "Invalid or expired API key"
        if "403" in msg or "permission" in msg:
            return "API key does not have sufficient permissions"
        if "429" in msg or "rate" in msg or "quota" in msg:
            return "Rate limited — key is valid but quota exceeded"
        if "timeout" in msg:
            return "API request timed out (15s)"
        if "connect" in msg or "dns" in msg or "resolv" in msg:
            return "Could not connect to API endpoint"
        return f"validation failed: {exc}"


def set_session_env(provider: str, api_key: str) -> None:
    """Set the provider-specific env var for the current process only.

    This is necessary so that ``resolve_llm_config()`` can find the key
    during this session.  The key is **not** written to disk or exported
    to child processes beyond the current process tree.
    """
    env_var = PROVIDER_ENV_VARS.get(provider)
    if env_var:
        os.environ[env_var] = api_key
        # Don't interpolate env_var/provider — ``api_key`` is in scope
        # at this LOG site (CodeQL py/clear-text-logging-sensitive-data).
        LOG.debug("Set session env var (provider configured)")


# ---------------------------------------------------------------------------
# Core setup flow (shared by interactive + inline)
# ---------------------------------------------------------------------------


_SIGNUP_URLS = {
    "gemini": "https://aistudio.google.com/apikey",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
}


def _pick_provider(console: Any) -> Optional[str]:
    """Pick an LLM provider.

    LiteLLM runs invisibly under the hood for every option (it's the
    default backend when installed). The user just picks a provider —
    they never see "litellm" as a choice. Adding a new provider
    becomes one row in this list because litellm already speaks them
    all (Groq, Bedrock, Azure, Vertex, Mistral, Cohere, …).

    Providers in the picker are surfaced based on what litellm
    supports + what's most likely useful to the user. The list expands
    as litellm's catalog grows.

    Returns the canonical provider key, or ``None`` if the user skipped.
    """
    from fluid_build.cli.forge_ui import ask_numbered_choice

    # Detect litellm availability once so we can offer the broader
    # list when present, and fall back to the native four when it
    # isn't installed.
    try:
        import litellm  # noqa: F401

        litellm_available = True
    except ImportError:
        litellm_available = False

    base_choices: List[Tuple[str, str]] = [
        ("gemini", "Google Gemini  (free tier available)"),
        ("openai", "OpenAI (ChatGPT)"),
        ("anthropic", "Anthropic (Claude)"),
    ]
    extended_choices: List[Tuple[str, str]] = [
        ("groq", "Groq  (fast inference, Llama / Mixtral)"),
        ("bedrock", "AWS Bedrock  (Claude / Llama / Titan via AWS)"),
        ("azure", "Azure OpenAI  (enterprise OpenAI deployments)"),
        ("vertex_ai", "Google Vertex AI  (Gemini via GCP)"),
        ("mistral", "Mistral AI"),
        ("cohere", "Cohere"),
    ]
    tail_choices: List[Tuple[str, str]] = [
        ("ollama", "Ollama  (run AI locally — free, no internet)"),
        ("browser", "Browser sign-in (OAuth — preview)"),
        ("skip", "Skip for now — I'll set this up later"),
    ]

    choices = list(base_choices)
    if litellm_available:
        choices += extended_choices
    choices += tail_choices

    provider_choice = ask_numbered_choice(
        console,
        "How do you want to connect?",
        choices,
        default=1,
    )

    if provider_choice == "skip":
        global _ai_setup_skipped  # noqa: PLW0603
        _ai_setup_skipped = True
        LOG.debug("User skipped AI setup (sticky for this session)")
        return None

    # Gemini sub-flow: free or BYO-key. Both end up with provider='gemini'.
    if provider_choice == "gemini":
        sub = ask_numbered_choice(
            console,
            "Gemini setup:",
            [
                ("byo", "I have a Gemini API key"),
                ("free", "I need a key — 30-second free signup"),
            ],
            default=1,
        )
        if sub == "free":
            console.print(
                "\n[bold]Get your free Gemini key:[/bold]\n"
                "  1. Open [bold cyan]https://aistudio.google.com/apikey[/bold cyan]\n"
                "  2. Sign in with your Google account\n"
                "  3. Click [bold]Create API Key[/bold]\n"
                "  4. Paste it below.\n"
            )
        return "gemini"

    return provider_choice


def _pick_tier(console: Any) -> str:
    """Phase 0.5 #9 — tier picker FIRST, before key validation.

    The user picks fast / balanced / cheap up-front so they understand
    the cost trade-off BEFORE pasting a key. Default ``balanced`` —
    cheapest option compatible with every provider.
    """
    from fluid_build.cli.forge_ui import ask_numbered_choice

    return ask_numbered_choice(
        console,
        "Which model tier?",
        [
            ("flagship", "Flagship — most capable (highest cost)"),
            ("balanced", "Balanced — recommended (cheaper, faster)"),
            ("fast", "Fast / cheap — for quick iteration"),
        ],
        default=2,  # balanced is the world-class default
    )


def _classify_key_shape(raw: str) -> str:
    """Phase 0.5 #12 — shape-detect on bad keys.

    Returns one of ``openai``, ``anthropic``, ``gemini``, or ``unknown``.
    Lightweight regex match on the key prefix; never validates against
    the provider — that's what ``_validate_api_key`` is for.
    """
    return detect_provider_from_api_key(raw) or "unknown"


def _rescue_after_attempts(console: Any) -> Optional[str]:
    """Phase 0.5 #11 — 3-attempt rescue dialog.

    Instead of dead-ending with "run fluid ai setup later", offer a
    way back into the flow:

    * switch provider — re-enter the picker
    * Ollama — bypass the cloud entirely
    * skip — abort gracefully (the existing path)

    Returns the new provider choice (``"switch"``, ``"ollama"``, or
    ``None`` for skip).
    """
    from fluid_build.cli._ai_setup_prompt import render as render_ai_panel
    from fluid_build.cli.forge_ui import ask_numbered_choice

    render_ai_panel(reason="rescue", console=console)
    rescue = ask_numbered_choice(
        console,
        "Pick a path forward:",
        [
            ("switch", "Try a different provider"),
            ("ollama", "Switch to Ollama (run AI locally, free)"),
            ("skip", "Skip AI for now (I'll use --blank or come back later)"),
        ],
        default=1,
    )
    if rescue == "skip":
        return None
    return rescue


def _prompt_for_api_key(console: Any) -> Optional[LlmConfig]:
    """Walk the user through picking an AI provider via numbered menu.

    Phase 0.5 fixes (#6, #7, #9, #11, #12, #15, #18) all land here:

    * Unified panel via ``_ai_setup_prompt.render`` (#6)
    * Single Gemini entry with sub-flow (#7)
    * Tier picker FIRST (#9)
    * 3-attempt rescue dialog (#11)
    * Shape-detect on bad keys BEFORE retry (#12)
    * Doctor hint promoted (#15)
    * Browser OAuth scaffold (#18)

    Returns a resolved ``LlmConfig`` or ``None`` if the user cancels.
    """
    if not console or not RICH_AVAILABLE:
        LOG.debug("Cannot prompt for API key: no Rich console available")
        return None

    from fluid_build.cli._ai_setup_prompt import render as render_ai_panel

    render_ai_panel(reason="missing", console=console, show_doctor_hint=True)

    return _prompt_for_api_key_loop(console, allow_browser=True)


def _prompt_for_api_key_loop(console: Any, *, allow_browser: bool = True) -> Optional[LlmConfig]:
    """Inner loop — picker + tier + key + validation + rescue.

    Split from :func:`_prompt_for_api_key` so the rescue path can
    re-enter without re-rendering the unified top panel.
    """
    provider_choice = _pick_provider(console)
    if provider_choice is None:
        return None

    if provider_choice == "ollama":
        return _setup_ollama(console)

    if provider_choice == "browser":
        if not allow_browser:
            return None
        return _setup_browser_oauth(console)

    # Phase 0.5 #9 — tier picker FIRST so the user understands trade-offs.
    tier = _pick_tier(console)

    return _collect_and_validate_api_key(console, provider_choice=provider_choice, tier=tier)


def _collect_and_validate_api_key(
    console: Any,
    *,
    provider_choice: str,
    tier: str,
    max_attempts: int = 3,
) -> Optional[LlmConfig]:
    """Ask for the API key, validate, retry up to ``max_attempts``,
    rescue on exhaustion. Returns a resolved :class:`LlmConfig` or
    ``None`` when the user gives up.
    """
    label = PROVIDER_DISPLAY_NAMES.get(provider_choice, provider_choice)
    signup_url = _SIGNUP_URLS.get(provider_choice, "")
    last_error: Optional[str] = None

    for attempt in range(1, max_attempts + 1):
        url_hint = (
            f"\n[dim]Get your key at: [bold cyan]{signup_url}[/bold cyan][/dim]"
            if signup_url
            else ""
        )
        if attempt == 1:
            console.print(f"\n[bold]{label}[/bold] selected.{url_hint}")
        console.print("[dim]Paste your API key (input is hidden).[/dim]")

        raw = Prompt.ask("[bold]API key[/bold]", password=True)
        raw = (raw or "").strip()
        if not raw:
            console.print("[yellow]No key entered.[/yellow]")
            return None

        # Phase 0.5 #12 — shape-detect BEFORE validation when format is
        # obviously wrong for the chosen provider. Saves the user a
        # round-trip to the API and a confusing 401 error.
        shape = _classify_key_shape(raw)
        if shape != "unknown" and shape != provider_choice:
            actual_label = PROVIDER_DISPLAY_NAMES.get(shape, shape)
            console.print(
                f"[yellow]That key looks like a {actual_label} key, "
                f"but you picked {label}.[/yellow]"
            )
            use_detected = Confirm.ask(f"Use {actual_label} instead?", default=True)
            if use_detected:
                provider_choice = shape
                label = actual_label
                signup_url = _SIGNUP_URLS.get(provider_choice, "")
        elif shape == "unknown":
            console.print(
                "[dim]Could not match key format to any known provider — "
                "trying validation anyway.[/dim]"
            )

        provider = BUILTIN_LLM_PROVIDERS.get(provider_choice)
        if not provider:
            # Litellm-only provider (groq, bedrock, azure, vertex_ai,
            # mistral, cohere, ...). Synthesise a LiteLLMProvider so
            # the validation + persistence path doesn't need a
            # dedicated class for every provider litellm speaks.
            try:
                from fluid_build.cli.forge_copilot_llm_litellm import (
                    get_litellm_provider,
                )

                provider = get_litellm_provider(provider_choice)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("litellm_provider_unavailable: %s", exc)
                console.print(f"[red]Unknown provider: {provider_choice}[/red]")
                return None

        console.print("[dim]Verifying API key...[/dim]")
        error = _validate_api_key(provider, raw)
        if error:
            last_error = error
            remaining = max_attempts - attempt
            if remaining > 0:
                console.print(
                    f"[red]Key validation failed: {error}[/red]\n"
                    f"[dim]{remaining} attempt(s) left. "
                    "[bold]Tip:[/bold] run [bold cyan]fluid doctor[/bold cyan] "
                    "if you suspect an environment issue.[/dim]"
                )
                continue

            # Phase 0.5 #11 — 3-attempt rescue.
            console.print(f"[red]Last error: {error}[/red]")
            rescue = _rescue_after_attempts(console)
            if rescue == "switch":
                # Re-enter the picker — but skip browser to avoid loop.
                return _prompt_for_api_key_loop(console, allow_browser=False)
            if rescue == "ollama":
                return _setup_ollama(console)
            return None  # skip

        return _persist_and_return(
            console,
            provider_choice=provider_choice,
            provider=provider,
            label=label,
            tier=tier,
            raw_key=raw,
        )

    # Defensive — should not be reached because the loop above either
    # returns or exhausts via the rescue branch. Don't interpolate
    # ``last_error`` (CodeQL py/clear-text-logging-sensitive-data:
    # ``raw_key`` is in scope from the loop).
    LOG.debug("api_key_loop_exhausted_silently")
    return None


def _persist_and_return(
    console: Any,
    *,
    provider_choice: str,
    provider: Any,
    label: str,
    tier: str,
    raw_key: str,
) -> LlmConfig:
    """Save key + config, set session env, return :class:`LlmConfig`."""
    from fluid_build.cli.forge_copilot_llm_providers import get_catalog_tier_model

    console.print(f"[green]Verified! Connected to {label}.[/green]")
    saved = _save_key_to_keyring(provider_choice, raw_key)
    if saved:
        console.print("[green]Saved to system keychain (you won't be asked again).[/green]")
        api_key_for_config: Optional[str] = None
    elif _allow_plaintext_ai_secrets():
        console.print(
            f"[yellow]System keychain unavailable; saved key to {_CONFIG_FILE} "
            "because FLUID_ALLOW_PLAINTEXT_AI_SECRETS is enabled.[/yellow]"
        )
        api_key_for_config = raw_key
    else:
        console.print(
            "[yellow]System keychain unavailable; provider/model will be saved, "
            "but the API key will only be used for this process. Export the "
            "provider API key env var, install a keyring backend, or set "
            "FLUID_ALLOW_PLAINTEXT_AI_SECRETS=1 to opt into local plaintext "
            "persistence.[/yellow]"
        )
        api_key_for_config = None

    set_session_env(provider_choice, raw_key)
    model = get_catalog_tier_model(provider_choice, tier) or provider.default_model
    _save_ai_config(provider_choice, model, api_key=api_key_for_config)
    env = dict(os.environ)
    # CodeQL py/clear-text-logging-sensitive-data: ``raw_key`` and
    # ``api_key_for_config`` are in scope. Log only a constant
    # confirmation; the provider/model/tier are visible to the
    # operator on the next ``fluid forge`` invocation.
    LOG.info("AI setup: configuration saved")
    return LlmConfig(
        provider=provider_choice,
        model=model,
        endpoint=provider.default_endpoint(model, env),
        api_key=raw_key,
    )


def _setup_browser_oauth(console: Any) -> Optional[LlmConfig]:
    """Phase 0.5 #18 — browser OAuth flow (preview).

    Today this is a scaffold: it confirms intent + falls through to
    the keyring-stored credential check for whichever provider the
    user picks. A full OAuth implementation will land per-provider in
    follow-up work; this surface keeps the option visible and the
    fallback safe.
    """
    from fluid_build.cli.forge_ui import ask_numbered_choice

    console.print(
        "\n[bold]Browser sign-in[/bold] is a preview — only a small set of "
        "providers expose first-class OAuth today."
    )
    provider = ask_numbered_choice(
        console,
        "Which provider?",
        [
            ("openai", "OpenAI"),
            ("anthropic", "Anthropic"),
            ("gemini", "Google"),
            ("back", "Back to the picker"),
        ],
        default=1,
    )
    if provider == "back":
        return _prompt_for_api_key_loop(console, allow_browser=False)

    try:
        from fluid_build.cli.ai_setup_browser import open_oauth_flow

        return open_oauth_flow(console, provider=provider)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("browser_oauth_failed: %s", exc)
        console.print(
            "[yellow]Browser OAuth isn't fully wired for that provider yet.[/yellow]\n"
            "[dim]Falling back to API-key entry.[/dim]"
        )
        tier = _pick_tier(console)
        return _collect_and_validate_api_key(console, provider_choice=provider, tier=tier)


# Ollama setup helpers — physically extracted to
# ``cli/_ai_setup_ollama.py``. Re-exported via the
# module-attribute-access indirection pattern.
from fluid_build.cli._ai_setup_ollama import (  # noqa: E402,F401
    _make_ollama_config,
    _setup_ollama,
)


def run_ai_setup_interactive(console: Any) -> Optional[LlmConfig]:
    """Full interactive AI setup.  Called by ``fluid ai setup``."""
    if not console or not RICH_AVAILABLE:
        from fluid_build.cli.console import error as console_error

        console_error(
            "Interactive AI setup requires a terminal with Rich support.\n"
            "Install Rich: pip install rich\n"
            "Or set API keys directly: export OPENAI_API_KEY=sk-..."
        )
        return None

    # Show current status first
    readiness = check_llm_readiness()
    if readiness.ready:
        console.print(
            Panel(
                f"[green]AI is already configured:[/green]\n"
                f"  Provider: [bold]{readiness.provider}[/bold]\n"
                f"  Model:    [bold]{readiness.model}[/bold]",
                title="Current AI Config",
                border_style="green",
            )
        )
        if not Confirm.ask("Reconfigure?", default=False):
            return None

    config = _prompt_for_api_key(console)
    if config:
        console.print(
            Panel(
                f"[green]AI ready![/green]\n"
                f"  Provider: [bold]{config.provider}[/bold]\n"
                f"  Model:    [bold]{config.model}[/bold]\n\n"
                "Run [bold cyan]fluid forge[/bold cyan] to create a data product with AI.",
                title="Setup Complete",
                border_style="green",
            )
        )
    return config


def _make_cloud_config(
    pname: str,
    api_key: str,
    *,
    model: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> LlmConfig:
    """Build a fully-formed ``LlmConfig`` for a cloud provider.

    Defaults *model* to the provider's built-in default and *endpoint* to
    the provider's computed default when the respective arguments are
    ``None``.  Reads ``os.environ`` once.
    """
    provider = BUILTIN_LLM_PROVIDERS[pname]
    env = dict(os.environ)
    resolved_model = model or provider.default_model
    return LlmConfig(
        provider=pname,
        model=resolved_model,
        endpoint=endpoint or provider.default_endpoint(resolved_model, env),
        api_key=api_key,
    )


def run_ai_setup_inline(console: Any) -> Optional[LlmConfig]:
    """Compact inline setup triggered when forge starts without a configured provider.

    Resolves an LLM config in this priority order:

        1. saved config file + OS keyring (``~/.fluid/ai_config.json``)
        2. cloud provider env vars (``OPENAI_API_KEY`` etc.)
        3. explicit ``OLLAMA_HOST`` env var
        4. auto-detected local Ollama (with user confirmation on TTY)
        5. interactive provider picker

    Returns ``None`` if the user skips setup or stdin is not a TTY.
    """
    # 0. Respect session-level skip.
    if _ai_setup_skipped:
        LOG.debug("AI setup was skipped earlier in this session")
        return None

    # 1. Check saved provider/model config and OS-keyring secret.
    saved = _load_ai_config()
    if saved and saved.get("provider"):
        pname = saved["provider"]
        model = saved.get("model")
        provider = BUILTIN_LLM_PROVIDERS.get(pname)
        if provider:
            if pname == "ollama":
                ollama_host = _sanitize_ollama_host(
                    saved.get("ollama_host", "http://localhost:11434")
                )
                os.environ["OLLAMA_HOST"] = ollama_host
                config = _make_ollama_config(model=model)
                if console and RICH_AVAILABLE:
                    console.print(f"[dim]Using Ollama ({config.model}).[/dim]")
                LOG.info("Inline AI setup: loaded ollama from config")
                return config
            # Cloud provider — key is in the opt-in plaintext config
            # or, preferentially, the OS keyring.
            api_key = saved.get("api_key") or _load_key_from_keyring(pname)
            if api_key:
                set_session_env(pname, api_key)
                config = _make_cloud_config(
                    pname, api_key, model=model, endpoint=saved.get("endpoint")
                )
                label = PROVIDER_DISPLAY_NAMES.get(pname, pname)
                if console and RICH_AVAILABLE:
                    console.print(f"[dim]Using {label} ({config.model}).[/dim]")
                LOG.info("Inline AI setup: loaded %s from saved config", pname)
                return config
            # Config exists but no key anywhere — fall through to prompt.

    # 2. Check cloud-provider env vars (e.g. CI environments that
    #    inject ANTHROPIC_API_KEY / OPENAI_API_KEY directly).
    for pname, env_var in PROVIDER_ENV_VARS.items():
        env_key = os.environ.get(env_var)
        if env_key:
            if console and RICH_AVAILABLE:
                label = PROVIDER_DISPLAY_NAMES.get(pname, pname)
                console.print(f"[dim]Using {label} from environment.[/dim]")
            LOG.info("Inline AI setup: loaded %s from env var", pname)
            return _make_cloud_config(pname, env_key)

    # 3. Explicit OLLAMA_HOST env var.
    if os.environ.get("OLLAMA_HOST"):
        if console and RICH_AVAILABLE:
            console.print("[dim]Using local Ollama.[/dim]")
        LOG.info("Inline AI setup: using ollama from OLLAMA_HOST env var")
        return _make_ollama_config()

    # 4. Auto-detect local Ollama — ask the user before selecting it.
    if detect_ollama_available(os.environ):
        if sys.stdin.isatty() and console and RICH_AVAILABLE:
            use_ollama = ask_confirmation(
                console,
                "Local Ollama detected. Use it?",
                default=True,
                preview=(
                    "Ollama runs LLMs on your machine — free, no API key, no internet.\n"
                    "Good for experimenting and privacy-sensitive work.\n"
                    "For faster/better results on real projects, use a cloud provider."
                ),
                title="Local AI Available",
                border_style="blue",
            )
            if use_ollama:
                config = _make_ollama_config()
                _save_ai_config("ollama", config.model)
                console.print(f"[dim]Using Ollama ({config.model}).[/dim]")
                LOG.info("Inline AI setup: user confirmed local Ollama")
                return config
            # User declined Ollama — fall through to the full provider prompt.
        else:
            # Non-interactive (CI) — auto-select Ollama silently.
            LOG.info("Inline AI setup: auto-selected local Ollama (non-interactive)")
            return _make_ollama_config()

    # 5. Nothing found — prompt user (only if stdin is interactive).
    if not sys.stdin.isatty():
        LOG.debug("Inline AI setup: stdin is not a TTY, skipping interactive prompt")
        return None

    if console and RICH_AVAILABLE:
        console.print()
        return _prompt_for_api_key(console)

    return None


def show_ai_status(console: Any) -> None:
    """Display current AI configuration status.  Used by ``fluid doctor``."""
    readiness = check_llm_readiness()

    if not console or not RICH_AVAILABLE:
        from fluid_build.cli.console import cprint

        status = "ready" if readiness.ready else "not configured"
        cprint(f"AI Copilot: {status}")
        if readiness.provider:
            cprint(f"  Provider: {readiness.provider}  Model: {readiness.model}")
        if readiness.error:
            cprint(f"  {readiness.error}")
        return

    if readiness.ready:
        table = Table(title="AI Copilot Status", border_style="green")
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Status", "Ready")
        table.add_row("Provider", readiness.provider or "--")
        table.add_row("Model", readiness.model or "--")
        console.print(table)
    else:
        console.print(
            Panel(
                f"[yellow]{readiness.error or 'AI not configured.'}[/yellow]\n\n"
                "Run [bold cyan]fluid ai setup[/bold cyan] to configure, "
                "or just run [bold cyan]fluid forge[/bold cyan] and you'll be guided through it.",
                title="AI Copilot Status",
                border_style="yellow",
            )
        )


def _provider_model_plan(provider_name: str) -> dict:
    """Build a display-safe model plan for one provider."""
    provider = BUILTIN_LLM_PROVIDERS[provider_name]
    model = get_catalog_default(provider_name) or provider.default_model
    routing_model = get_catalog_routing_model(provider_name, model)
    tier_models = get_catalog_tier_models(provider_name)
    cfg = LlmConfig(
        provider=provider.name,
        model=model,
        endpoint=provider.default_endpoint(model, dict(os.environ)),
        api_key=None,
        routing_model=routing_model,
        tier_models=tier_models,
    )
    return build_llm_run_plan(cfg, tiered=bool(tier_models))


def show_ai_models(
    console: Any,
    *,
    provider_filter: Optional[str] = None,
    as_json: bool = False,
) -> None:
    """Display bundled LLM model defaults, routing, and tier plans."""
    import json

    provider_names = [
        name
        for name in ("gemini", "openai", "anthropic", "ollama")
        if name in BUILTIN_LLM_PROVIDERS
    ]
    if provider_filter:
        requested = provider_filter.strip().lower()
        if requested == "claude":
            requested = "anthropic"
        provider_names = [name for name in provider_names if name == requested]

    plans = {name: _provider_model_plan(name) for name in provider_names}
    if as_json:
        text = json.dumps(plans, indent=2, sort_keys=True)
        sys.stdout.write(text + "\n")
        return

    if not console or not RICH_AVAILABLE:
        from fluid_build.cli.console import cprint

        for name, plan in plans.items():
            tiers = plan.get("tier_models") or {}
            tier_text = ", ".join(f"{k}={v}" for k, v in tiers.items()) or "single model"
            cprint(
                f"{name}: primary={plan.get('primary_model')} "
                f"routing={plan.get('routing_model')} tiers={tier_text}"
            )
        return

    table = Table(title="AI Model Plan", border_style="cyan")
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Role", style="bright_white", no_wrap=True)
    table.add_column("Model", overflow="fold")
    for name, plan in plans.items():
        tiers = plan.get("tier_models") or {}
        provider_label = PROVIDER_DISPLAY_NAMES.get(name, name)
        rows = [
            ("primary", plan.get("primary_model")),
            ("routing", plan.get("routing_model")),
            ("deep", tiers.get("deep")),
            ("balanced", tiers.get("balanced")),
            ("fast", tiers.get("fast")),
        ]
        for index, (role, model) in enumerate(rows):
            table.add_row(provider_label if index == 0 else "", role, str(model or "--"))
    console.print(table)
    console.print(
        "[dim]Contract forging, dbt SQL generation, and validation are deterministic from the logical sidecar.[/dim]"
    )


# AI-test plumbing — physically extracted to ``cli/_ai_setup_test.py``.
# ~500 LOC of config resolution, endpoint validation, token caps,
# model preflight, smoke call, error classification, JSON report
# emission. Re-exported here so existing call sites (``run_ai_test``)
# and test patches keep resolving.
from fluid_build.cli._ai_setup_test import (  # noqa: E402,F401
    _ai_test_token_budget_label,
    _cap_ai_test_token_budget,
    _check_ai_test_model_availability,
    _classify_ai_test_error,
    _coerce_ai_test_timeout,
    _http_error_name,
    _http_status_from_cause,
    _new_ai_test_report,
    _normalize_ai_test_provider,
    _resolve_ai_test_config,
    _run_ai_smoke_call,
    _safe_ai_test_display,
    _validate_ai_test_endpoint,
    _with_freeform_ai_test_payload,
    run_ai_test,
)


def register(subparsers) -> None:
    """Register the ``fluid ai`` command group."""
    parser = subparsers.add_parser(
        "ai",
        help="Configure AI / LLM settings for Forge Copilot",
    )
    ai_sub = parser.add_subparsers(dest="ai_action")
    setup_parser = ai_sub.add_parser(
        "setup", help="Interactive LLM provider OR metadata-source setup"
    )
    setup_parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear saved API keys from keychain",
    )
    setup_parser.add_argument(
        "--source",
        choices=[
            "snowflake",
            "unity",
            "bigquery",
            "dataplex",
            "glue",
            "datahub",
            "datamesh_manager",
        ],
        default=None,
        help=(
            "V1.5 — configure a metadata-source catalog instead of "
            "the LLM provider. Walks you through auth-method choice "
            "(key-pair / OAuth / token / etc.), captures the "
            "credentials, saves to OS keyring + ~/.fluid/sources.yaml, "
            "and tests the connection. Use the saved name as "
            "--credential-id on `fluid forge data-model from-source`."
        ),
    )
    setup_parser.add_argument(
        "--name",
        default=None,
        help=(
            "Saved name for the source / LLM config. Defaults to '<source>-prod' for source setup."
        ),
    )
    setup_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress the v2-preview banner (also honours $FLUID_QUIET / $FLUID_NONINTERACTIVE).",
    )
    status_parser = ai_sub.add_parser("status", help="Show current AI + source configuration")
    status_parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress the v2-preview banner (also honours $FLUID_QUIET / $FLUID_NONINTERACTIVE).",
    )
    models_parser = ai_sub.add_parser(
        "models",
        help="Show provider defaults, routing model, and tiered model plan",
    )
    models_parser.add_argument(
        "--provider",
        choices=["gemini", "openai", "anthropic", "claude", "ollama"],
        help="Limit output to one provider",
    )
    models_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    test_parser = ai_sub.add_parser(
        "test",
        help="Run a quick configured-provider connectivity test",
    )
    test_parser.add_argument(
        "--provider",
        dest="llm_provider",
        choices=["gemini", "openai", "anthropic", "claude", "ollama"],
        help="Override the configured provider for this test",
    )
    test_parser.add_argument(
        "--model",
        dest="llm_model",
        help="Override the configured model for this test",
    )
    test_parser.add_argument(
        "--endpoint",
        dest="llm_endpoint",
        help="Override the provider endpoint for this test",
    )
    test_parser.add_argument(
        "--timeout-seconds",
        dest="llm_timeout_seconds",
        type=int,
        default=None,
        help="HTTP timeout for the diagnostic request",
    )
    test_parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a stable JSON report to stdout instead of the Rich table. "
            "Schema: schema_version, ok, exit_code, provider, model, endpoint, "
            "model_availability, live_call, usage, output_cap, latency_ms, error."
        ),
    )
    parser.set_defaults(func=_run_ai_command)


def _run_ai_command(args, logger: logging.Logger) -> int:
    """Entry point for ``fluid ai setup|status``."""
    console = Console() if RICH_AVAILABLE else None
    action = getattr(args, "ai_action", None)

    if action == "setup":
        if getattr(args, "clear", False):
            _clear_ai_config()
            for p in PROVIDER_ENV_VARS:
                _clear_key_from_keyring(p)
            # Also reset Ollama detection cache so next run re-probes
            try:
                from fluid_build.cli.forge_copilot_llm_providers import reset_llm_caches

                reset_llm_caches()
            except ImportError:
                pass
            if console:
                console.print("[green]Cleared saved AI config and API keys.[/green]")
                console.print("[dim]Run 'fluid forge' to choose a provider.[/dim]")
            else:
                from fluid_build.cli.console import cprint

                cprint("Cleared saved AI config and API keys.")
            return 0

        # Metadata-source catalog wizard.
        # Routes to the dedicated source-setup module when --source
        # is set; otherwise falls through to the LLM-provider
        # wizard. Kept as a single ``setup`` subcommand (rather
        # than a separate ``setup-source`` verb) so the operator's
        # mental model is "fluid ai setup configures my AI / data
        # plumbing" — one place for both.
        source = getattr(args, "source", None)
        if source:
            from fluid_build.cli.ai_source_setup import setup_source

            rc = setup_source(source, name=getattr(args, "name", None), console=console)
            if rc == 0:
                print_v2_banner("ai_setup", quiet=getattr(args, "quiet", False))
            return rc

        result = run_ai_setup_interactive(console)
        if result is not None:
            print_v2_banner("ai_setup", quiet=getattr(args, "quiet", False))
        return 0 if result else 1

    if action == "status":
        show_ai_status(console)
        # V1.5 — also surface configured metadata-source catalogs
        # so ``fluid ai status`` is one stop for "what's wired up".
        from fluid_build.cli.ai_source_setup import show_source_status

        show_source_status(console)
        return 0

    if action == "models":
        show_ai_models(
            console,
            provider_filter=getattr(args, "provider", None),
            as_json=bool(getattr(args, "json", False)),
        )
        return 0

    if action == "test":
        exit_code, _report = run_ai_test(console, args)
        return exit_code

    # No subcommand -- default to showing status
    show_ai_status(console)
    return 0
