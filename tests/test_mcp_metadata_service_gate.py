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

"""The metadata-service credential fallback is an operator opt-in on ``fluid mcp serve``.

The source-catalog MCP tools accept an ``allow_metadata_service`` argument.
The credential fallback it controls (instance role, workload identity,
gcloud ADC) is an operator decision, so the argument may only *request* it
when the operator enabled it with ``--allow-metadata-service`` or
``FLUID_ALLOW_METADATA_SERVICE=1`` in the server's own environment. Without
that opt-in a request is refused with a tool error, mirroring how
``--allow-inline-credentials`` gates ``credentials.inline``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build.cli.mcp import (
    McpPolicy,
    _build_policy_from_args,
    _build_source_adapter,
    _call_tool,
)
from tests._infrastructure.aws_endpoint_logger import AwsEndpointLogger, point_boto3_at

_ENV = "FLUID_ALLOW_METADATA_SERVICE"

# Every MCP tool whose arguments carry ``allow_metadata_service``.
_CATALOG_TOOLS = (
    "list_source_tables",
    "inspect_source_table",
    "list_source_lineage",
    "list_source_glossary",
    "forge_from_source",
)


class _ResolverCaptured(Exception):
    """Raised by the stub adapter once it has seen the resolver."""


@pytest.fixture(autouse=True)
def _no_operator_env(monkeypatch, tmp_path):
    """The ambient env var is an operator opt-in; never inherit one.

    Nor the developer's own netrc file or DataHub settings: the inline gate
    reads both, so a test runs against an empty HOME.
    """
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("NETRC", "DATAHUB_CLIENT_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def captured_resolvers(monkeypatch) -> List[Any]:
    """Replace adapter import with a stub that records the resolver it gets."""
    seen: List[Any] = []

    class _StubAdapter:
        @classmethod
        def from_resolver(cls, resolver, *, credential_id=None, inline_credentials=None):
            seen.append(resolver)
            raise _ResolverCaptured()

    monkeypatch.setattr(
        "fluid_build.cli.mcp.dispatch._import_adapter_class", lambda _path: _StubAdapter
    )
    return seen


def _args(**extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "source": "snowflake",
        "credentials": {"credential_id": "test-cred"},
        "scope": {"database": "DB", "schema_name": "SCH"},
        "fqn": "DB.SCH.T",
        "output_path": "out.fluid.yaml",
    }
    base.update(extra)
    return base


class TestAdapterBuilderGate:
    def test_client_request_refused_without_operator_opt_in(self, captured_resolvers):
        with pytest.raises(RuntimeError, match="--allow-metadata-service") as exc:
            _build_source_adapter(_args(allow_metadata_service=True))
        assert _ENV in str(exc.value)
        assert captured_resolvers == []

    def test_operator_flag_lets_client_request_it(self, captured_resolvers):
        with pytest.raises(_ResolverCaptured):
            _build_source_adapter(_args(allow_metadata_service=True), allow_metadata_service=True)
        assert captured_resolvers[0].allow_metadata_service is True

    def test_operator_env_var_counts_as_opt_in(self, captured_resolvers, monkeypatch):
        monkeypatch.setenv(_ENV, "1")
        with pytest.raises(_ResolverCaptured):
            _build_source_adapter(_args(allow_metadata_service=True))
        assert captured_resolvers[0].allow_metadata_service is True

    @pytest.mark.parametrize("value", ["0", "true", "yes", ""])
    def test_env_var_other_than_1_is_not_an_opt_in(self, captured_resolvers, monkeypatch, value):
        monkeypatch.setenv(_ENV, value)
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _build_source_adapter(_args(allow_metadata_service=True))

    @pytest.mark.parametrize("value", [False, None])
    def test_no_request_stays_off_even_when_operator_allows(self, captured_resolvers, value):
        """The operator flag permits the fallback; it does not force it on."""
        with pytest.raises(_ResolverCaptured):
            _build_source_adapter(_args(allow_metadata_service=value), allow_metadata_service=True)
        assert captured_resolvers[0].allow_metadata_service is False

    def test_absent_argument_without_opt_in_is_unchanged(self, captured_resolvers):
        with pytest.raises(_ResolverCaptured):
            _build_source_adapter(_args())
        assert captured_resolvers[0].allow_metadata_service is False

    @pytest.mark.parametrize("value", ["false", "yes", 1])
    def test_non_bool_truthy_request_fails_closed(self, captured_resolvers, value):
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _build_source_adapter(_args(allow_metadata_service=value))


class TestCallToolGate:
    @pytest.mark.parametrize("tool", _CATALOG_TOOLS)
    def test_every_catalog_tool_refuses_without_opt_in(self, tool, captured_resolvers):
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _call_tool(tool, _args(allow_metadata_service=True), read_only=False)
        assert captured_resolvers == []

    @pytest.mark.parametrize("tool", _CATALOG_TOOLS)
    def test_every_catalog_tool_threads_the_operator_flag(self, tool, captured_resolvers):
        with pytest.raises(_ResolverCaptured):
            _call_tool(
                tool,
                _args(allow_metadata_service=True),
                read_only=False,
                allow_metadata_service=True,
            )
        assert captured_resolvers[0].allow_metadata_service is True

    def test_jdbc_forge_from_source_refuses_too(self, tmp_path):
        """JDBC sources never use the resolver, and the JDBC path ignores the
        argument: asking for the fallback does not let a client-named sqlite
        path through, which the operator's opt-in alone decides (see
        test_mcp_jdbc_source_gate.TestClientMetadataArgumentIsIgnored)."""
        db = tmp_path / "source.db"
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _call_tool(
                "forge_from_source",
                {
                    "source": "sqlite",
                    "uri": f"sqlite:///{db}",
                    "output_path": str(tmp_path / "out.fluid.yaml"),
                    "allow_metadata_service": True,
                },
                read_only=False,
            )
        assert not db.exists()


class TestServerWiring:
    def test_serve_flag_parses_into_policy(self):
        from fluid_build.cli.mcp import register

        parser = argparse.ArgumentParser()
        register(parser.add_subparsers(dest="cmd"))
        args = parser.parse_args(["mcp", "serve", "--allow-metadata-service"])
        assert _build_policy_from_args(args).allow_metadata_service is True
        default = parser.parse_args(["mcp", "serve"])
        assert _build_policy_from_args(default).allow_metadata_service is False

    def test_split_cli_register_stays_in_lock_step(self):
        from fluid_build.cli.mcp.cli import register

        parser = argparse.ArgumentParser()
        register(parser.add_subparsers(dest="cmd"))
        args = parser.parse_args(["mcp", "serve", "--allow-metadata-service"])
        assert args.allow_metadata_service is True

    @pytest.mark.parametrize("module", ["fluid_build.cli.mcp", "fluid_build.cli.mcp.cli"])
    def test_serve_help_says_what_the_env_var_adds(self, module):
        """The variable is not the flag's twin: the resolver reads it on every
        catalog call (``CredentialResolver.__init__``), so it also turns the
        fallback on for a call that does not ask for it."""
        register = importlib.import_module(module).register
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers(dest="cmd"))
        for name in ("mcp", "serve"):
            sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            parser = sub.choices[name]
        flag = next(a for a in parser._actions if "--allow-metadata-service" in a.option_strings)
        assert "equivalent" not in flag.help
        assert "FLUID_ALLOW_METADATA_SERVICE=1" in flag.help
        assert "does not ask for it" in flag.help

    @pytest.mark.parametrize("module", ["fluid_build.cli.mcp", "fluid_build.cli.mcp.cli"])
    @pytest.mark.parametrize(
        ("option", "what"),
        [
            ("--allow-inline-credentials", "a JDBC URI that carries a password"),
            (
                "--allow-metadata-service",
                "a JDBC URI that would use this host's database credentials, files or identity",
            ),
        ],
    )
    def test_serve_help_says_the_flag_also_governs_jdbc_uris(self, module, option, what):
        """docs/MCP.md item 7: forge_from_source with a postgres, mysql or sqlite
        source is gated by these two flags too (``_gate_jdbc_source``), and the
        client's own allow_metadata_service plays no part there."""
        register = importlib.import_module(module).register
        parser = argparse.ArgumentParser()
        register(parser.add_subparsers(dest="cmd"))
        for name in ("mcp", "serve"):
            sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
            parser = sub.choices[name]
        flag = next(a for a in parser._actions if option in a.option_strings)
        text = " ".join(flag.help.split())
        assert f"forge_from_source also needs this flag for {what}" in text
        if option == "--allow-metadata-service":
            assert "there the client's allow_metadata_service plays no part" in text

    def test_env_var_turns_it_on_for_a_call_that_does_not_ask(
        self, captured_resolvers, monkeypatch
    ):
        """What the serve help now says, against the code: the flag alone leaves
        such a call off (``test_no_request_stays_off_even_when_operator_allows``)."""
        monkeypatch.setenv(_ENV, "1")
        with pytest.raises(_ResolverCaptured):
            _build_source_adapter(_args())
        assert captured_resolvers[0].allow_metadata_service is True

    def test_policy_defaults_off(self):
        assert McpPolicy().allow_metadata_service is False

    @pytest.mark.parametrize("operator_allows", [False, True])
    def test_server_dispatch_passes_policy_flag(self, operator_allows, captured_resolvers):
        from fluid_build.cli.mcp import policy as policy_mod
        from fluid_build.cli.mcp.server import _dispatch_sync_tool

        previous = policy_mod._current_policy
        policy_mod._set_policy(McpPolicy(allow_metadata_service=operator_allows))
        try:
            call = _dispatch_sync_tool("list_source_tables", _args(allow_metadata_service=True))
            if operator_allows:
                with pytest.raises(_ResolverCaptured):
                    asyncio.run(call)
                assert captured_resolvers[0].allow_metadata_service is True
            else:
                from fluid_build._mcp_compat import get_tool_error

                # Must be the SDK's ToolError: SDK 2.x masks any other
                # exception as "Error executing tool <name>", hiding the flag.
                with pytest.raises(get_tool_error(), match="--allow-metadata-service"):
                    asyncio.run(call)
        finally:
            policy_mod._current_policy = previous


