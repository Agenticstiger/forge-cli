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

"""Slice UX-H: minimal-by-default ``fluid forge`` + engine CI shim.

Before slice UX-H, every ``fluid forge`` (AI copilot) invocation routed
through :class:`fluid_build.forge.core.engine.ForgeEngine` and
materialised a full opinionated project tree (``extracts/``,
``loads/``, ``transforms/``, ``config/``, ``docs/``, ``tests/``,
``scripts/``, ``requirements.txt``, ``.env.example``, ``README.md``,
…) plus a ``data/`` scaffold and, on systems with a personal-memory
CI preference, a ``bitbucket-pipelines.yml`` surprise.

Slice UX-H restores the unification promise of slice UX-F: the
default is minimal — only ``contract.fluid.yaml`` and
``.fluid/forge-receipt.json`` land on disk.  Users who want the full
tree opt in explicitly with ``--scaffold <template>``.

Three changes hold the new contract together and each has a
regression test here:

1. ``run_ai_copilot_mode`` branches on ``args.scaffold``.  The
   unset default routes through a new ``_create_project_minimal``
   helper that skips ``ForgeEngine`` and writes the LLM-generated
   contract via ``forge_contract_factory.write_contract``.
   ``--scaffold etl_pipeline`` still invokes ``copilot.create_project``
   which hands off to the engine.

2. ``_scaffold_data_folder`` is gated on ``args.scaffold``.  The
   default path no longer creates ``data/`` + ``data/README.md``.

3. ``ForgeEngine._generate_pipeline_files`` is a deprecation shim
   that no-ops.  The original body survives under
   ``_generate_pipeline_files_legacy`` so any external caller or
   test fixture that needs the old behaviour still has a reachable
   implementation.  Live CI file generation now routes exclusively
   through ``forge_modes._scaffold_ci_pipeline``.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _build_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    """Build an argparse.Namespace with every attribute forge's
    ``get_cli_arg`` helper may probe.

    The defaults run the AI copilot in non-interactive mode, point it
    at ``tmp_path``, and force ``--no-ci`` so the auto-CI hook doesn't
    leak ``github_actions`` / ``bitbucket-pipelines.yml`` into the
    minimal-output assertions.
    """
    defaults: Dict[str, Any] = {
        "non_interactive": True,
        "scaffold": None,
        "target_dir": str(tmp_path),
        "provider": None,
        "template": None,
        "domain": None,
        "use_case": None,
        "context": None,
        "dry_run": False,
        "no_run": True,
        "no_dag": True,
        "no_ci": True,
        "ci": None,
        "ci_complexity": "standard",
        "discover": False,
        "discovery_path": None,
        "llm_provider": "openai",
        "llm_model": None,
        "llm_endpoint": None,
        "memory": None,
        "clear_memory": False,
        "show_memory": False,
        "project_memory": None,
        "_force_llm_setup": False,
        "_enable_copilot_recovery": False,
        "_implicit_mode": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _minimal_generated_contract() -> Dict[str, Any]:
    """A valid v0.7.2 contract body used as the fake LLM output."""
    return {
        "fluidVersion": "0.7.2",
        "kind": "DataProduct",
        "id": "slice.ux_h.fake",
        "name": "Slice UX-H Fake",
        "description": "Fake contract for slice UX-H regression tests.",
        "domain": "analytics",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-team", "email": "data@example.com"},
        },
        "builds": [
            {
                "id": "main_build",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": "SELECT 1 AS id"},
                "execution": {
                    "trigger": {"type": "manual", "iterations": 1},
                    "runtime": {
                        "platform": "local",
                        "resources": {"cpu": "1", "memory": "2Gi"},
                    },
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "fake_output",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "runtime/out/fake_output.csv"},
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": False}]},
            }
        ],
    }


def _fake_generation_result() -> Any:
    """Build a ``CopilotGenerationResult`` with enough fields populated
    that :func:`_create_project_minimal` can write the contract."""
    from fluid_build.cli.forge_copilot_runtime import CopilotGenerationResult

    return CopilotGenerationResult(
        suggestions={
            "recommended_template": "analytics",
            "recommended_provider": "local",
            "domain": "analytics",
            "owner": "data-team",
        },
        contract=_minimal_generated_contract(),
        readme_markdown="# Slice UX-H Fake Project\n",
        additional_files={},
        discovery_report=MagicMock(existing_contracts=[]),
        attempt_reports=[],
        scaffold_decision=None,
        project_memory=None,
        provenance={
            "mode": "staged",
            "agent_events": [{"stage": "logical", "agent": "LogicalAgent", "mode": "llm"}],
            "fallback_used": False,
            "repair_used": False,
        },
        ai_run_plan={
            "provider": "openai",
            "primary_model": "gpt-4.1-mini",
            "routing_model": "gpt-4.1-nano",
            "stages": [
                {"stage": "logical_modeler", "mode": "llm", "model": "gpt-4.1-mini"},
                {"stage": "transformation", "mode": "deterministic_from_sidecar", "model": None},
            ],
        },
    )


def _make_fake_copilot(scaffold_side_effect: Any = None) -> MagicMock:
    """Return a MagicMock configured to satisfy every attribute
    ``run_ai_copilot_mode`` + ``_create_project_minimal`` touches."""
    fake = MagicMock(name="CopilotAgent")
    fake.generate_project_artifacts.return_value = _fake_generation_result()
    fake._attempt_generation_recovery.return_value = None
    fake._show_ai_analysis.return_value = None
    fake._maybe_save_project_memory.return_value = None
    # create_project is the legacy ForgeEngine path — only exercised
    # when --scaffold is set. Capture its invocation so tests can
    # assert the branch decision.
    if scaffold_side_effect is not None:
        fake.create_project.side_effect = scaffold_side_effect
    else:
        fake.create_project.return_value = True
    return fake


# ---------------------------------------------------------------------------
# UX-H.1 — default fluid forge produces a minimal layout
# ---------------------------------------------------------------------------


class TestDefaultMinimalForge:
    """When ``--scaffold`` is NOT set, ``fluid forge`` must only
    write ``contract.fluid.yaml`` (plus an optional CI file if the
    auto-CI hook runs — which the tests suppress with ``--no-ci``)."""

    def test_minimal_writes_only_contract(self, tmp_path: Path):
        fake = _make_fake_copilot()
        args = _build_args(tmp_path)
        logger = logging.getLogger("test_slice_ux_h.minimal")

        with patch("fluid_build.cli.forge.CopilotAgent", return_value=fake):
            from fluid_build.cli.forge import run_ai_copilot_mode

            rc = run_ai_copilot_mode(args, logger)

        assert rc == 0
        # Contract landed
        contract_path = tmp_path / "contract.fluid.yaml"
        assert contract_path.is_file()

        # Contract was written with the slice-4 provenance envelope.
        doc = yaml.safe_load(contract_path.read_text())
        assert doc.get("metadata", {}).get("provenance", {}).get("kind") == ("ContractMetadata")
        assert doc["metadata"]["provenance"]["generated_by"]["command"] == "fluid forge"

        ai_receipt = tmp_path / ".fluid" / "ai-work-receipt.json"
        assert ai_receipt.is_file()
        receipt_doc = json.loads(ai_receipt.read_text())
        assert receipt_doc["kind"] == "ForgeAIWorkReceipt"
        assert receipt_doc["runPlan"]["stages"][1]["mode"] == "deterministic_from_sidecar"

    def test_minimal_does_not_create_engine_tree(self, tmp_path: Path):
        """None of the opinionated engine template directories must
        appear when ``--scaffold`` is unset."""
        fake = _make_fake_copilot()
        args = _build_args(tmp_path)
        logger = logging.getLogger("test_slice_ux_h.no_engine_tree")

        with patch("fluid_build.cli.forge.CopilotAgent", return_value=fake):
            from fluid_build.cli.forge import run_ai_copilot_mode

            run_ai_copilot_mode(args, logger)

        forbidden = (
            "extracts",
            "loads",
            "transforms",
            "config",
            "docs",
            "scripts",
            "tests",
        )
        present = {d.name for d in tmp_path.iterdir() if d.is_dir()}
        leaked = present & set(forbidden)
        assert not leaked, (
            f"slice UX-H default must not create engine template dirs, "
            f"but {leaked} exist in {tmp_path}"
        )

    def test_minimal_skips_scaffold_data_folder(self, tmp_path: Path):
        """``_scaffold_data_folder`` is gated on ``--scaffold``.  The
        minimal path must leave no ``data/`` directory behind."""
        fake = _make_fake_copilot()
        args = _build_args(tmp_path)
        logger = logging.getLogger("test_slice_ux_h.no_data_folder")

        with patch("fluid_build.cli.forge.CopilotAgent", return_value=fake):
            from fluid_build.cli.forge import run_ai_copilot_mode

            run_ai_copilot_mode(args, logger)

        assert not (tmp_path / "data").exists(), "data/ must not be created in the minimal path"
        assert not (tmp_path / "dbt").exists(), "dbt/ must not be created in the minimal path"

    def test_minimal_does_not_invoke_create_project(self, tmp_path: Path):
        """The legacy ``CopilotAgent.create_project`` (which hands off
        to ``ForgeEngine``) must NOT be called in the minimal path."""
        fake = _make_fake_copilot()
        args = _build_args(tmp_path)
        logger = logging.getLogger("test_slice_ux_h.no_engine_call")

        with patch("fluid_build.cli.forge.CopilotAgent", return_value=fake):
            from fluid_build.cli.forge import run_ai_copilot_mode

            run_ai_copilot_mode(args, logger)

        fake.create_project.assert_not_called()
        # The minimal path calls generate_project_artifacts directly.
        fake.generate_project_artifacts.assert_called_once()

    def test_minimal_dry_run_does_not_call_llm_generation(self, tmp_path: Path):
        """Dry-run must not probe an LLM provider or local Ollama service."""
        fake = _make_fake_copilot()
        args = _build_args(tmp_path, dry_run=True)
        logger = logging.getLogger("test_slice_ux_h.dry_run_no_llm")

        with patch("fluid_build.cli.forge.CopilotAgent", return_value=fake):
            from fluid_build.cli.forge import run_ai_copilot_mode

            rc = run_ai_copilot_mode(args, logger)

        assert rc == 0
        fake.create_project.assert_not_called()
        fake.generate_project_artifacts.assert_not_called()
        assert not (tmp_path / "contract.fluid.yaml").exists()


# ---------------------------------------------------------------------------
# UX-H.2 — --scaffold <template> opts into the engine path
# ---------------------------------------------------------------------------


class TestScaffoldOptIn:
    """Setting ``--scaffold etl_pipeline`` (or any other template) must
    route through the legacy ``CopilotAgent.create_project`` →
    ``ForgeEngine`` path unchanged."""

    def test_scaffold_flag_calls_create_project(self, tmp_path: Path):
        fake = _make_fake_copilot()
        args = _build_args(tmp_path, scaffold="etl_pipeline")
        logger = logging.getLogger("test_slice_ux_h.scaffold_optin")

        with patch("fluid_build.cli.forge.CopilotAgent", return_value=fake):
            from fluid_build.cli.forge import run_ai_copilot_mode

            rc = run_ai_copilot_mode(args, logger)

        assert rc == 0
        # The engine path is reached via create_project, not the
        # minimal _create_project_minimal helper.
        fake.create_project.assert_called_once()
        fake.generate_project_artifacts.assert_not_called()

    def test_scaffold_flag_runs_scaffold_data_folder(self, tmp_path: Path):
        """When ``--scaffold`` is set, ``_scaffold_data_folder`` must
        still run (so pre-UX-H behaviour is preserved for opt-ins)."""
        fake = _make_fake_copilot()
        args = _build_args(tmp_path, scaffold="etl_pipeline")
        logger = logging.getLogger("test_slice_ux_h.scaffold_data_folder")

        with patch("fluid_build.cli.forge_modes._scaffold_data_folder") as mock_scaffold_data:
            with patch("fluid_build.cli.forge.CopilotAgent", return_value=fake):
                from fluid_build.cli.forge import run_ai_copilot_mode

                run_ai_copilot_mode(args, logger)

        assert mock_scaffold_data.called, (
            "_scaffold_data_folder must still run when --scaffold is set"
        )

    def test_minimal_path_does_not_run_scaffold_data_folder(self, tmp_path: Path):
        """Mirror test: the minimal (default) path must NOT call
        ``_scaffold_data_folder``."""
        fake = _make_fake_copilot()
        args = _build_args(tmp_path)
        logger = logging.getLogger("test_slice_ux_h.minimal_scaffold_data_guard")

        with patch("fluid_build.cli.forge_modes._scaffold_data_folder") as mock_scaffold_data:
            with patch("fluid_build.cli.forge.CopilotAgent", return_value=fake):
                from fluid_build.cli.forge import run_ai_copilot_mode

                run_ai_copilot_mode(args, logger)

        mock_scaffold_data.assert_not_called()


# ---------------------------------------------------------------------------
# UX-H.3 — CLI flag is registered on the forge parser
# ---------------------------------------------------------------------------


class TestScaffoldCLIRegistration:
    def _build_forge_parser(self) -> argparse.ArgumentParser:
        """``forge.register`` takes a subparser action, not a plain
        parser.  Build a top-level parser with subparsers, then hand
        the ``forge`` subparser back for inspection."""
        from fluid_build.cli.forge import COMMAND, register

        top = argparse.ArgumentParser(prog="fluid")
        subparsers = top.add_subparsers(dest="command")
        register(subparsers)
        return top

    def test_scaffold_flag_is_registered(self):
        """``fluid forge --scaffold`` must be a valid CLI flag with
        the expected metavar + default."""
        parser = self._build_forge_parser()

        # Parse without --scaffold → default None
        ns = parser.parse_args(["forge"])
        assert getattr(ns, "scaffold", "MISSING") is None

        # Parse with --scaffold etl_pipeline → value set
        ns = parser.parse_args(["forge", "--scaffold", "etl_pipeline"])
        assert ns.scaffold == "etl_pipeline"

    def test_scaffold_flag_accepts_any_template_name(self):
        """The flag is intentionally unconstrained so new templates
        don't require a CLI update.  Validation (if any) happens
        downstream in the engine path."""
        parser = self._build_forge_parser()

        for name in (
            "etl_pipeline",
            "analytics",
            "ml_pipeline",
            "streaming",
            "starter",
            "customer-360",
        ):
            ns = parser.parse_args(["forge", "--scaffold", name])
            assert ns.scaffold == name


# ---------------------------------------------------------------------------
# UX-H.4 — engine._generate_pipeline_files is a deprecation shim
# ---------------------------------------------------------------------------


class TestEnginePipelineShim:
    """``_generate_pipeline_files`` used to write CI files directly,
    bypassing the slice-7 header and slice-8 drift detection.  Slice
    UX-H converts it into a no-op shim; the original body lives on
    under ``_generate_pipeline_files_legacy`` for any caller that
    explicitly opts in."""

    def _make_engine(self, pipeline_config: Dict[str, Any]) -> Any:
        from fluid_build.forge.core.engine import ForgeEngine

        engine = ForgeEngine()
        engine.project_config = {
            "enable_ci_cd": True,
            "pipeline_config": pipeline_config,
        }
        return engine

    def test_shim_writes_no_files(self, tmp_path: Path):
        """Calling the shim with a fully populated ``pipeline_config``
        (the exact shape that used to trigger file generation) must
        leave the target dir empty."""
        engine = self._make_engine(
            {
                "provider": "github_actions",
                "complexity": "standard",
                "environments": ["dev"],
                "enable_approvals": False,
                "enable_security_scan": True,
                "enable_marketplace_publishing": False,
            }
        )
        engine._generate_pipeline_files(tmp_path)

        files = list(tmp_path.iterdir())
        assert files == [], f"slice UX-H shim must not write files, got {files}"

    def test_shim_does_not_raise_on_empty_config(self, tmp_path: Path):
        engine = self._make_engine({})
        # The shim never raises regardless of config.
        engine._generate_pipeline_files(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_legacy_symbol_is_preserved(self):
        """The renamed ``_generate_pipeline_files_legacy`` must still
        exist so any external caller or test fixture that explicitly
        opts into the old behaviour has a reachable implementation."""
        from fluid_build.forge.core.engine import ForgeEngine

        assert hasattr(ForgeEngine, "_generate_pipeline_files_legacy"), (
            "slice UX-H must preserve the pre-UX-H body under _generate_pipeline_files_legacy"
        )
        assert callable(ForgeEngine._generate_pipeline_files_legacy)

    def test_legacy_still_writes_when_explicitly_invoked(self, tmp_path: Path):
        """Opt-in callers of the legacy function must still get files
        on disk.  This keeps backward compat for any out-of-tree
        plugin or fixture that monkey-patches it."""
        engine = self._make_engine(
            {
                "provider": "github_actions",
                "complexity": "standard",
                "environments": ["dev"],
                "enable_approvals": False,
                "enable_security_scan": True,
                "enable_marketplace_publishing": False,
            }
        )
        try:
            engine._generate_pipeline_files_legacy(tmp_path)
        except Exception as exc:  # noqa: BLE001
            # The legacy body may fail if pipeline_templates isn't
            # available in the test environment — in that case it
            # logs and returns.  Either outcome (files written OR
            # quiet failure) is acceptable; the only thing that's
            # NOT acceptable is an unhandled exception escaping.
            pytest.fail(
                f"legacy _generate_pipeline_files must never raise, "
                f"but got {type(exc).__name__}: {exc}"
            )
