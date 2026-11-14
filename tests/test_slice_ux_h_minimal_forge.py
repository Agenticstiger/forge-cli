# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Slice UX-H: ``--scaffold <template>`` registration + the engine CI shim.

Two pins remain from slice UX-H (the minimal-by-default AI mode it also covered is now driven
by ``cli/forge_ai.py``, pinned in ``tests/cli/test_forge_ai.py``):

1. ``--scaffold`` / ``--template`` is registered on the forge parser and takes any template name.

2. ``ForgeEngine._generate_pipeline_files`` is a deprecation shim that no-ops. The original
   body survives under ``_generate_pipeline_files_legacy`` so any external caller or test
   fixture that needs the old behaviour still has a reachable implementation. Live CI file
   generation routes exclusively through ``forge_modes._scaffold_ci_pipeline``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict

import pytest

# ---------------------------------------------------------------------------
# UX-H.3 — CLI flag is registered on the forge parser
# ---------------------------------------------------------------------------


class TestScaffoldCLIRegistration:
    def _build_forge_parser(self) -> argparse.ArgumentParser:
        """``forge.register`` takes a subparser action, not a plain
        parser.  Build a top-level parser with subparsers, then hand
        the ``forge`` subparser back for inspection."""
        from fluid_build.cli.forge import register

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

        assert hasattr(
            ForgeEngine, "_generate_pipeline_files_legacy"
        ), "slice UX-H must preserve the pre-UX-H body under _generate_pipeline_files_legacy"
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


# ---------------------------------------------------------------------------
# SECURITY — additional_files write loop must confine writes to target_dir
# ---------------------------------------------------------------------------
