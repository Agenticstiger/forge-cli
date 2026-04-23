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

"""End-to-end: `fluid init --blank` must write `.fluid/init-receipt.json`."""

from __future__ import annotations

import json
import logging
import os
from argparse import Namespace
from pathlib import Path

import pytest

from fluid_build.cli import init as init_module
from fluid_build.cli.artifact_paths import (
    ENVELOPE_SCHEMA_VERSION,
    workspace_init_receipt_path,
)


@pytest.fixture
def fluid_home(tmp_path, monkeypatch):
    """Isolate ~/.fluid to a tmp dir so tests don't pollute real home."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("FLUID_HOME", str(fake_home / ".fluid"))
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    yield fake_home


@pytest.fixture
def empty_workspace(tmp_path, monkeypatch):
    """Create an empty workspace dir and chdir into it for the test."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    yield ws


def _build_args(**overrides) -> Namespace:
    defaults: dict = {
        "name": "test-workspace",
        "quickstart": False,
        "blank": True,
        "template": None,
        "list_templates": False,
        "provider": "local",
        "use_case": None,
        "no_run": True,
        "no_dag": True,
        "dry_run": False,
        "yes": True,
        "target_dir": None,
        "scan": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


class TestInitBlankReceipt:
    def test_blank_mode_writes_init_receipt(self, fluid_home, empty_workspace, monkeypatch):
        logger = logging.getLogger("test.init_receipt")
        args = _build_args()

        result = init_module.run(args, logger)

        assert result == 0, "fluid init --blank should succeed"

        receipt_path = workspace_init_receipt_path(empty_workspace)
        assert receipt_path.exists(), (
            f"init-receipt.json was not written at {receipt_path}. "
            f"Files present: {list(empty_workspace.rglob('*'))}"
        )

        doc = json.loads(receipt_path.read_text())

        # Envelope shape
        assert doc["schema_version"] == ENVELOPE_SCHEMA_VERSION
        assert doc["kind"] == "InitReceipt"
        assert "T" in doc["generated_at"]
        assert doc["generated_at"].endswith("Z")
        assert doc["generated_by"]["tool"] == "fluid-cli"
        assert "command" in doc["generated_by"]

        # Payload shape
        assert doc["flow"] == "blank"
        assert doc["dry_run"] is False
        assert isinstance(doc["run_id"], str)
        assert doc["run_id"]
        assert isinstance(doc["artifacts"], list)
        assert len(doc["artifacts"]) > 0, "receipt should list at least one artifact"
        assert isinstance(doc["skipped"], list)
        assert isinstance(doc["inputs"], dict)

        # Every artifact entry carries the required fields
        for entry in doc["artifacts"]:
            assert "path" in entry
            assert "action" in entry
            assert entry["action"] in {
                "create",
                "update",
                "unchanged",
                "would-create",
            }

    def test_receipt_records_workspace_config_creation(
        self, fluid_home, empty_workspace, monkeypatch
    ):
        logger = logging.getLogger("test.init_receipt")
        args = _build_args()

        init_module.run(args, logger)

        receipt_path = workspace_init_receipt_path(empty_workspace)
        doc = json.loads(receipt_path.read_text())

        paths = {e["path"] for e in doc["artifacts"]}
        # fluid.workspace.yaml is the minimum artifact a blank init creates
        assert "fluid.workspace.yaml" in paths

    def test_receipt_inputs_include_blank_flag(self, fluid_home, empty_workspace, monkeypatch):
        logger = logging.getLogger("test.init_receipt")
        args = _build_args()

        init_module.run(args, logger)

        receipt_path = workspace_init_receipt_path(empty_workspace)
        doc = json.loads(receipt_path.read_text())

        # Inputs should record the blank=True flag (None values stripped)
        assert doc["inputs"].get("blank") is True
        assert "template" not in doc["inputs"]  # None-valued, should be dropped

    def test_command_string_preserved_in_envelope(self, fluid_home, empty_workspace, monkeypatch):
        logger = logging.getLogger("test.init_receipt")
        args = _build_args()

        init_module.run(args, logger)

        receipt_path = workspace_init_receipt_path(empty_workspace)
        doc = json.loads(receipt_path.read_text())

        # Command string should mention init and the blank flag
        command = doc["generated_by"]["command"]
        assert "fluid init" in command
        assert "--blank" in command
