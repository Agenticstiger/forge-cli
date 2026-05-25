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

# ruff: noqa: F821 — this helper resolves host-module symbols at
# call-time via a _host() indirection accessor; ruff cannot statically
# see those bindings.
"""Per-provider auth implementations — physical extraction from ``auth.py``.

Houses the 5 ``AuthProvider`` subclasses
(``GoogleCloudAuthProvider`` / ``AWSAuthProvider`` /
``AzureAuthProvider`` / ``SnowflakeAuthProvider`` /
``DatabricksAuthProvider``) — ~1,400 LOC of credential-resolution +
service-detection logic that used to live inline in ``auth.py``.

The base :class:`AuthProvider` + :class:`AuthResult` + :class:`AuthStatus`
stay in ``auth.py`` (all 5 subclasses inherit from it). The
:class:`AuthManager`, the CLI ``register`` / ``run`` entry points, and
the doctor checks also stay there — only the per-provider class
bodies moved.

Test patches that target ``fluid_build.cli.auth.<ProviderClass>``
still resolve because ``auth`` re-imports each class from this module
at top level — the symbol is bound on both namespaces.
"""

from __future__ import annotations

# Stdlib + third-party imports the provider class bodies depend on.
import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

# ``_auth`` indirection — moved class bodies reference
# ``RICH_AVAILABLE`` (and a few other module-level toggles) on the
# original ``cli.auth`` namespace. Tests routinely
# ``patch("fluid_build.cli.auth.RICH_AVAILABLE", False)`` to exercise
# the no-rich code path; without indirection, those patches don't
# reach the moved class bodies. We resolve via ``_auth.RICH_AVAILABLE``
# at call time so every patch flows through.
from fluid_build.cli import auth as _auth  # noqa: E402
from fluid_build.cli.auth import (  # noqa: E402,F401
    AuthProvider,
    AuthResult,
    AuthStatus,
    _sanitize_argv,
)
from fluid_build.cli.console import cprint, success  # noqa: E402,F401
from fluid_build.observability.secret_redactor import redact_secret_text  # noqa: E402

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.prompt import Confirm
    from rich.table import Table

except ImportError:  # pragma: no cover
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Confirm = None  # type: ignore[assignment]
    Progress = None  # type: ignore[assignment]
    SpinnerColumn = None  # type: ignore[assignment]
    TextColumn = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]


def _rich_available() -> bool:
    """Read ``RICH_AVAILABLE`` from the canonical ``cli.auth`` module
    so test patches on ``fluid_build.cli.auth.RICH_AVAILABLE`` flow
    through to the moved provider class bodies."""
    return getattr(_auth, "RICH_AVAILABLE", False)


