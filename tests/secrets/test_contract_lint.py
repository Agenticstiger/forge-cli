# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Linting a contract's secretRef sites: grammar, policy and destination, never a fetch."""

from __future__ import annotations

import pytest

from fluid_build.secrets import resolve
from fluid_build.secrets.contract import lint_secret_refs, secret_ref_sites
from fluid_build.secrets.errors import SecretRefError
from fluid_build.secrets.policy import ALLOWLIST_ENV, AcquisitionSecretPolicyError

REMOTE = "db.example.com"


@pytest.fixture(autouse=True)
def _no_resolver_may_run(monkeypatch):
    """Linting may look up which resolver serves a scheme, never ask it anything."""
    from fluid_build.secrets._aws_sm import AwsSecretsManagerResolver
    from fluid_build.secrets._env import EnvResolver
    from fluid_build.secrets._file import FileResolver
    from fluid_build.secrets._vault import VaultResolver

    def boom(*args, **kwargs):
        raise AssertionError("linting must never ask a store")

    for cls in (EnvResolver, FileResolver, VaultResolver, AwsSecretsManagerResolver):
        monkeypatch.setattr(cls, "resolve", boom)
        monkeypatch.setattr(cls, "exists", boom)
    monkeypatch.setattr(resolve, "_fetch", boom)


def _contract(props):
    return {"builds": [{"properties": props}]}


def _conn(**connection):
    return _contract({"source": {"connection": connection}})


@pytest.mark.unit
def test_a1_aws_alias_to_a_remote_host_needs_an_allowlist_entry(monkeypatch):
    contract = _conn(secretRef="aws://x", host=REMOTE)
    (finding,) = lint_secret_refs(contract, {})
    assert not finding.ok
    assert isinstance(finding.error, AcquisitionSecretPolicyError)
    assert "aws-sm://x@db.example.com" in finding.error.fix
    environ = {ALLOWLIST_ENV: f"aws-sm://x@{REMOTE}"}
    (finding,) = lint_secret_refs(contract, environ)
    assert finding.ok
    assert finding.ref.canonical == "aws-sm://x"


@pytest.mark.unit
def test_a1_reads_the_process_environment_when_none_is_given(monkeypatch):
    contract = _conn(secretRef="aws://x", host=REMOTE)
    assert not lint_secret_refs(contract)[0].ok
    monkeypatch.setenv(ALLOWLIST_ENV, f"aws-sm://x@{REMOTE}")
    assert lint_secret_refs(contract)[0].ok


@pytest.mark.unit
def test_source_connection_checks_every_connection_host():
    contract = _conn(secretRef="env://PG", host="localhost", uri=f"postgres://{REMOTE}/db")
    (finding,) = lint_secret_refs(contract, {})
    assert isinstance(finding.error, AcquisitionSecretPolicyError)
    assert finding.site.pointer == "builds[0].properties.source.connection.secretRef"
    assert lint_secret_refs(contract, {ALLOWLIST_ENV: f"PG@{REMOTE}"})[0].ok


@pytest.mark.unit
def test_loopback_connection_needs_no_entry():
    conn = _conn(secretRef="env://PG", host="127.0.0.1")  # pragma: allowlist secret
    assert lint_secret_refs(conn, {})[0].ok


@pytest.mark.unit
def test_inline_password_connection_is_not_destination_policed():
    contract = _conn(secretRef="env://PG", host=REMOTE, password="inline")
    (finding,) = lint_secret_refs(contract, {})
    assert finding.ok
    assert finding.site.sent is False


def _deployment(engine, **auth):
    return _contract(
        {engine: {"deployment": {"server_url": f"https://{REMOTE}:8000/api", "auth": auth}}}
    )


@pytest.mark.unit
def test_airbyte_deployment_auth_checks_the_server_url_host():
    contract = _deployment("airbyte", secretRef="env://DEPLOY_TOKEN")  # pragma: allowlist secret
    (finding,) = lint_secret_refs(contract, {})
    assert finding.site.pointer == "builds[0].properties.airbyte.deployment.auth.secretRef"
    assert isinstance(finding.error, AcquisitionSecretPolicyError)
    assert REMOTE in finding.error.what
    assert lint_secret_refs(contract, {ALLOWLIST_ENV: f"DEPLOY_TOKEN@{REMOTE}"})[0].ok


@pytest.mark.unit
def test_an_inline_airbyte_token_wins_so_the_reference_is_not_sent():
    contract = _deployment(
        "airbyte", token="inline", secretRef="env://DEPLOY_TOKEN"  # pragma: allowlist secret
    )
    (finding,) = lint_secret_refs(contract, {})
    assert finding.ok and finding.site.sent is False


