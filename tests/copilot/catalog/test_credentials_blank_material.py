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

"""A blank credential value is no credential, in the model and at the SDK.

boto3 reads an empty access key as "none supplied" and continues down its
default chain, which on a cloud host ends at the container or instance role.
google-auth and the Databricks SDK have default chains of their own. So a blank
or whitespace-only key, secret, token or service-account path must never reach
an SDK as if it were credential material: the models record it as absent, and
the auth method's own "requires ..." check refuses it.

:func:`selects_host_identity` is the classification the MCP server's inline
credential gate uses. It fails closed: only a block that carries non-blank
material of its own counts as the caller's.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Type

import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from fluid_build.copilot.catalog.credentials import (
    PLAINTEXT_SOURCE_SECRETS_ENV,
    BigQueryCredentials,
    CredentialNotFoundError,
    CredentialResolver,
    DataHubCredentials,
    DataMeshManagerCredentials,
    DataplexCredentials,
    GlueCredentials,
    SnowflakeCredentials,
    UnityCredentials,
)
from tests._infrastructure.aws_endpoint_logger import AwsEndpointLogger, point_boto3_at


def selects_host_identity(credentials: BaseModel) -> Any:
    # Resolved per call, so each case below reports on its own.
    from fluid_build.copilot.catalog import credentials as module

    return module.selects_host_identity(credentials)


@pytest.fixture(autouse=True)
def _bare_home(tmp_path, monkeypatch):
    """The classification reads this host's netrc and DataHub settings; use none."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("NETRC", "DATAHUB_CLIENT_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)


_BLANKS = [pytest.param("", id="empty"), pytest.param(" \t\n ", id="whitespace")]

_TEST_KEY_ID = "test-access-key-id"
_TEST_SECRET = "test-secret-access-key"  # pragma: allowlist secret

_GLUE_KEY = {"region": "us-east-1", "auth_method": "iam_key"}
_BQ_SA = {"project": "proj", "auth_method": "service_account_json"}
_DATAPLEX_SA = {"project": "proj", "location": "eu", "auth_method": "service_account_json"}
_UNITY = {"host": "https://x.cloud.databricks.com"}
_SNOWFLAKE = {"account": "acct", "user": "analyst"}
_DATAHUB = {"server": "https://datahub.example.com"}

# Every field that carries credential material, or names where it lives.
_MATERIAL_FIELDS = [
    pytest.param(GlueCredentials, _GLUE_KEY, "access_key_id", id="glue-access_key_id"),
    pytest.param(GlueCredentials, _GLUE_KEY, "secret_access_key", id="glue-secret_access_key"),
    pytest.param(GlueCredentials, _GLUE_KEY, "session_token", id="glue-session_token"),
    pytest.param(GlueCredentials, _GLUE_KEY, "profile_name", id="glue-profile_name"),
    pytest.param(BigQueryCredentials, _BQ_SA, "service_account_path", id="bigquery-sa-path"),
    pytest.param(DataplexCredentials, _DATAPLEX_SA, "service_account_path", id="dataplex-sa-path"),
    pytest.param(UnityCredentials, _UNITY, "token", id="unity-token"),
    pytest.param(UnityCredentials, _UNITY, "oauth_client_id", id="unity-oauth_client_id"),
    pytest.param(UnityCredentials, _UNITY, "oauth_client_secret", id="unity-oauth_client_secret"),
    pytest.param(UnityCredentials, _UNITY, "azure_tenant_id", id="unity-azure_tenant_id"),
    pytest.param(
        UnityCredentials, _UNITY, "google_service_account_path", id="unity-google-sa-path"
    ),
    pytest.param(SnowflakeCredentials, _SNOWFLAKE, "password", id="snowflake-password"),
    pytest.param(SnowflakeCredentials, _SNOWFLAKE, "private_key_path", id="snowflake-key-path"),
    pytest.param(
        SnowflakeCredentials, _SNOWFLAKE, "private_key_passphrase", id="snowflake-passphrase"
    ),
    pytest.param(SnowflakeCredentials, _SNOWFLAKE, "oauth_token", id="snowflake-oauth_token"),
    pytest.param(DataHubCredentials, _DATAHUB, "token", id="datahub-token"),
    pytest.param(DataHubCredentials, _DATAHUB, "oauth_client_id", id="datahub-oauth_client_id"),
    pytest.param(
        DataHubCredentials, _DATAHUB, "oauth_client_secret", id="datahub-oauth_client_secret"
    ),
]


