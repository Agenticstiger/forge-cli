# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``--adopt-tables``: an existing Glue table taken over with the real ``tofu`` against moto.

The tables are made the way their writers make them (an Iceberg table as Iceberg's Glue
catalog writes it: ``table_type`` ICEBERG, a ``metadata_location``, an
``iceberg.field.*`` parameter set on every column; a Hive table with its Parquet input
and output formats and SerDe), then a contract names them:

* one that matches is planned through an ``import`` block (``fluid plan --tofu``), the
  saved plan destroys nothing, and ``fluid apply plan.json`` takes it over: the state
  holds it, its metadata location is untouched, and the next plan is empty; what it
  holds of its own that the contract does not declare (a description, an owner, column
  comments) is left as it is: the adoption updates no Iceberg table in place;
* one that differs, or a Hive table under an Iceberg binding, is refused naming the
  differences, with nothing in state and the table as it was;
* without the flag an existing table is refused, unless forge made it: a table that
  carries forge's marker, met again after its state was lost, is imported as before;
* the Lake Formation grants the contract declares are planned beside the adopted
  table, which is imported, not replaced;
* a partitioned Hive table cannot be adopted: its partition trigger is new to the state,
  and OpenTofu replaces a resource whose ``replace_triggered_by`` target is created.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed;
``iac-tests.yml`` Stage 1 installs both and fails if this file only skipped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.forge.core.plan_digest import TOFU_PLAN_FIELD, inject_digests
from fluid_build.iac.naming import safe_ident
from tests.iac.test_iac_aws_apply_engine_moto import (  # noqa: F401 - moto_endpoint is a fixture
    _LOG,
    _REGION,
    _SKIP,
    _SKIP_REASON,
    _apply_args,
    _boto,
    _write_plan,
    moto_endpoint,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.aws,
    pytest.mark.skipif(_SKIP, reason=_SKIP_REASON),
]

ACCOUNT = "123456789012"  # moto's account
DATABASE = "adopt_silver"
TABLE = "events"
ANALYST = f"arn:aws:iam::{ACCOUNT}:role/analyst"


@pytest.fixture(autouse=True)
def _account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The account a Glue import id names, so nothing asks STS for it."""
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)


def _sidecar(workspace: Path, cid: str, endpoint: str) -> Path:
    """The provider override aiming every service the module uses at moto (Lake
    Formation included), in the engine's own workdir."""
    workdir = workspace / ".fluid" / "iac" / "aws" / safe_ident(cid)
    workdir.mkdir(parents=True, exist_ok=True)
    services = ("s3", "glue", "sts", "iam", "kinesis", "lambda", "events", "lakeformation")
    override = {
        "provider": {
            "aws": {
                "region": _REGION,
                "access_key": "testing",
                "secret_key": "testing",  # pragma: allowlist secret - moto dummy
                "skip_credentials_validation": True,
                "skip_metadata_api_check": True,
                "skip_requesting_account_id": True,
                "s3_use_path_style": True,
                "endpoints": {service: endpoint for service in services},
            }
        }
    }
    (workdir / "provider_override.tf.json").write_text(json.dumps(override), encoding="utf-8")
    return workdir


def _contract(
    bucket: str,
    cid: str,
    *,
    format_: str = "iceberg",
    path: str = "silver/events",
    schema: Sequence[Dict[str, Any]] = ({"name": "id", "type": "bigint", "required": True},),
    grants: Optional[List[Dict[str, Any]]] = None,
    partition_by: Optional[List[str]] = None,
) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "database": DATABASE,
        "table": TABLE,
        "bucket": bucket,
        "path": path,
    }
    if partition_by:
        location["partitionBy"] = partition_by
    binding: Dict[str, Any] = {"platform": "aws", "format": format_, "location": location}
    if grants is not None:
        binding["governance"] = {"lakeFormation": {"grants": grants}}
    return {
        "id": cid,
        "name": "Adoption",
        "exposes": [{"exposeId": TABLE, "binding": binding, "contract": {"schema": list(schema)}}],
    }


def _iceberg_column(name: str, type_: str, field_id: int, current: bool = True) -> Dict[str, Any]:
    return {
        "Name": name,
        "Type": type_,
        "Parameters": {
            "iceberg.field.id": str(field_id),
            "iceberg.field.optional": "false" if field_id == 1 else "true",
            "iceberg.field.current": "true" if current else "false",
        },
    }


