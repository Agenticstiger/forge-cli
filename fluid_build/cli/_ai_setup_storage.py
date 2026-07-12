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
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

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

# ``ai_config.json`` schema version. v2 is the multi-provider map
# ``{"version": 2, "active": <name>, "providers": {<name>: {<entry>}}}``.
# The pre-v2 single-provider shape ``{"provider": .., "model": ..}`` is
# still read transparently (see :func:`_normalize_config`) so upgrades
# never strand an existing config.
_CONFIG_SCHEMA_VERSION = 2

# Top-level keys that are NOT part of a per-provider entry — used when
# folding the legacy single-provider shape into a one-entry map.
_MAP_META_KEYS = frozenset({"provider", "version", "active", "providers", "domain_history"})

# ── Domain-detection history (usage-based personalization) ──────────────
#
# Track which product domains a user keeps building so ``fluid forge`` can
# proactively nudge that domain's template on repeat usage. Only an
# aggregate ``count`` + ``last_seen`` timestamp is kept per domain SLUG —
# never prompt text, contract content, paths, or PII.
#
# Ranking is *frecency* (frequency + recency), the statistic Mozilla coined
# for Places history suggestions. We adapt the exponential-decay model that
# camdencheek/fre and Mozilla's post-Firefox-147 ranking use
# (``score = Σ weight · 2^(−age / half_life)``); because we store one
# aggregate row per domain (à la zoxide's ``rank`` + ``last_accessed``
# rather than every visit) it collapses to ``count · 2^(−age / half_life)``.
# Preferred over autojump's ``sqrt(freq² + w²)`` because that ignores time,
# so a domain built heavily a year ago would out-rank one built this week.
_DOMAIN_HISTORY_KEY = "domain_history"

# Detections before a domain's template is proactively suggested. Three
# repeats is a deliberate, conservative floor — enough signal to be sure the
# pattern is real, low enough to feel timely.
_DOMAIN_SUGGEST_THRESHOLD = 3

# Recency decay half-life (days): a domain's frecency halves every 30 days of
# inactivity, so a stale favourite gracefully yields to a fresh interest.
_DOMAIN_FRECENCY_HALF_LIFE_DAYS = 30.0

