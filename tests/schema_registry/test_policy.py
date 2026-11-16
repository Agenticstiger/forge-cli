# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Founder decision #818: severities of the registry egress rules."""

from __future__ import annotations

import copy

import pytest

from fluid_build.schema_registry.policy import subject_findings, validation_findings
from fluid_build.schema_registry.reference import consumed_subjects, produced_subjects

from .conftest import consumed_contract, produced_contract

pytestmark = pytest.mark.unit

HOSTS = "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST"
GRANTS = "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"
HOST = "registry.example.com"
URL = f"https://{HOST}"
AUTH = {"secretRef": "env://SCHEMA_REGISTRY_AUTH"}  # pragma: allowlist secret - a reference


def consumed(url=URL, auth=None, **kw):
    spec = {"kind": "confluent", "url": url}
    if auth:
        spec["auth"] = auth
    (subject,) = consumed_subjects(consumed_contract(spec, **kw))
    return subject


def produced(url=URL, **extra):
    spec = {"kind": "confluent", "url": url, **extra}
    (subject,) = produced_subjects(produced_contract(spec))
    return subject


def severities(findings):
    return sorted({s for s, _ in findings})


def messages(findings):
    return " | ".join(m for _, m in findings)


# --------------------------------------------------------------- consumed, no auth


class TestConsumedWithoutAuth:
    def test_unresolvable_host_has_no_findings(self):
        assert subject_findings(consumed(), stage="plan", environ={}) == []
        assert subject_findings(consumed(), stage="validate", environ={}) == []

    def test_public_unlisted_host_is_an_error_at_plan(self, public_dns):
        findings = subject_findings(consumed(), stage="plan", environ={})
        assert severities(findings) == ["error"]
        assert HOSTS in messages(findings) and HOST in messages(findings)

    def test_public_unlisted_host_is_not_judged_at_validate(self, public_dns):
        assert subject_findings(consumed(), stage="validate", environ={}) == []

    def test_listed_host_passes_at_plan(self, public_dns):
        assert subject_findings(consumed(), stage="plan", environ={HOSTS: HOST}) == []

    def test_parent_domain_entry_covers_a_subdomain(self, public_dns):
        assert subject_findings(consumed(), stage="plan", environ={HOSTS: "example.com"}) == []

    def test_http_without_credential_is_allowed(self):
        assert subject_findings(consumed("http://sr.internal:8081"), stage="plan", environ={}) == []

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_link_local_is_an_error_at_every_stage(self, stage):
        findings = subject_findings(consumed("http://169.254.169.254"), stage=stage, environ={})
        assert severities(findings) == ["error"] and "link-local" in messages(findings)

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_link_local_is_refused_even_when_listed(self, stage):
        env = {HOSTS: "169.254.169.254"}
        findings = subject_findings(consumed("http://169.254.169.254"), stage=stage, environ=env)
        assert severities(findings) == ["error"]

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_loopback_is_exempt(self, stage):
        assert subject_findings(consumed("http://localhost:8081"), stage=stage, environ={}) == []
        assert subject_findings(consumed("http://127.0.0.1:8081"), stage=stage, environ={}) == []


# ------------------------------------------------------------------ consumed, auth


