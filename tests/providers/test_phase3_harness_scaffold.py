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

# tests/providers/test_phase3_harness_scaffold.py
"""Tests for Phase 3: Test Harness + Scaffolder."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from fluid_build.schema_manager import FluidSchemaManager

try:
    import fluid_provider_sdk  # noqa: F401

    HAS_SDK = True
except ImportError:
    HAS_SDK = False

pytestmark = pytest.mark.skipif(not HAS_SDK, reason="fluid-provider-sdk not installed")

# ---------------------------------------------------------------------------
# Test Harness (SDK testing subpackage)
# ---------------------------------------------------------------------------


class TestHarnessImports:
    """Verify the testing subpackage is importable and exposes the API."""

    def test_import_harness(self):
        from fluid_provider_sdk.testing import ProviderTestHarness

        assert ProviderTestHarness is not None

    def test_import_fixtures(self):
        from fluid_provider_sdk.testing import (
            SAMPLE_CONTRACTS,
        )

        assert isinstance(SAMPLE_CONTRACTS, list)
        assert len(SAMPLE_CONTRACTS) == 4

    def test_fixtures_are_valid_contracts(self):
        from fluid_provider_sdk.testing import SAMPLE_CONTRACTS

        for contract in SAMPLE_CONTRACTS:
            assert "fluidVersion" in contract
            assert "kind" in contract
            assert "id" in contract
            assert "exposes" in contract


class TestHarnessFixtureContent:
    """Verify fixture contracts have required structure."""

    def test_local_contract_has_consumes(self):
        from fluid_provider_sdk.testing import LOCAL_CONTRACT

        assert "consumes" in LOCAL_CONTRACT
        assert len(LOCAL_CONTRACT["consumes"]) > 0

    def test_gcp_contract_has_labels(self):
        from fluid_provider_sdk.testing import GCP_CONTRACT

        assert "labels" in GCP_CONTRACT

    def test_aws_contract_has_retention_policy(self):
        from fluid_provider_sdk.testing import AWS_CONTRACT

        policies = AWS_CONTRACT.get("metadata", {}).get("policies", {})
        assert "retention_days" in policies

    def test_snowflake_contract_has_warehouse(self):
        from fluid_provider_sdk.testing import SNOWFLAKE_CONTRACT

        exposes = SNOWFLAKE_CONTRACT.get("exposes", [])
        assert len(exposes) > 0
        location = exposes[0].get("binding", {}).get("location", {})
        assert "warehouse" in location or "database" in location


class TestHarnessWithLocalProvider:
    """Run the conformance harness against the built-in LocalProvider."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from fluid_build.providers import clear_providers

        clear_providers()
        yield
        clear_providers()

    def _get_harness(self):
        """Create a harness-like test instance for LocalProvider."""
        from fluid_provider_sdk.testing import LOCAL_CONTRACT, ProviderTestHarness

        from fluid_build.providers.local import LocalProvider

        class _LocalHarness(ProviderTestHarness):
            provider_class = LocalProvider
            init_kwargs = {"project": "test-project"}
            sample_contracts = [LOCAL_CONTRACT]
            skip_apply = True  # Don't execute actual DuckDB

        return _LocalHarness()

    def test_identity_subclass(self):
        h = self._get_harness()
        h.test_subclasses_base_provider()

    def test_identity_valid_name(self):
        h = self._get_harness()
        h.test_name_is_valid()

    def test_identity_not_reserved(self):
        h = self._get_harness()
        h.test_name_not_reserved()

    def test_constructor(self):
        h = self._get_harness()
        h.test_constructor_accepts_kwargs()

    def test_logger_set(self):
        h = self._get_harness()
        h.test_logger_is_set()

    def test_capabilities(self):
        h = self._get_harness()
        h.test_capabilities_returns_mapping()

    def test_capabilities_bool(self):
        h = self._get_harness()
        h.test_capabilities_values_are_bool()

    def test_plan_returns_list(self):
        h = self._get_harness()
        h.test_plan_returns_list()

    def test_plan_actions_have_op(self):
        h = self._get_harness()
        h.test_plan_actions_have_op()

    def test_plan_actions_have_resource_id(self):
        h = self._get_harness()
        h.test_plan_actions_have_resource_id()

    def test_metadata_exists(self):
        h = self._get_harness()
        h.test_get_provider_info_exists()

    def test_metadata_matches(self):
        h = self._get_harness()
        h.test_get_provider_info_returns_metadata()

    def test_metadata_has_description(self):
        h = self._get_harness()
        h.test_get_provider_info_has_description()


