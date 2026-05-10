# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Acquisition-runner credential layer — delegates to OSS, doesn't reinvent.

Architecture (per /borrow-before-build receipts):

- **Layered config**: ``pydantic_settings.BaseSettings`` handles the
  init→env→.env→secrets-dir→defaults precedence chain. We don't roll our
  own ``setdefault_env`` machinery.
  Docs: https://docs.pydantic.dev/latest/concepts/pydantic_settings/

- **Auth flow detection**: each engine SDK does it itself. dlt's
  destination ``credentials_class`` is a discriminated union; populate the
  fields that apply and dlt picks the flow. PyAirbyte/Airbyte source specs
  declare auth via ``oneOf``; the connector itself dispatches. Forge-cli
  doesn't replicate this logic — it just gathers the values.

- **Credential bridging**: each engine has its OWN env-var convention
  (dlt's ``DESTINATION__X__CREDENTIALS__Y``, Meltano's ``TARGET_X__Y``,
  …). The per-engine introspector in ``<engine>/destinations.py`` reads
  the engine SDK's expected fields and writes them. Forge-cli's job is the
  ~50-line FLUID-canonical → engine-canonical alias table here, nothing
  more.

- **Cloud-native chains**: when an SDK ALREADY has a credential chain
  (``google.auth.default()``, ``botocore.session.Session().get_credentials()``,
  ``azure.identity.DefaultAzureCredential()``, AWS STS assume-role), let
  it run. Don't re-implement IAM role assumption, ADC, or workload
  identity here — those are 10k-star libraries with edge cases we'd never
  catch.

The forge-cli-owned surface is small and stable:

1. Per-platform ``BaseSettings`` subclass with FLUID-canonical field names
   and env-var aliases.
2. ``get_credentials_for(platform, binding)`` — merges binding override
   onto resolved settings.
3. ``make_destination(engine, platform, ...)`` — dispatches to the
   per-engine introspector registered in ``<engine>/destinations.py``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Type

try:
    from pydantic import AliasChoices, Field
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover — defensive; pydantic-settings is a hard dep
    _HAS_PYDANTIC_SETTINGS = False


# ── FLUID-canonical credential schemas (per platform) ──────────────────
#
# Each subclass declares the fields the engine SDKs care about, with
# AliasChoices for cross-convention env-var compatibility (e.g. GCP_PROJECT
# / GOOGLE_CLOUD_PROJECT / BIGQUERY_PROJECT all populate ``project_id``).
# The env_prefix establishes the FLUID-canonical convention for each
# platform's "obvious" env vars (SNOWFLAKE_*, AWS_*, AZURE_*); per-field
# aliases handle the exceptions (ADC path is unprefixed, SSO env vars
# follow IAM-Identity-Center conventions, etc.).
#
# All subclasses set ``extra='ignore'`` so unrelated env vars in the
# operator's shell don't crash instantiation.


