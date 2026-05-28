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

"""Personal memory for individual engineers (schema v1).

Stores per-user preferences in ``~/.fluid/personal-memory.json`` with a
namespaced ``preferences`` / ``history`` split and a standard envelope
(``schema_version``/``kind``/``generated_at``/``generated_by``).

This is a plain JSON file the user can edit or delete directly
(like ``~/.dbt/profiles.yml`` or ``~/.config/gh/hosts.yml``).

Schema
------

::

    {
      "schema_version": 1,
      "kind": "PersonalMemory",
      "generated_at": "2026-04-11T14:22:18Z",
      "generated_by": {"tool": "fluid-cli", "version": "0.7.9", "command": "fluid forge"},
      "preferences": {
        "provider": "gcp",
        "engine": "dbt",
        "domain": "retail",
        "owner_team": "data-platform",
        "ci_provider": "github_actions",
        "ci_complexity": "standard"
      },
      "history": {
        "recent_domains": ["retail", "marketing"],
        "recent_use_cases": ["activation", "reporting"]
      }
    }

Clean cut from the v0 ``engineer_memory.json`` layout
-----------------------------------------------------

v0's flat ``preferred_*`` / ``recent_*`` keys have been retired entirely.
There is **no legacy read fallback**: if an ``engineer_memory.json``
file exists in the user's ``~/.fluid/`` directory, it is ignored and
untouched.  The CLI simply starts fresh on the new path.  This is
deliberate (per the "clean cut" directive in the redesign plan).

Callers still see the same interface: :func:`load_personal_memory` and
:func:`save_personal_memory` with a context dict keyed on
``provider`` / ``build_engine`` / ``domain`` / ``owner_team`` /
``ci_provider`` / ``ci_complexity`` / ``use_case``.  The internal shape
change is invisible to them.
"""

from __future__ import annotations

__all__ = [
    "load_personal_memory",
    "save_personal_memory",
    "_sanitize_existing_personal_memory",
]

import json
import logging
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.cli.artifact_envelope import build_envelope
from fluid_build.cli.artifact_paths import user_personal_memory_path

LOG = logging.getLogger("fluid.cli.forge.personal_memory")

# Token signatures of test-double ``repr()`` strings that should never
# land in user-global personal memory.  A prior bug let a MagicMock's
# ``__repr__`` slip into ``~/.fluid/personal-memory.json`` (showing up
# in every subsequent streaming preview).  These tokens drive three
# defensive layers: (1) sanitise on load, (2) refuse to persist, and
# (3) a one-shot module-level cleanup helper invoked from conftest at
# session start so contributor laptops self-heal.
_POISON_TOKENS: tuple[str, ...] = ("<MagicMock", "<Mock ", "<NonCallableMagicMock")


#: Resolved lazily so tests can override ``FLUID_HOME`` after import.
def _memory_file() -> Path:
    return user_personal_memory_path()


#: Module-level alias kept for tests that historically patched
#: ``_MEMORY_FILE``.  New tests should use the ``FLUID_HOME`` env
#: override exposed by :mod:`fluid_build.cli.artifact_paths` instead.
_MEMORY_FILE = _memory_file()

_MAX_ITEMS = 5  # Max items per list (recent domains, use cases, etc.)


def load_personal_memory() -> Optional[Dict[str, Any]]:
    """Load personal preferences from ``~/.fluid/personal-memory.json``.

    Returns a flat dict with the v0 keys (``preferred_provider``,
    ``preferred_engine``, ``preferred_domain``, ``owner_team``,
    ``preferred_ci_provider``, ``preferred_ci_complexity``,
    ``recent_domains``, ``recent_use_cases``) so existing consumers
    throughout the CLI don't need to change.  The on-disk shape is v1
    namespaced; this function flattens it at the read boundary.

    Returns ``None`` if no memory file exists or the file is
    unparseable.

    Defensive: values that look like a test-double ``repr()`` (e.g.
    ``<MagicMock name=… id=…>``) are silently dropped on read.  See
    ``_POISON_TOKENS`` for the watch-list and ``save_personal_memory``
    for the symmetric write-time guard.
    """
    path = _MEMORY_FILE
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
    except (json.JSONDecodeError, OSError):
        return None

    raw = _strip_poisoned_values(raw)
    return _flatten_for_callers(raw)


