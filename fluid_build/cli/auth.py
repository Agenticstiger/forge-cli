#!/usr/bin/env python3
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
FLUID Authentication CLI
Provides unified authentication for various cloud and data platform providers
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint

# Check for optional dependencies
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.prompt import Confirm, Prompt
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

COMMAND = "auth"


class CLIError(Exception):
    """Custom exception for CLI errors"""

    def __init__(self, code: int, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class AuthStatus(Enum):
    """Authentication status"""

    AUTHENTICATED = "authenticated"
    NOT_AUTHENTICATED = "not_authenticated"
    EXPIRED = "expired"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class AuthResult:
    """Authentication result information"""

    provider: str
    status: AuthStatus
    user_info: Dict[str, Any] = field(default_factory=dict)
    credentials_path: Optional[str] = None
    expires_at: Optional[str] = None
    scopes: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    auth_method: Optional[str] = None  # e.g., "gcloud CLI", "service account key", "env var"


class AuthProvider:
    """Base class for authentication providers"""

    def __init__(self, name: str, config: Dict[str, Any], logger: logging.Logger):
        self.name = name
        self.config = config
        self.logger = logger
        self.console = Console() if RICH_AVAILABLE else None

    async def login(self, **kwargs) -> AuthResult:
        """Initiate login flow for this provider"""
        raise NotImplementedError("Subclasses must implement login method")

    async def logout(self) -> bool:
        """Logout from this provider"""
        raise NotImplementedError("Subclasses must implement logout method")

    async def check_auth(self) -> AuthResult:
        """Check current authentication status"""
        raise NotImplementedError("Subclasses must implement check_auth method")

    def _run_command(
        self, command: List[str], capture_output: bool = True, check: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a shell command with proper error handling"""
        try:
            self.logger.debug(f"Running command: {' '.join(command)}")
            result = subprocess.run(command, capture_output=capture_output, text=True, check=check)
            return result
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Command failed: {e}")
            raise
        except FileNotFoundError as e:
            self.logger.error(f"Command not found: {command[0]} - {e}")
            raise CLIError(1, "command_not_found", {"command": command[0]})

    # ── Smart-auth helpers ────────────────────────────────────────────────

    def _is_interactive(self) -> bool:
        """True when running in an interactive terminal (not CI)."""
        return os.isatty(0) and os.isatty(1)

    def _get_keyring_credential(self, key: str) -> Optional[str]:
        """Retrieve a credential previously saved to the OS keyring."""
        try:
            from fluid_build.credentials.keyring_store import KeyringCredentialStore

            return KeyringCredentialStore.get_credential(f"{self.name}.{key}")
        except Exception:
            return None

    def _save_to_keyring(self, key: str, value: str) -> bool:
        """Save a credential to the OS keyring. Returns True on success."""
        try:
            from fluid_build.credentials.keyring_store import KeyringCredentialStore

            KeyringCredentialStore.set_credential(f"{self.name}.{key}", value)
            return True
        except Exception as e:
            self.logger.debug(f"Failed to save to keyring: {e}")
            return False

    def _offer_save_to_keyring(self, credentials: Dict[str, str]) -> None:
        """If interactive, ask the user whether to persist credentials to the OS keyring."""
        if not self._is_interactive():
            return
        try:
            if RICH_AVAILABLE:
                save = Confirm.ask("\n  Save to secure keyring for future use?", default=True)
            else:
                resp = input("\n  Save to secure keyring for future use? (y/n): ")
                save = resp.strip().lower() in ("y", "yes", "")

            if save:
                ok = True
                for k, v in credentials.items():
                    if not self._save_to_keyring(k, v):
                        ok = False
                if ok:
                    msg = "  ✅ Credentials saved — next login will be automatic"
                else:
                    msg = "  ⚠️  Could not save (keyring may not be available)"
                if self.console and RICH_AVAILABLE:
                    self.console.print(f"[green]{msg}[/green]" if ok else f"[yellow]{msg}[/yellow]")
                else:
                    cprint(msg)
        except Exception:
            pass

    def _annotate_method(self, result: AuthResult, method: str) -> AuthResult:
        """Stamp the auth method used onto the result."""
        result.auth_method = method
        result.user_info["auth_method"] = method
        return result

    def _show_methods_panel(self, methods: List[tuple]) -> None:
        """Display a Rich panel showing which auth methods are available.

        Each entry is (label, available: bool, detail: str).
        """
        if not (self.console and RICH_AVAILABLE):
            cprint("  Checking available auth methods...")
            for label, available, detail in methods:
                icon = "✓" if available else "✗"
                cprint(f"    {icon} {label:<30} {detail}")
            return

        lines = []
        for label, available, detail in methods:
            icon = "[green]✓[/green]" if available else "[red]✗[/red]"
            lines.append(f"  {icon} {label:<30} {detail}")
        self.console.print("\n  Checking available auth methods...")
        for line in lines:
            self.console.print(line)


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
        if self.console and RICH_AVAILABLE:
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
        if RICH_AVAILABLE:
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
                error_message=f"Google Cloud authentication failed: {e}",
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
            self.logger.error(f"Google Cloud logout failed: {e}")
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
        if self.console and RICH_AVAILABLE:
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

        if RICH_AVAILABLE:
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
                error_message=f"AWS authentication failed: {e}",
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
            self.logger.error(f"AWS logout failed: {e}")
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


class AzureAuthProvider(AuthProvider):
    """Microsoft Azure authentication provider"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        super().__init__("azure", config, logger)
        self.tenant_id = config.get("tenant_id")
        self.subscription_id = config.get("subscription_id")

    @staticmethod
    def _has_sdk() -> bool:
        try:
            import azure.identity  # noqa: F401

            return True
        except ImportError:
            return False

    def _validate_via_sdk(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> AuthResult:
        """Validate Azure credentials via azure-identity SDK (no CLI needed)."""
        try:
            from azure.identity import ClientSecretCredential, DefaultAzureCredential

            if client_id and client_secret and tenant_id:
                credential = ClientSecretCredential(
                    tenant_id=tenant_id,
                    client_id=client_id,
                    client_secret=client_secret,
                )
            else:
                credential = DefaultAzureCredential()

            token = credential.get_token("https://management.azure.com/.default")
            return AuthResult(
                provider=self.name,
                status=AuthStatus.AUTHENTICATED,
                user_info={
                    "tenant_id": tenant_id or self.tenant_id or "default",
                    "client_id": client_id or "default credential",
                    "token_expires": str(token.expires_on) if token else "unknown",
                },
            )
        except Exception as e:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message=str(e),
            )

    def _login_via_cli(self) -> AuthResult:
        """Run the interactive az CLI login flow."""
        if self.console and RICH_AVAILABLE:
            self.console.print(
                Panel.fit(
                    "[bold blue]🔐 Azure Authentication[/bold blue]\n\n"
                    f"Tenant: [cyan]{self.tenant_id or 'Default'}[/cyan]\n"
                    f"Subscription: [cyan]{self.subscription_id or 'Default'}[/cyan]\n\n"
                    "This will open your web browser to complete authentication.",
                    border_style="blue",
                )
            )
            if not Confirm.ask("\nProceed with Azure authentication?", default=True):
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
                task = progress.add_task("Initiating Azure login...", total=1)
                command = ["az", "login"]
                if self.tenant_id:
                    command.extend(["--tenant", self.tenant_id])
                self._run_command(command, capture_output=False)
                if self.subscription_id:
                    self._run_command(
                        ["az", "account", "set", "--subscription", self.subscription_id]
                    )
                progress.update(task, completed=1)
        else:
            cprint("🔐 Initiating Azure authentication via CLI...")
            command = ["az", "login"]
            if self.tenant_id:
                command.extend(["--tenant", self.tenant_id])
            self._run_command(command, capture_output=False)
            if self.subscription_id:
                self._run_command(["az", "account", "set", "--subscription", self.subscription_id])

        result = self._validate_via_sdk()
        if result.status == AuthStatus.AUTHENTICATED:
            return self._annotate_method(result, "Azure CLI")
        # Fallback to CLI status check
        try:
            r = self._run_command(
                ["az", "account", "show", "--output", "json"], capture_output=True
            )
            if r.returncode == 0:
                info = json.loads(r.stdout)
                return self._annotate_method(
                    AuthResult(
                        provider=self.name,
                        status=AuthStatus.AUTHENTICATED,
                        user_info={
                            "name": info.get("name"),
                            "tenant_id": info.get("tenantId"),
                            "user": info.get("user", {}).get("name"),
                        },
                    ),
                    "Azure CLI",
                )
        except Exception:
            pass
        return result

    def _prompt_for_credentials(self) -> AuthResult:
        """Guide the user to provide Azure service principal credentials."""
        from getpass import getpass

        if RICH_AVAILABLE:
            tenant = Prompt.ask("\n  Azure Tenant ID")
            client = Prompt.ask("  Application (Client) ID")
        else:
            tenant = input("\n  Azure Tenant ID: ")
            client = input("  Application (Client) ID: ")
        secret = getpass("  Client Secret: ")

        tenant, client, secret = tenant.strip(), client.strip(), secret.strip()
        if not all([tenant, client, secret]):
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message="Tenant ID, Client ID, and Client Secret are all required",
            )

        result = self._validate_via_sdk(client, secret, tenant)
        if result.status == AuthStatus.AUTHENTICATED:
            result = self._annotate_method(result, "service principal")
            self._offer_save_to_keyring(
                {
                    "tenant_id": tenant,
                    "client_id": client,
                    "client_secret": secret,
                }
            )
        return result

    async def login(self, **kwargs) -> AuthResult:
        """Smart login: CLI → env vars → keyring → DefaultAzureCredential → guided prompt."""
        import shutil

        try:
            has_cli = bool(shutil.which("az"))
            has_sdk = self._has_sdk()
            env_client = os.environ.get("AZURE_CLIENT_ID")
            env_secret = os.environ.get("AZURE_CLIENT_SECRET")
            env_tenant = os.environ.get("AZURE_TENANT_ID")
            kr_client = self._get_keyring_credential("client_id")
            kr_secret = self._get_keyring_credential("client_secret")
            kr_tenant = self._get_keyring_credential("tenant_id")
            interactive = self._is_interactive()

            if interactive:
                self._show_methods_panel(
                    [
                        ("Azure CLI", has_cli, "installed" if has_cli else "not installed"),
                        ("AZURE_CLIENT_ID", bool(env_client), "set" if env_client else "not set"),
                        ("Saved credentials", bool(kr_client), "found" if kr_client else "none"),
                        ("Manual setup", interactive, "available"),
                    ]
                )

            # 1. CLI
            if has_cli and interactive:
                try:
                    return self._login_via_cli()
                except Exception:
                    self.logger.debug("Azure CLI login failed, trying next method")

            # 2. Env vars + SDK
            if env_client and env_secret and env_tenant and has_sdk:
                result = self._validate_via_sdk(env_client, env_secret, env_tenant)
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "AZURE_CLIENT_ID env var")

            # 3. Keyring + SDK
            if kr_client and kr_secret and kr_tenant and has_sdk:
                result = self._validate_via_sdk(kr_client, kr_secret, kr_tenant)
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "saved service principal")

            # 4. DefaultAzureCredential (managed identity, VS Code, etc.)
            if has_sdk:
                result = self._validate_via_sdk()
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "DefaultAzureCredential")

            # 5. Interactive prompt
            if interactive and has_sdk:
                return self._prompt_for_credentials()

            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message=(
                    "No Azure credentials found. Options:\n"
                    "  • Install Azure CLI: https://learn.microsoft.com/en-us/cli/azure/install-azure-cli\n"
                    "  • Set AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID\n"
                    "  • Run interactively: fluid auth login azure"
                ),
            )
        except Exception as e:
            return AuthResult(
                provider=self.name,
                status=AuthStatus.ERROR,
                error_message=f"Azure authentication failed: {e}",
            )

    async def logout(self) -> bool:
        """Logout from Azure"""
        import shutil

        try:
            if shutil.which("az"):
                self._run_command(["az", "logout"], check=False)
            self.logger.info("Azure logout completed")
            return True
        except Exception as e:
            self.logger.error(f"Azure logout failed: {e}")
            return False

    async def check_auth(self) -> AuthResult:
        """Check Azure auth — tries SDK first, CLI second."""
        import shutil

        try:
            if self._has_sdk():
                result = self._validate_via_sdk()
                if result.status == AuthStatus.AUTHENTICATED:
                    return self._annotate_method(result, "SDK")

            if shutil.which("az"):
                try:
                    r = self._run_command(
                        ["az", "account", "show", "--output", "json"], capture_output=True
                    )
                    if r.returncode == 0:
                        info = json.loads(r.stdout)
                        return self._annotate_method(
                            AuthResult(
                                provider=self.name,
                                status=AuthStatus.AUTHENTICATED,
                                user_info={
                                    "name": info.get("name"),
                                    "id": info.get("id"),
                                    "tenant_id": info.get("tenantId"),
                                    "user": info.get("user", {}).get("name"),
                                },
                            ),
                            "Azure CLI",
                        )
                except Exception:
                    pass

            return AuthResult(
                provider=self.name,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message="No valid Azure credentials found",
            )
        except Exception as e:
            return AuthResult(provider=self.name, status=AuthStatus.ERROR, error_message=str(e))


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

        error_msg = result.stderr.strip() if result.stderr else "Authentication failed"
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
        import urllib.request

        url = f"{host.rstrip('/')}/api/2.0/preview/scim/v2/Me"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
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
        if self.console and RICH_AVAILABLE:
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
            error_message=test.stderr.strip() if test.stderr else "CLI configuration failed",
        )

    def _prompt_for_credentials(self) -> AuthResult:
        """Guide the user to provide Databricks host + personal access token."""
        from getpass import getpass

        if RICH_AVAILABLE:
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
            self.logger.error(f"Failed to clear Databricks configuration: {e}")
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


