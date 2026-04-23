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

"""Slice 9 tests: fluid demo receipt + workspace gitignore template."""

from __future__ import annotations

import json
import logging
from argparse import Namespace
from pathlib import Path

import pytest

from fluid_build.cli import init as init_module
from fluid_build.cli.artifact_paths import workspace_init_receipt_path
from fluid_build.cli.init import (
    FLUID_GITIGNORE_BLOCK,
    _ensure_gitignore_template,
)


@pytest.fixture
def fluid_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("FLUID_HOME", str(fake_home / ".fluid"))
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    yield fake_home


@pytest.fixture
def empty_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    yield ws


# ---------------------------------------------------------------------------
# Gitignore template
# ---------------------------------------------------------------------------


class TestEnsureGitignoreTemplate:
    def test_creates_gitignore_when_missing(self, tmp_path: Path):
        _ensure_gitignore_template(tmp_path)
        gi = tmp_path / ".gitignore"
        assert gi.exists()
        content = gi.read_text()
        assert "fluid-cli: engineer-personal state" in content
        assert ".fluid/init-receipt.json" in content
        assert ".fluid/forge-receipt.json" in content
        assert ".fluid/copilot-memory.json" in content
        assert "runtime/" in content

    def test_team_shared_files_are_not_rules(self, tmp_path: Path):
        """skills.yaml / ci-state.json appear only in comments, not as rules."""
        _ensure_gitignore_template(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        # Every non-comment, non-blank line is a rule.  None of them
        # should match a team-shared file.
        rules = [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert ".fluid/skills.yaml" not in rules
        assert ".fluid/ci-state.json" not in rules
        # But the rules ARE what we expect.
        assert ".fluid/init-receipt.json" in rules
        assert "runtime/" in rules

    def test_appends_to_existing_gitignore(self, tmp_path: Path):
        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules/\n*.pyc\n", encoding="utf-8")

        _ensure_gitignore_template(tmp_path)

        content = gi.read_text()
        assert "node_modules/" in content  # user content preserved
        assert "*.pyc" in content
        assert "fluid-cli: engineer-personal state" in content

    def test_idempotent_on_second_call(self, tmp_path: Path):
        _ensure_gitignore_template(tmp_path)
        first = (tmp_path / ".gitignore").read_text()

        _ensure_gitignore_template(tmp_path)
        second = (tmp_path / ".gitignore").read_text()

        assert first == second

    def test_never_raises_on_oserror(self, tmp_path: Path, monkeypatch):
        """Permission error on gitignore write must not propagate."""
        # Read-only parent dir — write will fail
        # Use monkeypatch to force Path.write_text to raise
        import builtins

        original_write_text = Path.write_text

        def raising(self, *a, **kw):
            if self.name == ".gitignore":
                raise OSError("simulated permission denied")
            return original_write_text(self, *a, **kw)

        monkeypatch.setattr(Path, "write_text", raising)
        # Must not raise
        _ensure_gitignore_template(tmp_path)

    def test_block_constant_is_importable(self):
        """FLUID_GITIGNORE_BLOCK must be exported for tests / tooling."""
        assert isinstance(FLUID_GITIGNORE_BLOCK, str)
        assert FLUID_GITIGNORE_BLOCK.endswith("\n")
        assert ".fluid/init-receipt.json" in FLUID_GITIGNORE_BLOCK


class TestInitCreatesGitignore:
    def test_blank_init_drops_gitignore_template(self, fluid_home, empty_workspace, monkeypatch):
        logger = logging.getLogger("test.slice9")
        args = Namespace(
            name="ws",
            quickstart=False,
            blank=True,
            template=None,
            list_templates=False,
            provider="local",
            use_case=None,
            no_run=True,
            no_dag=True,
            dry_run=False,
            yes=True,
            target_dir=None,
            scan=False,
        )
        rc = init_module.run(args, logger)
        assert rc == 0

        gi = empty_workspace / ".gitignore"
        assert gi.exists()
        assert "fluid-cli: engineer-personal state" in gi.read_text()

    def test_gitignore_block_lists_personal_state_files(
        self, fluid_home, empty_workspace, monkeypatch
    ):
        logger = logging.getLogger("test.slice9")
        args = Namespace(
            name="ws",
            quickstart=False,
            blank=True,
            template=None,
            list_templates=False,
            provider="local",
            use_case=None,
            no_run=True,
            no_dag=True,
            dry_run=False,
            yes=True,
            target_dir=None,
            scan=False,
        )
        init_module.run(args, logger)

        content = (empty_workspace / ".gitignore").read_text()
        for expected in (
            ".fluid/init-receipt.json",
            ".fluid/forge-receipt.json",
            ".fluid/copilot-memory.json",
            ".fluid/logs/",
            "runtime/",
        ):
            assert expected in content, f"missing rule: {expected}"


# ---------------------------------------------------------------------------
# fluid demo receipt
# ---------------------------------------------------------------------------


class TestDemoReceipt:
    def test_demo_writes_init_receipt_with_flow_demo(self, fluid_home, tmp_path, monkeypatch):
        """fluid demo must produce the same receipt shape as fluid init,
        tagged with flow="demo" so consumers can tell them apart."""
        from fluid_build.cli import demo as demo_module

        # Chdir to a clean dir so the demo's target path is deterministic
        work = tmp_path / "workdir"
        work.mkdir()
        monkeypatch.chdir(work)

        logger = logging.getLogger("test.demo_receipt")
        args = Namespace(name="my-demo", dry_run=False, no_run=True)
        rc = demo_module.run(args, logger)
        assert rc == 0, f"fluid demo returned {rc}"

        demo_root = work / "my-demo"
        assert demo_root.exists()

        receipt_path = workspace_init_receipt_path(demo_root)
        assert receipt_path.exists(), f"demo receipt not written at {receipt_path}"

        doc = json.loads(receipt_path.read_text())
        assert doc["kind"] == "InitReceipt"
        assert doc["flow"] == "demo"
        assert doc["dry_run"] is False
        assert len(doc["artifacts"]) > 0
        # The receipt records demo-specific inputs
        assert doc["inputs"].get("demo") is True
        assert doc["inputs"].get("name") == "my-demo"
        # Command string carries the demo invocation
        assert "fluid demo" in doc["generated_by"]["command"]

    def test_demo_dry_run_does_not_write_receipt(self, fluid_home, tmp_path, monkeypatch):
        """Dry run must not leave any files behind — including the receipt."""
        from fluid_build.cli import demo as demo_module

        work = tmp_path / "workdir"
        work.mkdir()
        monkeypatch.chdir(work)

        logger = logging.getLogger("test.demo_receipt")
        args = Namespace(name="my-dry-demo", dry_run=True, no_run=True)
        rc = demo_module.run(args, logger)
        assert rc == 0

        # Demo dry-run should create nothing under the target
        demo_root = work / "my-dry-demo"
        if demo_root.exists():
            # If the dir exists, no receipt should be in it
            receipt_path = workspace_init_receipt_path(demo_root)
            assert not receipt_path.exists()
