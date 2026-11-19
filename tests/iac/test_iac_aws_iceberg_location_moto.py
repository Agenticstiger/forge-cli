# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An Iceberg table's Glue location after an engine's commit plans no change, with the real
``tofu`` against moto.

Iceberg keeps a table's location without the trailing slash, and every commit writes it back
into Glue (``GlueTableOperations``) beside the new ``metadata_location``. forge emitted the
location with the slash, so each plan after a commit updated the table to put the slash back:
an ``UpdateTable`` beside a running stream's commits. The commit is played here as Iceberg
makes it, through boto3. Skipped unless ``tofu`` is on PATH and moto's ``server`` extra is
installed; ``iac-tests.yml`` Stage 1 installs both and fails if this file only skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from tests.iac.test_iac_aws_apply_engine_moto import (  # noqa: F401 - moto_endpoint is a fixture
    _LOG,
    _SKIP,
    _SKIP_REASON,
    _apply_args,
    _aws_iceberg_contract,
    _boto,
    _drop_moto_provider_sidecar,
    _write_plan,
    moto_endpoint,
)

pytestmark = [pytest.mark.integration, pytest.mark.provider, pytest.mark.aws]


def _commit_as_iceberg_does(glue, location: str) -> None:
    """What ``GlueTableOperations.doCommit`` sends: the table as it is, its location as
    Iceberg keeps it, and the new metadata file."""
    table = glue.get_table(DatabaseName="engine_silver", Name="events")["Table"]
    storage = dict(table["StorageDescriptor"], Location=location)
    parameters = dict(
        table.get("Parameters") or {},
        metadata_location=f"{location}/metadata/00001-commit.metadata.json",
        previous_metadata_location=f"{location}/metadata/00000-create.metadata.json",
    )
    glue.update_table(
        DatabaseName="engine_silver",
        TableInput={
            "Name": "events",
            "TableType": table.get("TableType", "EXTERNAL_TABLE"),
            "StorageDescriptor": storage,
            "Parameters": parameters,
        },
    )


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_a_plan_after_an_iceberg_commit_changes_nothing(
    moto_endpoint: str, tmp_path: Path, capsys
) -> None:
    cid = "iac.engine.iceberg-location"
    plan = _write_plan(tmp_path / "plan.json", _aws_iceberg_contract("iceberg-location-lake", cid))
    _drop_moto_provider_sidecar(tmp_path, cid, moto_endpoint)
    assert engine.apply_via_opentofu(_apply_args(plan, tmp_path), _LOG) == 0

    glue = _boto("glue", moto_endpoint)
    emitted = glue.get_table(DatabaseName="engine_silver", Name="events")["Table"]
    assert emitted["StorageDescriptor"]["Location"] == "s3://iceberg-location-lake/silver/events"

    _commit_as_iceberg_does(glue, "s3://iceberg-location-lake/silver/events")
    capsys.readouterr()
    assert engine.apply_via_opentofu(_apply_args(plan, tmp_path, dry_run=True), _LOG) == 0
    summary = re.search(r"tofu plan: \+(\d+) ~(\d+) -(\d+)", capsys.readouterr().out)
    assert summary is not None and summary.groups() == ("0", "0", "0")
