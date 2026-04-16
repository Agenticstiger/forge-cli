# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Slice UX-E: regression tests for three bugs surfaced by the QA pass.

Bug #1 — ``fluid status`` read ``metadata.domain`` but v0.7.2 contracts
put ``domain`` at the top level, so the Domain row was always blank
after ``fluid forge --blank``.

Bug #2 — ``fluid init --blank`` wrote a ``.gitignore`` and a legacy
v0.5.7 ``contract.fluid.json`` under ``bronze_*/`` that never made it
into ``init-receipt.json`` because ``artifact_scan`` only tracked
``fluid.workspace.yaml``/``contract.fluid.yaml``/``skills.yaml``.

Bug #4 — ``fluid init`` → interactive menu → "Start from a template"
crashed with ``unsupported operand type(s) for /: 'PosixPath' and
'NoneType'`` because ``_resolve_menu_choice`` didn't populate
``args.template``, so ``copy_template`` concatenated ``Path / None``.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fluid_build.cli import init as init_module
from fluid_build.cli import status as status_module
from fluid_build.cli.artifact_scan import diff_snapshots, snapshot_workspace
from fluid_build.cli.status import StatusSummary, build_status_summary


# ---------------------------------------------------------------------------
# Bug #1 — status reads top-level domain
# ---------------------------------------------------------------------------


def _write_contract_v072_top_level_domain(target: Path) -> None:
    """Write a minimal v0.7.2-shape contract: domain at top level."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.dump(
            {
                "fluidVersion": "0.7.2",
                "kind": "DataProduct",
                "id": "test-product",
                "name": "Test Product",
                "description": "test",
                "domain": "retail",
                "metadata": {
                    "layer": "Bronze",
                    "owner": {"team": "data"},
                },
                "builds": [
                    {
                        "id": "main",
                        "engine": "sql",
                        "pattern": "embedded-logic",
                        "properties": {"sql": "SELECT 1"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_legacy_contract_nested_domain(target: Path) -> None:
    """Write a pre-0.7.2 contract: domain nested under metadata."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.dump(
            {
                "fluidVersion": "0.7.1",
                "kind": "DataProduct",
                "id": "legacy-product",
                "name": "Legacy Product",
                "metadata": {
                    "domain": "finance",
                    "owner": {"team": "legacy-team"},
                },
            }
        ),
        encoding="utf-8",
    )