class AuthManager:
    """Manages authentication for multiple providers"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.console = Console() if RICH_AVAILABLE else None
        self.providers: Dict[str, AuthProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self):
        """Initialize available authentication providers"""
        provider_classes = {
            "google_cloud": GoogleCloudAuthProvider,
            "gcp": GoogleCloudAuthProvider,  # Alias
            "google": GoogleCloudAuthProvider,  # Alias
            "aws": AWSAuthProvider,
            "amazon": AWSAuthProvider,  # Alias
            "azure": AzureAuthProvider,
            "microsoft": AzureAuthProvider,  # Alias
            "snowflake": SnowflakeAuthProvider,
            "databricks": DatabricksAuthProvider,
        }

        for provider_name, provider_class in provider_classes.items():
            try:
                # Get provider config from various sources
                provider_config = {}

                # Check for provider-specific config
                if provider_name in self.config:
                    provider_config.update(self.config[provider_name])

                # Check for the base name in config (e.g., 'aws' for 'amazon')
                base_name = {
                    "gcp": "google_cloud",
                    "google": "google_cloud",
                    "amazon": "aws",
                    "microsoft": "azure",
                }.get(provider_name, provider_name)

                if base_name in self.config and base_name != provider_name:
                    provider_config.update(self.config[base_name])

                self.providers[provider_name] = provider_class(provider_config, self.logger)
            except Exception as e:
                self.logger.warning(f"Failed to initialize {provider_name} provider: {e}")

    def get_provider(self, provider_name: str) -> Optional[AuthProvider]:
        """Get authentication provider by name"""
        return self.providers.get(provider_name.lower())

    def list_providers(self) -> List[str]:
        """List available authentication providers"""
        # Return unique provider types (not aliases)
        unique_providers = []
        seen_classes = set()
        for name, provider in self.providers.items():
            provider_class = type(provider).__name__
            if provider_class not in seen_classes:
                unique_providers.append(name)
                seen_classes.add(provider_class)
        return unique_providers

    async def login(self, provider_name: str, **kwargs) -> AuthResult:
        """Login to specified provider"""
        provider = self.get_provider(provider_name)
        if not provider:
            return AuthResult(
                provider=provider_name,
                status=AuthStatus.ERROR,
                error_message=f"Provider '{provider_name}' not supported. Available: {', '.join(self.list_providers())}",
            )

        return await provider.login(**kwargs)

    async def logout(self, provider_name: str) -> bool:
        """Logout from specified provider"""
        provider = self.get_provider(provider_name)
        if not provider:
            self.logger.error(f"Provider '{provider_name}' not found")
            return False

        return await provider.logout()

    async def check_auth(self, provider_name: str) -> AuthResult:
        """Check authentication status for specified provider"""
        provider = self.get_provider(provider_name)
        if not provider:
            return AuthResult(
                provider=provider_name,
                status=AuthStatus.ERROR,
                error_message=f"Provider '{provider_name}' not supported",
            )

        return await provider.check_auth()


# Enhanced CLI Registration
def register(subparsers: argparse._SubParsersAction):
    """Register the auth command with enhanced functionality"""
    p = subparsers.add_parser(
        COMMAND,
        help="Provider authentication management",
        description=(
            "Manage authentication for cloud and data platform providers.\n\n"
            "Usage:\n"
            "  fluid auth login gcp        Authenticate with Google Cloud\n"
            "  fluid auth login aws         Authenticate with AWS\n"
            "  fluid auth status            Show status for all providers\n"
            "  fluid auth doctor            Audit credential hygiene\n"
            "  fluid auth list              List available providers"
        ),
    )

    # NOTE: --provider is intentionally NOT defined here because the top-level
    # CLI already has --provider (for data infrastructure selection). Adding it
    # again would cause argparse conflicts. Auth providers are passed as
    # positional arguments to each verb instead (e.g., `fluid auth login gcp`).

    # Create subcommands
    sp = p.add_subparsers(dest="verb", required=True, help="Authentication action")

    _provider_names = "gcp, aws, azure, snowflake, databricks"

    # Login command
    login_parser = sp.add_parser(
        "login",
        help="Authenticate with a cloud provider",
        description=f"Log in to a cloud or data platform provider.\n\nAvailable providers: {_provider_names}",
    )
    login_parser.add_argument(
        "provider",
        nargs="?",
        help=f"Provider name ({_provider_names})",
    )
    login_parser.set_defaults(func=run)

    # Status command
    status_parser = sp.add_parser(
        "status",
        help="Show authentication status",
        description="Show current auth status. Omit provider to check all.",
    )
    status_parser.add_argument(
        "provider",
        nargs="?",
        help="Provider to check (omit to check all)",
    )
    status_parser.set_defaults(func=run)

    # Logout command
    logout_parser = sp.add_parser(
        "logout",
        help="Logout from a provider",
        description=f"Revoke credentials for a provider.\n\nAvailable providers: {_provider_names}",
    )
    logout_parser.add_argument(
        "provider",
        nargs="?",
        help=f"Provider to logout from ({_provider_names})",
    )
    logout_parser.set_defaults(func=run)

    # List providers command
    list_parser = sp.add_parser("list", help="List available authentication providers")
    list_parser.set_defaults(func=run)

    # Doctor command — audit credential hygiene and security posture
    doctor_parser = sp.add_parser(
        "doctor",
        help="Audit credential hygiene and security posture",
        description=(
            "Run security checks on your credential setup.\n\n"
            "Checks: file permissions, keyring availability, OIDC in CI,\n"
            "long-lived credentials, and per-provider auth status.\n\n"
            "Use --fix to auto-remediate permission issues."
        ),
    )
    doctor_parser.add_argument(
        "provider",
        nargs="?",
        help="Provider to audit (omit to audit all)",
    )
    doctor_parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-fix issues where possible",
    )
    doctor_parser.set_defaults(func=run)

    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Main entry point for auth command with enhanced functionality"""
    try:
        # Simple config (since load_config is not available)
        config = {}
        auth_manager = AuthManager(config, logger)

        # Handle list command
        if args.verb == "list":
            console = Console() if RICH_AVAILABLE else None
            providers = auth_manager.list_providers()

            if console:
                console.print("\n[bold blue]🔐 Available Authentication Providers[/bold blue]")
                console.print("=" * 50)

                table = Table()
                table.add_column("Provider", style="cyan")
                table.add_column("Aliases", style="dim")
                table.add_column("Description")

                provider_info = {
                    "google_cloud": ("gcp, google", "Google Cloud Platform"),
                    "aws": ("amazon", "Amazon Web Services"),
                    "azure": ("microsoft", "Microsoft Azure"),
                    "snowflake": ("", "Snowflake Data Cloud"),
                    "databricks": ("", "Databricks Unified Analytics Platform"),
                }

                for provider in providers:
                    if provider in provider_info:
                        aliases, description = provider_info[provider]
                        table.add_row(provider, aliases, description)

                console.print(table)
                console.print("\n[dim]Usage: fluid auth login <provider>[/dim]")
            else:
                cprint("Available authentication providers:")
                for provider in providers:
                    cprint(f"  - {provider}")
                cprint("\nUsage: fluid auth login <provider>")

            return 0

        # Provider comes from the positional argument on each verb subparser
        provider = getattr(args, "provider", None)

        # Run async commands
        if args.verb == "login":
            if not provider:
                logger.error("❌ Provider required. Usage: fluid auth login <provider>")
                logger.info(f"Available providers: {', '.join(auth_manager.list_providers())}")
                logger.info("Example: fluid auth login gcp")
                return 1

            return asyncio.run(handle_login(provider, auth_manager, logger))

        elif args.verb == "status":
            return asyncio.run(handle_status(provider, auth_manager, logger))

        elif args.verb == "logout":
            if not provider:
                logger.error("❌ Provider required. Usage: fluid auth logout <provider>")
                logger.info("Example: fluid auth logout gcp")
                return 1

            return asyncio.run(handle_logout(provider, auth_manager, logger))

        elif args.verb == "doctor":
            fix = getattr(args, "fix", False)
            return asyncio.run(handle_doctor(provider, auth_manager, logger, fix=fix))

        else:
            # Simplified authentication for compatibility
            logger.info(
                f"Authentication command not fully implemented for verb: {getattr(args, 'verb', 'unknown')}"
            )
            return 0

    except KeyboardInterrupt:
        logger.warning("⚠️ Authentication interrupted by user")
        return 130
    except CLIError:
        raise
    except Exception as e:
        logger.error(f"💥 Authentication failed: {e}")
        raise CLIError(1, "auth_failed", {"error": str(e)})


