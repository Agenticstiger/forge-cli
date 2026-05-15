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

"""``fluid ai setup`` config + keyring storage — physical extraction.

Lifted from ``cli/ai_setup.py`` (the host file was 1888 LOC; this
~155 LOC of pure storage I/O has no UI coupling). The exported
helpers:

* :func:`_save_ai_config` — write provider/model + endpoint to
  ``~/.fluid/ai_config.json`` (mode 600); API key goes to keyring.
* :func:`_load_ai_config` — read with ``unified_config`` priority,
  then the ``ai_config.json`` file.
* :func:`_clear_ai_config` — delete the JSON file (best-effort).
* :func:`_save_key_to_keyring`, :func:`_load_key_from_keyring`,
  :func:`_clear_key_from_keyring` — keyring round-trips.
* :func:`_allow_plaintext_ai_secrets` — env-var gate for plaintext
  fallback.

``ai_setup.py`` re-imports each at module top so existing test
patches that target ``fluid_build.cli.ai_setup.<helper>`` keep
resolving via the namespace. Constants (``_CONFIG_DIR``,
``_CONFIG_FILE``, ``_KEYRING_PREFIX``,
``_PLAINTEXT_AI_SECRETS_ENV``) are read via attribute access on the
canonical ``cli.ai_setup`` module so test-time patches flow through.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

LOG = logging.getLogger("fluid.cli.ai_setup.storage")


# ── Indirection accessors ───────────────────────────────────────────────


def _config_dir():
    from fluid_build.cli import ai_setup as _as

    return _as._CONFIG_DIR


def _config_file():
    from fluid_build.cli import ai_setup as _as

    return _as._CONFIG_FILE


def _keyring_prefix() -> str:
    from fluid_build.cli import ai_setup as _as

    return _as._KEYRING_PREFIX


def _plaintext_env_var() -> str:
    from fluid_build.cli import ai_setup as _as

    return _as._PLAINTEXT_AI_SECRETS_ENV


# ── Config storage ──────────────────────────────────────────────────────


def _allow_plaintext_ai_secrets() -> bool:
    """Return True when the operator explicitly opts into plaintext key persistence."""
    return os.environ.get(_plaintext_env_var(), "").strip().lower() in {
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
    fallback is intentionally opt-in via
    ``FLUID_ALLOW_PLAINTEXT_AI_SECRETS=1`` so automated and
    agent-facing workflows don't quietly leave live provider tokens
    on disk.
    """
    import json
    import stat

    # Resolve ``_save_key_to_keyring`` via the canonical
    # ``cli.ai_setup`` namespace so test patches on
    # ``fluid_build.cli.ai_setup._save_key_to_keyring`` flow through
    # to this caller (matches the pattern used in
    # ``_init_interactive_helpers`` etc.).
    from fluid_build.cli import ai_setup as _as

    save_key_fn = getattr(_as, "_save_key_to_keyring", _save_key_to_keyring)

    try:
        _config_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        data: dict = {"provider": provider, "model": model}
        if api_key:
            saved_to_keyring = save_key_fn(provider, api_key)
            if saved_to_keyring:
                LOG.debug("Saved API key to keyring; not writing it to %s", _config_file())
            elif _allow_plaintext_ai_secrets():
                data["api_key"] = api_key
                LOG.warning("Plaintext local AI credential fallback is enabled.")
            else:
                LOG.debug("Keyring unavailable; sensitive AI value was not persisted.")
        if endpoint:
            data["endpoint"] = endpoint
        if ollama_host:
            data["ollama_host"] = ollama_host
        _config_file().write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Owner-only read/write — protect the API key
        _config_file().chmod(stat.S_IRUSR | stat.S_IWUSR)
        LOG.debug("Saved AI config to %s (mode 600)", _config_file())
        return True
    except OSError as exc:
        LOG.debug("Could not save AI config: %s", exc)
        return False


def _load_ai_config() -> Optional[dict]:
    """Load saved AI preferences.  Returns None if no config exists.

    Lookup order:

    1. ``~/.fluid/config.yaml`` ``llm:`` section (unified path).
    2. ``~/.fluid/ai_config.json`` (the file ``_save_ai_config``
       writes provider/model/endpoint to).
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

    # 2. ``~/.fluid/ai_config.json`` — the file ``_save_ai_config`` writes.
    try:
        cf = _config_file()
        if not cf.exists():
            return None
        data = json.loads(cf.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("provider"):
            return data
        return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _clear_ai_config() -> None:
    """Delete the saved AI config file."""
    try:
        cf = _config_file()
        if cf.exists():
            cf.unlink()
            LOG.debug("Deleted AI config at %s", cf)
    except OSError as exc:
        LOG.debug("Could not delete AI config: %s", exc)


# ── Keyring round-trips ─────────────────────────────────────────────────


def _log_keyring_action(action: str) -> None:
    """Log a keyring action with constant label only.

    Don't interpolate provider/api_key/exc — CodeQL py/clear-text-
    logging-sensitive-data flags any LOG site where ``api_key`` is in
    a caller's frame. Logging only the constant action verb breaks
    the taint flow.
    """
    LOG.debug("Keyring %s", action)


def _save_key_to_keyring(provider: str, api_key: str) -> bool:
    """Persist *api_key* in the OS keyring.  Returns True on success."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.set_credential(f"{_keyring_prefix()}.{provider}", api_key)
        _log_keyring_action("saved")
        return True
    except ImportError:
        _log_keyring_action("save_unavailable")
        return False
    except OSError:
        _log_keyring_action("save_oserror")
        return False
    except Exception:  # noqa: BLE001 — keyring backends can raise anything
        _log_keyring_action("save_failed")
        return False


def _load_key_from_keyring(provider: str) -> Optional[str]:
    """Load a previously saved API key from the OS keyring."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        return KeyringCredentialStore.get_credential(f"{_keyring_prefix()}.{provider}")
    except (ImportError, OSError):
        _log_keyring_action("load_failed")
        return None
    except Exception:  # noqa: BLE001 — keyring backends can raise anything
        _log_keyring_action("load_unexpected")
        return None


def _clear_key_from_keyring(provider: str) -> bool:
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.delete_credential(f"{_keyring_prefix()}.{provider}")
        return True
    except (ImportError, OSError):
        _log_keyring_action("clear_failed")
        return False
    except Exception:  # noqa: BLE001
        _log_keyring_action("clear_unexpected")
        return False


__all__ = [
    "_allow_plaintext_ai_secrets",
    "_clear_ai_config",
    "_clear_key_from_keyring",
    "_load_ai_config",
    "_load_key_from_keyring",
    "_save_ai_config",
    "_save_key_to_keyring",
]
