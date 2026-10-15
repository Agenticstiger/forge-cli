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

"""What in an inline credential block is this host's, not the caller's.

The MCP server's inline gate asks :func:`host_identity_reason` whether a block
a caller sent would authenticate as this host. Besides an ``auth_method`` that
selects the SDK's default chain, three things make it the host's:

* A path. The SDK reads the file from this host's disk, so a path names
  credential material this host holds (gcloud ADC, a mounted service-account
  key, a Snowflake private key), however it is spelled.
* Configuration of this host the SDK reads in place of the block: DataHub's
  ``__from_env__`` server, its system client credentials (which stand in for
  OAuth client credentials the adapter does not send), its client certificate.
* A netrc file on this host, for a catalog the caller names an HTTP endpoint
  for: ``requests`` (under the DataHub and Databricks SDKs) attaches its logins
  to that endpoint and to any host the endpoint redirects to.
"""

from __future__ import annotations

import typing
from pathlib import Path, PurePath
from typing import Any, Dict, List, Tuple, Type

import pytest
from pydantic import BaseModel

from fluid_build.copilot.catalog.credentials import (
    BigQueryCredentials,
    DataHubCredentials,
    DataMeshManagerCredentials,
    DataplexCredentials,
    GlueCredentials,
    SnowflakeCredentials,
    UnityCredentials,
)

_TEST_SECRET = "test-secret-value"  # pragma: allowlist secret

# One block per credential model that is the caller's own on a host with no
# netrc file and no DataHub settings: the baseline each case below changes.
_CALLER_BLOCKS: Dict[Type[BaseModel], Dict[str, Any]] = {
    GlueCredentials: {
        "region": "us-east-1",
        "auth_method": "iam_key",
        "access_key_id": "test-access-key-id",
        "secret_access_key": _TEST_SECRET,
    },
    BigQueryCredentials: {"project": "proj"},  # has no caller form; see below
    DataplexCredentials: {"project": "proj", "location": "eu"},
    UnityCredentials: {
        "host": "https://x.cloud.databricks.com",
        "auth_method": "pat",
        "token": "t",
    },
    SnowflakeCredentials: {
        "account": "acct",
        "user": "analyst",
        "auth_method": "password",
        "password": _TEST_SECRET,
    },
    DataHubCredentials: {
        "server": "https://datahub.example.com",
        "auth_method": "pat",
        "token": "t",
    },
    DataMeshManagerCredentials: {"server": "https://dmm.example.com", "api_key": _TEST_SECRET},
}


def host_identity_reason(credentials: BaseModel) -> Any:
    # Resolved per call, so each case reports on its own before the fix lands.
    from fluid_build.copilot.catalog import credentials as module

    return module.host_identity_reason(credentials)


def selects_host_identity(credentials: BaseModel) -> Any:
    from fluid_build.copilot.catalog import credentials as module

    return module.selects_host_identity(credentials)


