# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Resolving a reference: policy before the store, cache, redaction and audit."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from fluid_build.observability import tracing
from fluid_build.observability.secret_redactor import redact_secret_text
from fluid_build.secrets import registry, secret_run
from fluid_build.secrets.errors import SecretPolicyError, SecretRefError, SecretResolveError
from fluid_build.secrets.policy import AcquisitionSecretPolicyError
from fluid_build.secrets.resolve import resolve_credential, resolve_secret, secret_exists

VALUE = "Zq9xKw-7TrLpV2mN_sample"
REMOTE = "db.example.com"


class FakeResolver:
    scheme = "op"

    def __init__(self, value=VALUE):
        self.value = value
        self.resolved = []
        self.probed = []

    def resolve(self, ref, run):
        self.resolved.append(ref.canonical)
        return self.value

    def exists(self, ref, run):
        self.probed.append(ref.canonical)
        return registry.Existence(True, "fake")


class BrokenResolver(FakeResolver):
    """A pack that breaks the protocol: it raises something other than SecretResolveError."""

    def resolve(self, ref, run):
        raise RuntimeError(f"socket said {VALUE}")

    def exists(self, ref, run):
        raise RuntimeError(f"socket said {VALUE}")


@pytest.fixture
def fake(monkeypatch):
    resolver = FakeResolver()
    registry.register_resolver("op", resolver)
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env,op")
    return resolver


@pytest.fixture
def events(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        tracing, "add_span_event", lambda name, attributes=None: recorded.append((name, attributes))
    )
    return recorded


# ── a resolver that breaks the protocol ──────────────────────────────────────


@pytest.mark.unit
def test_a_resolver_raising_anything_else_is_a_typed_refusal(monkeypatch):
    registry.register_resolver("op", BrokenResolver())
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env,op")
    with secret_run("apply"), pytest.raises(SecretResolveError) as info:
        resolve_secret("op://a/b", unsent=True)
    assert "RuntimeError" in info.value.why
    assert VALUE not in info.value.as_json()
    with secret_run("secrets-check"), pytest.raises(SecretResolveError) as probed:
        secret_exists("op://a/b")
    assert VALUE not in probed.value.as_json()


# ── destination argument ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_exactly_one_of_connection_host_unsent_is_required(fake):
    with secret_run("apply"):
        with pytest.raises(TypeError):
            resolve_secret("op://a/b")
        with pytest.raises(TypeError):
            resolve_secret("op://a/b", host="localhost", unsent=True)
        with pytest.raises(TypeError):
            resolve_secret("op://a/b", connection={"host": "localhost"}, host="localhost")
    assert fake.resolved == []


# ── stage gate ───────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_refused_outside_a_stage(fake):
    with pytest.raises(SecretPolicyError) as info:
        resolve_secret("op://a/b", unsent=True)
    assert "outside a pipeline stage" in info.value.what
    assert fake.resolved == []


@pytest.mark.unit
@pytest.mark.parametrize("stage", ["validate", "plan", "bundle", "diff"])
def test_refused_inside_lint_stages(fake, stage):
    with secret_run(stage):
        with pytest.raises(SecretPolicyError) as info:
            resolve_secret("op://a/b", unsent=True)
    assert "never fetches" in info.value.what
    assert fake.resolved == []


@pytest.mark.unit
def test_apply_resolves_to_a_masked_secretstr(monkeypatch):
    monkeypatch.setenv("FLUID_TEST_TOKEN", VALUE)
    with secret_run("apply"):
        secret = resolve_secret("env://FLUID_TEST_TOKEN", unsent=True)
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == VALUE
    assert VALUE not in repr(secret)
    assert VALUE not in str(secret)


@pytest.mark.unit
def test_resolved_value_is_registered_with_the_redactor(monkeypatch):
    value = "Hq4Ty8-Rm2NvXc6_registered"
    monkeypatch.setenv("FLUID_TEST_TOKEN", value)
    with secret_run("apply"):
        resolve_secret("env://FLUID_TEST_TOKEN", unsent=True)
    assert value not in redact_secret_text(f"client said: bad token {value} sorry")


# ── cache ────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_second_resolve_in_a_run_hits_the_cache_and_a_new_run_fetches_again(fake):
    with secret_run("apply"):
        first = resolve_secret("op://a/b", unsent=True)
        second = resolve_secret("op://a/b", unsent=True)
        assert first is second
        assert fake.resolved == ["op://a/b"]
    with secret_run("apply"):
        resolve_secret("op://a/b", unsent=True)
    assert fake.resolved == ["op://a/b", "op://a/b"]


@pytest.mark.unit
def test_empty_value_is_refused(fake):
    fake.value = ""
    with secret_run("apply"):
        with pytest.raises(SecretResolveError) as info:
            resolve_secret("op://a/b", unsent=True)
    assert info.value.extras["reason"] == "empty"


# ── policy happens before the store ──────────────────────────────────────────


