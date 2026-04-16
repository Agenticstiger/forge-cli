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

"""Tests for the ``fluid test`` CLI command and ContractValidator DQ wiring."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("FLUID_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FLUID_BUILD_PROFILE", "experimental")


CONTRACT_WITH_DQ = {
    "fluidVersion": "0.7.1",
    "kind": "DataProduct",
    "id": "test.dq_contract",
    "name": "DQ Test Product",
    "description": "Contract with DQ rules",
    "domain": "testing",
    "metadata": {
        "layer": "Bronze",
        "owner": {"team": "qa", "email": "qa@example.com"},
    },
    "consumes": [],
    "builds": [
        {
            "id": "noop_build",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 1"},
        }
    ],
    "exposes": [
        {
            "id": "test_table",
            "type": "table",
            "binding": {
                "platform": "local",
                "location": {
                    "format": "csv",
                    "path": "data/test.csv",
                },
            },
            "schema": [
                {"name": "id", "type": "INTEGER"},
                {"name": "name", "type": "STRING"},
            ],
            "dq": {
                "rules": [
                    {
                        "id": "no_null_ids",
                        "type": "completeness",
                        "selector": "id",
                        "threshold": 1.0,
                        "operator": ">=",
                        "severity": "error",
                    },
                    {
                        "id": "unique_ids",
                        "type": "uniqueness",
                        "selector": "id",
                        "threshold": 1.0,
                        "operator": ">=",
                        "severity": "warning",
                    },
                ]
            },
        }
    ],
}


def _write_contract(directory: Path, contract: dict, name: str = "contract.fluid.yaml") -> Path:
    p = directory / name
    p.write_text(yaml.dump(contract, sort_keys=False))
    return p


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------


class TestTestCommandRegistration:
    def test_test_command_registered(self, _clean_env):
        import argparse

        from fluid_build.cli.bootstrap import register_core_commands

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        register_core_commands(sp)
        # Parse a minimal test invocation
        parsed = parser.parse_args(["test", "contract.fluid.yaml"])
        assert parsed.command == "test"

    def test_test_accepts_server_flag(self, _clean_env):
        import argparse

        from fluid_build.cli.bootstrap import register_core_commands

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        register_core_commands(sp)
        parsed = parser.parse_args(
            ["test", "contract.fluid.yaml", "--server", "my-account.snowflakecomputing.com"]
        )
        assert parsed.server == "my-account.snowflakecomputing.com"

    def test_test_accepts_output_flag(self, _clean_env):
        import argparse

        from fluid_build.cli.bootstrap import register_core_commands

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers(dest="command")
        register_core_commands(sp)
        parsed = parser.parse_args(["test", "contract.fluid.yaml", "--output", "json"])
        assert parsed.output == "json"


# ---------------------------------------------------------------------------
# ContractValidator wiring
# ---------------------------------------------------------------------------


class TestContractValidatorDQ:
    def test_dq_rules_extracted_from_expose(self, tmp_path, _clean_env):
        """The validator should pick up dq.rules from exposes and call run_quality_checks."""
        contract_file = _write_contract(tmp_path, CONTRACT_WITH_DQ)

        from fluid_build.cli.contract_validation import ContractValidator

        validator = ContractValidator(
            contract_path=contract_file,
            check_data=True,
        )

        # Mock the validation provider
        mock_provider = MagicMock()
        mock_provider.validate_connection.return_value = True
        mock_provider.get_resource_schema.return_value = None
        mock_provider.validate_resource.return_value = MagicMock(issues=[])
        mock_provider.run_quality_checks.return_value = []
        mock_provider.provider_name = "local"

        # Patch _detect_and_validate_provider to use our mock
        def _set_mock_provider():
            validator.provider_name = "local"
            validator.validation_provider = mock_provider
            validator.report.provider_name = "local"

        with patch.object(
            validator, "_detect_and_validate_provider", side_effect=_set_mock_provider
        ):
            validator.validate()

        # run_quality_checks should have been called with the DQ rules
        assert mock_provider.run_quality_checks.called
        call_args = mock_provider.run_quality_checks.call_args
        rules = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("rules", [])
        assert len(rules) == 2

    def test_no_dq_rules_means_no_call(self, tmp_path, _clean_env):
        """When no dq.rules are defined, run_quality_checks should not be called."""
        contract = dict(CONTRACT_WITH_DQ)
        # Remove DQ rules from expose
        contract = json.loads(json.dumps(CONTRACT_WITH_DQ))
        del contract["exposes"][0]["dq"]
        contract_file = _write_contract(tmp_path, contract)

        from fluid_build.cli.contract_validation import ContractValidator

        validator = ContractValidator(
            contract_path=contract_file,
            check_data=True,
        )

        mock_provider = MagicMock()
        mock_provider.validate_connection.return_value = True
        mock_provider.get_resource_schema.return_value = None
        mock_provider.validate_resource.return_value = MagicMock(issues=[])
        mock_provider.run_quality_checks.return_value = []
        mock_provider.provider_name = "local"

        def _set_mock_provider():
            validator.provider_name = "local"
            validator.validation_provider = mock_provider
            validator.report.provider_name = "local"

        with patch.object(
            validator, "_detect_and_validate_provider", side_effect=_set_mock_provider
        ):
            validator.validate()

        assert not mock_provider.run_quality_checks.called


# ---------------------------------------------------------------------------
# Provider label detection
# ---------------------------------------------------------------------------


class TestProviderLabel:
    def test_gcp_label(self):
        from fluid_build.cli.test import _detect_provider_label

        report = MagicMock()
        report.provider_name = "gcp"
        assert _detect_provider_label(report) == "gcp (BigQuery)"

    def test_snowflake_label(self):
        from fluid_build.cli.test import _detect_provider_label

        report = MagicMock()
        report.provider_name = "snowflake"
        assert _detect_provider_label(report) == "snowflake"

    def test_local_label(self):
        from fluid_build.cli.test import _detect_provider_label

        report = MagicMock()
        report.provider_name = "local"
        assert _detect_provider_label(report) == "local (DuckDB)"

    def test_aws_label(self):
        from fluid_build.cli.test import _detect_provider_label

        report = MagicMock()
        report.provider_name = "aws"
        assert _detect_provider_label(report) == "aws (Glue/Athena)"

    def test_unknown_label(self):
        from fluid_build.cli.test import _detect_provider_label

        report = MagicMock()
        report.provider_name = None
        assert _detect_provider_label(report) == "auto-detected"


# ---------------------------------------------------------------------------
# ValidationReport.provider_name field
# ---------------------------------------------------------------------------


class TestValidationReportProviderField:
    def test_provider_name_default_none(self):
        from datetime import datetime

        from fluid_build.cli.contract_validation import ValidationReport

        report = ValidationReport(
            contract_path="/test.yaml",
            contract_id="test",
            contract_version="1.0.0",
            validation_time=datetime.now(),
            duration=0.0,
        )
        assert report.provider_name is None

    def test_provider_name_set(self):
        from datetime import datetime

        from fluid_build.cli.contract_validation import ValidationReport

        report = ValidationReport(
            contract_path="/test.yaml",
            contract_id="test",
            contract_version="1.0.0",
            validation_time=datetime.now(),
            duration=0.0,
        )
        report.provider_name = "snowflake"
        assert report.provider_name == "snowflake"


# ---------------------------------------------------------------------------
# Server flag wiring
# ---------------------------------------------------------------------------


class TestServerFlag:
    def test_server_stored_on_validator(self, tmp_path, _clean_env):
        contract_file = _write_contract(tmp_path, CONTRACT_WITH_DQ)
        from fluid_build.cli.contract_validation import ContractValidator

        validator = ContractValidator(
            contract_path=contract_file,
            server="my-custom-account",
        )
        assert validator.server == "my-custom-account"

    def test_server_overrides_snowflake_account(self, tmp_path, _clean_env, monkeypatch):
        # Ensure env vars don't interfere
        monkeypatch.delenv("SNOWFLAKE_ACCOUNT", raising=False)
        monkeypatch.delenv("SNOWFLAKE_USER", raising=False)
        monkeypatch.delenv("SNOWFLAKE_PASSWORD", raising=False)

        contract_file = _write_contract(tmp_path, CONTRACT_WITH_DQ)
        from fluid_build.cli.contract_validation import ContractValidator

        validator = ContractValidator(
            contract_path=contract_file,
            server="override-account",
        )
        # Load contract so _build_snowflake_config can read it
        validator.contract = CONTRACT_WITH_DQ
        config = validator._build_snowflake_config()
        assert config["account"] == "override-account"
