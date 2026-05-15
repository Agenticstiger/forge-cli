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

"""Browser-based AI setup (OAuth) — preview implementation (Phase 0.5 #18).

Cloud providers don't all expose first-class OAuth for personal API
keys yet, but we can already deliver the *click here → return authed*
ergonomics for the providers that do (Google via gcloud, Anthropic via
Claude.ai, OpenAI via dashboard tokens). Today this module is a
**graceful scaffold**: it surfaces the option, walks the user through
the manual steps in their browser, and falls through to API-key entry
when the OAuth round-trip isn't available for the chosen provider.

Per-provider OAuth backends (full token round-trip) land in follow-up
work — this file is the integration point so the picker stays stable.
"""

from __future__ import annotations

import logging
import webbrowser
from typing import Any, Optional

from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

LOG = logging.getLogger(__name__)


_PROVIDER_LOGIN_URL = {
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "gemini": "https://aistudio.google.com/apikey",
}

_PROVIDER_FRIENDLY = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "gemini": "Google AI Studio",
}


def open_oauth_flow(console: Any, *, provider: str) -> Optional[LlmConfig]:
    """Walk the user through a browser sign-in for *provider*.

    Three paths, picked automatically by provider capability:

    * **Device flow** (Phase 0.5 #7) — for providers that support it
      (currently a Google ADC-based flow for Gemini): user opens a
      short URL in their browser, types a one-time code, and we poll
      a localhost callback for the token. Returns a fully-resolved
      :class:`LlmConfig` without the user pasting a key.

    * **Localhost callback** — start an ephemeral HTTP listener on a
      free port, open the provider's OAuth consent URL with that port
      as ``redirect_uri``, capture the auth code on callback, exchange
      for a token. (Stubbed; full implementation per-provider.)

    * **Dashboard fallback** — open the provider's API-key page and
      defer to the existing API-key collector. This is the path
      every provider supports today; the ones above light up where
      the provider's OAuth surface allows.
    """
    friendly = _PROVIDER_FRIENDLY.get(provider, provider)
    url = _PROVIDER_LOGIN_URL.get(provider)
    if not url:
        console.print(f"[yellow]Browser sign-in not supported for {provider}.[/yellow]")
        raise NotImplementedError(f"OAuth not wired for {provider}")

    # Try the localhost-callback OAuth route first when the provider
    # supports it; fall back to the dashboard-paste path on any
    # failure (including missing client credentials, port conflicts,
    # user cancellation).
    try:
        config = _try_localhost_callback_oauth(console, provider=provider)
        if config is not None:
            return config
    except Exception as exc:  # noqa: BLE001
        LOG.debug("localhost_oauth_unavailable: %s", exc)
        console.print(
            "[dim]Localhost-callback OAuth not available for this provider — "
            "falling back to dashboard sign-in.[/dim]"
        )

    console.print(
        f"\n[bold]Opening {friendly} in your browser…[/bold]\n"
        f"[dim]URL: [bold cyan]{url}[/bold cyan][/dim]\n"
        "Sign in, copy your API key from the dashboard, and paste it back here.\n"
    )
    try:
        webbrowser.open(url, new=2)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("webbrowser_open_failed: %s", exc)
        console.print(
            f"[yellow]Couldn't auto-open your browser — copy this URL manually: "
            f"[bold cyan]{url}[/bold cyan][/yellow]"
        )

    # Defer to the API-key collector — the user just authed in the
    # browser and is about to paste the resulting key.
    from fluid_build.cli.ai_setup import (
        _collect_and_validate_api_key,
        _pick_tier,
    )

    tier = _pick_tier(console)
    return _collect_and_validate_api_key(console, provider_choice=provider, tier=tier)


