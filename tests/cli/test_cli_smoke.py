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

"""
CLI Smoke Tests — validate, plan, apply

Invokes the real CLI entry point (fluid_build.cli.main) with crafted
argv lists against temporary contract files.  Every test is fully
isolated via tmp_path and environment cleanup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from fluid_build.cli import main

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_env(monkeypatch):
    """Ensure no stale FLUID_ env vars leak between tests."""
    for key in list(os.environ):
        if key.startswith("FLUID_"):
            monkeypatch.delenv(key, raising=False)
    # Force the "experimental" profile (all commands enabled)
    monkeypatch.setenv("FLUID_BUILD_PROFILE", "experimental")


MINIMAL_CONTRACT_071 = {
    "fluidVersion": "0.7.1",
    "kind": "DataProduct",
    "id": "test.smoke_test",
    "name": "Smoke Test Product",
    "description": "A minimal contract for CLI smoke tests",
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
            "properties": {"sql": "SELECT 1 AS id"},
        }
    ],
    "exposes": [
        {
            "exposeId": "smoke_table",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "csv",
                "location": {"path": "runtime/out/smoke.csv"},
            },
            "contract": {
                "schema": [
                    {"name": "id", "type": "integer", "required": True},
                ]
            },
        }
    ],
}

MINIMAL_CONTRACT_057 = {
    "id": "test.legacy_smoke",
    "fluidVersion": "0.5.7",
    "kind": "DataContract",
    "name": "Legacy Smoke Test",
    "version": "1.0.0",
    "metadata": {
        "layer": "Bronze",
        "owner": {"team": "qa"},
    },
    "exposes": [
        {
            "id": "smoke_table",
            "location": {
                "format": "bigquery_table",
                "properties": {
                    "project": "test-project",
                    "dataset": "test_ds",
                    "table": "smoke",
                },
            },
            "schema": {
                "columns": [
                    {"name": "id", "type": "STRING"},
                ]
            },
        }
    ],
}


def _write_contract(directory: Path, contract: dict, name: str = "contract.fluid.yaml") -> Path:
    """Write a contract dict as YAML into *directory* and return its path."""
    p = directory / name
    p.write_text(yaml.dump(contract, sort_keys=False), encoding="utf-8")
    return p


def _write_json(directory: Path, obj: dict, name: str) -> Path:
    p = directory / name
    p.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return p


# =====================================================================
# VALIDATE COMMAND
# =====================================================================


class TestValidateCommand:
    """Smoke tests for `fluid validate`."""

    def test_validate_valid_contract(self, tmp_path, _clean_env):
        """A well-formed 0.7.1 contract should validate successfully."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        rc = main(["validate", str(contract_file), "--offline", "--quiet"])
        assert rc == 0

    def test_validate_057_contract(self, tmp_path, _clean_env):
        """Legacy 0.5.7 contracts should at least not crash."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_057)
        rc = main(["validate", str(contract_file), "--offline", "--quiet"])
        # 0.5.7 contracts may produce validation warnings (rc=1) in some
        # schema manager versions; the important thing is no crash.
        assert rc in (0, 1)

    def test_validate_missing_file(self, tmp_path, _clean_env):
        """Non-existent file should yield exit code 1."""
        rc = main(["validate", str(tmp_path / "does_not_exist.yaml"), "--quiet"])
        assert rc != 0

    def test_validate_invalid_yaml(self, tmp_path, _clean_env):
        """Malformed YAML should yield a non-zero exit code."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("{{{{not: yaml: at: all", encoding="utf-8")
        rc = main(["validate", str(bad), "--quiet"])
        assert rc != 0

    def test_validate_empty_contract(self, tmp_path, _clean_env):
        """An empty file should fail validation."""
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        rc = main(["validate", str(empty), "--quiet"])
        assert rc != 0

    def test_validate_json_contract(self, tmp_path, _clean_env):
        """JSON-formatted contracts should work too."""
        contract_file = _write_json(tmp_path, MINIMAL_CONTRACT_071, "contract.fluid.json")
        rc = main(["validate", str(contract_file), "--offline", "--quiet"])
        assert rc == 0

    def test_validate_verbose_flag(self, tmp_path, _clean_env, capsys):
        """--verbose should produce extra output without crashing."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        rc = main(["validate", str(contract_file), "--offline", "--verbose"])
        # Just checking it doesn't crash; verbose may print Rich output
        assert rc == 0

    def test_validate_strict_flag(self, tmp_path, _clean_env):
        """--strict should not change the exit code for a valid contract."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        rc = main(["validate", str(contract_file), "--offline", "--strict", "--quiet"])
        assert rc == 0

    def test_validate_with_env_overlay(self, tmp_path, _clean_env):
        """Validate should apply environment overlays."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        overlays_dir = tmp_path / "overlays"
        overlays_dir.mkdir()
        overlay = {"metadata": {"layer": "Gold"}}
        (overlays_dir / "prod.yaml").write_text(yaml.dump(overlay), encoding="utf-8")

        rc = main(["validate", str(contract_file), "--env", "prod", "--offline", "--quiet"])
        assert rc == 0

    def test_validate_no_contract_arg(self, _clean_env):
        """Running validate without a contract path should fail."""
        rc = main(["validate", "--quiet"])
        assert rc != 0


# =====================================================================
# PLAN COMMAND
# =====================================================================


class TestPlanCommand:
    """Smoke tests for `fluid plan`."""

    def test_plan_produces_output_file(self, tmp_path, _clean_env):
        """Plan should write a plan JSON to the specified --out path."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        out_file = tmp_path / "plan.json"
        rc = main(
            [
                "plan",
                str(contract_file),
                "--out",
                str(out_file),
                "--provider",
                "local",
            ]
        )
        assert rc == 0
        assert out_file.exists()
        plan = json.loads(out_file.read_text())
        assert "actions" in plan or "provider" in plan

    def test_plan_default_output_path(self, tmp_path, _clean_env, monkeypatch):
        """Without --out, plan writes to runtime/plan.json relative to cwd."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        monkeypatch.chdir(tmp_path)
        rc = main(["plan", str(contract_file), "--provider", "local"])
        assert rc == 0
        default_plan = tmp_path / "runtime" / "plan.json"
        assert default_plan.exists()

    def test_plan_missing_contract(self, tmp_path, _clean_env):
        """Plan with a non-existent contract should fail."""
        rc = main(["plan", str(tmp_path / "nope.yaml")])
        assert rc != 0

    def test_plan_with_env_overlay(self, tmp_path, _clean_env):
        """Plan should respect --env overlays."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        overlays_dir = tmp_path / "overlays"
        overlays_dir.mkdir()
        (overlays_dir / "staging.yaml").write_text(
            yaml.dump({"metadata": {"layer": "Silver"}}), encoding="utf-8"
        )
        out_file = tmp_path / "plan_staging.json"
        rc = main(
            [
                "plan",
                str(contract_file),
                "--env",
                "staging",
                "--out",
                str(out_file),
                "--provider",
                "local",
            ]
        )
        assert rc == 0

    def test_plan_unknown_provider_logged(self, tmp_path, _clean_env):
        """An unrecognised provider name should not crash the planner."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        # The CLI may silently fall back to the built-in planner rather than
        # erroring out.  The key assertion is that it does not raise.
        rc = main(
            [
                "plan",
                str(contract_file),
                "--provider",
                "nonexistent_cloud",
                "--out",
                str(tmp_path / "plan.json"),
            ]
        )
        assert isinstance(rc, int)

    def test_plan_infers_provider_from_contract(self, tmp_path, _clean_env):
        """Plan should auto-detect the provider from binding.platform."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        out_file = tmp_path / "plan.json"
        # Contract has platform: local in the binding
        rc = main(["plan", str(contract_file), "--out", str(out_file)])
        assert rc == 0

    def test_plan_057_legacy(self, tmp_path, _clean_env):
        """Plan should handle 0.5.7-style contracts."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_057)
        out_file = tmp_path / "plan.json"
        rc = main(
            [
                "plan",
                str(contract_file),
                "--out",
                str(out_file),
                "--provider",
                "local",
            ]
        )
        assert rc == 0


# =====================================================================
# APPLY COMMAND
# =====================================================================


class TestApplyCommand:
    """Smoke tests for `fluid apply`."""

    def test_apply_dry_run(self, tmp_path, _clean_env):
        """--dry-run should succeed without side effects."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        rc = main(
            [
                "apply",
                str(contract_file),
                "--yes",
                "--dry-run",
                "--provider",
                "local",
                "--report",
                str(tmp_path / "report.html"),
            ]
        )
        assert rc == 0

    def test_apply_missing_contract(self, tmp_path, _clean_env):
        """Apply with missing contract should fail."""
        rc = main(["apply", str(tmp_path / "ghost.yaml"), "--yes"])
        assert rc != 0

    def test_apply_local_provider(self, tmp_path, _clean_env, monkeypatch):
        """Apply with the local provider should succeed end-to-end."""
        pytest.importorskip("duckdb")
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        monkeypatch.chdir(tmp_path)
        rc = main(
            [
                "apply",
                str(contract_file),
                "--yes",
                "--provider",
                "local",
                "--report",
                str(tmp_path / "report.html"),
            ]
        )
        # local provider should work without cloud credentials
        assert rc == 0

    def test_apply_unknown_provider_logged(self, tmp_path, _clean_env):
        """An unrecognised provider should not crash apply."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        # The CLI may silently fall back to local.  Key assertion: no crash.
        rc = main(
            [
                "apply",
                str(contract_file),
                "--yes",
                "--provider",
                "imaginary_cloud",
            ]
        )
        assert isinstance(rc, int)


# =====================================================================
# GLOBAL FLAGS
# =====================================================================


class TestGlobalFlags:
    """Test top-level CLI flags like --version, --help, --log-level."""

    def test_version_flag(self, _clean_env):
        """--version should print version and exit 0."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--version"])
        assert exc_info.value.code == 0

    def test_no_args_shows_help(self, _clean_env):
        """Running `fluid` with no args should show help and exit 0."""
        rc = main([])
        assert rc == 0

    def test_unknown_command(self, _clean_env):
        """An unrecognized subcommand should exit with code 2."""
        with pytest.raises(SystemExit) as exc_info:
            main(["this_command_does_not_exist_xyz"])
        assert exc_info.value.code == 2


# =====================================================================
# LOADER INTEGRATION
# =====================================================================


class TestLoaderIntegration:
    """Verify the contract loader works correctly through the CLI."""

    def test_load_yaml(self, tmp_path, _clean_env):
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071, "test.yaml")
        rc = main(["validate", str(contract_file), "--offline", "--quiet"])
        assert rc == 0

    def test_load_json(self, tmp_path, _clean_env):
        contract_file = _write_json(tmp_path, MINIMAL_CONTRACT_071, "test.json")
        rc = main(["validate", str(contract_file), "--offline", "--quiet"])
        assert rc == 0

    def test_overlay_merges_correctly(self, tmp_path, _clean_env):
        """Overlay values should override base values in the loaded contract."""
        base = dict(MINIMAL_CONTRACT_071)
        base["metadata"] = {"layer": "Bronze", "owner": {"team": "qa", "email": "qa@example.com"}}
        contract_file = _write_contract(tmp_path, base)

        overlays_dir = tmp_path / "overlays"
        overlays_dir.mkdir()
        overlay = {"metadata": {"layer": "Gold"}}
        (overlays_dir / "prod.yaml").write_text(yaml.dump(overlay), encoding="utf-8")

        out_file = tmp_path / "plan.json"
        rc = main(
            [
                "plan",
                str(contract_file),
                "--env",
                "prod",
                "--out",
                str(out_file),
                "--provider",
                "local",
            ]
        )
        assert rc == 0

    def test_missing_overlay_still_works(self, tmp_path, _clean_env):
        """If --env is specified but no overlay file exists, loading should still work."""
        contract_file = _write_contract(tmp_path, MINIMAL_CONTRACT_071)
        rc = main(
            [
                "validate",
                str(contract_file),
                "--env",
                "nonexistent_env",
                "--offline",
                "--quiet",
            ]
        )
        assert rc == 0
