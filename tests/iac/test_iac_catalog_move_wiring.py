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

"""``fluid apply`` stops before ``tofu plan`` when a catalog move would destroy.

Behaviour through the real entrypoint, ``apply_via_opentofu``, with only the
``tofu`` shell-outs replaced: the state listing returns what an older
forge-cli applied for the contract, and ``tofu plan`` / ``tofu apply`` fail
the test if they are reached. The guard's own detection is covered in
``test_iac_catalog_moves.py``; this file pins that the engine runs it, for
the provider it applies, ahead of the plan.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import runner

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_LOG = logging.getLogger("test.iac.catalog_move_wiring")

_SCHEMA = [{"name": "order_id", "type": "string"}]

# What 0.19.0 applied for the contracts below, as ``tofu state list`` prints it.
_AWS_OLD_STATE = [
    "aws_glue_catalog_database.analytics_lake_sales",
    "aws_glue_catalog_table.analytics_lake_sales_orders",
    "aws_s3_bucket.analytics_lake_lake",
]
_SNOWFLAKE_OLD_STATE = [
    "snowflake_database.analytics_lake_ANALYTICS",
    "snowflake_external_volume.analytics_lake_vol_FLUID_ANALYTICS_LAKE_VOL",
    "snowflake_schema.analytics_lake_ANALYTICS_SALES",
    "snowflake_table.analytics_lake_ANALYTICS_SALES_ORDERS",
]


def _aws_contract(catalog: Optional[str]) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "database": "sales",
        "table": "orders",
        "bucket": "lake",
        "path": "orders/",
        "region": "eu-west-1",
    }
    if catalog:
        location.update(catalog=catalog, uri="http://lakekeeper:8181/catalog", warehouse="lake")
    return {
        "id": "analytics.lake",
        "name": "Lake",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"platform": "aws", "format": "iceberg", "location": location},
                "contract": {"schema": list(_SCHEMA)},
            }
        ],
    }


def _snowflake_contract(catalog: Optional[str]) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "database": "ANALYTICS",
        "schema": "SALES",
        "table": "ORDERS",
        "warehouse": "s3://lake/warehouse",
        "iam_role_arn": "arn:aws:iam::123456789012:role/snowflake-lake",
    }
    if catalog:
        location.update(catalog=catalog, uri="http://lakekeeper:8181/catalog")
    return {
        "id": "analytics.lake",
        "name": "Lake",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"platform": "snowflake", "format": "iceberg", "location": location},
                "contract": {"schema": list(_SCHEMA)},
            }
        ],
    }


class _Tofu:
    """The ``tofu`` shell-outs ``apply_via_opentofu`` makes, recorded."""

    def __init__(self, monkeypatch, state: List[str], *, plan_allowed: bool) -> None:
        self.calls: List[str] = []
        self.state = list(state)
        self.plan_allowed = plan_allowed
        ok = lambda command: runner.TofuResult(command, 0, "", "")  # noqa: E731

        def _unexpected(args, **_kw):
            raise AssertionError(f"unexpected tofu {' '.join(args)}")

        def _record(name, result=None):
            def _call(*_a, **_k):
                self.calls.append(name)
                return result() if callable(result) else result

            return _call

        def _plan(*_a, **_k):
            self.calls.append("plan")
            if not self.plan_allowed:
                raise AssertionError("tofu plan ran although the catalog-move guard should block")
            return runner.TofuResult("plan", 0, "", "", events=[])

        def _apply(*_a, **_k):
            raise AssertionError("tofu apply ran")

        monkeypatch.setattr(runner, "_run", _unexpected)
        monkeypatch.setattr(runner, "tofu_path", lambda: "/usr/bin/tofu")
        monkeypatch.setattr(runner, "require_tofu_version", lambda: None)
        monkeypatch.setattr(runner, "tofu_init", _record("init", lambda: ok("init")))
        monkeypatch.setattr(runner, "tofu_state_list", _record("state-list", lambda: self.state))
        monkeypatch.setattr(runner, "tofu_state_resources", _record("state-resources", list))
        monkeypatch.setattr(runner, "tofu_prior_state_resources", _record("prior-state", list))
        monkeypatch.setattr(runner, "tofu_state_pull", _record("state-pull", lambda: ok("pull")))
        monkeypatch.setattr(
            runner, "tofu_import", _record("import", runner.TofuResult("import", 1, "", "absent"))
        )
        monkeypatch.setattr(runner, "tofu_plan", _plan)
        monkeypatch.setattr(runner, "tofu_apply", _apply)


def _apply(monkeypatch, tmp_path: Path, contract: Dict[str, Any], *, dry_run: bool) -> int:
    monkeypatch.delenv("FLUID_STATE_BACKEND", raising=False)
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    args = argparse.Namespace(
        contract=str(path),
        env=None,
        provider=None,
        workspace_dir=str(tmp_path),
        state_backend=None,
        dry_run=dry_run,
        allow_data_loss=False,
    )
    return engine.apply_via_opentofu(args, _LOG)


def _relocated(contract: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
    """``contract`` with its one expose's location changed (``None`` drops a key)."""
    location = contract["exposes"][0]["binding"]["location"]
    for key, value in changes.items():
        if value is None:
            location.pop(key, None)
        else:
            location[key] = value
    return contract


