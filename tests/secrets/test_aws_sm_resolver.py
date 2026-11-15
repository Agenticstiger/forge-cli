# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``aws-sm://`` resolver against a fake boto3: no AWS call can be made."""

from __future__ import annotations

import json
import sys
import types
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build.secrets import secret_run
from fluid_build.secrets._aws_sm import AwsSecretsManagerResolver
from fluid_build.secrets.errors import SecretResolveError
from fluid_build.secrets.ref import parse_secret_ref

SECRET_VALUE = "correct-horse-battery-staple"  # pragma: allowlist secret
ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:app/db-AbCdEf"
VERSION = "11111111-2222-3333-4444-555555555555"


class ClientError(Exception):
    def __init__(self, code: str, message: str = "boom") -> None:
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


NoCredentialsError = type("NoCredentialsError", (Exception,), {})
PartialCredentialsError = type("PartialCredentialsError", (Exception,), {})
NoRegionError = type("NoRegionError", (Exception,), {})


class FakeClient:
    def __init__(self, region: Optional[str]) -> None:
        self.region = region
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.secret: Any = {"SecretString": SECRET_VALUE}
        self.described: Any = {"VersionIdsToStages": {VERSION: ["AWSCURRENT"]}}

    def _answer(self, value: Any) -> Any:
        if isinstance(value, Exception):
            raise value
        return value

    def get_secret_value(self, **kwargs: Any) -> Any:
        self.calls.append(("get_secret_value", kwargs))
        return self._answer(self.secret)

    def describe_secret(self, **kwargs: Any) -> Any:
        self.calls.append(("describe_secret", kwargs))
        return self._answer(self.described)


@pytest.fixture
def boto(monkeypatch):
    """A fake ``boto3``; ``clients`` records every client it built, in order."""
    clients: List[FakeClient] = []
    template: Dict[str, Any] = {}

    def client(service: str, region_name: Optional[str] = None) -> FakeClient:
        assert service == "secretsmanager"
        made = FakeClient(region_name)
        for name, value in template.items():
            setattr(made, name, value)
        clients.append(made)
        return made

    module = types.SimpleNamespace(client=client, template=template, clients=clients)
    monkeypatch.setitem(sys.modules, "boto3", module)
    return module


def ref(text: str):
    return parse_secret_ref(text)


def resolve(text: str = "aws-sm://app/db") -> str:
    with secret_run("apply") as run:
        return AwsSecretsManagerResolver().resolve(ref(text), run)


def failure(text: str = "aws-sm://app/db") -> SecretResolveError:
    with pytest.raises(SecretResolveError) as info:
        resolve(text)
    return info.value


def text_of(exc: SecretResolveError) -> str:
    return " ".join([exc.what, exc.why, exc.fix, json.dumps(exc.extras)])


# ── values ───────────────────────────────────────────────────────────────────


def test_plain_secret_string(boto):
    assert resolve() == SECRET_VALUE
    (client,) = boto.clients
    assert client.calls == [
        ("get_secret_value", {"SecretId": "app/db"})  # pragma: allowlist secret
    ]
    assert client.region is None


def test_binary_secret_is_decoded_as_utf8(boto):
    boto.template["secret"] = {"SecretBinary": "pässword".encode()}  # pragma: allowlist secret
    assert resolve() == "pässword"


def test_binary_secret_that_is_not_utf8_is_invalid(boto):
    boto.template["secret"] = {"SecretBinary": b"\xff\xfe\xfa"}
    assert failure().extras["reason"] == "invalid"


def test_secret_without_a_value_is_empty(boto):
    boto.template["secret"] = {}
    assert failure().extras["reason"] == "empty"


def test_json_field(boto):
    boto.template["secret"] = {"SecretString": json.dumps({"user": "u", "password": SECRET_VALUE})}
    assert resolve("aws-sm://app/db#password") == SECRET_VALUE


def test_missing_json_field_is_not_found(boto):
    boto.template["secret"] = {"SecretString": json.dumps({"user": SECRET_VALUE})}
    exc = failure("aws-sm://app/db#password")
    assert exc.extras["reason"] == "not_found"
    assert SECRET_VALUE not in text_of(exc)


def test_field_on_a_secret_that_is_not_json_is_invalid(boto):
    exc = failure("aws-sm://app/db#password")
    assert exc.extras["reason"] == "invalid"
    assert SECRET_VALUE not in text_of(exc)


@pytest.mark.parametrize("payload", [{"password": 1234567}, {"password": {"in": SECRET_VALUE}}])
def test_non_string_json_value_is_invalid(boto, payload):
    boto.template["secret"] = {"SecretString": json.dumps(payload)}
    exc = failure("aws-sm://app/db#password")
    assert exc.extras["reason"] == "invalid"
    assert SECRET_VALUE not in text_of(exc)


def test_json_array_with_a_field_is_not_found(boto):
    boto.template["secret"] = {"SecretString": json.dumps([SECRET_VALUE])}
    exc = failure("aws-sm://app/db#password")
    assert exc.extras["reason"] == "not_found"
    assert SECRET_VALUE not in text_of(exc)


def test_version_id_is_passed_when_given(boto):
    resolve(f"aws-sm://app/db?version={VERSION}")
    expected = {"SecretId": "app/db", "VersionId": VERSION}  # pragma: allowlist secret
    assert boto.clients[0].calls[0][1] == expected


def test_alias_aws_is_accepted(boto):
    assert resolve("aws://app/db") == SECRET_VALUE


# ── clients and regions ──────────────────────────────────────────────────────


def test_region_comes_from_the_arn(boto):
    resolve(f"aws-sm://{ARN}")
    assert boto.clients[0].region == "eu-west-1"
    assert boto.clients[0].calls[0][1]["SecretId"] == ARN


