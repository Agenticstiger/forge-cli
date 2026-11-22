# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Edges of ``exposes[].iceberg.maintenance`` that a contract which skipped the schema can reach:
each is an error naming the field the contract wrote, never a crash and never a silent drop."""

from __future__ import annotations

import copy

import yaml

from fluid_build.maintenance.spec import read_maintenance

from .conftest import EXPOSE, glue_block, maintained, spark_block


def _errors(contract):
    tables, errors = read_maintenance(contract)
    assert not tables or not errors
    return errors


def test_a_max_commits_that_is_not_a_number_is_an_error_not_a_crash() -> None:
    for value in ("abc", [1], 2.5):
        errors = _errors(maintained(spark_block(compaction={"maxCommits": value})))
        assert any("compaction.maxCommits must be a whole number" in e for e in errors), errors


def test_the_legacy_target_file_size_is_backend_sparks_default_only() -> None:
    legacy = {"compaction": {"targetFileSizeMb": 512}}
    tables, errors = read_maintenance(maintained(glue_block(), legacy=legacy))
    assert not errors
    assert tables[0].compaction is not None and tables[0].compaction.target_file_size_mb is None
    (table,) = read_maintenance(maintained(spark_block(), legacy=legacy))[0]
    assert table.compaction is not None and table.compaction.target_file_size_mb == 512
    # Written in the block itself, Glue's optimizer still has no such setting.
    errors = _errors(maintained(glue_block(compaction={"targetFileSizeMb": 512})))
    assert any("Glue's optimizer reads the table's" in e for e in errors), errors


def test_a_bad_legacy_default_names_the_legacy_field() -> None:
    errors = _errors(
        maintained(
            spark_block(expireSnapshots={}),
            legacy={"snapshotRetention": {"minSnapshotsToKeep": 0}},
        )
    )
    assert any(e.startswith("iceberg.snapshotRetention.minSnapshotsToKeep must be") for e in errors)
    errors = _errors(
        maintained(
            glue_block(expireSnapshots={}),
            legacy={"snapshotRetention": {"maxSnapshotAgeMs": 36 * 3600 * 1000}},
        )
    )
    assert any(
        e.startswith("iceberg.snapshotRetention.maxSnapshotAgeMs must be whole days")
        for e in errors
    ), errors


def test_run_every_hours_with_only_compaction_is_refused_on_glue() -> None:
    errors = _errors(maintained(glue_block(runEveryHours=6)))
    assert any("runEveryHours paces Glue's retention" in e for e in errors), errors
    tables, errors = read_maintenance(maintained(glue_block(runEveryHours=6, expireSnapshots={})))
    assert not errors and tables[0].run_every_hours == 6


def test_a_build_named_like_the_tables_maintenance_is_refused() -> None:
    contract = maintained(spark_block())
    other = copy.deepcopy(contract["builds"][0])
    other["id"] = f"maintain-{EXPOSE}"  # its DAG file and plan action spell it maintain_<expose>
    other["outputs"] = ["somewhere_else"]
    contract["builds"].append(other)
    errors = _errors(contract)
    assert any(
        f"build maintain-{EXPOSE} has the name this table's maintenance" in e for e in errors
    )


def test_another_scheduler_says_it_writes_no_maintenance_dag(tmp_path, capsys) -> None:
    from fluid_build.cli import main

    contract = maintained(spark_block())
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    main(
        ["generate", "schedule", str(path), "--scheduler", "dagster", "--out", str(tmp_path / "o")]
    )
    out = " ".join(capsys.readouterr().out.split())  # the console wraps long lines
    assert "only the Airflow fluid apply DAGs carry" in out
    assert "fluid apply --mode maintain --expose-id <expose>" in out


def test_an_expose_id_the_dag_cannot_take_is_refused_on_backend_spark() -> None:
    contract = maintained(spark_block())
    contract["exposes"][0]["exposeId"] = "2024_orders"
    contract["builds"][0]["outputs"] = ["2024_orders"]
    errors = _errors(contract)
    assert any("'2024_orders' is not an id they take" in e for e in errors), errors
    # Glue's optimizers name nothing after the expose.
    contract["exposes"][0]["iceberg"]["maintenance"] = glue_block()
    assert not read_maintenance(contract)[1]
