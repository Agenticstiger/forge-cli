# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid apply`` checks the consumed subjects plan left to it: those that need a credential.

The credential is resolved under the secrets policy for the registry's host, then the
same read and comparison as plan runs before any ``tofu apply``.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from fluid_build.cli._common import CLIError
from fluid_build.schema_registry import verify as verify_mod
from fluid_build.schema_registry.clients import ConfluentRegistry
from fluid_build.schema_registry.verify import verify_at_apply

from .conftest import consumed_contract
from .test_verify import COLUMNS, FakeConfluent

pytestmark = pytest.mark.unit

URL = "https://sr.example.com"
REF = "env://SR_AUTH"  # pragma: allowlist secret - a reference, not a value
ENV = {
    "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST": "sr.example.com",
    "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST": "env://SR_AUTH@sr.example.com",  # pragma: allowlist secret - a test reference, not a value
}


def _contract(**registry):
    spec = {"kind": "confluent", "url": URL, "auth": {"secretRef": REF}, **registry}
    return consumed_contract(spec, columns=COLUMNS)


@pytest.fixture
def resolved(monkeypatch):
    """``resolve_secret`` without a store: records what it was asked."""
    from pydantic import SecretStr

    calls = []

    def resolve_secret(ref, *, host=None, environ=None, **kw):
        calls.append({"ref": ref, "host": host})
        return SecretStr("key:s3cret")  # pragma: allowlist secret - a test value

    monkeypatch.setattr("fluid_build.secrets.resolve.resolve_secret", resolve_secret)
    return calls


def _factory(fake, seen):
    def make(url, credential=None):
        seen.append(credential)
        return fake

    return make


def test_a_compatible_subject_passes_with_the_credential(resolved):
    seen = []
    records = verify_at_apply(_contract(), environ=ENV, confluent=_factory(FakeConfluent(), seen))
    assert resolved == [{"ref": REF, "host": "sr.example.com"}]
    assert seen == ["key:s3cret"]
    assert records[0]["result"] == "compatible" and records[0]["version"] == 7


def test_an_incompatible_subject_refuses_before_tofu(resolved):
    fake = FakeConfluent(compatible=False, messages=["reader type: int not compatible"])
    with pytest.raises(CLIError) as err:
        verify_at_apply(_contract(), environ=ENV, confluent=_factory(fake, []))
    assert err.value.event == "apply_schema_registry_failed"
    assert "reader type: int not compatible" in str(err.value.context["errors"])
    assert "s3cret" not in str(err.value.context)


def test_a_dry_run_fetches_nothing(resolved):
    assert verify_at_apply(_contract(), dry_run=True, environ=ENV, confluent=None) == []
    assert resolved == []


def test_a_contract_without_credentialed_subjects_is_left_alone(resolved):
    contract = consumed_contract({"kind": "confluent", "url": URL}, columns=COLUMNS)
    assert verify_at_apply(contract, environ=ENV) == []
    assert resolved == []


@pytest.mark.parametrize(
    "env, says",
    [
        (
            {
                "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST": ENV[
                    "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"
                ]
            },
            "is not in",
        ),
        ({"FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST": "sr.example.com"}, "secretRef"),
    ],
)
def test_a_missing_operator_entry_refuses_and_reads_no_secret(resolved, env, says):
    with pytest.raises(CLIError) as err:
        verify_at_apply(_contract(), environ=env, confluent=_factory(FakeConfluent(), []))
    assert says in str(err.value.context["errors"])
    assert resolved == []


def test_http_with_a_credential_is_refused_at_apply(resolved):
    with pytest.raises(CLIError) as err:
        verify_at_apply(
            _contract(url="http://sr.example.com"),
            environ=ENV,
            confluent=_factory(FakeConfluent(), []),
        )
    assert "https only" in str(err.value.context["errors"])
    assert resolved == []