# Enhanced Handler Functions
async def handle_login(provider: str, auth_manager: AuthManager, logger: logging.Logger) -> int:
    """Handle login command with rich output"""
    try:
        console = Console() if RICH_AVAILABLE else None

        if console:
            console.print("\n[bold green]🔐 FLUID Authentication[/bold green]")
            console.print("=" * 50)

        result = await auth_manager.login(provider)

        if result.status == AuthStatus.AUTHENTICATED:
            if console:
                console.print(
                    f"\n[bold green]✅ Successfully authenticated with {provider}![/bold green]"
                )
                if result.user_info:
                    table = Table(title="Authentication Details", border_style="green")
                    table.add_column("Property", style="cyan")
                    table.add_column("Value", style="green")

                    for key, value in result.user_info.items():
                        table.add_row(key.replace("_", " ").title(), str(value))

                    console.print(table)

                console.print(
                    f"\n[dim]💡 You can now use FLUID to manage resources in {provider}[/dim]"
                )
            else:
                logger.info(f"✅ Successfully authenticated with {provider}")
                if result.user_info:
                    for key, value in result.user_info.items():
                        logger.info(f"{key}: {value}")

            return 0
        else:
            error_msg = result.error_message or "Authentication failed"
            if console:
                console.print(f"\n[bold red]❌ Authentication failed: {error_msg}[/bold red]")

                if "not installed" in error_msg.lower():
                    console.print(
                        Panel.fit(
                            f"[yellow]Please install the required CLI tool for {provider}:\n\n"
                            f"• Google Cloud: https://cloud.google.com/sdk/docs/install\n"
                            f"• AWS: https://aws.amazon.com/cli/\n"
                            f"• Azure: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli[/yellow]",
                            title="Installation Required",
                            border_style="yellow",
                        )
                    )
            else:
                logger.error(f"❌ Authentication failed: {error_msg}")
            return 1

    except Exception as e:
        logger.error(f"❌ Login failed: {e}")
        return 1


