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

"""``fluid diff --baseline``: what reaches the gate, how it is printed, what 0.7.x carries.

Once 0.7.x columns reached ``--fail-on-breaking`` (PR #789), its reviews found
four gaps. The diff ran on contracts no schema accepts, so a value of the wrong
type hid a change. It printed contract names raw to a terminal and to Markdown.
It ignored the quality, sensitivity and primary-key information a 0.7.x contract
keeps under ``exposes[].contract``. And it read no columns from a
``{"fields": [...]}`` schema.
"""

from __future__ import annotations

import argparse
import logging

import pytest
import yaml

from fluid_build.api.changelog import ContractValidationError, compare_contracts
from fluid_build.api.changelog_rules import diff_quality_severity
from tests.test_diff_changelog import _ID_COLUMN as _ID
from tests.test_diff_changelog import _contract_076


def _contract(columns, **section):
    """``_contract_076`` with ``section`` (``dq``, ``quality``) beside the schema too."""
    contract = _contract_076(columns)
    contract["exposes"][0]["contract"].update(section)
    return contract


def _run_cli(tmp_path, old, new, *, fmt="json", fail_on_breaking=False):
    """``fluid diff <new> --baseline <old>`` on two contracts written to ``tmp_path``."""
    from fluid_build.cli import diff as diff_mod

    old_path, new_path = tmp_path / "old.fluid.yaml", tmp_path / "new.fluid.yaml"
    old_path.write_text(yaml.safe_dump(old), encoding="utf-8")
    new_path.write_text(yaml.safe_dump(new), encoding="utf-8")
    args = argparse.Namespace(
        contract=str(new_path),
        baseline=str(old_path),
        env=None,
        state=None,
        out=str(tmp_path / "diff.json"),
        exit_on_drift=False,
        fail_on_breaking=fail_on_breaking,
        format=fmt,
        cmd="diff",
    )
    return diff_mod.run(args, logging.getLogger("test.diff"))


# ---------------------------------------------------------------------------
# Both contracts are schema-validated before they are diffed
# ---------------------------------------------------------------------------


def test_every_contract_below_validates_against_the_bundled_schema():
    """The shapes these tests diff: if 0.7.6 stops accepting them they prove nothing."""
    from fluid_build.schema_manager import FluidSchemaManager

    shapes = [
        _contract([_ID]),
        _contract([{**_ID, "tags": ["primary-key"]}, {"name": "email", "type": "string"}]),
        _contract([{"name": "email", "type": "string", "sensitivity": "pii"}]),
        _contract(
            [
                {
                    "name": "email",
                    "type": "string",
                    "quality": [{"id": "email-valid", "type": "text", "severity": "warning"}],
                }
            ],
            dq={"rules": [{"id": "complete", "type": "completeness", "severity": "critical"}]},
            quality=[{"rule": "positive", "expression": "amount > 0", "severity": "warning"}],
        ),
    ]
    for shape in shapes:
        result = FluidSchemaManager().validate_contract(shape, offline_only=True)
        assert result.is_valid, result.errors


def test_a_value_of_the_wrong_type_cannot_hide_a_change_from_the_diff():
    """A ``type`` that is a number was never compared, so the retype passed the gate."""
    old = _contract([_ID, {"name": "amount", "type": "decimal(18,2)"}])
    new = _contract([_ID, {"name": "amount", "type": 18}])

    with pytest.raises(ContractValidationError) as excinfo:
        compare_contracts(old, new)

    assert set(excinfo.value.errors) == {"new"}
    assert any("exposes[0].contract.schema[1].type" in e for e in excinfo.value.errors["new"])


def test_every_invalid_side_is_reported_at_once():
    old = _contract([_ID])
    del old["metadata"]["owner"]
    new = _contract([{"name": "id"}])

    with pytest.raises(ContractValidationError) as excinfo:
        compare_contracts(old, new)

    assert set(excinfo.value.errors) == {"baseline", "new"}
    assert "baseline contract" in str(excinfo.value) and "new contract" in str(excinfo.value)


@pytest.mark.parametrize("version", ["0.6.0", "0.5.7", "0.4"])
def test_a_pre_07_contract_is_refused_with_the_upgrade_message(version):
    old = _contract([_ID])
    old["fluidVersion"] = version

    with pytest.raises(ContractValidationError) as excinfo:
        compare_contracts(old, _contract([_ID]))

    (message,) = excinfo.value.errors["baseline"]
    assert "no longer supported" in message and "0.7.x" in message


def test_a_contract_without_a_fluid_version_is_refused():
    new = _contract([_ID])
    del new["fluidVersion"]

    with pytest.raises(ContractValidationError) as excinfo:
        compare_contracts(_contract([_ID]), new)

    assert set(excinfo.value.errors) == {"new"}


def test_validate_false_diffs_what_it_is_given():
    """The refine preview's opt-out: an advisory diff of a contract still being repaired."""
    old = {"exposes": [{"id": "x", "schema": [{"name": "a", "type": "STRING"}]}]}
    new = {"exposes": [{"id": "x", "schema": []}]}

    report = compare_contracts(old, new, validate=False)

    assert [c.kind for c in report.breaking] == ["column_removed"]


