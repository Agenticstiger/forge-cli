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

"""Mission checks — registry, frozen predicate DSL, ai_ready, validate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fluid_build.copilot.missions.checks import (
    MISSION_CHECKS,
    CheckResult,
    MissionCheckError,
    _check_ai_ready,
    _check_predicate,
    load_contract_for_checks,
    register_mission_check,
    run_mission_checks,
)
from fluid_build.copilot.missions.spec import CriterionSpec, load_builtin_mission_spec

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PASSING_CONTRACT = REPO_ROOT / "examples" / "customer360" / "contract.fluid.yaml"
NO_DQ_CONTRACT = REPO_ROOT / "examples" / "05-data-quality-validation" / "contract.fluid.yaml"


def _predicate(path, op, value=None, *, value_provided=None, advisory=False):
    provided = value_provided if value_provided is not None else value is not None
    return CriterionSpec(
        check="predicate", advisory=advisory, path=path, op=op, value=value, value_provided=provided
    )


def _run_predicate(contract, path, op, value=None, **kwargs):
    return _check_predicate(
        _predicate(path, op, value, **kwargs), contract, contract_path=Path("x.yaml")
    )


CONTRACT = {
    "id": "p1",
    "exposes": [
        {
            "exposeId": "a",
            "policy": {"agentPolicy": {"retentionPolicy": {"maxRetentionDays": 30}}},
            "contract": {"dq": {"rules": [{"id": "r1"}]}},
        },
        {
            "exposeId": "b",
            "policy": {"agentPolicy": {"retentionPolicy": {"maxRetentionDays": 90}}},
            "contract": {"dq": {"rules": []}},
        },
    ],
    "metadata": {"layer": "Silver"},
    "tags": ["pii", "gdpr"],
}


# ---------------------------------------------------------------------------
# Predicate DSL (frozen)
# ---------------------------------------------------------------------------


def test_predicate_eq_and_ne_on_scalar():
    assert _run_predicate(CONTRACT, "metadata.layer", "eq", "Silver").passed
    assert not _run_predicate(CONTRACT, "metadata.layer", "eq", "Gold").passed
    assert _run_predicate(CONTRACT, "metadata.layer", "ne", "Gold").passed


def test_predicate_ordered_comparisons_fan_out_all_semantics():
    path = "exposes[*].policy.agentPolicy.retentionPolicy.maxRetentionDays"
    ok = _run_predicate(CONTRACT, path, "lte", 90)
    assert ok.passed
    bad = _run_predicate(CONTRACT, path, "lte", 30)
    assert not bad.passed
    # Diagnostics name the concrete failing leaf, index substituted.
    assert any("exposes[1]" in line and "90" in line for line in bad.diagnostics)


@pytest.mark.parametrize(
    ("op", "value", "passed"),
    [("lt", 31, True), ("gt", 29, True), ("gte", 30, True), ("gt", 30, False)],
)
def test_predicate_ordered_ops(op, value, passed):
    result = _run_predicate(
        CONTRACT, "exposes[*].policy.agentPolicy.retentionPolicy.maxRetentionDays", op, value
    )
    # Only expose a (30) considered when both leaves compared: b is 90.
    contract_one = {"exposes": [CONTRACT["exposes"][0]]}
    result = _run_predicate(
        contract_one, "exposes[*].policy.agentPolicy.retentionPolicy.maxRetentionDays", op, value
    )
    assert result.passed is passed


def test_predicate_type_mismatch_fails_closed():
    result = _run_predicate(CONTRACT, "metadata.layer", "lte", 30)
    assert not result.passed
    assert any("cannot order-compare" in line for line in result.diagnostics)


def test_predicate_contains_on_list_and_string():
    assert _run_predicate(CONTRACT, "tags", "contains", "gdpr").passed
    assert not _run_predicate(CONTRACT, "tags", "contains", "hipaa").passed
    assert _run_predicate(CONTRACT, "metadata.layer", "contains", "ilv").passed


def test_predicate_exists_semantics():
    # Present, non-empty → exists.
    assert (
        _run_predicate(CONTRACT, "exposes[0].contract.dq.rules", "exists").passed is False
    )  # [*] needed
    one = {"exposes": [CONTRACT["exposes"][0]]}
    assert _run_predicate(one, "exposes[*].contract.dq.rules", "exists").passed
    # Empty list at the leaf counts as absent (fail closed).
    result = _run_predicate(CONTRACT, "exposes[*].contract.dq.rules", "exists")
    assert not result.passed
    assert any("empty/null" in line for line in result.diagnostics)
    # exists: false passes only when nothing resolves.
    absent = _run_predicate(CONTRACT, "metadata.deprecated", "exists", False, value_provided=True)
    assert absent.passed
    present = _run_predicate(CONTRACT, "metadata.layer", "exists", False, value_provided=True)
    assert not present.passed


def test_predicate_missing_paths_and_empty_arrays_fail_closed():
    assert not _run_predicate(CONTRACT, "no.such.path", "eq", 1).passed
    assert not _run_predicate(CONTRACT, "no.such.path", "exists").passed
    empty = {"exposes": []}
    result = _run_predicate(empty, "exposes[*].contract.dq.rules", "exists")
    assert not result.passed
    assert any("empty array" in line for line in result.diagnostics)
    not_array = _run_predicate({"exposes": {"a": 1}}, "exposes[*].x", "exists")
    assert not not_array.passed


def test_predicate_partial_missing_fails_comparisons():
    contract = {"exposes": [{"policy": {"x": 1}}, {"nope": True}]}
    result = _run_predicate(contract, "exposes[*].policy.x", "eq", 1)
    assert not result.passed
    assert any("path not found" in line for line in result.diagnostics)


# ---------------------------------------------------------------------------
# ai_ready check
# ---------------------------------------------------------------------------


def _ai_contract(*, described=True, sensitive_annotated=True):
    column = {
        "name": "email",
        "type": "string",
        "sensitivity": "pii",
    }
    if described:
        column["description"] = "Customer email."
    expose = {
        "exposeId": "out",
        "contract": {"schema": [column]},
    }
    if described:
        expose["description"] = "Output port."
    if sensitive_annotated:
        expose["policy"] = {"agentPolicy": {"auditRequired": True}}
    return {"id": "p", "exposes": [expose]}


def _ai_criterion(require=None):
    return CriterionSpec(check="ai_ready", require=require or {})


def test_ai_ready_default_verdict_and_missing_descriptions():
    good = _check_ai_ready(_ai_criterion(), _ai_contract(), contract_path=Path("x"))
    assert good.passed
    bad = _check_ai_ready(_ai_criterion(), _ai_contract(described=False), contract_path=Path("x"))
    assert not bad.passed
    assert any("missing description" in line for line in bad.diagnostics)


def test_ai_ready_require_missing_descriptions_tolerance():
    criterion = _ai_criterion({"missing_descriptions": 5})
    result = _check_ai_ready(criterion, _ai_contract(described=False), contract_path=Path("x"))
    assert result.passed
    strict = _ai_criterion({"missing_descriptions": 0})
    result = _check_ai_ready(strict, _ai_contract(described=False), contract_path=Path("x"))
    assert not result.passed


def test_ai_ready_require_sensitive_exposes_annotated_reads_disk_truth():
    criterion = _ai_criterion({"sensitive_exposes_annotated": True})
    annotated = _check_ai_ready(criterion, _ai_contract(), contract_path=Path("x"))
    assert annotated.passed
    bare = _check_ai_ready(
        criterion, _ai_contract(sensitive_annotated=False), contract_path=Path("x")
    )
    assert not bare.passed
    assert any("sensitive port without agentPolicy: out" in line for line in bare.diagnostics)


def test_ai_ready_check_never_mutates_the_contract():
    contract = _ai_contract(sensitive_annotated=False)
    before = yaml.safe_dump(contract, sort_keys=True)
    _check_ai_ready(_ai_criterion(), contract, contract_path=Path("x"))
    assert yaml.safe_dump(contract, sort_keys=True) == before


def test_ai_ready_kill_switch_fails_closed(monkeypatch):
    monkeypatch.setenv("FLUID_AI_READY", "0")
    result = _check_ai_ready(_ai_criterion(), _ai_contract(), contract_path=Path("x"))
    assert not result.passed
    assert "disabled" in result.detail


# ---------------------------------------------------------------------------
# Harness: re-read, re-hash, fail-closed, redaction
# ---------------------------------------------------------------------------


def test_run_mission_checks_scorecard_pass_and_fail():
    spec = load_builtin_mission_spec("quality-coverage")
    green = run_mission_checks(spec, PASSING_CONTRACT)
    assert green.passed
    assert green.gating_passed == green.gating_total == 2
    assert [r.name for r in green.results] == ["validate", "predicate"]

    red = run_mission_checks(spec, NO_DQ_CONTRACT)
    assert not red.passed
    assert red.gating_passed == 1
    payload = red.to_dict()
    assert payload["passed"] is False
    assert payload["results"][1]["diagnostics"]


def test_scorecard_is_digest_bound_to_the_on_disk_contract():
    from fluid_build.copilot.checkpoint_stale import _canonical_hash

    spec = load_builtin_mission_spec("quality-coverage")
    scorecard = run_mission_checks(spec, PASSING_CONTRACT)
    contract = yaml.safe_load(PASSING_CONTRACT.read_text(encoding="utf-8"))
    assert scorecard.contract_sha256 == _canonical_hash(contract)


def test_validate_check_fails_on_broken_contract(tmp_path):
    broken = yaml.safe_load(PASSING_CONTRACT.read_text(encoding="utf-8"))
    broken["kind"] = 12345  # schema violation
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")
    spec = load_builtin_mission_spec("quality-coverage")
    scorecard = run_mission_checks(spec, path)
    validate = scorecard.results[0]
    assert not validate.passed
    assert validate.diagnostics


def test_advisory_results_never_gate(tmp_path):
    from fluid_build.copilot.missions.spec import load_mission_spec_from_path

    spec_path = tmp_path / "advisory.yaml"
    spec_path.write_text(
        "name: advisory-demo\n"
        "description: Advisory demo.\n"
        "goal: Gate on validate only.\n"
        "success_criteria:\n"
        "  - check: validate\n"
        "  - check: predicate\n"
        "    advisory: true\n"
        "    path: no.such.path\n"
        "    op: exists\n",
        encoding="utf-8",
    )
    spec = load_mission_spec_from_path(spec_path)
    scorecard = run_mission_checks(spec, PASSING_CONTRACT)
    assert scorecard.results[1].advisory and not scorecard.results[1].passed
    assert scorecard.passed  # advisory failure does not gate
    assert scorecard.gating_total == 1
    assert scorecard.to_dict()["results"][1]["advisory"] is True


def test_unknown_and_crashing_checks_fail_closed(monkeypatch):
    def _bomb(criterion, contract, *, contract_path):
        raise RuntimeError("password=super-secret-token do not leak me")

    monkeypatch.setitem(MISSION_CHECKS, "validate", _bomb)
    spec = load_builtin_mission_spec("quality-coverage")
    scorecard = run_mission_checks(spec, PASSING_CONTRACT)
    crashed = scorecard.results[0]
    assert not crashed.passed
    assert "RuntimeError" in crashed.detail
    # Exception TEXT never round-trips into the scorecard (PRs #28–#33 posture).
    assert "super-secret-token" not in str(crashed.to_dict())

    monkeypatch.delitem(MISSION_CHECKS, "predicate")
    scorecard = run_mission_checks(spec, PASSING_CONTRACT)
    assert not scorecard.results[1].passed
    assert "failing closed" in scorecard.results[1].detail


def test_every_result_passes_the_secret_redactor(monkeypatch):
    token = "Bearer sk-abc123def456ghi789"

    def _leaky(criterion, contract, *, contract_path):
        return CheckResult(
            name="validate", passed=False, detail=f"auth {token}", diagnostics=[f"saw {token}"]
        )

    monkeypatch.setitem(MISSION_CHECKS, "validate", _leaky)
    spec = load_builtin_mission_spec("quality-coverage")
    scorecard = run_mission_checks(spec, PASSING_CONTRACT)
    leaked = scorecard.results[0]
    assert "sk-abc123def456ghi789" not in leaked.detail
    assert all("sk-abc123def456ghi789" not in line for line in leaked.diagnostics)
    assert "REDACTED" in leaked.detail


def test_register_mission_check_seam(monkeypatch):
    calls = []

    def _custom(criterion, contract, *, contract_path):
        calls.append(criterion.check)
        return CheckResult(name="custom", passed=True)

    monkeypatch.setitem(MISSION_CHECKS, "custom_probe", _custom)
    register_mission_check("custom_probe", _custom)
    assert MISSION_CHECKS["custom_probe"] is _custom


def test_load_contract_for_checks_errors_are_typed(tmp_path):
    with pytest.raises(MissionCheckError, match="not found"):
        load_contract_for_checks(tmp_path / "ghost.yaml")
    bad = tmp_path / "list.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(MissionCheckError, match="not a mapping"):
        load_contract_for_checks(bad)
    broken = tmp_path / "broken.yaml"
    broken.write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(MissionCheckError, match="not readable YAML"):
        load_contract_for_checks(broken)