_BLOCKED = [
    pytest.param(
        _aws_contract("lakekeeper"),
        _AWS_OLD_STATE,
        [_AWS_OLD_STATE[0], _AWS_OLD_STATE[1]],
        id="aws-glue-database-and-table",
    ),
    # RT-707-1: a namespace-level expose (a database, no table); 0.19.0 created
    # only the Glue database for it, so no Glue table can prove it.
    pytest.param(
        _relocated(_aws_contract("lakekeeper"), table=None),
        [_AWS_OLD_STATE[0], _AWS_OLD_STATE[2]],
        [_AWS_OLD_STATE[0]],
        id="aws-table-less-glue-database",
    ),
    pytest.param(
        _snowflake_contract("lakekeeper"),
        _SNOWFLAKE_OLD_STATE,
        [_SNOWFLAKE_OLD_STATE[1]],
        id="snowflake-external-volume",
    ),
    # JRN-707-1: the same upgrade also changed the location, so today's
    # emitter can no longer derive the volume the old release created.
    pytest.param(
        _relocated(_snowflake_contract("lakekeeper"), warehouse="analytics"),
        _SNOWFLAKE_OLD_STATE,
        [_SNOWFLAKE_OLD_STATE[1]],
        id="snowflake-volume-warehouse-now-a-name",
    ),
    pytest.param(
        _relocated(_snowflake_contract("lakekeeper"), iam_role_arn=None),
        _SNOWFLAKE_OLD_STATE,
        [_SNOWFLAKE_OLD_STATE[1]],
        id="snowflake-volume-without-iam-role",
    ),
]


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
@pytest.mark.parametrize("contract, state, released", _BLOCKED)
def test_a_catalog_move_stops_the_apply_before_plan(
    monkeypatch, tmp_path, contract, state, released, dry_run
):
    tofu = _Tofu(monkeypatch, state, plan_allowed=False)

    with pytest.raises(CLIError) as excinfo:
        _apply(monkeypatch, tmp_path, contract, dry_run=dry_run)

    assert excinfo.value.event == "iceberg_catalog_move_blocked"
    assert "plan" not in tofu.calls
    remediation = excinfo.value.context["remediation"]
    assert [command.rsplit(" ", 1)[-1] for command in remediation] == released
    assert all(" state rm " in command for command in remediation)


_PASSES = [
    pytest.param(_aws_contract(None), _AWS_OLD_STATE, id="aws-still-glue"),
    pytest.param(
        _aws_contract("lakekeeper"),
        [a for a in _AWS_OLD_STATE if "glue" not in a],
        id="aws-applied-by-this-release",
    ),
    pytest.param(_snowflake_contract(None), _SNOWFLAKE_OLD_STATE, id="snowflake-still-managed"),
    pytest.param(
        _snowflake_contract("lakekeeper"),
        [a for a in _SNOWFLAKE_OLD_STATE if "external_volume" not in a],
        id="snowflake-applied-by-this-release",
    ),
    pytest.param(_aws_contract("lakekeeper"), [], id="fresh-workdir"),
]


@pytest.mark.parametrize("contract, state", _PASSES)
def test_nothing_to_release_reaches_plan(monkeypatch, tmp_path, contract, state, caplog):
    tofu = _Tofu(monkeypatch, state, plan_allowed=True)

    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        assert _apply(monkeypatch, tmp_path, contract, dry_run=True) == 0

    assert "plan" in tofu.calls
    assert "iceberg_catalog_move_probe_skipped" not in caplog.text


def test_the_blocked_event_links_to_the_guard_section():
    """The CLI prints the docs link the catalog maps the event to; unmapped, it
    fell back to the generic troubleshooting page, which never mentions it."""
    from fluid_build._error_catalog import docs_url_for, suggestions_for

    url = docs_url_for("iceberg_catalog_move_blocked")
    assert url is not None and url.endswith("cli/apply.html#iceberg-catalog-move-guard")
    assert any("tofu state rm" in s for s in suggestions_for("iceberg_catalog_move_blocked"))
