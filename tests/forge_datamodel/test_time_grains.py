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

"""Single-source pins for the time-grain vocabulary.

The vocabulary used to exist in three hand-maintained copies which had
measurably drifted (the canonicalizer accepted ``hr``/``mins`` the other
two rejected; they accepted ``hourly``/``ms`` it didn't). These tests pin:

1. Every alias (the union of all three former tables) normalizes.
2. The vocabulary is defined in exactly ONE module — a grep-based drift
   pin in the spirit of ``tests/perf/test_startup_budget.py``.
3. The three former call sites all resolve through the shared module.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fluid_build.forge_datamodel.time_grains import (
    ALLOWED_TIME_GRAINS,
    TIME_GRAIN_ALIASES,
    normalize_time_grain,
    resolve_grain_alias,
)

_FLUID_BUILD = Path(__file__).resolve().parents[2] / "fluid_build"


@pytest.mark.parametrize(("alias", "canonical"), sorted(TIME_GRAIN_ALIASES.items()))
def test_every_alias_normalizes(alias: str, canonical: str) -> None:
    assert normalize_time_grain(alias) == canonical
    assert canonical in ALLOWED_TIME_GRAINS


@pytest.mark.parametrize("canonical", sorted(ALLOWED_TIME_GRAINS))
def test_canonical_grains_are_fixed_points(canonical: str) -> None:
    assert normalize_time_grain(canonical) == canonical
    assert resolve_grain_alias(canonical) == canonical


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Daily", "day"),
        ("  per month ", "month"),
        ("day grain", "day"),
        ("HOURLY", "hour"),
        ("hr", "hour"),  # formerly canonicalizer-only
        ("mins", "minute"),  # formerly canonicalizer-only
        ("ms", "minute"),  # formerly quality/emit-only
        ("time_grain", None),
        ("fortnight", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_time_grain_edge_cases(raw, expected) -> None:
    assert normalize_time_grain(raw) == expected


def test_resolve_grain_alias_passes_unknown_through() -> None:
    """The canonicalizer keeps unknown spellings so the quality lint can
    error with the author's original value."""
    assert resolve_grain_alias("Fortnight") == "fortnight"


def test_vocabulary_is_defined_in_exactly_one_module() -> None:
    """Drift pin: no module outside ``time_grains.py`` may define its own
    grain alias table or allowed-grain set. Three copies drifted once;
    never again."""
    pattern = re.compile(
        r"(_?TIME_GRAIN_ALIASES\s*=\s*\{|_?ALLOWED_TIME_GRAINS\s*=\s*(\{|frozenset))"
    )
    offenders = []
    for path in _FLUID_BUILD.rglob("*.py"):
        if path.name == "time_grains.py":
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(_FLUID_BUILD)))
    assert not offenders, (
        "Time-grain vocabulary must live only in forge_datamodel/time_grains.py; "
        f"found definitions in: {offenders}"
    )


def test_former_call_sites_resolve_through_the_shared_module() -> None:
    from fluid_build.forge_datamodel import logical_canonicalizer
    from fluid_build.forge_datamodel.emit import fluid_contract, semantic_quality

    for module in (logical_canonicalizer, fluid_contract, semantic_quality):
        assert module._time_grains.TIME_GRAIN_ALIASES is TIME_GRAIN_ALIASES
