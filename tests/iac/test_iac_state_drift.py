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

"""``iac.drift``: a saved OpenTofu plan read as drift, pending and match.

The plan documents here have the shapes ``tofu show -json`` printed for an
AWS contract applied against moto (OpenTofu 1.12, hashicorp/aws 5.x): the
Glue ``null`` -> ``{}`` read-backs a clean apply leaves in
``resource_drift``, a bucket tag added by hand, a column added to the Glue
table by hand, versioning and default encryption switched on by hand, and
the table deleted by hand. The same scenarios run end to end in
``test_iac_state_drift_moto.py``.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac import drift, runner

pytestmark = pytest.mark.unit

BUCKET = "aws_s3_bucket.drift_m_bucket"
DB = "aws_glue_catalog_database.drift_m_silver"
TABLE = "aws_glue_catalog_table.drift_m_silver_events"
TAGS = {"fluid_contract": "drift_m", "managed_by": "fluid"}
COLUMNS = [{"comment": "", "name": "id", "parameters": None, "type": "bigint"}]


def _bucket(**over: Any) -> Dict[str, Any]:
    values = {
        "bucket": "m-bucket",
        "force_destroy": True,
        "tags": dict(TAGS),
        "tags_all": dict(TAGS),
        "versioning": [{"enabled": False, "mfa_delete": False}],
        "server_side_encryption_configuration": [],
    }
    values.update(over)
    return values


def _db(**over: Any) -> Dict[str, Any]:
    values = {"name": "m_silver", "parameters": None, "tags": None}
    values.update(over)
    return values


def _table(**over: Any) -> Dict[str, Any]:
    values = {
        "name": "events",
        "database_name": "m_silver",
        "storage_descriptor": [
            {
                "columns": [dict(c) for c in COLUMNS],
                "parameters": None,
                "bucket_columns": None,
                "additional_locations": None,
            }
        ],
    }
    values.update(over)
    return values


def _entry(address: str, actions: List[str], before: Any, after: Any, **extra: Any) -> Dict:
    return {
        "address": address,
        "mode": "managed",
        "type": address.split(".")[0],
        "change": {"actions": actions, "before": before, "after": after, **extra},
    }


def _state(*resources: tuple) -> Dict[str, Any]:
    return {
        "values": {
            "root_module": {
                "resources": [
                    {"address": a, "mode": "managed", "type": a.split(".")[0], "values": v}
                    for a, v in resources
                ]
            }
        }
    }


def _noise() -> List[Dict[str, Any]]:
    """What the refresh reports straight after a clean apply against moto."""
    table_after = _table()
    table_after["storage_descriptor"][0].update(
        {"parameters": {}, "bucket_columns": [], "additional_locations": []}
    )
    table_after["storage_descriptor"][0]["columns"][0]["parameters"] = {}
    return [
        _entry(DB, ["update"], _db(), _db(parameters={}, tags={})),
        _entry(TABLE, ["update"], _table(), table_after),
    ]


def _plan(
    *,
    drift_entries: Optional[List[Dict]] = None,
    changes: Optional[List[Dict]] = None,
    relevant: Optional[List[Dict]] = None,
    state: Optional[Dict] = None,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "prior_state": state or _state((BUCKET, _bucket()), (DB, _db()), (TABLE, _table())),
        "resource_drift": drift_entries if drift_entries is not None else _noise(),
        "resource_changes": changes
        or [
            _entry(BUCKET, ["no-op"], _bucket(), _bucket()),
            _entry(DB, ["no-op"], _db(), _db()),
            _entry(TABLE, ["no-op"], _table(), _table()),
        ],
        "errored": False,
    }
    if relevant is not None:
        doc["relevant_attributes"] = relevant
    return doc


def _by_address(report: drift.StateDriftReport) -> Dict[str, drift.ResourceState]:
    return {r.address: r for r in report.resources}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_clean_apply_is_no_drift_although_the_refresh_reports_changes():
    """``resource_drift`` is not empty and ``-refresh-only`` exits 2 here;
    the read-backs are ``null`` -> ``{}`` / ``[]`` and nothing is planned."""
    doc = _plan()
    assert doc["resource_drift"]
    report = drift.report_from_plan(doc, state="local: x", plan_exit_code=0)
    assert report.status == drift.CHECKED
    assert report.has_drift is False
    assert report.counts() == {"drift": 0, "pending": 0, "match": 3}
    assert all(not r.outside_contract for r in report.resources)


def test_a_tag_added_by_hand_is_drift_on_the_tag():
    tagged = dict(TAGS, rogue="1")
    doc = _plan(
        drift_entries=_noise()
        + [_entry(BUCKET, ["update"], _bucket(), _bucket(tags=tagged, tags_all=tagged))],
        changes=[
            _entry(BUCKET, ["update"], _bucket(tags=tagged, tags_all=tagged), _bucket()),
            _entry(DB, ["no-op"], _db(), _db()),
            _entry(TABLE, ["no-op"], _table(), _table()),
        ],
    )
    report = drift.report_from_plan(doc, state="local: x", plan_exit_code=2)
    bucket = _by_address(report)[BUCKET]
    assert report.has_drift is True
    assert bucket.status == drift.DRIFT
    assert bucket.drifted == ["tags.rogue", "tags_all.rogue"]
    assert bucket.actions == ["update"]
    assert "tags.rogue" in bucket.human()
    assert _by_address(report)[TABLE].status == drift.MATCH


def test_a_setting_the_contract_does_not_declare_is_reported_not_drift():
    """Versioning and default encryption switched on by hand: the refresh sees
    them, the module declares neither, the plan leaves them."""
    changed = _bucket(
        versioning=[{"enabled": True, "mfa_delete": False}],
        server_side_encryption_configuration=[{"rule": [{"sse_algorithm": "AES256"}]}],
    )
    doc = _plan(drift_entries=_noise() + [_entry(BUCKET, ["update"], _bucket(), changed)])
    report = drift.report_from_plan(doc, state="local: x", plan_exit_code=0)
    bucket = _by_address(report)[BUCKET]
    assert report.has_drift is False
    assert bucket.status == drift.MATCH
    assert bucket.outside_contract == [
        "server_side_encryption_configuration",
        "versioning[0].enabled",
    ]


def test_a_column_added_to_the_glue_table_by_hand_is_drift():
    extra = COLUMNS + [{"comment": "", "name": "rogue_col", "parameters": {}, "type": "string"}]
    doc = _plan(
        drift_entries=[
            _entry(TABLE, ["update"], _table(), _table(storage_descriptor=[{"columns": extra}]))
        ],
        changes=[
            _entry(
                TABLE,
                ["update"],
                _table(storage_descriptor=[{"columns": extra}]),
                _table(storage_descriptor=[{"columns": COLUMNS}]),
            )
        ],
        relevant=[{"resource": DB, "attribute": ["name"]}],
    )
    table = _by_address(drift.report_from_plan(doc, state="s", plan_exit_code=2))[TABLE]
    assert table.status == drift.DRIFT
    assert table.drifted == ["storage_descriptor[0].columns"]


def test_a_resource_deleted_by_hand_is_drift_not_to_be_created():
    doc = _plan(
        drift_entries=[_entry(TABLE, ["delete"], _table(), None)],
        changes=[
            _entry(
                TABLE,
                ["create"],
                None,
                _table(),
                after_unknown={"arn": True, "id": True},
            )
        ],
        state=_state((BUCKET, _bucket()), (DB, _db())),
    )
    table = _by_address(drift.report_from_plan(doc, state="s", plan_exit_code=2))[TABLE]
    assert table.status == drift.DRIFT
    assert table.deleted_outside is True
    assert "deleted outside the apply" in table.human()


def test_a_contract_change_since_the_apply_is_pending_not_drift():
    """The plan changes what the refresh saw no change in: the contract moved."""
    new_columns = COLUMNS + [{"name": "added", "type": "string"}]
    new_bucket = "aws_s3_bucket.drift_m_second"
    doc = _plan(
        changes=[
            _entry(BUCKET, ["no-op"], _bucket(), _bucket()),
            _entry(
                TABLE, ["update"], _table(), _table(storage_descriptor=[{"columns": new_columns}])
            ),
            _entry(new_bucket, ["create"], None, {"bucket": "second"}, after_unknown={"id": True}),
        ]
    )
    report = drift.report_from_plan(doc, state="s", plan_exit_code=2)
    by = _by_address(report)
    assert report.has_drift is False
    assert by[TABLE].status == drift.PENDING
    assert by[TABLE].planned == ["storage_descriptor[0].columns"]
    assert by[new_bucket].status == drift.PENDING
    assert by[new_bucket].human() == "pending: apply will create it"


def test_drift_another_planned_change_reads_counts_through_relevant_attributes():
    """``relevant_attributes`` names what a planned change reads elsewhere: a
    change there outside the apply fed the plan even though its own resource
    is not replanned."""
    doc = _plan(
        drift_entries=[_entry(DB, ["update"], _db(), _db(name="renamed"))],
        changes=[_entry(TABLE, ["update"], _table(), _table(database_name="m_silver"))],
        relevant=[{"resource": DB, "attribute": ["name"]}],
    )
    by = _by_address(drift.report_from_plan(doc, state="s", plan_exit_code=2))
    assert by[DB].status == drift.DRIFT
    assert by[DB].drifted == ["name"]


def test_an_empty_state_is_not_checked():
    doc = {
        "prior_state": {"values": {"root_module": {}}},
        "resource_changes": [_entry(BUCKET, ["create"], None, _bucket())],
    }
    report = drift.report_from_plan(doc, state="remote: s3://b/k", plan_exit_code=2)
    assert report.status == drift.NOT_CHECKED
    assert "holds no resources" in (report.detail or "")
    assert report.has_drift is False


def test_an_errored_plan_is_an_error_not_a_pass():
    report = drift.report_from_plan({"errored": True}, state="s", plan_exit_code=2)
    assert report.status == drift.ERROR
    assert report.has_errors


def test_a_referenced_pool_bucket_is_listed_as_outside_state():
    doc = _plan()
    doc["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "data.aws_s3_bucket.pool", "mode": "data", "type": "aws_s3_bucket"}
    )
    report = drift.report_from_plan(doc, state="s", plan_exit_code=0)
    assert report.referenced == ["data.aws_s3_bucket.pool"]
    assert "data.aws_s3_bucket.pool" not in _by_address(report)


def test_the_report_carries_paths_and_never_values():
    secret = "s3cr3t-value-in-a-tag"  # pragma: allowlist secret
    tagged = dict(TAGS, token=secret)
    doc = _plan(
        drift_entries=[_entry(BUCKET, ["update"], _bucket(), _bucket(tags=tagged))],
        changes=[_entry(BUCKET, ["update"], _bucket(tags=tagged), _bucket())],
    )
    text = json.dumps(drift.report_from_plan(doc, state="s", plan_exit_code=2).to_dict())
    assert "tags.token" in text
    assert secret not in text


@pytest.mark.parametrize(
    "before, after, expected",
    [
        (None, {}, set()),
        ([], None, set()),
        ({"a": {"b": 1}}, {"a": {"b": 2}}, {("a", "b")}),
        ({"l": [1, 2]}, {"l": [1, 3]}, {("l", 1)}),
        ({"l": [1]}, {"l": [1, 2]}, {("l",)}),
        ({"x": 1}, None, {()}),
    ],
)
def test_changed_paths(before, after, expected):
    assert drift.changed_paths(before, after) == expected


def test_format_path():
    assert (
        drift.format_path(("storage_descriptor", 0, "columns")) == "storage_descriptor[0].columns"
    )
    assert drift.format_path(("tags", "a.b")) == "tags['a.b']"
    assert drift.format_path(()) == "(the whole resource)"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _fake_run(calls: List[List[str]], *, returncode: int = 0, stdout: str = ""):
    def _run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    return _run


def test_tofu_plan_detailed_asks_for_the_detailed_exit_code(monkeypatch, tmp_path):
    calls: List[List[str]] = []
    monkeypatch.setattr(runner, "tofu_path", lambda: "/bin/tofu")
    monkeypatch.setattr(runner.subprocess, "run", _fake_run(calls, returncode=2))
    result = runner.tofu_plan_detailed(str(tmp_path), out_file="fluid-drift.tfplan")
    assert calls[0][1:] == [
        "plan",
        "-input=false",
        "-no-color",
        "-detailed-exitcode",
        "-out=fluid-drift.tfplan",
    ]
    assert result.returncode == runner.PLAN_HAS_CHANGES
    assert result.ok is False  # 2 is "changes", never read through ``ok``


@pytest.mark.parametrize(
    "returncode, stdout, expected",
    [
        (0, '{"format_version": "1.2"}', {"format_version": "1.2"}),
        (0, "not json", None),
        (0, "[1, 2]", None),
        (1, "", None),
    ],
)
def test_tofu_show_plan(monkeypatch, tmp_path, returncode, stdout, expected):
    calls: List[List[str]] = []
    monkeypatch.setattr(runner, "tofu_path", lambda: "/bin/tofu")
    monkeypatch.setattr(
        runner.subprocess, "run", _fake_run(calls, returncode=returncode, stdout=stdout)
    )
    assert runner.tofu_show_plan(str(tmp_path), plan_file="p") == expected
    assert calls[0][1:] == ["show", "-json", "p"]