def _secret(value: Optional[Any]) -> Optional[str]:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return None if value is None else str(value)


class TestBlankValueIsAbsent:
    @pytest.mark.parametrize("model, base, field", _MATERIAL_FIELDS)
    @pytest.mark.parametrize("blank", _BLANKS)
    def test_blank_reads_as_not_supplied(
        self, model: Type[BaseModel], base: Dict[str, Any], field: str, blank: str
    ):
        creds = model.model_validate({**base, field: blank})
        assert getattr(creds, field) is None

    def test_a_blank_secretstr_is_absent_too(self):
        creds = GlueCredentials(
            region="us-east-1",
            auth_method="iam_key",
            access_key_id=_TEST_KEY_ID,
            secret_access_key=SecretStr("  "),
        )
        assert creds.secret_access_key is None

    @pytest.mark.parametrize("model, base, field", _MATERIAL_FIELDS)
    def test_a_real_value_is_kept_verbatim(
        self, model: Type[BaseModel], base: Dict[str, Any], field: str
    ):
        creds = model.model_validate({**base, field: " test-value "})
        assert _secret(getattr(creds, field)) == " test-value "

    @pytest.mark.parametrize("blank", _BLANKS)
    def test_data_mesh_manager_blank_api_key_fails_validation(self, blank: str):
        with pytest.raises(ValidationError):
            DataMeshManagerCredentials.model_validate({"server": "https://dmm", "api_key": blank})


# Blocks whose auth method needs material that is blank: the model must refuse
# to build SDK kwargs instead of handing the SDK an empty key.
_BLANK_MATERIAL_BLOCKS = [
    pytest.param(
        GlueCredentials,
        {**_GLUE_KEY, "access_key_id": "", "secret_access_key": ""},
        id="glue-empty-keys",
    ),
    pytest.param(
        GlueCredentials,
        {**_GLUE_KEY, "access_key_id": "", "secret_access_key": "", "session_token": ""},
        id="glue-empty-keys-and-token",
    ),
    pytest.param(
        GlueCredentials,
        {**_GLUE_KEY, "access_key_id": " ", "secret_access_key": "\t"},
        id="glue-whitespace-keys",
    ),
    pytest.param(
        GlueCredentials,
        {**_GLUE_KEY, "access_key_id": "", "secret_access_key": _TEST_SECRET},
        id="glue-empty-key-id",
    ),
    pytest.param(
        GlueCredentials,
        {**_GLUE_KEY, "access_key_id": _TEST_KEY_ID, "secret_access_key": ""},
        id="glue-empty-secret",
    ),
    pytest.param(
        GlueCredentials,
        {**_GLUE_KEY, "access_key_id": "", "secret_access_key": "", "session_token": "tok"},
        id="glue-token-without-keys",
    ),
    pytest.param(BigQueryCredentials, {**_BQ_SA, "service_account_path": ""}, id="bigquery-sa"),
    pytest.param(
        DataplexCredentials, {**_DATAPLEX_SA, "service_account_path": " "}, id="dataplex-sa"
    ),
    pytest.param(UnityCredentials, {**_UNITY, "auth_method": "pat", "token": ""}, id="unity-pat"),
    pytest.param(
        UnityCredentials,
        {**_UNITY, "auth_method": "oauth_m2m", "oauth_client_id": "", "oauth_client_secret": ""},
        id="unity-oauth_m2m",
    ),
    pytest.param(
        UnityCredentials,
        {**_UNITY, "auth_method": "google_id", "google_service_account_path": ""},
        id="unity-google_id",
    ),
    pytest.param(
        SnowflakeCredentials,
        {**_SNOWFLAKE, "auth_method": "password", "password": ""},
        id="snowflake-password",
    ),
    pytest.param(
        SnowflakeCredentials,
        {**_SNOWFLAKE, "auth_method": "private_key", "private_key_path": " "},
        id="snowflake-private_key",
    ),
    pytest.param(
        SnowflakeCredentials,
        {**_SNOWFLAKE, "auth_method": "oauth", "oauth_token": ""},
        id="snowflake-oauth",
    ),
    pytest.param(
        DataHubCredentials, {**_DATAHUB, "auth_method": "pat", "token": ""}, id="datahub-pat"
    ),
    pytest.param(
        DataHubCredentials,
        {**_DATAHUB, "auth_method": "oauth", "oauth_client_id": "", "oauth_client_secret": ""},
        id="datahub-oauth",
    ),
]