class GoogleCloudAuthProvider(AuthProvider):
    """Google Cloud Platform authentication provider"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        super().__init__("google_cloud", config, logger)
        self.project_id = config.get("project_id")
        self.scopes = config.get(
            "scopes",
            [
                "https://www.googleapis.com/auth/cloud-platform",
                "https://www.googleapis.com/auth/bigquery",
                "https://www.googleapis.com/auth/datacatalog",
            ],
        )

    @staticmethod
    def _has_sdk() -> bool:
        try:
            import google.auth  # noqa: F401

            return True
        except ImportError:
            return False

    def _validate_via_sdk(self, credentials_path: Optional[str] = None) -> AuthResult:
        """Validate GCP credentials via the google-auth SDK (no CLI needed)."""
        try:
            import google.auth
            import google.auth.transport.requests

            if credentials_path:
                # Use from_service_account_file when given an explicit path,
                # avoiding mutation of os.environ["GOOGLE_APPLICATION_CREDENTIALS"].
                from google.oauth2 import service_account as _sa

                credentials = _sa.Credentials.from_service_account_file(
                    credentials_path, scopes=self.scopes
                )
                project = None
                try:
                    with open(credentials_path) as _f:
                        project = json.load(_f).get("project_id")
                except Exception:
                    pass
            else:
                credentials, project = google.auth.default(scopes=self.scopes)

            credentials.refresh(google.auth.transport.requests.Request())

            email = getattr(credentials, "service_account_email", None)
            return AuthResult(
                provider=self.name,
                status=AuthStatus.AUTHENTICATED,
                user_info={
                    "account": email or "authenticated",
                    "project": project or self.project_id or "unknown",
                    "credential_type": type(credentials).__name__,
                },
                scopes=self.scopes,
            )
        except Exception as e:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message=str(e),
            )

    def _login_via_cli(self) -> AuthResult:
        """Run the interactive gcloud CLI login flow."""
        if self.console and _rich_available():
            self.console.print(
                Panel.fit(
                    "[bold blue]🔐 Google Cloud Authentication[/bold blue]\n\n"
                    "This will open your web browser to complete authentication.\n"
                    f"Project: [cyan]{self.project_id or 'Not specified'}[/cyan]",
                    border_style="blue",
                )
            )
            if not Confirm.ask("\nProceed with authentication?", default=True):
                return AuthResult(
                    provider=self.name,
                    status=AuthStatus.NOT_AUTHENTICATED,
                    error_message="User cancelled authentication",
                )
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self.console,
            ) as progress:
                task = progress.add_task("Configuring application default credentials...", total=1)
                command = ["gcloud", "auth", "application-default", "login"]
                if self.scopes:
                    command.extend(["--scopes", ",".join(self.scopes)])
                if self.project_id:
                    command.extend(["--project", self.project_id])
                self._run_command(command, capture_output=False)
                progress.update(task, completed=1)
        else:
            cprint("🔐 Initiating Google Cloud authentication via gcloud CLI...")
            command = ["gcloud", "auth", "application-default", "login"]
            if self.scopes:
                command.extend(["--scopes", ",".join(self.scopes)])
            if self.project_id:
                command.extend(["--project", self.project_id])
            self._run_command(command, capture_output=False)

        result = self._validate_via_sdk()
        if result.status == AuthStatus.AUTHENTICATED:
            return self._annotate_method(result, "gcloud CLI")
        return result

    def _prompt_for_credentials(self) -> AuthResult:
        """Guide the user to provide a service account key file path."""
        msg = "  Path to service account key JSON: "
        if _rich_available():
            path = Prompt.ask("\n  Path to service account key JSON")
        else:
            path = input(f"\n{msg}")

        path = os.path.expanduser(path.strip())
        if not os.path.isfile(path):
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message=f"File not found: {path}",
            )

        # Quick sanity: must be valid JSON with required fields
        try:
            with open(path) as f:
                data = json.load(f)
            if "type" not in data:
                return AuthResult(
                    provider=self.name,
                    status=AuthStatus.ERROR,
                    error_message=f"Invalid key file (missing 'type' field): {path}",
                )
        except json.JSONDecodeError:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message=f"File is not valid JSON: {path}",
            )

        result = self._validate_via_sdk(credentials_path=path)
        if result.status == AuthStatus.AUTHENTICATED:
            result = self._annotate_method(result, "service account key")
            self._offer_save_to_keyring({"service_account_key_path": path})
        return result

    async def login(self, **kwargs) -> AuthResult:
        """Smart login: CLI → env var → keyring → ADC → guided prompt."""
        import shutil

        try:
            has_cli = bool(shutil.which("gcloud"))
            has_sdk = self._has_sdk()
            env_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            keyring_path = self._get_keyring_credential("service_account_key_path")
            interactive = self._is_interactive()

            if interactive:
                self._show_methods_panel(
                    [
                        ("gcloud CLI", has_cli, "installed" if has_cli else "not installed"),
                        ("GOOGLE_APPLICATION_CREDENTIALS", bool(env_creds), env_creds or "not set"),
                        (
                            "Saved credentials",
                            bool(keyring_path),
                            "found" if keyring_path else "none",
                        ),
                        ("Manual setup", interactive, "available"),
                    ]
                )

            # 1. CLI (interactive only — opens browser)
            if has_cli and interactive:
                try:
                    return self._login_via_cli()
                except Exception:
                    self.logger.debug("gcloud CLI login failed, trying next method")

            # 2. Env var + SDK
            if env_creds and has_sdk:
                result = self._validate_via_sdk(env_creds)
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "GOOGLE_APPLICATION_CREDENTIALS")

            # 3. Keyring + SDK
            if keyring_path and has_sdk:
                result = self._validate_via_sdk(keyring_path)
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "saved service account key")

            # 4. ADC default chain (covers WIF, compute engine metadata, etc.)
            if has_sdk:
                result = self._validate_via_sdk()
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "Application Default Credentials")

            # 5. Interactive prompt
            if interactive and has_sdk:
                return self._prompt_for_credentials()

            # 6. Nothing worked
            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message=(
                    "No GCP credentials found. Options:\n"
                    "  • Install gcloud: https://cloud.google.com/sdk/docs/install\n"
                    "  • Set GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json\n"
                    "  • Run interactively: fluid auth login gcp"
                ),
            )
        except Exception as e:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message=f"Google Cloud authentication failed: {redact_secret_text(str(e))}",
            )

    async def logout(self) -> bool:
        """Logout from Google Cloud"""
        import shutil

        try:
            if shutil.which("gcloud"):
                try:
                    self._run_command(
                        ["gcloud", "auth", "application-default", "revoke"], check=False
                    )
                except Exception:
                    pass
                try:
                    self._run_command(["gcloud", "auth", "revoke", "--all"], check=False)
                except Exception:
                    pass

            if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
                del os.environ["GOOGLE_APPLICATION_CREDENTIALS"]

            self.logger.info("Google Cloud logout completed")
            return True
        except Exception as e:
            self.logger.error("Google Cloud logout failed: %s", redact_secret_text(str(e)))
            return False

    async def check_auth(self) -> AuthResult:
        """Check GCP auth — tries SDK first, CLI second."""
        import shutil

        try:
            # Try SDK first (works without gcloud)
            if self._has_sdk():
                result = self._validate_via_sdk()
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "SDK")

            # Fallback: gcloud CLI
            if shutil.which("gcloud"):
                try:
                    r = self._run_command(
                        ["gcloud", "auth", "application-default", "print-access-token"],
                        capture_output=True,
                    )
                    if r.returncode == 0:
                        account_r = self._run_command(
                            ["gcloud", "config", "get-value", "account"], capture_output=True
                        )
                        project_r = self._run_command(
                            ["gcloud", "config", "get-value", "project"], capture_output=True
                        )
                        return self._annotate_method(
                            AuthResult(
                                provider=self.name,
                                status=AuthStatus.AUTHENTICATED,
                                user_info={
                                    "account": (
                                        account_r.stdout.strip() if account_r.stdout else "unknown"
                                    ),
                                    "project": (
                                        project_r.stdout.strip()
                                        if project_r.stdout
                                        else self.project_id
                                    ),
                                },
                                scopes=self.scopes,
                            ),
                            "gcloud CLI",
                        )
                except Exception:
                    pass

            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message="No valid GCP credentials found",
            )
        except Exception as e:
            return AuthResult(provider=self.name, status=AuthStatus.ERROR, error_message=str(e))


class AWSAuthProvider(AuthProvider):
    """Amazon Web Services authentication provider"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        super().__init__("aws", config, logger)
        self.region = config.get("region", "us-east-1")
        self.profile = config.get("profile", "default")

    @staticmethod
    def _has_boto3() -> bool:
        try:
            import boto3  # noqa: F401

            return True
        except ImportError:
            return False

    def _validate_via_sdk(
        self,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        session_token: Optional[str] = None,
    ) -> AuthResult:
        """Validate AWS credentials via boto3 STS (no CLI needed)."""
        try:
            import boto3

            kwargs: Dict[str, Any] = {"region_name": self.region}
            if access_key and secret_key:
                kwargs["aws_access_key_id"] = access_key
                kwargs["aws_secret_access_key"] = secret_key
                if session_token:
                    kwargs["aws_session_token"] = session_token

            session = boto3.Session(**kwargs)
            sts = session.client("sts")
            identity = sts.get_caller_identity()

            return AuthResult(
                provider=self.name,
                status=AuthStatus.AUTHENTICATED,
                user_info={
                    "user_id": identity.get("UserId"),
                    "account": identity.get("Account"),
                    "arn": identity.get("Arn"),
                    "region": self.region,
                },
            )
        except Exception as e:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message=str(e),
            )

    def _login_via_cli(self) -> AuthResult:
        """Run the interactive AWS CLI login flow (SSO or configure)."""
        if self.console and _rich_available():
            self.console.print(
                Panel.fit(
                    "[bold blue]🔐 AWS Authentication[/bold blue]\n\n"
                    f"Region: [cyan]{self.region}[/cyan]\n"
                    f"Profile: [cyan]{self.profile}[/cyan]\n\n"
                    "This will initiate AWS SSO login or configure credentials.",
                    border_style="blue",
                )
            )
            if not Confirm.ask("\nProceed with AWS authentication?", default=True):
                return AuthResult(
                    provider=self.name,
                    status=AuthStatus.NOT_AUTHENTICATED,
                    error_message="User cancelled authentication",
                )
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self.console,
            ) as progress:
                task = progress.add_task("Initiating AWS SSO login...", total=1)
                try:
                    self._run_command(
                        ["aws", "sso", "login", "--profile", self.profile], capture_output=False
                    )
                except Exception:
                    self.console.print(
                        "\n[yellow]SSO not configured. Setting up AWS credentials...[/yellow]"
                    )
                    self._run_command(
                        ["aws", "configure", "--profile", self.profile], capture_output=False
                    )
                progress.update(task, completed=1)
        else:
            cprint("🔐 Initiating AWS authentication via CLI...")
            try:
                self._run_command(
                    ["aws", "sso", "login", "--profile", self.profile], capture_output=False
                )
            except Exception:
                cprint("SSO not configured. Setting up AWS credentials...")
                self._run_command(
                    ["aws", "configure", "--profile", self.profile], capture_output=False
                )

        result = self._validate_via_sdk()
        if result.status == AuthStatus.AUTHENTICATED:
            return self._annotate_method(result, "AWS CLI")
        return result

    def _prompt_for_credentials(self) -> AuthResult:
        """Guide the user to provide AWS access keys."""
        from getpass import getpass

        if _rich_available():
            access_key = Prompt.ask("\n  AWS Access Key ID")
        else:
            access_key = input("\n  AWS Access Key ID: ")
        secret_key = getpass("  AWS Secret Access Key: ")
        session_token = getpass("  AWS Session Token (optional, press Enter to skip): ")

        access_key = access_key.strip()
        secret_key = secret_key.strip()
        session_token = session_token.strip() or None

        if not access_key or not secret_key:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message="Access Key ID and Secret Access Key are required",
            )

        result = self._validate_via_sdk(access_key, secret_key, session_token)
        if result.status == AuthStatus.AUTHENTICATED:
            result = self._annotate_method(result, "access key")
            creds_to_save = {"access_key_id": access_key, "secret_access_key": secret_key}
            if session_token:
                creds_to_save["session_token"] = session_token
            self._offer_save_to_keyring(creds_to_save)
        return result

    async def login(self, **kwargs) -> AuthResult:
        """Smart login: CLI → env vars → keyring → boto3 default → guided prompt."""
        import shutil

        try:
            has_cli = bool(shutil.which("aws"))
            has_sdk = self._has_boto3()
            env_key = os.environ.get("AWS_ACCESS_KEY_ID")
            env_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
            kr_key = self._get_keyring_credential("access_key_id")
            kr_secret = self._get_keyring_credential("secret_access_key")
            interactive = self._is_interactive()

            if interactive:
                self._show_methods_panel(
                    [
                        ("AWS CLI", has_cli, "installed" if has_cli else "not installed"),
                        ("AWS_ACCESS_KEY_ID", bool(env_key), "set" if env_key else "not set"),
                        ("Saved credentials", bool(kr_key), "found" if kr_key else "none"),
                        ("Manual setup", interactive, "available"),
                    ]
                )

            # 1. CLI (interactive)
            if has_cli and interactive:
                try:
                    return self._login_via_cli()
                except Exception:
                    self.logger.debug("AWS CLI login failed, trying next method")

            # 2. Env vars + SDK
            if env_key and env_secret and has_sdk:
                result = self._validate_via_sdk(
                    env_key, env_secret, os.environ.get("AWS_SESSION_TOKEN")
                )
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "AWS_ACCESS_KEY_ID env var")

            # 3. Keyring + SDK
            if kr_key and kr_secret and has_sdk:
                result = self._validate_via_sdk(
                    kr_key, kr_secret, self._get_keyring_credential("session_token")
                )
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "saved access key")

            # 4. boto3 default chain (~/.aws/credentials, IAM roles, etc.)
            if has_sdk:
                result = self._validate_via_sdk()
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "AWS credential chain")

            # 5. Interactive prompt
            if interactive and has_sdk:
                return self._prompt_for_credentials()

            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message=(
                    "No AWS credentials found. Options:\n"
                    "  • Install AWS CLI: https://aws.amazon.com/cli/\n"
                    "  • Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY\n"
                    "  • Run interactively: fluid auth login aws"
                ),
            )
        except Exception as e:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message=f"AWS authentication failed: {redact_secret_text(str(e))}",
            )

    async def logout(self) -> bool:
        """Logout from AWS"""
        import shutil

        try:
            if shutil.which("aws"):
                try:
                    self._run_command(
                        ["aws", "sso", "logout", "--profile", self.profile], check=False
                    )
                except Exception:
                    pass
            self.logger.info("AWS logout completed")
            return True
        except Exception as e:
            self.logger.error("AWS logout failed: %s", redact_secret_text(str(e)))
            return False

    async def check_auth(self) -> AuthResult:
        """Check AWS auth — tries boto3 SDK first, CLI second."""
        import shutil

        try:
            if self._has_boto3():
                result = self._validate_via_sdk()
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "SDK")

            if shutil.which("aws"):
                try:
                    r = self._run_command(
                        [
                            "aws",
                            "sts",
                            "get-caller-identity",
                            "--profile",
                            self.profile,
                            "--output",
                            "json",
                        ],
                        capture_output=True,
                    )
                    if r.returncode == 0:
                        identity = json.loads(r.stdout)
                        return self._annotate_method(
                            AuthResult(
                                provider=self.name,
                                status=AuthStatus.AUTHENTICATED,
                                user_info={
                                    "user_id": identity.get("UserId"),
                                    "account": identity.get("Account"),
                                    "arn": identity.get("Arn"),
                                    "profile": self.profile,
                                    "region": self.region,
                                },
                            ),
                            "AWS CLI",
                        )
                except Exception:
                    pass

            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message="No valid AWS credentials found",
            )
        except Exception as e:
            return AuthResult(provider=self.name, status=AuthStatus.ERROR, error_message=str(e))


