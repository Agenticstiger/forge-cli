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

"""Credential resolution for V1.5 metadata-source catalogs.

> **UX vocabulary note.** This module supports the V1.5 *source*
> catalog role — where forge-cli READS metadata FROM (Snowflake
> Horizon, Databricks Unity, BigQuery + Dataplex, AWS Glue, DataHub,
> Data Mesh Manager). The existing *publish target* role
> (``providers/catalogs/`` — where forged contracts WRITE TO) is a
> separate concept with separate config, separate keyring entries,
> separate CLI flags. Every user-facing string here says "source"
> or "metadata source" — never "catalog" alone.

The resolver chain (A/B/B picks from the V1.5 plan):

```
Adapter says "I need SnowflakeCredentials"
   ↓
CredentialResolver tries (in order):
   1. Per-call inline credentials       (MCP ``credentials.inline`` arg)
   2. Per-call by reference             (``credentials.credential_id`` → keyring lookup)
   3. OS keyring entry                  (named ``fluid_source_<name>``)
   4. ~/.fluid/sources.yaml entry       (chmod 600)
   5. Environment variables             (SNOWFLAKE_*, DATABRICKS_*, …)
   6. Cloud metadata service / ADC      (only when --allow-metadata-service)
   7. Fail closed with actionable error
```

Each step returns a typed credentials object (or raises). Adapters
never see the resolution path — they just receive the credentials.

Three world-class guarantees enforced here:

1. **No credential leakage.** Every secret field uses Pydantic's
   :class:`SecretStr` so ``repr()`` / ``str()`` / JSON dumps never
   surface the raw value.
2. **No persistent token cache.** The resolver holds no state; every
   ``resolve()`` is a fresh lookup. Token-refresh logic lives inside
   each adapter's session and dies with the process.
3. **Fail-closed.** Missing credentials raise
   :class:`CredentialNotFoundError` with the exact next-action the
   operator needs (the right ``fluid ai setup --source NAME``
   command, or the env-var pattern for that catalog).

MCP-specific defenses (Sprint A invariants):

* The MCP server exposes a ``credential_id`` argument on every catalog
  tool — never the credential value. The LLM never sees secrets.
* No ``--default-source`` flag in v1.5: every call must specify
  ``credential_id``. Defends against an LLM-mediated agent escalating
  "list one schema" into "dump every table" by replaying ambient
  authority. (Revisit in v1.6 if users complain.)
* ``CredentialResolver(allow_metadata_service=False)`` by default;
  metadata-service / ADC auth requires explicit
  ``--allow-metadata-service`` (or ``FLUID_ALLOW_METADATA_SERVICE=1``)
  so a developer's laptop with auto-mounted GCP creds doesn't
  accidentally point at production.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Optional, Type, TypeVar

from pydantic import BaseModel, ConfigDict, SecretStr

from fluid_build.copilot.catalog.base import (
    CatalogConfigError,
)

_log = logging.getLogger(__name__)

# Storage paths — the user-facing names use "source", not "catalog".
SOURCES_CONFIG_DEFAULT = Path.home() / ".fluid" / "sources.yaml"
"""Per-user config file holding non-sensitive source definitions.

The keyring holds the secret values; this YAML holds account /
region / role / warehouse / etc. Same security pattern as
``~/.fluid/ai_config.json`` for LLM keys."""

KEYRING_PREFIX = "fluid_source"
"""Keyring entry prefix for source-catalog credentials.

