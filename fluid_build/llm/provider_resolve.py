# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""LLM provider + API-key resolution helpers — physical extraction.

Lifted from ``cli/forge_copilot_llm_providers.py`` (host file was
1550 LOC). ~130 LOC of pure provider-inference + keyring round-trips.

* :func:`_infer_provider_from_explicit_keys` /
  :func:`_infer_provider_from_ambient` /
  :func:`_infer_provider_from_env` /
  :func:`_infer_provider_from_keyring` — pick a provider hint from
  whatever signal we can find.
* :func:`_resolve_api_key` — env var first, keyring fallback;
  ``FLUID_LLM_API_KEY`` only for a provider the operator named.
* :func:`save_api_key_to_keyring` / :func:`clear_api_key_from_keyring`
  / :func:`_get_api_key_from_keyring` / :func:`_keyring_key` —
  keyring round-trips for cloud-provider secrets.

The host module re-imports each at module top so existing call sites
keep resolving.
"""

# ruff: noqa: F821 — this helper resolves host-module symbols
# (PROVIDER_ENV_VARS, BUILTIN_LLM_PROVIDERS, etc.) at call-time via
# a ``_host()`` indirection accessor; ruff cannot statically see those
# bindings.

from __future__ import annotations

import logging
import shutil
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

LOG = logging.getLogger("fluid.cli.llm_providers.resolve")


def _host():
    from fluid_build.llm import providers as _llm

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


# Each built-in provider and the explicit variables that name it. Exactly one
# provider named this way selects it; several select none, and
# :func:`no_provider_message` says so.
_EXPLICIT_KEY_VARS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("openai", ("OPENAI_API_KEY",)),
    ("anthropic", ("ANTHROPIC_API_KEY",)),
    ("gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ("ollama", ("OLLAMA_HOST",)),
)


def _explicit_key_signals(env: Mapping[str, str]) -> Dict[str, List[str]]:
    """Each provider an explicit variable names, with the names of the ones set."""
    signals: Dict[str, List[str]] = {}
    for provider, names in _EXPLICIT_KEY_VARS:
        present = [name for name in names if env.get(name)]
        if present:
            signals[provider] = present
    return signals


def ambiguous_keys_message(env: Mapping[str, str]) -> Optional[str]:
    """Why the key variables select no provider, when they name two or more.

    ``None`` when they name one provider or none. Forge, ``fluid doctor``,
    interactive setup and ``fluid ai test`` all stop with this message. None of
    them picks one of the keys, or a running Ollama, in the operator's place
    (founder decision, 2026-11-12: stop and name the keys).
    """
    signals = _explicit_key_signals(env)
    if len(signals) < 2:
        return None
    names = ", ".join(name for present in signals.values() for name in present)
    return (
        f"No LLM provider is selected: {names} are all set, so none of them is "
        "chosen. Pick one with --llm-provider or FLUID_LLM_PROVIDER."
    )


def _canonical_provider_name(name: Any) -> Optional[str]:
    """*name* as the provider registry spells it, ``None`` when it is not a name.

    A hand-written ``~/.fluid/config.yaml`` may say ``claude`` or ``claude_code``;
    the registry answers ``anthropic`` and ``claude-code``. A name it does not
    know (a plugin provider) comes back as written.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    registry = getattr(_host(), "BUILTIN_LLM_PROVIDERS", {})
    key = name.strip().lower()
    provider = registry.get(key) or registry.get(key.replace("_", "-"))
    return provider.name if provider is not None else name.strip()


def _saved_provider_name() -> Optional[str]:
    """The provider ``fluid ai setup`` saved, if any (``~/.fluid``), by its canonical name."""
    try:
        from fluid_build._ai_config_shared import load_ai_config

        name = (load_ai_config() or {}).get("provider")
    except Exception:  # noqa: BLE001 — an unreadable config names nothing
        return None
    return _canonical_provider_name(name)


def _saved_choice_for(args: Any, env: Dict[str, str], provider: str) -> dict:
    """The active ``fluid ai setup`` entry when it is *provider*'s, else ``{}``.

    It fills in what a run does not state, for that provider only: its model,
    and its Ollama host, set in *env* here when ``OLLAMA_HOST`` is not. A run
    that names its provider (``--llm-provider``, ``FLUID_LLM_PROVIDER``,
    ``FLUID_FORGE_AGENT``) takes nothing from it: a named provider without a
    stated model gets its catalog model, which Command Center's drafting call
    relies on. Shared by ``check_llm_readiness`` and ``resolve_llm_config``.
    """
    if _host()._explicit_provider_name(args, env):
        return {}
    try:
        from fluid_build._ai_config_shared import load_ai_config

        saved = load_ai_config()
    except Exception:  # noqa: BLE001 — an unreadable config supplies nothing
        return {}
    if not saved or _canonical_provider_name(saved.get("provider")) != provider:
        return {}
    if provider == "ollama" and saved.get("ollama_host") and not env.get("OLLAMA_HOST"):
        env["OLLAMA_HOST"] = saved["ollama_host"]
    return saved


