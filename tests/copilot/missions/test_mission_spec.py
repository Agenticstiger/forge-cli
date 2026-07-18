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

"""Mission spec loader — format pinning + typed validation errors."""

from __future__ import annotations

import re

import pytest

from fluid_build.copilot.missions.spec import (
    MISSION_CHECK_TYPES,
    MissionSpecError,
    discover_all_mission_specs,
    load_builtin_mission_spec,
    load_mission_spec_from_path,
    resolve_mission_spec,
)

pytestmark = pytest.mark.unit

MINIMAL = """\
name: my-mission
description: A mission.
goal: Do the thing.
success_criteria:
  - check: validate
"""


def _write(tmp_path, text, name="mission.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Built-ins (shipped package data)
# ---------------------------------------------------------------------------


def test_builtin_gdpr_clean_pins_rfc_shape():
    spec = load_builtin_mission_spec("gdpr-clean")
    assert spec.name == "gdpr-clean"
    assert spec.builtin is True
    assert re.fullmatch(r"[0-9a-f]{64}", spec.content_sha256)
    checks = [c.check for c in spec.success_criteria]
    assert checks == ["validate", "ai_ready", "predicate"]
    ai_ready = spec.success_criteria[1]
    assert ai_ready.require == {
        "sensitive_exposes_annotated": True,
        "missing_descriptions": 0,
    }
    predicate = spec.success_criteria[2]
    assert predicate.path == "exposes[*].policy.agentPolicy.retentionPolicy.maxRetentionDays"
    assert predicate.op == "lte"
    assert predicate.value == 30
    assert spec.budgets.max_usd == 5.00
    assert spec.budgets.max_iterations == 6
    assert spec.budgets.max_wall_seconds == 1800
    assert spec.gates.destructive == "ask"
    assert "check_pii_classification" in spec.tools_allow
    assert spec.plan_hint == ("inspect", "classify_pii", "stamp_policies")


def test_builtin_quality_coverage_pins_rfc_shape():
    spec = load_builtin_mission_spec("quality-coverage")
    checks = [(c.check, c.op) for c in spec.success_criteria]
    assert checks == [("validate", ""), ("predicate", "exists")]
    assert spec.success_criteria[1].path == "exposes[*].contract.dq.rules"


def test_builtin_lookup_accepts_underscore_and_dash():
    assert load_builtin_mission_spec("gdpr_clean").name == "gdpr-clean"


def test_unknown_builtin_raises_typed_error():
    with pytest.raises(MissionSpecError, match="No built-in mission"):
        load_builtin_mission_spec("no-such-mission")


# ---------------------------------------------------------------------------
# Typed validation errors
# ---------------------------------------------------------------------------


def test_minimal_spec_loads(tmp_path):
    spec = load_mission_spec_from_path(_write(tmp_path, MINIMAL))
    assert spec.name == "my-mission"
    assert spec.builtin is False
    assert spec.budgets.max_usd is None
    assert spec.gates.destructive == "ask"


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("name: ''", "name, description, and goal are required"),
        ("name: 'Bad Name!'", "must be lowercase"),
        ("extra_key: 1", "unknown key"),
        ("success_criteria: []", "non-empty list"),
        ("success_criteria:\n  - check: judge", "unknown check"),
        ("success_criteria:\n  - check: validate\n    path: a.b", "only applies to predicate"),
        ("success_criteria:\n  - check: validate\n    require: {x: 1}", "only applies to ai_ready"),
        ("budgets:\n  max_usd: -1", "positive number"),
        ("budgets:\n  max_iterations: 1.5", "positive integer"),
        ("budgets:\n  max_lattes: 9", "unknown key"),
        ("gates:\n  destructive: yolo", "gates.destructive"),
        ("tools:\n  allow: [1]", "non-empty strings"),
        ("plan_hint: nope", "must be a list"),
    ],
)
def test_invalid_specs_raise_typed_errors(tmp_path, mutation, match):
    text = MINIMAL + mutation + "\n"
    if mutation.startswith(("name:", "success_criteria:")):
        # Replace instead of append for keys the minimal spec already has.
        key = mutation.split(":", 1)[0]
        lines = [
            line
            for i, line in enumerate(MINIMAL.splitlines())
            if not line.startswith(f"{key}:") and not (key == "success_criteria" and i >= 3)
        ]
        text = "\n".join(lines) + "\n" + mutation + "\n"
    with pytest.raises(MissionSpecError, match=match):
        load_mission_spec_from_path(_write(tmp_path, text))