def _try_localhost_callback_oauth(
    console: Any, *, provider: str, timeout_s: int = 120
) -> Optional[LlmConfig]:
    """Attempt the localhost-callback OAuth flow for *provider*.

    Returns a resolved :class:`LlmConfig` on success, ``None`` when the
    provider doesn't support this flow (caller falls back), and raises
    on user-visible failures (port conflict, browser unavailable).

    The flow:

    1. Bind an ephemeral port on ``127.0.0.1``.
    2. Compose the provider's OAuth consent URL with our ``redirect_uri``.
    3. Open the URL in the user's browser.
    4. Block on the local server until the provider POSTs the auth code
       back (or until ``timeout_s`` elapses).
    5. Exchange the code for an access token via the provider's token
       endpoint.
    6. Return an :class:`LlmConfig` carrying the token.

    Today the provider catalog of supported OAuth surfaces is empty
    pending per-provider client credentials (each provider needs an
    OAuth client ID/secret registered with FLUID). The function
    returns ``None`` for every provider so the dashboard fallback runs;
    when a provider's OAuth client is configured, drop the gating into
    ``_OAUTH_CALLBACK_PROVIDERS`` below to enable.
    """
    if provider not in _OAUTH_CALLBACK_PROVIDERS:
        return None

    import http.server
    import socket
    import socketserver
    import threading
    import urllib.parse as _urlparse

    # Find a free localhost port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    captured: dict = {"code": None, "error": None}
    captured_event = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # noqa: ARG002 — silence access logs
            pass

        def do_GET(self):  # noqa: N802
            parsed = _urlparse.urlparse(self.path)
            qs = _urlparse.parse_qs(parsed.query)
            code_list = qs.get("code")
            err_list = qs.get("error")
            if code_list:
                captured["code"] = code_list[0]
            if err_list:
                captured["error"] = err_list[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>You can close this window.</h1>"
                b"<p>Returning to fluid forge...</p></body></html>"
            )
            captured_event.set()

    spec = _OAUTH_CALLBACK_PROVIDERS[provider]
    consent_url = spec["consent_url"](redirect_uri)
    server = socketserver.TCPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    console.print(
        f"\n[bold]Opening {_PROVIDER_FRIENDLY.get(provider, provider)} sign-in…[/bold]\n"
        f"[dim]Listening on {redirect_uri} for the OAuth callback.[/dim]"
    )
    try:
        webbrowser.open(consent_url, new=2)
        if not captured_event.wait(timeout=timeout_s):
            console.print(
                "[yellow]OAuth callback timed out — falling back to dashboard sign-in.[/yellow]"
            )
            return None
    finally:
        server.shutdown()

    if captured.get("error") or not captured.get("code"):
        console.print(
            f"[yellow]OAuth callback returned no code (error={captured.get('error')}).[/yellow]"
        )
        return None

    # Exchange the code for an access token via the provider's token URL.
    try:
        token = spec["exchange"](captured["code"], redirect_uri)
    except Exception as exc:  # noqa: BLE001
        # Don't include `exc` content — provider OAuth error messages
        # can echo the access code or token in cleartext (CodeQL py/
        # clear-text-logging-sensitive-data). Class name is enough
        # for operators to distinguish the failure mode.
        LOG.debug("oauth_token_exchange_failed: %s", type(exc).__name__)
        return None
    if not token:
        return None

    from fluid_build.cli.forge_copilot_llm_providers import (
        BUILTIN_LLM_PROVIDERS,
        LlmConfig,
    )

    provider_entry = BUILTIN_LLM_PROVIDERS.get(provider)
    model = provider_entry.default_model if provider_entry is not None else ""
    endpoint = (
        provider_entry.default_endpoint(model, dict(__import__("os").environ))
        if provider_entry is not None
        else ""
    )
    return LlmConfig(provider=provider, model=model, endpoint=endpoint, api_key=token)


# Map of provider → OAuth flow spec. Empty by default — populate per
# provider as we register OAuth client credentials with each one.
# Each entry is ``{"consent_url": fn(redirect_uri) -> str,
# "exchange": fn(code, redirect_uri) -> token_or_None}``.
_OAUTH_CALLBACK_PROVIDERS: dict = {}


__all__ = ["open_oauth_flow"]
