# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The egress floor: forge's own rules for every registry kind, whatever the kind adds.

A kind whose ``egress_findings`` returns ``[]`` still gets the url, link-local and
credential rules (review D1 on #926), and ``fluid apply`` refuses it before any secret
is resolved or any client is built. The host a credential goes to is the one the kind
names (D2).
"""

from __future__ import annotations

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.schema_registry import kinds, policy
from fluid_build.schema_registry.kinds import RegistryKind, register_kind
from fluid_build.schema_registry.policy import subject_findings, validation_findings
from fluid_build.schema_registry.reference import checked_subjects
from fluid_build.schema_registry.verify import verify_at_apply, verify_consumed

from .conftest import consumed_contract
from .test_verify import COLUMNS

pytestmark = pytest.mark.unit

REF = "env://SR_AUTH"  # pragma: allowlist secret - a reference, not a value
GRANTS = "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"
HOSTS = "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST"


class Leaky(RegistryKind):
    """An HTTP kind that takes a credential and states no egress rules of its own."""

    name = "leaky"
    title = "Leaky"
    modes = ("BACKWARD",)
    default_compatibility = "BACKWARD"
    registering_cloud = "nowhere"
    fields = frozenset({"url", "auth"})

    def egress_findings(self, subject, *, stage, environ=None):
        return []

    def location(self, ref):
        return ref.url


class SeparateAuth(Leaky):
    """A kind that sends its credential to an authentication endpoint of its own."""

    name = "separate-auth"

    def credential_url(self, ref):
        return "https://auth.example.com/token"


@pytest.fixture
def leaky():
    yield register_kind(Leaky())
    kinds._base._KINDS.pop("leaky", None)


@pytest.fixture
def separate_auth():
    yield register_kind(SeparateAuth())
    kinds._base._KINDS.pop("separate-auth", None)


@pytest.fixture
def no_secret(monkeypatch):
    """Fail the test if any secret is resolved."""

    def resolve_secret(*args, **kwargs):
        pytest.fail("a secret was resolved for a refused subject")

    monkeypatch.setattr("fluid_build.secrets.resolve.resolve_secret", resolve_secret)


def _contract(kind, url, **extra):
    spec = {"kind": kind, "url": url, "auth": {"secretRef": REF}, **extra}
    return consumed_contract(spec, columns=COLUMNS)


def _never(ref, *args, **kwargs):
    pytest.fail("a registry client was built for a refused subject")


def test_metadata_host_with_a_credential_is_refused_at_validate(leaky):
    errors, _ = validation_findings(_contract("leaky", "http://169.254.169.254/"), environ={})
    assert any("169.254.169.254" in e and "link-local" in e for e in errors), errors


def test_a_credential_over_http_is_refused_at_validate(leaky):
    errors, _ = validation_findings(_contract("leaky", "http://sr.example.com"), environ={})
    assert any("a registry credential is sent over https only" in e for e in errors), errors


def test_missing_operator_entries_warn_at_validate_and_refuse_at_plan(leaky):
    contract = _contract("leaky", "https://sr.example.com")
    errors, warnings = validation_findings(contract, environ={})
    assert errors == []
    assert any(HOSTS in w for w in warnings) and any("secretRef" in w for w in warnings)
    _, plan_errors, _ = verify_consumed(contract, environ={}, leaky=_never)
    assert any(HOSTS in e for e in plan_errors)


def test_apply_refuses_before_any_secret_or_client(leaky, no_secret):
    contract = _contract("leaky", "http://169.254.169.254/")
    with pytest.raises(CLIError) as caught:
        verify_at_apply(contract, environ={}, leaky=_never)
    assert caught.value.event == "apply_schema_registry_failed"
    assert any("link-local" in e for e in caught.value.context["errors"])


def test_a_granted_https_credential_passes_the_floor(leaky):
    env = {HOSTS: "sr.example.com", GRANTS: f"{REF}@sr.example.com"}
    (subject,) = checked_subjects(_contract("leaky", "https://sr.example.com"))
    assert subject_findings(subject, stage="apply", environ=env) == []


def test_the_grant_is_judged_against_the_kinds_credential_host(separate_auth, monkeypatch):
    contract = _contract("separate-auth", "https://sr.example.com")
    (subject,) = checked_subjects(contract)
    # Granted for the registry host only: the credential goes elsewhere, so it is refused.
    registry_only = {HOSTS: "sr.example.com,auth.example.com", GRANTS: f"{REF}@sr.example.com"}
    assert any(
        "auth.example.com" in m
        for _, m in subject_findings(subject, stage="apply", environ=registry_only)
    )
    # Granted for the credential host: it passes, and apply resolves for that host.
    granted = {HOSTS: "sr.example.com,auth.example.com", GRANTS: f"{REF}@auth.example.com"}
    assert subject_findings(subject, stage="apply", environ=granted) == []
    hosts = []

    class Stop(Exception):
        pass

    def resolve_secret(ref, *, host=None, environ=None, **kw):
        hosts.append(host)
        raise Stop

    monkeypatch.setattr("fluid_build.secrets.resolve.resolve_secret", resolve_secret)
    with pytest.raises(Stop):
        verify_at_apply(contract, environ=granted, **{"separate-auth": _never})
    assert hosts == ["auth.example.com"]


def test_a_kind_naming_no_credential_url_is_refused(leaky, monkeypatch):
    monkeypatch.setattr(Leaky, "credential_url", lambda self, ref: None)
    (subject,) = checked_subjects(_contract("leaky", "https://sr.example.com"))
    (finding,) = subject_findings(subject, stage="validate", environ={})
    assert finding[0] == "error" and "names no URL to send the credential to" in finding[1]


def test_the_floor_runs_even_when_the_kind_adds_nothing(leaky):
    # The mutation the floor guards against: with floor_findings removed, a leaky kind
    # gets no finding at all for a metadata host.
    (subject,) = checked_subjects(_contract("leaky", "http://169.254.169.254/"))
    assert Leaky().egress_findings(subject, stage="plan") == []
    assert policy.floor_findings(subject, stage="plan", environ={})
    assert subject_findings(subject, stage="plan", environ={})