@pytest.mark.parametrize(
    ("criterion", "match"),
    [
        ("- check: predicate\n    op: eq\n    value: 1", "non-empty path"),
        (
            "- check: predicate\n    path: a.b\n    op: matches\n    value: x",
            "unknown predicate op",
        ),
        ("- check: predicate\n    path: a[0].b\n    op: exists", "invalid path segment"),
        ("- check: predicate\n    path: a.b\n    op: lte", "requires a value"),
        ("- check: predicate\n    path: a.b\n    op: exists\n    value: 3", "optional boolean"),
        ("- check: predicate\n    path: a.b\n    op: eq\n    value: {x: 1}", "must be scalars"),
        ("- check: ai_ready\n    require: {surprise: 1}", "unknown key"),
        (
            "- check: ai_ready\n    require: {sensitive_exposes_annotated: false}",
            "only accepts true",
        ),
        (
            "- check: ai_ready\n    require: {missing_descriptions: -2}",
            "non-negative integer",
        ),
        (
            "- check: ai_ready\n    require: {missing_descriptions: true}",
            "non-negative integer",
        ),
    ],
)
def test_invalid_criteria_raise_typed_errors(tmp_path, criterion, match):
    text = MINIMAL.replace("  - check: validate", f"  {criterion}")
    with pytest.raises(MissionSpecError, match=match):
        load_mission_spec_from_path(_write(tmp_path, text))


def test_all_advisory_criteria_rejected(tmp_path):
    text = MINIMAL.replace("- check: validate", "- check: validate\n    advisory: true")
    with pytest.raises(MissionSpecError, match="at least one non-advisory"):
        load_mission_spec_from_path(_write(tmp_path, text))


def test_unparseable_yaml_raises_typed_error(tmp_path):
    with pytest.raises(MissionSpecError, match="not parseable as YAML"):
        load_mission_spec_from_path(_write(tmp_path, "a: [unclosed"))


def test_missing_file_raises_typed_error(tmp_path):
    with pytest.raises(MissionSpecError, match="was not found"):
        load_mission_spec_from_path(tmp_path / "ghost.yaml")


def test_check_types_are_the_v1_three():
    assert MISSION_CHECK_TYPES == ("validate", "ai_ready", "predicate")


# ---------------------------------------------------------------------------
# Resolution + discovery (workspace shadows built-in)
# ---------------------------------------------------------------------------


def test_resolve_by_name_prefers_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    missions = tmp_path / ".fluid" / "missions"
    missions.mkdir(parents=True)
    shadow = MINIMAL.replace("my-mission", "gdpr-clean")
    (missions / "gdpr_clean.yaml").write_text(shadow, encoding="utf-8")

    spec = resolve_mission_spec("gdpr-clean")
    assert spec.builtin is False
    assert spec.source_path == (missions / "gdpr_clean.yaml").resolve()
    assert discover_all_mission_specs()["gdpr-clean"].builtin is False


def test_resolve_by_path_and_unknown_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _write(tmp_path, MINIMAL)
    assert resolve_mission_spec(str(path)).name == "my-mission"
    with pytest.raises(MissionSpecError, match="No mission named 'nope'"):
        resolve_mission_spec("nope")


def test_discover_skips_invalid_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    missions = tmp_path / ".fluid" / "missions"
    missions.mkdir(parents=True)
    (missions / "broken.yaml").write_text("name: [", encoding="utf-8")
    specs = discover_all_mission_specs()
    assert "gdpr-clean" in specs and "quality-coverage" in specs
