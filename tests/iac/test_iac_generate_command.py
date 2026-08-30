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

    def test_explicit_provider_contradicting_the_binding_is_rejected(self):
        # An explicit --provider used to override whatever the contract
        # declared, which emitted a module against the wrong plugin: either
        # resource-free, or (when a binding is shape-compatible across
        # clouds) describing the wrong cloud entirely. `--provider`
        # disambiguates; it does not retarget.
        contract = {"binding": {"provider": "aws"}}
        with pytest.raises(CLIError) as exc:
            generate_iac._resolve_provider(contract, "snowflake")
        assert exc.value.event == "generate_iac_provider_mismatch"
        assert "aws" in exc.value.context["error"]
        assert "snowflake" in exc.value.context["error"]

    def test_explicit_provider_allowed_when_it_matches_the_binding(self):
        contract = {"binding": {"provider": "aws"}}
        assert generate_iac._resolve_provider(contract, "aws") == "aws"

    def test_explicit_provider_picks_one_of_an_ambiguous_contract(self):
        # The documented use of --provider: `generate_iac_ambiguous_provider`
        # tells the operator to pass it when a contract spans clouds. Both
        # declared clouds must remain selectable.
        contract = {
            "exposes": [
                {"binding": {"platform": "gcp"}},
                {"binding": {"platform": "aws"}},
            ]
        }
        assert generate_iac._resolve_provider(contract, "aws") == "aws"
        assert generate_iac._resolve_provider(contract, "gcp") == "gcp"

    def test_explicit_provider_still_wins_when_nothing_is_detectable(self):
        # The other documented use: `generate_iac_no_provider` tells the
        # operator to pass --provider when the contract declares no cloud.
        # There is nothing to contradict, so the gate must not fire.
        assert generate_iac._resolve_provider({"exposes": []}, "gcp") == "gcp"

    def test_local_contract_rejects_a_cloud_provider_override(self):
        # The reported reproduction: `--provider gcp` on the local-bound
        # hello-world contract emitted a resource-free module with exit 0.
        contract = {"exposes": [{"binding": {"platform": "local"}}]}
        with pytest.raises(CLIError) as exc:
            generate_iac._resolve_provider(contract, "gcp")
        assert exc.value.event == "generate_iac_provider_mismatch"

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

    # ── auto-detection from every documented binding location ──────────
    # Pre-2026-06 the resolver only read ``exposes[].binding.platform``, so a
    # contract that declared its cloud via the top-level ``binding.provider``
    # (the common single-binding shape) 422'd with "no supported cloud".

    def test_auto_detects_top_level_binding_provider(self):
        # The headline bug: `binding.provider: aws` must resolve to aws.
        contract = {"binding": {"provider": "aws", "region": "us-east-1"}}
        assert generate_iac._resolve_provider(contract, "auto") == "aws"

    def test_auto_detects_top_level_binding_platform(self):
        # Snowflake-style: cloud declared once at the root via `platform`.
        contract = {"binding": {"platform": "snowflake"}}
        assert generate_iac._resolve_provider(contract, "auto") == "snowflake"

    def test_auto_detects_expose_binding_provider(self):
        contract = {"exposes": [{"binding": {"provider": "gcp"}}]}
        assert generate_iac._resolve_provider(contract, "auto") == "gcp"

    def test_auto_detects_builds_provider(self):
        contract = {"builds": [{"provider": "aws"}]}
        assert generate_iac._resolve_provider(contract, "auto") == "aws"

    def test_auto_detects_builds_runtime_platform(self):
        contract = {"builds": [{"execution": {"runtime": {"platform": "gcp"}}}]}
        assert generate_iac._resolve_provider(contract, "auto") == "gcp"

    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("google", "gcp"),
            ("bigquery", "gcp"),
            ("s3", "aws"),
            ("glue", "aws"),
            ("athena", "aws"),
        ],
    )
    def test_auto_normalises_provider_aliases(self, alias, expected):
        contract = {"binding": {"provider": alias}}
        assert generate_iac._resolve_provider(contract, "auto") == expected

    def test_auto_infers_aws_from_region_only(self):
        # Region is a last-resort hint when no platform/provider is declared.
        contract = {"binding": {"location": {"region": "eu-west-2"}}}
        assert generate_iac._resolve_provider(contract, "auto") == "aws"

    def test_region_never_overrides_explicit_binding(self):
        # A GCP binding wins even when an AWS-shaped region is also present.
        contract = {"binding": {"provider": "gcp", "region": "us-east-1"}}
        assert generate_iac._resolve_provider(contract, "auto") == "gcp"

    def test_local_target_raises_actionable_error(self):
        # `local`/DuckDB is detected (not "unknown cloud") but has no IaC
        # plugin — the error must name `local` and point at `fluid apply`.
        contract = {"exposes": [{"binding": {"platform": "local"}}]}
        with pytest.raises(CLIError) as exc:
            generate_iac._resolve_provider(contract, "auto")
        assert exc.value.event == "generate_iac_local_target"
        assert "local" in exc.value.context["error"]

    def test_duckdb_alias_resolves_to_local_target_error(self):
        contract = {"builds": [{"provider": "duckdb"}]}
        with pytest.raises(CLIError) as exc:
            generate_iac._resolve_provider(contract, "auto")
        assert exc.value.event == "generate_iac_local_target"


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


