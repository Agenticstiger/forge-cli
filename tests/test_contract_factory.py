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

"""Tests for fluid_build.cli.forge_contract_factory."""

from __future__ import annotations

import logging

import yaml


class TestBuildMinimalContract:
    def test_default_contract_has_required_keys(self):
        from fluid_build.cli.forge_contract_factory import build_minimal_contract

        contract = build_minimal_contract()
        assert "fluidVersion" in contract
        assert "kind" in contract
        assert contract["kind"] == "DataProduct"
        assert "id" in contract
        assert "metadata" in contract
        assert "builds" in contract
        assert "exposes" in contract

    def test_custom_values(self):
        from fluid_build.cli.forge_contract_factory import build_minimal_contract

        contract = build_minimal_contract(
            product_id="test-product",
            name="Test Product",
            domain="finance",
            owner="finance-team",
            description="A test product",
            engine="dbt",
            tags=["test", "finance"],
        )
        assert contract["id"] == "test-product"
        assert contract["name"] == "Test Product"
        assert contract["domain"] == "finance"
        assert contract["metadata"]["owner"] == {"team": "finance-team"}
        assert contract["tags"] == ["test", "finance"]
        assert contract["builds"][0]["engine"] == "dbt"

    def test_default_name_derived_from_id(self):
        from fluid_build.cli.forge_contract_factory import build_minimal_contract

        contract = build_minimal_contract(product_id="my-cool-product")
        assert contract["name"] == "My Cool Product"


class TestWriteContract:
    def test_writes_valid_yaml(self, tmp_path):
        from fluid_build.cli.forge_contract_factory import build_minimal_contract, write_contract

        contract = build_minimal_contract()
        path = tmp_path / "contract.fluid.yaml"
        write_contract(contract, path)

        assert path.exists()
        content = path.read_text()
        assert "# FLUID Data Product Contract" in content

        parsed = yaml.safe_load(content)
        assert parsed["id"] == "my-data-product"

    def test_special_chars_escaped(self, tmp_path):
        from fluid_build.cli.forge_contract_factory import build_minimal_contract, write_contract

        contract = build_minimal_contract(
            description='Contains: colons, "quotes", and\nnewlines',
        )
        path = tmp_path / "contract.fluid.yaml"
        write_contract(contract, path)

        parsed = yaml.safe_load(path.read_text())
        assert "colons" in parsed["description"]
        assert "quotes" in parsed["description"]


class TestValidateContractFile:
    def test_valid_contract(self, tmp_path):
        from fluid_build.cli.forge_contract_factory import (
            build_minimal_contract,
            validate_contract_file,
            write_contract,
        )

        path = tmp_path / "contract.fluid.yaml"
        write_contract(build_minimal_contract(), path)
        assert validate_contract_file(path) is None

    def test_missing_keys(self, tmp_path):
        path = tmp_path / "contract.fluid.yaml"
        path.write_text("foo: bar\n")

        from fluid_build.cli.forge_contract_factory import validate_contract_file

        error = validate_contract_file(path)
        assert error is not None
        assert "missing required keys" in error

    def test_invalid_yaml(self, tmp_path):
        path = tmp_path / "contract.fluid.yaml"
        path.write_text("{{invalid yaml")

        from fluid_build.cli.forge_contract_factory import validate_contract_file

        error = validate_contract_file(path)
        assert error is not None
        assert "Invalid YAML" in error

    def test_not_a_mapping(self, tmp_path):
        path = tmp_path / "contract.fluid.yaml"
        path.write_text("- just\n- a\n- list\n")

        from fluid_build.cli.forge_contract_factory import validate_contract_file

        error = validate_contract_file(path)
        assert error is not None
        assert "not a YAML mapping" in error

    def test_nonexistent_file(self, tmp_path):
        from fluid_build.cli.forge_contract_factory import validate_contract_file

        error = validate_contract_file(tmp_path / "nope.yaml")
        assert error is not None
        assert "Cannot read" in error


class TestCreateAndValidateContract:
    def test_creates_and_validates(self, tmp_path):
        from fluid_build.cli.forge_contract_factory import (
            build_minimal_contract,
            create_and_validate_contract,
        )

        logger = logging.getLogger("test")
        contract = build_minimal_contract(product_id="test-proj")
        result = create_and_validate_contract(contract, tmp_path / "out", logger)
        assert result is not None
        assert result.name == "contract.fluid.yaml"
        assert result.exists()

    def test_creates_target_dir(self, tmp_path):
        from fluid_build.cli.forge_contract_factory import (
            build_minimal_contract,
            create_and_validate_contract,
        )

        logger = logging.getLogger("test")
        target = tmp_path / "nested" / "deep" / "project"
        contract = build_minimal_contract()
        result = create_and_validate_contract(contract, target, logger)
        assert result is not None
        assert target.exists()

    def test_returns_none_on_invalid_contract(self, tmp_path):
        from fluid_build.cli.forge_contract_factory import create_and_validate_contract

        logger = logging.getLogger("test")
        # Missing required keys
        bad_contract = {"foo": "bar"}
        result = create_and_validate_contract(bad_contract, tmp_path / "bad", logger)
        assert result is None
