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

"""Heuristic dbt source-freshness block proposer.

Maps a human cadence string (``hourly``, ``daily``, ``streaming``,
…) into a dbt ``freshness:`` block. The output mirrors the canonical
dbt source-properties shape exactly — ``warn_after`` / ``error_after``
each carry ``{count, period}`` where ``period`` is one of
``minute|hour|day`` (per dbt-labs/dbt-core).

Source: https://docs.getdbt.com/reference/resource-properties/freshness

CDC streams (``source_type='cdc'``) get tighter thresholds — CDC
publishers tend to surface back-pressure / replication lag faster
than the underlying batch process they replicate from.

The ``filter`` slot stays ``None`` here; the caller knows the
domain-specific ``WHERE`` clause (e.g. ``is_deleted='false'``)
and overrides it after the fact.
"""

from __future__ import annotations

import logging
from typing import Any

_LOG = logging.getLogger(__name__)


# Base thresholds in MINUTES so CDC halving stays integer-clean
# without re-shaping ``{count, period}`` mid-flight. The emitter
# converts back to the largest sensible ``period`` per dbt's
# minute|hour|day vocabulary.
#
# Anchors (default, non-CDC):
#   streaming → warn 5m  / error 15m
#   hourly    → warn 90m / error 3h
#   daily     → warn 30h / error 48h
#   weekly    → warn 8d  / error 14d
#   monthly   → warn 32d / error 45d
_CADENCE_TABLE_MINUTES: dict[str, tuple[int, int]] = {
    "streaming": (5, 15),
    "hourly": (90, 180),
    "daily": (30 * 60, 48 * 60),
    "weekly": (8 * 24 * 60, 14 * 24 * 60),
    "monthly": (32 * 24 * 60, 45 * 24 * 60),
}

# Recognised aliases → canonical cadence key.
_CADENCE_ALIASES: dict[str, str] = {
    "streaming": "streaming",
    "realtime": "streaming",
    "real-time": "streaming",
    "real_time": "streaming",
    "hourly": "hourly",
    "every_hour": "hourly",
    "every-hour": "hourly",
    "daily": "daily",
    "every_day": "daily",
    "every-day": "daily",
    "nightly": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
}


def _to_freshness_unit(total_minutes: int) -> dict[str, Any]:
    """Render an integer-minute count as the largest sensible dbt unit.

    dbt's freshness vocabulary is ``minute | hour | day``. We prefer
    days when the value is a clean multiple of 1440 minutes, then
    hours for clean multiples of 60, else minutes.
    """
    if total_minutes <= 0:
        # Floor at 1 minute so we never emit count=0; that's invalid
        # per dbt's positive-integer constraint.
        return {"count": 1, "period": "minute"}
    if total_minutes % (24 * 60) == 0:
        return {"count": total_minutes // (24 * 60), "period": "day"}
    if total_minutes % 60 == 0:
        return {"count": total_minutes // 60, "period": "hour"}
    return {"count": total_minutes, "period": "minute"}


def propose_freshness(
    refresh_cadence: str,
    *,
    source_type: str | None = None,
) -> dict[str, Any]:
    """Propose a dbt source-freshness block.

    Parameters
    ----------
    refresh_cadence
        Human-readable cadence string. Matched case-insensitively
        against the alias table above (streaming/realtime, hourly,
        daily/nightly, weekly, monthly).
    source_type
        Optional. ``'cdc'`` halves both thresholds; anything else is
        ignored.

    Returns
    -------
    dict
        Either an empty dict (unknown cadence — caller decides what
        to do, logged at WARN) or::

            {
              "warn_after":  {"count": <int>, "period": "minute"|"hour"|"day"},
              "error_after": {"count": <int>, "period": "minute"|"hour"|"day"},
              "filter": None,
            }
    """
    if not refresh_cadence or not isinstance(refresh_cadence, str):
        _LOG.warning("unrecognized cadence: %r", refresh_cadence)
        return {}

    key = _CADENCE_ALIASES.get(refresh_cadence.strip().lower())
    if key is None:
        _LOG.warning("unrecognized cadence: %r", refresh_cadence)
        return {}

    warn_m, error_m = _CADENCE_TABLE_MINUTES[key]

    if isinstance(source_type, str) and source_type.strip().lower() == "cdc":
        # CDC streams are more lag-sensitive — halve both thresholds.
        # Integer-floor on odd numbers (e.g. 5 → 2) is intentional;
        # the floor in ``_to_freshness_unit`` keeps it >= 1 minute.
        warn_m = max(1, warn_m // 2)
        error_m = max(1, error_m // 2)

    return {
        "warn_after": _to_freshness_unit(warn_m),
        "error_after": _to_freshness_unit(error_m),
        "filter": None,
    }


__all__ = ["propose_freshness"]