class TestBlankMaterialNeverReachesTheSdk:
    @pytest.mark.parametrize("model, fields", _BLANK_MATERIAL_BLOCKS)
    def test_connection_kwargs_refuse_blank_material(
        self, model: Type[BaseModel], fields: Dict[str, Any]
    ):
        creds = model.model_validate(fields)
        with pytest.raises(CredentialNotFoundError):
            creds.to_connection_kwargs()  # type: ignore[attr-defined]

    def test_glue_keys_with_a_blank_session_token_send_no_token(self):
        creds = GlueCredentials.model_validate(
            {
                **_GLUE_KEY,
                "access_key_id": _TEST_KEY_ID,
                "secret_access_key": _TEST_SECRET,
                "session_token": " ",
            }
        )
        assert creds.to_connection_kwargs() == {
            "region_name": "us-east-1",
            "aws_access_key_id": _TEST_KEY_ID,
            "aws_secret_access_key": _TEST_SECRET,
        }


# Blocks that authenticate as this host: they carry no material of their own,
# or only blank material, or name an identity this host holds.
_HOST_IDENTITY = [
    pytest.param(GlueCredentials, {"region": "us-east-1"}, id="glue-default-iam_role"),
    pytest.param(
        GlueCredentials, {"region": "us-east-1", "auth_method": "instance_profile"}, id="glue-ip"
    ),
    pytest.param(
        GlueCredentials,
        {"region": "us-east-1", "auth_method": "sso", "profile_name": "default"},
        id="glue-sso",
    ),
    pytest.param(
        GlueCredentials,
        {
            **_GLUE_KEY,
            "profile_name": "default",
            "access_key_id": _TEST_KEY_ID,
            "secret_access_key": _TEST_SECRET,
        },
        id="glue-iam_key-plus-profile",
    ),
    pytest.param(GlueCredentials, dict(_GLUE_KEY), id="glue-iam_key-no-keys"),
    pytest.param(BigQueryCredentials, {"project": "proj"}, id="bigquery-adc"),
    pytest.param(BigQueryCredentials, {"project": "proj", "auth_method": "oidc"}, id="bq-oidc"),
    pytest.param(BigQueryCredentials, dict(_BQ_SA), id="bigquery-sa-no-path"),
    pytest.param(DataplexCredentials, {"project": "p", "location": "eu"}, id="dataplex-adc"),
    pytest.param(DataplexCredentials, dict(_DATAPLEX_SA), id="dataplex-sa-no-path"),
    pytest.param(
        UnityCredentials,
        {**_UNITY, "auth_method": "azure_ad", "azure_tenant_id": "t"},
        id="unity-azure_ad",
    ),
    pytest.param(
        UnityCredentials,
        {**_UNITY, "auth_method": "google_id", "google_service_account_path": "/sa.json"},
        id="unity-google_id",
    ),
    pytest.param(UnityCredentials, {**_UNITY, "auth_method": "pat"}, id="unity-pat-no-token"),
    pytest.param(
        UnityCredentials,
        {**_UNITY, "auth_method": "oauth_m2m", "oauth_client_id": "client"},
        id="unity-oauth_m2m-no-secret",
    ),
    # External Browser signs in with whatever session this host's browser holds.
    pytest.param(SnowflakeCredentials, {**_SNOWFLAKE, "auth_method": "sso"}, id="snowflake-sso"),
    pytest.param(
        SnowflakeCredentials, {**_SNOWFLAKE, "auth_method": "password"}, id="snowflake-no-pw"
    ),
    # No credential of its own; an HTTP client can still find one in this
    # host's files (``~/.netrc``).
    pytest.param(DataHubCredentials, {**_DATAHUB, "auth_method": "none"}, id="datahub-none"),
    pytest.param(DataHubCredentials, {**_DATAHUB, "auth_method": "pat"}, id="datahub-no-token"),
    # A path names a file on this host, so its material is the host's.
    pytest.param(
        BigQueryCredentials, {**_BQ_SA, "service_account_path": "/sa.json"}, id="bigquery-sa"
    ),
    pytest.param(
        DataplexCredentials, {**_DATAPLEX_SA, "service_account_path": "/sa.json"}, id="dataplex"
    ),
    pytest.param(
        SnowflakeCredentials,
        {**_SNOWFLAKE, "auth_method": "private_key", "private_key_path": "/k.p8"},
        id="snowflake-private_key",
    ),
    # The adapter never sends DataHub OAuth client credentials; the SDK then
    # uses this host's system client credentials.
    pytest.param(
        DataHubCredentials,
        {**_DATAHUB, "auth_method": "oauth", "oauth_client_id": "c", "oauth_client_secret": "s"},
        id="datahub-oauth",
    ),
    *(
        pytest.param(model, fields, id=f"blank-{param.id}")
        for param in _BLANK_MATERIAL_BLOCKS
        for model, fields in [param.values]
    ),
]

