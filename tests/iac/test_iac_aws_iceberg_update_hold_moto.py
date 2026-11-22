# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A live Iceberg table the plan would update in place is left as it is, with the real
``tofu`` against moto.

hashicorp/aws 6.73 updates an ``aws_glue_catalog_table`` with an ``UpdateTable`` that
carries no ``VersionId`` (``resourceCatalogTableUpdate``), so a commit an Iceberg writer
makes during the apply could be reverted. A table forge made is created through Glue's
Iceberg create, then an engine commits to it as Iceberg's Glue catalog does
(``GlueTableOperations``: a new ``metadata_location``, the old one as
``previous_metadata_location``, the columns with their Iceberg field ids, passing the
``VersionId`` it read). Then the contract changes the table's description and columns:

* ``fluid apply <contract>``, with the state kept or lost (a fresh workspace on every run),
  plans again leaving the table alone: Glue's ``VersionId`` does not move, so no
  ``UpdateTable`` was sent, and the note names what the contract wanted;
* ``--allow-iceberg-update`` makes the update and audits it;
* ``fluid plan --tofu`` saves the plan that leaves the table alone, or, with the flag, the
  one that updates it, recorded under ``planDigest``; ``fluid apply plan.json`` updates
  only what that record names;
* a Hive table keeps its in-place update; a rename stays with the data-loss gate.

Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is installed;
``iac-tests.yml`` Stage 1 installs both and fails if this file only skipped.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.forge.core.plan_digest import TOFU_PLAN_FIELD, inject_digests
from tests.iac.test_iac_aws_table_adoption_moto import (  # noqa: F401 - fixtures
    _LOG,
    _SKIP,
    _SKIP_REASON,
    DATABASE,
    TABLE,
    _account,
    _apply_args,
    _boto,
    _contract,
    _saved_plan,
    _shown,
    _sidecar,
    _state_addresses,
    _table_address,
    _write_plan,
    moto_endpoint,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.aws,
    pytest.mark.skipif(_SKIP, reason=_SKIP_REASON),
]

SCHEMA = ({"name": "id", "type": "bigint", "required": True},)
#: What the contract changes once the table holds data: a description, a column comment and
#: a column the writers do not write yet.
CHANGED_SCHEMA = (
    {"name": "id", "type": "bigint", "required": True, "description": "the event id"},
    {"name": "note", "type": "string"},
)


def _changed(bucket: str, cid: str, *, format_: str = "iceberg") -> Dict[str, Any]:
    contract = _contract(bucket, cid, format_=format_, schema=CHANGED_SCHEMA)
    contract["metadata"] = {"description": "Order events"}
    return contract


def _table(endpoint: str) -> Dict[str, Any]:
    table: Dict[str, Any] = _boto("glue", endpoint).get_table(DatabaseName=DATABASE, Name=TABLE)[
        "Table"
    ]
    return table


def _commit_as_iceberg_does(endpoint: str, sequence: int) -> str:
    """What ``GlueTableOperations.persistGlueTable`` sends for a commit: the table's location
    and columns with their field ids (``IcebergToGlueConverter``), its description kept, the
    new metadata file, the version it read. Returns the new metadata location."""
    table = _table(endpoint)
    storage = table["StorageDescriptor"]
    location = storage["Location"]
    columns = [
        dict(
            column,
            Parameters={
                "iceberg.field.id": str(field_id),
                "iceberg.field.optional": "false" if field_id == 1 else "true",
                "iceberg.field.current": "true",
            },
        )
        for field_id, column in enumerate(storage["Columns"], start=1)
    ]
    parameters = dict(table.get("Parameters") or {})
    previous = parameters.get("metadata_location") or f"{location}/metadata/00000-create.json"
    metadata = f"{location}/metadata/{sequence:05d}-commit.metadata.json"
    parameters.update(
        table_type="ICEBERG", metadata_location=metadata, previous_metadata_location=previous
    )
    table_input: Dict[str, Any] = {
        "Name": TABLE,
        "TableType": "EXTERNAL_TABLE",
        "StorageDescriptor": {"Location": location, "Columns": columns},
        "Parameters": parameters,
    }
    if table.get("Description"):
        table_input["Description"] = table["Description"]
    _boto("glue", endpoint).update_table(
        DatabaseName=DATABASE, TableInput=table_input, VersionId=table["VersionId"]
    )
    return metadata


