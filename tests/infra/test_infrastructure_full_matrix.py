# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Infrastructure layer — full matrix (Slice H).

Asserts every back-end (Docker / Kubernetes / Terraform) emits deterministic
artifacts for every engine that supports managed mode. Validators run on
the emitted artifacts. NO hyperscaler SDK imports allowed.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.infra import (
    DockerComposeGenerator,
    HelmGenerator,
    TerraformGenerator,
    build_values_overlay,
    get_chart,
    get_generator,
)
from fluid_build.infra.base import ArtifactBundle
from fluid_build.infra.values import (
    build_external_secrets,
    build_network_policy,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _contract_with_managed_engine(
    engine: str, *, sovereignty: Dict[str, Any] = None, secrets=None, egress=None, profile="small"
) -> Dict[str, Any]:
    deployment = {
        "mode": "managed",
        "managed": {
            "target": "kubernetes",
            "profile": profile,
            "secrets": secrets or [],
            "network": {"egressAllowList": egress or []},
        },
    }
    chart = get_chart(engine)
    if chart:
        deployment["managed"]["chart"] = {
            "repo": chart.repo,
            "name": chart.name,
            "version": chart.version,
        }
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": f"bronze.{engine}_managed",
        "name": f"{engine} managed",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": engine,
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "connection": {"host": "x"},
                        "mode": "full_refresh",
                    },
                    "sink": {"format": "parquet"},
                    engine: {"deployment": deployment},
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": {"platform": "local", "format": "parquet"},
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }
    if sovereignty:
        contract["sovereignty"] = sovereignty
    return contract


# ── Generator registry ─────────────────────────────────────────────────


class TestGeneratorRegistry:
    def test_all_three_targets_registered(self):
        for target in ("docker", "kubernetes", "terraform"):
            gen = get_generator(target)
            assert gen is not None
            assert gen.target == target

    def test_unknown_target_returns_none(self):
        assert get_generator("aws-cdk") is None


# ── Docker generator ───────────────────────────────────────────────────


class TestDockerGenerator:
    def test_generates_compose_for_airbyte(self, tmp_path: Path):
        gen = DockerComposeGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte"))
        assert bundle.target == "docker"
        files = {f.relative_path: f.content for f in bundle.files}
        assert "docker-compose.yaml" in files
        compose = yaml.safe_load(files["docker-compose.yaml"])
        services = compose["services"]
        assert "airbyte-server" in services
        assert "airbyte-db" in services
        assert services["airbyte-server"]["image"].startswith("airbyte/server")

    def test_generates_compose_for_kafka_connect(self, tmp_path: Path):
        gen = DockerComposeGenerator()
        bundle = gen.generate(_contract_with_managed_engine("kafka-connect"))
        compose = yaml.safe_load(
            next(f.content for f in bundle.files if f.relative_path == "docker-compose.yaml")
        )
        services = compose["services"]
        for required in ("kafka", "zookeeper", "kafka-connect"):
            assert required in services

    def test_debezium_uses_kafka_connect_topology(self, tmp_path: Path):
        gen = DockerComposeGenerator()
        bundle = gen.generate(_contract_with_managed_engine("debezium"))
        compose = yaml.safe_load(
            next(f.content for f in bundle.files if f.relative_path == "docker-compose.yaml")
        )
        assert "kafka-connect" in compose["services"]

    def test_meltano_service(self):
        gen = DockerComposeGenerator()
        bundle = gen.generate(_contract_with_managed_engine("meltano"))
        compose = yaml.safe_load(
            next(f.content for f in bundle.files if f.relative_path == "docker-compose.yaml")
        )
        assert "meltano" in compose["services"]

    def test_duckdb_dlt_have_no_services(self):
        gen = DockerComposeGenerator()
        for engine in ("duckdb", "dlt"):
            bundle = gen.generate(_contract_with_managed_engine(engine))
            compose = yaml.safe_load(
                next(f.content for f in bundle.files if f.relative_path == "docker-compose.yaml")
            )
            assert compose["services"] == {}

    def test_validate_catches_missing_image(self):
        # Hand-craft a malformed bundle to exercise the validator.
        from fluid_build.infra.base import ArtifactBundle, make_file

        bad = make_file("docker-compose.yaml", "services:\n  x:\n    environment:\n      A: B\n")
        bundle = ArtifactBundle.of("docker", [bad])
        result = DockerComposeGenerator().validate(bundle)
        assert not result.ok
        assert any("missing 'image'" in e for e in result.errors)

    def test_validate_passes_for_valid_compose(self):
        gen = DockerComposeGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte"))
        result = gen.validate(bundle)
        assert result.ok, result.errors

    def test_env_template_lists_secrets(self):
        gen = DockerComposeGenerator()
        contract = _contract_with_managed_engine(
            "airbyte",
            secrets=[{"name": "AIRBYTE_API_TOKEN", "ref": "vault://airbyte/token"}],
        )
        bundle = gen.generate(contract)
        env_file = next(f.content for f in bundle.files if f.relative_path == ".env.template")
        assert "AIRBYTE_API_TOKEN" in env_file
        assert "vault://airbyte/token" in env_file