# Privacy guard: only a bare domain slug is ever persisted. Mirrors
# ``forge_domain_enrichment._SAFE_DOMAIN_RE``, lower-cased and length-capped —
# the regex is what keeps prompt text / paths / PII out of the history file.
_DOMAIN_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _allow_plaintext_ai_secrets() -> bool:
    """Return True when the operator explicitly opts into plaintext key persistence."""
    return os.environ.get(_plaintext_env_var(), "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _read_config_file() -> Optional[dict]:
    """Read + JSON-parse ``~/.fluid/ai_config.json``.

    Returns the raw ``dict`` (either shape) or ``None`` when the file is
    absent, unreadable, or not a JSON object.
    """
    import json

    try:
        cf = _config_file()
        if not cf.exists():
            return None
        data = json.loads(cf.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _normalize_config(data: Optional[dict]) -> dict:
    """Normalise either config shape to ``{"active": name|None, "providers": {name: entry}}``.

    Accepts the v2 multi-provider map *and* the legacy single-provider
    shape (``{"provider": .., "model": ..}``), folding the latter into a
    one-entry map so every reader sees the same structure. Each ``entry``
    holds the non-name fields (``model``, ``endpoint``, ``ollama_host``,
    ``tiered``, and — only under the opt-in plaintext fallback —
    ``api_key``).
    """
    if not isinstance(data, dict):
        return {"active": None, "providers": {}}

    raw_providers = data.get("providers")
    if isinstance(raw_providers, dict):
        providers = {
            name: dict(entry)
            for name, entry in raw_providers.items()
            if isinstance(name, str) and isinstance(entry, dict)
        }
        active = data.get("active")
        if active not in providers:
            active = next(iter(providers), None)
        return {"active": active, "providers": providers}

    # Legacy single-provider shape.
    provider = data.get("provider")
    if not isinstance(provider, str) or not provider:
        return {"active": None, "providers": {}}
    entry = {k: v for k, v in data.items() if k not in _MAP_META_KEYS}
    return {"active": provider, "providers": {provider: entry}}


def _write_config_dict(payload: dict) -> bool:
    """Serialise an arbitrary config dict to ``ai_config.json`` (mode 600).

    The single low-level writer — every save path funnels through here so the
    owner-only permission + atomic-write invariants live in one place.
    """
    import json

    from fluid_build.credentials.encrypted_store import _atomic_write_bytes

    try:
        _config_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        # Owner-only read/write from the start — ``_atomic_write_bytes``
        # opens the file 0o600, so a (possibly api_key-bearing) entry is
        # never world-readable, even briefly.
        _atomic_write_bytes(
            _config_file(),
            json.dumps(payload, indent=2).encode("utf-8"),
            mode=0o600,
        )
        LOG.debug("Saved AI config to %s (mode 600)", _config_file())
        return True
    except OSError as exc:
        LOG.debug("Could not save AI config: %s", exc)
        return False


def _write_config_map(active: Optional[str], providers: dict) -> bool:
    """Serialise the multi-provider map to ``ai_config.json`` (mode 600).

    Read-modify-write: any *sibling* top-level block already in the file
    (e.g. the ``domain_history`` usage stats) is preserved. A provider save
    must never clobber usage history any more than it clobbers a sibling
    provider entry — the write path is symmetric with the read path.
    """
    existing = _read_config_file()
    payload: dict = dict(existing) if isinstance(existing, dict) else {}
    payload["version"] = _CONFIG_SCHEMA_VERSION
    payload["active"] = active
    payload["providers"] = providers
    return _write_config_dict(payload)


def _save_ai_config(
    provider: str,
    model: str,
    *,
    api_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    ollama_host: Optional[str] = None,
) -> bool:
    """Save one provider's AI config into the multi-provider map.

    Records/updates *provider*'s entry and marks it the active/default,
    while **preserving every other provider's saved entry** — the second
    ``fluid ai setup --provider <x>`` must never clobber the first. The
    per-provider entry fully replaces any prior entry for the same
    provider (matching the pre-map single-write semantics).

    Provider/model choices live in the JSON file. API keys are persisted
    to the OS keyring whenever possible; each provider gets a distinct
    keyring entry (``llm_api_key.<provider>``). Plaintext key fallback is
    intentionally opt-in via ``FLUID_ALLOW_PLAINTEXT_AI_SECRETS=1`` so
    automated and agent-facing workflows don't quietly leave live
    provider tokens on disk.
    """
    # Resolve ``_save_key_to_keyring`` via the canonical
    # ``cli.ai_setup`` namespace so test patches on
    # ``fluid_build.cli.ai_setup._save_key_to_keyring`` flow through
    # to this caller (matches the pattern used in
    # ``_init_interactive_helpers`` etc.).
    from fluid_build.cli import ai_setup as _as

    save_key_fn = getattr(_as, "_save_key_to_keyring", _save_key_to_keyring)

    # Load the existing map first so sibling providers survive.
    normalized = _normalize_config(_read_config_file())
    providers: dict = normalized["providers"]

    entry: dict = {"model": model}
    if api_key:
        saved_to_keyring = save_key_fn(provider, api_key)
        if saved_to_keyring:
            LOG.debug("Saved API key to keyring; not writing it to %s", _config_file())
        elif _allow_plaintext_ai_secrets():
            entry["api_key"] = api_key
            LOG.warning("Plaintext local AI credential fallback is enabled.")
        else:
            LOG.debug("Keyring unavailable; sensitive AI value was not persisted.")
    if endpoint:
        entry["endpoint"] = endpoint
    if ollama_host:
        entry["ollama_host"] = ollama_host

    providers[provider] = entry
    return _write_config_map(active=provider, providers=providers)


def _load_ai_config() -> Optional[dict]:
    """Load the ACTIVE provider's saved AI preferences (back-compat shape).

    Returns the flat ``{"provider": .., "model": .., ...}`` dict every
    existing caller expects, or ``None`` when nothing is configured.

    Lookup order:

    1. ``~/.fluid/config.yaml`` ``llm:`` section (unified path).
    2. ``~/.fluid/ai_config.json`` — the active entry of the
       multi-provider map (or the sole entry of a legacy file).
    3. ``None`` — no config saved yet.
    """
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

    # 2. ``~/.fluid/ai_config.json`` — active provider from the map.
    normalized = _normalize_config(_read_config_file())
    active = normalized["active"]
    if not active:
        return None
    entry = normalized["providers"].get(active) or {}
    return {"provider": active, **entry}


def _load_ai_config_map() -> Optional[dict]:
    """Return the full multi-provider view, or ``None`` when unconfigured.

    Shape: ``{"active": <name>, "providers": {<name>: {"provider": <name>,
    "model": .., ...}}}`` — each entry is flattened to carry its own
    ``provider`` name so callers can iterate uniformly.
    """
    normalized = _normalize_config(_read_config_file())
    providers = normalized["providers"]
    if not providers:
        return None
    return {
        "active": normalized["active"],
        "providers": {name: {"provider": name, **entry} for name, entry in providers.items()},
    }


def _load_ai_config_for(provider: str) -> Optional[dict]:
    """Return the flat config dict for a SPECIFIC saved provider, or ``None``.

    Used by the resolver so ``fluid forge --llm-provider <name>`` can pick
    the requested provider out of the map regardless of which one is
    active.
    """
    if not provider:
        return None
    normalized = _normalize_config(_read_config_file())
    entry = normalized["providers"].get(provider)
    if entry is None:
        return None
    return {"provider": provider, **entry}


def _list_configured_providers() -> list:
    """Return the sorted names of every provider saved in the map."""
    normalized = _normalize_config(_read_config_file())
    return sorted(normalized["providers"].keys())


def _set_active_provider(provider: str) -> bool:
    """Switch the active/default provider (like ``gh auth switch``).

    Returns ``True`` when *provider* is present in the map and the active
    marker was updated; ``False`` for an unknown provider (no-op).
    """
    normalized = _normalize_config(_read_config_file())
    if provider not in normalized["providers"]:
        return False
    return _write_config_map(active=provider, providers=normalized["providers"])


def _remove_provider(provider: str) -> bool:
    """Drop *provider* from the multi-provider map (config side only).

    Reassigns the active marker to a surviving provider when the removed
    one was active, and deletes the file entirely when it was the last
    entry. The keyring secret is cleared separately by the caller (via
    :func:`_clear_key_from_keyring`). Returns ``True`` when an entry was
    removed.
    """
    normalized = _normalize_config(_read_config_file())
    providers = normalized["providers"]
    if provider not in providers:
        return False
    del providers[provider]
    if not providers:
        _clear_ai_config()
        return True
    active = normalized["active"]
    if active == provider or active not in providers:
        active = next(iter(providers), None)
    _write_config_map(active=active, providers=providers)
    return True


def _clear_ai_config() -> None:
    """Delete the saved AI config file."""
    try:
        cf = _config_file()
        if cf.exists():
            cf.unlink()
            LOG.debug("Deleted AI config at %s", cf)
    except OSError as exc:
        LOG.debug("Could not delete AI config: %s", exc)


# ── Domain-detection history ────────────────────────────────────────────


def _now_utc() -> datetime:
    """Timezone-aware UTC now — the single clock source (test seam)."""
    return datetime.now(timezone.utc)


def _resolve_domain_slug(domain_or_context: Any) -> Optional[str]:
    """Reduce a slug string *or* an interview context to a validated slug.

    The one gate through which anything reaches the persisted history — it
    never returns anything but a bare ``[a-z0-9._-]`` slug, which is what
    keeps prompt text / paths / PII out of the file. A ``Mapping`` is run
    through the shared :func:`detect_domain` detector (deferred import so the
    ``fluid --help`` cold path never pulls in the YAML keyword loader);
    anything else, or an unrecognised domain, yields ``None``.
    """
    if isinstance(domain_or_context, str):
        candidate: Optional[str] = domain_or_context
    elif isinstance(domain_or_context, Mapping):
        try:
            from fluid_build.cli.forge_domain_enrichment import detect_domain

            candidate = detect_domain(dict(domain_or_context))
        except Exception:  # noqa: BLE001 — detection is best-effort
            candidate = None
    else:
        candidate = None

    if not candidate:
        return None
    slug = str(candidate).strip().lower()
    return slug if _DOMAIN_SLUG_RE.match(slug) else None


def record_domain_detection(
    domain_or_context: Any,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Record one domain detection under ``domain_history`` in ``ai_config.json``.

    Accepts either an already-resolved domain slug or the interview *context*
    (from which the slug is derived via :func:`detect_domain`). Increments the
    domain's running ``count`` and refreshes ``last_seen`` (ISO-8601 UTC),
    **preserving every other key in the file** — provider entries, the active
    marker, and every sibling ``domain_history`` domain all survive the
    read-modify-write. Returns the recorded slug, or ``None`` when no valid
    slug could be resolved (in which case the file is left untouched).

    Privacy: only the domain slug + count + timestamp are ever written — never
    prompt text, contract content, paths, or PII.
    """
    slug = _resolve_domain_slug(domain_or_context)
    if slug is None:
        return None

    data = _read_config_file() or {}
    history = data.get(_DOMAIN_HISTORY_KEY)
    if not isinstance(history, dict):
        history = {}

    prior = 0
    entry = history.get(slug)
    if isinstance(entry, dict):
        try:
            prior = max(int(entry.get("count", 0)), 0)
        except (TypeError, ValueError):
            prior = 0

    stamp = (now or _now_utc()).astimezone(timezone.utc)
    history[slug] = {"count": prior + 1, "last_seen": stamp.isoformat()}
    data[_DOMAIN_HISTORY_KEY] = history

    if _write_config_dict(data):
        # Log only the input-derived, regex-validated slug — NOT the count,
        # which is read from the shared ai_config (that file can hold a
        # gated api_key), so CodeQL taints any value sourced from it
        # (py/clear-text-logging-sensitive-data). Repo convention is to
        # avoid interpolating config-sourced values, not to suppress.
        LOG.debug("Recorded domain detection: %s", slug)
        return slug
    return None


def get_domain_history() -> dict:
    """Return a read-only snapshot of ``{slug: {"count": n, "last_seen": iso}}``.

    Empty dict when nothing has been recorded. The returned dict is a copy;
    mutating it does not touch the file. Malformed rows are skipped so a
    hand-edited file can't crash a reader.
    """
    data = _read_config_file() or {}
    history = data.get(_DOMAIN_HISTORY_KEY)
    if not isinstance(history, dict):
        return {}
    return {
        slug: dict(entry)
        for slug, entry in history.items()
        if isinstance(slug, str) and isinstance(entry, dict)
    }


def _frecency_score(entry: dict, *, now: datetime) -> float:
    """Frequency × recency for one history entry.

    Exponential-decay model (camdencheek/fre; Mozilla Places post-FF147):
    ``count · 2^(−age_days / half_life)``. A domain seen often but long ago
    decays below one seen slightly less often but recently — the whole point
    of frecency over a raw max-count.
    """
    try:
        count = int(entry.get("count", 0))
    except (TypeError, ValueError):
        return 0.0
    if count <= 0:
        return 0.0

    age_days = 0.0
    last_seen = entry.get("last_seen")
    if isinstance(last_seen, str):
        try:
            seen = datetime.fromisoformat(last_seen)
        except ValueError:
            seen = None
        if seen is not None:
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            age_days = max((now - seen).total_seconds(), 0.0) / 86400.0

    decay = 0.5 ** (age_days / _DOMAIN_FRECENCY_HALF_LIFE_DAYS)
    return count * decay


def suggest_domain_template(
    exclude: Optional[str] = None,
    *,
    threshold: int = _DOMAIN_SUGGEST_THRESHOLD,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return the domain slug to proactively nudge a template for, or ``None``.

    A domain becomes a candidate once it has been detected at least
    *threshold* times (default 3). Among candidates the highest-*frecency*
    domain wins — frequency weighted by recency — so a burst of recent
    activity out-ranks a stale historical favourite. *exclude* drops the
    run's already-active domain so we never nag about the pack that is
    already loaded. Returns ``None`` before any domain crosses the threshold
    (the neutral / no-suggestion state).
    """
    threshold = max(threshold, 1)
    history = get_domain_history()
    if not history:
        return None

    ref = (now or _now_utc()).astimezone(timezone.utc)
    exclude_slug = exclude.strip().lower() if isinstance(exclude, str) else None

    best_slug: Optional[str] = None
    best_score = 0.0
    for slug, entry in history.items():
        if slug == exclude_slug:
            continue
        try:
            count = int(entry.get("count", 0))
        except (TypeError, ValueError):
            continue
        if count < threshold:
            continue
        score = _frecency_score(entry, now=ref)
        if score > best_score:
            best_score = score
            best_slug = slug
    return best_slug


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
    "_list_configured_providers",
    "_load_ai_config",
    "_load_ai_config_for",
    "_load_ai_config_map",
    "_load_key_from_keyring",
    "_remove_provider",
    "_save_ai_config",
    "_save_key_to_keyring",
    "_set_active_provider",
    "get_domain_history",
    "record_domain_detection",
    "suggest_domain_template",
]
