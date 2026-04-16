# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Tests for ``fluid status`` (slice UX-A)."""

from __future__ import annotations

import json
import logging
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from fluid_build.cli import status as status_module
from fluid_build.cli.artifact_envelope import dump_json_with_envelope
from fluid_build.cli.artifact_paths import (
    product_ci_state_path,
    product_contract_path,
    product_forge_receipt_path,
    workspace_init_receipt_path,
)
from fluid_build.cli.status import StatusSummary, build_status_summary


def _write_workspace_config(root: Path, name: str = "test-ws") -> None:
    (root / "fluid.workspace.yaml").write_text(
        yaml.dump({"workspace": {"name": name, "domain": "retail"}}),
        encoding="utf-8",
    )


def _write_contract(
    product_root: Path,
    *,
    product_id: str = "test-product",
    domain: str = "retail",
    owner: str = "data-team",
    with_provenance: bool = False,
) -> None:
    product_root.mkdir(parents=True, exist_ok=True)
    contract = {
        "fluidVersion": "0.7.1",
        "kind": "DataProduct",
        "id": product_id,
        "name": product_id.replace("-", " ").title(),
        "metadata": {
            "domain": domain,
            "owner": {"team": owner},
            "description": "test product",
        },
    }
    if with_provenance:
        contract["metadata"]["provenance"] = {
            "schema_version": 1,
            "kind": "ContractMetadata",
            "generated_at": "2026-04-11T14:22:18Z",
            "generated_by": {
                "tool": "fluid-cli",
                "version": "0.7.9",
                "command": "fluid forge",
            },
        }
    product_contract_path(product_root).write_text(yaml.dump(contract), encoding="utf-8")


def _write_forge_receipt(
    product_root: Path,
    *,
    flow: str = "blank",
    generated_at: str = "2026-04-11T14:22:18Z",
) -> None:
    receipt_path = product_forge_receipt_path(product_root)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    body = dump_json_with_envelope(
        {
            "run_id": "test-run",
            "flow": flow,
            "dry_run": False,
            "artifacts": [{"path": "contract.fluid.yaml", "action": "create"}],
            "skipped": [],
            "inputs": {},
        },
        kind="ForgeReceipt",
        command=f"fluid forge --{flow}",
        tool_version="0.7.9",
        generated_at=generated_at,
    )
    receipt_path.write_text(body, encoding="utf-8")


def _write_init_receipt(
    workspace_root: Path,
    *,
    generated_at: str = "2026-04-11T14:00:00Z",
) -> None:
    receipt_path = workspace_init_receipt_path(workspace_root)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    body = dump_json_with_envelope(
        {
            "run_id": "init-run",
            "flow": "blank",
            "dry_run": False,
            "artifacts": [],
            "skipped": [],
            "inputs": {},
        },
        kind="InitReceipt",
        command="fluid init",
        tool_version="0.7.9",
        generated_at=generated_at,
    )
    receipt_path.write_text(body, encoding="utf-8")


