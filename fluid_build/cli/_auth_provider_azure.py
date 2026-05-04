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
"""Azure auth provider — physical extraction.

Lifted from ``cli/_auth_provider_impls.py`` (host file was 1517
LOC). ~280 LOC of Azure-specific auth flow. ``_auth_provider_impls``
re-imports :class:`AzureAuthProvider` so existing call sites and
test patches keep resolving.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fluid_build.cli import auth as _auth
from fluid_build.cli.auth import AuthProvider, AuthResult, AuthStatus, _sanitize_argv
from fluid_build.cli.console import cprint, success


def _rich_available() -> bool:
    return getattr(_auth, "RICH_AVAILABLE", False)


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
        if self.console and _rich_available():
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

        if _rich_available():
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
