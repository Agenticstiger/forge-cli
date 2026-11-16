# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""How ``fluid apply`` and ``fluid diff`` wire state encryption (``cli/_apply_opentofu_engine.py``).

``iac/encryption.py`` is pinned on its own (``test_iac_encryption.py``) and against a
real ``tofu`` (``test_iac_state_encryption_tofu.py``); this file pins the engine's
glue: ``resolve_state_target`` reads ``FLUID_STATE_ENCRYPTION`` and the migration
flag, ``_tofu_env`` joins the plugin's, the state backend's and the key's clouds and
fetches the key only on a supported ``tofu``, ``_plan_failure`` names an encryption
failure, and ``fluid apply`` takes the hidden flag.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import encryption as enc
from fluid_build.iac import get_iac_plugin, runner

pytestmark = pytest.mark.unit

ARN = "arn:aws:kms:eu-west-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"
GCP_KEY = "projects/fluid-lab-1/locations/europe-west1/keyRings/fluid/cryptoKeys/state"
CONTRACT: Dict[str, Any] = {"id": "demo.encrypted", "exposes": []}


@pytest.fixture(autouse=True)
def _no_operator_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FLUID_STATE_ENCRYPTION", "FLUID_STATE_BACKEND"):
        monkeypatch.delenv(name, raising=False)


def _args(tmp_path, **extra: Any) -> argparse.Namespace:
    return argparse.Namespace(state_backend=None, workspace_dir=str(tmp_path), **extra)


class TestResolveStateTarget:
    def test_unset_leaves_the_state_unencrypted(self, tmp_path):
        target = engine.resolve_state_target(_args(tmp_path), CONTRACT, "aws")
        assert target.encryption is None and target.migrate_encryption is False

    def test_the_key_and_the_migration_flag_reach_the_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLUID_STATE_ENCRYPTION", f"aws-kms://{ARN}")
        args = _args(tmp_path, migrate_state_encryption=True)
        target = engine.resolve_state_target(args, CONTRACT, "aws")
        assert (target.encryption.provider, target.encryption.region) == ("aws_kms", "eu-west-1")
        assert target.migrate_encryption is True

    def test_a_value_forge_cannot_use_is_refused_with_the_forms_it_takes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FLUID_STATE_ENCRYPTION", "kms://alias/state")
        with pytest.raises(CLIError) as raised:
            engine.resolve_state_target(_args(tmp_path), CONTRACT, "aws")
        assert raised.value.event == "opentofu_state_encryption_invalid"
        assert any("pbkdf2+<secret reference>" in s for s in raised.value.suggestions)

    def test_migrating_without_a_key_is_refused(self, tmp_path):
        with pytest.raises(CLIError) as raised:
            engine.resolve_state_target(
                _args(tmp_path, migrate_state_encryption=True), CONTRACT, "aws"
            )
        assert raised.value.event == "opentofu_state_encryption_invalid"
        assert raised.value.suggestions == ["Set FLUID_STATE_ENCRYPTION, then re-run"]


def _target(tmp_path, spec: str = "", backend: Any = None) -> engine.StateTarget:
    return engine.StateTarget(
        workdir=tmp_path,
        backend=backend,
        origin="default",
        encryption=enc.parse_state_encryption(spec, {}) if spec else None,
    )


class TestTofuEnv:
    def test_the_backend_and_the_key_clouds_join_the_plugin_family(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "tofu_version", lambda: (1, 13, 1))
        for name, value in {
            "AWS_PROFILE": "state",
            "GOOGLE_APPLICATION_CREDENTIALS": "/adc.json",
            "SNOWFLAKE_ACCOUNT": "org-acct",
            "GITHUB_TOKEN": "ghp_x",  # pragma: allowlist secret
        }.items():
            monkeypatch.setenv(name, value)
        backend = {"s3": {"bucket": "b", "key": "fluid/x/snowflake/terraform.tfstate"}}
        env = engine._tofu_env(
            get_iac_plugin("snowflake"), _target(tmp_path, f"gcp-kms://{GCP_KEY}", backend)
        )
        assert env["AWS_PROFILE"] == "state"  # the S3 state backend
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/adc.json"  # the gcp-kms key
        assert env["SNOWFLAKE_ORGANIZATION_NAME"] == "org"  # the plugin's own overlay
        assert "GITHUB_TOKEN" not in env

    def test_without_encryption_no_key_variable_is_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "tofu_version", lambda: (1, 6, 0))
        env = engine._tofu_env(get_iac_plugin("aws"), _target(tmp_path))
        assert not [k for k in env if k.startswith("TF_VAR_") or k.startswith("BAO_")]

    def test_the_passphrase_reaches_the_child_environment_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "tofu_version", lambda: (1, 13, 1))
        monkeypatch.setenv("LAB_STATE_PASSPHRASE", "correct-horse-battery-staple")
        target = _target(tmp_path, "pbkdf2+env://LAB_STATE_PASSPHRASE")
        env = engine._tofu_env(get_iac_plugin("aws"), target)
        assert env["TF_VAR_fluid_state_passphrase"] == "correct-horse-battery-staple"
        import os

        assert "TF_VAR_fluid_state_passphrase" not in os.environ

    def test_an_old_tofu_is_refused_before_any_key_is_fetched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "tofu_version", lambda: (1, 10, 7))

        def _never(_encryption):
            raise AssertionError("the key was fetched for a tofu that cannot use it")

        monkeypatch.setattr(engine, "encryption_env", _never)
        target = _target(tmp_path, "pbkdf2+env://LAB_STATE_PASSPHRASE")
        with pytest.raises(CLIError) as raised:
            engine._tofu_env(get_iac_plugin("aws"), target)
        assert raised.value.event == "opentofu_state_encryption_unsupported"
        assert "1.10.7" in raised.value.context["error"]

    def test_a_key_that_does_not_resolve_is_a_typed_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runner, "tofu_version", lambda: (1, 13, 1))
        monkeypatch.delenv("LAB_STATE_PASSPHRASE", raising=False)
        target = _target(tmp_path, "pbkdf2+env://LAB_STATE_PASSPHRASE")
        with pytest.raises(CLIError) as raised:
            engine._tofu_env(get_iac_plugin("aws"), target)
        assert raised.value.event == "opentofu_state_encryption_unavailable"


class TestPlanFailure:
    def test_plaintext_state_under_encryption_names_the_migration(self):
        plan = runner.TofuResult(
            command="plan",
            returncode=1,
            stdout="",
            stderr="Error: failed to write backup file: encountered unencrypted payload\n"
            "without unencrypted method configured",
        )
        err = engine._plan_failure(get_iac_plugin("aws"), CONTRACT, plan, "/nonexistent", {})
        assert err.event == "opentofu_state_not_encrypted"
        assert any("--migrate-state-encryption" in s for s in err.suggestions)

    def test_other_plan_failures_keep_their_own_error(self):
        plan = runner.TofuResult(command="plan", returncode=1, stdout="", stderr="Error: boom")
        err = engine._plan_failure(get_iac_plugin("aws"), CONTRACT, plan, "/nonexistent", {})
        assert err.event == "opentofu_plan_failed"


class TestApplyFlag:
    def test_apply_takes_the_hidden_migration_flag(self):
        from fluid_build.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["apply", "contract.fluid.yaml", "--migrate-state-encryption"])
        assert args.migrate_state_encryption is True
        assert parser.parse_args(["apply", "contract.fluid.yaml"]).migrate_state_encryption is False
