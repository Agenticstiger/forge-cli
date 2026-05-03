# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``fluid import`` full matrix tests (Slice K).

Four importers covered: Meltano project / Airbyte workspace / dlt pipeline /
Singer config. Every produced contract must validate against the v0.7.3
schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli.import_workflow import (
    AirbyteImporter,
    DltImporter,
    MeltanoImporter,
    SingerImporter,
    get_importer,
)
from fluid_build.schema_manager import FluidSchemaManager


@pytest.fixture(scope="module")
def schema_manager() -> FluidSchemaManager:
    return FluidSchemaManager()


# ── Registry ────────────────────────────────────────────────────────────


class TestImporterRegistry:
    def test_four_importers_registered(self):
        for name in ("meltano", "airbyte", "dlt", "singer"):
            assert get_importer(name) is not None

    def test_unknown_importer_returns_none(self):
        assert get_importer("dremio") is None


# ── Meltano importer ──────────────────────────────────────────────────


class TestMeltanoImporter:
    def test_imports_minimal_project(self, tmp_path: Path, schema_manager: FluidSchemaManager):
        project = tmp_path / "meltano-project"
        project.mkdir()
        (project / "meltano.yml").write_text(
            yaml.safe_dump(
                {
                    "default_environment": "dev",
                    "plugins": {
                        "extractors": [
                            {
                                "name": "tap-postgres",
                                "config": {
                                    "host": "db",
                                    "port": 5432,
                                    "database": "mydb",
                                    "user": "u",
                                    "password": "secretvalue",
                                },
                                "select": [
                                    "tap-postgres.public-orders.*",
                                    "tap-postgres.public-customers.*",
                                ],
                            }
                        ],
                        "loaders": [{"name": "target-snowflake"}],
                    },
                }
            ),
            encoding="utf-8",
        )
        importer = MeltanoImporter()
        assert importer.can_import(str(project))
        contract, report = importer.import_to_contract(str(project))
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors
        assert "extractor.tap-postgres" in report.mapped_one_to_one
        assert "loader.target-snowflake" in report.mapped_one_to_one
        # Streams from the select pattern.
        streams = contract["builds"][0]["properties"]["source"]["streams"]
        assert "public-orders" in streams
        assert "public-customers" in streams

    def test_secrets_are_redacted(self, tmp_path: Path):
        project = tmp_path / "p"
        project.mkdir()
        (project / "meltano.yml").write_text(
            yaml.safe_dump(
                {
                    "plugins": {
                        "extractors": [
                            {
                                "name": "tap-stripe",
                                "config": {"api_token": "literal-token-here", "host": "x"},
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        contract, _ = MeltanoImporter().import_to_contract(str(project))
        connection = contract["builds"][0]["properties"]["source"]["connection"]
        # api_token must be redacted to a placeholder; host stays literal.
        assert "literal-token-here" not in json.dumps(connection)
        assert "{{ env.API_TOKEN }}" in connection["api_token"]
        assert connection["host"] == "x"

    def test_missing_meltano_yml_can_import_returns_false(self, tmp_path: Path):
        importer = MeltanoImporter()
        assert not importer.can_import(str(tmp_path))

    def test_no_extractors_records_unsupported(self, tmp_path: Path):
        project = tmp_path / "p"
        project.mkdir()
        (project / "meltano.yml").write_text(yaml.safe_dump({"plugins": {}}), encoding="utf-8")
        contract, report = MeltanoImporter().import_to_contract(str(project))
        assert contract == {}
        assert any("no extractors" in u for u in report.unsupported)


# ── Airbyte importer ──────────────────────────────────────────────────


class TestAirbyteImporter:
    def test_imports_from_mock_workspace(self, airbyte_mock, schema_manager: FluidSchemaManager):
        # Pre-stage a source in the mock so list_sources returns it.
        airbyte_mock.sources["src-1"] = {
            "sourceId": "src-1",
            "sourceName": "Postgres",
            "workspaceId": "ws-1",
            "connectionConfiguration": {
                "host": "db",
                "port": 5432,
                "database": "mydb",
                "username": "u",
                "password": "literal-pw",
            },
        }
        importer = AirbyteImporter(server_url="https://airbyte.test")
        contract, report = importer.import_to_contract("ws-1")
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors
        # Password redacted.
        assert "literal-pw" not in json.dumps(contract)
        assert "source" in report.mapped_one_to_one
        assert (
            contract["builds"][0]["properties"]["airbyte"]["deployment"]["mode"] == "bring-your-own"
        )

    def test_empty_workspace_is_unsupported(self, airbyte_mock):
        # No sources pre-staged.
        contract, report = AirbyteImporter(server_url="https://airbyte.test").import_to_contract(
            "ws-empty"
        )
        assert contract == {}
        assert any("no sources" in u for u in report.unsupported)


# ── dlt importer ──────────────────────────────────────────────────────


class TestDltImporter:
    def test_imports_pipeline_state(self, tmp_path: Path, schema_manager: FluidSchemaManager):
        pipeline_dir = tmp_path / "my_pipeline"
        pipeline_dir.mkdir()
        state = {
            "destination_type": "duckdb",
            "dataset_name": "bronze_dataset",
            "source_name": "stripe",
            "schemas": {"customers": {}, "subscriptions": {}},
        }
        (pipeline_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        importer = DltImporter()
        assert importer.can_import(str(pipeline_dir))
        contract, report = importer.import_to_contract(str(pipeline_dir))
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors
        assert "pipeline.state" in report.mapped_one_to_one
        # Streams come from the schema list.
        streams = contract["builds"][0]["properties"]["source"]["streams"]
        assert sorted(streams) == ["customers", "subscriptions"]

    def test_pipeline_without_state_uses_defaults(self, tmp_path: Path):
        pipeline_dir = tmp_path / "p2"
        pipeline_dir.mkdir()
        importer = DltImporter()
        contract, report = importer.import_to_contract(str(pipeline_dir))
        assert "pipeline.state" not in report.mapped_one_to_one
        assert any("no state.json" in d for d in report.required_defaults)
        # Defaults: destination=duckdb, dataset=bronze.
        assert contract["builds"][0]["properties"]["dlt"]["destination"] == "duckdb"

    def test_named_pipeline_resolves_under_dlt_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path))
        pipeline_dir = tmp_path / "pipelines" / "my_named"
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "state.json").write_text("{}", encoding="utf-8")
        importer = DltImporter()
        assert importer.can_import("my_named")


# ── Singer importer ──────────────────────────────────────────────────


class TestSingerImporter:
    def test_imports_tap_config(self, tmp_path: Path, schema_manager: FluidSchemaManager):
        cfg = tmp_path / "tap-postgres.json"
        cfg.write_text(
            json.dumps(
                {
                    "host": "db",
                    "port": 5432,
                    "database": "mydb",
                    "user": "u",
                    "password": "secret",
                }
            ),
            encoding="utf-8",
        )
        importer = SingerImporter()
        assert importer.can_import(str(cfg))
        contract, report = importer.import_to_contract(str(cfg))
        result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors
        assert "tap.config" in report.mapped_one_to_one
        # Engine should be Meltano (Singer-protocol runner).
        assert contract["builds"][0]["engine"] == "meltano"
        # Tap kind derived from filename.
        assert contract["builds"][0]["properties"]["source"]["kind"] == "postgres"
        # Secret redacted.
        assert "secret" not in json.dumps(
            contract["builds"][0]["properties"]["source"]["connection"]
        )

    def test_with_target_config(self, tmp_path: Path):
        tap = tmp_path / "tap-stripe.json"
        target = tmp_path / "target-snowflake.json"
        tap.write_text(json.dumps({"api_token": "x"}), encoding="utf-8")
        target.write_text(json.dumps({"account": "acme"}), encoding="utf-8")
        importer = SingerImporter()
        contract, report = importer.import_to_contract(f"{tap}:{target}")
        assert "target.config" in report.mapped_one_to_one

    def test_target_missing_records_required_default(self, tmp_path: Path):
        tap = tmp_path / "tap-x.json"
        tap.write_text(json.dumps({"k": "v"}), encoding="utf-8")
        contract, report = SingerImporter().import_to_contract(
            f"{tap}:{tmp_path}/missing-target.json"
        )
        assert any("target config" in d for d in report.required_defaults)

    def test_missing_tap_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            SingerImporter().import_to_contract(str(tmp_path / "nope.json"))