def _existing_iceberg_table(
    endpoint: str,
    bucket: str,
    *,
    columns: Optional[List[Dict[str, Any]]] = None,
    location: Optional[str] = None,
) -> str:
    """Make the bucket, the database and an Iceberg table as Iceberg's Glue catalog writes
    them (``GlueTableOperations``). Returns the table's metadata location."""
    location = location or f"s3://{bucket}/silver/events"
    metadata = f"{location}/metadata/00003-6f0c.metadata.json"
    _boto("s3", endpoint).create_bucket(Bucket=bucket)
    glue = _boto("glue", endpoint)
    glue.create_database(DatabaseInput={"Name": DATABASE})
    glue.create_table(
        DatabaseName=DATABASE,
        TableInput={
            "Name": TABLE,
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {
                "table_type": "ICEBERG",
                "metadata_location": metadata,
                "previous_metadata_location": f"{location}/metadata/00002-1a2b.metadata.json",
            },
            "StorageDescriptor": {
                "Location": location,
                "Columns": columns or [_iceberg_column("id", "bigint", 1)],
            },
        },
    )
    return metadata


def _existing_hive_table(
    endpoint: str, bucket: str, *, partition_keys: Sequence[Dict[str, str]] = ()
) -> None:
    _boto("s3", endpoint).create_bucket(Bucket=bucket)
    glue = _boto("glue", endpoint)
    glue.create_database(DatabaseInput={"Name": DATABASE})
    glue.create_table(
        DatabaseName=DATABASE,
        TableInput={
            "Name": TABLE,
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {"classification": "parquet", "EXTERNAL": "TRUE"},
            "PartitionKeys": list(partition_keys),
            "StorageDescriptor": {
                "Location": f"s3://{bucket}/silver/events/",
                "Columns": [{"Name": "id", "Type": "bigint"}],
                "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                "SerdeInfo": {
                    "SerializationLibrary": (
                        "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                    ),
                    "Parameters": {"serialization.format": "1"},
                },
            },
        },
    )


def _table_address(cid: str) -> str:
    return f"aws_glue_catalog_table.{safe_ident(f'{safe_ident(cid)}_{DATABASE}_{TABLE}')}"


def _state_addresses(workdir: Path) -> List[str]:
    state = workdir / "terraform.tfstate"
    if not state.exists():
        return []
    document = json.loads(state.read_text(encoding="utf-8"))
    return sorted(
        f"{resource['type']}.{resource['name']}"
        for resource in document.get("resources", [])
        if resource.get("mode") == "managed"
    )


def _saved_plan(tmp_path: Path, contract: Dict[str, Any], **over: Any) -> Path:
    """``fluid plan --tofu --adopt-tables``: the saved plan, bound into a digested plan.json."""
    out = tmp_path / "plan.json"
    args = _apply_args(_write_plan(tmp_path / "contract.json", contract), tmp_path, **over)
    record = engine.plan_with_opentofu(args, _LOG, contract, str(out))
    out.write_text(
        json.dumps(inject_digests({"contract": contract, "actions": [], TOFU_PLAN_FIELD: record})),
        encoding="utf-8",
    )
    return out


def _shown(tmp_path: Path, plan: Path) -> Dict[str, Dict[str, Any]]:
    record = json.loads(plan.read_text(encoding="utf-8"))[TOFU_PLAN_FIELD]
    shown = json.loads((tmp_path / record["showJson"]).read_text(encoding="utf-8"))
    return {change["address"]: change["change"] for change in shown["resource_changes"]}


def _summary(out: str) -> tuple:
    found = re.findall(r"tofu plan: \+(\d+) ~(\d+) -(\d+)", out)
    assert found, out
    return found[-1]


def test_a_matching_iceberg_table_is_adopted_and_keeps_its_metadata(
    moto_endpoint: str, tmp_path: Path, capfd
) -> None:
    cid = "iac.adopt.match"
    bucket = "adopt-match-lake"
    metadata = _existing_iceberg_table(moto_endpoint, bucket)
    workdir = _sidecar(tmp_path, cid, moto_endpoint)
    contract = _contract(bucket, cid)

    plan = _saved_plan(tmp_path, contract, adopt_tables=True)

    record = json.loads(plan.read_text(encoding="utf-8"))[TOFU_PLAN_FIELD]
    assert record["adoptedTables"] == [f"{DATABASE}.{TABLE}"]
    changes = _shown(tmp_path, plan)
    table = changes[_table_address(cid)]
    assert table["importing"] == {"id": f"{ACCOUNT}:{DATABASE}:{TABLE}"}
    assert table["actions"] == ["no-op"]
    assert not [address for address, change in changes.items() if "delete" in change["actions"]]
    assert _state_addresses(workdir) == [], "the plan wrote no state"
    assert f"{DATABASE}.{TABLE} matches the contract" in " ".join(capfd.readouterr().out.split())

    assert (
        engine.apply_via_opentofu(_apply_args(plan, tmp_path, no_verify_plan_binding=False), _LOG)
        == 0
    )

    assert _table_address(cid) in _state_addresses(workdir)
    live = _boto("glue", moto_endpoint).get_table(DatabaseName=DATABASE, Name=TABLE)["Table"]
    assert live["Parameters"]["metadata_location"] == metadata
    assert live["Parameters"]["table_type"] == "ICEBERG"
    assert [column["Name"] for column in live["StorageDescriptor"]["Columns"]] == ["id"]

    capfd.readouterr()
    again = _apply_args(_write_plan(tmp_path / "contract.json", contract), tmp_path, dry_run=True)
    assert engine.apply_via_opentofu(again, _LOG) == 0
    assert _summary(capfd.readouterr().out) == ("0", "0", "0")