class TestSchemaIsHonest:
    @pytest.mark.parametrize("tool", _CATALOG_TOOLS)
    def test_static_schema_names_the_server_flag(self, tool):
        from fluid_build.cli.mcp import TOOL_CAPABILITIES

        prop = TOOL_CAPABILITIES[tool].input_schema["properties"]["allow_metadata_service"]
        assert "--allow-metadata-service" in prop["description"]

    @pytest.mark.parametrize("tool", _CATALOG_TOOLS)
    def test_advertised_schema_names_the_server_flag(self, tool):
        """What a live client sees in ``tools/list`` (the FastMCP-derived schema)."""
        from fluid_build._mcp_compat import attr as _mcp_attr
        from fluid_build.cli.mcp import _get_mcp_app

        async def _go() -> Dict[str, Any]:
            for advertised in await _get_mcp_app().list_tools():
                if advertised.name == tool:
                    return _mcp_attr(advertised, "input_schema", "inputSchema")  # type: ignore[no-any-return]
            raise AssertionError(f"{tool!r} not advertised")

        prop = asyncio.run(_go())["properties"]["allow_metadata_service"]
        assert "--allow-metadata-service" in prop["description"]

    @pytest.mark.parametrize("tool", _CATALOG_TOOLS)
    def test_schema_says_a_jdbc_source_ignores_it(self, tool):
        """forge_from_source's JDBC branch never reads the argument; only the
        operator's opt-in opens a JDBC host-identity URI (pinned in
        test_mcp_jdbc_source_gate.TestClientMetadataArgumentIsIgnored)."""
        from fluid_build.cli.mcp import TOOL_CAPABILITIES

        prop = TOOL_CAPABILITIES[tool].input_schema["properties"]["allow_metadata_service"]
        text = prop["description"]
        assert "catalog sources only" in text.lower()
        assert "A JDBC source" in text and "ignores it" in text