# ── Helm generator ─────────────────────────────────────────────────────


class TestHelmGenerator:
    def test_emits_helm_release_per_engine(self):
        gen = HelmGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte"))
        files = {f.relative_path: f.content for f in bundle.files}
        assert "airbyte/release.yaml" in files
        assert "airbyte/values.yaml" in files
        # release.yaml is multi-doc YAML (Namespace, HelmRepository, HelmRelease).
        docs = list(yaml.safe_load_all(files["airbyte/release.yaml"]))
        kinds = {d["kind"] for d in docs}
        assert {"Namespace", "HelmRepository", "HelmRelease"} <= kinds
        helm_release = [d for d in docs if d["kind"] == "HelmRelease"][0]
        chart_spec = helm_release["spec"]["chart"]["spec"]
        assert chart_spec["chart"] == "airbyte"
        assert "version" in chart_spec

    def test_values_overlay_includes_resource_profile(self):
        gen = HelmGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte", profile="medium"))
        values_yaml = next(
            f.content for f in bundle.files if f.relative_path == "airbyte/values.yaml"
        )
        v = yaml.safe_load(values_yaml)
        assert v["server"]["resources"]["limits"]["memory"] == "4Gi"

    def test_external_secrets_emitted(self):
        gen = HelmGenerator()
        contract = _contract_with_managed_engine(
            "airbyte",
            secrets=[
                {"name": "AIRBYTE_API_TOKEN", "ref": "vault://airbyte/token"},
                {"name": "SF_OAUTH", "ref": "aws://salesforce/oauth"},
            ],
        )
        bundle = gen.generate(contract)
        files = {f.relative_path for f in bundle.files}
        assert "airbyte/external-secrets.yaml" in files

    def test_network_policy_emitted_for_egress(self):
        gen = HelmGenerator()
        contract = _contract_with_managed_engine("airbyte", egress=["sf.com", "stripe.com"])
        bundle = gen.generate(contract)
        files = {f.relative_path for f in bundle.files}
        assert "airbyte/network-policy.yaml" in files

    def test_sovereignty_propagates_to_values(self):
        gen = HelmGenerator()
        contract = _contract_with_managed_engine(
            "airbyte",
            sovereignty={
                "jurisdiction": "EU",
                "dataResidency": {"region": "eu-west-1", "prohibitTransferTo": ["US"]},
                "egressAllowList": ["sf.com"],
            },
        )
        bundle = gen.generate(contract)
        values = yaml.safe_load(
            next(f.content for f in bundle.files if f.relative_path == "airbyte/values.yaml")
        )
        assert values["fluid_sovereignty"]["jurisdiction"] == "EU"
        assert values["fluid_sovereignty"]["region"] == "eu-west-1"
        assert "US" in values["fluid_sovereignty"]["prohibit_transfer_to"]

    def test_embedded_engines_are_skipped(self):
        gen = HelmGenerator()
        # DuckDB and dlt have no managed-mode chart; the generator skips them.
        for engine in ("duckdb", "dlt"):
            contract = _contract_with_managed_engine(engine)
            bundle = gen.generate(contract)
            assert bundle.files == []
            assert bundle.metadata["engines"] == []

    def test_validate_catches_missing_chart_fields(self):
        from fluid_build.infra.base import ArtifactBundle, make_file

        bad = make_file(
            "airbyte/release.yaml",
            yaml.safe_dump(
                {
                    "apiVersion": "helm.toolkit.fluxcd.io/v2beta2",
                    "kind": "HelmRelease",
                    "metadata": {"name": "x"},
                    "spec": {"chart": {"spec": {"chart": "x"}}},  # missing version + sourceRef
                }
            ),
        )
        bundle = ArtifactBundle.of("kubernetes", [bad])
        result = HelmGenerator().validate(bundle)
        assert not result.ok

    def test_validate_passes_for_valid_release(self):
        gen = HelmGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte"))
        result = gen.validate(bundle)
        assert result.ok, result.errors


