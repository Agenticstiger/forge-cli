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

"""End-to-end pin for the AWS-first example contracts under ``examples/aws-*``.

Each example is a single ``platform: aws`` data product that must, **fully
offline** (no AWS account, no credentials, no network, no ``tofu``):

  1. Pass ``fluid validate`` against its declared ``fluidVersion``.
  2. Compile through ``fluid generate iac`` into a credential-free
     ``main.tf.json`` whose resources are exactly the AWS services the
     example claims to exercise (Glue Data Catalog + S3; Athena reads the
     Glue catalog natively, so it needs no distinct resource).

Both steps run in-process against the real CLI entry point
(``fluid_build.cli.main``) in an isolated ``tmp_path``. The ``_no_aws``
fixture strips every AWS credential source from the environment first, so a
green run is positive proof that these examples require no live AWS — the
IaC emit degrades gracefully to ``account_id = null`` and still produces a
valid module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"


# Per-example expectations. ``counts`` is the exact set of AWS resource
# *types* the offline emit must produce and how many of each — a rename or
# regression in the AWS IaC emitter, or a drift in the example contract,
# trips the relevant assertion.
AWS_EXAMPLES: Dict[str, Dict[str, Any]] = {
    "aws-s3-glue-athena": {
        "services": "S3 + Glue Data Catalog + Athena (native)",
        "counts": {
            "aws_glue_catalog_database": 1,
            "aws_glue_catalog_table": 1,
            "aws_s3_bucket": 1,
        },
        "iceberg": False,
    },
    "aws-iceberg-lakehouse": {
        "services": "S3 + Glue Data Catalog (Apache Iceberg) + Athena",
        "counts": {
            "aws_glue_catalog_database": 1,
            "aws_glue_catalog_table": 1,
            "aws_s3_bucket": 1,
        },
        "iceberg": True,
    },
    "aws-medallion-lake": {
        # Bronze (raw CSV) + Silver (curated Parquet) = two Glue databases
        # and two tables sharing one S3 bucket.
        "services": "S3 (raw + curated) + Glue Data Catalog + Athena",
        "counts": {
            "aws_glue_catalog_database": 2,
            "aws_glue_catalog_table": 2,
            "aws_s3_bucket": 1,
        },
        "iceberg": False,
    },
}

EXAMPLE_IDS = sorted(AWS_EXAMPLES)


def _contract_path(example: str) -> Path:
    return EXAMPLES_DIR / example / "contract.fluid.yaml"


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open() as fh:
        return yaml.safe_load(fh)


@pytest.fixture()
def _no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee the emit path can reach no live AWS.

    Removes every ambient credential source (env vars, shared files,
    profile, EC2/ECS metadata) so the test proves the offline property
    rather than accidentally relying on a developer's ``~/.aws``.
    """
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCOUNT_ID",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(key, raising=False)
    # Point credential/config files at a nonexistent path and disable the
    # instance-metadata endpoints so boto3 cannot resolve an identity.
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def test_expected_example_dirs_are_present() -> None:
    """Each pinned example ships a contract, a README, and nothing missing."""
    for example in AWS_EXAMPLES:
        example_dir = EXAMPLES_DIR / example
        assert (example_dir / "contract.fluid.yaml").is_file(), f"{example}: missing contract"
        assert (example_dir / "README.md").is_file(), f"{example}: missing README"


@pytest.mark.parametrize("example", EXAMPLE_IDS)
def test_contract_targets_aws(example: str) -> None:
    """Every exposure binds to ``platform: aws`` and declares a version."""
    contract = _load_yaml(_contract_path(example))
    assert contract.get("fluidVersion"), f"{example}: no fluidVersion"
    exposes: List[Dict[str, Any]] = contract.get("exposes") or []
    assert exposes, f"{example}: no exposes[]"
    platforms = {(e.get("binding") or {}).get("platform") for e in exposes}
    assert platforms == {"aws"}, f"{example}: non-AWS bindings {platforms}"


@pytest.mark.parametrize("example", EXAMPLE_IDS)
def test_contract_validates_offline(example: str, _no_aws: None) -> None:
    """``fluid validate --offline`` accepts the contract (exit 0)."""
    rc = main(["validate", str(_contract_path(example)), "--offline", "--quiet"])
    assert rc == 0, f"{example}: fluid validate returned {rc}"


@pytest.mark.parametrize("example", EXAMPLE_IDS)
def test_generate_iac_emits_expected_aws_resources_offline(
    example: str, tmp_path: Path, _no_aws: None
) -> None:
    """``fluid generate iac`` emits a valid, credential-free ``main.tf.json``.

    Runs with all AWS credentials stripped (``_no_aws``) into an isolated
    ``tmp_path`` — a green assertion here is proof no live AWS is required.
    """
    rc = main(["generate", "iac", str(_contract_path(example)), "--out", str(tmp_path)])
    assert rc == 0, f"{example}: fluid generate iac returned {rc}"

    module_path = tmp_path / "main.tf.json"
    assert module_path.is_file(), f"{example}: no main.tf.json emitted"

    text = module_path.read_text()
    module = json.loads(text)  # must be valid JSON
    resources = module.get("resource", {})

    spec = AWS_EXAMPLES[example]
    actual_counts = {rtype: len(entries) for rtype, entries in resources.items()}
    assert (
        actual_counts == spec["counts"]
    ), f"{example}: emitted resources {actual_counts} != expected {spec['counts']}"

    # The emitted module must never carry credentials — the whole point of
    # the credential-free emit contract.
    lowered = text.lower()
    assert "aws_access_key" not in lowered, f"{example}: emitted an access key"
    assert "aws_secret_access_key" not in lowered, f"{example}: emitted a secret key"
    assert "AKIA" not in text, f"{example}: emitted an AKIA access-key id"

    # Iceberg examples must tag the Glue table so Athena treats it as an
    # Iceberg table (ACID / time-travel); plain examples must not.
    tables = resources.get("aws_glue_catalog_table", {})
    table_types = {body.get("parameters", {}).get("table_type") for body in tables.values()}
    if spec["iceberg"]:
        assert "ICEBERG" in table_types, f"{example}: expected an ICEBERG table"
    else:
        assert "ICEBERG" not in table_types, f"{example}: unexpected ICEBERG table"