def test_one_client_per_region_per_run(boto):
    other = "arn:aws:secretsmanager:us-east-2:123456789012:secret:other-AbCdEf"
    with secret_run("apply") as run:
        resolver = AwsSecretsManagerResolver()
        resolver.resolve(ref("aws-sm://app/db"), run)
        resolver.resolve(ref("aws-sm://app/other"), run)
        resolver.resolve(ref(f"aws-sm://{ARN}"), run)
        resolver.resolve(ref(f"aws-sm://{ARN}"), run)
        resolver.resolve(ref(f"aws-sm://{other}"), run)
    assert [c.region for c in boto.clients] == [None, "eu-west-1", "us-east-2"]


def test_a_new_run_builds_a_new_client(boto):
    resolve()
    resolve()
    assert len(boto.clients) == 2


# ── errors ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code, reason",
    [
        ("ResourceNotFoundException", "not_found"),
        ("AccessDeniedException", "denied"),
        ("UnrecognizedClientException", "denied"),
        ("InvalidRequestException", "not_found"),
        ("ThrottlingException", "unreachable"),
        ("InternalServiceError", "unreachable"),
    ],
)
def test_error_codes(boto, code, reason):
    boto.template["secret"] = ClientError(code, f"details with {SECRET_VALUE}")
    exc = failure()
    assert exc.extras["reason"] == reason
    assert SECRET_VALUE not in text_of(exc)


def test_no_credentials(boto):
    boto.template["secret"] = NoCredentialsError("Unable to locate credentials")
    exc = failure()
    assert exc.extras["reason"] == "denied"
    assert "no AWS credentials" in exc.why


def test_partial_credentials(boto):
    boto.template["secret"] = PartialCredentialsError("partial")
    assert failure().extras["reason"] == "denied"


def test_no_region(boto):
    boto.template["secret"] = NoRegionError("You must specify a region.")
    exc = failure()
    assert exc.extras["reason"] == "invalid"
    assert "AWS_REGION" in exc.fix


def test_other_exception_is_unreachable_and_names_the_type_only(boto):
    boto.template["secret"] = RuntimeError(f"socket said {SECRET_VALUE}")
    exc = failure()
    assert exc.extras["reason"] == "unreachable"
    assert "RuntimeError" in exc.why
    assert SECRET_VALUE not in text_of(exc)


@pytest.mark.parametrize(
    "raised, reason, words",
    [
        (NoRegionError("You must specify a region."), "invalid", "AWS_REGION"),
        (
            type("ProfileNotFound", (Exception,), {})("profile (nope) not found"),
            "invalid",
            "AWS_PROFILE",
        ),
        (RuntimeError(f"endpoint said {SECRET_VALUE}"), "unreachable", "reachable"),
    ],
    ids=["no-region", "unknown-profile", "other"],
)
def test_a_client_that_cannot_be_built_is_a_typed_error(monkeypatch, raised, reason, words):
    """boto3 resolves the region and the profile when it builds the client."""

    def client(service: str, region_name: Optional[str] = None) -> FakeClient:
        raise raised

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    exc = failure()
    assert exc.extras["reason"] == reason
    assert words in exc.why + exc.fix
    assert SECRET_VALUE not in text_of(exc)


def test_boto3_missing_names_the_aws_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    exc = failure()
    assert exc.extras["reason"] == "no_resolver"
    assert "data-product-forge[aws]" in exc.fix


# ── exists() ─────────────────────────────────────────────────────────────────


def exists(text: str = "aws-sm://app/db"):
    with secret_run("apply") as run:
        return AwsSecretsManagerResolver().exists(ref(text), run)


def test_exists_uses_describe_and_never_reads_the_value(boto):
    assert exists().found is True
    assert [name for name, _ in boto.clients[0].calls] == ["describe_secret"]


def test_deleted_secret_does_not_exist(boto):
    boto.template["described"] = {"DeletedDate": "2026-01-01"}
    assert exists().found is False


def test_missing_version_does_not_exist(boto):
    assert exists(f"aws-sm://app/db?version={'a' * 32}").found is False


def test_present_version_exists(boto):
    assert exists(f"aws-sm://app/db?version={VERSION}").found is True


def test_not_found_does_not_exist(boto):
    boto.template["described"] = ClientError("ResourceNotFoundException")
    assert exists().found is False


def test_denied_is_unknown(boto):
    boto.template["described"] = ClientError("AccessDeniedException")
    result = exists()
    assert result.found is None
    assert "may not read" in result.detail


# ── A1: an ambient region never sends a lookup to Secrets Manager ────────────


def test_aws_region_alone_never_reaches_aws_or_the_secret_registry(monkeypatch):
    import fluid_build.secrets
    from fluid_build.credentials.adapters import SnowflakeCredentialAdapter

    def fail(*args, **kwargs):
        raise AssertionError("the credential chain reached a secret store")

    class Boom:
        def __getattr__(self, name):
            fail()

    class Adapter(SnowflakeCredentialAdapter):
        """Only the store step is real: no keyring, no files, no prompt."""

        def _get_from_keyring(self, key):
            return None

        def _get_from_dotenv(self, key):
            return None

        def _project_endpoint_in_effect(self):
            return False

    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("GCP_PROJECT", "some-project")
    monkeypatch.delenv("FLUID_CREDENTIAL_STORE", raising=False)
    monkeypatch.setitem(sys.modules, "boto3", Boom())
    monkeypatch.setattr(fluid_build.secrets, "resolve_credential", fail, raising=False)
    monkeypatch.setattr("fluid_build.secrets.resolve.resolve_credential", fail)

    adapter = Adapter()
    assert adapter._get_from_secret_store("zz_missing_key") is None
    assert adapter.get_credential("zz_missing_key", required=False) is None