# AzureAuthProvider — physically extracted to
# ``cli/_auth_provider_azure.py`` (~280 LOC). Re-exported here so
# existing call sites and test patches keep resolving.
from fluid_build.cli._auth_provider_azure import AzureAuthProvider  # noqa: E402,F401


class SnowflakeAuthProvider(AuthProvider):
    """Snowflake authentication provider"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        super().__init__("snowflake", config, logger)
        self.account = config.get("account")
        self.user = config.get("user")
        self.warehouse = config.get("warehouse")
        self.database = config.get("database")
        self.schema = config.get("schema")
        self.role = config.get("role")
        self.authenticator = config.get("authenticator")
        self.password = config.get("password")
        self.private_key_path = config.get("private_key_path")
        self.private_key_passphrase = config.get("private_key_passphrase")
        self.oauth_token = config.get("oauth_token")

    def _resolve_settings(self) -> Dict[str, Any]:
        from fluid_build.providers.snowflake.util.config import resolve_snowflake_settings

        return resolve_snowflake_settings(
            account=self.account,
            user=self.user,
            warehouse=self.warehouse,
            database=self.database,
            schema=self.schema,
            role=self.role,
            authenticator=self.authenticator,
            password=self.password,
            private_key_path=self.private_key_path,
            private_key_passphrase=self.private_key_passphrase,
            oauth_token=self.oauth_token,
        )

    @staticmethod
    def _has_connector_auth(settings: Dict[str, Any]) -> bool:
        return any(
            settings.get(key)
            for key in ["password", "private_key_path", "oauth_token", "authenticator"]
        )

    def _check_auth_with_connector(self, settings: Dict[str, Any]) -> AuthResult:
        from fluid_build.providers.snowflake.connection import SnowflakeConnection
        from fluid_build.providers.snowflake.util.config import get_connection_params

        params = get_connection_params(
            account=settings.get("account"),
            warehouse=settings.get("warehouse"),
            database=settings.get("database"),
            schema=settings.get("schema"),
            user=settings.get("user"),
            role=settings.get("role"),
            authenticator=settings.get("authenticator"),
            password=settings.get("password"),
            private_key_path=settings.get("private_key_path"),
            private_key_passphrase=settings.get("private_key_passphrase"),
            oauth_token=settings.get("oauth_token"),
        )

        with SnowflakeConnection(**params) as conn:
            rows = conn.execute(
                "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA()"
            )
            current_user, current_role, current_warehouse, current_database, current_schema = (
                rows[0] if rows else ("unknown", "unknown", "unknown", "unknown", "unknown")
            )

        return AuthResult(
            provider=self.name,
            status=AuthStatus.AUTHENTICATED,
            user_info={
                "account": settings.get("account"),
                "user": current_user or settings.get("user"),
                "warehouse": current_warehouse or settings.get("warehouse"),
                "database": current_database or settings.get("database"),
                "schema": current_schema or settings.get("schema"),
                "role": current_role or settings.get("role"),
                "authenticator": settings.get("authenticator") or "password",
            },
        )

    def _login_with_snowsql(self) -> AuthResult:
        """Fallback interactive SnowSQL login for SSO/browser-first setups."""
        # Check if SnowSQL is installed
        try:
            self._run_command(["snowsql", "--version"], capture_output=True)
        except CLIError:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message="SnowSQL CLI not installed. Please install SnowSQL from Snowflake.",
            )

        if self.console:
            self.console.print(
                Panel(
                    f"🏔️ Snowflake Authentication\n\n"
                    f"Account: [cyan]{self.account or 'Not specified'}[/cyan]\n"
                    f"User: [cyan]{self.user or 'Not specified'}[/cyan]\n"
                    f"Warehouse: [cyan]{self.warehouse or 'Not specified'}[/cyan]\n\n"
                    "This will prompt for your Snowflake credentials.",
                    border_style="blue",
                )
            )

            if not Confirm.ask("\nProceed with Snowflake authentication?", default=True):
                return AuthResult(
                    provider=self.name,
                    status=AuthStatus.NOT_AUTHENTICATED,
                    error_message="User cancelled authentication",
                )

        connection_params = []
        if self.account:
            connection_params.extend(["-a", self.account])
        if self.user:
            connection_params.extend(["-u", self.user])
        if self.warehouse:
            connection_params.extend(["-w", self.warehouse])
        if self.database:
            connection_params.extend(["-d", self.database])
        if self.role:
            connection_params.extend(["-r", self.role])

        if self.console:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self.console,
            ) as progress:
                task = progress.add_task("Connecting to Snowflake...", total=1)
                command = ["snowsql"] + connection_params + ["-q", "SELECT CURRENT_USER();"]
                result = self._run_command(command, capture_output=True, check=False)
                progress.update(task, completed=1)
        else:
            command = ["snowsql"] + connection_params + ["-q", "SELECT CURRENT_USER();"]
            result = self._run_command(command, capture_output=True, check=False)

        if result.returncode == 0:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.AUTHENTICATED,
                user_info={
                    "account": self.account,
                    "user": self.user,
                    "warehouse": self.warehouse,
                    "database": self.database,
                    "role": self.role,
                    "cli_version": "installed",
                },
            )

        error_msg = (
            redact_secret_text(result.stderr.strip()) if result.stderr else "Authentication failed"
        )
        return AuthResult(
            provider=self.name, status=AuthStatus.NOT_AUTHENTICATED, error_message=error_msg
        )

    def _check_auth_with_snowsql(self) -> AuthResult:
        """Fallback status check using SnowSQL when no connector auth is configured."""
        try:
            self._run_command(["snowsql", "--version"], capture_output=True)
        except CLIError:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message="SnowSQL CLI not installed. Please install SnowSQL from Snowflake.",
            )

        if self.account and self.user:
            connection_params = ["-a", self.account, "-u", self.user]
            if self.warehouse:
                connection_params.extend(["-w", self.warehouse])
            if self.database:
                connection_params.extend(["-d", self.database])
            if self.role:
                connection_params.extend(["-r", self.role])

            command = ["snowsql"] + connection_params + ["-q", "SELECT CURRENT_USER();"]
            result = self._run_command(command, capture_output=True, check=False)

            if result.returncode == 0:
                return AuthResult(
                    provider=self.name,
                    status=AuthStatus.AUTHENTICATED,
                    user_info={
                        "account": self.account,
                        "user": self.user,
                        "warehouse": self.warehouse,
                        "database": self.database,
                        "role": self.role,
                    },
                )

            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message="Snowflake credentials not configured or invalid",
            )

        return AuthResult(
            provider=self.name,
            status=AuthStatus.NOT_AUTHENTICATED,
            error_message="Snowflake account and user not configured",
        )

    async def login(self, **kwargs) -> AuthResult:
        """Validate Snowflake authentication using the same connector path as the provider."""
        try:
            # Check if at least one auth method is available
            import shutil

            try:
                import snowflake.connector  # noqa: F401

                _has_connector = True
            except ImportError:
                _has_connector = False

            _has_snowsql = bool(shutil.which("snowsql"))

            if not _has_connector and not _has_snowsql:
                return AuthResult(
                    provider=self.name,
                    status=AuthStatus.ERROR,
                    error_message=(
                        "No Snowflake auth method available. Install one of:\n"
                        "  pip install snowflake-connector-python   (recommended)\n"
                        "  https://docs.snowflake.com/en/user-guide/snowsql-install-config  (SnowSQL CLI)"
                    ),
                )

            settings = self._resolve_settings()
            self.account = settings.get("account")
            self.user = settings.get("user")
            self.warehouse = settings.get("warehouse")
            self.database = settings.get("database")
            self.schema = settings.get("schema")
            self.role = settings.get("role")
            missing = [key for key in ["account", "user"] if not settings.get(key)]
            if missing and self._has_connector_auth(settings):
                return AuthResult(
                    provider=self.name,
                    status=AuthStatus.NOT_AUTHENTICATED,
                    error_message=(
                        "Snowflake connection is missing required settings: "
                        + ", ".join(missing)
                        + ". Set them in the contract binding, credential store, or SNOWFLAKE_* env vars."
                    ),
                )

            if self._has_connector_auth(settings):
                return self._check_auth_with_connector(settings)

            return self._login_with_snowsql()
        except Exception as e:
            return AuthResult(provider=self.name, status=AuthStatus.ERROR, error_message=str(e))

    async def logout(self) -> bool:
        """Logout from Snowflake (clear stored credentials)"""
        # Snowflake doesn't maintain persistent sessions like cloud providers
        # But we can clear any stored connection info
        self.logger.info("Snowflake logout completed (session-based authentication)")
        return True

    async def check_auth(self) -> AuthResult:
        """Check Snowflake authentication status"""
        try:
            settings = self._resolve_settings()
            self.account = settings.get("account")
            self.user = settings.get("user")
            self.warehouse = settings.get("warehouse")
            self.database = settings.get("database")
            self.schema = settings.get("schema")
            self.role = settings.get("role")

            if not (settings.get("account") and settings.get("user")) and self._has_connector_auth(
                settings
            ):
                return AuthResult(
                    provider=self.name,
                    status=AuthStatus.NOT_AUTHENTICATED,
                    error_message="Snowflake account and user not configured",
                )

            if self._has_connector_auth(settings):
                return self._check_auth_with_connector(settings)

            return self._check_auth_with_snowsql()
        except Exception as e:
            return AuthResult(provider=self.name, status=AuthStatus.ERROR, error_message=str(e))


class DatabricksAuthProvider(AuthProvider):
    """Databricks authentication provider"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        super().__init__("databricks", config, logger)
        self.host = config.get("host")
        self.token = config.get("token")
        self.cluster_id = config.get("cluster_id")
        self.workspace_id = config.get("workspace_id")

    def _validate_via_api(self, host: str, token: str) -> AuthResult:
        """Validate Databricks credentials via REST API (no CLI needed)."""
        # SSRF guard — host is operator config and the request carries
        # a Databricks Bearer token. allow_private=True for self-hosted
        # / VPC workspaces; require_https=True so the token never rides
        # an http:// connection. The hook still scheme-validates and
        # DNS-pins the connection.
        from fluid_build.util.safe_http import safe_httpx_client

        try:
            with safe_httpx_client(
                base_url=host,
                timeout=15.0,
                allow_private=True,
                require_https=True,
            ) as client:
                resp = client.get(
                    "/api/2.0/preview/scim/v2/Me",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
            data = resp.json()
            return AuthResult(
                provider=self.name,
                status=AuthStatus.AUTHENTICATED,
                user_info={
                    "user_name": data.get("userName"),
                    "display_name": data.get("displayName"),
                    "host": host,
                    "workspace_id": self.workspace_id,
                    "cluster_id": self.cluster_id,
                },
            )
        except Exception as e:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message=str(e),
            )

    def _login_via_cli(self) -> AuthResult:
        """Run the interactive Databricks CLI configure flow."""
        if self.console and _rich_available():
            self.console.print(
                Panel(
                    f"🧱 Databricks Authentication\n\n"
                    f"Host: [cyan]{self.host or 'Not specified'}[/cyan]\n\n"
                    "This will configure Databricks CLI authentication.",
                    border_style="blue",
                )
            )
            if not Confirm.ask("\nProceed with Databricks authentication?", default=True):
                return AuthResult(
                    provider=self.name,
                    status=AuthStatus.NOT_AUTHENTICATED,
                    error_message="User cancelled authentication",
                )
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self.console,
            ) as progress:
                task = progress.add_task("Configuring Databricks CLI...", total=1)
                command = ["databricks", "configure", "--token"]
                if self.host:
                    command.extend(["--host", self.host])
                self._run_command(command, capture_output=False, check=False)
                progress.update(task, completed=1)
        else:
            command = ["databricks", "configure", "--token"]
            if self.host:
                command.extend(["--host", self.host])
            self._run_command(command, capture_output=False, check=False)

        # Verify via CLI
        test = self._run_command(
            ["databricks", "workspace", "list", "/"], capture_output=True, check=False
        )
        if test.returncode == 0:
            return self._annotate_method(
                AuthResult(
                    provider=self.name,
                    status=AuthStatus.AUTHENTICATED,
                    user_info={"host": self.host, "workspace_id": self.workspace_id},
                ),
                "Databricks CLI",
            )
        return AuthResult(
            provider=self.name,
            status=AuthStatus.NOT_AUTHENTICATED,
            error_message=(
                redact_secret_text(test.stderr.strip())
                if test.stderr
                else "CLI configuration failed"
            ),
        )

    def _prompt_for_credentials(self) -> AuthResult:
        """Guide the user to provide Databricks host + personal access token."""
        from getpass import getpass

        if _rich_available():
            host = Prompt.ask(
                "\n  Databricks workspace URL (e.g., https://dbc-xxx.cloud.databricks.com)"
            )
        else:
            host = input("\n  Databricks workspace URL: ")
        token = getpass("  Personal Access Token: ")

        host, token = host.strip(), token.strip()
        if not host or not token:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message="Workspace URL and Personal Access Token are required",
            )
        if not host.startswith("https://"):
            host = f"https://{host}"

        result = self._validate_via_api(host, token)
        if result.status == AuthStatus.AUTHENTICATED:
            result = self._annotate_method(result, "personal access token")
            self._offer_save_to_keyring({"host": host, "token": token})
        return result

    async def login(self, **kwargs) -> AuthResult:
        """Smart login: CLI → env vars → keyring → config file → guided prompt."""
        import shutil

        try:
            has_cli = bool(shutil.which("databricks"))
            env_host = os.environ.get("DATABRICKS_HOST") or self.host
            env_token = os.environ.get("DATABRICKS_TOKEN") or self.token
            kr_host = self._get_keyring_credential("host")
            kr_token = self._get_keyring_credential("token")
            interactive = self._is_interactive()

            if interactive:
                self._show_methods_panel(
                    [
                        ("Databricks CLI", has_cli, "installed" if has_cli else "not installed"),
                        ("DATABRICKS_HOST", bool(env_host), env_host or "not set"),
                        ("Saved credentials", bool(kr_host), "found" if kr_host else "none"),
                        ("Manual setup", interactive, "available"),
                    ]
                )

            # 1. CLI (interactive)
            if has_cli and interactive:
                try:
                    return self._login_via_cli()
                except Exception:
                    self.logger.debug("Databricks CLI login failed, trying next method")

            # 2. Env vars + REST API
            if env_host and env_token:
                result = self._validate_via_api(env_host, env_token)
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "DATABRICKS_TOKEN env var")

            # 3. Keyring + REST API
            if kr_host and kr_token:
                result = self._validate_via_api(kr_host, kr_token)
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "saved access token")

            # 4. ~/.databrickscfg file
            cfg_path = os.path.expanduser("~/.databrickscfg")
            if os.path.exists(cfg_path):
                try:
                    import configparser

                    cfg = configparser.ConfigParser()
                    cfg.read(cfg_path)
                    cfg_host = cfg.get("DEFAULT", "host", fallback=None)
                    cfg_token = cfg.get("DEFAULT", "token", fallback=None)
                    if cfg_host and cfg_token:
                        result = self._validate_via_api(cfg_host, cfg_token)
                        if result.status == AuthStatus.AUTHENTICATED:
                            return self._annotate_method(result, "~/.databrickscfg")
                except Exception:
                    pass

            # 5. Interactive prompt
            if interactive:
                return self._prompt_for_credentials()

            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message=(
                    "No Databricks credentials found. Options:\n"
                    "  • Set DATABRICKS_HOST and DATABRICKS_TOKEN\n"
                    "  • Install Databricks CLI: pip install databricks-cli\n"
                    "  • Run interactively: fluid auth login databricks"
                ),
            )
        except Exception as e:
            return AuthResult(provider=self.name, status=AuthStatus.ERROR, error_message=str(e))

    async def logout(self) -> bool:
        """Logout from Databricks (clear stored configuration)"""
        try:
            config_file = os.path.expanduser("~/.databrickscfg")
            if os.path.exists(config_file):
                os.remove(config_file)
                self.logger.info("Databricks configuration cleared")
            return True
        except Exception as e:
            self.logger.error(
                "Failed to clear Databricks configuration: %s", redact_secret_text(str(e))
            )
            return False

    async def check_auth(self) -> AuthResult:
        """Check Databricks auth — tries REST API first, CLI second."""
        import shutil

        try:
            # Try env/keyring + API first
            host = (
                os.environ.get("DATABRICKS_HOST")
                or self.host
                or self._get_keyring_credential("host")
            )
            token = (
                os.environ.get("DATABRICKS_TOKEN")
                or self.token
                or self._get_keyring_credential("token")
            )
            if host and token:
                result = self._validate_via_api(host, token)
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "REST API")

            # Try ~/.databrickscfg
            cfg_path = os.path.expanduser("~/.databrickscfg")
            if os.path.exists(cfg_path):
                try:
                    import configparser

                    cfg = configparser.ConfigParser()
                    cfg.read(cfg_path)
                    cfg_host = cfg.get("DEFAULT", "host", fallback=None)
                    cfg_token = cfg.get("DEFAULT", "token", fallback=None)
                    if cfg_host and cfg_token:
                        result = self._validate_via_api(cfg_host, cfg_token)
                        if result.status == AuthStatus.AUTHENTICATED:
                            return self._annotate_method(result, "~/.databrickscfg")
                except Exception:
                    pass

            # Fallback: CLI
            if shutil.which("databricks"):
                r = self._run_command(
                    ["databricks", "workspace", "list", "/"], capture_output=True, check=False
                )
                if r.returncode == 0:
                    return self._annotate_method(
                        AuthResult(
                            provider=self.name,
                            status=AuthStatus.AUTHENTICATED,
                            user_info={"host": self.host, "workspace_id": self.workspace_id},
                        ),
                        "Databricks CLI",
                    )

            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message="No valid Databricks credentials found",
            )
        except Exception as e:
            return AuthResult(provider=self.name, status=AuthStatus.ERROR, error_message=str(e))