class TestConsumedWithAuth:
    @pytest.mark.parametrize("stage, severity", [("validate", "warning"), ("plan", "error")])
    def test_an_unlisted_credential_host_is_a_missing_entry(self, stage, severity):
        env = {GRANTS: f"SCHEMA_REGISTRY_AUTH@{HOST}"}
        findings = subject_findings(consumed(auth=AUTH), stage=stage, environ=env)
        assert severities(findings) == [severity] and HOSTS in messages(findings)

    def test_no_grant_warns_at_validate(self):
        findings = subject_findings(consumed(auth=AUTH), stage="validate", environ={})
        assert severities(findings) == ["warning"]
        assert (
            "SCHEMA_REGISTRY_AUTH" in messages(findings)
            or "allowlist" in messages(findings).lower()
        )

    def test_no_grant_is_an_error_at_plan(self):
        findings = subject_findings(consumed(auth=AUTH), stage="plan", environ={})
        assert severities(findings) == ["error"]

    @pytest.mark.parametrize(
        "entry",
        [f"SCHEMA_REGISTRY_AUTH@{HOST}", f"env://SCHEMA_REGISTRY_AUTH@{HOST}"],
    )
    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_grant_clears_the_finding(self, entry, stage):
        findings = subject_findings(
            consumed(auth=AUTH), stage=stage, environ={GRANTS: entry, HOSTS: HOST}
        )
        assert findings == []

    def test_grant_for_another_host_does_not_cover(self):
        env = {GRANTS: "SCHEMA_REGISTRY_AUTH@other.example.com"}
        assert severities(subject_findings(consumed(auth=AUTH), stage="plan", environ=env)) == [
            "error"
        ]

    def test_grant_for_another_variable_does_not_cover(self):
        env = {GRANTS: f"SOMETHING_ELSE@{HOST}"}
        assert severities(subject_findings(consumed(auth=AUTH), stage="plan", environ=env)) == [
            "error"
        ]

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_http_credential_destination_is_an_error_at_every_stage(self, stage):
        subject = consumed("http://sr.example.com", auth=AUTH)
        env = {GRANTS: "SCHEMA_REGISTRY_AUTH@sr.example.com", HOSTS: "sr.example.com"}
        findings = subject_findings(subject, stage=stage, environ=env)
        assert severities(findings) == ["error"] and "https" in messages(findings)

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_loopback_http_with_auth_is_exempt_from_https(self, stage):
        subject = consumed("http://localhost:8081", auth=AUTH)
        findings = subject_findings(subject, stage=stage, environ={})
        assert "https" not in messages(findings)
        assert findings == []

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_link_local_with_auth_is_an_error(self, stage):
        subject = consumed("http://169.254.169.254", auth=AUTH)
        assert severities(subject_findings(subject, stage=stage, environ={})) == ["error"]

    def test_scheme_not_enabled_follows_the_missing_entry_rule(self):
        subject = consumed(auth={"secretRef": "vault://kv/data/sr#key"})
        env = {GRANTS: f"vault://kv/data/sr#key@{HOST}"}
        validate = subject_findings(subject, stage="validate", environ=env)
        plan = subject_findings(subject, stage="plan", environ=env)
        assert severities(validate) in (["warning"], [])
        assert severities(plan) in (["error"], [])
        assert bool(validate) == bool(plan)

    def test_unparseable_secret_ref_is_an_error(self):
        findings = subject_findings(
            consumed(auth={"secretRef": "not a ref"}), stage="validate", environ={}
        )
        assert severities(findings) == ["error"] and "not a secret reference" in messages(findings)

    def test_messages_never_carry_a_value(self):
        findings = subject_findings(consumed(auth=AUTH), stage="plan", environ={})
        assert "hunter2" not in messages(findings)


# ---------------------------------------------------------------------- produced


class TestProducedConfluent:
    def test_missing_grant_and_host_warn_at_validate(self):
        findings = subject_findings(produced(), stage="validate", environ={})
        assert severities(findings) == ["warning"]
        text = messages(findings)
        assert HOSTS in text and "SCHEMA_REGISTRY_API_SECRET" in text
        assert len(findings) == 2

    def test_missing_grant_and_host_error_at_plan(self):
        findings = subject_findings(produced(), stage="plan", environ={})
        assert severities(findings) == ["error"] and len(findings) == 2

    def test_host_listed_only_leaves_the_grant_finding(self):
        findings = subject_findings(produced(), stage="plan", environ={HOSTS: HOST})
        assert len(findings) == 1 and "SCHEMA_REGISTRY_API_SECRET" in messages(findings)

    def test_grant_only_leaves_the_host_finding(self):
        env = {GRANTS: f"SCHEMA_REGISTRY_API_SECRET@{HOST}"}
        findings = subject_findings(produced(), stage="plan", environ=env)
        assert len(findings) == 1 and HOSTS in messages(findings)

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_host_and_grant_clear_everything(self, stage):
        env = {HOSTS: HOST, GRANTS: f"SCHEMA_REGISTRY_API_SECRET@{HOST}"}
        assert subject_findings(produced(), stage=stage, environ=env) == []

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_http_is_an_error_at_every_stage(self, stage):
        env = {HOSTS: HOST, GRANTS: f"SCHEMA_REGISTRY_API_SECRET@{HOST}"}
        findings = subject_findings(produced(f"http://{HOST}"), stage=stage, environ=env)
        assert severities(findings) == ["error"] and "https" in messages(findings)

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_link_local_is_an_error(self, stage):
        findings = subject_findings(produced("http://169.254.169.254"), stage=stage, environ={})
        assert severities(findings) == ["error"] and "link-local" in messages(findings)

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    @pytest.mark.parametrize("url", ["http://localhost:8081", "http://127.0.0.1:8081"])
    def test_loopback_is_exempt(self, stage, url):
        assert subject_findings(produced(url), stage=stage, environ={}) == []

    def test_contract_auth_on_a_produced_subject_is_an_error(self):
        subject = produced(auth=AUTH)
        env = {HOSTS: HOST, GRANTS: f"SCHEMA_REGISTRY_API_SECRET@{HOST}"}
        findings = subject_findings(subject, stage="validate", environ=env)
        assert severities(findings) == ["error"] and "remove auth" in messages(findings)