@pytest.fixture
def metadata_endpoint(tmp_path, monkeypatch):
    """A Glue-capable server env with no credential material, only a region.

    boto3's default chain is pointed at a loopback logger instead of the real
    link-local endpoint, and every file or variable it could otherwise read
    credentials from is cleared, so the only identity left is the host's. The
    Glue and Lake Formation endpoints point at the same logger, so a call that
    does carry keys stays on loopback and records which key signed it.
    """
    pytest.importorskip("boto3")
    logger = AwsEndpointLogger()
    point_boto3_at(logger, tmp_path, monkeypatch)
    yield logger
    logger.close()


def _glue_args(**extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "source": "glue",
        "credentials": {"credential_id": "not-saved-on-this-host"},
        "scope": {"database": "analytics"},
    }
    base.update(extra)
    return base


class TestHostIdentityAtTheSink:
    """The request that matters is the one the SDK sends, not the argument."""

    def test_no_metadata_request_without_operator_opt_in(self, metadata_endpoint):
        from fluid_build.copilot.catalog.credentials import CredentialNotFoundError

        with pytest.raises(CredentialNotFoundError, match="not-saved-on-this-host"):
            _call_tool("list_source_tables", _glue_args(), read_only=True)
        assert metadata_endpoint.requests == []

    def test_operator_flag_alone_does_not_reach_it(self, metadata_endpoint):
        """The flag permits a request; a call that does not ask stays off it."""
        from fluid_build.copilot.catalog.credentials import CredentialNotFoundError

        with pytest.raises(CredentialNotFoundError):
            _call_tool(
                "list_source_tables", _glue_args(), read_only=True, allow_metadata_service=True
            )
        assert metadata_endpoint.requests == []

    def test_operator_opt_in_plus_request_reaches_it(self, metadata_endpoint):
        """Positive control: the logger does see the SDK when both sides opt in."""
        with pytest.raises(Exception):
            _call_tool(
                "list_source_tables",
                _glue_args(allow_metadata_service=True),
                read_only=True,
                allow_metadata_service=True,
            )
        assert metadata_endpoint.requests, "boto3 never consulted the metadata endpoint"


def _inline(source: str, inline: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "source": source,
        "credentials": {"inline": inline},
        "scope": {"database": "analytics"},
    }
    base.update(extra)
    return base


