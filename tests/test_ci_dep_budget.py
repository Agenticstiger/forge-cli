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

"""Tests for ``scripts/ci/dep_budget.py`` (pure parts; no ``uv`` is run)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "dep_budget.py"
_spec = importlib.util.spec_from_file_location("dep_budget", _SCRIPT)
assert _spec and _spec.loader
budget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(budget)


def test_parse_packages_normalises_and_ignores_markers_and_comments() -> None:
    compiled = "# a comment\nPyYAML==6.0\ntyping_extensions==4.0 ; python_version < '3.11'\n\n"
    assert budget.parse_packages(compiled) == ["pyyaml", "typing-extensions"]


def test_check_passes_at_or_under_ceiling() -> None:
    assert budget.check("t", ["a", "b"], 2, ["a", "b"]) is None


def test_check_names_added_and_removed_when_over_ceiling() -> None:
    msg = budget.check("t", ["a", "c", "d"], 2, ["a", "b"])
    assert msg and "3 packages (ceiling 2)" in msg
    assert "added vs baseline:   c, d" in msg
    assert "removed vs baseline: b" in msg


def test_every_target_has_a_committed_baseline_within_its_ceiling() -> None:
    for name, (_, _, ceiling) in budget.TARGETS.items():
        listed = (budget.BASELINES / f"{name}.txt").read_text().split()
        assert 0 < len(listed) <= ceiling, name
