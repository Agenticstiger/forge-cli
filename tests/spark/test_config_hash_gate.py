# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The job config a run starts on is the one fluid apply compiled: apply passes its sha256
at submit (``--fluid-job-sha256``) on every runtime, and the runner refuses any other
config before it reads anything. On AWS, apply reads the config OpenTofu uploaded back and
checks it first, the account and the MSK brokers OpenTofu fills in aside."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from fluid_build.build_runners.spark._desired import desired_runs
from fluid_build.iac import get_iac_plugin
from tests.iac.test_iac_aws_spark_infra import (  # noqa: F401 - operator_policy is a fixture
    CONTRACT,
    KEY,
    operator_policy,
)

ACCOUNT = "123456789012"
BROKERS = (
    "b-1.orders.kafka.eu-west-1.amazonaws.com:9098,b-2.orders.kafka.eu-west-1.amazonaws.com:9098"
)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_runner_takes_the_config_it_was_started_on(runner):
    text = '{"configVersion": 1}\n'
    assert runner.checked_text(text, sha(text), "s3://b/job.json") == text
    assert runner.checked_text(text, sha(text).upper(), "s3://b/job.json") == text
    assert runner.checked_text(text, None, "s3://b/job.json") == text  # started by hand


def test_the_runner_refuses_another_config_before_reading_anything(runner):
    with pytest.raises(runner.JobConfigError, match="not the one fluid apply started") as caught:
        runner.checked_text('{"configVersion": 1}\n', sha("{}"), "s3://b/job.json")
    assert sha("{}") in str(caught.value) and "s3://b/job.json" in str(caught.value)


def uploaded(contract):
    """The job config OpenTofu uploads, with what it fills in at apply resolved."""
    resources = get_iac_plugin("aws").emit(contract)
    text = resources["aws_s3_object"][f"{KEY}_config"]["content"]
    text = str(text).replace("${data.aws_caller_identity.fluid_lf_caller.account_id}", ACCOUNT)
    return text.replace(
        f"${{data.aws_msk_bootstrap_brokers.{KEY}.bootstrap_brokers_sasl_iam}}", BROKERS
    )


@pytest.mark.parametrize("bucketless", [False, True], ids=["named bucket", "account bucket"])
def test_the_config_opentofu_uploads_is_the_one_apply_checks(bucketless):
    contract = copy.deepcopy(CONTRACT)
    if bucketless:
        loc = contract["exposes"][0]["binding"]["location"]
        del loc["bucket"]
        loc["path"] = "commerce.orders-stream/order_lines/"
    body = uploaded(contract)
    assert "${" not in body and BROKERS in body
    (run,) = desired_runs(contract, account=ACCOUNT)
    assert run.config_matches(body.encode("utf-8"))
    # Another config in its place: a changed field, or other brokers' config is refused.
    changed = json.loads(body)
    changed["sink"]["table"] = "commerce.other"
    assert not run.config_matches(json.dumps(changed).encode("utf-8"))