class TestProviderMismatchEndToEnd:
    """The reported reproduction, driven through ``generate_iac.run``.

    `--provider gcp` on the local-bound hello-world contract used to emit a
    resource-free `main.tf.json`, print a warning, and exit 0 — and
    `--validate` on that module reported "OpenTofu validation passed."
    because `tofu validate` genuinely considers a resource-free config
    valid. Nothing downstream could catch it.
    """

    _LOCAL = _EXAMPLES / "01-hello-world" / "contract.fluid.yaml"
    _AWS = _EXAMPLES / "aws-iceberg-lakehouse" / "contract.fluid.yaml"

    def test_repro_exits_nonzero_and_writes_nothing(self, tmp_path):
        args = argparse.Namespace(
            contract=str(self._LOCAL), provider="gcp", out=str(tmp_path), env=None
        )
        with pytest.raises(CLIError) as exc:
            generate_iac.run(args, logging.getLogger("test"))
        assert exc.value.exit_code == 1
        assert exc.value.event == "generate_iac_provider_mismatch"
        # Rejected before any emit — no module for a later `tofu apply` to find.
        assert not (tmp_path / "main.tf.json").exists()

    def test_validate_cannot_report_green_on_the_repro(self, tmp_path, monkeypatch):
        from fluid_build.iac import runner

        def _boom():
            raise AssertionError("tofu must not be reached on a rejected provider")

        monkeypatch.setattr(runner, "tofu_path", _boom)
        args = argparse.Namespace(
            contract=str(self._LOCAL),
            provider="gcp",
            out=str(tmp_path),
            env=None,
            validate=True,
        )
        with pytest.raises(CLIError) as exc:
            generate_iac.run(args, logging.getLogger("test"))
        assert exc.value.event == "generate_iac_provider_mismatch"

    def test_wrong_cloud_emit_is_rejected(self, tmp_path):
        # Worse than the empty module: this AWS contract's S3-bound expose is
        # shape-compatible with the GCP emitter, which produced a
        # google_storage_bucket named after the S3 bucket carrying
        # `location: us-east-1` — an AWS region, invalid for GCS. A
        # zero-resource gate alone would not catch this one.
        args = argparse.Namespace(
            contract=str(self._AWS), provider="gcp", out=str(tmp_path), env=None
        )
        with pytest.raises(CLIError) as exc:
            generate_iac.run(args, logging.getLogger("test"))
        assert exc.value.event == "generate_iac_provider_mismatch"
        assert not (tmp_path / "main.tf.json").exists()

    def test_matching_provider_still_emits(self, tmp_path):
        args = argparse.Namespace(
            contract=str(self._AWS), provider="aws", out=str(tmp_path), env=None
        )
        assert generate_iac.run(args, logging.getLogger("test")) == 0
        doc = json.loads((tmp_path / "main.tf.json").read_text())
        assert doc.get("resource")


class TestEmptyModuleGate:
    """A module with no resources provisions nothing — that is a failure.

    Backstop for the emit-when-derivable emitters (PR #475), which can skip
    a resource on a *matching* provider when a required binding input is
    absent. `tofu validate` calls such a module valid, so this is the only
    layer that can tell "no infrastructure" from "no contract".
    """

    def _args(self, tmp_path, **kw) -> argparse.Namespace:
        base = dict(
            contract=str(_EXAMPLES / "aws-s3-glue-athena" / "contract.fluid.yaml"),
            provider="auto",
            out=str(tmp_path),
            env=None,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _force_empty(self, monkeypatch):
        from fluid_build.iac.providers.aws import AwsIacPlugin

        monkeypatch.setattr(AwsIacPlugin, "emit", lambda self, contract, actions: {})

    def test_zero_resources_is_an_error_by_default(self, tmp_path, monkeypatch):
        self._force_empty(monkeypatch)
        with pytest.raises(CLIError) as exc:
            generate_iac.run(self._args(tmp_path), logging.getLogger("test"))
        assert exc.value.exit_code == 1
        assert exc.value.event == "generate_iac_empty_module"

    def test_allow_empty_opts_out(self, tmp_path, monkeypatch):
        self._force_empty(monkeypatch)
        rc = generate_iac.run(self._args(tmp_path, allow_empty=True), logging.getLogger("test"))
        assert rc == 0
        doc = json.loads((tmp_path / "main.tf.json").read_text())
        assert "resource" not in doc

    def test_validate_is_not_reached_on_an_empty_module(self, tmp_path, monkeypatch):
        # Suggested-work item 3: --validate must not report success on a
        # module with no resources. The gate runs first, so tofu is never
        # consulted — and cannot answer "valid" for a module that does nothing.
        from fluid_build.iac import runner

        self._force_empty(monkeypatch)

        def _boom():
            raise AssertionError("tofu must not be reached on an empty module")

        monkeypatch.setattr(runner, "tofu_path", _boom)
        with pytest.raises(CLIError) as exc:
            generate_iac.run(self._args(tmp_path, validate=True), logging.getLogger("test"))
        assert exc.value.event == "generate_iac_empty_module"

    def test_nonempty_module_is_unaffected(self, tmp_path):
        assert generate_iac.run(self._args(tmp_path), logging.getLogger("test")) == 0


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