def _passed_over(saved: str, allow_agents: bool) -> bool:
    """Whether a run passes over the saved provider *saved*.

    Only an agent (a coding-agent CLI or MCP sampling), and only where the keyless
    auto-pick is off too (``allow_agents=False``): an agent cannot call a stated
    endpoint, may not get a stated model meant for another LLM, and has no tool use
    for the agent loop.
    """
    return not allow_agents and saved in _host()._NON_LITELLM_PROVIDERS


def no_provider_message(env: Mapping[str, str]) -> str:
    """Why no LLM provider is selected. ``fluid doctor`` and forge both say this."""
    saved = _saved_provider_name()
    if saved in _host()._NON_LITELLM_PROVIDERS:
        # The one saved choice the ladder passes over (:func:`_passed_over`).
        return (
            f"No LLM provider is selected: 'fluid ai setup' saved {saved}, and an agent "
            "is not used when the run states an endpoint or a model, or runs the agent "
            "loop. Name a provider with --llm-provider."
        )
    ambiguous = ambiguous_keys_message(env)
    if ambiguous:
        return ambiguous
    if env.get("FLUID_LLM_API_KEY"):
        return (
            "No LLM provider is selected: FLUID_LLM_API_KEY does not say which provider "
            "the key is for. Pick one with --llm-provider or FLUID_LLM_PROVIDER."
        )
    return "No LLM provider configured. Run 'fluid ai setup' or set an API key env var."


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

    signals = _explicit_key_signals(env)
    if len(signals) == 1:
        return next(iter(signals))
    return None


def _is_non_interactive(non_interactive: bool = False) -> bool:
    """True when the run cannot answer a prompt: ``--non-interactive`` or no TTY on stdin."""
    if non_interactive:
        return True
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):  # no stdin, or a closed one
        return True


# Variables that mean the operator configured an LLM: an API key, or a stated
# endpoint, model or Ollama host. A coding agent is never auto-picked over any of them, so a
# run never sends its prompt somewhere other than the destination it was given.
_CONFIG_ENV_VARS = (
    "FLUID_LLM_API_KEY",
    "FLUID_LLM_ENDPOINT",
    "FLUID_LLM_MODEL",
    "FLUID_LLM_ROUTING_ENDPOINT",
    "FLUID_LLM_ROUTING_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OLLAMA_HOST",
)


def _configured_by_env(env: Mapping[str, str]) -> bool:
    """Whether the environment configures an LLM (see :data:`_CONFIG_ENV_VARS`).

    Every provider's key in the host's ``PROVIDER_ENV_VARS`` counts too.
    """
    provider_keys = tuple(getattr(_host(), "PROVIDER_ENV_VARS", {}).values())
    return any(env.get(var) for var in _CONFIG_ENV_VARS + provider_keys)


def _operator_configured_a_provider(env: Mapping[str, str]) -> bool:
    """Whether anything but ambient discovery already names a provider.

    An API-key, endpoint or model env var (even several keys, which
    :func:`_infer_provider_from_explicit_keys` refuses to choose between), a
    keyring key, or a saved ``~/.fluid`` provider. A coding agent is never
    auto-picked over any of these.
    """
    if _configured_by_env(env):
        return True
    keyring_fn = getattr(_host(), "_get_api_key_from_keyring", _get_api_key_from_keyring)
    if any(keyring_fn(name) for name in ("openai", "anthropic", "gemini")):
        return True
    try:
        from fluid_build._ai_config_shared import load_ai_config

        saved = load_ai_config()
    except Exception:  # noqa: BLE001 — detection must never hard-fail
        return False
    return bool(saved and saved.get("provider"))


def _first_coding_agent_on_path(env: Mapping[str, str]) -> Optional[str]:
    """The first installed coding-agent CLI, in ``CODING_AGENT_NAMES`` order."""
    from fluid_build.llm.coding_agent import _AGENT_SPECS, CODING_AGENT_NAMES

    # Search the PATH of the environment we were given, never the process's:
    # an environment without PATH finds nothing, as it would for a child process.
    path = env.get("PATH", "")
    for name in CODING_AGENT_NAMES:
        if shutil.which(_AGENT_SPECS[name].binary, path=path):
            return name
    return None


