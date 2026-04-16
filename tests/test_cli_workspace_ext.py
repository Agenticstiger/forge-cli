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

"""Unit tests for fluid_build/cli/workspace.py (76 missed lines)."""

import argparse
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Enums / dataclasses
# ---------------------------------------------------------------------------


class TestWorkspaceEnums(unittest.TestCase):
    def test_workspace_role_values(self):
        from fluid_build.cli.workspace import WorkspaceRole

        assert WorkspaceRole.OWNER.value == "owner"
        assert WorkspaceRole.ADMIN.value == "admin"
        assert WorkspaceRole.DEVELOPER.value == "developer"
        assert WorkspaceRole.VIEWER.value == "viewer"

    def test_contract_status_values(self):
        from fluid_build.cli.workspace import ContractStatus

        assert ContractStatus.DRAFT.value == "draft"
        assert ContractStatus.DEPLOYED.value == "deployed"

    def test_change_request_status_values(self):
        from fluid_build.cli.workspace import ChangeRequestStatus

        assert ChangeRequestStatus.OPEN.value == "open"
        assert ChangeRequestStatus.MERGED.value == "merged"


class TestTeamMemberToDict(unittest.TestCase):
    def test_to_dict_has_required_keys(self):
        from fluid_build.cli.workspace import TeamMember, WorkspaceRole

        member = TeamMember(
            id="m1",
            name="Alice",
            email="alice@example.com",
            role=WorkspaceRole.DEVELOPER,
            joined_at=datetime(2024, 1, 1),
        )
        d = member.to_dict()
        assert d["id"] == "m1"
        assert d["name"] == "Alice"
        assert d["role"] == "developer"
        assert d["last_active"] is None

    def test_to_dict_with_last_active(self):
        from fluid_build.cli.workspace import TeamMember, WorkspaceRole

        member = TeamMember(
            id="m2",
            name="Bob",
            email="bob@example.com",
            role=WorkspaceRole.ADMIN,
            joined_at=datetime(2024, 1, 1),
            last_active=datetime(2024, 6, 1),
        )
        d = member.to_dict()
        assert d["last_active"] is not None


class TestContractVersionToDict(unittest.TestCase):
    def test_to_dict_has_required_keys(self):
        from fluid_build.cli.workspace import ContractStatus, ContractVersion

        cv = ContractVersion(
            id="v1",
            contract_path="my-contract.yaml",
            version="v1.0.0",
            author="alice",
            status=ContractStatus.DRAFT,
            created_at=datetime(2024, 1, 1),
            message="Initial version",
        )
        d = cv.to_dict()
        assert d["id"] == "v1"
        assert d["version"] == "v1.0.0"
        assert d["status"] == "draft"
        assert d["changes"] == []


class TestChangeRequestToDict(unittest.TestCase):
    def test_to_dict_basic(self):
        from fluid_build.cli.workspace import ChangeRequest, ChangeRequestStatus

        cr = ChangeRequest(
            id="cr1",
            title="Fix schema",
            description="Updated column types",
            author="alice",
            target_contract="my-contract.yaml",
            status=ChangeRequestStatus.OPEN,
            created_at=datetime(2024, 1, 1),
            updated_at=datetime(2024, 1, 2),
            changes={"type_change": "STRING->INT"},
        )
        d = cr.to_dict()
        assert d["id"] == "cr1"
        assert d["status"] == "open"
        assert d["changes"] == {"type_change": "STRING->INT"}


class TestWorkspaceConfigToDict(unittest.TestCase):
    def test_to_dict_basic(self):
        from fluid_build.cli.workspace import WorkspaceConfig

        config = WorkspaceConfig(
            name="test-workspace",
            description="Test",
            owner="alice",
            created_at=datetime(2024, 1, 1),
        )
        d = config.to_dict()
        assert d["name"] == "test-workspace"
        assert d["owner"] == "alice"


# ---------------------------------------------------------------------------
# WorkspaceManager
# ---------------------------------------------------------------------------


class TestWorkspaceManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def _make_manager(self):
        from fluid_build.cli.workspace import WorkspaceManager

        return WorkspaceManager(workspace_dir=Path(self.temp_dir))

    def test_init_creates_workspace_dir(self):
        manager = self._make_manager()
        assert manager.workspace_dir.exists()

    def test_init_creates_database(self):
        manager = self._make_manager()
        assert manager.db_path.exists()

    def test_initialize_workspace_returns_true(self):
        manager = self._make_manager()
        result = manager.initialize_workspace("test-ws", "Test workspace", "alice")
        assert result is True

    def test_get_workspace_config_after_init(self):
        manager = self._make_manager()
        manager.initialize_workspace("my-workspace", "Desc", "alice")
        config = manager.get_workspace_config()
        assert config is not None
        assert config.name == "my-workspace"
        assert config.owner == "alice"

    def test_get_workspace_config_none_before_init(self):
        manager = self._make_manager()
        config = manager.get_workspace_config()
        assert config is None

    def test_add_team_member(self):
        from fluid_build.cli.workspace import TeamMember, WorkspaceRole

        manager = self._make_manager()
        member = TeamMember(
            id="m1",
            name="Alice",
            email="alice@example.com",
            role=WorkspaceRole.DEVELOPER,
            joined_at=datetime.now(),
        )
        result = manager.add_team_member(member)
        assert result is True

    def test_get_team_members_after_add(self):
        from fluid_build.cli.workspace import TeamMember, WorkspaceRole

        manager = self._make_manager()
        member = TeamMember(
            id="m2",
            name="Bob",
            email="bob@example.com",
            role=WorkspaceRole.VIEWER,
            joined_at=datetime.now(),
        )
        manager.add_team_member(member)
        members = manager.get_team_members()
        assert len(members) == 1
        assert members[0].name == "Bob"

    def test_create_contract_version(self):
        manager = self._make_manager()
        version_id = manager.create_contract_version(
            "contract.yaml", "alice", "Initial version", ["Added field x"]
        )
        assert version_id is not None
        assert len(version_id) > 0

    def test_get_contract_versions_after_create(self):
        manager = self._make_manager()
        manager.create_contract_version("contract.yaml", "alice", "v1", [])
        versions = manager.get_contract_versions("contract.yaml")
        assert len(versions) == 1

    def test_get_all_contract_versions(self):
        manager = self._make_manager()
        manager.create_contract_version("c1.yaml", "alice", "v1", [])
        manager.create_contract_version("c2.yaml", "bob", "v1", [])
        versions = manager.get_contract_versions()
        assert len(versions) == 2

    def test_create_change_request(self):
        manager = self._make_manager()
        req_id = manager.create_change_request(
            "Fix schema", "Update column types", "alice", "contract.yaml", {"key": "value"}
        )
        assert req_id is not None

    def test_get_change_requests_after_create(self):
        manager = self._make_manager()
        manager.create_change_request("Fix schema", "desc", "alice", "contract.yaml", {})
        requests = manager.get_change_requests()
        assert len(requests) == 1
        assert requests[0].title == "Fix schema"

    def test_get_change_requests_by_status(self):
        from fluid_build.cli.workspace import ChangeRequestStatus

        manager = self._make_manager()
        manager.create_change_request("Open req", "desc", "alice", "c.yaml", {})
        open_requests = manager.get_change_requests(ChangeRequestStatus.OPEN)
        assert len(open_requests) == 1

    def test_approve_change_request_fails_when_not_in_review(self):
        manager = self._make_manager()
        req_id = manager.create_change_request("Fix", "desc", "alice", "c.yaml", {})
        # Request is OPEN, not IN_REVIEW, so approval should fail
        result = manager.approve_change_request(req_id, "reviewer")
        assert result is False

    def test_approve_change_request_nonexistent_id(self):
        manager = self._make_manager()
        result = manager.approve_change_request("nonexistent-id", "reviewer")
        assert result is False

    def test_get_activity_log(self):
        manager = self._make_manager()
        manager.initialize_workspace("ws", "desc", "alice")
        activities = manager.get_activity_log(limit=10)
        assert isinstance(activities, list)

    def test_version_numbering(self):
        manager = self._make_manager()
        v1_id = manager.create_contract_version("c.yaml", "alice", "v1", [])
        v2_id = manager.create_contract_version("c.yaml", "alice", "v2", [])
        versions = manager.get_contract_versions("c.yaml")
        version_nums = [v.version for v in versions]
        # Should have two distinct versions
        assert len(set(version_nums)) == 2


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


