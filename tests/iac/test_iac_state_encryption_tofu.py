# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Proof against a real ``tofu`` (design invariant I1): the state key never lands anywhere.

The apply engine's own pieces run here, with no cloud and no provider download:
``StateTarget`` with ``FLUID_STATE_ENCRYPTION=pbkdf2+env://FLUID_CANARY_SECRET``,
``emit_module`` (``main.tf.json``), ``_tofu_env`` (the allowlisted environment,
with the passphrase resolved through the operator's secret policy) and the
runner's ``init``, ``plan`` and ``apply``. The plugin is a stand-in whose one
resource is OpenTofu's built-in ``terraform_data``.

The canary passphrase must appear in no file of the workdir (``main.tf.json``,
the state, the saved plan, ``.terraform/``) and in neither ``tofu show -json``
of the plan nor ``tofu state pull``; the state and the plan on disk must be
encrypted. A planted canary shows the scan can find one. The migration of
plaintext state and the three refusals are driven through the same pieces.

Skips without ``tofu`` 1.11 or newer (ephemeral variables).
"""

from __future__ import annotations

import json
import logging
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

import pytest

from fluid_build.cli._apply_opentofu_engine import (
    StateTarget,
    _encryption_failure,
    _tofu_env,
    emit_module,
)
from fluid_build.iac import runner
from fluid_build.iac.encryption import MIN_TOFU_VERSION, parse_state_encryption

_VERSION = runner.tofu_version()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        runner.tofu_path() is None or _VERSION is None or _VERSION < MIN_TOFU_VERSION,
        reason="needs tofu 1.11 or newer",
    ),
]

CANARY_ENV = "FLUID_CANARY_SECRET"
CANARY = "fluid-canary-6f1e9c2a-state-passphrase"
_CONTRACT = {"id": "canary.encryption", "exposes": []}
_LOG = logging.getLogger("test.state_encryption")


class _BuiltinOnlyPlugin:
    """A stand-in IaC plugin: one ``terraform_data``, built into OpenTofu."""

    name = "canary"
    required_providers: Dict[str, Dict[str, str]] = {}
    credential_env_vars = ()
    credential_env_prefixes = ()

    def __init__(self, value: str = "not-secret") -> None:
        self.value = value

    def emit(self, contract: Mapping[str, Any], actions: Iterable[Any] = ()) -> Dict[str, Any]:
        return {"terraform_data": {"holder": {"input": self.value}}}

    def emit_data(self, contract: Mapping[str, Any], actions: Iterable[Any] = ()) -> Dict[str, Any]:
        return {}

    def credential_env(self, env: Mapping[str, str]) -> Dict[str, str]:
        return {}

    def provider_block(self) -> Dict[str, Any]:
        return {}


@pytest.fixture
def canary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setenv(CANARY_ENV, CANARY)
    monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", str(tmp_path / "plugin-cache"))
    (tmp_path / "plugin-cache").mkdir()
    return CANARY


def _target(workdir: Path, spec: str = "", *, migrate: bool = False) -> StateTarget:
    encryption = parse_state_encryption(spec) if spec else None
    return StateTarget(
        workdir=workdir,
        backend=None,
        origin="default",
        encryption=encryption,
        migrate_encryption=migrate,
    )


def _run(plugin: _BuiltinOnlyPlugin, target: StateTarget) -> runner.TofuResult:
    """init, plan and apply one module, the way ``fluid apply`` does: one environment."""
    workdir = target.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    module, _ = emit_module(plugin, _CONTRACT, target, _LOG)
    (workdir / "main.tf.json").write_text(module, encoding="utf-8")
    env = _tofu_env(plugin, target)
    init = runner.tofu_init(str(workdir), backend=False, env=env)
    assert init.ok, init.stderr
    plan = runner.tofu_plan(str(workdir), env=env)
    if not plan.ok:
        return plan
    return runner.tofu_apply(str(workdir), plan_file="tfplan", env=env)


def _hits(root: Path, needle: str) -> List[str]:
    """Every file under ``root`` holding ``needle``; zip members (a plain plan) opened too."""
    found: List[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        data = path.read_bytes()
        if needle.encode() in data:
            found.append(str(path.relative_to(root)))
        elif zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                found += [
                    f"{path.relative_to(root)}!{name}"
                    for name in archive.namelist()
                    if needle.encode() in archive.read(name)
                ]
    return found


SPEC = f"pbkdf2+env://{CANARY_ENV}"


def test_the_canary_passphrase_reaches_no_file_plan_or_state(tmp_path: Path, canary: str) -> None:
    target = _target(tmp_path / "work", SPEC)
    applied = _run(_BuiltinOnlyPlugin(), target)
    assert applied.ok, applied.stderr

    workdir = target.workdir
    env = _tofu_env(_BuiltinOnlyPlugin(), target)
    shown_plan = runner.tofu_show_plan(str(workdir), plan_file="tfplan", env=env)
    pulled = runner.tofu_state_pull(str(workdir), env=env)
    assert shown_plan is not None and pulled.ok

    assert _hits(workdir, canary) == []
    assert canary not in json.dumps(shown_plan)
    assert canary not in pulled.stdout
    # Not vacuous: the state and the plan were written, and both are encrypted.
    state = json.loads((workdir / "terraform.tfstate").read_text())
    assert "encrypted_data" in state and "resources" not in state
    assert not zipfile.is_zipfile(workdir / "tfplan")
    assert json.loads(pulled.stdout)["resources"], "the decrypted state holds the resource"
    # main.tf.json names the variable, never its value.
    module = (workdir / "main.tf.json").read_text()
    assert "${var.fluid_state_passphrase}" in module


def test_the_scan_finds_a_planted_canary(tmp_path: Path, canary: str) -> None:
    target = _target(tmp_path / "work")
    applied = _run(_BuiltinOnlyPlugin(value=canary), target)
    assert applied.ok, applied.stderr
    assert "terraform.tfstate" in _hits(target.workdir, canary)


def test_a_saved_plan_cannot_be_applied_without_the_key(tmp_path: Path, canary: str) -> None:
    target = _target(tmp_path / "work", SPEC)
    workdir = target.workdir
    workdir.mkdir(parents=True)
    module, _ = emit_module(_BuiltinOnlyPlugin(), _CONTRACT, target, _LOG)
    (workdir / "main.tf.json").write_text(module, encoding="utf-8")
    env = _tofu_env(_BuiltinOnlyPlugin(), target)
    assert runner.tofu_init(str(workdir), backend=False, env=env).ok
    assert runner.tofu_plan(str(workdir), env=env).ok
    keyless = {k: v for k, v in env.items() if not k.startswith("TF_VAR_")}
    assert not runner.tofu_apply(str(workdir), plan_file="tfplan", env=keyless).ok


def test_plaintext_state_migrates_once_then_stays_enforced(tmp_path: Path, canary: str) -> None:
    workdir = tmp_path / "work"
    assert _run(_BuiltinOnlyPlugin(), _target(workdir)).ok  # plaintext state
    assert "resources" in json.loads((workdir / "terraform.tfstate").read_text())

    refused = _run(_BuiltinOnlyPlugin("v2"), _target(workdir, SPEC))
    assert not refused.ok
    error = _encryption_failure(refused.stdout + refused.stderr)
    assert error is not None and error.event == "opentofu_state_not_encrypted"

    migrated = _run(_BuiltinOnlyPlugin("v2"), _target(workdir, SPEC, migrate=True))
    assert migrated.ok, migrated.stderr
    assert "encrypted_data" in json.loads((workdir / "terraform.tfstate").read_text())

    again = _run(_BuiltinOnlyPlugin("v3"), _target(workdir, SPEC))
    assert again.ok, again.stderr


def test_encrypted_state_without_the_key_or_with_another_is_explained(
    tmp_path: Path, canary: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "work"
    assert _run(_BuiltinOnlyPlugin(), _target(workdir, SPEC)).ok

    keyless = _run(_BuiltinOnlyPlugin("v2"), _target(workdir))
    error = _encryption_failure(keyless.stdout + keyless.stderr)
    assert error is not None and error.event == "opentofu_state_encrypted_without_key"

    monkeypatch.setenv(CANARY_ENV, "another-passphrase-of-enough-length")
    wrong = _run(_BuiltinOnlyPlugin("v2"), _target(workdir, SPEC))
    error = _encryption_failure(wrong.stdout + wrong.stderr)
    assert error is not None and error.event == "opentofu_state_decrypt_failed"


#: What tofu says when it cannot decode a key provider block (measured on 1.13.1
#: with a misspelt attribute), as against failing to reach the key it names.
_DECODE_ERRORS = (
    "Extraneous JSON object property",
    "Missing required argument",
    "Unsupported argument",
)

_UNREACHABLE = "http://127.0.0.1:9"


@pytest.mark.parametrize(
    ("spec", "env", "provider"),
    [
        (
            "aws-kms://arn:aws:kms:eu-west-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            {
                "AWS_ACCESS_KEY_ID": "AKIAFAKEFAKEFAKE",
                "AWS_SECRET_ACCESS_KEY": "fake",  # pragma: allowlist secret
                "AWS_CONFIG_FILE": os.devnull,
                "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_ENDPOINT_URL_STS": _UNREACHABLE,
                "AWS_ENDPOINT_URL_KMS": _UNREACHABLE,
            },
            "aws_kms",
        ),
        (
            "gcp-kms://projects/fluid-lab-1/locations/europe-west1/keyRings/fluid/cryptoKeys/state",
            {"GCE_METADATA_HOST": "127.0.0.1:9"},
            "gcp_kms",
        ),
        (
            "openbao://transit/fluid",
            {"BAO_ADDR": _UNREACHABLE, "BAO_TOKEN": "fake"},  # pragma: allowlist secret
            "openbao",
        ),
    ],
)
def test_each_key_provider_block_decodes_and_only_the_key_is_out_of_reach(
    tmp_path: Path, spec: str, env: Dict[str, str], provider: str
) -> None:
    """No cloud and no store: the emitted block must get as far as asking for its key.

    ``tofu validate`` does not decode key providers, so a misspelt attribute
    passes it; a plan does, and fails on the attribute before any key is asked.
    """
    target = StateTarget(
        workdir=tmp_path,
        backend=None,
        origin="default",
        encryption=parse_state_encryption(spec, {}),
    )
    module, _ = emit_module(_BuiltinOnlyPlugin(), _CONTRACT, target, _LOG)
    (tmp_path / "main.tf.json").write_text(module, encoding="utf-8")
    child = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CLOUDSDK_CONFIG": str(tmp_path / "gcloud"),
        **env,
    }
    assert runner.tofu_init(str(tmp_path), backend=False, env=child).ok
    plan = runner.tofu_plan(str(tmp_path), env=child)
    output = " ".join((plan.stdout + plan.stderr).split())
    assert not plan.ok
    assert f"key_provider.{provider}.fluid failed" in output
    assert not any(error in output for error in _DECODE_ERRORS)
