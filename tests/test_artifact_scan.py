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

"""Tests for fluid_build.cli.artifact_scan — snapshot/diff machinery."""

from __future__ import annotations

from pathlib import Path

from fluid_build.cli.artifact_scan import (
    ArtifactSnapshot,
    diff_snapshots,
    snapshot_workspace,
)


def _touch(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestSnapshotWorkspace:
    def test_empty_workspace_has_empty_snapshot(self, tmp_path: Path):
        snapshot = snapshot_workspace(tmp_path)
        assert snapshot.files == {}
        assert snapshot.root == tmp_path.resolve()

    def test_finds_workspace_config(self, tmp_path: Path):
        _touch(tmp_path / "fluid.workspace.yaml", "workspace: {name: acme}")
        snapshot = snapshot_workspace(tmp_path)
        assert len(snapshot.files) == 1
        assert any("fluid.workspace.yaml" in str(p) for p in snapshot.files)

    def test_finds_skills_file_under_dot_fluid(self, tmp_path: Path):
        _touch(tmp_path / ".fluid" / "skills.yaml", "skills: []")
        snapshot = snapshot_workspace(tmp_path)
        assert any("skills.yaml" in str(p) for p in snapshot.files)

    def test_finds_flat_layout_contract_at_root(self, tmp_path: Path):
        _touch(tmp_path / "contract.fluid.yaml", "id: flat")
        snapshot = snapshot_workspace(tmp_path)
        assert any("contract.fluid.yaml" in str(p) for p in snapshot.files)

    def test_finds_product_subdir_contracts(self, tmp_path: Path):
        _touch(tmp_path / "customer-360" / "contract.fluid.yaml", "id: c360")
        _touch(tmp_path / "orders" / "contract.fluid.yaml", "id: orders")
        snapshot = snapshot_workspace(tmp_path)
        assert len(snapshot.files) == 2

    def test_respects_max_product_depth(self, tmp_path: Path):
        _touch(tmp_path / "a" / "b" / "c" / "contract.fluid.yaml", "id: deep")
        snapshot = snapshot_workspace(tmp_path, max_product_depth=1)
        assert snapshot.files == {}

        deeper = snapshot_workspace(tmp_path, max_product_depth=3)
        assert len(deeper.files) == 1

    def test_ignores_venv_and_git_dirs(self, tmp_path: Path):
        _touch(tmp_path / ".git" / "contract.fluid.yaml", "id: hidden")
        _touch(tmp_path / ".venv" / "contract.fluid.yaml", "id: hidden")
        _touch(tmp_path / "node_modules" / "contract.fluid.yaml", "id: hidden")
        snapshot = snapshot_workspace(tmp_path)
        assert snapshot.files == {}

    def test_ignores_hidden_fluid_dir(self, tmp_path: Path):
        _touch(tmp_path / ".fluid" / "subproduct" / "contract.fluid.yaml", "id: x")
        snapshot = snapshot_workspace(tmp_path)
        # The skills file path under .fluid is explicitly tracked, but
        # contracts under .fluid/ are not.
        assert not any("subproduct" in str(p) for p in snapshot.files)


class TestDiffSnapshots:
    def test_create_action_on_new_file(self, tmp_path: Path):
        before = snapshot_workspace(tmp_path)
        _touch(tmp_path / "fluid.workspace.yaml", "workspace: {name: acme}")
        after = snapshot_workspace(tmp_path)

        entries = diff_snapshots(before, after)
        assert len(entries) == 1
        assert entries[0].action == "create"
        assert entries[0].path == "fluid.workspace.yaml"
        assert entries[0].sha256 is not None
        assert entries[0].size > 0

    def test_update_action_on_modified_file(self, tmp_path: Path):
        _touch(tmp_path / "fluid.workspace.yaml", "workspace: {name: acme}")
        before = snapshot_workspace(tmp_path)

        _touch(tmp_path / "fluid.workspace.yaml", "workspace: {name: updated}")
        after = snapshot_workspace(tmp_path)

        entries = diff_snapshots(before, after)
        assert len(entries) == 1
        assert entries[0].action == "update"
        assert entries[0].sha256 is not None

    def test_unchanged_action_on_same_file(self, tmp_path: Path):
        _touch(tmp_path / "fluid.workspace.yaml", "workspace: {name: acme}")
        before = snapshot_workspace(tmp_path)
        after = snapshot_workspace(tmp_path)

        entries = diff_snapshots(before, after)
        assert len(entries) == 1
        assert entries[0].action == "unchanged"

    def test_paths_are_relative_to_root(self, tmp_path: Path):
        before = snapshot_workspace(tmp_path)
        _touch(tmp_path / "customer-360" / "contract.fluid.yaml", "id: c360")
        after = snapshot_workspace(tmp_path)

        entries = diff_snapshots(before, after)
        # Normalised path separators for cross-platform stability
        assert entries[0].path.replace("\\", "/") == "customer-360/contract.fluid.yaml"

    def test_deleted_files_are_ignored(self, tmp_path: Path):
        """init/forge never delete files; the diff should not flag deletions."""
        _touch(tmp_path / "fluid.workspace.yaml", "workspace: {name: acme}")
        before = snapshot_workspace(tmp_path)
        (tmp_path / "fluid.workspace.yaml").unlink()
        after = snapshot_workspace(tmp_path)

        entries = diff_snapshots(before, after)
        assert entries == []

    def test_multi_file_scaffold_diff(self, tmp_path: Path):
        before = snapshot_workspace(tmp_path)

        _touch(tmp_path / "fluid.workspace.yaml", "workspace: {name: acme}")
        _touch(tmp_path / ".fluid" / "skills.yaml", "skills: []")
        _touch(tmp_path / "my-product" / "contract.fluid.yaml", "id: my-product")
        after = snapshot_workspace(tmp_path)

        entries = diff_snapshots(before, after)
        assert {e.action for e in entries} == {"create"}
        assert len(entries) == 3
        paths = {e.path.replace("\\", "/") for e in entries}
        assert paths == {
            "fluid.workspace.yaml",
            ".fluid/skills.yaml",
            "my-product/contract.fluid.yaml",
        }