def _write_ci_state(
    product_root: Path,
    *,
    provider: str = "github_actions",
    complexity: str = "standard",
    files: list | None = None,
) -> None:
    path = product_ci_state_path(product_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dump_json_with_envelope(
        {
            "provider": provider,
            "complexity": complexity,
            "environments": ["dev"],
            "options": {},
            "files": files or [{"path": ".github/workflows/ci.yml", "sha256": "abc"}],
        },
        kind="CIState",
        command=f"fluid forge --ci {provider}",
        tool_version="0.7.9",
    )
    path.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# build_status_summary — data gathering
# ---------------------------------------------------------------------------


class TestBuildStatusSummaryDegraded:
    """Degraded-graceful: missing state never crashes, fields read None."""

    def test_empty_dir_produces_empty_summary(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        summary = build_status_summary()
        assert isinstance(summary, StatusSummary)
        assert summary.product_id is None
        assert summary.domain is None
        assert summary.owner is None
        assert summary.authoring_mode == "flat"
        assert summary.ci_provider is None
        assert summary.last_forge_at is None
        assert summary.last_init_at is None

    def test_contract_only_populates_identity(self, tmp_path, monkeypatch):
        _write_contract(tmp_path, product_id="lonely", domain="retail")
        monkeypatch.chdir(tmp_path)

        summary = build_status_summary()
        assert summary.product_id == "lonely"
        assert summary.domain == "retail"
        assert summary.owner == "data-team"
        assert summary.fluid_version == "0.7.1"
        assert summary.ci_provider is None  # no ci-state
        assert summary.last_forge_at is None  # no receipt


class TestBuildStatusSummaryFullState:
    def test_contract_workspace_forge_and_ci(self, tmp_path, monkeypatch):
        _write_workspace_config(tmp_path, name="acme")
        _write_contract(tmp_path, product_id="customer-360", domain="retail")
        _write_forge_receipt(tmp_path, flow="copilot")
        _write_init_receipt(tmp_path)
        _write_ci_state(tmp_path, provider="github_actions", complexity="standard")
        monkeypatch.chdir(tmp_path)

        summary = build_status_summary()

        assert summary.workspace_name == "acme"
        assert summary.product_id == "customer-360"
        assert summary.domain == "retail"
        assert summary.owner == "data-team"
        assert summary.last_forge_flow == "copilot"
        assert summary.last_forge_at == "2026-04-11T14:22:18Z"
        assert summary.last_init_at == "2026-04-11T14:00:00Z"
        assert summary.ci_provider == "github_actions"
        assert summary.ci_complexity == "standard"
        assert summary.ci_file_count == 1


class TestAuthoringModeDetection:
    def test_no_fragments_is_flat(self, tmp_path, monkeypatch):
        _write_contract(tmp_path)
        monkeypatch.chdir(tmp_path)
        summary = build_status_summary()
        assert summary.authoring_mode == "flat"
        assert summary.fragment_count == 0

    def test_empty_fragments_dir_still_flat(self, tmp_path, monkeypatch):
        _write_contract(tmp_path)
        (tmp_path / "fragments").mkdir()
        monkeypatch.chdir(tmp_path)
        summary = build_status_summary()
        assert summary.authoring_mode == "flat"
        assert summary.fragment_count == 0

    def test_fragments_with_yaml_files_is_fragment_first(self, tmp_path, monkeypatch):
        _write_contract(tmp_path)
        (tmp_path / "fragments").mkdir()
        (tmp_path / "fragments" / "access-policy.yaml").write_text("pii: restricted\n")
        (tmp_path / "fragments" / "builds").mkdir()
        (tmp_path / "fragments" / "builds" / "main.yaml").write_text("id: main\n")
        (tmp_path / "overlays").mkdir()
        (tmp_path / "overlays" / "prod.yaml").write_text("env: prod\n")
        monkeypatch.chdir(tmp_path)

        summary = build_status_summary()
        assert summary.authoring_mode == "fragment-first"
        assert summary.fragment_count == 2
        assert summary.overlay_count == 1


class TestCIDriftRows:
    def test_pristine_ci_state_shows_clean(self, tmp_path, monkeypatch):
        _write_contract(tmp_path)
        # Write a pristine CI file + ci-state that matches its body sha
        import hashlib

        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        body = "name: ci\njobs: {}\n"
        (workflow_dir / "ci.yml").write_text(body)
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()

        _write_ci_state(
            tmp_path,
            files=[{"path": ".github/workflows/ci.yml", "sha256": sha}],
        )
        monkeypatch.chdir(tmp_path)

        summary = build_status_summary()
        assert summary.ci_provider == "github_actions"
        assert summary.ci_file_count == 1
        assert summary.ci_pristine_count == 1
        assert summary.ci_drifted_count == 0

    def test_drifted_file_shows_in_drift_count(self, tmp_path, monkeypatch):
        _write_contract(tmp_path)

        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "ci.yml").write_text("hand-edited\n")

        # ci-state records a sha for the ORIGINAL body, not what's on disk
        _write_ci_state(
            tmp_path,
            files=[
                {
                    "path": ".github/workflows/ci.yml",
                    "sha256": "00" * 32,  # fake — definitely doesn't match
                }
            ],
        )
        monkeypatch.chdir(tmp_path)

        summary = build_status_summary()
        assert summary.ci_drifted_count == 1
        assert summary.ci_pristine_count == 0


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class TestFormatters:
    def test_format_scalar_returns_em_dash_for_none(self):
        from fluid_build.cli.status import _format_scalar

        assert _format_scalar(None) == "—"
        assert _format_scalar("") == "—"
        assert _format_scalar("hello") == "hello"

    def test_authoring_formatter_shows_fragment_count(self):
        from fluid_build.cli.status import _format_authoring

        summary = StatusSummary(authoring_mode="fragment-first", fragment_count=5, overlay_count=2)
        result = _format_authoring(summary)
        assert "fragment-first" in result
        assert "5 fragments" in result
        assert "2 overlays" in result

    def test_authoring_formatter_flat_omits_counts(self):
        from fluid_build.cli.status import _format_authoring

        assert _format_authoring(StatusSummary()) == "flat"

    def test_ci_drift_shows_warning_on_drift(self):
        from fluid_build.cli.status import _format_ci_drift

        summary = StatusSummary(
            ci_provider="github_actions",
            ci_file_count=3,
            ci_pristine_count=1,
            ci_drifted_count=2,
        )
        result = _format_ci_drift(summary)
        assert "drifted: 2" in result
        assert "⚠" in result or "drifted" in result

    def test_ci_drift_shows_clean_on_no_drift(self):
        from fluid_build.cli.status import _format_ci_drift

        summary = StatusSummary(
            ci_provider="github_actions",
            ci_file_count=2,
            ci_pristine_count=2,
        )
        result = _format_ci_drift(summary)
        assert "clean" in result or "pristine" in result


# ---------------------------------------------------------------------------
# Top-level run() — never raises
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_on_empty_dir_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        logger = logging.getLogger("test.status")
        rc = status_module.run(Namespace(), logger)
        assert rc == 0

    def test_run_on_full_state_returns_zero(self, tmp_path, monkeypatch):
        _write_workspace_config(tmp_path)
        _write_contract(tmp_path)
        _write_forge_receipt(tmp_path)
        _write_ci_state(tmp_path)
        monkeypatch.chdir(tmp_path)

        logger = logging.getLogger("test.status")
        rc = status_module.run(Namespace(), logger)
        assert rc == 0

    def test_register_adds_status_subcommand(self):
        import argparse

        parser = argparse.ArgumentParser()
        sp = parser.add_subparsers()
        status_module.register(sp)

        # Parsing "status" should now route to status.run
        args = parser.parse_args(["status"])
        assert getattr(args, "cmd", None) == "status"
        assert getattr(args, "func", None) is status_module.run
