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

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional
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
_AI_TEST_GEMINI_OUTPUT_TOKENS = 256
_AI_TEST_DISPLAY_LIMIT = 160


# ---------------------------------------------------------------------------
# Config file — persists provider + model choice across sessions
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path.home() / ".fluid"
_CONFIG_FILE = _CONFIG_DIR / "ai_config.json"


def _allow_plaintext_ai_secrets() -> bool:
    """Return True when the operator explicitly opts into plaintext key persistence."""
    return os.environ.get(_PLAINTEXT_AI_SECRETS_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _save_ai_config(
    provider: str,
    model: str,
    *,
    api_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    ollama_host: Optional[str] = None,
) -> bool:
    """Save non-sensitive AI config to ``~/.fluid/ai_config.json``.

    Provider and model choices live in the JSON file. API keys are
    persisted to the OS keyring whenever possible. Plaintext key
    fallback is intentionally opt-in via ``FLUID_ALLOW_PLAINTEXT_AI_SECRETS=1``
    so automated and agent-facing workflows don't quietly leave live
    provider tokens on disk.
    """
    import json
    import stat

    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        data: dict = {"provider": provider, "model": model}
        if api_key:
            saved_to_keyring = _save_key_to_keyring(provider, api_key)
            if saved_to_keyring:
                LOG.debug("Saved API key to keyring; not writing it to %s", _CONFIG_FILE)
            elif _allow_plaintext_ai_secrets():
                data["api_key"] = api_key
                LOG.warning("Plaintext local AI credential fallback is enabled.")
            else:
                LOG.debug("Keyring unavailable; sensitive AI value was not persisted.")
        if endpoint:
            data["endpoint"] = endpoint
        if ollama_host:
            data["ollama_host"] = ollama_host
        _CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Owner-only read/write — protect the API key
        _CONFIG_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        LOG.debug("Saved AI config to %s (mode 600)", _CONFIG_FILE)
        return True
    except OSError as exc:
        LOG.debug("Could not save AI config: %s", exc)
        return False


def _load_ai_config() -> Optional[dict]:
    """Load saved AI preferences.  Returns None if no config exists.

    Lookup order (Sprint #7 wiring):

    1. ``~/.fluid/config.yaml`` ``llm:`` section (unified path —
       new operators land here on first ``fluid ai setup`` call).
    2. ``~/.fluid/ai_config.json`` (legacy v1.5 file — pre-existing
       installs continue to work without re-migrating).
    3. ``None`` — no config saved yet.
    """
    import json

    # 1. Unified config — new operators.
    try:
        from fluid_build.copilot.unified_config import load_unified_config

        cfg = load_unified_config()
        if cfg is not None and cfg.llm and cfg.llm.provider:
            data: dict = {"provider": cfg.llm.provider}
            if cfg.llm.model:
                data["model"] = cfg.llm.model
            if cfg.llm.tiered:
                data["tiered"] = cfg.llm.tiered
            return data
    except Exception as exc:  # pragma: no cover — defensive
        LOG.debug("Could not load unified AI config: %s", exc)

    # 2. Legacy ``~/.fluid/ai_config.json`` — pre-existing installs.
    try:
        if not _CONFIG_FILE.exists():
            return None
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("provider"):
            return data
        return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _clear_ai_config() -> None:
    """Delete the saved AI config file."""
    try:
        if _CONFIG_FILE.exists():
            _CONFIG_FILE.unlink()
            LOG.debug("Deleted AI config at %s", _CONFIG_FILE)
    except OSError as exc:
        LOG.debug("Could not delete AI config: %s", exc)


# ---------------------------------------------------------------------------
# Keyring helpers
# ---------------------------------------------------------------------------


def _save_key_to_keyring(provider: str, api_key: str) -> bool:
    """Persist *api_key* in the OS keyring.  Returns True on success."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.set_credential(f"{_KEYRING_PREFIX}.{provider}", api_key)
        LOG.debug("Saved API key to keyring for provider=%s", provider)
        return True
    except ImportError as exc:
        LOG.debug("Keyring library not available: %s", exc)
        return False
    except OSError as exc:
        LOG.debug("Could not save API key to keyring for %s: %s", provider, exc)
        return False
    except Exception as exc:  # noqa: BLE001 — keyring backends can raise anything
        LOG.debug("Unexpected keyring error for %s: %s", provider, exc)
        return False


def _load_key_from_keyring(provider: str) -> Optional[str]:
    """Load a previously saved API key from the OS keyring."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        return KeyringCredentialStore.get_credential(f"{_KEYRING_PREFIX}.{provider}")
    except (ImportError, OSError) as exc:
        LOG.debug("Could not load key from keyring for %s: %s", provider, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — keyring backends can raise anything
        LOG.debug("Unexpected keyring error loading key for %s: %s", provider, exc)
        return None


def _clear_key_from_keyring(provider: str) -> bool:
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.delete_credential(f"{_KEYRING_PREFIX}.{provider}")
        return True
    except (ImportError, OSError) as exc:
        LOG.debug("Could not clear keyring for %s: %s", provider, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Unexpected keyring error clearing %s: %s", provider, exc)
        return False


def _query_ollama_models(host: str) -> list:
    """Return a list of model names available on the local Ollama instance.

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


def _validate_api_key(provider: Any, api_key: str) -> Optional[str]:
    """Make a lightweight API call to validate the key works.

    Returns ``None`` on success or an error message string on failure.
    Uses provider-specific minimal requests (e.g. list-models endpoint).
    """
    try:
        import httpx

        env = dict(os.environ)
        # Build a minimal request — ask the model to return a short response
        config = LlmConfig(
            provider=provider.name,
            model=provider.default_model,
            endpoint=provider.default_endpoint(provider.default_model, env),
            api_key=api_key,
            timeout_seconds=15,
        )
        headers, payload = provider.build_request(
            config,
            system_prompt="Respond with exactly: ok",
            user_prompt="Say ok",
        )
        # Reduce token budget for validation
        if "max_tokens" in payload:
            payload["max_tokens"] = 10

        with httpx.Client(timeout=config.timeout_seconds) as client:
            resp = client.post(config.endpoint, headers=headers, json=payload)
            resp.raise_for_status()

        return None  # Success
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            return "Invalid or expired API key"
        if status == 403:
            return "API key does not have sufficient permissions"
        if status == 429:
            return "Rate limited -- key is valid but quota exceeded"
        return f"API returned {status}"
    except httpx.ConnectError:
        return "Could not connect to API endpoint"
    except httpx.TimeoutException:
        return "API request timed out (15s)"
    except Exception as exc:  # noqa: BLE001
        return f"Unexpected error: {exc}"


def set_session_env(provider: str, api_key: str) -> None:
    """Set the provider-specific env var for the current process only.

    This is necessary so that ``resolve_llm_config()`` can find the key
    during this session.  The key is **not** written to disk or exported
    to child processes beyond the current process tree.
    """
    env_var = PROVIDER_ENV_VARS.get(provider)
    if env_var:
        os.environ[env_var] = api_key
        LOG.debug("Set session env var %s for provider=%s", env_var, provider)


# ---------------------------------------------------------------------------
# Core setup flow (shared by interactive + inline)
# ---------------------------------------------------------------------------


def _prompt_for_api_key(console: Any) -> Optional[LlmConfig]:
    """Walk the user through picking an AI provider via numbered menu.

    Returns a resolved ``LlmConfig`` or ``None`` if the user cancels.
    """
    if not console or not RICH_AVAILABLE:
        LOG.debug("Cannot prompt for API key: no Rich console available")
        return None

    from fluid_build.cli.forge_ui import ask_numbered_choice

    console.print(
        Panel(
            (
                "Forge uses AI to generate your data product.\n\n"
                "[dim]Not sure your environment is ready? Run [bold]fluid doctor[/bold] "
                "first\nto check Python version, credentials, and local providers.[/dim]\n\n"
                "Got an API key? Pick your provider below.\n"
                "Don't have one? No worries -- pick [bold]Google Gemini[/bold] to kick\n"
                "the tyres for [bold]free[/bold] (no credit card, 30 seconds to sign up)."
            ),
            title="AI Setup",
            border_style="blue",
        )
    )

    provider_choice = ask_numbered_choice(
        console,
        "How do you want to connect?",
        [
            ("gemini_free", "Google Gemini (free!) -- get a key in 30 seconds"),
            ("gemini", "Google Gemini -- I have an API key"),
            ("openai", "OpenAI (ChatGPT) -- I have an API key"),
            ("anthropic", "Anthropic (Claude) -- I have an API key"),
            ("ollama", "Ollama -- run AI locally on my machine (free, no internet)"),
            ("skip", "Skip for now -- I'll set this up later"),
        ],
        default=1,
    )

    if provider_choice == "skip":
        global _ai_setup_skipped  # noqa: PLW0603
        _ai_setup_skipped = True
        LOG.debug("User skipped AI setup (sticky for this session)")
        return None

    # --- Ollama path ---
    if provider_choice == "ollama":
        return _setup_ollama(console)

    # --- Free Gemini path: show signup URL then ask for key ---
    if provider_choice == "gemini_free":
        provider_choice = "gemini"
        console.print(
            "\n[bold]Here's how to get your free Gemini key:[/bold]\n"
            "  1. Go to [bold cyan]https://aistudio.google.com/apikey[/bold cyan]\n"
            "  2. Sign in with your Google account\n"
            "  3. Click [bold]Create API Key[/bold]\n"
            "  4. Copy the key and paste it below\n"
        )

    # --- Cloud provider path: ask for API key with retry ---
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        if provider_choice:
            label = PROVIDER_DISPLAY_NAMES.get(provider_choice, provider_choice)
            signup_url = {
                "gemini": "https://aistudio.google.com/apikey",
                "openai": "https://platform.openai.com/api-keys",
                "anthropic": "https://console.anthropic.com/settings/keys",
            }.get(provider_choice, "")
            if attempt > 1 or provider_choice != "gemini":
                url_hint = (
                    f"\n[dim]Get your key at: [bold cyan]{signup_url}[/bold cyan][/dim]"
                    if signup_url
                    else ""
                )
                console.print(f"\n[bold]{label}[/bold] selected.{url_hint}")
        console.print("[dim]Paste your API key (input is hidden).[/dim]")

        raw = Prompt.ask("[bold]API key[/bold]", password=True)
        raw = raw.strip()
        if not raw:
            console.print("[yellow]No key entered. You can run 'fluid ai setup' anytime.[/yellow]")
            return None

        # Auto-detect provider from key format — warn if mismatch
        detected = detect_provider_from_api_key(raw)
        if detected and detected != provider_choice:
            actual_label = PROVIDER_DISPLAY_NAMES.get(detected, detected)
            console.print(f"[yellow]That looks like a key for {actual_label}.[/yellow]")
            use_detected = Confirm.ask(f"Use {actual_label} instead?", default=True)
            if use_detected:
                provider_choice = detected

        provider = BUILTIN_LLM_PROVIDERS.get(provider_choice)
        if not provider:
            console.print(f"[red]Unknown provider: {provider_choice}[/red]")
            return None

        # Validate the key by making a lightweight API call
        console.print("[dim]Verifying API key...[/dim]")
        error = _validate_api_key(provider, raw)
        if error:
            remaining = max_attempts - attempt
            if remaining > 0:
                console.print(
                    f"[red]Key validation failed: {error}[/red]\n"
                    f"[dim]You have {remaining} attempt(s) remaining.[/dim]"
                )
                continue
            else:
                console.print(
                    f"[red]Key validation failed: {error}[/red]\n"
                    "[yellow]Run 'fluid ai setup' when you have a valid key.[/yellow]"
                )
                return None

        # Key is valid — save and return
        console.print(f"[green]Verified! Connected to {label}.[/green]")

        saved = _save_key_to_keyring(provider_choice, raw)
        if saved:
            console.print("[green]Saved to system keychain (you won't be asked again).[/green]")
            api_key_for_config = None
        elif _allow_plaintext_ai_secrets():
            console.print(
                f"[yellow]System keychain unavailable; saved key to {_CONFIG_FILE} "
                "because FLUID_ALLOW_PLAINTEXT_AI_SECRETS is enabled.[/yellow]"
            )
            api_key_for_config = raw
        else:
            console.print(
                "[yellow]System keychain unavailable; provider/model will be saved, "
                "but the API key will only be used for this process. Export the "
                "provider API key env var, install a keyring backend, or set "
                "FLUID_ALLOW_PLAINTEXT_AI_SECRETS=1 to opt into local plaintext "
                "persistence.[/yellow]"
            )
            api_key_for_config = None

        set_session_env(provider_choice, raw)

        # Model tier choice: flagship (most capable) vs balanced.
        # The catalog drives the actual model names so this code
        # never hardcodes a model string.
        from fluid_build.cli.forge_copilot_llm_providers import get_catalog_tier_model

        tier = ask_numbered_choice(
            console,
            "Which model tier?",
            [
                ("flagship", "Most capable (recommended)"),
                ("balanced", "Most balanced (faster, lower cost)"),
            ],
            default=1,
        )
        model = get_catalog_tier_model(provider_choice, tier) or provider.default_model

        _save_ai_config(provider_choice, model, api_key=api_key_for_config)

        env = dict(os.environ)
        LOG.info("AI setup: configured provider=%s model=%s tier=%s", provider_choice, model, tier)
        return LlmConfig(
            provider=provider_choice,
            model=model,
            endpoint=provider.default_endpoint(model, env),
            api_key=raw,
        )

    return None  # Shouldn't reach here, but satisfy type checker


def _setup_ollama(console: Any) -> Optional[LlmConfig]:
    """Handle the Ollama setup path with model discovery."""
    from fluid_build.cli.forge_ui import ask_numbered_choice

    provider = BUILTIN_LLM_PROVIDERS["ollama"]
    host = _sanitize_ollama_host(os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    os.environ["OLLAMA_HOST"] = host

    available_models = _query_ollama_models(host)
    if not available_models:
        console.print(
            "[yellow]Could not reach Ollama or no models are installed.[/yellow]\n\n"
            "To get started with Ollama:\n"
            "  1. Install from [bold cyan]https://ollama.com[/bold cyan]\n"
            "  2. Run: [bold]ollama pull llama3.1[/bold]\n"
            "  3. Then try [bold]fluid forge[/bold] again"
        )
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


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


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


def _make_ollama_config(*, model: Optional[str] = None) -> LlmConfig:
    """Build a fully-formed ``LlmConfig`` for local Ollama.

    Reads ``os.environ`` once and defaults the model to the provider's
    built-in default when *model* is ``None`` or empty.  Callers that need
    ``OLLAMA_HOST`` respected should set it on ``os.environ`` before calling.
    """
    provider = BUILTIN_LLM_PROVIDERS["ollama"]
    env = dict(os.environ)
    resolved_model = model or provider.default_model
    return LlmConfig(
        provider="ollama",
        model=resolved_model,
        endpoint=provider.default_endpoint(resolved_model, env),
        api_key=None,
    )


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
            # Cloud provider — key is in keyring, env, or legacy plaintext config.
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

    # 2. Check cloud-provider env vars (backward compat / CI).
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
    # Also check config file for extra info
    saved = _load_ai_config()
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


def _safe_ai_test_display(value: Any, *, limit: int = _AI_TEST_DISPLAY_LIMIT) -> str:
    text = str(value or "")
    clean = "".join(ch if ch.isprintable() and ch != "\x1b" else "?" for ch in text)
    if len(clean) > limit:
        return clean[: limit - 1] + "…"
    return clean


def _http_error_name(exc: httpx.HTTPError) -> str:
    return exc.__class__.__name__


def _validate_ai_test_endpoint(provider_name: str, endpoint: str) -> Optional[str]:
    parsed = urlsplit(str(endpoint or ""))
    if parsed.username or parsed.password:
        return "AI test endpoint URLs must not embed credentials."
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


def _run_ai_smoke_call(provider: Any, config: LlmConfig) -> tuple[str, dict]:
    headers, payload = _with_freeform_ai_test_payload(provider, config)
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

    try:
        response_json = response.json()
        text = provider.extract_text(response_json).strip()
        usage = provider.extract_usage(response_json)
    except Exception as exc:  # noqa: BLE001
        raise CopilotGenerationError(
            "ai_test_response_invalid",
            f"AI test response from {config.provider} could not be parsed.",
            suggestions=["Try a different model or re-run `fluid ai setup`."],
        ) from exc
    if "FLUID_OK" not in text:
        raise CopilotGenerationError(
            "ai_test_unexpected_response",
            f"AI test response from {config.provider} was not the expected diagnostic token.",
            suggestions=["Try again, or choose a different model with `fluid ai setup`."],
        )
    return "FLUID_OK", usage


def run_ai_test(console: Any, args: Any) -> bool:
    """Run a quick configured-provider connectivity and model smoke test."""
    config, config_error = _resolve_ai_test_config(args)
    if not config:
        message = config_error or "AI provider is not configured."
        if console and RICH_AVAILABLE:
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
        return False

    provider = BUILTIN_LLM_PROVIDERS[config.provider]
    label = PROVIDER_DISPLAY_NAMES.get(config.provider, config.provider)
    try:
        availability, available_models = _check_ai_test_model_availability(provider, config)
        text, usage = _run_ai_smoke_call(provider, config)
    except CopilotGenerationError as exc:
        if console and RICH_AVAILABLE:
            suggestions = "\n".join(f"- {s}" for s in exc.suggestions)
            body = f"[red]{exc.message}[/red]"
            if suggestions:
                body += f"\n\n[dim]{suggestions}[/dim]"
            console.print(Panel(body, title="AI Provider Test Failed", border_style="red"))
        else:
            from fluid_build.cli.console import cprint

            cprint(f"AI Provider Test Failed: {exc.message}")
        return False

    endpoint = config.redacted_endpoint
    usage_text = (
        f"{usage.get('input_tokens', 0)} input / "
        f"{usage.get('output_tokens', 0)} output / "
        f"{usage.get('total_tokens', 0)} total"
    )
    output_cap = _ai_test_token_budget_label(config)
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
        cprint(f"  Token usage: {usage_text}; output cap: {output_cap}")
    return True


# ---------------------------------------------------------------------------
# CLI registration -- ``fluid ai setup``
# ---------------------------------------------------------------------------


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
            "Saved name for the source / LLM config. Defaults to "
            "'<source>-prod' for source setup."
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

        # V1.5 — metadata-source catalog wizard.
        # Routes to the dedicated source-setup module when --source
        # is set; otherwise falls through to the legacy LLM-provider
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
        return 0 if run_ai_test(console, args) else 1

    # No subcommand -- default to showing status
    show_ai_status(console)
    return 0
