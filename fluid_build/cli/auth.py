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

from fluid_build.cli._common import CLIError
from fluid_build.cli.console import cprint

# SECURITY_REVIEW S-011: when logging argv for provider subprocesses, the
# next argument after a credential-bearing flag (and the RHS of
# ``--flag=value`` forms) must be redacted. The central
# SecretRedactingFilter catches most secret-shaped values, but flag
# values like ``--password hunter2`` or a ``--key-file`` pointing at a
# filesystem path aren't caught by generic pattern matching — they need
# flag-aware stripping.
_REDACTED = "***REDACTED***"
_SENSITIVE_FLAG_SUFFIXES = ("-secret", "-key", "-token", "-password", "-passphrase")
_SENSITIVE_FLAGS = frozenset(
    {
        "--password",
        "--pass",
        "--token",
        "--api-key",
        "--key-file",
        "--key",
        "--secret",
        "--passphrase",
        "--credentials",
        "-p",
    }
)


def _sanitize_argv(command: List[str]) -> List[str]:
    """Return a copy of ``command`` with credential-bearing argv values
    replaced by ``***REDACTED***``.

    Handles two shapes:
    - ``["--password", "hunter2"]`` → the next element is redacted.
    - ``["--password=hunter2"]`` → the RHS of ``=`` is redacted.

    Sensitive flags are the exact entries in ``_SENSITIVE_FLAGS`` plus
    anything ending in ``-secret``, ``-key``, ``-token``, ``-password``,
    ``-passphrase`` (catches provider-specific variants like
    ``--client-secret`` or ``--service-account-key``).
    """
    out: List[str] = []
    redact_next = False
    for arg in command:
        if redact_next:
            out.append(_REDACTED)
            redact_next = False
            continue
        if "=" in arg and arg.startswith("--"):
            flag, _, _ = arg.partition("=")
            if flag in _SENSITIVE_FLAGS or any(
                flag.endswith(suffix) for suffix in _SENSITIVE_FLAG_SUFFIXES
            ):
                out.append(f"{flag}={_REDACTED}")
                continue
        if arg in _SENSITIVE_FLAGS or any(
            arg.endswith(suffix) for suffix in _SENSITIVE_FLAG_SUFFIXES
        ):
            out.append(arg)
            redact_next = True
            continue
        out.append(arg)
    return out


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
        """Run an external command as an argv list.

        Never uses ``shell=True`` — ``command`` is always the argv list
        passed directly to ``subprocess.run``. See SECURITY.md for why
        ``shell=True`` is banned across this codebase.
        """
        try:
            # S-011: strip credential-bearing flag values before logging.
            # ``%s`` + positional keeps the SecretRedactingFilter in the
            # logging chain as defense-in-depth.
            self.logger.debug("Running command: %s", " ".join(_sanitize_argv(command)))
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


# ── Per-provider auth class re-imports ────────────────────────────
# The 5 ``AuthProvider`` subclasses (GoogleCloud / AWS / Azure /
# Snowflake / Databricks) were physically extracted into the
# ``_auth_provider_impls`` sibling module so ~1,400 LOC of
# credential-resolution + service-detection logic lives in a
# dedicated file. Re-imported here at module top so existing test
# patches that target ``fluid_build.cli.auth.<ProviderClass>``
# still resolve via the namespace.
from fluid_build.cli._auth_provider_impls import (  # noqa: E402,F401
    AWSAuthProvider,
    AzureAuthProvider,
    DatabricksAuthProvider,
    GoogleCloudAuthProvider,
    SnowflakeAuthProvider,
)


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

    # Create subcommands.  ``required=False`` so a bare
    # ``fluid auth`` doesn't blow up with the bare-bones argparse
    # "the following arguments are required: verb" error.  ``run``
    # catches the ``args.verb is None`` case and renders a
    # Rich-friendly panel listing the verbs instead.
    p.set_defaults(func=run)
    sp = p.add_subparsers(dest="verb", required=False, help="Authentication action")

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
    if getattr(args, "verb", None) is None:
        # Bare ``fluid auth`` — render an intuitive guide instead of
        # the legacy argparse "the following arguments are required"
        # error.
        return _render_auth_guide()
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


def _render_auth_guide() -> int:
    """Render an intuitive guide for ``fluid auth`` with no verb.

    Detects whether any provider is already authenticated (saved
    auth file or keyring entry) and promotes ``status`` when the
    operator already has credentials configured; otherwise points
    them at ``login`` as the right starting move.
    """

    from pathlib import Path

    from fluid_build.cli._subcommand_guide import (
        SubcommandEntry,
        SubcommandGuide,
        SubcommandHint,
        render_subcommand_guide,
    )

    entries = [
        SubcommandEntry(
            name="login",
            description="Authenticate with a cloud / data-platform provider.",
            example="fluid auth login gcp",
        ),
        SubcommandEntry(
            name="status",
            description="Show current auth status (omit provider to check all).",
            example="fluid auth status",
        ),
        SubcommandEntry(
            name="logout",
            description="Sign out of a provider (clear local credentials).",
            example="fluid auth logout aws",
        ),
        SubcommandEntry(
            name="list",
            description="List available authentication providers and aliases.",
            example="fluid auth list",
        ),
        SubcommandEntry(
            name="doctor",
            description="Audit credential hygiene + recommend rotations.",
            example="fluid auth doctor",
        ),
    ]

    def _detect_hint() -> Any:
        # Cheap detection: ``~/.fluid/auth.json`` exists, OR
        # ``~/.fluid/store/audit/`` has a recent ``auth_*`` event.
        # Avoids any keyring round-trip so the guide stays fast.
        auth_json = Path.home() / ".fluid" / "auth.json"
        ai_config = Path.home() / ".fluid" / "ai_config.json"
        if auth_json.is_file() or ai_config.is_file():
            return SubcommandHint(
                subcommand="status",
                rationale="you already have local auth state — see what's signed in.",
            )
        return SubcommandHint(
            subcommand="login",
            rationale="no local auth state yet — sign in to a provider first.",
        )

    guide = SubcommandGuide(
        command_path="fluid auth",
        headline=(
            "Authenticate with cloud / data-platform providers "
            "(gcp, aws, azure, snowflake, databricks)."
        ),
        entries=entries,
        hint_provider=_detect_hint,
        quick_start="fluid auth status",
    )
    return render_subcommand_guide(guide)