class TestGlue:
    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_glue_has_no_findings(self, stage):
        (subject,) = produced_subjects(produced_contract({"kind": "glue", "registryName": "c"}))
        assert subject_findings(subject, stage=stage, environ={}) == []

    @pytest.mark.parametrize("stage", ["validate", "plan"])
    def test_consumed_glue_has_no_findings(self, stage):
        (subject,) = consumed_subjects(consumed_contract({"kind": "glue"}))
        assert subject_findings(subject, stage=stage, environ={}) == []


# ------------------------------------------------------------- validation_findings


class TestValidationFindings:
    def test_clean_glue_contract(self):
        contract = produced_contract({"kind": "glue", "registryName": "c"})
        assert validation_findings(contract, environ={}) == ([], [])

    def test_contract_without_registries(self):
        assert validation_findings({"id": "x"}, environ={}) == ([], [])

    def test_produced_subject_without_columns_is_an_error(self):
        contract = produced_contract({"kind": "glue"})
        contract["exposes"][0]["contract"]["schema"] = []
        errors, warnings = validation_findings(contract, environ={})
        assert any("declares no columns" in e for e in errors) and warnings == []

    def test_produced_subject_without_a_contract_block_is_an_error(self):
        contract = produced_contract({"kind": "glue"})
        del contract["exposes"][0]["contract"]
        errors, _ = validation_findings(contract, environ={})
        assert any("declares no columns" in e for e in errors)

    def test_registry_on_a_non_kafka_expose_is_a_warning(self):
        contract = produced_contract({"kind": "glue"}, platform="aws", fmt="iceberg")
        errors, warnings = validation_findings(contract, environ={})
        assert errors == []
        assert any("is ignored" in w and "Kafka topic expose" in w for w in warnings)

    def test_malformed_registry_is_an_error(self):
        contract = produced_contract({"kind": "confluent", "url": "nope"})
        errors, _ = validation_findings(contract, environ={})
        assert errors and "exposes[0].binding.properties.registry.url" in errors[0]

    def test_malformed_consumed_registry_is_an_error(self):
        contract = consumed_contract({"kind": "glue", "url": "https://x.example"})
        errors, _ = validation_findings(contract, environ={})
        assert errors

    def test_missing_operator_entry_is_a_warning_not_an_error(self):
        contract = produced_contract({"kind": "confluent", "url": URL})
        errors, warnings = validation_findings(contract, environ={})
        assert errors == [] and len(warnings) == 2

    def test_link_local_is_an_error(self):
        contract = produced_contract({"kind": "confluent", "url": "http://169.254.169.254"})
        errors, _ = validation_findings(contract, environ={})
        assert any("link-local" in e for e in errors)

    def test_consumed_without_grant_warns(self):
        contract = consumed_contract({"kind": "confluent", "url": URL, "auth": AUTH})
        errors, warnings = validation_findings(contract, environ={HOSTS: HOST})
        assert errors == [] and len(warnings) == 1

    def test_consumed_with_grant_is_clean(self):
        contract = consumed_contract({"kind": "confluent", "url": URL, "auth": AUTH})
        env = {GRANTS: f"SCHEMA_REGISTRY_AUTH@{HOST}", HOSTS: HOST}
        assert validation_findings(contract, environ=env) == ([], [])

    def test_does_not_mutate_the_contract(self):
        contract = produced_contract({"kind": "confluent", "url": URL})
        before = copy.deepcopy(contract)
        validation_findings(contract, environ={})
        assert contract == before

    def test_reads_the_process_environment_when_none_is_given(self, monkeypatch):
        contract = produced_contract({"kind": "confluent", "url": URL})
        monkeypatch.setenv(HOSTS, HOST)
        monkeypatch.setenv(GRANTS, f"SCHEMA_REGISTRY_API_SECRET@{HOST}")
        assert validation_findings(contract) == ([], [])


class TestProducedFindings:
    def test_a_column_no_registry_format_carries_fails_at_validate(self):
        contract = produced_contract({"kind": "glue"})
        contract["exposes"][0]["contract"]["schema"].append({"name": "v", "type": "varchar(255)"})
        errors, _ = validation_findings(contract, environ={})
        assert any("varchar(255)" in e for e in errors)

    @pytest.mark.parametrize("kind, cloud", [("glue", "aws"), ("confluent", "confluent")])
    def test_no_bound_cloud_warns_that_nothing_registers_it(self, kind, cloud):
        registry = (
            {"kind": kind} if kind == "glue" else {"kind": kind, "url": "http://localhost:8081"}
        )
        contract = produced_contract(registry, bound=False)
        _, warnings = validation_findings(contract, environ={})
        assert any(f"no expose binds {cloud}" in w for w in warnings)