# ── Terraform generator ────────────────────────────────────────────────


class TestTerraformGenerator:
    def test_emits_main_tf_per_engine(self):
        gen = TerraformGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte"))
        names = {f.relative_path for f in bundle.files}
        assert {"main.tf", "variables.tf", "versions.tf"} <= names
        main = next(f.content for f in bundle.files if f.relative_path == "main.tf")
        assert 'resource "helm_release" "airbyte"' in main
        assert 'resource "kubernetes_namespace" "airbyte"' in main

    def test_versions_tf_pins_providers(self):
        gen = TerraformGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte"))
        versions = next(f.content for f in bundle.files if f.relative_path == "versions.tf")
        assert "required_providers" in versions
        assert "hashicorp/helm" in versions
        assert "hashicorp/kubernetes" in versions

    def test_variables_tf_includes_default_values(self):
        gen = TerraformGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte"))
        variables = next(f.content for f in bundle.files if f.relative_path == "variables.tf")
        assert 'variable "airbyte_values"' in variables
        # default is a heredoc — must contain JSON that parses.
        eot_start = variables.index("<<EOT") + len("<<EOT\n")
        eot_end = variables.index("\nEOT", eot_start)
        json_str = variables[eot_start:eot_end]
        import json

        parsed = json.loads(json_str)
        assert "global" in parsed or "server" in parsed

    def test_safe_id_handles_kebab_case(self):
        gen = TerraformGenerator()
        bundle = gen.generate(_contract_with_managed_engine("kafka-connect"))
        main = next(f.content for f in bundle.files if f.relative_path == "main.tf")
        # Resource ids must be valid Terraform identifiers (no dashes).
        assert 'resource "helm_release" "kafka_connect"' in main

    def test_validate_passes_for_valid_module(self):
        gen = TerraformGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte"))
        result = gen.validate(bundle)
        assert result.ok, result.errors

    def test_validate_catches_missing_helm_release(self):
        from fluid_build.infra.base import ArtifactBundle, make_file

        bad = make_file("main.tf", 'resource "kubernetes_namespace" "x" {}\n')
        # Need to also include versions.tf with required_providers to isolate the helm_release error.
        versions = make_file("versions.tf", "required_providers {}")
        bundle = ArtifactBundle.of("terraform", [bad, versions])
        result = TerraformGenerator().validate(bundle)
        assert not result.ok
        assert any("helm_release" in e for e in result.errors)


# ── Values overlay ─────────────────────────────────────────────────────


class TestValuesOverlay:
    def test_user_values_override_defaults(self):
        v = build_values_overlay(
            "airbyte", profile="small", user_values={"webapp": {"replicaCount": 5}}
        )
        assert v["webapp"]["replicaCount"] == 5
        # And defaults still present.
        assert "resources" in v["webapp"]

    def test_profile_changes_resource_limits(self):
        s = build_values_overlay("airbyte", profile="small")
        m = build_values_overlay("airbyte", profile="medium")
        l = build_values_overlay("airbyte", profile="large")
        assert s["server"]["resources"]["limits"]["memory"] == "1Gi"
        assert m["server"]["resources"]["limits"]["memory"] == "4Gi"
        assert l["server"]["resources"]["limits"]["memory"] == "8Gi"

    def test_unknown_engine_returns_empty_or_user_values(self):
        v = build_values_overlay("alien", user_values={"foo": "bar"})
        assert v == {"foo": "bar"}


# ── ExternalSecret + NetworkPolicy emitters ────────────────────────────