async def handle_logout(provider: str, auth_manager: AuthManager, logger: logging.Logger) -> int:
    """Handle logout command"""
    try:
        success = await auth_manager.logout(provider)

        if success:
            logger.info(f"✅ Successfully logged out from {provider}")
            return 0
        else:
            logger.error(f"❌ Failed to logout from {provider}")
            return 1

    except Exception as e:
        logger.error(f"❌ Logout failed: {e}")
        return 1


async def handle_status(
    provider: Optional[str], auth_manager: AuthManager, logger: logging.Logger
) -> int:
    """Handle status command with rich output"""
    try:
        console = Console() if RICH_AVAILABLE else None

        if provider:
            # Check specific provider
            result = await auth_manager.check_auth(provider)

            if console:
                status_color = {
                    AuthStatus.AUTHENTICATED: "green",
                    AuthStatus.NOT_AUTHENTICATED: "red",
                    AuthStatus.EXPIRED: "yellow",
                    AuthStatus.ERROR: "red",
                }.get(result.status, "white")

                console.print(
                    f"\n[bold blue]🔍 Authentication Status - {provider.title()}[/bold blue]"
                )
                console.print("=" * 40)
                console.print(
                    f"Status: [{status_color}]{result.status.value.replace('_', ' ').title()}[/{status_color}]"
                )

                if result.user_info:
                    table = Table(title="Account Information", border_style=status_color)
                    table.add_column("Property", style="cyan")
                    table.add_column("Value")

                    for key, value in result.user_info.items():
                        table.add_row(key.replace("_", " ").title(), str(value))

                    console.print(table)

                if result.error_message:
                    console.print(f"\n[red]Error: {result.error_message}[/red]")

                if result.status == AuthStatus.NOT_AUTHENTICATED:
                    console.print(f"\n[dim]💡 Run: fluid auth login {provider}[/dim]")
            else:
                cprint(f"{provider}: {result.status.value}")
                if result.user_info:
                    for key, value in result.user_info.items():
                        cprint(f"  {key}: {value}")
                if result.error_message:
                    cprint(f"  Error: {result.error_message}")

            return 0 if result.status == AuthStatus.AUTHENTICATED else 1
        else:
            # Check all providers
            providers = auth_manager.list_providers()
            all_authenticated = True

            if console:
                console.print("\n[bold blue]🔍 Authentication Status - All Providers[/bold blue]")
                console.print("=" * 50)

                table = Table()
                table.add_column("Provider", style="cyan")
                table.add_column("Status", style="bold")
                table.add_column("Account/Details")

                for provider_name in providers:
                    result = await auth_manager.check_auth(provider_name)

                    status_style = {
                        AuthStatus.AUTHENTICATED: "green",
                        AuthStatus.NOT_AUTHENTICATED: "red",
                        AuthStatus.EXPIRED: "yellow",
                        AuthStatus.ERROR: "red",
                    }.get(result.status, "white")

                    if result.status != AuthStatus.AUTHENTICATED:
                        all_authenticated = False

                    details = ""
                    if result.user_info:
                        # Show most relevant detail
                        if "account" in result.user_info:
                            details = result.user_info["account"]
                        elif "user" in result.user_info:
                            details = result.user_info["user"]
                        elif "name" in result.user_info:
                            details = result.user_info["name"]
                    elif result.error_message:
                        details = (
                            result.error_message[:40] + "..."
                            if len(result.error_message) > 40
                            else result.error_message
                        )

                    table.add_row(
                        provider_name.title(),
                        f"[{status_style}]{result.status.value.replace('_', ' ').title()}[/{status_style}]",
                        details,
                    )

                console.print(table)
                console.print("\n[dim]💡 Use: fluid auth login <provider> to authenticate[/dim]")
            else:
                cprint("Authentication Status:")
                for provider_name in providers:
                    result = await auth_manager.check_auth(provider_name)
                    cprint(f"  {provider_name}: {result.status.value}")
                    if result.status != AuthStatus.AUTHENTICATED:
                        all_authenticated = False
                    if result.error_message:
                        cprint(f"    Error: {result.error_message}")

            return 0 if all_authenticated else 1

    except Exception as e:
        logger.error(f"❌ Status check failed: {e}")
        return 1


