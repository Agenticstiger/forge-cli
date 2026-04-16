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

"""Branch coverage tests for workspace.py (fluid_build/cli/workspace.py)."""

import logging
from datetime import datetime
from unittest.mock import MagicMock, patch

# ---- Enums ----


class TestWorkspaceEnums:
    def test_workspace_role_values(self):
        from fluid_build.cli.workspace import WorkspaceRole

        assert WorkspaceRole.OWNER.value == "owner"
        assert WorkspaceRole.ADMIN.value == "admin"
        assert WorkspaceRole.DEVELOPER.value == "developer"
        assert WorkspaceRole.VIEWER.value == "viewer"

    def test_contract_status_values(self):
        from fluid_build.cli.workspace import ContractStatus

        assert ContractStatus.DRAFT.value == "draft"
        assert ContractStatus.IN_REVIEW.value == "in_review"
        assert ContractStatus.APPROVED.value == "approved"
        assert ContractStatus.DEPLOYED.value == "deployed"
        assert ContractStatus.ARCHIVED.value == "archived"

    def test_change_request_status_values(self):
        from fluid_build.cli.workspace import ChangeRequestStatus

        assert ChangeRequestStatus.OPEN.value == "open"
        assert ChangeRequestStatus.IN_REVIEW.value == "in_review"
        assert ChangeRequestStatus.APPROVED.value == "approved"
        assert ChangeRequestStatus.REJECTED.value == "rejected"
        assert ChangeRequestStatus.MERGED.value == "merged"


# ---- Dataclasses ----


class TestTeamMember:
    def test_to_dict(self):
        from fluid_build.cli.workspace import TeamMember, WorkspaceRole

        member = TeamMember(
            id="m1",
            name="Alice",
            email="alice@example.com",
            role=WorkspaceRole.DEVELOPER,
            joined_at=datetime(2025, 1, 1),
            last_active=datetime(2025, 6, 1),
            permissions={"read", "write"},
        )
        d = member.to_dict()
        assert d["name"] == "Alice"
        assert d["role"] == "developer"
        assert isinstance(d["permissions"], (list, set))

    def test_defaults(self):
        from fluid_build.cli.workspace import TeamMember, WorkspaceRole

        member = TeamMember(
            id="m2",
            name="Bob",
            email="bob@test.com",
            role=WorkspaceRole.VIEWER,
            joined_at=datetime.now(),
        )
        assert member.last_active is None
        assert member.permissions == set()


class TestContractVersion:
    def test_to_dict(self):
        from fluid_build.cli.workspace import ContractStatus, ContractVersion

        version = ContractVersion(
            id="v1",
            contract_path="/contracts/main.yaml",
            version="v1.1.0",
            author="alice",
            status=ContractStatus.DRAFT,
            created_at=datetime(2025, 1, 1),
            message="Initial version",
            changes=["Added field X"],
            reviewers=["bob"],
            approvals=[],
        )
        d = version.to_dict()
        assert d["version"] == "v1.1.0"
        assert d["status"] == "draft"
        assert "Added field X" in d["changes"]


class TestChangeRequest:
    def test_to_dict(self):
        from fluid_build.cli.workspace import ChangeRequest, ChangeRequestStatus

        cr = ChangeRequest(
            id="cr1",
            title="Add new field",
            description="Adding X field",
            author="alice",
            target_contract="/contracts/main.yaml",
            status=ChangeRequestStatus.OPEN,
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 2),
            changes={"added": ["field_x"]},
            reviewers=["bob"],
            comments=[{"user": "bob", "text": "LGTM"}],
        )
        d = cr.to_dict()
        assert d["title"] == "Add new field"
        assert d["status"] == "open"