_CALLER_MATERIAL = [
    pytest.param(
        GlueCredentials,
        {**_GLUE_KEY, "access_key_id": _TEST_KEY_ID, "secret_access_key": _TEST_SECRET},
        id="glue-iam_key",
    ),
    pytest.param(
        GlueCredentials,
        {
            **_GLUE_KEY,
            "access_key_id": _TEST_KEY_ID,
            "secret_access_key": _TEST_SECRET,
            "session_token": "",
        },
        id="glue-iam_key-blank-token",
    ),
    pytest.param(UnityCredentials, {**_UNITY, "auth_method": "pat", "token": "t"}, id="unity-pat"),
    pytest.param(
        UnityCredentials,
        {**_UNITY, "auth_method": "oauth_m2m", "oauth_client_id": "c", "oauth_client_secret": "s"},
        id="unity-oauth_m2m",
    ),
    pytest.param(
        SnowflakeCredentials,
        {**_SNOWFLAKE, "auth_method": "password", "password": "pw"},  # pragma: allowlist secret
        id="snowflake-password",
    ),
    pytest.param(
        SnowflakeCredentials,
        {**_SNOWFLAKE, "auth_method": "oauth", "oauth_token": "t"},
        id="snowflake-oauth",
    ),
    pytest.param(
        DataHubCredentials, {**_DATAHUB, "auth_method": "pat", "token": "t"}, id="datahub-pat"
    ),
    pytest.param(
        DataMeshManagerCredentials, {"server": "https://dmm", "api_key": "k"}, id="dmm-api_key"
    ),
]


class TestSelectsHostIdentity:
    @pytest.mark.parametrize("model, fields", _HOST_IDENTITY)
    def test_host_identity(self, model: Type[BaseModel], fields: Dict[str, Any]):
        assert selects_host_identity(model.model_validate(fields)) is True

    @pytest.mark.parametrize("model, fields", _CALLER_MATERIAL)
    def test_caller_material(self, model: Type[BaseModel], fields: Dict[str, Any]):
        assert selects_host_identity(model.model_validate(fields)) is False

    def test_an_unclassified_credential_type_fails_closed(self):
        class _NewCatalogCredentials(BaseModel):
            token: str

        assert selects_host_identity(_NewCatalogCredentials(token="t")) is True

    def test_blank_material_that_skipped_validation_still_counts_as_none(self):
        """``model_construct`` bypasses the validators; the check does not rely on them."""
        creds = GlueCredentials.model_construct(
            region="us-east-1",
            auth_method="iam_key",
            profile_name=None,
            access_key_id=" ",
            secret_access_key=SecretStr(""),
        )
        assert selects_host_identity(creds) is True