@pytest.fixture(autouse=True)
def bare_host(tmp_path, monkeypatch) -> Path:
    """A host with no netrc file and no DataHub settings; HOME is ``tmp_path/home``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home)
    monkeypatch.delenv("NETRC", raising=False)
    for name in [
        "DATAHUB_CLIENT_CERT_PATH",
        "DATAHUB_CLIENT_KEY_PATH",
        "DATAHUB_SYSTEM_CLIENT_ID",
        "DATAHUB_SYSTEM_CLIENT_SECRET",
        "DATAHUB_GMS_URL",
        "DATAHUB_GMS_HOST",
        "DATAHUB_GMS_TOKEN",
    ]:
        monkeypatch.delenv(name, raising=False)
    return home


def _path_fields() -> List[Tuple[Type[BaseModel], str]]:
    """Every field of every credential model whose type is a filesystem path."""

    def is_path(annotation: Any) -> bool:
        if isinstance(annotation, type) and issubclass(annotation, PurePath):
            return True
        return any(is_path(arg) for arg in typing.get_args(annotation))

    return [
        (model, name)
        for model in _CALLER_BLOCKS
        for name, field in model.model_fields.items()
        if is_path(field.annotation)
    ]


class TestPathIsHostMaterial:
    def test_the_path_fields_are_the_ones_this_suite_expects(self):
        """A new path field is covered by the generic cases below; this pins the inventory."""
        assert {(model.__name__, name) for model, name in _path_fields()} == {
            ("BigQueryCredentials", "service_account_path"),
            ("DataplexCredentials", "service_account_path"),
            ("UnityCredentials", "google_service_account_path"),
            ("SnowflakeCredentials", "private_key_path"),
        }

    @pytest.mark.parametrize(
        ("model", "field"),
        [pytest.param(m, f, id=f"{m.__name__}.{f}") for m, f in _path_fields()],
    )
    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/run/secrets/sa.json", id="absolute"),
            pytest.param("~/.config/gcloud/application_default_credentials.json", id="tilde"),
            pytest.param("$HOME/.snowflake/rsa_key.p8", id="env-var"),
            pytest.param("${GOOGLE_APPLICATION_CREDENTIALS}", id="env-var-braced"),
            pytest.param("keys/sa.json", id="relative"),
            pytest.param("../../etc/sa.json", id="dot-dot"),
        ],
    )
    def test_any_path_in_any_path_field_is_the_hosts(self, model, field, path):
        """Whatever the auth method, and beside material of the caller's own."""
        creds = model.model_validate({**_CALLER_BLOCKS[model], field: path})
        reason = host_identity_reason(creds)
        assert reason is not None
        assert field in reason
        assert selects_host_identity(creds) is True

    def test_a_symlink_is_a_path_too(self, bare_host):
        target = bare_host / "sa.json"
        target.write_text("{}", encoding="utf-8")
        (bare_host / "link.json").symlink_to(target)
        creds = BigQueryCredentials(
            project="p", auth_method="service_account_json", service_account_path=Path("link.json")
        )
        assert "service_account_path" in host_identity_reason(creds)

    @pytest.mark.parametrize(
        ("model", "fields", "field"),
        [
            pytest.param(
                BigQueryCredentials,
                {"project": "p", "auth_method": "service_account_json"},
                "service_account_path",
                id="bigquery-service_account_json",
            ),
            pytest.param(
                DataplexCredentials,
                {"project": "p", "location": "eu", "auth_method": "service_account_json"},
                "service_account_path",
                id="dataplex-service_account_json",
            ),
            pytest.param(
                SnowflakeCredentials,
                {"account": "a", "user": "u", "auth_method": "private_key"},
                "private_key_path",
                id="snowflake-private_key",
            ),
            pytest.param(
                UnityCredentials,
                {"host": "https://x.cloud.databricks.com", "auth_method": "google_id"},
                "google_service_account_path",
                id="unity-google_id",
            ),
        ],
    )
    def test_the_auth_methods_that_take_a_file(self, model, fields, field):
        creds = model.model_validate({**fields, field: "/run/secrets/key"})
        assert field in host_identity_reason(creds)

    def test_a_path_that_skipped_validation_still_counts(self):
        """``model_construct`` leaves a ``str`` in a path field; the field's type decides."""
        creds = SnowflakeCredentials.model_construct(
            account="a",
            user="u",
            auth_method="private_key",
            private_key_path="~/.snowflake/rsa_key.p8",
        )
        assert "private_key_path" in host_identity_reason(creds)

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_a_blank_path_is_no_material_not_a_file(self, blank):
        creds = SnowflakeCredentials.model_validate(
            {**_CALLER_BLOCKS[SnowflakeCredentials], "private_key_path": blank}
        )
        assert host_identity_reason(creds) is None


class TestCallerMaterialOnABareHost:
    @pytest.mark.parametrize(
        "model",
        [m for m in _CALLER_BLOCKS if m not in (BigQueryCredentials, DataplexCredentials)],
        ids=lambda m: m.__name__,
    )
    def test_value_material_is_the_callers(self, model):
        assert host_identity_reason(model.model_validate(_CALLER_BLOCKS[model])) is None

    @pytest.mark.parametrize("model", [BigQueryCredentials, DataplexCredentials])
    @pytest.mark.parametrize("auth_method", ["adc", "oidc", "service_account_json"])
    def test_google_catalogs_have_no_caller_form(self, model, auth_method):
        """Their models take a key only as a file path, so nothing inline is the caller's."""
        creds = model.model_validate({**_CALLER_BLOCKS[model], "auth_method": auth_method})
        assert host_identity_reason(creds) is not None


class TestFailsClosed:
    @pytest.mark.parametrize(
        "model",
        [m for m in _CALLER_BLOCKS if "auth_method" in m.model_fields],
        ids=lambda m: m.__name__,
    )
    def test_an_auth_method_added_later_is_the_hosts_until_classified(self, model):
        """``model_construct`` stands in for a Literal widened without this function."""
        fields = {**_CALLER_BLOCKS[model], "auth_method": "added_later"}
        reason = host_identity_reason(model.model_construct(**fields))
        assert reason is not None and "does not classify" in reason