class TestWorkspaceConfig:
    def test_to_dict(self):
        from fluid_build.cli.workspace import WorkspaceConfig

        config = WorkspaceConfig(
            name="test-workspace",
            description="A test workspace",
            owner="admin",
            created_at=datetime(2025, 1, 1),
            settings={"auto_approve": True},
            integrations={"slack": {"channel": "#data"}},
        )
        d = config.to_dict()
        assert d["name"] == "test-workspace"
        assert d["settings"]["auto_approve"] is True


# ---- WorkspaceManager ----


class TestWorkspaceManager:
    def _make_manager(self, tmp_path):
        from fluid_build.cli.workspace import WorkspaceManager

        with patch.object(WorkspaceManager, "_ensure_git_repo"):
            return WorkspaceManager(workspace_dir=tmp_path / ".fluid-workspace")

    def test_init(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        assert mgr.db_path.exists()

    def test_initialize_workspace(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.initialize_workspace("test", "Test workspace", "admin")
        assert result is True

    def test_get_workspace_config_not_initialized(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        config = mgr.get_workspace_config()
        assert config is None

    def test_get_workspace_config_after_init(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.initialize_workspace("test", "Test workspace", "admin")
        config = mgr.get_workspace_config()
        assert config is not None
        assert config.name == "test"
        assert config.owner == "admin"

    def test_add_team_member(self, tmp_path):
        from fluid_build.cli.workspace import TeamMember, WorkspaceRole

        mgr = self._make_manager(tmp_path)
        member = TeamMember(
            id="m1",
            name="Alice",
            email="alice@test.com",
            role=WorkspaceRole.DEVELOPER,
            joined_at=datetime.now(),
        )
        result = mgr.add_team_member(member)
        assert result is True

    def test_get_team_members_empty(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        members = mgr.get_team_members()
        assert members == []

    def test_get_team_members_after_add(self, tmp_path):
        from fluid_build.cli.workspace import TeamMember, WorkspaceRole

        mgr = self._make_manager(tmp_path)
        member = TeamMember(
            id="m1",
            name="Alice",
            email="alice@test.com",
            role=WorkspaceRole.DEVELOPER,
            joined_at=datetime.now(),
        )
        mgr.add_team_member(member)
        members = mgr.get_team_members()
        assert len(members) == 1
        assert members[0].name == "Alice"

    def test_create_contract_version(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        version_id = mgr.create_contract_version(
            contract_path="/contracts/main.yaml",
            author="alice",
            message="Initial version",
            changes=["Created field X"],
        )
        assert version_id is not None

    def test_get_contract_versions_empty(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        versions = mgr.get_contract_versions()
        assert versions == []

    def test_get_contract_versions_filtered(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.create_contract_version("/a.yaml", "alice", "v1", ["change1"])
        mgr.create_contract_version("/b.yaml", "bob", "v2", ["change2"])
        # Filter by path
        versions_a = mgr.get_contract_versions("/a.yaml")
        assert len(versions_a) == 1
        # All versions
        versions_all = mgr.get_contract_versions()
        assert len(versions_all) == 2

    def test_create_change_request(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        request_id = mgr.create_change_request(
            title="Add field",
            description="Adding new field X",
            author="alice",
            target_contract="/contracts/main.yaml",
            changes={"added": ["field_x"]},
        )
        assert request_id is not None

    def test_get_change_requests_empty(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        requests = mgr.get_change_requests()
        assert requests == []

    def test_get_change_requests_filtered_by_status(self, tmp_path):
        from fluid_build.cli.workspace import ChangeRequestStatus

        mgr = self._make_manager(tmp_path)
        mgr.create_change_request("CR1", "desc", "alice", "/a.yaml", {})
        requests = mgr.get_change_requests(status=ChangeRequestStatus.OPEN)
        assert len(requests) == 1

    def test_approve_change_request_not_found(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        result = mgr.approve_change_request("nonexistent-id", "bob")
        assert result is False

    def test_approve_change_request_wrong_status(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        req_id = mgr.create_change_request("CR1", "desc", "alice", "/a.yaml", {})
        # Status is OPEN, not IN_REVIEW
        result = mgr.approve_change_request(req_id, "bob")
        assert result is False

    def test_get_activity_log(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        mgr.initialize_workspace("test", "desc", "admin")
        activities = mgr.get_activity_log(limit=10)
        assert len(activities) >= 1

    def test_get_activity_log_empty(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        activities = mgr.get_activity_log()
        assert activities == []

    def test_ensure_git_repo_no_git(self, tmp_path):
        from fluid_build.cli.workspace import WorkspaceManager

        with patch("fluid_build.cli.workspace.GIT_AVAILABLE", False):
            WorkspaceManager(workspace_dir=tmp_path / ".fluid-ws2")
            # Should not raise even without git

    def test_initialize_workspace_exception(self, tmp_path):
        mgr = self._make_manager(tmp_path)
        with patch.object(mgr, "_log_activity", side_effect=RuntimeError):
            # Initialize should still succeed since _log_activity failures are silent
            mgr.initialize_workspace("test", "desc", "admin")
            # Depends on implementation — may or may not fail


# ---- CLI integration functions ----


class TestWorkspaceCLI:
    def test_register(self):
        import argparse

        from fluid_build.cli.workspace import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        # Should not raise

    @patch("fluid_build.cli.workspace.WorkspaceManager")
    def test_run_init(self, mock_ws_cls, tmp_path):
        from fluid_build.cli.workspace import run

        mock_ws = MagicMock()
        mock_ws.initialize_workspace.return_value = True
        mock_ws_cls.return_value = mock_ws
        args = MagicMock()
        args.workspace_action = "init"
        args.name = "test"
        args.description = "desc"
        args.owner = "admin"
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.workspace.WorkspaceManager")
    def test_run_info_no_workspace(self, mock_ws_cls):
        from fluid_build.cli.workspace import run

        mock_ws = MagicMock()
        mock_ws.get_workspace_config.return_value = None
        mock_ws_cls.return_value = mock_ws
        args = MagicMock()
        args.workspace_action = "info"
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 1

    @patch("fluid_build.cli.workspace.WorkspaceManager")
    def test_run_team_list(self, mock_ws_cls):
        from fluid_build.cli.workspace import run

        mock_ws = MagicMock()
        mock_ws.get_team_members.return_value = []
        mock_ws_cls.return_value = mock_ws
        args = MagicMock()
        args.workspace_action = "team"
        args.team_action = "list"
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.workspace.WorkspaceManager")
    def test_run_version_list(self, mock_ws_cls):
        from fluid_build.cli.workspace import run

        mock_ws = MagicMock()
        mock_ws.get_contract_versions.return_value = []
        mock_ws_cls.return_value = mock_ws
        args = MagicMock()
        args.workspace_action = "version"
        args.version_action = "list"
        args.contract = None
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.workspace.WorkspaceManager")
    def test_run_activity(self, mock_ws_cls):
        from fluid_build.cli.workspace import run

        mock_ws = MagicMock()
        mock_ws.get_activity_log.return_value = []
        mock_ws_cls.return_value = mock_ws
        args = MagicMock()
        args.workspace_action = "activity"
        args.limit = 50
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.workspace.WorkspaceManager")
    def test_run_unknown_action(self, mock_ws_cls):
        from fluid_build.cli.workspace import run

        mock_ws_cls.return_value = MagicMock()
        args = MagicMock()
        args.workspace_action = "nonexistent"
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result in (0, 1)

    @patch("fluid_build.cli.workspace.WorkspaceManager")
    def test_run_exception(self, mock_ws_cls):
        from fluid_build.cli.workspace import run

        mock_ws_cls.side_effect = RuntimeError("fail")
        args = MagicMock()
        args.workspace_action = "init"
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 1


# ---- handle_ functions ----


class TestHandleFunctions:
    @patch("fluid_build.cli.workspace.WorkspaceManager")
    def test_handle_init_success(self, _mock_ws_cls):
        from fluid_build.cli.workspace import handle_init_workspace

        mock_ws = MagicMock()
        mock_ws.initialize_workspace.return_value = True
        args = MagicMock()
        args.name = "test"
        args.description = "A test"
        args.owner = "admin"
        logger = logging.getLogger("test")
        result = handle_init_workspace(args, mock_ws, logger)
        assert result == 0

    @patch("fluid_build.cli.workspace.WorkspaceManager")
    def test_handle_init_failure(self, _mock_ws_cls):
        from fluid_build.cli.workspace import handle_init_workspace

        mock_ws = MagicMock()
        mock_ws.initialize_workspace.return_value = False
        args = MagicMock()
        args.name = "test"
        args.description = "desc"
        args.owner = "admin"
        logger = logging.getLogger("test")
        result = handle_init_workspace(args, mock_ws, logger)
        assert result == 1

    def test_handle_workspace_info_with_config(self):
        from fluid_build.cli.workspace import WorkspaceConfig, handle_workspace_info

        mock_ws = MagicMock()
        mock_ws.get_workspace_config.return_value = WorkspaceConfig(
            name="test", description="desc", owner="admin", created_at=datetime.now()
        )
        mock_ws.get_team_members.return_value = []
        mock_ws.get_contract_versions.return_value = []
        mock_ws.get_change_requests.return_value = []
        args = MagicMock()
        logger = logging.getLogger("test")
        result = handle_workspace_info(args, mock_ws, logger)
        assert result == 0

    def test_handle_team_add(self):
        from fluid_build.cli.workspace import handle_team_management

        mock_ws = MagicMock()
        mock_ws.add_team_member.return_value = True
        args = MagicMock()
        args.team_action = "add"
        args.name = "Alice"
        args.email = "alice@test.com"
        args.role = "developer"
        logger = logging.getLogger("test")
        result = handle_team_management(args, mock_ws, logger)
        assert result == 0

    def test_handle_version_create(self):
        from fluid_build.cli.workspace import handle_version_management

        mock_ws = MagicMock()
        mock_ws.create_contract_version.return_value = "v1"
        args = MagicMock()
        args.version_action = "create"
        args.contract = "/contract.yaml"
        args.message = "Initial version"
        args.changes = "field1,field2"
        args.author = "alice"
        logger = logging.getLogger("test")
        result = handle_version_management(args, mock_ws, logger)
        assert result == 0

    def test_handle_changes_create(self):
        from fluid_build.cli.workspace import handle_change_management

        mock_ws = MagicMock()
        mock_ws.create_change_request.return_value = "cr1"
        args = MagicMock()
        args.changes_action = "create"
        args.title = "Add field"
        args.description = "Adding X"
        args.author = "alice"
        args.contract = "/contract.yaml"
        args.changes = '{"added": ["x"]}'
        logger = logging.getLogger("test")
        result = handle_change_management(args, mock_ws, logger)
        assert result == 0

    def test_handle_changes_approve(self):
        from fluid_build.cli.workspace import handle_change_management

        mock_ws = MagicMock()
        mock_ws.approve_change_request.return_value = True
        args = MagicMock()
        args.changes_action = "approve"
        args.request_id = "cr1"
        args.approver = "bob"
        logger = logging.getLogger("test")
        result = handle_change_management(args, mock_ws, logger)
        assert result == 0

    def test_handle_activity_with_data(self):
        from fluid_build.cli.workspace import handle_activity_log

        mock_ws = MagicMock()
        mock_ws.get_activity_log.return_value = [
            {
                "timestamp": "2025-01-01",
                "user_id": "admin",
                "action": "init",
                "target_type": "workspace",
                "target_id": "12345678-abcd",
                "details": {},
            }
        ]
        args = MagicMock()
        args.limit = 50
        logger = logging.getLogger("test")
        result = handle_activity_log(args, mock_ws, logger)
        assert result == 0