def _lean_keyless_autopick(
    env: Mapping[str, str], *, non_interactive: bool = False, allow_agents: bool = True
) -> Optional[str]:
    """The agent a run without litellm selects, else ``None``.

    The selection ladder without its keyring read, so the litellm preflight
    stays cheap. A key variable, a keyring key and Ollama all need litellm, so
    two rungs can answer: an agent ``fluid ai setup`` saved, and the keyless
    auto-pick (a non-interactive run, no configuring variable, no saved
    provider, an agent CLI on PATH, and no local Ollama: a running one is never
    silently swapped for a cloud agent). ``allow_agents=False`` (the agent loop,
    or a stated endpoint or model) disables both.
    """
    if _infer_provider_from_explicit_keys(env):
        return None
    saved = _saved_provider_name()
    if saved:
        # Below the saved rung only providers that need litellm remain.
        if saved in _host()._NON_LITELLM_PROVIDERS and not _passed_over(saved, allow_agents):
            return saved
        return None
    if not allow_agents or not _is_non_interactive(non_interactive):
        return None
    if _configured_by_env(env):
        return None
    agent = _first_coding_agent_on_path(env)
    if agent is None:
        return None
    detect_fn = getattr(_host(), "detect_ollama_available", None)
    if detect_fn is not None and detect_fn(env):
        return None
    return agent


def _infer_provider_from_ambient(
    env: Mapping[str, str], *, non_interactive: bool = False, allow_agents: bool = True
) -> Optional[str]:
    """Last-resort detection, shared by the readiness ladder and the real selection.

    1. Ollama already running on localhost: data stays local.
    2. Non-interactive runs only, and only when nothing is configured: the first
       coding-agent CLI on PATH. Interactive runs keep "offer, don't force"
       (the ``fluid ai setup`` inline offer). ``allow_agents=False`` skips this
       rung: for the agent loop, which needs tool use, and when the operator
       stated an endpoint or model.

    Neither rung answers when key variables name two or more providers
    (:func:`ambiguous_keys_message`): the operator set those keys, and a
    running Ollama is not a choice between them. The run stops and names them.

    Only call this AFTER the saved config and the keyring have been
    consulted — ambient services must not override an explicit
    operator choice.  See :func:`_infer_provider_from_env` for the full
    resolution ladder.
    """

    if len(_explicit_key_signals(env)) > 1:
        # Checked before the Ollama probe, so nothing is dialled either.
        return None
    # Resolve via the host module: ``detect_ollama_available`` is
    # defined later in the host's module body, so an import-time
    # binding would be ``None``. Late-resolution at call time picks
    # up the real function.
    detect_fn = getattr(_host(), "detect_ollama_available", None)
    if detect_fn is not None and detect_fn(env):
        return "ollama"
    if (
        allow_agents
        and _is_non_interactive(non_interactive)
        and not _operator_configured_a_provider(env)
    ):
        return _first_coding_agent_on_path(env)
    return None


def _infer_provider_from_env(
    env: Mapping[str, str], *, non_interactive: bool = False, allow_agents: bool = True
) -> Optional[str]:
    """The provider the environment selects when no provider is named.

    1. Exactly one explicit key variable: a deliberate per-run choice.
    2. The provider ``fluid ai setup`` saved, unless :func:`_passed_over`.
    3. Exactly one keyring key.
    4. Ambient discovery (:func:`_infer_provider_from_ambient`), except when
       key variables name two or more providers: then nothing is selected.

    The rest of the ladder after a named provider, shared by the real selection
    and :func:`check_llm_readiness`, so ``fluid doctor`` names what a run uses.
    """
    explicit = _infer_provider_from_explicit_keys(env)
    if explicit:
        return explicit
    saved = _saved_provider_name()
    if saved and not _passed_over(saved, allow_agents):
        return saved
    keyring_match = _infer_provider_from_keyring()
    if keyring_match:
        return keyring_match
    return _infer_provider_from_ambient(
        env, non_interactive=non_interactive, allow_agents=allow_agents
    )


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


def _resolve_api_key(
    provider: str, env: Mapping[str, str], *, named: bool = False
) -> Optional[str]:
    """The API key for *provider*: ``FLUID_LLM_API_KEY``, its own variable, the keyring.

    ``FLUID_LLM_API_KEY`` does not say which provider the key is for, so it is
    read only when *named* is true: the operator named *provider* for this run
    (``--llm-provider``, ``FLUID_LLM_PROVIDER``). A provider that forge found
    itself (a running Ollama, ``OLLAMA_HOST``, the one keyring key, the one
    provider key variable, interactive setup's pick) gets its own key or none,
    never a key that may belong to another vendor.
    """
    if named and env.get("FLUID_LLM_API_KEY"):
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
    # ``fluid_build.llm.providers._get_api_key_from_keyring``
    # flow through. ``_get_api_key_from_keyring`` checks BOTH the native
    # ``llm.<provider>.api_key`` scheme and the ``fluid ai setup``
    # ``llm_api_key.<provider>`` scheme, so a key saved by ``fluid ai
    # setup --provider <name>`` resolves here for the requested provider.
    keyring_fn = getattr(_host(), "_get_api_key_from_keyring", _get_api_key_from_keyring)
    key = keyring_fn(provider)
    if key:
        return key
    # Last resort: the opt-in plaintext entry in ``ai_config.json``. Gated
    # on ``FLUID_ALLOW_PLAINTEXT_AI_SECRETS`` (same gate that governs
    # *writing* it) so the default keyring-only posture is unchanged and
    # no on-disk config is read unless the operator opted into plaintext.
    return _resolve_plaintext_saved_key(provider, env)