# ──────────────────────────────────────────────────────────────────────────────
# CI Environment Detection
# ──────────────────────────────────────────────────────────────────────────────


class CIEnvironment(Enum):
    """Detected CI/CD environment."""

    GITHUB_ACTIONS = "github_actions"
    GITLAB_CI = "gitlab_ci"
    JENKINS = "jenkins"
    CIRCLECI = "circleci"
    BITBUCKET = "bitbucket"
    AZURE_DEVOPS = "azure_devops"
    NONE = "none"


def detect_ci_environment() -> CIEnvironment:
    """Detect the current CI/CD environment from env vars."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return CIEnvironment.GITHUB_ACTIONS
    if os.environ.get("GITLAB_CI") == "true":
        return CIEnvironment.GITLAB_CI
    if os.environ.get("JENKINS_URL"):
        return CIEnvironment.JENKINS
    if os.environ.get("CIRCLECI") == "true":
        return CIEnvironment.CIRCLECI
    if os.environ.get("BITBUCKET_PIPELINE_UUID"):
        return CIEnvironment.BITBUCKET
    if os.environ.get("TF_BUILD") == "True":
        return CIEnvironment.AZURE_DEVOPS
    return CIEnvironment.NONE


def _is_ci() -> bool:
    """Return True if running in any CI environment."""
    return detect_ci_environment() != CIEnvironment.NONE or os.environ.get("CI") == "true"


def _has_oidc_available() -> Dict[str, bool]:
    """Check which OIDC providers are available in the current CI environment."""
    return {
        "gcp": bool(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")),
        "aws": bool(
            os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
            or os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        ),
        "azure": bool(os.environ.get("AZURE_FEDERATED_TOKEN_FILE")),
        "gitlab_oidc": bool(os.environ.get("CI_JOB_JWT_V2")),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Auth Doctor — Credential Hygiene Audit
# ──────────────────────────────────────────────────────────────────────────────

# Minimum permissions FLUID needs per provider (informational)
PROVIDER_MINIMAL_SCOPES = {
    "google_cloud": {
        "recommended_roles": [
            "roles/bigquery.dataEditor",
            "roles/bigquery.jobUser",
            "roles/datacatalog.viewer",
        ],
        "overly_broad": ["roles/owner", "roles/editor", "roles/bigquery.admin"],
        "note": "Use Workload Identity Federation instead of service account keys",
    },
    "aws": {
        "recommended_policies": [
            "AmazonS3ReadOnlyAccess",
            "AWSGlueConsoleFullAccess",
        ],
        "overly_broad": ["AdministratorAccess", "PowerUserAccess"],
        "note": "Use OIDC role assumption instead of long-lived access keys",
    },
    "snowflake": {
        "recommended": "Create a dedicated FLUID role with minimal warehouse/database grants",
        "overly_broad": ["ACCOUNTADMIN", "SYSADMIN", "SECURITYADMIN"],
        "note": "Use key-pair authentication instead of passwords",
    },
}


class DoctorStatus(Enum):
    """Severity level for a doctor check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"