def test_a_table_forge_made_is_re_attached_after_its_state_is_lost(
    moto_endpoint: str, tmp_path: Path
) -> None:
    """A fresh workspace on every run keeps no state: the next apply meets the table forge
    made and imports it as before, with no flag, and can still change it."""
    cid = "iac.adopt.own"
    bucket = "adopt-own-lake"
    workdir = _sidecar(tmp_path, cid, moto_endpoint)
    plan = _write_plan(tmp_path / "contract.json", _contract(bucket, cid))
    assert engine.apply_via_opentofu(_apply_args(plan, tmp_path), _LOG) == 0
    (workdir / "terraform.tfstate").unlink()
    grown = _contract(
        bucket,
        cid,
        schema=[
            {"name": "id", "type": "bigint", "required": True},
            {"name": "note", "type": "string"},
        ],
    )

    again = _write_plan(tmp_path / "contract.json", grown)
    assert engine.apply_via_opentofu(_apply_args(again, tmp_path), _LOG) == 0

    assert _table_address(cid) in _state_addresses(workdir)
    live = _boto("glue", moto_endpoint).get_table(DatabaseName=DATABASE, Name=TABLE)["Table"]
    assert [column["Name"] for column in live["StorageDescriptor"]["Columns"]] == ["id", "note"]


def test_an_adopted_iceberg_table_with_its_own_drift_plans_no_update_and_keeps_it(
    moto_endpoint: str, tmp_path: Path, capfd
) -> None:
    """A description, an owner, a column comment and a column of an older schema that the
    contract does not declare: the comparison lets them differ, and an adopted Iceberg
    table is never updated in place, so the saved plan imports it with no change and the
    table keeps them."""
    cid = "iac.adopt.drift"
    bucket = "adopt-drift-lake"
    commented = dict(_iceberg_column("id", "bigint", 1), Comment="the order")
    metadata = _existing_iceberg_table(
        moto_endpoint,
        bucket,
        columns=[commented, _iceberg_column("legacy", "string", 2, current=False)],
    )
    glue = _boto("glue", moto_endpoint)
    table = glue.get_table(DatabaseName=DATABASE, Name=TABLE)["Table"]
    glue.update_table(
        DatabaseName=DATABASE,
        TableInput={
            "Name": TABLE,
            "TableType": table["TableType"],
            "Owner": "hadoop",
            "Description": "written by the stream",
            "Parameters": table["Parameters"],
            "StorageDescriptor": table["StorageDescriptor"],
        },
    )
    _sidecar(tmp_path, cid, moto_endpoint)

    plan = _saved_plan(tmp_path, _contract(bucket, cid), adopt_tables=True)

    changes = _shown(tmp_path, plan)
    change = changes[_table_address(cid)]
    assert change["importing"] is not None and change["actions"] == ["no-op"]
    assert not [
        address
        for address, planned in changes.items()
        if address.startswith("aws_glue_catalog_table.") and "update" in planned["actions"]
    ]
    out = " ".join(capfd.readouterr().out.split())
    assert "leaves as it is (an adopted Iceberg table is not updated in place)" in out

    assert (
        engine.apply_via_opentofu(_apply_args(plan, tmp_path, no_verify_plan_binding=False), _LOG)
        == 0
    )

    live = glue.get_table(DatabaseName=DATABASE, Name=TABLE)["Table"]
    assert live["Description"] == "written by the stream" and live["Owner"] == "hadoop"
    assert [c.get("Comment") for c in live["StorageDescriptor"]["Columns"]] == ["the order", None]
    assert live["Parameters"]["metadata_location"] == metadata


def test_a_table_that_differs_is_refused_naming_its_columns_and_location(
    moto_endpoint: str, tmp_path: Path
) -> None:
    cid = "iac.adopt.differs"
    bucket = "adopt-differs-lake"
    legacy = f"s3://{bucket}/legacy/events"
    _existing_iceberg_table(
        moto_endpoint,
        bucket,
        columns=[_iceberg_column("id", "bigint", 1), _iceberg_column("note", "string", 2)],
        location=legacy,
    )
    workdir = _sidecar(tmp_path, cid, moto_endpoint)
    plan = _write_plan(tmp_path / "contract.json", _contract(bucket, cid))

    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_apply_args(plan, tmp_path, adopt_tables=True), _LOG)

    assert raised.value.event == "table_adoption_mismatch"
    error = raised.value.context["error"]
    assert "column note (string): in the table, not in the contract" in error
    assert f"location: {legacy} in the table, s3://{bucket}/silver/events in the contract" in error
    assert _state_addresses(workdir) == []
    live = _boto("glue", moto_endpoint).get_table(DatabaseName=DATABASE, Name=TABLE)["Table"]
    assert live["StorageDescriptor"]["Location"] == legacy
    assert [column["Name"] for column in live["StorageDescriptor"]["Columns"]] == ["id", "note"]