def _resolve_plaintext_saved_key(provider: str, env: Mapping[str, str]) -> Optional[str]:
    """Return a provider's opt-in plaintext key from ``ai_config.json``.

    Only active when ``FLUID_ALLOW_PLAINTEXT_AI_SECRETS`` is truthy in
    *env*; otherwise a no-op returning ``None`` (keyring-first posture).
    """
    flag = env.get("FLUID_ALLOW_PLAINTEXT_AI_SECRETS", "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return None
    try:
        # Read the specific provider's saved entry through the tier-0
        # ``_ai_config_shared`` leaf — the same function ``_ai_setup_storage``'s
        # ``_load_ai_config_for`` delegates to — so the LLM runtime carries no
        # ``cli`` back-edge.
        from fluid_build._ai_config_shared import load_ai_config_for

        entry = load_ai_config_for(provider)
        return entry.get("api_key") if entry else None
    except Exception:  # noqa: BLE001 — resolution must never hard-fail
        return None


# ---------------------------------------------------------------------------
# Keyring helpers
# ---------------------------------------------------------------------------

_LLM_KEYRING_PREFIX = "llm"

# Second keyring key-scheme: the one ``fluid ai setup`` writes under
# (``cli/_ai_setup_storage.py::_save_key_to_keyring`` →
# ``ai_setup._KEYRING_PREFIX``). Mirrored here — like the two parallel
# redaction layers — so the resolver can find a key saved by
# ``fluid ai setup`` for ANY provider, not just the active one. If
# ``ai_setup._KEYRING_PREFIX`` ever changes, update this constant too.
_AI_SETUP_KEYRING_PREFIX = "llm_api_key"


def _keyring_key(provider: str) -> str:
    return f"{_LLM_KEYRING_PREFIX}.{provider}.api_key"


def _ai_setup_keyring_key(provider: str) -> str:
    """Keyring key under the ``fluid ai setup`` scheme (``llm_api_key.<provider>``)."""
    return f"{_AI_SETUP_KEYRING_PREFIX}.{provider}"


def _log_keyring_failure(action: str) -> None:
    """Log a keyring failure with action label only — no provider name.

    The provider identifier is intentionally NOT interpolated. Even
    though "openai" / "anthropic" / etc. aren't secrets, CodeQL's
    interprocedural taint tracking flags the LOG sink whenever an
    ``api_key`` parameter is in any caller's frame, because it can't
    prove that the value being logged is the safe one. Logging only
    a constant action verb removes ambiguity.
    """
    LOG.debug("Keyring %s failed", action)


def _get_api_key_from_keyring(provider: str) -> Optional[str]:
    """Retrieve a saved LLM API key from the OS keyring.

    Checks both key-schemes so a key stored by ``fluid ai setup``
    (``llm_api_key.<provider>``) resolves under ``fluid forge
    --llm-provider <provider>`` even though ``fluid forge`` itself writes
    under ``llm.<provider>.api_key``. The native scheme is preferred.
    """
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        value = KeyringCredentialStore.get_credential(_keyring_key(provider))
        if value:
            return value
        return KeyringCredentialStore.get_credential(_ai_setup_keyring_key(provider))
    except Exception:  # noqa: BLE001
        _log_keyring_failure("read")
        return None


def save_api_key_to_keyring(provider: str, api_key: str) -> bool:
    """Persist an LLM API key in the OS keyring for future runs."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.set_credential(_keyring_key(provider), api_key)
        return True
    except Exception:  # noqa: BLE001
        _log_keyring_failure("write")
        return False


def clear_api_key_from_keyring(provider: str) -> bool:
    """Remove a saved LLM API key from the OS keyring."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.delete_credential(_keyring_key(provider))
        return True
    except Exception:  # noqa: BLE001
        _log_keyring_failure("delete")
        return False


# Bound after this module's own definitions: ``fluid_build.llm.providers``
# imports names from here when it loads, so binding first made
# ``import fluid_build.llm.provider_resolve`` fail when run first (circular
# import). The bound names are only read at call time.
_bind_constants_from_host()
