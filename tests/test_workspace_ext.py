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

"""Tests for workspace dataclass to_dict methods and enums in cli/workspace.py."""

from datetime import datetime

from fluid_build.cli.workspace import (
    ChangeRequest,
    ChangeRequestStatus,
    ContractStatus,
    ContractVersion,
    TeamMember,
    WorkspaceConfig,
    WorkspaceRole,
)


class TestTeamMemberToDict:
    def test_basic(self):
        now = datetime(2024, 6, 1, 12, 0, 0)
        tm = TeamMember(
            id="u1", name="Alice", email="a@b.com", role=WorkspaceRole.DEVELOPER, joined_at=now
        )
        d = tm.to_dict()
        assert d["id"] == "u1"
        assert d["name"] == "Alice"
        assert d["role"] == "developer"
        assert d["joined_at"] == "2024-06-01T12:00:00"
        assert d["last_active"] is None
        assert d["permissions"] == []

    def test_with_last_active_and_permissions(self):
        now = datetime(2024, 6, 1)
        active = datetime(2024, 6, 15)
        tm = TeamMember(
            id="u2",
            name="Bob",
            email="b@b.com",
            role=WorkspaceRole.ADMIN,
            joined_at=now,
            last_active=active,
            permissions={"read", "write"},
        )
        d = tm.to_dict()
        assert d["last_active"] == "2024-06-15T00:00:00"
        assert set(d["permissions"]) == {"read", "write"}


class TestContractVersionToDict:
    def test_basic(self):
        now = datetime(2024, 1, 1)
        cv = ContractVersion(
            id="v1",
            contract_path="/a/b.yaml",
            version="1.0.0",
            author="alice",
            status=ContractStatus.APPROVED,
            created_at=now,
            message="Initial release",
        )
        d = cv.to_dict()
        assert d["id"] == "v1"
        assert d["status"] == "approved"
        assert d["created_at"] == "2024-01-01T00:00:00"
        assert d["changes"] == []
        assert d["reviewers"] == []
        assert d["approvals"] == []

    def test_with_changes_and_reviewers(self):
        cv = ContractVersion(
            id="v2",
            contract_path="/x.yaml",
            version="2.0.0",
            author="bob",
            status=ContractStatus.IN_REVIEW,
            created_at=datetime(2024, 3, 1),
            message="Update",
            changes=["added expose"],
            reviewers=["alice"],
            approvals=["alice"],
        )
        d = cv.to_dict()
        assert d["changes"] == ["added expose"]
        assert d["reviewers"] == ["alice"]
        assert d["approvals"] == ["alice"]


class TestChangeRequestToDict:
    def test_basic(self):
        now = datetime(2024, 5, 1)
        cr = ChangeRequest(
            id="cr1",
            title="Add table",
            description="Add new expose",
            author="alice",
            target_contract="/c.yaml",
            status=ChangeRequestStatus.OPEN,
            created_at=now,
            updated_at=now,
            changes={"added": ["expose_x"]},
        )
        d = cr.to_dict()
        assert d["id"] == "cr1"
        assert d["status"] == "open"
        assert d["changes"] == {"added": ["expose_x"]}
        assert d["comments"] == []

    def test_with_comments(self):
        now = datetime(2024, 5, 1)
        cr = ChangeRequest(
            id="cr2",
            title="Fix",
            description="Fix schema",
            author="bob",
            target_contract="/c.yaml",
            status=ChangeRequestStatus.MERGED,
            created_at=now,
            updated_at=now,
            changes={},
            comments=[{"user": "alice", "text": "LGTM"}],
        )
        d = cr.to_dict()
        assert d["status"] == "merged"
        assert len(d["comments"]) == 1


class TestWorkspaceConfigToDict:
    def test_basic(self):
        now = datetime(2024, 1, 1)
        wc = WorkspaceConfig(
            name="ws1", description="Test workspace", owner="alice", created_at=now
        )
        d = wc.to_dict()
        assert d["name"] == "ws1"
        assert d["owner"] == "alice"
        assert d["settings"] == {}
        assert d["integrations"] == {}

    def test_with_settings(self):
        now = datetime(2024, 1, 1)
        wc = WorkspaceConfig(
            name="ws2",
            description="With settings",
            owner="bob",
            created_at=now,
            settings={"auto_validate": True},
            integrations={"slack": {"webhook": "https://example.com/hook"}},
        )
        d = wc.to_dict()
        assert d["settings"]["auto_validate"] is True
        assert "slack" in d["integrations"]


class TestWorkspaceEnums:
    def test_workspace_role_values(self):
        assert WorkspaceRole.OWNER.value == "owner"
        assert WorkspaceRole.VIEWER.value == "viewer"

    def test_contract_status_values(self):
        assert ContractStatus.DRAFT.value == "draft"
        assert ContractStatus.DEPLOYED.value == "deployed"
        assert ContractStatus.ARCHIVED.value == "archived"

    def test_change_request_status_values(self):
        assert ChangeRequestStatus.OPEN.value == "open"
        assert ChangeRequestStatus.MERGED.value == "merged"