class _Keyring:
    def __init__(self, entries: Dict[str, Dict[str, Any]]) -> None:
        self._entries = entries

    def get_password(self, service: str, account: str) -> Optional[str]:
        entry = self._entries.get(account)
        return None if entry is None else json.dumps(entry)


@pytest.fixture
def aws_endpoints(tmp_path, monkeypatch):
    pytest.importorskip("boto3")
    monkeypatch.delenv("FLUID_ALLOW_METADATA_SERVICE", raising=False)
    monkeypatch.delenv(PLAINTEXT_SOURCE_SECRETS_ENV, raising=False)
    logger = AwsEndpointLogger()
    point_boto3_at(logger, tmp_path, monkeypatch)
    yield logger
    logger.close()


class TestSavedSourceOnTheCliPath:
    """``fluid forge data-model from-source --credential-id`` uses the same resolver.

    A saved Glue source whose keyring entry holds blank keys must fail closed
    instead of letting boto3 fall back to the host's identity, with or without
    ``--allow-metadata-service`` (which allows the fallback when asked for, by
    ``instance_profile``; it does not turn blank keys into a request for it).
    """

    @pytest.mark.parametrize("allow_metadata_service", [False, True], ids=["default", "opt-in"])
    @pytest.mark.parametrize(
        "secrets",
        [
            pytest.param({"access_key_id": "", "secret_access_key": ""}, id="empty-keys"),
            pytest.param(
                {"access_key_id": "", "secret_access_key": "", "session_token": ""},
                id="empty-keys-and-token",
            ),
            pytest.param({"access_key_id": " ", "secret_access_key": " "}, id="whitespace-keys"),
        ],
    )
    def test_saved_blank_keys_never_reach_the_metadata_service(
        self, aws_endpoints, tmp_path, secrets, allow_metadata_service
    ):
        from fluid_build.cli.forge_data_model import _build_catalog_adapter
        from fluid_build.copilot.catalog.models import CatalogScope

        sources = tmp_path / "sources.yaml"
        sources.write_text(
            "sources:\n"
            "  glue-prod:\n"
            "    source_type: glue\n"
            "    config:\n"
            "      region: us-east-1\n"
            "      auth_method: iam_key\n",
            encoding="utf-8",
        )
        os.chmod(sources, 0o600)
        resolver = CredentialResolver(
            sources_config_path=sources,
            keyring_module=_Keyring({"glue-prod": secrets}),
            allow_metadata_service=allow_metadata_service,
        )
        adapter = _build_catalog_adapter(
            source="glue", resolver=resolver, credential_id="glue-prod"
        )
        with pytest.raises(CredentialNotFoundError):
            adapter.list_tables(CatalogScope(database="analytics"))
        assert aws_endpoints.requests == []
        assert aws_endpoints.api_calls == []

    def test_saved_real_keys_sign_the_call_without_the_metadata_service(
        self, aws_endpoints, tmp_path
    ):
        """Positive control: the logger sees the SDK, signed with the saved key."""
        from fluid_build.cli.forge_data_model import _build_catalog_adapter
        from fluid_build.copilot.catalog.models import CatalogScope

        sources = tmp_path / "sources.yaml"
        sources.write_text(
            "sources:\n  glue-prod:\n    source_type: glue\n"
            "    config: {region: us-east-1, auth_method: iam_key}\n",
            encoding="utf-8",
        )
        resolver = CredentialResolver(
            sources_config_path=sources,
            keyring_module=_Keyring(
                {"glue-prod": {"access_key_id": _TEST_KEY_ID, "secret_access_key": _TEST_SECRET}}
            ),
        )
        adapter = _build_catalog_adapter(
            source="glue", resolver=resolver, credential_id="glue-prod"
        )
        assert adapter.list_tables(CatalogScope(database="analytics")) == []
        assert aws_endpoints.api_calls == [_TEST_KEY_ID]
        assert aws_endpoints.requests == []