class TestRegister(unittest.TestCase):
    def test_registers_workspace_command(self):
        from fluid_build.cli.workspace import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["workspace"])
        # Should not raise
        assert hasattr(args, "func")

    def test_registers_init_subcommand(self):
        from fluid_build.cli.workspace import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["workspace", "init", "my-workspace"])
        assert args.name == "my-workspace"

    def test_registers_team_add_subcommand(self):
        from fluid_build.cli.workspace import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["workspace", "team", "add", "Alice", "alice@example.com"])
        assert args.name == "Alice"

    def test_registers_version_create_subcommand(self):
        from fluid_build.cli.workspace import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(
            ["workspace", "version", "create", "contract.yaml", "--message", "Initial"]
        )
        assert args.contract == "contract.yaml"
        assert args.message == "Initial"


# ---------------------------------------------------------------------------
# run() — unknown action
# ---------------------------------------------------------------------------


class TestRunUnknownAction(unittest.TestCase):
    def test_run_unknown_action_returns_1(self):
        from fluid_build.cli.workspace import run

        args = argparse.Namespace(workspace_action="nonexistent_action")
        logger = logging.getLogger("test")

        with patch("fluid_build.cli.workspace.WorkspaceManager"):
            result = run(args, logger)
        assert result == 1

    def test_run_exception_returns_1(self):
        from fluid_build.cli.workspace import run

        args = argparse.Namespace(
            workspace_action="init", name="test", description=None, owner=None
        )
        logger = logging.getLogger("test")

        with patch(
            "fluid_build.cli.workspace.WorkspaceManager", side_effect=RuntimeError("db fail")
        ):
            result = run(args, logger)
        assert result == 1


# ---------------------------------------------------------------------------
# handle_init_workspace()
# ---------------------------------------------------------------------------


class TestHandleInitWorkspace(unittest.TestCase):
    def _make_manager_with_tmp(self):
        from fluid_build.cli.workspace import WorkspaceManager

        tmp = tempfile.mkdtemp()
        return WorkspaceManager(workspace_dir=Path(tmp))

    def test_handle_init_success_returns_0(self):
        from fluid_build.cli.workspace import handle_init_workspace

        manager = self._make_manager_with_tmp()
        args = argparse.Namespace(name="ws-test", description="desc", owner="alice")
        logger = logging.getLogger("test")

        with patch("fluid_build.cli.workspace.RICH_AVAILABLE", True):
            result = handle_init_workspace(args, manager, logger)
        assert result == 0

    def test_handle_init_without_rich_returns_1(self):
        from fluid_build.cli.workspace import handle_init_workspace

        manager = self._make_manager_with_tmp()
        args = argparse.Namespace(name="ws-test", description="desc", owner="alice")
        logger = logging.getLogger("test")

        with (
            patch("fluid_build.cli.workspace.RICH_AVAILABLE", False),
            patch("fluid_build.cli.workspace.cprint"),
        ):
            result = handle_init_workspace(args, manager, logger)
        assert result == 1

    def test_handle_init_failure_returns_1(self):
        from fluid_build.cli.workspace import handle_init_workspace

        manager = self._make_manager_with_tmp()
        manager.initialize_workspace = MagicMock(return_value=False)
        args = argparse.Namespace(name="ws-fail", description="desc", owner="alice")
        logger = logging.getLogger("test")

        with patch("fluid_build.cli.workspace.RICH_AVAILABLE", True):
            result = handle_init_workspace(args, manager, logger)
        assert result == 1


if __name__ == "__main__":
    unittest.main()
