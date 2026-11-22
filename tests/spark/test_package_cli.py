# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A prebuilt package build through the real ``fluid`` entry point: validate, plan and
generate iac."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest
import yaml

from fluid_build.iac.providers import aws_identity
from fluid_build.spark import names
from fluid_build.spark.package import PACKAGE_ALLOWLIST_ENV

from .package_fixtures import PACKAGE_CONTRACT, SHA, SOURCE

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LISTED = "s3://commerce-artifacts/code/"
#: The operator's lists for the fixture's cluster and package store, and nothing else.
OPERATOR = {
    aws_identity.TARGET_ALLOWLIST_ENV: "emr-ec2:j-2AXXXXXXGAPLF",
    PACKAGE_ALLOWLIST_ENV: LISTED,
}


def _fluid(*args: str, cwd: Path, env: Optional[Dict[str, str]] = None):
    """``fluid`` as an operator runs it: a clean home (no schema cache), the operator's
    lists only, no AWS credentials."""
    keep = {"PATH", "SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL"}
    child = {k: v for k, v in os.environ.items() if k in keep}
    child.update(env or {})
    child["HOME"] = str(cwd / "home")
    child["PYTHONPATH"] = str(ROOT)
    child["COLUMNS"] = "200"
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=cwd,
        env=child,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture
def contract_file(tmp_path):
    (tmp_path / "home").mkdir()
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(PACKAGE_CONTRACT, sort_keys=False))
    return path


def test_validate_accepts_a_package_build(contract_file, tmp_path):
    done = _fluid("validate", str(contract_file), "--offline", cwd=tmp_path, env=OPERATOR)
    assert done.returncode == 0, done.stdout + done.stderr
    assert PACKAGE_ALLOWLIST_ENV not in done.stdout + done.stderr


def test_validate_warns_until_the_operator_lists_the_store(contract_file, tmp_path):
    env = {k: v for k, v in OPERATOR.items() if k != PACKAGE_ALLOWLIST_ENV}
    done = _fluid("validate", str(contract_file), "--offline", cwd=tmp_path, env=env)
    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    assert PACKAGE_ALLOWLIST_ENV in output and "merge-orders" in output


def test_validate_refuses_a_package_it_cannot_run(contract_file, tmp_path):
    contract = yaml.safe_load(contract_file.read_text())
    contract["builds"][0]["properties"]["package"]["sha256"] = SHA.upper()
    contract_file.write_text(yaml.safe_dump(contract, sort_keys=False))
    done = _fluid("validate", str(contract_file), "--offline", cwd=tmp_path, env=OPERATOR)
    assert done.returncode != 0
    assert "sha256" in done.stdout + done.stderr


def test_generate_iac_copies_the_pinned_package(contract_file, tmp_path):
    out = tmp_path / "iac"
    done = _fluid(
        "generate", "iac", str(contract_file), "--provider", "aws", "--out", str(out),
        cwd=tmp_path, env=OPERATOR,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    module = json.loads((out / "main.tf.json").read_text())
    (copy,) = module["resource"]["aws_s3_object_copy"].values()
    assert copy["key"] == names.package_key("commerce.orders-silver", "merge-orders", SHA)
    assert copy["source"] == SOURCE.removeprefix("s3://")
    assert "checksum_sha256" in copy["lifecycle"]["precondition"][0]["condition"]


def test_generate_iac_refuses_an_unlisted_store(contract_file, tmp_path):
    env = {k: v for k, v in OPERATOR.items() if k != PACKAGE_ALLOWLIST_ENV}
    done = _fluid(
        "generate", "iac", str(contract_file), "--provider", "aws", "--out", str(tmp_path / "iac"),
        cwd=tmp_path, env=env,
    )  # fmt: skip
    assert done.returncode != 0
    assert PACKAGE_ALLOWLIST_ENV in done.stdout + done.stderr
    assert not (tmp_path / "iac" / "main.tf.json").exists()