if _HAS_PYDANTIC_SETTINGS:

    class _Common:
        """Common SettingsConfigDict defaults for every credential class."""
        model_config = SettingsConfigDict(
            case_sensitive=False,
            extra="ignore",
            env_file_encoding="utf-8",
        )


    class SnowflakeCredentials(BaseSettings):
        """FLUID-canonical Snowflake credentials.

        Env-var convention: ``SNOWFLAKE_<FIELD>``. Engine adapters translate
        to whatever the SDK calls things (e.g. dlt's snowflake destination
        calls ``account`` ``host`` — the ``_dlt_introspect`` adapter does
        that rename, not this class).

        Multiple auth flows are supported simultaneously by populating the
        relevant fields; the SDK picks based on which are non-None:

        - **password**: ``user`` + ``password``
        - **keypair**:  ``user`` + ``private_key_path`` (+ optional
          ``private_key_passphrase``)
        - **oauth**:    ``user`` + ``oauth_token`` + ``authenticator='oauth'``
        - **SSO/external browser**: ``user`` + ``authenticator='externalbrowser'``
        """

        model_config = SettingsConfigDict(
            env_prefix="SNOWFLAKE_",
            case_sensitive=False,
            extra="ignore",
        )

        account: Optional[str] = None
        user: Optional[str] = None
        password: Optional[str] = None
        database: Optional[str] = None
        warehouse: Optional[str] = None
        role: Optional[str] = None
        private_key_path: Optional[str] = None
        private_key_passphrase: Optional[str] = None
        oauth_token: Optional[str] = None
        authenticator: Optional[str] = None


    class BigQueryCredentials(BaseSettings):
        """FLUID-canonical BigQuery / GCP credentials.

        For Application Default Credentials (ADC) — the recommended GCP
        auth mode — leave most fields unset and point
        ``GOOGLE_APPLICATION_CREDENTIALS`` at a service-account JSON.
        ``google.auth.default()`` (called by dlt's bigquery destination
        and PyAirbyte's bigquery cache) handles the chain from there
        (env → GCS metadata service → workload identity → user creds).
        """

        model_config = SettingsConfigDict(
            case_sensitive=False,
            extra="ignore",
        )

        project_id: Optional[str] = Field(
            default=None,
            validation_alias=AliasChoices(
                "GCP_PROJECT",
                "GOOGLE_CLOUD_PROJECT",
                "BIGQUERY_PROJECT",
                "PROJECT_ID",
            ),
        )
        google_application_credentials: Optional[str] = Field(
            default=None,
            validation_alias=AliasChoices(
                "GOOGLE_APPLICATION_CREDENTIALS",
                "BIGQUERY_KEYFILE_PATH",
            ),
        )
        location: Optional[str] = Field(
            default=None,
            validation_alias=AliasChoices("BIGQUERY_LOCATION", "GCP_LOCATION"),
        )


    class RedshiftCredentials(BaseSettings):
        """FLUID-canonical Redshift credentials. Env-var convention: ``REDSHIFT_<FIELD>``."""

        model_config = SettingsConfigDict(
            env_prefix="REDSHIFT_",
            case_sensitive=False,
            extra="ignore",
        )

        host: Optional[str] = None
        port: Optional[int] = None
        database: Optional[str] = None
        user: Optional[str] = None
        password: Optional[str] = None
        cluster_identifier: Optional[str] = None  # for IAM-based auth
        iam_role_arn: Optional[str] = None        # for IAM-based auth


    class PostgresCredentials(BaseSettings):
        """FLUID-canonical Postgres credentials. Honours both ``POSTGRES_*``
        (Postgres-official) and ``PG_*`` (lab convention) env-var prefixes.
        """

        model_config = SettingsConfigDict(
            case_sensitive=False,
            extra="ignore",
        )

        host: Optional[str] = Field(default=None, validation_alias=AliasChoices("POSTGRES_HOST", "PG_HOST"))
        port: Optional[int] = Field(default=None, validation_alias=AliasChoices("POSTGRES_PORT", "PG_PORT"))
        database: Optional[str] = Field(default=None, validation_alias=AliasChoices("POSTGRES_DATABASE", "POSTGRES_DB", "PG_DATABASE", "PG_DB"))
        user: Optional[str] = Field(default=None, validation_alias=AliasChoices("POSTGRES_USER", "PG_USER"))
        password: Optional[str] = Field(default=None, validation_alias=AliasChoices("POSTGRES_PASSWORD", "PG_PASSWORD"))


    class AwsCredentials(BaseSettings):
        """FLUID-canonical AWS credentials.

        For static creds: populate ``access_key_id`` + ``secret_access_key``
        (+ optional ``session_token`` for STS). For role assumption: leave
        static creds unset and populate ``role_arn``; ``botocore`` picks
        up the chain (assume role from profile / EC2 instance role / etc.).

        For Workload Identity / IRSA / Lambda execution role: leave
        EVERYTHING unset and let ``botocore.session`` find the creds via
        IMDS / Web Identity Token File.
        """

        model_config = SettingsConfigDict(
            env_prefix="AWS_",
            case_sensitive=False,
            extra="ignore",
        )

        access_key_id: Optional[str] = None
        secret_access_key: Optional[str] = None
        session_token: Optional[str] = None
        region: Optional[str] = None
        role_arn: Optional[str] = None
        profile: Optional[str] = None  # named profile in ~/.aws/credentials


    class AzureCredentials(BaseSettings):
        """FLUID-canonical Azure credentials.

        For service-principal auth: populate ``tenant_id`` + ``client_id`` +
        ``client_secret``. For Managed Identity / Workload Identity /
        Azure CLI: leave them unset and let ``azure.identity.DefaultAzureCredential()``
        find them (it walks env → workload identity → managed identity →
        CLI → developer credentials).
        """

        model_config = SettingsConfigDict(
            env_prefix="AZURE_",
            case_sensitive=False,
            extra="ignore",
        )

        tenant_id: Optional[str] = None
        client_id: Optional[str] = None
        client_secret: Optional[str] = None
        subscription_id: Optional[str] = None
        sas_token: Optional[str] = None
        storage_account_name: Optional[str] = None
        storage_account_key: Optional[str] = None


    # Registry: platform name → credentials class. Adding a new platform =
    # one new class above + one entry here. Engine introspectors that need
    # creds for the platform read from the resolved instance.
    _CREDENTIAL_CLASSES: Dict[str, Type[BaseSettings]] = {
        "snowflake": SnowflakeCredentials,
        "bigquery": BigQueryCredentials,
        "redshift": RedshiftCredentials,
        "postgres": PostgresCredentials,
        "aws": AwsCredentials,
        "azure": AzureCredentials,
        # s3 / gcs / azure-blob aliases share the underlying cloud creds
        "s3": AwsCredentials,
        "gcs": BigQueryCredentials,  # GCS uses GCP auth
        "azure-blob": AzureCredentials,
    }