@pytest.mark.unit
@pytest.mark.parametrize("engine", ["meltano", "kafka-connect", "debezium"])
def test_deployment_auth_no_runner_reads_is_linted_but_not_sent(engine):
    """No runner but airbyte's resolves deployment.auth.secretRef, so no host is judged."""
    contract = _deployment(engine, secretRef="env://DEPLOY_TOKEN")  # pragma: allowlist secret
    (finding,) = lint_secret_refs(contract, {})
    assert finding.site.pointer == f"builds[0].properties.{engine}.deployment.auth.secretRef"
    assert finding.ok and finding.site.sent is False
    bad = _deployment(engine, secretRef="vault://kv/no-field")  # pragma: allowlist secret
    assert isinstance(lint_secret_refs(bad, {})[0].error, SecretRefError)


@pytest.mark.unit
def test_deployment_auth_to_a_loopback_server_needs_no_entry():
    contract = _contract(
        {
            "airbyte": {
                "deployment": {
                    "server_url": "http://localhost:8000",
                    "auth": {"secretRef": "env://DEPLOY_TOKEN"},  # pragma: allowlist secret
                }
            }
        }
    )
    assert lint_secret_refs(contract, {})[0].ok


@pytest.mark.unit
def test_schema_registry_auth_is_linted_but_not_sent():
    """The schema allows it; no runner reads it today, so no host is judged."""
    contract = _contract(
        {
            "kafka-connect": {
                "schema_registry": {
                    "url": f"https://{REMOTE}:8081",
                    "auth": {"secretRef": "vault://kv/acme/sr#token"},  # pragma: allowlist secret
                }
            }
        }
    )
    (finding,) = lint_secret_refs(contract, {})
    assert finding.site.pointer.endswith("kafka-connect.schema_registry.auth.secretRef")
    assert finding.ok and finding.site.sent is False


@pytest.mark.unit
def test_other_secret_refs_are_linted_for_grammar_scheme_and_prefix_only():
    contract = {
        "metadata": {"a": {"secretRef": "env://FINE"}},  # pragma: allowlist secret
        "other": [
            {"secretRef": "not a reference"},  # pragma: allowlist secret
            {"secretRef": "gcp-sm://projects/p/secrets/s"},  # pragma: allowlist secret
        ],
        "files": {"secretRef": "file:///etc/passwd"},  # pragma: allowlist secret
    }
    findings = {f.site.pointer: f for f in lint_secret_refs(contract, {})}
    assert findings["metadata.a.secretRef"].ok
    assert findings["metadata.a.secretRef"].site.sent is False
    assert isinstance(findings["other[0].secretRef"].error, SecretRefError)
    assert "not enabled" in findings["other[1].secretRef"].error.what
    assert "no resolver for gcp-sm://" in findings["other[1].secretRef"].warning.what
    assert "outside the prefixes" in findings["files.secretRef"].error.what


@pytest.mark.unit
def test_secret_refs_lists_are_ignored():
    contract = _contract({"streaming": {"secretRefs": ["env://A", "not a ref"]}})
    assert lint_secret_refs(contract, {}) == []
    assert secret_ref_sites(contract) == []


@pytest.mark.unit
def test_known_sites_come_first_and_are_not_reported_twice():
    connection = {"secretRef": "env://PG", "host": "localhost"}  # pragma: allowlist secret
    contract = _contract(
        {
            "source": {"connection": connection},
            "x": {"secretRef": "env://OTHER"},  # pragma: allowlist secret
        }
    )
    pointers = [site.pointer for site in secret_ref_sites(contract)]
    assert pointers == [
        "builds[0].properties.source.connection.secretRef",
        "builds[0].properties.x.secretRef",
    ]


@pytest.mark.unit
def test_non_string_secret_ref_is_a_ref_error_and_contract_without_refs_is_clean():
    (finding,) = lint_secret_refs(_conn(secretRef=5, host="localhost"), {})
    assert isinstance(finding.error, SecretRefError)
    assert lint_secret_refs({"builds": "nope"}, {}) == []
    assert lint_secret_refs({}, {}) == []


@pytest.mark.unit
def test_validate_fails_closed_when_the_lint_itself_crashes(monkeypatch):
    from fluid_build.cli import validate as validate_cmd
    from fluid_build.schema_manager import ValidationResult
    from fluid_build.secrets import contract as contract_lint

    def crash(_contract):
        raise RuntimeError("boom")

    monkeypatch.setattr(contract_lint, "lint_secret_refs", crash)
    result = ValidationResult(is_valid=True, schema_version=None)
    validate_cmd._check_secret_refs(result, {})
    assert result.is_valid is False
    assert any("NOT checked" in error for error in result.errors)