class TestStatusTopLevelDomainBug1:
    def test_v072_contract_top_level_domain_populates_summary(self, tmp_path, monkeypatch):
        _write_contract_v072_top_level_domain(tmp_path / "contract.fluid.yaml")
        monkeypatch.chdir(tmp_path)
        summary = build_status_summary()
        assert summary.domain == "retail"
        assert summary.owner == "data"

    def test_legacy_contract_nested_domain_still_read(self, tmp_path, monkeypatch):
        """Backward compat: pre-0.7.2 contracts keep working."""
        _write_contract_v072_top_level_domain(tmp_path / "_ignored.yaml")
        _write_legacy_contract_nested_domain(tmp_path / "contract.fluid.yaml")
        monkeypatch.chdir(tmp_path)
        summary = build_status_summary()
        assert summary.domain == "finance"
        assert summary.owner == "legacy-team"

    def test_fluid_version_accepts_float_from_yaml(self, tmp_path, monkeypatch):
        """Unquoted fluidVersion in YAML becomes a float; summary must
        coerce it to a string instead of silently dropping the row."""
        (tmp_path / "contract.fluid.yaml").write_text(
            "fluidVersion: 0.7.2\nkind: DataProduct\nid: test\nname: Test\n"
            "domain: ops\nmetadata:\n  owner:\n    team: data\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        summary = build_status_summary()
        assert summary.fluid_version == "0.7.2"

    def test_missing_domain_stays_none(self, tmp_path, monkeypatch):
        (tmp_path / "contract.fluid.yaml").write_text(
            yaml.dump(
                {
                    "fluidVersion": "0.7.2",
                    "kind": "DataProduct",
                    "id": "no-domain",
                    "name": "No Domain",
                    "metadata": {"owner": {"team": "data"}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        summary = build_status_summary()
        assert summary.domain is None


# ---------------------------------------------------------------------------
# Bug #2 — artifact_scan tracks .gitignore and contract.fluid.json
# ---------------------------------------------------------------------------


class TestArtifactScanCoverageBug2:
    def test_gitignore_is_tracked(self, tmp_path: Path):
        before = snapshot_workspace(tmp_path)
        (tmp_path / ".gitignore").write_text("runtime/\n", encoding="utf-8")
        after = snapshot_workspace(tmp_path)
        entries = diff_snapshots(before, after)
        paths = {e.path for e in entries if e.action == "create"}
        assert ".gitignore" in paths

    def test_json_contract_at_root_is_tracked(self, tmp_path: Path):
        before = snapshot_workspace(tmp_path)
        (tmp_path / "contract.fluid.json").write_text('{"id": "x"}', encoding="utf-8")
        after = snapshot_workspace(tmp_path)
        entries = diff_snapshots(before, after)
        paths = {e.path for e in entries if e.action == "create"}
        assert "contract.fluid.json" in paths

    def test_json_contract_in_subdir_is_tracked(self, tmp_path: Path):
        """Blank-init writes bronze_<ws>/contract.fluid.json — the scan
        must walk one level deep and pick it up."""
        before = snapshot_workspace(tmp_path)
        (tmp_path / "bronze_ws").mkdir()
        (tmp_path / "bronze_ws" / "contract.fluid.json").write_text('{"id": "b"}', encoding="utf-8")
        after = snapshot_workspace(tmp_path)
        entries = diff_snapshots(before, after)
        paths = {e.path for e in entries if e.action == "create"}
        # The scan records the relative path from the root
        assert any("contract.fluid.json" in p for p in paths)

    def test_both_yaml_and_json_siblings_are_tracked(self, tmp_path: Path):
        before = snapshot_workspace(tmp_path)
        (tmp_path / "contract.fluid.yaml").write_text("fluidVersion: '0.7.2'", encoding="utf-8")
        (tmp_path / "contract.fluid.json").write_text('{"fluidVersion": "0.7.2"}', encoding="utf-8")
        after = snapshot_workspace(tmp_path)
        entries = diff_snapshots(before, after)
        paths = {e.path for e in entries if e.action == "create"}
        assert "contract.fluid.yaml" in paths
        assert "contract.fluid.json" in paths


# ---------------------------------------------------------------------------
# Bug #4 — interactive menu "Start from a template" crash
# ---------------------------------------------------------------------------


def _build_args(**overrides) -> argparse.Namespace:
    defaults: dict = {
        "name": None,
        "template": None,
        "blank": False,
        "quickstart": False,
        "yes": False,
        "provider": "local",
        "use_case": None,
        "no_run": True,
        "no_dag": True,
        "dry_run": False,
        "target_dir": None,
        "scan": False,
        "list_templates": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestTemplateMenuCrashBug4:
    def test_ask_template_name_exists_and_defaults_to_customer_360(self):
        """The helper must exist and return a sensible default."""
        # Force the non-Rich path so the prompt is deterministic.
        with patch.object(init_module, "RICH_AVAILABLE", False):
            assert init_module._ask_template_name() == "customer-360"

    def test_resolve_menu_template_populates_args_template(self, tmp_path, monkeypatch):
        """detect_mode's inner _resolve_menu_choice must call the
        template picker and populate args.template before returning."""
        monkeypatch.chdir(tmp_path)
        args = _build_args()
        with patch.object(init_module, "_ask_creation_mode", return_value="template"):
            with patch.object(
                init_module, "_ask_template_name", return_value="customer-360"
            ):
                mode = init_module.detect_mode(args, logging.getLogger("test"))
        assert mode == "template"
        assert args.template == "customer-360"

    def test_template_mode_defensive_guard_returns_1(self, tmp_path, monkeypatch):
        """template_mode called with args.template=None must return 1
        with a clear error, not crash inside copy_template."""
        monkeypatch.chdir(tmp_path)
        args = _build_args(template=None, name="somename")
        rc = init_module.template_mode(args, logging.getLogger("test"))
        assert rc == 1

    def test_copy_template_with_none_raises_typeerror(self):
        """Sanity: the old crash still exists at the copy_template level
        when called directly with None.  The guard in template_mode is
        what prevents users from ever reaching this code path."""
        with pytest.raises(TypeError):
            init_module.copy_template(Path("/tmp/nope"), None, logging.getLogger("t"))

    def test_quickstart_menu_still_sets_customer_360(self, tmp_path, monkeypatch):
        """Regression: the existing 'quickstart' menu path must keep
        working (customer-360 + --yes)."""
        monkeypatch.chdir(tmp_path)
        args = _build_args()
        with patch.object(init_module, "_ask_creation_mode", return_value="quickstart"):
            mode = init_module.detect_mode(args, logging.getLogger("test"))
        assert mode == "template"
        assert args.template == "customer-360"
        assert args.yes is True