def test_the_secret_policy_refusal_is_reported(monkeypatch):
    from fluid_build.secrets.errors import SecretPolicyError

    def refuse(*a, **kw):
        raise SecretPolicyError(
            what="env://SR_AUTH: the apply stage may not fetch", why="w", fix="f", doc="d"
        )

    monkeypatch.setattr("fluid_build.secrets.resolve.resolve_secret", refuse)
    with pytest.raises(CLIError) as err:
        verify_at_apply(_contract(), environ=ENV, confluent=_factory(FakeConfluent(), []))
    assert "auth.secretRef" in str(err.value.context["errors"])


def test_the_client_sends_basic_auth_over_https(monkeypatch):
    sent = {}

    def handler(request):
        sent["auth"] = request.headers.get("Authorization")
        body = {"subject": "orders-value", "version": 1, "schema": '"string"'}
        return httpx.Response(200, json=body)

    def fake_client(*, base_url, headers, require_https, **kw):
        sent["require_https"] = require_https
        return httpx.Client(
            transport=httpx.MockTransport(handler), base_url=base_url, headers=headers
        )

    monkeypatch.setattr("fluid_build.util.safe_http.safe_httpx_client", fake_client)
    ConfluentRegistry(URL, credential="key:s3cret").get_version(
        "orders-value"
    )  # pragma: allowlist secret
    assert sent["auth"] == "Basic " + base64.b64encode(b"key:s3cret").decode()
    assert sent["require_https"] is True


def test_a_credentialed_client_refuses_plain_http():
    from fluid_build.util.safe_http import UnsafeURLError

    with pytest.raises(UnsafeURLError):
        ConfluentRegistry(
            "http://93.184.216.34", credential="k:v"
        )._http()  # pragma: allowlist secret


def test_registry_messages_lose_control_characters():
    from fluid_build.schema_registry.clients import RegistryError

    assert "\x1b" not in str(RegistryError("bad \x1b[31mred\x1b[0m"))


def test_the_engine_calls_it_once_before_any_tofu_apply():
    import inspect

    from fluid_build.cli import _apply_opentofu_engine as engine

    source = inspect.getsource(engine._apply_via_opentofu)
    assert source.count("_verify_registry_at_apply(") == 1
    assert source.index("_verify_registry_at_apply(") < source.index("if bound is not None")
    assert verify_mod.verify_at_apply is verify_at_apply


def _produced_confluent():
    from .conftest import produced_contract

    return produced_contract({"kind": "confluent", "url": URL})


PRODUCED_ENV = {
    "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST": "sr.example.com",
    "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST": "SCHEMA_REGISTRY_API_SECRET@sr.example.com",  # pragma: allowlist secret - a test reference, not a value
}


def test_apply_refuses_an_unset_operator_endpoint_for_a_produced_subject():
    with pytest.raises(CLIError) as err:
        verify_at_apply(_produced_confluent(), environ=PRODUCED_ENV)
    assert "SCHEMA_REGISTRY_REST_ENDPOINT" in str(err.value.context["errors"])


def test_apply_refuses_another_operator_endpoint_even_on_a_dry_run():
    env = {**PRODUCED_ENV, "SCHEMA_REGISTRY_REST_ENDPOINT": "https://elsewhere.example.com"}
    with pytest.raises(CLIError) as err:
        verify_at_apply(_produced_confluent(), dry_run=True, environ=env)
    assert "another registry" in str(err.value.context["errors"])


def test_apply_accepts_the_contracts_registry():
    env = {**PRODUCED_ENV, "SCHEMA_REGISTRY_REST_ENDPOINT": URL + "/"}
    assert verify_at_apply(_produced_confluent(), environ=env) == []


def test_apply_checks_only_the_subjects_this_provider_registers():
    contract = _produced_confluent()
    assert verify_at_apply(contract, dry_run=True, environ={}, provider="aws") == []
    with pytest.raises(CLIError):
        verify_at_apply(contract, dry_run=True, environ={}, provider="confluent")