def save_personal_memory(context: Dict[str, Any], console: Any = None) -> bool:
    """Update personal memory from the latest successful forge run.

    Merges new preferences into existing memory (never clobbers
    unrelated fields).  Shows a hint on first save telling the user
    where the file is.

    Raises ``ValueError`` if any value in *context* looks like a
    test-double ``repr()`` (see ``_POISON_TOKENS``) — failing loudly is
    better than silently writing garbage into user-global state.  The
    typical trigger is a test that forgot to ``configure_mock`` a
    return value and let the resulting ``MagicMock`` flow through to
    the writer.
    """
    _reject_poisoned_context(context)

    path = _MEMORY_FILE

    existing_raw = _read_existing_raw(path)
    is_first_save = existing_raw is None

    existing_prefs = (
        (existing_raw or {}).get("preferences") if isinstance(existing_raw, dict) else None
    ) or {}
    existing_history = (
        (existing_raw or {}).get("history") if isinstance(existing_raw, dict) else None
    ) or {}

    preferences: Dict[str, Any] = dict(existing_prefs)

    def _update(key: str, value: Any) -> None:
        if value is not None:
            preferences[key] = value

    _update("provider", context.get("provider"))
    _update("engine", context.get("build_engine") or context.get("engine"))
    _update("domain", context.get("domain"))
    _update("owner_team", context.get("owner_team"))
    _update("ci_provider", context.get("ci_provider"))
    _update("ci_complexity", context.get("ci_complexity"))

    # History lists (FIFO, deduplicated, bounded)
    history: Dict[str, Any] = {}
    history["recent_domains"] = _push_recent(
        existing_history.get("recent_domains") or [],
        context.get("domain"),
    )
    history["recent_use_cases"] = _push_recent(
        existing_history.get("recent_use_cases") or [],
        context.get("use_case"),
    )

    try:
        from fluid_build import __version__ as tool_version
    except Exception:  # pragma: no cover — defensive
        tool_version = ""

    envelope = build_envelope(
        kind="PersonalMemory",
        command="fluid forge",
        tool_version=str(tool_version),
    )
    document: Dict[str, Any] = {
        **envelope,
        "preferences": preferences,
        "history": history,
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        LOG.debug("Saved personal memory to %s", path)

        if is_first_save and console is not None:
            try:
                console.print(
                    f"\n[dim]Your preferences saved to {path}[/dim]\n"
                    "[dim](Edit or delete this file to reset your preferences.)[/dim]"
                )
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Could not print first-save hint: %s", exc)

        return True
    except OSError as exc:
        LOG.debug("Could not save personal memory: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_existing_raw(path: Path) -> Optional[Dict[str, Any]]:
    """Return the raw v1 document on disk, or ``None`` if absent/unparseable."""
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return raw
    except (json.JSONDecodeError, OSError):
        return None


def _push_recent(existing: list, value: Any) -> list:
    """Push *value* onto a FIFO list, deduplicating and bounding to _MAX_ITEMS."""
    result = list(existing)
    if value is None:
        return result[:_MAX_ITEMS]
    if value in result:
        return result[:_MAX_ITEMS]
    result.insert(0, value)
    return result[:_MAX_ITEMS]


def _flatten_for_callers(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Project the v1 on-disk shape onto the flat keys existing callers use.

    Other modules in the CLI (``forge_copilot_runtime``,
    ``forge_modes._resolve_ci_choice``, etc.) read keys like
    ``preferred_provider`` / ``preferred_ci_provider`` directly from the
    loaded dict.  Slice 5 preserves that interface — only the on-disk
    format changes.  When those modules eventually migrate to reading
    from ``preferences.*`` directly, this projection can be deleted.
    """
    flat: Dict[str, Any] = {}
    prefs = raw.get("preferences") or {}
    history = raw.get("history") or {}

    if isinstance(prefs, dict):
        if prefs.get("provider") is not None:
            flat["preferred_provider"] = prefs.get("provider")
        if prefs.get("engine") is not None:
            flat["preferred_engine"] = prefs.get("engine")
        if prefs.get("domain") is not None:
            flat["preferred_domain"] = prefs.get("domain")
        if prefs.get("owner_team") is not None:
            flat["owner_team"] = prefs.get("owner_team")
        if prefs.get("ci_provider") is not None:
            flat["preferred_ci_provider"] = prefs.get("ci_provider")
        if prefs.get("ci_complexity") is not None:
            flat["preferred_ci_complexity"] = prefs.get("ci_complexity")

    if isinstance(history, dict):
        if history.get("recent_domains") is not None:
            flat["recent_domains"] = list(history.get("recent_domains") or [])
        if history.get("recent_use_cases") is not None:
            flat["recent_use_cases"] = list(history.get("recent_use_cases") or [])

    return flat


# ---------------------------------------------------------------------------
# Poison-token defence (see _POISON_TOKENS)
# ---------------------------------------------------------------------------


def _is_poisoned_value(value: Any) -> bool:
    """Return True iff *value* looks like a test-double ``repr()`` string."""
    if isinstance(value, str):
        return any(token in value for token in _POISON_TOKENS)
    return False


def _strip_poisoned_values(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *raw* with poisoned scalar values dropped.

    Walks the v1 ``preferences`` and ``history`` sub-dicts (and any
    lists inside ``history``) — anywhere a string value matches a
    poison token, the key is omitted from the returned dict.  The
    on-disk file is *not* rewritten; that is the job of
    :func:`_sanitize_existing_personal_memory`.
    """
    cleaned: Dict[str, Any] = {}
    for top_key, top_val in raw.items():
        if top_key in {"preferences", "history"} and isinstance(top_val, dict):
            inner: Dict[str, Any] = {}
            for k, v in top_val.items():
                if isinstance(v, list):
                    pruned = [item for item in v if not _is_poisoned_value(item)]
                    inner[k] = pruned
                elif _is_poisoned_value(v):
                    LOG.warning(
                        "Dropping poisoned %s.%s from personal memory: %r",
                        top_key,
                        k,
                        v,
                    )
                    continue
                else:
                    inner[k] = v
            cleaned[top_key] = inner
        elif _is_poisoned_value(top_val):
            LOG.warning("Dropping poisoned top-level %s from personal memory", top_key)
            continue
        else:
            cleaned[top_key] = top_val
    return cleaned


def _reject_poisoned_context(context: Dict[str, Any]) -> None:
    """Raise ``ValueError`` if *context* carries a poisoned value.

    Caller-side bug: a test forgot to ``configure_mock`` a return value
    and let the resulting ``MagicMock`` flow through to the writer.
    Failing loudly here keeps the on-disk file pristine.
    """
    if not isinstance(context, dict):
        return
    poisoned_keys = [k for k, v in context.items() if _is_poisoned_value(v)]
    if poisoned_keys:
        raise ValueError(
            "Refusing to persist personal memory: test-double repr() detected in "
            f"context keys: {poisoned_keys!r}. The caller likely passed a "
            "MagicMock attribute by accident (see _POISON_TOKENS)."
        )


def _sanitize_existing_personal_memory(path: Optional[Path] = None) -> bool:
    """One-shot cleanup: rewrite ``~/.fluid/personal-memory.json`` with poison removed.

    Idempotent — if no poisoned values are present, the file is left
    untouched.  Returns ``True`` iff the file was rewritten.

    Called from ``tests/conftest.py`` at session start so contributor
    laptops self-heal from any prior test that leaked a MagicMock into
    user-global state.  Safe to call when the file does not exist.
    """
    target = path if path is not None else _MEMORY_FILE
    try:
        if not target.exists():
            return False
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(raw, dict):
        return False

    cleaned = _strip_poisoned_values(raw)
    if cleaned == raw:
        return False

    try:
        target.write_text(
            json.dumps(cleaned, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        LOG.info("Sanitised poisoned values from %s", target)
        return True
    except OSError as exc:  # pragma: no cover — defensive
        LOG.debug("Could not rewrite sanitised personal memory: %s", exc)
        return False