def _made_and_written(endpoint: str, tmp_path: Path, cid: str, bucket: str) -> Path:
    """The table forge makes, then one commit of its writer. Returns the workdir."""
    workdir = _sidecar(tmp_path, cid, endpoint)
    made = _write_plan(tmp_path / "contract.json", _contract(bucket, cid, schema=SCHEMA))
    assert engine.apply_via_opentofu(_apply_args(made, tmp_path), _LOG) == 0
    _commit_as_iceberg_does(endpoint, 1)
    return workdir


def _held_events(caplog: pytest.LogCaptureFixture, event: str) -> List[Dict[str, Any]]:
    found = []
    for record in caplog.records:
        message = record.getMessage()
        if f'"message": "{event}"' in message:
            assert record.levelno == logging.WARNING
            found.append(json.loads(message))
    return found


def _assert_left_alone(endpoint: str, before: Dict[str, Any]) -> None:
    after = _table(endpoint)
    assert after["VersionId"] == before["VersionId"], "an UpdateTable was sent"
    assert after.get("Description") == before.get("Description")
    assert after["StorageDescriptor"]["Columns"] == before["StorageDescriptor"]["Columns"]
    assert after["Parameters"]["metadata_location"] == before["Parameters"]["metadata_location"]


@pytest.mark.parametrize("state", ["kept", "lost"])
def test_a_contract_change_to_a_live_iceberg_table_is_left_as_it_is(
    moto_endpoint: str, tmp_path: Path, capfd, caplog, state: str
) -> None:
    cid, bucket = f"iac.hold.{state}", f"hold-{state}-lake"
    workdir = _made_and_written(moto_endpoint, tmp_path, cid, bucket)
    if state == "lost":  # a fresh workspace on every run: the table is re-attached by import
        (workdir / "terraform.tfstate").unlink()
    written = _table(moto_endpoint)
    capfd.readouterr()

    changed = _write_plan(tmp_path / "contract.json", _changed(bucket, cid))
    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        assert engine.apply_via_opentofu(_apply_args(changed, tmp_path), _LOG) == 0

    _assert_left_alone(moto_endpoint, written)
    assert _table_address(cid) in _state_addresses(workdir)
    out = " ".join(capfd.readouterr().out.split())
    assert "(again: live Iceberg tables left as they are)" in out
    assert f"{DATABASE}.{TABLE} is a live Iceberg table" in out
    assert "description: none in the table, Order events in the contract" in out
    assert "column note (string): in the contract, not in the table" in out
    assert "comment of column id: none in the table, the event id in the contract" in out
    (held,) = _held_events(caplog, "iceberg_table_update_held")
    assert [entry["table"] for entry in held["tables"]] == [f"{DATABASE}.{TABLE}"]

    # The next run plans the same hold, and still sends nothing.
    assert engine.apply_via_opentofu(_apply_args(changed, tmp_path), _LOG) == 0
    _assert_left_alone(moto_endpoint, written)


def test_with_the_flag_the_update_is_made_and_audited(
    moto_endpoint: str, tmp_path: Path, caplog
) -> None:
    cid, bucket = "iac.hold.allowed", "hold-allowed-lake"
    _made_and_written(moto_endpoint, tmp_path, cid, bucket)
    written = _table(moto_endpoint)

    changed = _write_plan(tmp_path / "contract.json", _changed(bucket, cid))
    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        assert (
            engine.apply_via_opentofu(
                _apply_args(changed, tmp_path, allow_iceberg_update=True), _LOG
            )
            == 0
        )

    updated = _table(moto_endpoint)
    assert updated["VersionId"] != written["VersionId"]
    assert updated["Description"] == "Order events"
    assert [column["Name"] for column in updated["StorageDescriptor"]["Columns"]] == ["id", "note"]
    # The provider copied the live pointer back: with no commit in between, nothing is lost.
    assert updated["Parameters"]["metadata_location"] == written["Parameters"]["metadata_location"]
    (accepted,) = _held_events(caplog, "iceberg_table_update_accepted")
    assert [entry["table"] for entry in accepted["tables"]] == [f"{DATABASE}.{TABLE}"]
    assert not _held_events(caplog, "iceberg_table_update_held")


def test_plan_tofu_saves_the_plan_that_leaves_it_alone(moto_endpoint: str, tmp_path: Path) -> None:
    cid, bucket = "iac.hold.saved", "hold-saved-lake"
    _made_and_written(moto_endpoint, tmp_path, cid, bucket)
    written = _table(moto_endpoint)

    plan = _saved_plan(tmp_path, _changed(bucket, cid))

    record = json.loads(plan.read_text(encoding="utf-8"))[TOFU_PLAN_FIELD]
    assert "icebergUpdates" not in record
    assert _shown(tmp_path, plan)[_table_address(cid)]["actions"] == ["no-op"]
    args = _apply_args(plan, tmp_path, no_verify_plan_binding=False)
    assert engine.apply_via_opentofu(args, _LOG) == 0
    _assert_left_alone(moto_endpoint, written)