@pytest.mark.parametrize("fail_on_breaking", [True, False])
def test_the_cli_exits_2_on_an_invalid_contract_whatever_fail_on_breaking_says(
    tmp_path, fail_on_breaking
):
    """diff(1)'s 2 is "trouble": an invalid contract never reads as 0 (clean) or 1 (breaking)."""
    from fluid_build.cli._common import CLIError

    old = _contract([_ID, {"name": "amount", "type": "decimal(18,2)"}])
    new = _contract([_ID, {"name": "amount", "type": 18}])

    with pytest.raises(CLIError) as excinfo:
        _run_cli(tmp_path, old, new, fail_on_breaking=fail_on_breaking)

    assert excinfo.value.exit_code == 2
    assert excinfo.value.event == "diff_contract_invalid"
    context = excinfo.value.context
    assert context["invalid"] == ["new"]
    assert str(tmp_path / "new.fluid.yaml") in context["message"]
    assert "fluid validate" in context["message"]
    assert not (tmp_path / "diff.json").exists()


# ---------------------------------------------------------------------------
# 0.7.x keeps quality, sensitivity and the primary key under exposes[].contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old_severity, new_severity, escalated",
    [
        ("warn", "error", True),
        ("warn", "critical", True),
        ("info", "error", True),
        ("critical", "error", False),
        ("error", "critical", False),
        ("error", "warn", False),
    ],
)
def test_a_dq_rule_escalated_to_a_failing_severity_is_breaking(
    old_severity, new_severity, escalated
):
    def rules(severity):
        return {"rules": [{"id": "orders_complete", "type": "completeness", "severity": severity}]}

    report = compare_contracts(
        _contract([_ID], dq=rules(old_severity)), _contract([_ID], dq=rules(new_severity))
    )

    if not escalated:
        assert report.total == 0
        return
    (change,) = report.breaking
    assert change.kind == "quality_severity_escalated"
    assert change.path == "exposes[0].contract.dq.rules.orders_complete.severity"
    assert (change.before, change.after) == (old_severity, new_severity)
    assert f"severity={new_severity}" in change.description


def test_a_contract_quality_rule_escalated_to_error_is_breaking():
    def quality(severity):
        return [{"rule": "positive_amount", "expression": "amount > 0", "severity": severity}]

    report = compare_contracts(
        _contract([_ID], quality=quality("warning")), _contract([_ID], quality=quality("error"))
    )

    (change,) = report.breaking
    assert change.kind == "quality_severity_escalated"
    assert change.path == "exposes[0].contract.quality.positive_amount.severity"


def test_a_column_quality_rule_escalated_to_error_is_breaking():
    def email(severity):
        rule = {"id": "email-valid", "type": "text", "severity": severity}
        return {"name": "email", "type": "string", "quality": [rule]}

    report = compare_contracts(_contract([email("warning")]), _contract([email("error")]))

    (change,) = report.breaking
    assert change.kind == "quality_severity_escalated"
    assert change.path == "exposes[0].contract.schema.email.quality.email-valid.severity"
    assert "column 'email'" in change.description


@pytest.mark.parametrize(
    "old_severity, new_severity, escalated",
    [("warn", "critical", True), ("critical", "error", False)],
)
def test_a_pre_07_quality_test_is_judged_by_the_same_severity_scale(
    old_severity, new_severity, escalated
):
    """``critical`` fails a run as ``error`` does (``fluid_build.severity``)."""

    def expose(severity):
        return {"id": "x", "quality": {"tests": [{"name": "t1", "severity": severity}]}}

    changes = diff_quality_severity(expose(old_severity), expose(new_severity), "x", 0)

    assert [c.kind for c in changes] == (["quality_severity_escalated"] if escalated else [])


@pytest.mark.parametrize(
    "old, new, kind, before, after",
    [
        ("none", "pii", "column_pii_added", False, True),
        (None, "phi", "column_pii_added", False, True),
        ("pii", "anonymized", "column_pii_removed", True, False),
        ("internal", "restricted", "column_sensitivity_changed", "internal", "restricted"),
        ("pii", "phi", "column_sensitivity_changed", "pii", "phi"),
        (None, "confidential", "column_sensitivity_changed", None, "confidential"),
    ],
)
def test_a_sensitivity_change_is_an_info_signal(old, new, kind, before, after):
    def email(sensitivity):
        column = {"name": "email", "type": "string"}
        return {**column, "sensitivity": sensitivity} if sensitivity else column

    report = compare_contracts(_contract([email(old)]), _contract([email(new)]))

    (change,) = report.info
    assert report.total == 1
    assert (change.kind, change.path) == (kind, "exposes[0].contract.schema.email.sensitivity")
    assert (change.before, change.after) == (before, after)


@pytest.mark.parametrize(
    "tag, validate",
    [
        ("primary-key", True),
        # The ODCS exporter's other spelling: ``$defs.tags`` is lower-kebab-case,
        # so only an unvalidated diff meets it.
        ("primaryKey", False),
    ],
)
def test_a_tagged_primary_key_column_made_nullable_is_breaking(tag, validate):
    def key(required):
        return {"name": "id", "type": "string", "required": required, "tags": [tag]}

    report = compare_contracts(_contract([key(True)]), _contract([key(False)]), validate=validate)

    (change,) = report.breaking
    assert report.total == 1
    assert change.kind == "primary_key_nullable"
    assert change.path == "exposes[0].contract.schema.id.required"


def test_the_cli_gate_fails_on_a_primary_key_made_nullable(tmp_path):
    key = {"name": "id", "type": "string", "required": True, "tags": ["primary-key"]}

    rc = _run_cli(
        tmp_path, _contract([key]), _contract([{**key, "required": False}]), fail_on_breaking=True
    )

    assert rc == 1