class TestHarnessValidation:
    """Test that the harness properly catches bad providers."""

    def test_default_name_is_reserved(self):
        """BaseProvider defaults name='unknown', which is reserved."""
        from fluid_provider_sdk import BaseProvider
        from fluid_provider_sdk.testing import ProviderTestHarness

        class BadProvider(BaseProvider):
            pass  # inherits name="unknown"

        class _Harness(ProviderTestHarness):
            provider_class = BadProvider
            init_kwargs = {}

        h = _Harness()
        with pytest.raises(AssertionError, match="reserved"):
            h.test_name_not_reserved()

    def test_reserved_name_fails(self):
        from fluid_provider_sdk import BaseProvider
        from fluid_provider_sdk.testing import ProviderTestHarness

        class TestProvider(BaseProvider):
            name = "test"

        class _Harness(ProviderTestHarness):
            provider_class = TestProvider

        h = _Harness()
        with pytest.raises(AssertionError, match="reserved"):
            h.test_name_not_reserved()

    def test_no_contracts_skips_plan(self):
        from fluid_provider_sdk import BaseProvider
        from fluid_provider_sdk.testing import ProviderTestHarness

        class MinimalProvider(BaseProvider):
            name = "minprov"

        class _Harness(ProviderTestHarness):
            provider_class = MinimalProvider
            sample_contracts = []  # no contracts

        h = _Harness()
        # Should not raise — just skip
        h.test_plan_returns_list()
        h.test_plan_actions_have_op()


# ---------------------------------------------------------------------------
# Scaffolder (provider_init.py)
# ---------------------------------------------------------------------------


class TestScaffolderRegistration:
    """Verify provider-init command is registered in bootstrap."""

    def test_provider_init_in_bootstrap(self):
        import importlib

        mod = importlib.import_module("fluid_build.cli.provider_init")
        assert hasattr(mod, "register")
        assert hasattr(mod, "run")

    def test_register_creates_subcommand(self):
        import argparse

        from fluid_build.cli.provider_init import register

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers()
        register(sp)

        # Verify the subcommand was added
        args = parser.parse_args(["provider-init", "databricks"])
        assert args.name == "databricks"
        assert args.author == "FLUID Community"  # default

    def test_register_accepts_all_options(self):
        import argparse

        from fluid_build.cli.provider_init import register

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers()
        register(sp)

        args = parser.parse_args(
            [
                "provider-init",
                "mydb",
                "--author",
                "My Corp",
                "--description",
                "My DB provider",
                "--output-dir",
                "/tmp/test",
            ]
        )
        assert args.name == "mydb"
        assert args.author == "My Corp"
        assert args.desc == "My DB provider"
        assert args.output_dir == "/tmp/test"


