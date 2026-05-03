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

"""
FLUID Workspace - Advanced Team Collaboration Features

This module provides comprehensive team collaboration capabilities including
shared workspaces, contract versioning, collaborative development workflows,
and enterprise team management features.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fluid_build.cli.console import cprint

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.table import Table
    from rich.tree import Tree

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

try:
    import git

    GIT_AVAILABLE = True
except ImportError:
    git = None  # type: ignore[assignment]
    GIT_AVAILABLE = False


COMMAND = "workspace"


class WorkspaceRole(Enum):
    """Team member roles"""

    OWNER = "owner"
    ADMIN = "admin"
    DEVELOPER = "developer"
    VIEWER = "viewer"


class ContractStatus(Enum):
    """Contract development status"""

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    DEPLOYED = "deployed"
    ARCHIVED = "archived"


class ChangeRequestStatus(Enum):
    """Change request status"""

    OPEN = "open"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"


@dataclass
class TeamMember:
    """Team member information"""

    id: str
    name: str
    email: str
    role: WorkspaceRole
    joined_at: datetime
    last_active: Optional[datetime] = None
    permissions: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "role": self.role.value,
            "joined_at": self.joined_at.isoformat(),
            "last_active": self.last_active.isoformat() if self.last_active else None,
            "permissions": list(self.permissions),
        }


@dataclass
class ContractVersion:
    """Contract version information"""

    id: str
    contract_path: str
    version: str
    author: str
    status: ContractStatus
    created_at: datetime
    message: str
    changes: List[str] = field(default_factory=list)
    reviewers: List[str] = field(default_factory=list)
    approvals: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "contract_path": self.contract_path,
            "version": self.version,
            "author": self.author,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "message": self.message,
            "changes": self.changes,
            "reviewers": self.reviewers,
            "approvals": self.approvals,
        }


@dataclass
class ChangeRequest:
    """Change request for contract modifications"""

    id: str
    title: str
    description: str
    author: str
    target_contract: str
    status: ChangeRequestStatus
    created_at: datetime
    updated_at: datetime
    changes: Dict[str, Any]
    reviewers: List[str] = field(default_factory=list)
    comments: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "author": self.author,
            "target_contract": self.target_contract,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "changes": self.changes,
            "reviewers": self.reviewers,
            "comments": self.comments,
        }


@dataclass
class WorkspaceConfig:
    """Workspace configuration"""

    name: str
    description: str
    owner: str
    created_at: datetime
    settings: Dict[str, Any] = field(default_factory=dict)
    integrations: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "owner": self.owner,
            "created_at": self.created_at.isoformat(),
            "settings": self.settings,
            "integrations": self.integrations,
        }


class WorkspaceManager:
    """Manages team workspaces and collaboration features"""

    def __init__(self, workspace_dir: Optional[Path] = None):
        self.workspace_dir = workspace_dir or Path.cwd() / ".fluid-workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.workspace_dir / "workspace.db"
        self.console = Console() if RICH_AVAILABLE else None

        self._init_database()
        self._ensure_git_repo()

    def _init_database(self):
        """Initialize workspace database"""
        with sqlite3.connect(self.db_path) as conn:
            # Workspace configuration
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workspace_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    name TEXT NOT NULL,
                    description TEXT,
                    owner TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    settings TEXT,
                    integrations TEXT
                )
            """)

            # Team members
            conn.execute("""
                CREATE TABLE IF NOT EXISTS team_members (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    role TEXT NOT NULL,
                    joined_at TEXT NOT NULL,
                    last_active TEXT,
                    permissions TEXT
                )
            """)

            # Contract versions
            conn.execute("""
                CREATE TABLE IF NOT EXISTS contract_versions (
                    id TEXT PRIMARY KEY,
                    contract_path TEXT NOT NULL,
                    version TEXT NOT NULL,
                    author TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message TEXT,
                    changes TEXT,
                    reviewers TEXT,
                    approvals TEXT
                )
            """)

            # Change requests
            conn.execute("""
                CREATE TABLE IF NOT EXISTS change_requests (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    author TEXT NOT NULL,
                    target_contract TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    changes TEXT,
                    reviewers TEXT,
                    comments TEXT
                )
            """)

            # Activity log
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_log (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT,
                    target_id TEXT,
                    details TEXT
                )
            """)

    def _ensure_git_repo(self):
        """Ensure workspace has git repository for version control"""
        if not GIT_AVAILABLE:
            return
        try:
            if not (self.workspace_dir / ".git").exists():
                repo = git.Repo.init(self.workspace_dir)

                # Create initial gitignore
                gitignore_content = """
