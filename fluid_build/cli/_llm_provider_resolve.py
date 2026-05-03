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

"""LLM provider + API-key resolution helpers — physical extraction.

Lifted from ``cli/forge_copilot_llm_providers.py`` (host file was
1550 LOC). ~130 LOC of pure provider-inference + keyring round-trips.

* :func:`_infer_provider_from_explicit_keys` /
  :func:`_infer_provider_from_ambient` /
  :func:`_infer_provider_from_env` /
  :func:`_infer_provider_from_keyring` — pick a provider hint from
  whatever signal we can find.
* :func:`_resolve_api_key` — env var first, keyring fallback.
* :func:`save_api_key_to_keyring` / :func:`clear_api_key_from_keyring`
  / :func:`_get_api_key_from_keyring` / :func:`_keyring_key` —
  keyring round-trips for cloud-provider secrets.

The host module re-imports each at module top so existing call sites
keep resolving.
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional

LOG = logging.getLogger("fluid.cli.llm_providers.resolve")


def _host():
    from fluid_build.cli import forge_copilot_llm_providers as _llm

    return _llm


def __getattr__(name: str):
    if name == "PROVIDER_ENV_VARS":
        return _host().PROVIDER_ENV_VARS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _bind_constants_from_host() -> None:
    """Pull names from the host that bare-name LOAD_GLOBAL inside
    ``_infer_provider_from_*`` references — constants and stable
    helpers."""
    host = _host()
    g = globals()
    for name in (
        "PROVIDER_ENV_VARS",
        "detect_ollama_available",
        "detect_provider_from_api_key",
    ):
        if hasattr(host, name):
            g[name] = getattr(host, name)


_bind_constants_from_host()


def _infer_provider_from_explicit_keys(env: Mapping[str, str]) -> Optional[str]:
    """Return the provider when the operator supplied exactly one explicit
    API-key env var.

    Explicit keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY,
    GOOGLE_API_KEY, OLLAMA_HOST) are deliberate per-run signals, so
    they should beat the saved ``~/.fluid/ai_config.json`` provider.
    Crucially, this helper does NOT auto-detect ambient Ollama on
    ``localhost:11434`` — a stray ``ollama serve`` running in the
    background must never override an explicit saved provider; that
    discovery happens later in :func:`_infer_provider_from_ambient`,
    which only fires once every other resolution step has failed.
    """

    detected = []
    if env.get("OPENAI_API_KEY"):
        detected.append("openai")
    if env.get("ANTHROPIC_API_KEY"):
        detected.append("anthropic")
    if env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
        detected.append("gemini")
    if env.get("OLLAMA_HOST"):
        detected.append("ollama")
    if len(detected) == 1:
        return detected[0]
    return None


def _infer_provider_from_ambient(env: Mapping[str, str]) -> Optional[str]:
    """Last-resort detection: Ollama already running on localhost.

    Only call this AFTER the saved config and the keyring have been
    consulted — ambient services must not override an explicit
    operator choice.  See :func:`check_llm_readiness` for the full
    resolution ladder.
    """

    # Resolve via the host module: ``detect_ollama_available`` is
    # defined later in the host's module body, so an import-time
    # binding would be ``None``. Late-resolution at call time picks
    # up the real function.
    detect_fn = getattr(_host(), "detect_ollama_available", None)
    if detect_fn is not None and detect_fn(env):
        return "ollama"
    return None


# Backwards-compatible alias for callers outside this module that
# imported ``_infer_provider_from_env``.  The new code paths inside
# :func:`check_llm_readiness` use the split functions above so the
# saved config gets a chance to win over ambient Ollama.
def _infer_provider_from_env(env: Mapping[str, str]) -> Optional[str]:
    explicit = _infer_provider_from_explicit_keys(env)
    if explicit:
        return explicit
    keyring_match = _infer_provider_from_keyring()
    if keyring_match:
        return keyring_match
    return _infer_provider_from_ambient(env)


def _infer_provider_from_keyring() -> Optional[str]:
    """Return the provider name if exactly one has a saved keyring key."""
    keyring_fn = getattr(_host(), "_get_api_key_from_keyring", _get_api_key_from_keyring)
    detected = []
    for name in ("openai", "anthropic", "gemini"):
        if keyring_fn(name):
            detected.append(name)
    if len(detected) == 1:
        return detected[0]
    return None


def _resolve_api_key(provider: str, env: Mapping[str, str]) -> Optional[str]:
    if env.get("FLUID_LLM_API_KEY"):
        return env["FLUID_LLM_API_KEY"]
    env_var = PROVIDER_ENV_VARS.get(provider)
    if env_var:
        key = env.get(env_var)
        if key:
            return key
    # Gemini accepts either the Forge-centric alias or Google's native env var.
    if provider == "gemini":
        key = env.get("GEMINI_API_KEY")
        if key:
            return key
        key = env.get("GOOGLE_API_KEY")
        if key:
            return key
    # Fallback: check the OS keyring for a saved key. Resolve via
    # the host module so test patches on
    # ``fluid_build.cli.forge_copilot_llm_providers._get_api_key_from_keyring``
    # flow through.
    keyring_fn = getattr(_host(), "_get_api_key_from_keyring", _get_api_key_from_keyring)
    return keyring_fn(provider)


# ---------------------------------------------------------------------------
# Keyring helpers
# ---------------------------------------------------------------------------

_LLM_KEYRING_PREFIX = "llm"


def _keyring_key(provider: str) -> str:
    return f"{_LLM_KEYRING_PREFIX}.{provider}.api_key"


def _get_api_key_from_keyring(provider: str) -> Optional[str]:
    """Retrieve a saved LLM API key from the OS keyring."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        return KeyringCredentialStore.get_credential(_keyring_key(provider))
    except Exception:  # noqa: BLE001
        LOG.debug("Keyring read failed for %s", _keyring_key(provider))
        return None


def save_api_key_to_keyring(provider: str, api_key: str) -> bool:
    """Persist an LLM API key in the OS keyring for future runs."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.set_credential(_keyring_key(provider), api_key)
        return True
    except Exception:  # noqa: BLE001
        LOG.debug("Keyring write failed for %s", _keyring_key(provider))
        return False


def clear_api_key_from_keyring(provider: str) -> bool:
    """Remove a saved LLM API key from the OS keyring."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.delete_credential(_keyring_key(provider))
        return True
    except Exception:  # noqa: BLE001
        LOG.debug("Keyring delete failed for %s", _keyring_key(provider))
        return False