@pytest.mark.unit
def test_scheme_not_enabled_never_reaches_the_resolver(fake, monkeypatch):
    monkeypatch.delenv("FLUID_SECRET_SCHEMES")
    with secret_run("apply"):
        with pytest.raises(SecretPolicyError):
            resolve_secret("op://a/b", unsent=True)
    assert fake.resolved == []


@pytest.mark.unit
def test_prefix_not_allowed_never_reaches_the_resolver(fake, monkeypatch):
    monkeypatch.setenv("FLUID_SECRET_PREFIXES", "op://allowed/")
    with secret_run("apply"):
        with pytest.raises(SecretPolicyError):
            resolve_secret("op://other/b", unsent=True)
        resolve_secret("op://allowed/b", unsent=True)
    assert fake.resolved == ["op://allowed/b"]


@pytest.mark.unit
def test_stage_not_allowed_never_reaches_the_resolver(fake):
    with secret_run("verify"):
        with pytest.raises(SecretPolicyError):
            resolve_secret("op://a/b", unsent=True)
    assert fake.resolved == []


@pytest.mark.unit
def test_destination_not_allowed_never_reaches_the_resolver(fake, monkeypatch):
    with secret_run("apply"):
        with pytest.raises(AcquisitionSecretPolicyError):
            resolve_secret("op://a/b", host=REMOTE)
        with pytest.raises(AcquisitionSecretPolicyError):
            resolve_secret("op://a/b", connection={"host": REMOTE})
    assert fake.resolved == []
    monkeypatch.setenv("FLUID_ACQUISITION_SECRET_REF_ALLOWLIST", f"op://a/b@{REMOTE}")
    with secret_run("apply"):
        resolve_secret("op://a/b", host=REMOTE)
    assert fake.resolved == ["op://a/b"]


@pytest.mark.unit
def test_unsent_skips_the_destination_policy(fake):
    with secret_run("apply"):
        resolve_secret("op://a/b", unsent=True)
    assert fake.resolved == ["op://a/b"]


# ── audit ────────────────────────────────────────────────────────────────────


def _outcomes(events):
    assert {name for name, _ in events} == {"fluid.secret.resolve"}
    return [attrs["fluid.secret.outcome"] for _, attrs in events]


@pytest.mark.unit
def test_one_audit_event_per_outcome_without_the_value(fake, events, monkeypatch):
    monkeypatch.setenv("FLUID_SECRET_PREFIXES", "op://a/")
    with secret_run("apply"):
        resolve_secret("op://a/b", unsent=True)
        resolve_secret("op://a/b", unsent=True)
        with pytest.raises(SecretPolicyError):
            resolve_secret("op://zzz/b", unsent=True)
        with pytest.raises(SecretResolveError):
            resolve_secret("env://FLUID_TEST_UNSET", unsent=True)
    assert _outcomes(events) == ["resolved", "cached", "refused", "not_found"]
    resolved, cached, refused, not_found = (attrs for _, attrs in events)
    assert resolved == {
        "fluid.secret.ref": "op://a/b",
        "fluid.secret.scheme": "op",
        "fluid.secret.outcome": "resolved",
        "fluid.secret.source": "store",
        "fluid.stage": "apply",
    }
    assert cached["fluid.secret.source"] == "cache"
    assert refused["fluid.secret.source"] == "policy"
    assert refused["fluid.secret.ref"] == "op://zzz/b"
    assert not_found["fluid.secret.source"] == "store"
    assert not_found["fluid.secret.scheme"] == "env"
    assert VALUE not in repr(events)


@pytest.mark.unit
def test_unparseable_reference_records_no_event(events):
    with secret_run("apply"):
        with pytest.raises(SecretRefError):
            resolve_secret("not a ref", unsent=True)
    assert events == []


# ── credentials and existence ────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_credential_skips_stage_gate_and_destination_policy(fake):
    assert resolve_credential("op://a/b").get_secret_value() == VALUE
    with secret_run("validate"):
        resolve_credential("op://a/c")
    assert fake.resolved == ["op://a/b", "op://a/c"]


@pytest.mark.unit
def test_resolve_credential_keeps_scheme_and_prefix_policy(fake, monkeypatch):
    monkeypatch.setenv("FLUID_SECRET_PREFIXES", "op://a/")
    with pytest.raises(SecretPolicyError):
        resolve_credential("op://zzz/b")
    monkeypatch.setenv("FLUID_SECRET_SCHEMES", "env")
    with pytest.raises(SecretPolicyError):
        resolve_credential("op://a/b")
    assert fake.resolved == []


@pytest.mark.unit
def test_secret_exists_never_resolves(fake):
    class Strict(FakeResolver):
        def resolve(self, ref, run):
            raise AssertionError("secret_exists must not read the value")

    strict = Strict()
    registry.register_resolver("op", strict)
    found = secret_exists("op://a/b")
    assert found.found is True
    assert strict.probed == ["op://a/b"]


@pytest.mark.unit
def test_secret_exists_applies_scheme_and_prefix_policy_first(fake, monkeypatch):
    monkeypatch.setenv("FLUID_SECRET_PREFIXES", "op://a/")
    with pytest.raises(SecretPolicyError):
        secret_exists("op://zzz/b")
    assert fake.probed == []