# FLUID Workspace
workspace.db
*.log
.DS_Store
.vscode/
.idea/

# Temporary files
*.tmp
*.temp
"""
                with open(self.workspace_dir / ".gitignore", "w") as f:
                    f.write(gitignore_content.strip())

                # Initial commit
                repo.index.add([".gitignore"])
                repo.index.commit("Initialize FLUID workspace")

        except Exception:
            pass  # Git not available or initialization failed

    def initialize_workspace(self, name: str, description: str, owner: str) -> bool:
        """Initialize a new workspace"""
        try:
            config = WorkspaceConfig(
                name=name,
                description=description,
                owner=owner,
                created_at=datetime.now(),
                settings={
                    "auto_backup": True,
                    "require_reviews": True,
                    "approval_threshold": 1,
                    "notification_settings": {"email": True, "slack": False},
                },
            )

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO workspace_config 
                    (id, name, description, owner, created_at, settings, integrations)
                    VALUES (1, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        config.name,
                        config.description,
                        config.owner,
                        config.created_at.isoformat(),
                        json.dumps(config.settings),
                        json.dumps(config.integrations),
                    ),
                )

            # Add owner as admin
            owner_member = TeamMember(
                id=str(uuid.uuid4()),
                name=owner,
                email=f"{owner}@company.com",  # In real implementation, get from auth
                role=WorkspaceRole.OWNER,
                joined_at=datetime.now(),
                permissions={
                    "manage_workspace",
                    "manage_members",
                    "approve_changes",
                    "deploy_contracts",
                },
            )

            self.add_team_member(owner_member)

            self._log_activity(owner, "workspace_created", "workspace", "main", {"name": name})

            return True

        except Exception as e:
            if self.console:
                self.console.print(f"[red]Failed to initialize workspace: {e}[/red]")
            return False

    def get_workspace_config(self) -> Optional[WorkspaceConfig]:
        """Get workspace configuration"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT * FROM workspace_config WHERE id = 1")
            row = cursor.fetchone()

            if row:
                return WorkspaceConfig(
                    name=row[1],
                    description=row[2],
                    owner=row[3],
                    created_at=datetime.fromisoformat(row[4]),
                    settings=json.loads(row[5]) if row[5] else {},
                    integrations=json.loads(row[6]) if row[6] else {},
                )

        return None

    def add_team_member(self, member: TeamMember) -> bool:
        """Add a team member to the workspace"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO team_members 
                    (id, name, email, role, joined_at, last_active, permissions)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        member.id,
                        member.name,
                        member.email,
                        member.role.value,
                        member.joined_at.isoformat(),
                        member.last_active.isoformat() if member.last_active else None,
                        json.dumps(list(member.permissions)),
                    ),
                )

            self._log_activity(
                "system",
                "member_added",
                "member",
                member.id,
                {"name": member.name, "role": member.role.value},
            )
            return True

        except Exception:
            return False

    def get_team_members(self) -> List[TeamMember]:
        """Get all team members"""
        members = []

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT * FROM team_members ORDER BY joined_at")

            for row in cursor.fetchall():
                member = TeamMember(
                    id=row[0],
                    name=row[1],
                    email=row[2],
                    role=WorkspaceRole(row[3]),
                    joined_at=datetime.fromisoformat(row[4]),
                    last_active=datetime.fromisoformat(row[5]) if row[5] else None,
                    permissions=set(json.loads(row[6])) if row[6] else set(),
                )
                members.append(member)

        return members

    def create_contract_version(
        self, contract_path: str, author: str, message: str, changes: List[str]
    ) -> str:
        """Create a new contract version"""
        version_id = str(uuid.uuid4())

        # Generate version number
        existing_versions = self.get_contract_versions(contract_path)
        version_num = f"v1.{len(existing_versions) + 1}.0"

        version = ContractVersion(
            id=version_id,
            contract_path=contract_path,
            version=version_num,
            author=author,
            status=ContractStatus.DRAFT,
            created_at=datetime.now(),
            message=message,
            changes=changes,
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO contract_versions 
                (id, contract_path, version, author, status, created_at, message, changes, reviewers, approvals)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    version.id,
                    version.contract_path,
                    version.version,
                    version.author,
                    version.status.value,
                    version.created_at.isoformat(),
                    version.message,
                    json.dumps(version.changes),
                    json.dumps(version.reviewers),
                    json.dumps(version.approvals),
                ),
            )

        self._log_activity(
            author,
            "version_created",
            "contract_version",
            version_id,
            {"contract": contract_path, "version": version_num},
        )

        return version_id

    def get_contract_versions(self, contract_path: Optional[str] = None) -> List[ContractVersion]:
        """Get contract versions"""
        versions = []

        with sqlite3.connect(self.db_path) as conn:
            if contract_path:
                cursor = conn.execute(
                    "SELECT * FROM contract_versions WHERE contract_path = ? ORDER BY created_at DESC",
                    (contract_path,),
                )
            else:
                cursor = conn.execute("SELECT * FROM contract_versions ORDER BY created_at DESC")

            for row in cursor.fetchall():
                version = ContractVersion(
                    id=row[0],
                    contract_path=row[1],
                    version=row[2],
                    author=row[3],
                    status=ContractStatus(row[4]),
                    created_at=datetime.fromisoformat(row[5]),
                    message=row[6] or "",
                    changes=json.loads(row[7]) if row[7] else [],
                    reviewers=json.loads(row[8]) if row[8] else [],
                    approvals=json.loads(row[9]) if row[9] else [],
                )
                versions.append(version)

        return versions

    def create_change_request(
        self,
        title: str,
        description: str,
        author: str,
        target_contract: str,
        changes: Dict[str, Any],
    ) -> str:
        """Create a new change request"""
        request_id = str(uuid.uuid4())

        change_request = ChangeRequest(
            id=request_id,
            title=title,
            description=description,
            author=author,
            target_contract=target_contract,
            status=ChangeRequestStatus.OPEN,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            changes=changes,
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO change_requests 
                (id, title, description, author, target_contract, status, created_at, updated_at, changes, reviewers, comments)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    change_request.id,
                    change_request.title,
                    change_request.description,
                    change_request.author,
                    change_request.target_contract,
                    change_request.status.value,
                    change_request.created_at.isoformat(),
                    change_request.updated_at.isoformat(),
                    json.dumps(change_request.changes),
                    json.dumps(change_request.reviewers),
                    json.dumps(change_request.comments),
                ),
            )

        self._log_activity(
            author,
            "change_request_created",
            "change_request",
            request_id,
            {"title": title, "contract": target_contract},
        )

        return request_id

    def get_change_requests(
        self, status: Optional[ChangeRequestStatus] = None
    ) -> List[ChangeRequest]:
        """Get change requests"""
        requests = []

        with sqlite3.connect(self.db_path) as conn:
            if status:
                cursor = conn.execute(
                    "SELECT * FROM change_requests WHERE status = ? ORDER BY updated_at DESC",
                    (status.value,),
                )
            else:
                cursor = conn.execute("SELECT * FROM change_requests ORDER BY updated_at DESC")

            for row in cursor.fetchall():
                request = ChangeRequest(
                    id=row[0],
                    title=row[1],
                    description=row[2] or "",
                    author=row[3],
                    target_contract=row[4],
                    status=ChangeRequestStatus(row[5]),
                    created_at=datetime.fromisoformat(row[6]),
                    updated_at=datetime.fromisoformat(row[7]),
                    changes=json.loads(row[8]) if row[8] else {},
                    reviewers=json.loads(row[9]) if row[9] else [],
                    comments=json.loads(row[10]) if row[10] else [],
                )
                requests.append(request)

        return requests

    def approve_change_request(self, request_id: str, approver: str) -> bool:
        """Approve a change request"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Get current request
                cursor = conn.execute(
                    "SELECT status, reviewers, comments FROM change_requests WHERE id = ?",
                    (request_id,),
                )
                row = cursor.fetchone()

                if not row:
                    return False

                current_status = ChangeRequestStatus(row[0])
                if current_status != ChangeRequestStatus.IN_REVIEW:
                    return False

                json.loads(row[1]) if row[1] else []
                comments = json.loads(row[2]) if row[2] else []

                # Add approval comment
                approval_comment = {
                    "id": str(uuid.uuid4()),
                    "author": approver,
                    "timestamp": datetime.now().isoformat(),
                    "type": "approval",
                    "content": f"Approved by {approver}",
                }
                comments.append(approval_comment)

                # Update status to approved
                conn.execute(
                    """
                    UPDATE change_requests 
                    SET status = ?, updated_at = ?, comments = ?
                    WHERE id = ?
                """,
                    (
                        ChangeRequestStatus.APPROVED.value,
                        datetime.now().isoformat(),
                        json.dumps(comments),
                        request_id,
                    ),
                )

            self._log_activity(
                approver, "change_request_approved", "change_request", request_id, {}
            )
            return True

        except Exception:
            return False

    def _log_activity(
        self,
        user_id: str,
        action: str,
        target_type: Optional[str],
        target_id: Optional[str],
        details: Dict[str, Any],
    ):
        """Log workspace activity"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO activity_log 
                    (id, timestamp, user_id, action, target_type, target_id, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        str(uuid.uuid4()),
                        datetime.now().isoformat(),
                        user_id,
                        action,
                        target_type,
                        target_id,
                        json.dumps(details),
                    ),
                )
        except Exception:
            pass  # Silent failure for activity logging

    def get_activity_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent workspace activity"""
        activities = []

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT timestamp, user_id, action, target_type, target_id, details 
                FROM activity_log 
                ORDER BY timestamp DESC 
                LIMIT ?
            """,
                (limit,),
            )

            for row in cursor.fetchall():
                activity = {
                    "timestamp": row[0],
                    "user_id": row[1],
                    "action": row[2],
                    "target_type": row[3],
                    "target_id": row[4],
                    "details": json.loads(row[5]) if row[5] else {},
                }
                activities.append(activity)

        return activities


# CLI Integration
def register(subparsers: argparse._SubParsersAction):
    """Register the workspace command"""
    p = subparsers.add_parser(COMMAND, help="🤝 Team workspace and collaboration features")

    workspace_subparsers = p.add_subparsers(dest="workspace_action", help="Workspace actions")

    # Initialize workspace
    init_parser = workspace_subparsers.add_parser("init", help="Initialize a new workspace")
    init_parser.add_argument("name", help="Workspace name")
    init_parser.add_argument("--description", help="Workspace description")
    init_parser.add_argument("--owner", help="Workspace owner")

    # Show workspace info
    _info_parser = workspace_subparsers.add_parser(
        "info", help="Show workspace information"
    )  # noqa: F841

    # Team management
    team_parser = workspace_subparsers.add_parser("team", help="Manage team members")
    team_subparsers = team_parser.add_subparsers(dest="team_action")

    _list_team_parser = team_subparsers.add_parser("list", help="List team members")  # noqa: F841
    add_team_parser = team_subparsers.add_parser("add", help="Add team member")
    add_team_parser.add_argument("name", help="Member name")
    add_team_parser.add_argument("email", help="Member email")
    add_team_parser.add_argument(
        "--role", choices=[r.value for r in WorkspaceRole], default="developer", help="Member role"
    )

    # Contract versioning
    version_parser = workspace_subparsers.add_parser("version", help="Manage contract versions")
    version_subparsers = version_parser.add_subparsers(dest="version_action")

    create_version_parser = version_subparsers.add_parser("create", help="Create contract version")
    create_version_parser.add_argument("contract", help="Contract file path")
    create_version_parser.add_argument("--message", required=True, help="Version message")
    create_version_parser.add_argument("--author", help="Version author")

    list_versions_parser = version_subparsers.add_parser("list", help="List contract versions")
    list_versions_parser.add_argument("--contract", help="Filter by contract path")

    # Change requests
    changes_parser = workspace_subparsers.add_parser("changes", help="Manage change requests")
    changes_subparsers = changes_parser.add_subparsers(dest="changes_action")

    create_change_parser = changes_subparsers.add_parser("create", help="Create change request")
    create_change_parser.add_argument("title", help="Change request title")
    create_change_parser.add_argument("contract", help="Target contract")
    create_change_parser.add_argument("--description", help="Change description")
    create_change_parser.add_argument("--author", help="Change author")

    list_changes_parser = changes_subparsers.add_parser("list", help="List change requests")
    list_changes_parser.add_argument(
        "--status", choices=[s.value for s in ChangeRequestStatus], help="Filter by status"
    )

    approve_change_parser = changes_subparsers.add_parser("approve", help="Approve change request")
    approve_change_parser.add_argument("request_id", help="Change request ID")
    approve_change_parser.add_argument("--approver", help="Approver name")

    # Activity log
    activity_parser = workspace_subparsers.add_parser("activity", help="Show workspace activity")
    activity_parser.add_argument(
        "--limit", type=int, default=20, help="Number of activities to show"
    )

    p.set_defaults(func=run)


def run(args, logger: logging.Logger) -> int:
    """Main entry point for workspace command"""
    try:
        workspace_manager = WorkspaceManager()

        if args.workspace_action == "init":
            return handle_init_workspace(args, workspace_manager, logger)
        elif args.workspace_action == "info":
            return handle_workspace_info(args, workspace_manager, logger)
        elif args.workspace_action == "team":
            return handle_team_management(args, workspace_manager, logger)
        elif args.workspace_action == "version":
            return handle_version_management(args, workspace_manager, logger)
        elif args.workspace_action == "changes":
            return handle_change_management(args, workspace_manager, logger)
        elif args.workspace_action == "activity":
            return handle_activity_log(args, workspace_manager, logger)
        else:
            if RICH_AVAILABLE:
                console = Console()
                console.print(
                    "[red]❌ Unknown workspace action. Use 'fluid workspace --help' for available options.[/red]"
                )
            return 1

    except Exception as e:
        logger.exception("Workspace command failed")
        if RICH_AVAILABLE:
            console = Console()
            console.print(f"[red]❌ Workspace command failed: {e}[/red]")
        return 1


def handle_init_workspace(args, workspace_manager: WorkspaceManager, logger: logging.Logger) -> int:
    """Handle workspace initialization"""
    if not RICH_AVAILABLE:
        cprint("Workspace functionality requires rich library")
        return 1

    console = Console()

    name = args.name
    description = args.description or f"FLUID workspace for {name}"
    owner = args.owner or "admin"

    success = workspace_manager.initialize_workspace(name, description, owner)

    if success:
        console.print(f"[green]✅ Workspace '{name}' initialized successfully[/green]")
        console.print(f"[dim]Owner: {owner}[/dim]")
        console.print(f"[dim]Location: {workspace_manager.workspace_dir}[/dim]")
        return 0
    else:
        console.print("[red]❌ Failed to initialize workspace[/red]")
        return 1


def handle_workspace_info(args, workspace_manager: WorkspaceManager, logger: logging.Logger) -> int:
    """Handle workspace info display"""
    if not RICH_AVAILABLE:
        cprint("Workspace functionality requires rich library")
        return 1

    console = Console()
    config = workspace_manager.get_workspace_config()

    if not config:
        console.print(
            "[yellow]No workspace found. Use 'fluid workspace init' to create one.[/yellow]"
        )
        return 1

    team_count = len(workspace_manager.get_team_members())
    versions_count = len(workspace_manager.get_contract_versions())
    open_changes = len(workspace_manager.get_change_requests(ChangeRequestStatus.OPEN))

    info_text = f"""
