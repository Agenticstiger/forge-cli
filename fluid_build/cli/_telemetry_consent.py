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

"""Opt-in consent gate for FLUID usage telemetry (privacy-preserving).

FLUID captures lightweight UX telemetry (``_ux_telemetry.UXTelemetry``)
and projects it onto the ``forge.invocation`` OTel span. Even though that
span only ever leaves the process when an OTLP exporter is configured,
we add an **explicit, default-OFF consent gate** in front of it so that
*nothing* about how the user runs ``fluid forge`` is emitted unless the
user has affirmatively opted in.

Precedence (highest wins) — adapted from dbt-core's anonymous-usage-stats
ladder and the cross-tool ``DO_NOT_TRACK`` convention
(https://consoledonottrack.com, honored by gh / Sanity / Kedro / Gatsby):

1. ``DO_NOT_TRACK`` truthy  -> telemetry OFF (always wins, never
   overridable by config — the universal kill switch).
2. ``FLUID_TELEMETRY`` set   -> explicit per-invocation override
   (``1``/``true``/``yes`` => ON, ``0``/``false``/``no`` => OFF).
3. ``~/.fluid/config.yaml`` ``telemetry.enabled``  -> persisted choice.
4. **Default: OFF.** Absent any of the above, telemetry is disabled.

This is stricter than dbt / gh (which default ON with an opt-out); FLUID
is opt-IN. The flag is persisted under a dedicated ``telemetry:`` block
in the user-global config so it survives across runs without an env var.

Everything here is best-effort: a read-only ``$HOME`` or a malformed
config file degrades to "telemetry off" and never raises into the run.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Optional

LOG = logging.getLogger(__name__)

# Default-OFF: the privacy-preserving default. Do not flip this without a
# product decision — every consumer relies on "absent config => no emit".
DEFAULT_ENABLED = False

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}


def _env_truthy(name: str) -> Optional[bool]:
    """Return True/False if *name* is set to a recognised value, else None."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    # Any other non-empty value: treat presence as truthy for DO_NOT_TRACK
    # (the convention is "set at all => opt out"); for FLUID_TELEMETRY an
    # unrecognised value is treated as enabled (explicit set => intent).
    return True


def _do_not_track_set() -> bool:
    """True when the cross-tool ``DO_NOT_TRACK`` kill switch is engaged."""
    return _env_truthy("DO_NOT_TRACK") is True


def _config_path():
    from fluid_build.paths import user_config_file

    return user_config_file()


def _load_config() -> Dict[str, Any]:
    """Best-effort read of ``~/.fluid/config.yaml`` as a dict."""
    try:
        import yaml

        path = _config_path()
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 — never let config IO break a run
        LOG.debug("telemetry_config_read_failed: %s", exc)
        return {}


def _persisted_enabled() -> Optional[bool]:
    """Return the persisted ``telemetry.enabled`` flag, or None if unset."""
    block = _load_config().get("telemetry")
    if isinstance(block, dict) and "enabled" in block:
        return bool(block.get("enabled"))
    return None


def telemetry_enabled() -> bool:
    """Resolve whether telemetry may be emitted for this invocation.

    Default-OFF. See module docstring for the precedence ladder.
    """
    # 1. DO_NOT_TRACK is the universal, non-overridable kill switch.
    if _do_not_track_set():
        return False
    # 2. Explicit per-invocation override.
    env = _env_truthy("FLUID_TELEMETRY")
    if env is not None:
        return env
    # 3. Persisted user choice.
    persisted = _persisted_enabled()
    if persisted is not None:
        return persisted
    # 4. Privacy-preserving default.
    return DEFAULT_ENABLED


def consent_recorded() -> bool:
    """True when the user has already answered the one-time consent prompt."""
    block = _load_config().get("telemetry")
    return bool(isinstance(block, dict) and block.get("consent_recorded"))


def set_telemetry_enabled(enabled: bool) -> bool:
    """Persist the telemetry choice to ``~/.fluid/config.yaml``.

    Read-merge-write so unrelated config keys survive. Best-effort:
    a write failure (e.g. read-only home) returns ``False`` silently
    rather than crashing the caller. Mirrors
    ``_welcome_scan.bump_forge_count``.
    """
    try:
        import yaml

        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _load_config()
        block = data.get("telemetry")
        if not isinstance(block, dict):
            block = {}
        block["enabled"] = bool(enabled)
        block["consent_recorded"] = True
        data["telemetry"] = block
        path.write_text(
            yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.debug("telemetry_config_write_failed: %s", exc)
        return False


def maybe_prompt_for_consent() -> None:
    """Ask once whether to enable telemetry; non-interactive-safe.

    Returns immediately (recording nothing, default stays OFF) when:
    * ``DO_NOT_TRACK`` is set (respect the kill switch silently),
    * ``FLUID_TELEMETRY`` is set (explicit override — don't second-guess),
    * consent was already recorded (one-time only), or
    * stdin is not a TTY (headless / CI / piped — never block on input()).

    Otherwise prints a short notice and asks ``[y/N]`` (default No), then
    persists the answer. Any error is swallowed — the prompt must never
    break the run.
    """
    try:
        if _do_not_track_set():
            return
        if _env_truthy("FLUID_TELEMETRY") is not None:
            return
        if consent_recorded():
            return
        if not sys.stdin.isatty():
            return
        sys.stdout.write(
            "\nFLUID can send anonymous usage telemetry (which interview "
            "mode/questions/timings — never contract contents, names, or "
            "credentials) to help improve the CLI.\n"
            "This is OFF by default. Enable it? [y/N] "
        )
        sys.stdout.flush()
        ans = input().strip().lower()
    except (KeyboardInterrupt, EOFError, OSError):
        # Treat any interruption as "no" but record it so we don't re-ask.
        try:
            set_telemetry_enabled(False)
        except Exception:  # noqa: BLE001
            pass
        return
    except Exception as exc:  # noqa: BLE001
        LOG.debug("telemetry_consent_prompt_failed: %s", exc)
        return
    set_telemetry_enabled(ans in ("y", "yes"))


def describe_state() -> Dict[str, Any]:
    """Structured snapshot for ``fluid doctor`` surfacing."""
    return {
        "enabled": telemetry_enabled(),
        "do_not_track": _do_not_track_set(),
        "env_override": os.environ.get("FLUID_TELEMETRY"),
        "persisted": _persisted_enabled(),
        "consent_recorded": consent_recorded(),
        "default": DEFAULT_ENABLED,
    }


__all__ = [
    "DEFAULT_ENABLED",
    "telemetry_enabled",
    "consent_recorded",
    "set_telemetry_enabled",
    "maybe_prompt_for_consent",
    "describe_state",
]