def test_plan_tofu_with_the_flag_records_the_update_that_its_apply_makes(
    moto_endpoint: str, tmp_path: Path, caplog
) -> None:
    cid, bucket = "iac.hold.recorded", "hold-recorded-lake"
    _made_and_written(moto_endpoint, tmp_path, cid, bucket)
    written = _table(moto_endpoint)

    plan = _saved_plan(tmp_path, _changed(bucket, cid), allow_iceberg_update=True)

    record = json.loads(plan.read_text(encoding="utf-8"))[TOFU_PLAN_FIELD]
    assert record["icebergUpdates"] == [f"{DATABASE}.{TABLE}"]
    assert _shown(tmp_path, plan)[_table_address(cid)]["actions"] == ["update"]
    # The apply of a saved plan takes no flag: the record decides.
    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        args = _apply_args(plan, tmp_path, no_verify_plan_binding=False)
        assert engine.apply_via_opentofu(args, _LOG) == 0

    updated = _table(moto_endpoint)
    assert updated["VersionId"] != written["VersionId"]
    assert updated["Description"] == "Order events"
    (accepted,) = _held_events(caplog, "iceberg_table_update_accepted")
    assert [entry["table"] for entry in accepted["tables"]] == [f"{DATABASE}.{TABLE}"]


def test_a_saved_plan_whose_record_names_no_update_is_refused(
    moto_endpoint: str, tmp_path: Path
) -> None:
    """The plan of a release before the hold: it updates the table in place, and its
    record, bound again, does not name it."""
    cid, bucket = "iac.hold.unrecorded", "hold-unrecorded-lake"
    _made_and_written(moto_endpoint, tmp_path, cid, bucket)
    written = _table(moto_endpoint)
    plan = _saved_plan(tmp_path, _changed(bucket, cid), allow_iceberg_update=True)
    document = json.loads(plan.read_text(encoding="utf-8"))
    del document[TOFU_PLAN_FIELD]["icebergUpdates"]
    plan.write_text(json.dumps(inject_digests(document, force=True)), encoding="utf-8")

    for flags in ({}, {"allow_iceberg_update": True}):
        args = _apply_args(plan, tmp_path, no_verify_plan_binding=False, **flags)
        with pytest.raises(CLIError) as raised:
            engine.apply_via_opentofu(args, _LOG)

        assert raised.value.event == "iceberg_table_update_in_place"
        assert raised.value.context["tables"][0]["table"] == f"{DATABASE}.{TABLE}"
    _assert_left_alone(moto_endpoint, written)


def test_a_hive_table_keeps_its_in_place_update(moto_endpoint: str, tmp_path: Path) -> None:
    cid, bucket = "iac.hold.hive", "hold-hive-lake"
    _sidecar(tmp_path, cid, moto_endpoint)
    made = _write_plan(
        tmp_path / "contract.json", _contract(bucket, cid, format_="parquet", schema=SCHEMA)
    )
    assert engine.apply_via_opentofu(_apply_args(made, tmp_path), _LOG) == 0
    written = _table(moto_endpoint)

    changed = _write_plan(tmp_path / "contract.json", _changed(bucket, cid, format_="parquet"))
    assert engine.apply_via_opentofu(_apply_args(changed, tmp_path), _LOG) == 0

    updated = _table(moto_endpoint)
    assert updated["VersionId"] != written["VersionId"]
    assert updated["Description"] == "Order events"
    assert [column["Name"] for column in updated["StorageDescriptor"]["Columns"]] == ["id", "note"]


def test_renaming_the_table_stays_with_the_data_loss_gate(
    moto_endpoint: str, tmp_path: Path
) -> None:
    cid, bucket = "iac.hold.renamed", "hold-renamed-lake"
    _made_and_written(moto_endpoint, tmp_path, cid, bucket)
    written = _table(moto_endpoint)
    renamed = copy.deepcopy(_contract(bucket, cid, schema=SCHEMA))
    renamed["exposes"][0]["binding"]["location"]["table"] = "events_v2"

    plan = _write_plan(tmp_path / "contract.json", renamed)
    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_apply_args(plan, tmp_path), _LOG)

    assert raised.value.event == "opentofu_data_loss_gate"
    assert _table(moto_endpoint)["VersionId"] == written["VersionId"]