@dataclass
class DoctorCheck:
    """Result of a single doctor check."""

    name: str
    status: DoctorStatus
    message: str
    fix_hint: Optional[str] = None


async def handle_doctor(
    provider: Optional[str],
    auth_manager: AuthManager,
    logger: logging.Logger,
    fix: bool = False,
) -> int:
    """Audit credential hygiene and security posture."""
    console = Console() if RICH_AVAILABLE else None
    checks: List[DoctorCheck] = []

    # ── Check 1: CI environment detection ──
    ci_env = detect_ci_environment()
    is_ci = _is_ci()
    if is_ci:
        checks.append(
            DoctorCheck(
                name="CI Environment",
                status=DoctorStatus.INFO,
                message=f"Running in CI: {ci_env.value}",
            )
        )

        # Check for OIDC availability
        oidc = _has_oidc_available()
        oidc_available = [k for k, v in oidc.items() if v]
        if oidc_available:
            checks.append(
                DoctorCheck(
                    name="OIDC Availability",
                    status=DoctorStatus.PASS,
                    message=f"OIDC tokens available for: {', '.join(oidc_available)}",
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="OIDC Availability",
                    status=DoctorStatus.WARN,
                    message="No OIDC tokens detected in CI — using stored secrets",
                    fix_hint="Configure Workload Identity Federation for your CI provider",
                )
            )
    else:
        checks.append(
            DoctorCheck(
                name="CI Environment",
                status=DoctorStatus.INFO,
                message="Running locally (not CI)",
            )
        )

    # ── Check 2: Keyring availability ──
    try:
        import keyring as _kr  # noqa: F401

        checks.append(
            DoctorCheck(
                name="OS Keyring",
                status=DoctorStatus.PASS,
                message="OS keyring available for secure credential storage",
            )
        )
    except ImportError:
        checks.append(
            DoctorCheck(
                name="OS Keyring",
                status=DoctorStatus.WARN,
                message="keyring library not installed — credentials may use less secure storage",
                fix_hint="pip install keyring",
            )
        )

    # ── Check 3: .env file permissions ──
    env_files = [".env", ".env.local"]
    project_root = os.getcwd()
    for env_file in env_files:
        env_path = os.path.join(project_root, env_file)
        if os.path.exists(env_path):
            try:
                stat = os.stat(env_path)
                mode = stat.st_mode & 0o777
                if mode & 0o044:  # world or group readable
                    checks.append(
                        DoctorCheck(
                            name=f"{env_file} Permissions",
                            status=DoctorStatus.FAIL,
                            message=f"{env_file} is readable by group/others (mode {oct(mode)})",
                            fix_hint=f"chmod 600 {env_file}",
                        )
                    )
                    if fix:
                        os.chmod(env_path, 0o600)
                        checks[-1].status = DoctorStatus.PASS
                        checks[-1].message += " [FIXED]"
                else:
                    checks.append(
                        DoctorCheck(
                            name=f"{env_file} Permissions",
                            status=DoctorStatus.PASS,
                            message=f"{env_file} has secure permissions ({oct(mode)})",
                        )
                    )
            except OSError:
                pass

    # ── Check 4: Encrypted store key file permissions ──
    fluid_dir = os.path.expanduser("~/.fluid")
    key_path = os.path.join(fluid_dir, ".key")
    if os.path.exists(key_path):
        try:
            mode = os.stat(key_path).st_mode & 0o777
            if mode != 0o600:
                checks.append(
                    DoctorCheck(
                        name="Encryption Key Perms",
                        status=DoctorStatus.FAIL,
                        message=f"~/.fluid/.key has insecure permissions ({oct(mode)})",
                        fix_hint="chmod 600 ~/.fluid/.key",
                    )
                )
                if fix:
                    os.chmod(key_path, 0o600)
                    checks[-1].status = DoctorStatus.PASS
                    checks[-1].message += " [FIXED]"
            else:
                checks.append(
                    DoctorCheck(
                        name="Encryption Key Perms",
                        status=DoctorStatus.PASS,
                        message="~/.fluid/.key has secure permissions (0o600)",
                    )
                )
        except OSError:
            pass

    # ── Check 5: Long-lived credentials in CI ──
    if is_ci:
        long_lived_env_vars = [
            ("AWS_ACCESS_KEY_ID", "aws", "Use OIDC role assumption instead"),
            ("AWS_SECRET_ACCESS_KEY", "aws", "Use OIDC role assumption instead"),
            ("GOOGLE_APPLICATION_CREDENTIALS", "gcp", "Use Workload Identity Federation instead"),
            ("SNOWFLAKE_PASSWORD", "snowflake", "Use key-pair or OAuth authentication"),
        ]
        for env_var, prov, hint in long_lived_env_vars:
            if provider and prov != _normalize_provider(provider):
                continue
            if os.environ.get(env_var):
                checks.append(
                    DoctorCheck(
                        name=f"Long-Lived Credential ({env_var})",
                        status=DoctorStatus.WARN,
                        message=f"{env_var} is set in CI — this is a long-lived credential",
                        fix_hint=hint,
                    )
                )

    # ── Check 6: Auth status per provider ──
    providers_to_check = (
        [_normalize_provider(provider)] if provider else auth_manager.list_providers()
    )
    for prov in providers_to_check:
        try:
            result = await auth_manager.check_auth(prov)
            if result.status == AuthStatus.AUTHENTICATED:
                cred_type = result.user_info.get("credential_type", "unknown")
                checks.append(
                    DoctorCheck(
                        name=f"{prov} Auth",
                        status=DoctorStatus.PASS,
                        message=f"Authenticated (type: {cred_type})",
                    )
                )
            elif result.status == AuthStatus.EXPIRED:
                checks.append(
                    DoctorCheck(
                        name=f"{prov} Auth",
                        status=DoctorStatus.WARN,
                        message="Credentials expired — re-authenticate",
                        fix_hint=f"fluid auth login {prov}",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        name=f"{prov} Auth",
                        status=DoctorStatus.INFO,
                        message=f"Not authenticated ({result.error_message or 'not configured'})",
                    )
                )
        except Exception:
            checks.append(
                DoctorCheck(
                    name=f"{prov} Auth",
                    status=DoctorStatus.INFO,
                    message="Could not check (provider CLI not available)",
                )
            )

    # ── Check 7: Least-privilege scope recommendations ──
    for prov in providers_to_check:
        if prov in PROVIDER_MINIMAL_SCOPES:
            scope_info = PROVIDER_MINIMAL_SCOPES[prov]
            checks.append(
                DoctorCheck(
                    name=f"{prov} Scope Guidance",
                    status=DoctorStatus.INFO,
                    message=scope_info.get("note", "Review IAM permissions for least privilege"),
                )
            )

    # ── Render results ──
    warn_count = sum(1 for c in checks if c.status == DoctorStatus.WARN)
    fail_count = sum(1 for c in checks if c.status == DoctorStatus.FAIL)

    if console and RICH_AVAILABLE:
        console.print("\n[bold blue]Auth Doctor — Credential Hygiene Audit[/bold blue]")
        console.print("=" * 55)

        table = Table()
        table.add_column("Check", style="cyan", min_width=25)
        table.add_column("Status", min_width=6)
        table.add_column("Details")
        table.add_column("Fix", style="dim")

        status_style = {
            DoctorStatus.PASS: "[green]PASS[/green]",
            DoctorStatus.WARN: "[yellow]WARN[/yellow]",
            DoctorStatus.FAIL: "[red]FAIL[/red]",
            DoctorStatus.INFO: "[blue]INFO[/blue]",
        }

        for check in checks:
            table.add_row(
                check.name,
                status_style.get(check.status, check.status.value),
                check.message,
                check.fix_hint or "",
            )

        console.print(table)

        if fail_count:
            console.print(f"\n[red]{fail_count} critical issue(s) found.[/red]")
        if warn_count:
            console.print(f"[yellow]{warn_count} warning(s) — review recommended.[/yellow]")
        if not fail_count and not warn_count:
            console.print("\n[green]All checks passed.[/green]")
    else:
        cprint("Auth Doctor — Credential Hygiene Audit")
        cprint("=" * 55)
        for check in checks:
            icon = {
                DoctorStatus.PASS: "+",
                DoctorStatus.WARN: "!",
                DoctorStatus.FAIL: "X",
                DoctorStatus.INFO: "i",
            }.get(check.status, "?")
            cprint(f"  [{icon}] {check.name}: {check.message}")
            if check.fix_hint:
                cprint(f"      Fix: {check.fix_hint}")

    return 1 if fail_count else 0


def _normalize_provider(provider: str) -> str:
    """Normalize provider name aliases to canonical form."""
    aliases = {
        "gcp": "google_cloud",
        "google": "google_cloud",
        "amazon": "aws",
        "microsoft": "azure",
    }
    return aliases.get(provider, provider)