else:
    _CREDENTIAL_CLASSES = {}


def get_credentials_for(
    platform: str, binding: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Return resolved credentials for ``platform`` as a flat ``dict``.

    Resolution order (highest precedence first):

    1. ``binding.location.<field>`` — contract-author override (lets a
       contract pin to e.g. a specific dev account).
    2. FLUID env vars via pydantic-settings — operator's shell / .env /
       secrets-dir.
    3. Field defaults (typically ``None`` — engine SDK then surfaces a
       missing-credential error).

    ``None`` values are stripped from the returned dict so engine introspectors
    can use ``setdefault``-style population without overwriting with None.
    """
    cls = _CREDENTIAL_CLASSES.get(platform.lower())
    if cls is None:
        # Unknown platform — return whatever the binding supplied, no env
        # resolution. Engine introspector can still attempt construction
        # with whatever the contract author put in binding.location.
        return dict((binding or {}).get("location", {}) or {})

    settings = cls()
    fields = {k: v for k, v in settings.model_dump(exclude_none=True).items()}
    # Binding overrides env (contract author override always wins).
    binding_loc = (binding or {}).get("location") or {}
    for k, v in binding_loc.items():
        if v is not None and v != "":
            fields[k] = v
    return fields


# ── Per-engine introspector registry ──────────────────────────────────
#
# Each acquisition engine has ONE introspector (NOT one factory per
# destination). The introspector receives the resolved FLUID credentials
# and is responsible for calling its engine SDK with the right shape:
#
#   - dlt:     mutate ``DESTINATION__X__CREDENTIALS__Y`` env vars
#   - airbyte: construct ``ab.caches.<X>Cache(...)`` instance
#   - meltano: build target-plugin config dict for meltano.yml
#   - debezium / kafka_connect: build sink-connector config dict
#   - duckdb:  emit ``CREATE SECRET`` SQL for object stores
#
# The introspector uses each engine's OWN field-name discovery (dlt's
# credentials_class.__dataclass_fields__, PyAirbyte's __init__ signature,
# Meltano's plugin manifest, …) so adding a new destination = zero code
# in most cases (engine SDK upgrade picks it up automatically).

_ENGINE_INTROSPECTORS: Dict[str, Callable[..., Any]] = {}


def register_engine_introspector(
    engine: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register the per-engine destination introspector.

    Engines provide one introspector each in ``<engine>/destinations.py``;
    that module is imported as a side-effect from the engine's
    ``__init__.py`` so registration fires when the runner loads.

    Example
    -------
    >>> @register_engine_introspector("dlt")
    ... def _dlt_introspect(*, platform, credentials, binding, contract, product_id):
    ...     ...  # populate DESTINATION__<X>__CREDENTIALS__* env vars
    """
    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        _ENGINE_INTROSPECTORS[engine.lower()] = fn
        return fn

    return _wrap


def make_destination(
    engine: str,
    platform: str,
    *,
    binding: Optional[Dict[str, Any]] = None,
    contract: Optional[Dict[str, Any]] = None,
    product_id: str = "",
) -> Any:
    """Dispatch to ``engine``'s introspector with FLUID-resolved credentials.

    Returns whatever the engine's introspector produces:

    - dlt: ``None`` (env-var side effect)
    - airbyte: a ``ab.caches.<X>Cache`` instance
    - meltano: a config dict for meltano.yml
    - debezium / kafka_connect: a sink-connector config dict
    - duckdb: a ``CREATE SECRET`` SQL string OR ``None`` for local files

    No introspector registered for ``engine`` → returns ``None`` (engine
    runner can fall back to whatever default it considers safe).
    """
    introspector = _ENGINE_INTROSPECTORS.get(engine.lower())
    if introspector is None:
        return None
    credentials = get_credentials_for(platform, binding=binding)
    return introspector(
        platform=platform,
        credentials=credentials,
        binding=binding or {},
        contract=contract or {},
        product_id=product_id,
    )
