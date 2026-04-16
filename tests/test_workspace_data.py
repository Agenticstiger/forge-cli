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

"""Tests for fluid_build.cli.workspace — enums, dataclasses, to_dict() methods."""

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


class TestEnums:
    def test_workspace_role(self):
        assert WorkspaceRole.OWNER.value == "owner"
        assert WorkspaceRole.VIEWER.value == "viewer"
        assert len(WorkspaceRole) == 4

    def test_contract_status(self):
        assert ContractStatus.DRAFT.value == "draft"
        assert ContractStatus.ARCHIVED.value == "archived"
        assert len(ContractStatus) == 5

    def test_change_request_status(self):
        assert ChangeRequestStatus.OPEN.value == "open"
        assert ChangeRequestStatus.MERGED.value == "merged"


class TestTeamMember:
    def test_to_dict_basic(self):
        t = datetime(2024, 1, 1, 12, 0)
        m = TeamMember(
            id="u1", name="Alice", email="a@b.com", role=WorkspaceRole.DEVELOPER, joined_at=t
        )
        d = m.to_dict()
        assert d["id"] == "u1"
        assert d["name"] == "Alice"
        assert d["role"] == "developer"
        assert d["joined_at"] == t.isoformat()
        assert d["last_active"] is None
        assert d["permissions"] == []

    def test_to_dict_with_optional_fields(self):
        t = datetime(2024, 1, 1)
        la = datetime(2024, 6, 15)
        m = TeamMember(
            id="u2",
            name="Bob",
            email="b@c.com",
            role=WorkspaceRole.ADMIN,
            joined_at=t,
            last_active=la,
            permissions={"read", "write"},
        )
        d = m.to_dict()
        assert d["last_active"] == la.isoformat()
        assert set(d["permissions"]) == {"read", "write"}


class TestContractVersion:
    def test_to_dict(self):
        t = datetime(2024, 3, 1)
        cv = ContractVersion(
            id="v1",
            contract_path="c.yaml",
            version="1.0.0",
            author="dev",
            status=ContractStatus.APPROVED,
            created_at=t,
            message="Initial release",
            changes=["Added schema"],
            reviewers=["r1"],
            approvals=["r1"],
        )
        d = cv.to_dict()
        assert d["id"] == "v1"
        assert d["status"] == "approved"
        assert d["changes"] == ["Added schema"]
        assert d["version"] == "1.0.0"


class TestChangeRequest:
    def test_to_dict(self):
        t1 = datetime(2024, 1, 1)
        t2 = datetime(2024, 1, 5)
        cr = ChangeRequest(
            id="cr1",
            title="Add column",
            description="adds email",
            author="dev",
            target_contract="c.yaml",
            status=ChangeRequestStatus.OPEN,
            created_at=t1,
            updated_at=t2,
            changes={"add_columns": ["email"]},
        )
        d = cr.to_dict()
        assert d["id"] == "cr1"
        assert d["status"] == "open"
        assert d["changes"] == {"add_columns": ["email"]}
        assert d["created_at"] == t1.isoformat()


class TestWorkspaceConfig:
    def test_to_dict(self):
        t = datetime(2024, 2, 1)
        wc = WorkspaceConfig(
            name="my-ws",
            description="Workspace",
            owner="admin",
            created_at=t,
            settings={"auto_review": True},
        )
        d = wc.to_dict()
        assert d["name"] == "my-ws"
        assert d["settings"]["auto_review"] is True
        assert d["integrations"] == {}