class TestDataHubHostConfiguration:
    @pytest.mark.parametrize(
        "server",
        [
            pytest.param("__from_env__", id="from-env"),
            pytest.param("datahub.example.com:8080", id="no-scheme"),
            pytest.param("file:///etc/datahub", id="file-url"),
            pytest.param("https://", id="no-host"),
        ],
    )
    def test_a_server_that_is_not_an_http_url_is_the_hosts(self, server):
        creds = DataHubCredentials.model_validate(
            {**_CALLER_BLOCKS[DataHubCredentials], "server": server}
        )
        assert "server" in host_identity_reason(creds)

    @pytest.mark.parametrize("server", ["http://127.0.0.1:8080", "https://datahub.example.com/gms"])
    def test_an_http_url_is_fine(self, server):
        creds = DataHubCredentials.model_validate(
            {**_CALLER_BLOCKS[DataHubCredentials], "server": server}
        )
        assert host_identity_reason(creds) is None

    def test_oauth_client_credentials_are_never_sent_so_oauth_is_the_hosts(self):
        """The adapter passes the SDK a server and a token only; with no token the
        SDK authenticates with this host's DATAHUB_SYSTEM_CLIENT_ID / _SECRET."""
        creds = DataHubCredentials.model_validate(
            {
                "server": "https://datahub.example.com",
                "auth_method": "oauth",
                "oauth_client_id": "test-client",
                "oauth_client_secret": _TEST_SECRET,
            }
        )
        assert "oauth" in host_identity_reason(creds)

    def test_the_hosts_client_certificate_is_the_hosts(self, monkeypatch, bare_host):
        monkeypatch.setenv("DATAHUB_CLIENT_CERT_PATH", str(bare_host / "client.pem"))
        creds = DataHubCredentials.model_validate(_CALLER_BLOCKS[DataHubCredentials])
        assert "DATAHUB_CLIENT_CERT_PATH" in host_identity_reason(creds)


def _write_netrc(path: Path) -> None:
    path.write_text("machine example.com login test-user password test-value\n", encoding="utf-8")
    path.chmod(0o600)


class TestHostNetrcLogin:
    _HTTP = [UnityCredentials, DataHubCredentials]

    @pytest.mark.parametrize("model", _HTTP, ids=lambda m: m.__name__)
    @pytest.mark.parametrize("name", [".netrc", "_netrc"])
    def test_a_netrc_in_home_makes_a_named_endpoint_the_hosts(self, model, name, bare_host):
        _write_netrc(bare_host / name)
        reason = host_identity_reason(model.model_validate(_CALLER_BLOCKS[model]))
        assert reason is not None and "netrc" in reason

    @pytest.mark.parametrize("model", _HTTP, ids=lambda m: m.__name__)
    def test_a_netrc_named_by_the_netrc_variable_too(self, model, tmp_path, monkeypatch):
        elsewhere = tmp_path / "elsewhere" / "netrc"
        elsewhere.parent.mkdir()
        _write_netrc(elsewhere)
        monkeypatch.setenv("NETRC", str(elsewhere))
        assert "netrc" in host_identity_reason(model.model_validate(_CALLER_BLOCKS[model]))

    def test_the_reason_does_not_echo_the_hosts_paths(self, bare_host):
        _write_netrc(bare_host / ".netrc")
        reason = host_identity_reason(
            UnityCredentials.model_validate(_CALLER_BLOCKS[UnityCredentials])
        )
        assert str(bare_host) not in reason

    @pytest.mark.parametrize(
        "model",
        [GlueCredentials, SnowflakeCredentials, DataMeshManagerCredentials],
        ids=lambda m: m.__name__,
    )
    def test_catalogs_whose_client_never_reads_netrc_are_unaffected(self, model, bare_host):
        """boto3 and the Snowflake connector reach fixed vendor endpoints; Data Mesh
        Manager's ``httpx`` client applies netrc only when asked to (see the MCP
        sink test, which proves it at the wire)."""
        _write_netrc(bare_host / ".netrc")
        assert host_identity_reason(model.model_validate(_CALLER_BLOCKS[model])) is None


class TestWhatToSendInstead:
    def test_value_forms_are_named(self):
        from fluid_build.copilot.catalog.credentials import inline_credential_forms

        key_file = SnowflakeCredentials.model_validate(
            {"account": "a", "user": "u", "auth_method": "private_key", "private_key_path": "/k.p8"}
        )
        text = inline_credential_forms(key_file)
        assert "'password' with password" in text and "oauth_token" in text

    def test_nothing_to_change_when_only_the_host_stands_in(self, bare_host):
        """A netrc login is the host's doing; no other block would avoid it."""
        from fluid_build.copilot.catalog.credentials import inline_credential_forms

        _write_netrc(bare_host / ".netrc")
        creds = DataHubCredentials.model_validate(_CALLER_BLOCKS[DataHubCredentials])
        assert host_identity_reason(creds) is not None
        assert inline_credential_forms(creds) is None

    @pytest.mark.parametrize("model", [BigQueryCredentials, DataplexCredentials])
    def test_google_catalogs_say_there_is_no_inline_form(self, model):
        from fluid_build.copilot.catalog.credentials import inline_credential_forms

        text = inline_credential_forms(model.model_validate(_CALLER_BLOCKS[model]))
        assert "file path" in text
