# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the ``fluid generate iac`` subcommand."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from fluid_build.cli import generate_iac
from fluid_build.cli._common import CLIError

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


class TestResolveProvider:
    def test_explicit_provider_wins(self):
        assert generate_iac._resolve_provider({}, "snowflake") == "snowflake"

    def test_auto_detects_single_platform(self):
        contract = {"exposes": [{"binding": {"platform": "gcp"}}]}
        assert generate_iac._resolve_provider(contract, "auto") == "gcp"

    def test_auto_errors_when_no_supported_cloud(self):
        with pytest.raises(CLIError):
            generate_iac._resolve_provider({"exposes": []}, "auto")

    def test_auto_errors_on_multiple_clouds(self):
        contract = {
            "exposes": [
                {"binding": {"platform": "gcp"}},
                {"binding": {"platform": "aws"}},
            ]
        }
        with pytest.raises(CLIError):
            generate_iac._resolve_provider(contract, "auto")


class TestGenerateIacRun:
    def test_writes_tofu_json_for_gcp_contract(self, tmp_path):
        contract = (
            _EXAMPLES / "bitcoin-price-api-imperative-part-a" / "contract-bigquery.fluid.yaml"
        )
        args = argparse.Namespace(
            contract=str(contract), provider="auto", out=str(tmp_path), env=None
        )
        rc = generate_iac.run(args, logging.getLogger("test"))
        assert rc == 0
        out = tmp_path / "main.tf.json"
        assert out.exists()
        doc = json.loads(out.read_text())
        assert "google_bigquery_table" in doc["resource"]
        assert doc["terraform"]["required_providers"]["google"]["source"] == "hashicorp/google"

    def test_missing_contract_returns_error(self):
        args = argparse.Namespace(contract=None, provider="auto", out="x", env=None)
        assert generate_iac.run(args, logging.getLogger("test")) == 1


class TestGenerateIacValidate:
    """`fluid generate iac --validate` — opt-in `tofu validate` on the emit."""

    _CONTRACT = _EXAMPLES / "bitcoin-price-api-imperative-part-a" / "contract-bigquery.fluid.yaml"

    def _args(self, tmp_path) -> argparse.Namespace:
        return argparse.Namespace(
            contract=str(self._CONTRACT),
            provider="auto",
            out=str(tmp_path),
            env=None,
            validate=True,
        )

    def test_validate_without_tofu_raises_clear_error(self, tmp_path, monkeypatch):
        from fluid_build.iac import runner

        monkeypatch.setattr(runner, "tofu_path", lambda: None)
        with pytest.raises(CLIError) as exc:
            generate_iac.run(self._args(tmp_path), logging.getLogger("test"))
        assert exc.value.event == "generate_iac_no_tofu"
        # The module is emitted before validation runs.
        assert (tmp_path / "main.tf.json").exists()

    def test_validate_passes_when_tofu_validate_ok(self, tmp_path, monkeypatch):
        from fluid_build.iac import runner
        from fluid_build.iac.runner import TofuResult

        ok = TofuResult(command="x", returncode=0, stdout="", stderr="")
        monkeypatch.setattr(runner, "tofu_path", lambda: "/usr/bin/tofu")
        monkeypatch.setattr(runner, "tofu_init", lambda *a, **k: ok)
        monkeypatch.setattr(runner, "tofu_validate", lambda *a, **k: ok)
        assert generate_iac.run(self._args(tmp_path), logging.getLogger("test")) == 0

    def test_validate_surfaces_tofu_validate_failure(self, tmp_path, monkeypatch):
        from fluid_build.iac import runner
        from fluid_build.iac.runner import TofuResult

        ok = TofuResult(command="init", returncode=0, stdout="", stderr="")
        bad = TofuResult(command="validate", returncode=1, stdout="", stderr="invalid resource")
        monkeypatch.setattr(runner, "tofu_path", lambda: "/usr/bin/tofu")
        monkeypatch.setattr(runner, "tofu_init", lambda *a, **k: ok)
        monkeypatch.setattr(runner, "tofu_validate", lambda *a, **k: bad)
        with pytest.raises(CLIError) as exc:
            generate_iac.run(self._args(tmp_path), logging.getLogger("test"))
        assert exc.value.event == "generate_iac_validate_failed"
        assert "invalid resource" in exc.value.context["error"]

    def test_no_validate_flag_never_touches_tofu(self, tmp_path, monkeypatch):
        from fluid_build.iac import runner

        def _boom():
            raise AssertionError("tofu must not be invoked without --validate")

        monkeypatch.setattr(runner, "tofu_path", _boom)
        args = self._args(tmp_path)
        args.validate = False
        assert generate_iac.run(args, logging.getLogger("test")) == 0


class TestEnvTemplateResolution:
    """`{{ env.* }}` placeholders are resolved before the contract reaches
    the OpenTofu emitter — otherwise a literal template lands in the .tf.json."""

    def test_resolver_walks_nested_contract(self, monkeypatch):
        from fluid_build.cli._common import resolve_env_templates_in_contract

        monkeypatch.setenv("FLUID_TEST_DB", "ANALYTICS_PROD")
        contract = {"exposes": [{"binding": {"location": {"database": "{{ env.FLUID_TEST_DB }}"}}}]}
        out = resolve_env_templates_in_contract(contract)
        assert out["exposes"][0]["binding"]["location"]["database"] == "ANALYTICS_PROD"

    def test_unresolved_template_is_left_intact(self, monkeypatch):
        from fluid_build.cli._common import resolve_env_templates_in_contract

        monkeypatch.delenv("FLUID_NO_SUCH_VAR", raising=False)
        out = resolve_env_templates_in_contract({"x": "{{ env.FLUID_NO_SUCH_VAR }}"})
        assert out["x"] == "{{ env.FLUID_NO_SUCH_VAR }}"

    def test_generate_iac_resolves_env_templates_in_emitted_module(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLUID_TEST_DATASET", "analytics_prod")
        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "p",
            "name": "P",
            "metadata": {"layer": "Bronze", "owner": {"team": "t", "email": "t@x.co"}},
            "exposes": [
                {
                    "exposeId": "events",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "{{ env.FLUID_TEST_DATASET }}", "table": "events"},
                    },
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
        }
        cpath = tmp_path / "contract.fluid.yaml"
        cpath.write_text(json.dumps(contract))  # JSON is valid YAML
        args = argparse.Namespace(contract=str(cpath), provider="auto", out=str(tmp_path), env=None)
        assert generate_iac.run(args, logging.getLogger("test")) == 0
        doc = json.loads((tmp_path / "main.tf.json").read_text())
        dataset = doc["resource"]["google_bigquery_dataset"]
        body = next(iter(dataset.values()))
        assert body["dataset_id"] == "analytics_prod"
        # The template must not survive into the resource key either.
        assert all("{{" not in key for key in dataset)
