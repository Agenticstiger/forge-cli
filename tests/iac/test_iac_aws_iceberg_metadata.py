# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Glue Iceberg table gets its first metadata file from Glue, and an existing one is
never replaced for it (``iac/providers/aws.py::_add_iceberg_metadata``).

Measured on real Glue and Athena (ca-central-1, 2026-11-17): a table created with
``open_table_format_input`` has a ``metadata_location`` and Athena reads it; one created
without it fails "Detected Iceberg type table without metadata location"; adding the block
to that existing table plans no change; a re-plan after create plans no change.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.module import build_module

pytestmark = [pytest.mark.unit, pytest.mark.provider]

TABLE = "lane_ice_lake_events"
CONTRACT = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "lane.ice",
    "name": "Iceberg events",
    "metadata": {"owner": {"team": "lane"}},
    "exposes": [
        {
            "exposeId": "events",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "lake",
                    "table": "events",
                    "bucket": "lane-lake",
                    "path": "iceberg/events/",
                    "region": "ca-central-1",
                },
            },
            "contract": {"schema": [{"name": "event_id", "type": "string"}]},
        }
    ],
}


@pytest.fixture
def contract():
    return copy.deepcopy(CONTRACT)


def table(contract):
    return get_iac_plugin("aws").emit(contract)["aws_glue_catalog_table"][TABLE]


def test_glue_writes_a_new_iceberg_tables_first_metadata(contract):
    body = table(contract)
    assert body["open_table_format_input"] == {
        "iceberg_input": {"metadata_operation": "CREATE", "version": "2"}
    }


def test_an_existing_table_is_never_replaced_and_engine_commits_never_drift(contract):
    # ForceNew and never read back, so an existing table's state keeps its own value;
    # metadata_location lives in the already-ignored parameters.
    assert table(contract)["lifecycle"] == {
        "ignore_changes": ["parameters", "open_table_format_input"]
    }


def test_the_reserved_table_type_is_left_to_glue(contract):
    parameters = table(contract)["parameters"]
    assert "table_type" not in parameters and "metadata_location" not in parameters
    assert parameters["classification"] == "iceberg"


def test_the_table_waits_for_the_bucket_glue_writes_into(contract):
    assert table(contract)["depends_on"] == ["aws_s3_bucket.lane_ice_lane_lake"]


def test_a_shared_bucket_is_not_waited_for(contract):
    contract["packaging"] = {"mode": "shared", "pool": "lake-pool"}
    assert "depends_on" not in table(contract)


def test_a_parquet_table_is_unchanged(contract):
    contract["exposes"][0]["binding"]["format"] = "parquet"
    body = table(contract)
    assert "open_table_format_input" not in body and "depends_on" not in body
    assert body["lifecycle"] == {"ignore_changes": ["parameters"]}
    assert "table_type" not in body["parameters"]


# --- an existing table plans no change (recorded state, real OpenTofu and provider) ---

_TOFU = shutil.which("tofu")
#: The state real Glue left for a table created before this change (no open_table_format_input),
#: recorded in ca-central-1 and with the account replaced by zeros.
_BEFORE = (
    Path(__file__).resolve().parents[1]
    / "fixtures/iac/aws_iceberg_table_before_metadata.tfstate.json"
)
_UPGRADE = {
    **CONTRACT,
    "id": "lane.sparkinfra-ice-upgrade",
    "name": "Spark infra Iceberg upgrade",
    "exposes": [
        {
            **CONTRACT["exposes"][0],
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "sparkinfra_ice_upgrade",
                    "table": "events",
                    "bucket": "fluid-lane-sparkinfra-ice-upgrade",
                    "path": "iceberg/events/",
                    "region": "ca-central-1",
                },
            },
            "contract": {
                "schema": [
                    {"name": "event_id", "type": "string", "required": True},
                    {"name": "amount", "type": "decimal(18,2)"},
                ]
            },
        }
    ],
}


@pytest.mark.integration
@pytest.mark.skipif(_TOFU is None, reason="tofu not on PATH")
def test_a_table_created_before_metadata_plans_no_change(tmp_path):
    """No ForceNew replace, so the data-loss gate has nothing to stop."""
    from tests.iac.test_iac_tofu_validate import _tofu_init_or_skip

    plugin = get_iac_plugin("aws")
    (tmp_path / "main.tf.json").write_text(build_module(plugin, _UPGRADE))
    # Credential-free: no refresh and no data source, so the provider calls nothing.
    (tmp_path / "provider_override.tf.json").write_text(
        json.dumps(
            {
                "provider": {
                    "aws": {
                        "access_key": "offline",
                        "secret_key": "offline",  # pragma: allowlist secret - a placeholder
                        "skip_credentials_validation": True,
                        "skip_requesting_account_id": True,
                        "skip_metadata_api_check": True,
                    }
                }
            }
        )
    )
    shutil.copy(_BEFORE, tmp_path / "terraform.tfstate")
    _tofu_init_or_skip(tmp_path)
    planned = subprocess.run(
        [_TOFU, "plan", "-refresh=false", "-input=false", "-no-color", "-out=tfplan"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert planned.returncode == 0, planned.stderr or planned.stdout
    shown = subprocess.run(
        [_TOFU, "show", "-json", "tfplan"], cwd=tmp_path, capture_output=True, text=True
    )
    plan = json.loads(shown.stdout)
    actions = {rc["address"]: rc["change"]["actions"] for rc in plan["resource_changes"]}
    table_address = (
        "aws_glue_catalog_table.lane_sparkinfra_ice_upgrade_sparkinfra_ice_upgrade_events"
    )
    assert actions[table_address] == ["no-op"], actions
    assert all(a == ["no-op"] for a in actions.values()), actions
    assert plugin.plan_data_changes(plan) == []