class TestScaffolderGeneration:
    """Verify the scaffolder generates valid provider packages."""

    @pytest.fixture()
    def tmp_dir(self):
        d = tempfile.mkdtemp(prefix="fluid_scaffold_test_")
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    def _run_scaffold(
        self,
        tmp_dir: Path,
        name: str = "testprov",
        author: str = "Test",
        desc: str = "Test provider",
    ):
        import argparse
        import logging

        from fluid_build.cli.provider_init import run

        args = argparse.Namespace(
            name=name,
            author=author,
            desc=desc,
            output_dir=str(tmp_dir),
        )
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0
        return tmp_dir / f"fluid-provider-{name.replace('_', '-')}"

    def test_creates_package_directory(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        assert pkg.is_dir()

    def test_creates_all_files(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        expected = [
            "pyproject.toml",
            "README.md",
            "src/fluid_provider_testprov/__init__.py",
            "src/fluid_provider_testprov/provider.py",
            "tests/__init__.py",
            "tests/test_conformance.py",
            "tests/fixtures/basic_contract.yaml",
        ]
        for relpath in expected:
            assert (pkg / relpath).exists(), f"Missing: {relpath}"

    def test_pyproject_has_entry_point(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        content = (pkg / "pyproject.toml").read_text()
        assert '[project.entry-points."fluid_build.providers"]' in content
        assert 'testprov = "fluid_provider_testprov:TestprovProvider"' in content

    def test_pyproject_has_sdk_dependency(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        content = (pkg / "pyproject.toml").read_text()
        assert "fluid-provider-sdk" in content

    def test_provider_subclasses_base(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        content = (pkg / "src/fluid_provider_testprov/provider.py").read_text()
        assert "class TestprovProvider(BaseProvider):" in content
        assert 'name = "testprov"' in content

    def test_provider_has_plan_apply(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        content = (pkg / "src/fluid_provider_testprov/provider.py").read_text()
        assert "def plan(" in content
        assert "def apply(" in content

    def test_provider_has_capabilities(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        content = (pkg / "src/fluid_provider_testprov/provider.py").read_text()
        assert "def capabilities(" in content

    def test_provider_has_get_provider_info(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        content = (pkg / "src/fluid_provider_testprov/provider.py").read_text()
        assert "def get_provider_info(" in content

    def test_tests_use_harness(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        content = (pkg / "tests/test_conformance.py").read_text()
        assert "ProviderTestHarness" in content
        assert "provider_class = TestprovProvider" in content

    def test_fixture_yaml_is_valid(self, tmp_dir):
        """Verify the generated YAML fixture is parseable."""
        import yaml

        pkg = self._run_scaffold(tmp_dir)
        with open(pkg / "tests/fixtures/basic_contract.yaml") as f:
            contract = yaml.safe_load(f)
        assert contract["fluidVersion"] == FluidSchemaManager.latest_bundled_version()
        assert "exposes" in contract

    def test_readme_has_install_instructions(self, tmp_dir):
        pkg = self._run_scaffold(tmp_dir)
        content = (pkg / "README.md").read_text()
        assert "pip install" in content
        assert "fluid-provider-testprov" in content

    def test_underscore_name(self, tmp_dir):
        """Provider names with underscores are handled properly."""
        pkg = self._run_scaffold(tmp_dir, name="my_custom_db")
        assert pkg.name == "fluid-provider-my-custom-db"
        content = (pkg / "pyproject.toml").read_text()
        assert "fluid_provider_my_custom_db" in content


class TestScaffolderValidation:
    """Verify the scaffolder rejects invalid inputs."""

    @pytest.fixture()
    def tmp_dir(self):
        d = tempfile.mkdtemp(prefix="fluid_scaffold_val_")
        yield Path(d)
        shutil.rmtree(d, ignore_errors=True)

    def test_rejects_invalid_name(self, tmp_dir):
        import argparse
        import logging

        from fluid_build.cli._common import CLIError
        from fluid_build.cli.provider_init import run

        args = argparse.Namespace(
            name="123bad",
            author="Test",
            desc="Test",
            output_dir=str(tmp_dir),
        )
        with pytest.raises(CLIError):
            run(args, logging.getLogger("test"))

    def test_rejects_reserved_name(self, tmp_dir):
        import argparse
        import logging

        from fluid_build.cli._common import CLIError
        from fluid_build.cli.provider_init import run

        args = argparse.Namespace(
            name="local",
            author="Test",
            desc="Test",
            output_dir=str(tmp_dir),
        )
        with pytest.raises(CLIError):
            run(args, logging.getLogger("test"))

    def test_rejects_existing_directory(self, tmp_dir):
        import argparse
        import logging

        from fluid_build.cli._common import CLIError
        from fluid_build.cli.provider_init import run

        # Create the directory first
        (tmp_dir / "fluid-provider-duptest").mkdir()

        args = argparse.Namespace(
            name="duptest",
            author="Test",
            desc="Test",
            output_dir=str(tmp_dir),
        )
        with pytest.raises(CLIError):
            run(args, logging.getLogger("test"))