# Inline credential blocks that carry no secret of their own: the SDK's default
# chain fills them in from the host (instance or container role, workload
# identity, gcloud ADC).
_HOST_IDENTITY_INLINE = [
    pytest.param("glue", {"region": "us-east-1"}, id="glue-default-iam_role-no-profile"),
    pytest.param(
        "glue",
        {"region": "us-east-1", "auth_method": "iam_role", "role_arn": "arn:aws:iam::0:role/x"},
        id="glue-iam_role-role_arn-only",
    ),
    pytest.param(
        "glue", {"region": "us-east-1", "auth_method": "instance_profile"}, id="glue-instance"
    ),
    pytest.param("bigquery", {"project": "proj"}, id="bigquery-default-adc"),
    pytest.param("bigquery", {"project": "proj", "auth_method": "oidc"}, id="bigquery-oidc"),
    pytest.param("dataplex", {"project": "proj", "location": "eu"}, id="dataplex-default-adc"),
    pytest.param(
        "dataplex",
        {"project": "proj", "location": "eu", "auth_method": "oidc"},
        id="dataplex-oidc",
    ),
    # A named AWS profile lives in this host's ~/.aws files. A profile with no
    # keys of its own still ends boto3's chain at the container or instance
    # role, so the name alone is the host's identity, not the caller's.
    pytest.param(
        "glue", {"region": "us-east-1", "profile_name": "default"}, id="glue-named-profile"
    ),
    pytest.param(
        "glue",
        {"region": "us-east-1", "auth_method": "sso", "profile_name": "default"},
        id="glue-sso-profile",
    ),
    pytest.param(
        "glue",
        {
            "region": "us-east-1",
            "auth_method": "iam_role",
            "profile_name": "default",
            "access_key_id": "test-access-key-id",
            "secret_access_key": "test-secret-access-key",  # pragma: allowlist secret
        },
        id="glue-iam_role-profile-plus-keys",
    ),
    pytest.param(
        "glue",
        {
            "region": "us-east-1",
            "auth_method": "iam_key",
            "profile_name": "default",
            "access_key_id": "test-access-key-id",
            "secret_access_key": "test-secret-access-key",  # pragma: allowlist secret
        },
        id="glue-iam_key-plus-profile",
    ),
    # Databricks: azure_ad authenticates as this host's `az login` session and
    # google_id impersonates through this host's Google ADC.
    pytest.param(
        "unity",
        {
            "host": "https://x.cloud.databricks.com",
            "auth_method": "azure_ad",
            "azure_tenant_id": "t",
        },
        id="unity-azure_ad",
    ),
    pytest.param(
        "unity",
        {
            "host": "https://x.cloud.databricks.com",
            "auth_method": "google_id",
            "google_service_account_path": "sa@p.iam.gserviceaccount.com",
        },
        id="unity-google_id",
    ),
    # Snowflake's External Browser flow signs in with whatever session this
    # host's browser holds; DataHub ``none`` sends no credential of its own.
    pytest.param(
        "snowflake",
        {"account": "acct", "user": "analyst", "auth_method": "sso"},
        id="snowflake-sso",
    ),
    pytest.param(
        "datahub",
        {"server": "https://datahub.example.com", "auth_method": "none"},
        id="datahub-none",
    ),
    # A path names a file on this host: the SDK reads the host's copy of it.
    # tests/test_mcp_inline_host_material.py covers every path field and form.
    pytest.param(
        "bigquery",
        {
            "project": "proj",
            "auth_method": "service_account_json",
            "service_account_path": "/nonexistent/sa.json",
        },
        id="bigquery-service-account-path",
    ),
]

# Inline blocks that name their own credential material are unaffected.
_EXPLICIT_INLINE = [
    pytest.param(
        "glue",
        {
            "region": "us-east-1",
            "auth_method": "iam_key",
            "access_key_id": "test-access-key-id",
            "secret_access_key": "test-secret-access-key",  # pragma: allowlist secret
        },
        id="glue-iam_key",
    ),
    pytest.param(
        "unity",
        {"host": "https://x.cloud.databricks.com", "auth_method": "pat", "token": "test-token"},
        id="unity-pat",
    ),
    pytest.param(
        "unity",
        {
            "host": "https://x.cloud.databricks.com",
            "auth_method": "oauth_m2m",
            "oauth_client_id": "test-client",
            "oauth_client_secret": "test-client-secret",  # pragma: allowlist secret
        },
        id="unity-oauth_m2m",
    ),
    pytest.param(
        "glue",
        {
            "region": "us-east-1",
            "auth_method": "iam_key",
            "access_key_id": "test-access-key-id",
            "secret_access_key": "test-secret-access-key",  # pragma: allowlist secret
            "session_token": "",
        },
        id="glue-iam_key-blank-session-token",
    ),
    pytest.param(
        "snowflake",
        {
            "account": "acct",
            "user": "analyst",
            "auth_method": "password",
            "password": "test-password",  # pragma: allowlist secret
        },
        id="snowflake-password",
    ),
    pytest.param(
        "datahub",
        {"server": "https://datahub.example.com", "auth_method": "pat", "token": "test-token"},
        id="datahub-pat",
    ),
    pytest.param(
        "datamesh_manager",
        {
            "server": "https://dmm.example.com",
            "api_key": "test-api-key",  # pragma: allowlist secret
        },
        id="datamesh_manager",
    ),
]


