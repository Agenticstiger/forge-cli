# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""End-to-end: `fluid forge --blank` must write `.fluid/forge-receipt.json`."""

from __future__ import annotations

import json
import logging
from argparse import Namespace
from pathlib import Path

import pytest

from fluid_build.cli import forge as forge_module
from fluid_build.cli.artifact_paths import (
    ENVELOPE_SCHEMA_VERSION,
    product_forge_receipt_path,
)


@pytest.fixture
def fluid_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("FLUID_HOME", str(fake_home / ".fluid"))
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    yield fake_home


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    """Pre-create a workspace dir and chdir into it for the test."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    # Minimal workspace config so find_workspace_root works.
    (ws / "fluid.workspace.yaml").write_text(
        "workspace:\n  name: test-workspace\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(ws)
    yield ws


def _build_forge_args(**overrides) -> Namespace:
    defaults: dict = {
        "help": False,
        "blank": True,
        "non_interactive": True,
        "dry_run": False,
        "target_dir": "my-blank-product",
        "context": None,
        "show_memory": False,
        "reset_memory": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


class TestForgeBlankReceipt:
    def test_blank_mode_writes_forge_receipt(
        self, fluid_home, workspace_dir, monkeypatch
    ):
        logger = logging.getLogger("test.forge_receipt")
        args = _build_forge_args()

        result = forge_module.run(args, logger)
        assert result == 0, "fluid forge --blank should succeed"

        product_root = workspace_dir / "my-blank-product"
        assert product_root.exists(), "product dir should have been created"
        assert (product_root / "contract.fluid.yaml").exists()

        receipt_path = product_forge_receipt_path(product_root)
        assert receipt_path.exists(), (
            f"forge-receipt.json was not written at {receipt_path}"
        )

        doc = json.loads(receipt_path.read_text())

        # Envelope shape
        assert doc["schema_version"] == ENVELOPE_SCHEMA_VERSION
        assert doc["kind"] == "ForgeReceipt"
        assert doc["generated_by"]["tool"] == "fluid-cli"
        assert "fluid forge" in doc["generated_by"]["command"]
        assert "--blank" in doc["generated_by"]["command"]

        # Payload shape
        assert doc["flow"] == "blank"
        assert doc["dry_run"] is False
        assert isinstance(doc["run_id"], str) and doc["run_id"]
        assert len(doc["artifacts"]) > 0
        # contract path is relative to the product root
        paths = {e["path"] for e in doc["artifacts"]}
        assert "contract.fluid.yaml" in paths

    def test_receipt_records_blank_flag_in_inputs(
        self, fluid_home, workspace_dir, monkeypatch
    ):
        logger = logging.getLogger("test.forge_receipt")
        args = _build_forge_args()

        forge_module.run(args, logger)

        product_root = workspace_dir / "my-blank-product"
        receipt_path = product_forge_receipt_path(product_root)
        doc = json.loads(receipt_path.read_text())
        assert doc["inputs"].get("blank") is True
        assert doc["inputs"].get("non_interactive") is True

    def test_receipt_lives_under_dot_fluid(
        self, fluid_home, workspace_dir, monkeypatch
    ):
        logger = logging.getLogger("test.forge_receipt")
        args = _build_forge_args()

        forge_module.run(args, logger)

        product_root = workspace_dir / "my-blank-product"
        receipt_path = product_forge_receipt_path(product_root)
        # Receipt lives in the hidden per-product state dir
        assert receipt_path.parent.name == ".fluid"
        assert receipt_path.parent.parent == product_root

    def test_second_blank_run_on_existing_product_does_not_crash(
        self, fluid_home, workspace_dir, monkeypatch
    ):
        """Re-running forge --blank on the same target fails gracefully."""
        logger = logging.getLogger("test.forge_receipt")
        args = _build_forge_args()

        first = forge_module.run(args, logger)
        assert first == 0
        first_receipt = product_forge_receipt_path(
            workspace_dir / "my-blank-product"
        ).read_text()

        # Second run — blank mode refuses to overwrite an existing contract
        second = forge_module.run(args, logger)
        assert second == 1  # blank mode returns 1 on collision

        # First receipt still intact — second run never got far enough
        # to overwrite it.
        assert first_receipt == product_forge_receipt_path(
            workspace_dir / "my-blank-product"
        ).read_text()
