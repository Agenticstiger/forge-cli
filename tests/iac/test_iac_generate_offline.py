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

"""``fluid generate iac`` calls no cloud API.

The native planner's AWS provider resolved a missing account with
``sts:GetCallerIdentity`` over whatever credentials the machine had, so
``fluid generate iac`` called AWS whenever ``AWS_ACCOUNT_ID`` was unset, and
wrote the account it found into the ARNs of the planned orchestration. The
examples' READMEs say the command needs no AWS account and no credentials.

Every boto3 client and resource is made by botocore's
``Session.create_client`` (boto3's ``Session.client`` returns
``self._session.create_client(...)`` and ``Session.resource`` calls
``client``), so the guard replaces it and records each call. The provider
swallows any error from the lookup, so the record, not an exception, is what
fails a test. ``fluid apply`` still resolves the account; the last class pins
that.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import generate_iac, main
from fluid_build.cli._common import CLIError

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
AWS_EXAMPLES = sorted(_EXAMPLES.glob("aws-*/contract*.fluid.yaml"))
ACCOUNT = "123456789012"


@pytest.fixture
def no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """No account, no credential source: env vars, shared files, profile, metadata."""
    for key in (
        "AWS_ACCOUNT_ID",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "FLUID_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """The service of every boto3 client constructed while the test runs."""
    session = pytest.importorskip("botocore.session")
    made: List[str] = []

    def refuse(self: Any, service_name: str, *args: Any, **kwargs: Any) -> Any:
        made.append(service_name)
        raise AssertionError(f"a boto3 {service_name} client was constructed")

    monkeypatch.setattr(session.Session, "create_client", refuse)
    return made


def _write(tmp_path: Path, contract: Dict[str, Any]) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _orders(**extra: Any) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "offline_orders",
        "name": "Offline orders",
        "metadata": {"layer": "Silver", "owner": {"team": "data", "email": "d@example.com"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "bucket": "offline-lake",
                        "path": "orders/",
                        "database": "sales",
                        "table": "orders",
                        "region": "us-east-1",
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }
    contract.update(extra)
    return contract


def _lake_formation() -> Dict[str, Any]:
    """Lake Formation grants to another account, on the ``{account}-fluid-data`` fallback."""
    contract = _orders()
    binding = contract["exposes"][0]["binding"]
    binding["location"]["bucket"] = "{{ env.FLUID_TEST_OFFLINE_BUCKET }}"
    binding["governance"] = {
        "lakeFormation": {
            "registerLocation": True,
            "grants": [
                {
                    "principal": "arn:aws:iam::222222222222:role/reader",
                    "permissions": ["SELECT", "DESCRIBE"],
                }
            ],
        }
    }
    return contract


def _scheduled() -> Dict[str, Any]:
    """An EventBridge schedule, whose planned Lambda and schedule ARNs name the account."""
    return _orders(orchestration={"schedule": "rate(1 hour)"})


def _args(contract: Path, out: Path) -> argparse.Namespace:
    return argparse.Namespace(contract=str(contract), provider="auto", out=str(out), env=None)


@pytest.mark.usefixtures("no_aws")
class TestGenerateIacCallsNoCloudApi:
    def test_the_examples_exist(self):
        assert len(AWS_EXAMPLES) >= 3, "the parametrisation below would prove nothing"

    @pytest.mark.parametrize("flags", [[], ["--shadow"]], ids=["plain", "shadow"])
    @pytest.mark.parametrize("contract", AWS_EXAMPLES, ids=lambda p: f"{p.parent.name}/{p.name}")
    def test_an_example_constructs_no_boto3_client(self, contract, flags, tmp_path, clients):
        rc = main(["generate", "iac", str(contract), "--out", str(tmp_path), *flags])
        assert rc == 0
        assert clients == []
        text = (tmp_path / "main.tf.json").read_text(encoding="utf-8")
        assert generate_iac.UNSET_AWS_ACCOUNT not in text

    def test_lake_formation_on_the_fallback_bucket_constructs_no_client(
        self, tmp_path, clients, monkeypatch
    ):
        monkeypatch.delenv("FLUID_TEST_OFFLINE_BUCKET", raising=False)
        out = tmp_path / "out"
        rc = main(["generate", "iac", str(_write(tmp_path, _lake_formation())), "--out", str(out)])
        assert rc == 0
        assert clients == []
        # The emitter's account references stay lookups ``tofu`` makes at plan time.
        text = (out / "main.tf.json").read_text(encoding="utf-8")
        assert "${data.aws_caller_identity.fluid_lf_caller.account_id}-fluid-data" in text

    def test_an_orchestration_that_names_the_account_is_refused(self, tmp_path, clients):
        out = tmp_path / "out"
        with pytest.raises(CLIError) as excinfo:
            generate_iac.run(_args(_write(tmp_path, _scheduled()), out), logging.getLogger("t"))
        assert excinfo.value.event == "generate_iac_aws_account_required"
        assert "AWS_ACCOUNT_ID" in excinfo.value.context["error"]
        assert excinfo.value.suggestions
        assert not (out / "main.tf.json").exists(), "nothing is written with a placeholder"
        assert clients == []

    def test_with_aws_account_id_the_orchestration_is_emitted(self, tmp_path, clients, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        out = tmp_path / "out"
        rc = generate_iac.run(_args(_write(tmp_path, _scheduled()), out), logging.getLogger("t"))
        assert rc == 0
        module = json.loads((out / "main.tf.json").read_text(encoding="utf-8"))
        (function,) = module["resource"]["aws_lambda_function"].values()
        assert function["role"] == f"arn:aws:iam::{ACCOUNT}:role/fluid-workflow-execution"
        assert clients == []


@pytest.mark.usefixtures("no_aws")
class TestApplyStillResolvesTheAccount:
    """``fluid apply`` builds its native plan with ``native_actions(contract, logger)``
    (``cli/_apply_opentofu_engine.py``); only ``generate iac`` passes ``offline``."""

    def test_the_apply_call_asks_sts_for_a_missing_account(self, clients):
        generate_iac.native_actions(_scheduled(), logging.getLogger("t"))
        assert clients == ["sts"]

    def test_the_offline_call_plans_without_asking(self, clients):
        actions = generate_iac.native_actions(_scheduled(), logging.getLogger("t"), offline=True)
        assert clients == []
        # The planner still ran, sovereignty check included, on the placeholder.
        assert any(a.get("op") == "lambda.ensure_function" for a in actions)
