# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The credential chain's operator-store step (``FLUID_CREDENTIAL_STORE``)."""

from __future__ import annotations

from typing import List, Optional

import pytest
from pydantic import SecretStr

import fluid_build.secrets
from fluid_build.credentials.resolver import (
    BaseCredentialResolver,
    CredentialConfig,
    CredentialError,
)
from fluid_build.secrets.errors import SecretPolicyError, SecretResolveError


class Chain(BaseCredentialResolver):
    """Only the secret-store step is real: no keyring, no files, no prompt."""

    def __init__(self) -> None:
        super().__init__("snowflake", CredentialConfig(allow_prompt=False))

    def _get_provider_default(self, key: str, **kwargs) -> Optional[str]:
        return None

    def _get_from_keyring(self, key):
        return None

    def _get_from_encrypted_file(self, key):
        return None

    def _get_from_config(self, key):
        return None

    def _get_from_dotenv(self, key):
        return None

    def _project_endpoint_in_effect(self):
        return False


class Calls(list):
    """The references the chain asked for; ``answers[0]`` (a value or an error) is returned."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: List[object] = []

    def __eq__(self, other):
        return list(self) == other


@pytest.fixture
def seen(monkeypatch) -> Calls:
    """Replace the resolver the chain calls; records each reference it was asked for."""
    calls = Calls()

    def fake(reference, **kwargs):
        calls.append(reference)
        answer = calls.answers[0] if calls.answers else SecretStr("the-value")
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(fluid_build.secrets, "resolve_credential", fake, raising=False)
    monkeypatch.setattr("fluid_build.secrets.resolve.resolve_credential", fake)
    return calls


def store_error(cls, reason: Optional[str]) -> Exception:
    return cls(
        what="vault://kv/x#y did not resolve",
        why="the store said no.",
        fix="Fix the store.",
        doc="https://example.invalid/doc",
        extras={"reason": reason} if reason else {},
    )


def test_unset_store_skips_the_step(seen):
    assert Chain()._get_from_secret_store("password") is None
    assert seen == []


def test_blank_store_skips_the_step(seen, monkeypatch):
    monkeypatch.setenv("FLUID_CREDENTIAL_STORE", "   ")
    assert Chain()._get_from_secret_store("password") is None
    assert seen == []


@pytest.mark.parametrize(
    "store, expected",
    [
        ("vault://kv/fluid/credentials", "vault://kv/fluid/credentials/snowflake#password"),
        ("vault://kv/fluid/credentials/", "vault://kv/fluid/credentials/snowflake#password"),
        ("openbao://kv/fluid/credentials", "openbao://kv/fluid/credentials/snowflake#password"),
        ("aws-sm://fluid/credentials", "aws-sm://fluid/credentials/snowflake#password"),
        ("aws://fluid/credentials/", "aws://fluid/credentials/snowflake#password"),
        ("VAULT://kv/fluid", "VAULT://kv/fluid/snowflake#password"),
    ],
)
def test_reference_is_store_provider_key(seen, monkeypatch, store, expected):
    monkeypatch.setenv("FLUID_CREDENTIAL_STORE", store)
    value = Chain()._get_from_secret_store("password")
    assert seen == [expected]
    assert value == "the-value"
    assert type(value) is str


@pytest.mark.parametrize(
    "store", ["env://FLUID_CRED", "file:///run/secrets/x", "gcp-sm://projects/p/secrets/s", "plain"]
)
def test_stores_without_fields_are_refused(seen, monkeypatch, store):
    monkeypatch.setenv("FLUID_CREDENTIAL_STORE", store)
    with pytest.raises(CredentialError) as info:
        Chain()._get_from_secret_store("password")
    assert "FLUID_CREDENTIAL_STORE" in str(info.value)
    assert info.value.suggestions
    assert seen == []


def test_not_found_falls_through_to_the_next_source(seen, monkeypatch):
    monkeypatch.setenv("FLUID_CREDENTIAL_STORE", "vault://kv/fluid/credentials")
    seen.answers.append(store_error(SecretResolveError, "not_found"))
    chain = Chain()
    assert chain._get_from_secret_store("password") is None
    assert chain.get_credential("password", required=False) is None
    with pytest.raises(CredentialError, match="Required credential not found"):
        chain.get_credential("password")


@pytest.mark.parametrize(
    "error",
    [
        store_error(SecretResolveError, "denied"),
        store_error(SecretResolveError, "unreachable"),
        store_error(SecretPolicyError, None),
    ],
)
def test_other_failures_raise_with_the_why_and_fix(seen, monkeypatch, error):
    monkeypatch.setenv("FLUID_CREDENTIAL_STORE", "vault://kv/fluid/credentials")
    seen.answers.append(error)
    with pytest.raises(CredentialError) as info:
        Chain().get_credential("password", required=False)
    assert "the store said no." in str(info.value)
    assert info.value.suggestions == ["Fix the store."]


def test_value_comes_through_the_whole_chain(seen, monkeypatch):
    monkeypatch.setenv("FLUID_CREDENTIAL_STORE", "vault://kv/fluid/credentials")
    assert Chain().get_credential("password") == "the-value"


def test_environment_still_wins_over_the_store(seen, monkeypatch):
    monkeypatch.setenv("FLUID_CREDENTIAL_STORE", "vault://kv/fluid/credentials")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "from-env")
    assert Chain().get_credential("password") == "from-env"
    assert seen == []


def test_real_resolver_refuses_a_scheme_the_operator_did_not_enable(monkeypatch):
    """No fake: the real policy refuses before any store is asked, and the chain raises."""
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env")
    monkeypatch.setenv("FLUID_CREDENTIAL_STORE", "vault://kv/fluid/credentials")
    with pytest.raises(CredentialError) as info:
        Chain().get_credential("password", required=False)
    assert "vault" in str(info.value)
    assert "FLUID_SECRET_SCHEMES" in " ".join(info.value.suggestions)