class TestInlineCredentialsCannotSelectHostIdentity:
    """``--allow-inline-credentials`` alone does not reach the host's cloud identity.

    The inline block's ``auth_method`` (and its defaults) can select the SDK's
    default credential chain just as ``allow_metadata_service`` does, so the
    same operator opt-in governs it.
    """

    @pytest.mark.parametrize(("source", "inline"), _HOST_IDENTITY_INLINE)
    def test_refused_without_operator_opt_in(self, source, inline):
        with pytest.raises(RuntimeError, match="--allow-metadata-service") as exc:
            _build_source_adapter(_inline(source, inline), allow_inline_credentials=True)
        assert _ENV in str(exc.value)

    @pytest.mark.parametrize(("source", "inline"), _HOST_IDENTITY_INLINE)
    def test_client_request_does_not_stand_in_for_the_operator(self, source, inline):
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _build_source_adapter(
                _inline(source, inline, allow_metadata_service=True),
                allow_inline_credentials=True,
            )

    @pytest.mark.parametrize(("source", "inline"), _HOST_IDENTITY_INLINE)
    def test_operator_flag_permits_it(self, source, inline):
        adapter = _build_source_adapter(
            _inline(source, inline), allow_inline_credentials=True, allow_metadata_service=True
        )
        assert adapter is not None

    @pytest.mark.parametrize(("source", "inline"), _HOST_IDENTITY_INLINE)
    def test_operator_env_var_permits_it(self, source, inline, monkeypatch):
        monkeypatch.setenv(_ENV, "1")
        adapter = _build_source_adapter(_inline(source, inline), allow_inline_credentials=True)
        assert adapter is not None

    @pytest.mark.parametrize(("source", "inline"), _EXPLICIT_INLINE)
    def test_explicit_credential_material_is_unaffected(self, source, inline):
        adapter = _build_source_adapter(_inline(source, inline), allow_inline_credentials=True)
        assert adapter is not None

    def test_refusal_reaches_the_client_as_a_tool_error(self):
        from fluid_build._mcp_compat import get_tool_error
        from fluid_build.cli.mcp import policy as policy_mod
        from fluid_build.cli.mcp.server import _dispatch_sync_tool

        previous = policy_mod._current_policy
        policy_mod._set_policy(McpPolicy(allow_inline_credentials=True))
        try:
            call = _dispatch_sync_tool("list_source_tables", _inline("bigquery", {"project": "p"}))
            with pytest.raises(get_tool_error(), match="--allow-metadata-service"):
                asyncio.run(call)
        finally:
            policy_mod._current_policy = previous

    @pytest.mark.parametrize(
        "inline",
        [{"region": "us-east-1"}, {"region": "us-east-1", "auth_method": "instance_profile"}],
        ids=["default-iam_role", "instance_profile"],
    )
    def test_no_metadata_request_at_the_sink(self, metadata_endpoint, inline):
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _call_tool(
                "list_source_tables",
                _inline("glue", inline),
                read_only=True,
                allow_inline_credentials=True,
            )
        assert metadata_endpoint.requests == []

    def test_operator_opt_in_reaches_the_sink(self, metadata_endpoint):
        """Positive control for the test above: the logger sees the SDK once allowed."""
        with pytest.raises(Exception):
            _call_tool(
                "list_source_tables",
                _inline("glue", {"region": "us-east-1"}),
                read_only=True,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        assert metadata_endpoint.requests, "boto3 never consulted the metadata endpoint"

    @pytest.mark.parametrize("tool", _CATALOG_TOOLS)
    def test_every_catalog_tool_applies_the_inline_gate(self, tool, tmp_path):
        args = _inline("dataplex", {"project": "proj", "location": "eu"})
        args.update(fqn="proj.ds.t", output_path=str(tmp_path / "out.fluid.yaml"))
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _call_tool(tool, args, read_only=False, allow_inline_credentials=True)


@pytest.fixture
def profile_metadata_endpoint(metadata_endpoint, tmp_path, monkeypatch):
    """As ``metadata_endpoint``, plus a host AWS config whose profile has no keys.

    ``[default] region = ...`` is what ``aws configure set region`` writes, so
    it is the common shape on a cloud host. boto3 finds no keys in it and
    continues down its chain to the container and instance roles.
    """
    config = tmp_path / "aws-config"
    config.write_text("[default]\nregion = us-east-1\n", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    return metadata_endpoint


_HOST_PROFILE_INLINE = [
    pytest.param({"region": "us-east-1", "profile_name": "default"}, id="iam_role-profile"),
    pytest.param(
        {"region": "us-east-1", "auth_method": "sso", "profile_name": "default"},
        id="sso-profile",
    ),
    pytest.param(
        {
            "region": "us-east-1",
            "auth_method": "iam_role",
            "profile_name": "default",
            "access_key_id": "test-access-key-id",
            "secret_access_key": "test-secret-access-key",  # pragma: allowlist secret
        },
        id="iam_role-profile-plus-keys",
    ),
]


class TestInlineNamedProfileIsHostIdentity:
    """A named profile in an inline Glue block is gated like the instance role."""

    @pytest.mark.parametrize("inline", _HOST_PROFILE_INLINE)
    def test_no_metadata_request_at_the_sink(self, profile_metadata_endpoint, inline):
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _call_tool(
                "list_source_tables",
                _inline("glue", inline),
                read_only=True,
                allow_inline_credentials=True,
            )
        assert profile_metadata_endpoint.requests == []

    def test_operator_opt_in_reaches_the_sink(self, profile_metadata_endpoint):
        """Positive control: with the operator's opt-in the profile does reach it."""
        with pytest.raises(Exception):
            _call_tool(
                "list_source_tables",
                _inline("glue", {"region": "us-east-1", "profile_name": "default"}),
                read_only=True,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        assert profile_metadata_endpoint.requests, "boto3 never consulted the metadata endpoint"


def _stdio_list_tables(server_flags: List[str], inline: Dict[str, Any], cwd: Any) -> Any:
    """Call ``list_source_tables`` on a real ``fluid mcp serve`` over stdio."""
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fluid_build.cli", "mcp", "serve", *server_flags],
        env=dict(os.environ),
        cwd=str(cwd),
    )

    async def _run() -> Any:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                return await client.call_tool(
                    "list_source_tables",
                    {
                        "source": "glue",
                        "credentials": {"inline": inline},
                        # Name a table: a scope that omits ``tables`` fails
                        # validation before the adapter is built.
                        "scope": {"database": "analytics", "tables": ["orders"]},
                    },
                )

    return asyncio.run(_run())


class TestInlineNamedProfileOverStdio:
    """The same gate holds on the real server, at the SDK's network boundary."""

    @pytest.fixture(autouse=True)
    def _needs_mcp(self):
        pytest.importorskip("mcp")

    def test_inline_credentials_flag_alone_sends_no_metadata_request(
        self, profile_metadata_endpoint, tmp_path
    ):
        result = _stdio_list_tables(
            ["--allow-inline-credentials"],
            {"region": "us-east-1", "auth_method": "iam_role", "profile_name": "default"},
            tmp_path,
        )
        from fluid_build._mcp_compat import attr

        assert attr(result, "is_error", "isError") is True
        text = " ".join(getattr(block, "text", "") for block in result.content)
        assert "--allow-metadata-service" in text
        assert profile_metadata_endpoint.requests == []

    def test_operator_opt_in_reaches_the_sink(self, profile_metadata_endpoint, tmp_path):
        """Positive control: the logger sees the server's SDK once the operator allows it."""
        _stdio_list_tables(
            ["--allow-inline-credentials", "--allow-metadata-service"],
            {"region": "us-east-1", "auth_method": "iam_role", "profile_name": "default"},
            tmp_path,
        )
        assert profile_metadata_endpoint.requests, "boto3 never consulted the metadata endpoint"


_TEST_KEY_ID = "test-access-key-id"
_TEST_KEYS = {
    "access_key_id": _TEST_KEY_ID,
    "secret_access_key": "test-secret-access-key",  # pragma: allowlist secret
}

# Inline Glue ``iam_key`` blocks whose keys are blank, whitespace-only or
# missing. boto3 reads a blank key as "none supplied" and continues down its
# default chain to the container or instance role, so none of these carries
# the caller's credential, whatever its ``auth_method`` says.
_BLANK_GLUE_KEYS = [
    pytest.param({"access_key_id": "", "secret_access_key": ""}, id="empty-keys"),
    pytest.param(
        {"access_key_id": "", "secret_access_key": "", "session_token": ""},
        id="empty-keys-and-token",
    ),
    pytest.param({"access_key_id": "   ", "secret_access_key": "\t\n"}, id="whitespace-keys"),
    pytest.param(
        {"access_key_id": " ", "secret_access_key": " ", "session_token": " "},
        id="whitespace-keys-and-token",
    ),
    pytest.param(
        {"access_key_id": "", "secret_access_key": _TEST_KEYS["secret_access_key"]},
        id="empty-key-id",
    ),
    pytest.param({"access_key_id": _TEST_KEY_ID, "secret_access_key": ""}, id="empty-secret"),
    pytest.param(
        {"access_key_id": "", "secret_access_key": "", "session_token": "test-session-token"},
        id="token-without-keys",
    ),
    pytest.param({}, id="no-keys"),
    pytest.param(
        {"access_key_id": "", "secret_access_key": "", "profile_name": ""},
        id="empty-keys-and-profile",
    ),
]


def _glue_key_block(keys: Dict[str, Any]) -> Dict[str, Any]:
    return {"region": "us-east-1", "auth_method": "iam_key", **keys}


# The same shape for every other catalog the inline gate classifies.
_BLANK_MATERIAL_INLINE = [
    pytest.param(
        "bigquery",
        {"project": "proj", "auth_method": "service_account_json", "service_account_path": ""},
        id="bigquery-service-account-path",
    ),
    pytest.param(
        "dataplex",
        {
            "project": "proj",
            "location": "eu",
            "auth_method": "service_account_json",
            "service_account_path": "  ",
        },
        id="dataplex-service-account-path",
    ),
    pytest.param(
        "unity",
        {"host": "https://x.cloud.databricks.com", "auth_method": "pat", "token": ""},
        id="unity-pat",
    ),
    pytest.param(
        "unity",
        {
            "host": "https://x.cloud.databricks.com",
            "auth_method": "oauth_m2m",
            "oauth_client_id": "",
            "oauth_client_secret": " ",
        },
        id="unity-oauth_m2m",
    ),
    pytest.param(
        "snowflake",
        {"account": "acct", "user": "analyst", "auth_method": "password", "password": ""},
        id="snowflake-password",
    ),
    pytest.param(
        "snowflake",
        {
            "account": "acct",
            "user": "analyst",
            "auth_method": "private_key",
            "private_key_path": " ",
        },
        id="snowflake-private_key",
    ),
    pytest.param(
        "snowflake",
        {"account": "acct", "user": "analyst", "auth_method": "oauth", "oauth_token": ""},
        id="snowflake-oauth",
    ),
    pytest.param(
        "datahub",
        {"server": "https://datahub.example.com", "auth_method": "pat", "token": ""},
        id="datahub-pat",
    ),
    pytest.param(
        "datahub",
        {
            "server": "https://datahub.example.com",
            "auth_method": "oauth",
            "oauth_client_id": "",
            "oauth_client_secret": "",
        },
        id="datahub-oauth",
    ),
]


class TestBlankCredentialMaterialIsNotTheCallers:
    """A blank key, token or path is no credential material.

    ``--allow-inline-credentials`` alone must refuse such a block exactly like
    one that selects the host's identity by its ``auth_method``, with the same
    tool error naming the operator's switch.
    """

    @pytest.mark.parametrize("keys", _BLANK_GLUE_KEYS)
    def test_glue_refused_without_operator_opt_in(self, keys):
        with pytest.raises(RuntimeError, match="--allow-metadata-service") as exc:
            _build_source_adapter(
                _inline("glue", _glue_key_block(keys)), allow_inline_credentials=True
            )
        assert "blank" in str(exc.value)
        assert _ENV in str(exc.value)

    @pytest.mark.parametrize(("source", "inline"), _BLANK_MATERIAL_INLINE)
    def test_other_catalogs_refused_without_operator_opt_in(self, source, inline):
        with pytest.raises(RuntimeError, match="--allow-metadata-service") as exc:
            _build_source_adapter(_inline(source, inline), allow_inline_credentials=True)
        assert "blank" in str(exc.value)

    def test_data_mesh_manager_blank_api_key_fails_validation(self):
        """Its key is required, so a blank one fails the model before the gate runs."""
        from fluid_build.copilot.catalog.base import CatalogConfigError

        with pytest.raises(CatalogConfigError, match="api_key"):
            _build_source_adapter(
                _inline("datamesh_manager", {"server": "https://dmm.example.com", "api_key": ""}),
                allow_inline_credentials=True,
            )

    @pytest.mark.parametrize("keys", _BLANK_GLUE_KEYS)
    def test_no_request_at_the_sink(self, metadata_endpoint, keys):
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _call_tool(
                "list_source_tables",
                _inline("glue", _glue_key_block(keys)),
                read_only=True,
                allow_inline_credentials=True,
            )
        assert metadata_endpoint.requests == []
        assert metadata_endpoint.api_calls == []

    @pytest.mark.parametrize("keys", _BLANK_GLUE_KEYS)
    def test_operator_opt_in_does_not_turn_blank_keys_into_the_host_role(
        self, metadata_endpoint, keys
    ):
        """``--allow-metadata-service`` permits a block that asks for the host's
        identity (``instance_profile``). A block that names keys gets its keys
        or a refusal, never the instance role in their place."""
        from fluid_build.copilot.catalog.credentials import CredentialNotFoundError

        with pytest.raises(CredentialNotFoundError):
            _call_tool(
                "list_source_tables",
                _inline("glue", _glue_key_block(keys)),
                read_only=True,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        assert metadata_endpoint.requests == []
        assert metadata_endpoint.api_calls == []

    @pytest.mark.parametrize("tool", _CATALOG_TOOLS)
    def test_every_catalog_tool_refuses_blank_keys(self, metadata_endpoint, tool, tmp_path):
        args = _inline("glue", _glue_key_block({"access_key_id": "", "secret_access_key": ""}))
        args.update(fqn="analytics.orders", output_path=str(tmp_path / "out.fluid.yaml"))
        with pytest.raises(RuntimeError, match="--allow-metadata-service"):
            _call_tool(tool, args, read_only=False, allow_inline_credentials=True)
        assert metadata_endpoint.requests == []

    def test_test_keys_sign_the_call_without_the_metadata_service(self, metadata_endpoint):
        """Positive control: the logger sees the SDK's call, signed with the caller's key."""
        out = _call_tool(
            "list_source_tables",
            _inline("glue", _glue_key_block(_TEST_KEYS)),
            read_only=True,
            allow_inline_credentials=True,
        )
        assert out == {"tables": []}
        assert metadata_endpoint.api_calls == [_TEST_KEY_ID]
        assert metadata_endpoint.requests == []


def _glue_tool_call(tool: str, inline: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    arguments: Dict[str, Any] = {"source": "glue", "credentials": {"inline": inline}}
    if tool in ("inspect_source_table", "list_source_lineage"):
        arguments["fqn"] = "analytics.orders"
    else:
        # Name a table: a scope that omits ``tables`` fails validation before
        # the adapter is built.
        arguments["scope"] = {"database": "analytics", "tables": ["orders"]}
    return tool, arguments


_EMPTY_KEYS = _glue_key_block({"access_key_id": "", "secret_access_key": ""})
_STDIO_BLANK_CASES: Dict[str, Tuple[str, Dict[str, Any]]] = {
    **{
        f"list-{param.id}": _glue_tool_call("list_source_tables", _glue_key_block(param.values[0]))
        for param in _BLANK_GLUE_KEYS
    },
    "inspect-empty-keys": _glue_tool_call("inspect_source_table", _EMPTY_KEYS),
    "lineage-empty-keys": _glue_tool_call("list_source_lineage", _EMPTY_KEYS),
    "glossary-empty-keys": _glue_tool_call("list_source_glossary", _EMPTY_KEYS),
}
_STDIO_CONTROL = "control-test-keys"
_STDIO_CASES = {
    **_STDIO_BLANK_CASES,
    _STDIO_CONTROL: _glue_tool_call("list_source_tables", _glue_key_block(_TEST_KEYS)),
}

# What one call produced: the tool result, then the metadata requests and the
# signed API calls the logger saw while it ran.
_Seen = Tuple[Any, List[str], List[Optional[str]]]


def _stdio_session(server_flags: List[str], home: Any) -> Dict[str, _Seen]:
    """Make every ``_STDIO_CASES`` call on one real ``fluid mcp serve`` over stdio.

    The server's environment is the reviewer's reproduction: no AWS credential
    variables, a host config whose ``[default]`` profile holds only a region,
    and the metadata and Glue endpoints on a loopback logger.
    """
    import os
    import sys

    pytest.importorskip("boto3")
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    logger = AwsEndpointLogger()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv(_ENV, raising=False)
            point_boto3_at(logger, home, mp)
            config = home / "aws-config"
            config.write_text("[default]\nregion = us-east-1\n", encoding="utf-8")
            mp.setenv("AWS_CONFIG_FILE", str(config))
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "fluid_build.cli", "mcp", "serve", *server_flags],
                env=dict(os.environ),
                cwd=str(home),
            )

            async def _run() -> Dict[str, _Seen]:
                seen: Dict[str, _Seen] = {}
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        for case, (tool, arguments) in _STDIO_CASES.items():
                            logger.clear()
                            result = await client.call_tool(tool, arguments)
                            seen[case] = (result, list(logger.requests), list(logger.api_calls))
                return seen

            return asyncio.run(_run())
    finally:
        logger.close()


@pytest.fixture(scope="module")
def stdio_inline_only(tmp_path_factory) -> Dict[str, _Seen]:
    return _stdio_session(["--allow-inline-credentials"], tmp_path_factory.mktemp("inline"))


@pytest.fixture(scope="module")
def stdio_opted_in(tmp_path_factory) -> Dict[str, _Seen]:
    return _stdio_session(
        ["--allow-inline-credentials", "--allow-metadata-service"],
        tmp_path_factory.mktemp("opted-in"),
    )


def _is_error(result: Any) -> bool:
    from fluid_build._mcp_compat import attr

    return attr(result, "is_error", "isError") is True


def _text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


class TestBlankCredentialMaterialOverStdio:
    """The same holds on the real server, at the SDK's network boundary."""

    @pytest.mark.parametrize("case", list(_STDIO_BLANK_CASES))
    def test_refused_with_no_metadata_request(self, stdio_inline_only, case):
        result, metadata_requests, api_calls = stdio_inline_only[case]
        assert metadata_requests == []
        assert api_calls == []
        assert _is_error(result)
        assert "--allow-metadata-service" in _text(result)

    @pytest.mark.parametrize("case", list(_STDIO_BLANK_CASES))
    def test_operator_opt_in_still_sends_no_metadata_request(self, stdio_opted_in, case):
        _result, metadata_requests, api_calls = stdio_opted_in[case]
        assert metadata_requests == []
        assert api_calls == []

    @pytest.mark.parametrize("session", ["stdio_inline_only", "stdio_opted_in"])
    def test_test_keys_sign_the_call_without_the_metadata_service(self, request, session):
        """Positive control: the logger does see this server's SDK, with the caller's key."""
        result, metadata_requests, api_calls = request.getfixturevalue(session)[_STDIO_CONTROL]
        assert not _is_error(result), _text(result)
        assert api_calls == [_TEST_KEY_ID]
        assert metadata_requests == []