class TestExternalSecretEmitter:
    def test_vault_backend_inferred(self):
        out = build_external_secrets(
            name_prefix="airbyte",
            secrets=[{"name": "AIRBYTE_API_TOKEN", "ref": "vault://airbyte/token"}],
        )
        assert len(out) == 1
        assert out[0]["spec"]["secretStoreRef"]["name"] == "vault-store"

    def test_aws_backend_inferred(self):
        out = build_external_secrets(
            name_prefix="airbyte",
            secrets=[{"name": "K", "ref": "aws://kms/key"}],
        )
        assert "aws-secretsmanager-store" in out[0]["spec"]["secretStoreRef"]["name"]

    def test_unknown_scheme_falls_back_to_env(self):
        out = build_external_secrets(
            name_prefix="x",
            secrets=[{"name": "K", "ref": "weird://x"}],
        )
        assert out[0]["spec"]["secretStoreRef"]["name"] == "env-store"


class TestNetworkPolicyEmitter:
    def test_emits_egress_rules(self):
        np = build_network_policy(
            namespace="forge-airbyte",
            name="forge-airbyte-egress",
            allow_list=["sf.com", "stripe.com"],
        )
        assert np["kind"] == "NetworkPolicy"
        assert np["spec"]["policyTypes"] == ["Egress"]
        assert len(np["spec"]["egress"]) == 2


# ── No hyperscaler SDK in infra/ ───────────────────────────────────────


class TestNoCloudSdkImports:
    def test_no_boto3_no_google_cloud_no_azure_imports(self):
        """Hyperscaler-agnostic invariant: no cloud SDKs leak into the infra layer.

        Checks ``import`` and ``from … import`` statements specifically; URI
        schemes like ``azure://`` in plain code are fine and do not count.
        """
        import re

        forbidden_patterns = (
            r"^\s*import\s+boto3\b",
            r"^\s*from\s+boto3(\.|\s)",
            r"^\s*import\s+google\.cloud\b",
            r"^\s*from\s+google\.cloud(\.|\s)",
            r"^\s*import\s+azure\.",
            r"^\s*from\s+azure(\.|\s)",
        )
        compiled = [re.compile(p, flags=re.MULTILINE) for p in forbidden_patterns]
        import fluid_build.infra as infra

        for modinfo in pkgutil.walk_packages(infra.__path__, prefix="fluid_build.infra."):
            module = importlib.import_module(modinfo.name)
            src = Path(module.__file__).read_text(encoding="utf-8")
            for pat in compiled:
                m = pat.search(src)
                assert m is None, (
                    f"hyperscaler SDK leaked: pattern {pat.pattern!r} matched in "
                    f"{modinfo.name} at {m.group(0)!r}"
                )


# ── ArtifactBundle digest stability ─────────────────────────────────────


class TestArtifactDigest:
    def test_same_input_same_digest(self):
        gen = HelmGenerator()
        b1 = gen.generate(_contract_with_managed_engine("airbyte"))
        b2 = gen.generate(_contract_with_managed_engine("airbyte"))
        assert b1.bundle_digest == b2.bundle_digest

    def test_different_input_different_digest(self):
        gen = HelmGenerator()
        b1 = gen.generate(_contract_with_managed_engine("airbyte", profile="small"))
        b2 = gen.generate(_contract_with_managed_engine("airbyte", profile="medium"))
        assert b1.bundle_digest != b2.bundle_digest

    def test_bundle_writes_to_disk(self, tmp_path: Path):
        gen = HelmGenerator()
        bundle = gen.generate(_contract_with_managed_engine("airbyte"))
        bundle.write_to(tmp_path / "out")
        for f in bundle.files:
            assert (tmp_path / "out" / f.relative_path).exists()


# ── Cross-back-end determinism ─────────────────────────────────────────


class TestCrossBackendDeterminism:
    @pytest.mark.parametrize("engine", ["airbyte", "kafka-connect", "debezium", "meltano"])
    def test_all_three_backends_produce_artifacts(self, engine: str):
        contract = _contract_with_managed_engine(engine)
        for target in ("docker", "kubernetes", "terraform"):
            gen = get_generator(target)
            assert gen is not None
            bundle = gen.generate(contract)
            assert isinstance(bundle, ArtifactBundle)
            # Every back-end produces artifacts when the engine has a managed-mode chart.
            if target == "docker":
                # Docker always emits compose + env template even if services dict is empty.
                assert any(f.relative_path == "docker-compose.yaml" for f in bundle.files)
            elif target in ("kubernetes", "terraform"):
                assert len(bundle.files) > 0