def test_without_the_flag_an_existing_table_is_refused_and_left_alone(
    moto_endpoint: str, tmp_path: Path
) -> None:
    cid = "iac.adopt.unasked"
    bucket = "adopt-unasked-lake"
    metadata = _existing_iceberg_table(moto_endpoint, bucket)
    workdir = _sidecar(tmp_path, cid, moto_endpoint)
    plan = _write_plan(tmp_path / "contract.json", _contract(bucket, cid))

    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_apply_args(plan, tmp_path), _LOG)

    assert raised.value.event == "table_adoption_requires_flag"
    assert raised.value.context["tables"] == [f"{DATABASE}.{TABLE}"]
    assert _state_addresses(workdir) == []
    live = _boto("glue", moto_endpoint).get_table(DatabaseName=DATABASE, Name=TABLE)["Table"]
    assert live["Parameters"]["metadata_location"] == metadata


def test_lake_formation_grants_are_planned_beside_the_adopted_table(
    moto_endpoint: str, tmp_path: Path
) -> None:
    cid = "iac.adopt.grants"
    bucket = "adopt-grants-lake"
    _existing_iceberg_table(moto_endpoint, bucket)
    _sidecar(tmp_path, cid, moto_endpoint)
    contract = _contract(
        bucket, cid, grants=[{"principal": ANALYST, "permissions": ["SELECT", "DESCRIBE"]}]
    )

    plan = _saved_plan(tmp_path, contract, adopt_tables=True)

    changes = _shown(tmp_path, plan)
    assert changes[_table_address(cid)]["importing"] is not None
    assert changes[_table_address(cid)]["actions"] == ["no-op"]
    grants = {
        address: change
        for address, change in changes.items()
        if address.startswith("aws_lakeformation_permissions.")
    }
    assert grants, "the contract's grant is planned"
    assert all(change["actions"] == ["create"] for change in grants.values())
    assert {change["after"]["principal"] for change in grants.values()} == {ANALYST}
    assert not [address for address, change in changes.items() if "delete" in change["actions"]]


def test_a_hive_table_under_an_iceberg_binding_is_refused(
    moto_endpoint: str, tmp_path: Path
) -> None:
    cid = "iac.adopt.hive"
    bucket = "adopt-hive-lake"
    _existing_hive_table(moto_endpoint, bucket)
    workdir = _sidecar(tmp_path, cid, moto_endpoint)
    plan = _write_plan(tmp_path / "contract.json", _contract(bucket, cid))

    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_apply_args(plan, tmp_path, adopt_tables=True), _LOG)

    assert raised.value.event == "table_adoption_mismatch"
    error = raised.value.context["error"]
    assert "input format: org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat" in error
    assert _state_addresses(workdir) == []


def test_a_partitioned_hive_table_is_refused_because_opentofu_would_replace_it(
    moto_endpoint: str, tmp_path: Path
) -> None:
    """Its partition trigger (``terraform_data``) is new to the state, and OpenTofu (1.12
    and later) replaces a resource whose ``replace_triggered_by`` target is created."""
    cid = "iac.adopt.partitioned"
    bucket = "adopt-partitioned-lake"
    _existing_hive_table(moto_endpoint, bucket, partition_keys=[{"Name": "day", "Type": "date"}])
    workdir = _sidecar(tmp_path, cid, moto_endpoint)
    contract = _contract(
        bucket,
        cid,
        format_="parquet",
        path="silver/events/",
        schema=[
            {"name": "id", "type": "bigint", "required": True},
            {"name": "day", "type": "date"},
        ],
        partition_by=["day"],
    )
    plan = _write_plan(tmp_path / "contract.json", contract)

    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_apply_args(plan, tmp_path, adopt_tables=True), _LOG)

    assert raised.value.event == "table_adoption_mismatch"
    assert "OpenTofu would replace it (a replace_triggered_by trigger fires)" in (
        raised.value.context["error"]
    )
    assert _state_addresses(workdir) == []
    live = _boto("glue", moto_endpoint).get_table(DatabaseName=DATABASE, Name=TABLE)["Table"]
    assert [key["Name"] for key in live["PartitionKeys"]] == ["day"]