[bold]Name:[/bold] {config.name}
[bold]Description:[/bold] {config.description}
[bold]Owner:[/bold] {config.owner}
[bold]Created:[/bold] {config.created_at.strftime('%Y-%m-%d %H:%M')}

[bold]Team Members:[/bold] {team_count}
[bold]Contract Versions:[/bold] {versions_count}
[bold]Open Change Requests:[/bold] {open_changes}

[bold]Settings:[/bold]
• Auto Backup: {'✅' if config.settings.get('auto_backup') else '❌'}
• Require Reviews: {'✅' if config.settings.get('require_reviews') else '❌'}
• Approval Threshold: {config.settings.get('approval_threshold', 1)}
    """

    console.print(Panel(info_text.strip(), title="Workspace Information", border_style="blue"))
    return 0


def handle_team_management(
    args, workspace_manager: WorkspaceManager, logger: logging.Logger
) -> int:
    """Handle team management actions"""
    if not RICH_AVAILABLE:
        cprint("Team management requires rich library")
        return 1

    console = Console()

    if args.team_action == "list":
        members = workspace_manager.get_team_members()

        if not members:
            console.print("[dim]No team members found[/dim]")
            return 0

        table = Table(title="Team Members")
        table.add_column("Name", style="cyan")
        table.add_column("Email", style="white")
        table.add_column("Role", style="green")
        table.add_column("Joined", style="dim")
        table.add_column("Last Active", style="dim")

        for member in members:
            last_active = member.last_active.strftime("%Y-%m-%d") if member.last_active else "Never"

            table.add_row(
                member.name,
                member.email,
                member.role.value,
                member.joined_at.strftime("%Y-%m-%d"),
                last_active,
            )

        console.print(table)

    elif args.team_action == "add":
        member = TeamMember(
            id=str(uuid.uuid4()),
            name=args.name,
            email=args.email,
            role=WorkspaceRole(args.role),
            joined_at=datetime.now(),
            permissions=(
                {"read_contracts", "create_versions"}
                if args.role == "developer"
                else {"read_contracts"}
            ),
        )

        success = workspace_manager.add_team_member(member)

        if success:
            console.print(f"[green]✅ Added {args.name} to workspace[/green]")
        else:
            console.print("[red]❌ Failed to add team member[/red]")
            return 1

    return 0


def handle_version_management(
    args, workspace_manager: WorkspaceManager, logger: logging.Logger
) -> int:
    """Handle version management actions"""
    if not RICH_AVAILABLE:
        cprint("Version management requires rich library")
        return 1

    console = Console()

    if args.version_action == "create":
        author = args.author or "unknown"
        contract_path = args.contract
        message = args.message

        # In real implementation, analyze contract changes
        changes = [f"Modified {contract_path}"]

        version_id = workspace_manager.create_contract_version(
            contract_path, author, message, changes
        )

        console.print(f"[green]✅ Created version {version_id[:8]} for {contract_path}[/green]")

    elif args.version_action == "list":
        versions = workspace_manager.get_contract_versions(
            args.contract if hasattr(args, "contract") else None
        )

        if not versions:
            console.print("[dim]No contract versions found[/dim]")
            return 0

        table = Table(title="Contract Versions")
        table.add_column("Version", style="cyan")
        table.add_column("Contract", style="white")
        table.add_column("Author", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Created", style="dim")
        table.add_column("Message", style="white")

        for version in versions:
            table.add_row(
                version.version,
                version.contract_path,
                version.author,
                version.status.value,
                version.created_at.strftime("%Y-%m-%d %H:%M"),
                version.message[:50] + "..." if len(version.message) > 50 else version.message,
            )

        console.print(table)

    return 0


def handle_change_management(
    args, workspace_manager: WorkspaceManager, logger: logging.Logger
) -> int:
    """Handle change request management"""
    if not RICH_AVAILABLE:
        cprint("Change management requires rich library")
        return 1

    console = Console()

    if args.changes_action == "create":
        author = args.author or "unknown"
        title = args.title
        description = args.description or ""
        contract = args.contract

        # In real implementation, capture actual changes
        changes = {"description": description, "contract": contract}

        request_id = workspace_manager.create_change_request(
            title, description, author, contract, changes
        )

        console.print(f"[green]✅ Created change request {request_id[:8]}[/green]")

    elif args.changes_action == "list":
        status_filter = (
            ChangeRequestStatus(args.status) if hasattr(args, "status") and args.status else None
        )
        requests = workspace_manager.get_change_requests(status_filter)

        if not requests:
            console.print("[dim]No change requests found[/dim]")
            return 0

        table = Table(title="Change Requests")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="white")
        table.add_column("Author", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Contract", style="blue")
        table.add_column("Created", style="dim")

        for request in requests:
            table.add_row(
                request.id[:8],
                request.title,
                request.author,
                request.status.value,
                request.target_contract,
                request.created_at.strftime("%Y-%m-%d"),
            )

        console.print(table)

    elif args.changes_action == "approve":
        approver = args.approver or "admin"
        request_id = args.request_id

        success = workspace_manager.approve_change_request(request_id, approver)

        if success:
            console.print(f"[green]✅ Approved change request {request_id}[/green]")
        else:
            console.print("[red]❌ Failed to approve change request[/red]")
            return 1

    return 0


def handle_activity_log(args, workspace_manager: WorkspaceManager, logger: logging.Logger) -> int:
    """Handle activity log display"""
    if not RICH_AVAILABLE:
        cprint("Activity log requires rich library")
        return 1

    console = Console()
    activities = workspace_manager.get_activity_log(args.limit)

    if not activities:
        console.print("[dim]No recent activity[/dim]")
        return 0

    table = Table(title="Recent Activity")
    table.add_column("Time", style="dim")
    table.add_column("User", style="cyan")
    table.add_column("Action", style="green")
    table.add_column("Target", style="white")
    table.add_column("Details", style="dim")

    for activity in activities:
        timestamp = datetime.fromisoformat(activity["timestamp"]).strftime("%m/%d %H:%M")
        target = (
            f"{activity['target_type']}: {activity['target_id'][:8]}"
            if activity["target_type"]
            else ""
        )
        details = str(activity["details"]) if activity["details"] else ""

        table.add_row(
            timestamp,
            activity["user_id"],
            activity["action"],
            target,
            details[:30] + "..." if len(details) > 30 else details,
        )

    console.print(table)
    return 0
