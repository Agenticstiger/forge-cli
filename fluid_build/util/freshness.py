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

"""ISO-8601 duration → dbt ``{count, period}`` freshness-unit conversion.

dbt's ``source freshness`` vocabulary is ``minute | hour | day`` (see
https://docs.getdbt.com/reference/resource-properties/freshness). Contract
freshness promises are ISO-8601 durations (``PT6H``, ``P1D`` …), so anything
emitting a dbt ``freshness:`` block has to translate one to the other.

``to_freshness_unit`` is lifted verbatim from
``fluid_build.copilot.tools.freshness_emitter._to_freshness_unit`` so the dbt
``engines/`` layer can reuse the converter WITHOUT importing ``copilot/`` (that
import edge is forbidden — the light CLI must not pull the AI runtime). The two
copies must stay in sync if dbt's period vocabulary ever changes; this is the
canonical home and ``freshness_emitter`` keeps its own copy only because
``copilot/*`` is off-limits to non-copilot callers.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def to_freshness_unit(total_minutes: int) -> Dict[str, Any]:
    """Render an integer-minute count as the largest sensible dbt unit.

    dbt's freshness vocabulary is ``minute | hour | day``. Prefer days when the
    value is a clean multiple of 1440 minutes, then hours for clean multiples of
    60, else minutes. Floors at 1 minute so we never emit ``count: 0`` — that is
    invalid per dbt's positive-integer constraint.

    Lifted from :func:`fluid_build.copilot.tools.freshness_emitter._to_freshness_unit`.
    """
    if total_minutes <= 0:
        return {"count": 1, "period": "minute"}
    if total_minutes % (24 * 60) == 0:
        return {"count": total_minutes // (24 * 60), "period": "day"}
    if total_minutes % 60 == 0:
        return {"count": total_minutes // 60, "period": "hour"}
    return {"count": total_minutes, "period": "minute"}


def iso_duration_to_freshness_unit(
    iso: Optional[str],
    *,
    multiplier: int = 1,
) -> Optional[Dict[str, Any]]:
    """Convert an ISO-8601 duration into a dbt ``{count, period}`` unit.

    Returns ``None`` when *iso* is empty or unparseable (callers treat that as
    "no threshold declared"). ``multiplier`` scales the duration before
    conversion — used to derive ``error_after = 2 × freshnessSLO`` when only a
    producer SLO is present.

    ISO parsing is delegated to
    :func:`fluid_build.build_runners._late_arrival.parse_iso_duration` (the
    repo's single ISO-8601-duration parser); the import is function-local so
    this module's import surface stays stdlib-only.
    """
    # Function-local import: parse_iso_duration is the canonical ISO-8601
    # duration parser, but importing it eagerly would pull the build_runners
    # package onto this module's import path for no cold-path benefit.
    from fluid_build.build_runners._late_arrival import parse_iso_duration

    td = parse_iso_duration(iso)
    if td is None:
        return None
    total_minutes = int((td.total_seconds() * multiplier) // 60)
    return to_freshness_unit(total_minutes)


__all__ = ["to_freshness_unit", "iso_duration_to_freshness_unit"]