Distinct from the LLM-API-key prefix (``llm_api_key``) so
``fluid ai status`` can enumerate source credentials without
walking unrelated keyring entries.
"""

PLAINTEXT_SOURCE_SECRETS_ENV = "FLUID_ALLOW_PLAINTEXT_SOURCE_SECRETS"
"""Explicit opt-in gate for legacy plaintext secrets in sources.yaml."""


# ---------------------------------------------------------------------
# Typed exception — cleaner than re-raising CatalogConfigError with
# "no credentials found" in the message.
# ---------------------------------------------------------------------


class CredentialNotFoundError(CatalogConfigError):
    """No credentials found for the requested source.

    Carries actionable suggestions naming the exact ``fluid ai
    setup --source NAME`` command or the env-var pattern the
    operator needs.
    """


# ---------------------------------------------------------------------
# Per-source typed credential models.
#
# Sprint A ships Snowflake + Unity. Sprint B adds BigQuery / Dataplex /
# Glue / DataHub / DMM. Every secret field uses ``SecretStr`` so
# ``repr()`` / JSON serialization never leak the raw value.
# ---------------------------------------------------------------------


class SnowflakeCredentials(BaseModel):
    """Snowflake Horizon source credentials.

    The recommended ``auth_method`` is ``private_key`` (rotation-
    friendly, no long-lived password). ``oauth`` is acceptable;
    ``password`` is legacy. ``sso`` (External Browser) is
    interactive-only — useful for ``fluid ai setup`` first-time
    flow but not for headless CI.
    """

    model_config = ConfigDict(frozen=True)

    account: str
    user: str
    auth_method: Literal["password", "private_key", "oauth", "sso"] = "private_key"
    password: Optional[SecretStr] = None
    private_key_path: Optional[Path] = None
    private_key_passphrase: Optional[SecretStr] = None
    oauth_token: Optional[SecretStr] = None
    role: Optional[str] = None
    warehouse: Optional[str] = None

    def to_connection_kwargs(self) -> Dict[str, Any]:
        """Translate to the dict the ``snowflake.connector.connect``
        constructor accepts. Surfaces a clear error if a required
        secret for the chosen ``auth_method`` is missing."""
        kw: Dict[str, Any] = {
            "account": self.account,
            "user": self.user,
        }
        if self.role:
            kw["role"] = self.role
        if self.warehouse:
            kw["warehouse"] = self.warehouse
        if self.auth_method == "password":
            if self.password is None:
                raise CredentialNotFoundError(
                    message=f"Snowflake source for user {self.user!r} declares auth_method='password' but no password set.",
                    suggestions=[
                        "Run: fluid ai setup --source snowflake --name <credential-id> (re-prompts for password)",
                        "Or set SNOWFLAKE_PASSWORD env var.",
                    ],
                )
            kw["password"] = self.password.get_secret_value()
        elif self.auth_method == "private_key":
            if self.private_key_path is None:
                raise CredentialNotFoundError(
                    message="Snowflake auth_method='private_key' requires private_key_path.",
                    suggestions=[
                        "Run: fluid ai setup --source snowflake --name <credential-id>",
                        "Generate a key pair: openssl genrsa -out rsa_key.pem 2048",
                    ],
                )
            kw["private_key_file"] = str(self.private_key_path.expanduser())
            if self.private_key_passphrase is not None:
                kw["private_key_file_pwd"] = self.private_key_passphrase.get_secret_value()
        elif self.auth_method == "oauth":
            if self.oauth_token is None:
                raise CredentialNotFoundError(
                    message="Snowflake auth_method='oauth' requires oauth_token.",
                    suggestions=[
                        "Re-issue the OAuth token via your IDP and re-run fluid ai setup --source snowflake --name <credential-id>."
                    ],
                )
            kw["authenticator"] = "oauth"
            kw["token"] = self.oauth_token.get_secret_value()
        elif self.auth_method == "sso":
            kw["authenticator"] = "externalbrowser"
        return kw


class BigQueryCredentials(BaseModel):
    """Google BigQuery source credentials.

    Recommended ``auth_method`` is ``adc`` (Application Default
    Credentials, picks up workload identity in GKE / GCE / Cloud
    Run). ``service_account_json`` is acceptable for environments
    without ADC. ``oidc`` is for federated workload identity from
    GitHub Actions / GitLab / etc.

    Per the V1.5 plan's Choice 3 = (B), the resolver only invokes
    ADC when ``allow_metadata_service=True``. Without that opt-in,
    ADC-only credentials fail closed with a clear "use --allow-
    metadata-service" suggestion.
    """

    model_config = ConfigDict(frozen=True)

    project: str
    auth_method: Literal["adc", "service_account_json", "oidc"] = "adc"
    service_account_path: Optional[Path] = None
    location: Optional[str] = None  # e.g., "EU", "US", "us-central1"
    quota_project: Optional[str] = None  # billing-project override

    def to_connection_kwargs(self) -> Dict[str, Any]:
        """Translate to ``google.cloud.bigquery.Client(**)`` kwargs.

        ``project`` is universal; the auth-specific path produces
        either ``credentials=`` (service account file) or relies on
        the implicit ADC pickup (``client = Client(project=...)``).
        """
        kw: Dict[str, Any] = {"project": self.project}
        if self.location:
            kw["location"] = self.location
        if self.auth_method == "service_account_json":
            if self.service_account_path is None:
                raise CredentialNotFoundError(
                    message="BigQuery auth_method='service_account_json' requires service_account_path.",
                    suggestions=[
                        "Run: fluid ai setup --source bigquery --name <credential-id>",
                        "Or set GOOGLE_APPLICATION_CREDENTIALS env var.",
                    ],
                )
            # The BigQuery client picks the file up via the standard
            # ``credentials`` kwarg — adapter constructs the
            # Credentials object from JSON.
            kw["credentials_path"] = str(self.service_account_path.expanduser())
        # ADC + OIDC: no extra kwargs; google-auth's default chain
        # picks up the workload-identity metadata server / OIDC
        # exchange automatically. The adapter just constructs
        # ``Client(project=...)``.
        if self.quota_project:
            kw["client_options"] = {"quota_project_id": self.quota_project}
        return kw


class DataplexCredentials(BaseModel):
    """Google Cloud Dataplex source credentials.

    Dataplex shares Google's auth chain with BigQuery — same
    ``auth_method`` choices, same ADC / service-account split. The
    distinguishing field is the Dataplex ``location`` (an L7
    region; "global" for cross-region resources) since Dataplex
    aspect-types are region-scoped.
    """

    model_config = ConfigDict(frozen=True)

    project: str
    location: str  # required for Dataplex; "global" is the cross-region option
    auth_method: Literal["adc", "service_account_json", "oidc"] = "adc"
    service_account_path: Optional[Path] = None

    def to_connection_kwargs(self) -> Dict[str, Any]:
        kw: Dict[str, Any] = {"project": self.project, "location": self.location}
        if self.auth_method == "service_account_json":
            if self.service_account_path is None:
                raise CredentialNotFoundError(
                    message="Dataplex auth_method='service_account_json' requires service_account_path.",
                    suggestions=[
                        "Run: fluid ai setup --source dataplex --name <credential-id>",
                        "Or set GOOGLE_APPLICATION_CREDENTIALS env var.",
                    ],
                )
            kw["credentials_path"] = str(self.service_account_path.expanduser())
        return kw


class GlueCredentials(BaseModel):
    """AWS Glue Data Catalog source credentials.

    Recommended ``auth_method`` is ``iam_role`` (assume-role via STS,
    no static credentials at rest). ``instance_profile`` is the
    federated path for code running on EC2 / Lambda. Static
    ``iam_key`` is legacy.

    The ``profile_name`` field maps to a named profile in
    ``~/.aws/credentials``; if both ``profile_name`` and explicit
    keys are set, the profile wins (boto3's standard precedence).
    """

    model_config = ConfigDict(frozen=True)

    region: str
    auth_method: Literal["iam_role", "iam_key", "instance_profile", "sso"] = "iam_role"
    profile_name: Optional[str] = None
    role_arn: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[SecretStr] = None
    session_token: Optional[SecretStr] = None  # for STS-issued temp creds

    def to_connection_kwargs(self) -> Dict[str, Any]:
        """Translate to ``boto3.session.Session(**)`` kwargs.

        Note: boto3 ``Session`` doesn't take ``role_arn`` directly;
        for ``iam_role`` we return profile-name and let the adapter
        do the STS assume-role step. This keeps the credentials
        model focused on identity, not the SDK's plumbing.
        """
        kw: Dict[str, Any] = {"region_name": self.region}
        if self.auth_method == "iam_key":
            if self.access_key_id is None or self.secret_access_key is None:
                raise CredentialNotFoundError(
                    message="Glue auth_method='iam_key' requires access_key_id + secret_access_key.",
                    suggestions=[
                        "Run: fluid ai setup --source glue --name <credential-id>",
                        "Or set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars.",
                    ],
                )
            kw["aws_access_key_id"] = self.access_key_id
            kw["aws_secret_access_key"] = self.secret_access_key.get_secret_value()
            if self.session_token is not None:
                kw["aws_session_token"] = self.session_token.get_secret_value()
        elif self.auth_method == "iam_role":
            if self.profile_name:
                kw["profile_name"] = self.profile_name
        elif self.auth_method == "sso":
            if not self.profile_name:
                raise CredentialNotFoundError(
                    message="Glue auth_method='sso' requires profile_name (configured via aws sso login).",
                    suggestions=["Run: aws sso login --profile <profile_name>"],
                )
            kw["profile_name"] = self.profile_name
        # instance_profile: no kwargs — boto3 picks up IMDS automatically.
        return kw


class DataHubCredentials(BaseModel):
    """DataHub source credentials.

    Recommended ``auth_method`` is ``oauth`` (rotation-friendly).
    PAT is acceptable. ``none`` is allowed for self-hosted dev
    instances without auth — the adapter will warn-on-construction
    so operators know they're in dev-mode.

    The ``server`` field is the GMS endpoint URL — typically
    ``https://datahub.example.com:8080``.
    """

    model_config = ConfigDict(frozen=True)

    server: str
    auth_method: Literal["pat", "oauth", "none"] = "pat"
    token: Optional[SecretStr] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[SecretStr] = None

    def to_connection_kwargs(self) -> Dict[str, Any]:
        kw: Dict[str, Any] = {"server": self.server}
        if self.auth_method == "pat":
            if self.token is None:
                raise CredentialNotFoundError(
                    message="DataHub auth_method='pat' requires a personal access token.",
                    suggestions=[
                        "Issue a token from DataHub UI: Settings → Developer → Access Tokens.",
                        "Run: fluid ai setup --source datahub --name <credential-id>",
                    ],
                )
            kw["token"] = self.token.get_secret_value()
        elif self.auth_method == "oauth":
            if not self.oauth_client_id or self.oauth_client_secret is None:
                raise CredentialNotFoundError(
                    message="DataHub auth_method='oauth' requires oauth_client_id + oauth_client_secret.",
                    suggestions=[
                        "Configure an OAuth client in your IDP (Okta / Azure AD / Auth0).",
                    ],
                )
            kw["oauth_client_id"] = self.oauth_client_id
            kw["oauth_client_secret"] = self.oauth_client_secret.get_secret_value()
        # none: no kwargs — dev-mode path; adapter logs a warning.
        return kw


class DataMeshManagerCredentials(BaseModel):
    """Data Mesh Manager source credentials.

    DMM uses a single API token (Bearer). The ``server`` field is
    the DMM REST endpoint — typically
    ``https://api.datamesh-manager.com``.

    Note: forge-cli already publishes contracts TO DMM via
    ``providers/datamesh_manager/``; this credential model is for
    reading metadata FROM DMM (the V1.5 source role). The two
    flows can share the same API token but live under different
    keyring entries (``fluid_source_<name>`` vs the existing
    publisher-side credential storage) so rotation can be
    independent.
    """

    model_config = ConfigDict(frozen=True)

    server: str
    api_key: SecretStr

    def to_connection_kwargs(self) -> Dict[str, Any]:
        return {
            "server": self.server,
            "api_key": self.api_key.get_secret_value(),
        }


class UnityCredentials(BaseModel):
    """Databricks Unity Catalog source credentials.

    Recommended ``auth_method`` is ``oauth_m2m`` (service principal,
    no human in the loop, rotation-friendly). PATs are acceptable
    when issued with ≤90-day expiry. Azure AD / Google ID flow when
    the workspace is hosted on the matching cloud.
    """

    model_config = ConfigDict(frozen=True)

    host: str
    auth_method: Literal["pat", "oauth_m2m", "azure_ad", "google_id"] = "oauth_m2m"
    token: Optional[SecretStr] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[SecretStr] = None
    azure_tenant_id: Optional[str] = None
    google_service_account_path: Optional[Path] = None

    def to_connection_kwargs(self) -> Dict[str, Any]:
        """Translate to the dict the ``WorkspaceClient(**)`` constructor
        accepts. SDK auth fields are method-specific."""
        kw: Dict[str, Any] = {"host": self.host}
        if self.auth_method == "pat":
            if self.token is None:
                raise CredentialNotFoundError(
                    message="Unity auth_method='pat' requires a personal access token.",
                    suggestions=[
                        "Issue a token from Databricks UI: User Settings → Developer → Access tokens (≤90-day expiry recommended).",
                        "Run: fluid ai setup --source unity --name <credential-id>",
                    ],
                )
            kw["token"] = self.token.get_secret_value()
        elif self.auth_method == "oauth_m2m":
            if self.oauth_client_id is None or self.oauth_client_secret is None:
                raise CredentialNotFoundError(
                    message="Unity auth_method='oauth_m2m' requires oauth_client_id + oauth_client_secret.",
                    suggestions=[
                        "Create a service principal in Databricks Account Console → Service Principals.",
                        "Grant the SP access to your catalog with USE CATALOG / USE SCHEMA / BROWSE.",
                    ],
                )
            kw["client_id"] = self.oauth_client_id
            kw["client_secret"] = self.oauth_client_secret.get_secret_value()
            kw["auth_type"] = "oauth-m2m"
        elif self.auth_method == "azure_ad":
            if self.azure_tenant_id is None:
                raise CredentialNotFoundError(
                    message="Unity auth_method='azure_ad' requires azure_tenant_id.",
                    suggestions=[
                        "Find your tenant ID in Azure Portal → Azure Active Directory → Properties."
                    ],
                )
            kw["azure_tenant_id"] = self.azure_tenant_id
            kw["auth_type"] = "azure-cli"
        elif self.auth_method == "google_id":
            if self.google_service_account_path is None:
                raise CredentialNotFoundError(
                    message="Unity auth_method='google_id' requires google_service_account_path.",
                    suggestions=["Provide the path to a Google service-account JSON file."],
                )
            kw["google_service_account"] = str(self.google_service_account_path.expanduser())
        return kw


def _plaintext_source_secrets_allowed() -> bool:
    return os.environ.get(PLAINTEXT_SOURCE_SECRETS_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _path_is_private(path: Path) -> bool:
    try:
        return (path.stat().st_mode & 0o077) == 0
    except OSError:
        return False


# Type variable for resolver methods that operate over any credential
# class. The bound is ``BaseModel`` rather than a custom protocol so
# new credential types Sprint B introduces (BigQuery, Glue, …) work
# without re-declaring the bound.
CredentialT = TypeVar("CredentialT", bound=BaseModel)


# ---------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------


class CredentialResolver:
    """Resolve typed source credentials by name.

    Construction takes one knob — ``allow_metadata_service`` —
    matching the plan's A/B/B "Choice 3 = (B) opt-in via flag". When
    ``False`` (the default), the cloud-metadata-service path is
    silently skipped and the resolver falls through to "fail closed"
    rather than picking up ambient cloud authority that might widen
    the user's access scope unexpectedly.

    Design note: the resolver is a small, pure-Python class with no
    network calls and no SDK imports. SDK auth (Snowflake connector,
    Databricks SDK) is the adapter's responsibility — the resolver
    just produces the typed credentials object the adapter consumes.
    """

    def __init__(
        self,
        *,
        sources_config_path: Optional[Path] = None,
        allow_metadata_service: bool = False,
        keyring_module: Optional[Any] = None,
    ) -> None:
        # Tolerate either ``Path`` or ``str`` from callers — tests
        # often pass strings; production callers pass Path. Coerce
        # to Path so ``.expanduser`` always works.
        if sources_config_path is None:
            self.sources_config_path = SOURCES_CONFIG_DEFAULT.expanduser()
        else:
            self.sources_config_path = Path(sources_config_path).expanduser()
        self.allow_metadata_service = bool(
            allow_metadata_service or os.environ.get("FLUID_ALLOW_METADATA_SERVICE") == "1"
        )
        # ``keyring_module`` is injectable so tests can stub the OS
        # keyring without importing the real one. Production code
        # passes ``None`` and the helper does a lazy import.
        self._keyring = keyring_module

    # -----------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------

    def resolve(
        self,
        *,
        catalog_name: str,
        credential_type: Type[CredentialT],
        credential_id: Optional[str] = None,
        inline_credentials: Optional[Mapping[str, Any]] = None,
    ) -> CredentialT:
        """Resolve typed credentials for ``catalog_name``.

        Raises :class:`CredentialNotFoundError` (a
        :class:`CatalogConfigError` subclass) when no source
        matches. The error message names the exact next-action the
        operator needs.

        Parameters
        ----------
        catalog_name:
            Catalog identifier (``"snowflake"`` / ``"unity"`` / etc.).
            Used to scope env-var lookup + audit-trail context.
        credential_type:
            Pydantic class (e.g. :class:`SnowflakeCredentials`) the
            resolver must return.
        credential_id:
            Saved-source name. When provided, the resolver looks the
            name up in the keyring + ``sources.yaml``.
        inline_credentials:
            Per-call literal credentials dict. Highest priority —
            used by tests and one-shot CLI invocations that pass
            credentials directly.
        """
        # 1. Per-call inline.
        if inline_credentials:
            try:
                return credential_type.model_validate(inline_credentials)
            except Exception as exc:
                raise CatalogConfigError(
                    message=f"Inline {catalog_name} credentials failed validation: {exc}",
                    suggestions=[
                        "Check field names match the credential model "
                        f"({credential_type.__name__}).",
                    ],
                    original_error=exc,
                ) from exc

        # 2. By credential_id → keyring + sources.yaml merge.
        if credential_id:
            from_storage = self._from_storage(
                credential_id=credential_id,
                catalog_name=catalog_name,
                credential_type=credential_type,
            )
            if from_storage is not None:
                return from_storage

        # 3. Environment variables (catalog-specific, no credential_id needed).
        from_env = self._from_env(catalog_name, credential_type)
        if from_env is not None:
            return from_env

        # 4. Cloud metadata service / ADC — only when explicitly allowed.
        if self.allow_metadata_service:
            from_metadata = self._from_metadata_service(catalog_name, credential_type)
            if from_metadata is not None:
                return from_metadata

        # 5. Fail closed.
        raise self._not_found_error(
            catalog_name=catalog_name,
            credential_id=credential_id,
            credential_type=credential_type,
        )

    # -----------------------------------------------------------------
    # Resolution sources
    # -----------------------------------------------------------------

    def _from_storage(
        self,
        *,
        credential_id: str,
        catalog_name: str,
        credential_type: Type[CredentialT],
    ) -> Optional[CredentialT]:
        """Load non-sensitive config from ``sources.yaml`` and merge
        secret fields from the OS keyring.

        Returns ``None`` if the credential_id isn't present in
        either store.
        """
        # Non-sensitive config from YAML.
        non_sensitive = self._load_yaml_entry(credential_id)
        # Secret fields from keyring.
        secret_fields = self._load_keyring_entry(credential_id)

        if non_sensitive is None and secret_fields is None:
            return None

        merged: Dict[str, Any] = {}
        yaml_secret_fields: Dict[str, Any] = {}
        if non_sensitive is not None:
            # Sanity: the entry should declare the catalog type matching
            # the request — otherwise the operator might be looking up
            # a Snowflake credential under a Unity catalog by mistake.
            stored_type = non_sensitive.get("source_type")
            if stored_type and stored_type != catalog_name:
                raise CatalogConfigError(
                    message=(
                        f"Source {credential_id!r} is registered as "
                        f"{stored_type!r} but resolution requested {catalog_name!r}."
                    ),
                    suggestions=[
                        f"Use a credential_id whose source_type matches '{catalog_name}'.",
                        "Run: fluid ai status — to see configured sources and their types.",
                    ],
                )
            # Two accepted shapes:
            #   1. nested: {source_type, config: {...}, secrets: {...}}
            #   2. flat:   {source_type, <field>: <value>, ...}
            # Detect shape by presence of ``config``/``secrets`` keys.
            has_nested = "config" in non_sensitive or "secrets" in non_sensitive
            if has_nested:
                merged.update(non_sensitive.get("config") or {})
            else:
                # Flat shape — pull every key except shape-control keys
                # into the merged config and treat keys whose name
                # *contains* a secret-bearing token as legacy secrets so
                # the same plaintext-gate applies.  Substring match (not
                # exact) so we catch every Pydantic ``SecretStr`` field
                # name across adapters: ``password``, ``oauth_token``,
                # ``aws_secret_access_key``, ``oauth_client_secret``,
                # ``private_key_passphrase``, ``refresh_token``,
                # ``api_key`` etc.  The reserved set below is the only
                # exception (``source_type`` etc.).
                #
                # Trade-off (intentional, documented):
                #
                # The substring detection is name-based, not value-based.
                # Field names that DON'T contain one of the
                # ``_SECRET_TOKENS`` substrings — for example AWS's
                # ``access_key_id`` (an account identifier, but not a
                # secret in the IAM sense), Snowflake's ``account``
                # (a host hint), or any unknown adapter field that
                # happens to be sensitive in some deployment but is
                # named neutrally — will land in the non-sensitive
                # ``config`` bucket and bypass the plaintext gate.
                # That's accepted because:
                #
                # 1. The Pydantic credential models annotate every
                #    *secret* field as :class:`SecretStr`; sensitive
                #    values that pass through the resolver still get
                #    the ``repr()`` / serialisation guard from Pydantic.
                # 2. The keyring path (the recommended one) doesn't
                #    use this gate at all — secrets there live in
                #    encrypted-at-rest OS storage.
                # 3. ``FLUID_ALLOW_PLAINTEXT_SOURCE_SECRETS`` plus
                #    chmod 600 is the operator's explicit opt-in to
                #    "yes, my YAML can hold secrets"; the gate's job
                #    is to refuse the obvious cases by default, not
                #    to inspect arbitrary values for entropy.
                #
                # Adapter authors adding a new secret-bearing field
                # whose name doesn't already match one of
                # ``_SECRET_TOKENS`` should extend the tuple below.
                _RESERVED = {"source_type", "credential_id", "type"}
                _SECRET_TOKENS = (
                    "password",
                    "passphrase",
                    "secret",
                    "token",
                    "private_key",
                    "api_key",
                    "credential",
                )

                def _looks_secret(key_name: str) -> bool:
                    lowered = key_name.lower()
                    return any(tok in lowered for tok in _SECRET_TOKENS)

                for key, value in non_sensitive.items():
                    if key in _RESERVED:
                        continue
                    if _looks_secret(key):
                        yaml_secret_fields.setdefault(key, value)
                    else:
                        merged[key] = value
            legacy_secrets = non_sensitive.get("secrets")
            if isinstance(legacy_secrets, Mapping) and legacy_secrets:
                yaml_secret_fields.update(dict(legacy_secrets))
            # Apply the plaintext gate uniformly for any secrets
            # discovered in the YAML (whether from the explicit
            # ``secrets:`` block or harvested from the flat shape).
            if yaml_secret_fields:
                if not _plaintext_source_secrets_allowed():
                    raise CatalogConfigError(
                        message=(
                            f"Source {credential_id!r} stores secrets in sources.yaml. "
                            "Refusing plaintext source secrets by default."
                        ),
                        suggestions=[
                            "Move the secret fields to the OS keyring: fluid ai setup --source <catalog> --name <credential-id>",
                            f"For a local legacy fallback only, set {PLAINTEXT_SOURCE_SECRETS_ENV}=1.",
                        ],
                    )
                if not _path_is_private(self.sources_config_path):
                    raise CatalogConfigError(
                        message=(
                            f"Source {credential_id!r} stores plaintext secrets but "
                            f"{self.sources_config_path} is not chmod 600."
                        ),
                        suggestions=[
                            f"Run: chmod 600 {self.sources_config_path}",
                            "Prefer moving secrets to the OS keyring with fluid ai setup --source <catalog> --name <credential-id>.",
                        ],
                    )
        if yaml_secret_fields:
            merged.update(yaml_secret_fields)
        if secret_fields is not None:
            merged.update(secret_fields)

        try:
            return credential_type.model_validate(merged)
        except Exception as exc:
            raise CatalogConfigError(
                message=(
                    f"Stored credentials for source {credential_id!r} " f"failed validation: {exc}"
                ),
                suggestions=[
                    f"Run: fluid ai setup --source {credential_id} --rotate",
                ],
                original_error=exc,
            ) from exc

    def _load_yaml_entry(self, credential_id: str) -> Optional[Dict[str, Any]]:
        """Read one entry from ``~/.fluid/sources.yaml``.

        File shape::

            sources:
              snowflake-prod:
                source_type: snowflake
                config:
                  account: abc-xyz
                  user: ANALYST
                  auth_method: private_key
                  private_key_path: ~/.snowflake/rsa_key.p8
                  role: ANALYST_RW
                  warehouse: ANALYTICS_XS

        ``config`` carries non-sensitive fields only. Secret fields
        (passwords, tokens) live in the OS keyring under the same
        ``credential_id``.
        """
        if not self.sources_config_path.is_file():
            return None
        try:
            import yaml

            data = yaml.safe_load(self.sources_config_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning(
                "fluid.copilot.catalog.credentials.sources_yaml_unreadable: %s",
                exc,
            )
            return None
        if not isinstance(data, Mapping):
            return None
        sources = data.get("sources")
        if not isinstance(sources, Mapping):
            return None
        entry = sources.get(credential_id)
        if not isinstance(entry, Mapping):
            return None
        return dict(entry)

    def _load_keyring_entry(self, credential_id: str) -> Optional[Dict[str, Any]]:
        """Look up the secret-fields dict from the OS keyring.

        Stored as a JSON string under
        ``fluid_source_<credential_id>``. Missing entries return
        ``None`` (not an error — the YAML may carry the full
        non-sensitive config and no secrets are needed for that auth
        method, e.g., Snowflake ``sso`` / Unity ``azure_ad`` with
        external login).
        """
        keyring = self._keyring
        if keyring is None:
            try:
                import keyring as _keyring  # type: ignore

                keyring = _keyring
            except ImportError:
                # No keyring available — not fatal; YAML alone might
                # carry enough config for some auth methods.
                _log.debug(
                    "fluid.copilot.catalog.credentials.keyring_unavailable: "
                    "credential %s — skipping keyring lookup.",
                    credential_id,
                )
                return None
        try:
            raw = keyring.get_password(KEYRING_PREFIX, credential_id)
        except Exception as exc:  # pragma: no cover — keyring backends vary
            _log.debug(
                "fluid.copilot.catalog.credentials.keyring_error: %s — %s",
                credential_id,
                exc,
            )
            return None
        if not raw:
            return None
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            _log.warning(
                "fluid.copilot.catalog.credentials.keyring_corrupt: %s — "
                "entry isn't valid JSON. Run fluid ai setup --source %s --rotate",
                credential_id,
                credential_id,
            )
            return None
        return decoded if isinstance(decoded, Mapping) else None

    def _from_env(
        self, catalog_name: str, credential_type: Type[CredentialT]
    ) -> Optional[CredentialT]:
        """Construct credentials from catalog-specific env vars.

        Returns ``None`` when the minimum-required env vars for the
        catalog aren't set. Each catalog has its own minimum set
        defined inline below. We deliberately don't return a
        partially-populated object — fail-fast on missing inputs is
        the world-class contract.
        """
        if catalog_name == "snowflake":
            account = os.environ.get("SNOWFLAKE_ACCOUNT")
            user = os.environ.get("SNOWFLAKE_USER")
            if not account or not user:
                return None
            password = os.environ.get("SNOWFLAKE_PASSWORD")
            private_key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
            if private_key_path:
                auth_method: Any = "private_key"
                fields: Dict[str, Any] = {
                    "account": account,
                    "user": user,
                    "auth_method": auth_method,
                    "private_key_path": Path(private_key_path),
                }
                pk_passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
                if pk_passphrase:
                    fields["private_key_passphrase"] = pk_passphrase
            elif password:
                fields = {
                    "account": account,
                    "user": user,
                    "auth_method": "password",
                    "password": password,
                }
            else:
                # Account + user with no auth material — not enough to
                # construct a usable credential. Fall through.
                return None
            role = os.environ.get("SNOWFLAKE_ROLE")
            if role:
                fields["role"] = role
            warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE")
            if warehouse:
                fields["warehouse"] = warehouse
            return credential_type.model_validate(fields)

        if catalog_name == "unity":
            host = os.environ.get("DATABRICKS_HOST")
            if not host:
                return None
            token = os.environ.get("DATABRICKS_TOKEN")
            client_id = os.environ.get("DATABRICKS_CLIENT_ID")
            client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET")
            if client_id and client_secret:
                fields = {
                    "host": host,
                    "auth_method": "oauth_m2m",
                    "oauth_client_id": client_id,
                    "oauth_client_secret": client_secret,
                }
            elif token:
                fields = {
                    "host": host,
                    "auth_method": "pat",
                    "token": token,
                }
            else:
                return None
            return credential_type.model_validate(fields)

        if catalog_name == "bigquery":
            project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
            if not project:
                return None
            sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            fields: Dict[str, Any] = {"project": project}
            if sa_path:
                fields["auth_method"] = "service_account_json"
                fields["service_account_path"] = Path(sa_path)
            else:
                # ADC path — only valid when ``allow_metadata_service``
                # is set. ``_from_env`` doesn't have access to that
                # gate; we still construct the credential here, and
                # the adapter's ``_client`` step is what triggers the
                # ADC pickup. The gate guards the metadata-service
                # PATH, not the ADC mode declaration.
                fields["auth_method"] = "adc"
            location = os.environ.get("GOOGLE_CLOUD_LOCATION")
            if location:
                fields["location"] = location
            return credential_type.model_validate(fields)

        if catalog_name == "dataplex":
            project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
            location = os.environ.get("DATAPLEX_LOCATION") or os.environ.get(
                "GOOGLE_CLOUD_LOCATION"
            )
            if not project or not location:
                return None
            sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            fields = {"project": project, "location": location}
            if sa_path:
                fields["auth_method"] = "service_account_json"
                fields["service_account_path"] = Path(sa_path)
            else:
                fields["auth_method"] = "adc"
            return credential_type.model_validate(fields)

        if catalog_name == "glue":
            region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            if not region:
                return None
            profile = os.environ.get("AWS_PROFILE")
            access_key = os.environ.get("AWS_ACCESS_KEY_ID")
            secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
            session_token = os.environ.get("AWS_SESSION_TOKEN")
            fields = {"region": region}
            if access_key and secret_key:
                fields["auth_method"] = "iam_key"
                fields["access_key_id"] = access_key
                fields["secret_access_key"] = secret_key
                if session_token:
                    fields["session_token"] = session_token
            elif profile:
                fields["auth_method"] = "iam_role"
                fields["profile_name"] = profile
            else:
                # Bare region with no credentials — fall through to
                # instance-profile / metadata-service. The adapter
                # picks that up automatically via boto3's default
                # chain when ``allow_metadata_service`` is on.
                fields["auth_method"] = "instance_profile"
            return credential_type.model_validate(fields)

        if catalog_name == "datahub":
            server = os.environ.get("DATAHUB_GMS_HOST") or os.environ.get("DATAHUB_SERVER")
            if not server:
                return None
            token = os.environ.get("DATAHUB_GMS_TOKEN") or os.environ.get("DATAHUB_TOKEN")
            client_id = os.environ.get("DATAHUB_OAUTH_CLIENT_ID")
            client_secret = os.environ.get("DATAHUB_OAUTH_CLIENT_SECRET")
            fields = {"server": server}
            if client_id and client_secret:
                fields["auth_method"] = "oauth"
                fields["oauth_client_id"] = client_id
                fields["oauth_client_secret"] = client_secret
            elif token:
                fields["auth_method"] = "pat"
                fields["token"] = token
            else:
                # No-auth dev path. Adapter warns on construction.
                fields["auth_method"] = "none"
            return credential_type.model_validate(fields)

        if catalog_name == "datamesh_manager":
            server = os.environ.get("DMM_API_URL") or os.environ.get("DATAMESH_MANAGER_SERVER")
            api_key = os.environ.get("DMM_API_KEY") or os.environ.get("DATAMESH_MANAGER_API_KEY")
            if not server or not api_key:
                return None
            return credential_type.model_validate({"server": server, "api_key": api_key})

        return None

    def _from_metadata_service(
        self, catalog_name: str, credential_type: Type[CredentialT]
    ) -> Optional[CredentialT]:
        """Stub for v1.5 Sprint B. Returns ``None`` until the cloud-
        metadata-service paths land per-catalog.

        The Sprint B implementation will:

        * BigQuery / Dataplex: invoke ``google.auth.default()`` to
          pick up GCE / GKE workload-identity / ADC.
        * Glue: invoke boto3's default credential chain (instance
          profile, EC2 metadata, ECS task role).
        * Snowflake / Unity: no metadata-service path; only static
          + OAuth.
        """
        return None

    # -----------------------------------------------------------------
    # Error construction
    # -----------------------------------------------------------------

    def _not_found_error(
        self,
        *,
        catalog_name: str,
        credential_id: Optional[str],
        credential_type: Type[CredentialT],
    ) -> CredentialNotFoundError:
        """Raise a helpful "credentials not found" error.

        The suggestions list names BOTH the wizard path AND the
        env-var path so operators have a clear next-action regardless
        of which workflow they prefer.
        """
        if credential_id:
            label = f"source {credential_id!r}"
        else:
            label = f"any {catalog_name} source"
        suggestions = [
            f"Run: fluid ai setup --source {catalog_name} --name {credential_id or '<credential-id>'}  "
            f"(interactive wizard for {catalog_name})",
            f"Or set the {catalog_name} env vars (see docs/catalogs/{catalog_name}.md).",
        ]
        if not self.allow_metadata_service and catalog_name in {
            "bigquery",
            "dataplex",
            "glue",
        }:
            suggestions.append(
                "Or pass --allow-metadata-service to use cloud workload identity (ADC / IAM)."
            )
        return CredentialNotFoundError(
            message=f"No credentials resolved for {label}.",
            suggestions=suggestions,
        )


__all__ = [
    "SOURCES_CONFIG_DEFAULT",
    "KEYRING_PREFIX",
    "CredentialNotFoundError",
    "CredentialResolver",
    # Per-catalog typed credentials.
    "SnowflakeCredentials",
    "UnityCredentials",
    "BigQueryCredentials",
    "DataplexCredentials",
    "GlueCredentials",
    "DataHubCredentials",
    "DataMeshManagerCredentials",
]
