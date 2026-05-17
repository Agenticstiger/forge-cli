# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end: a real ``tofu apply`` of plugin-emitted ``.tf.json``.

Proves the autogenerator concept — contract → ``.tf.json`` → ``tofu
apply`` → real cloud resources — without a real AWS account or
credentials, using the LocalStack AWS emulator.

Skipped unless ``tofu`` is installed AND LocalStack is reachable on
``localhost:4566``. Start LocalStack with::

    docker run -d -p 4566:4566 localstack/localstack
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.credentials import build_tofu_env

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.provider]

_LOCALSTACK = "http://localhost:4566"


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{_LOCALSTACK}/_localstack/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


_SKIP = runner.tofu_path() is None or not _localstack_up()

# An AWS contract whose plugin output is S3-only — the exposure has a
# bucket but no Glue database/table, so the AWS plugin emits a single
# ``aws_s3_bucket`` (S3 is fully supported by LocalStack community).
_S3_CONTRACT = {
    "id": "demo.lake",
    "exposes": [
        {
            "exposeId": "raw",
            "binding": {
                "platform": "aws",
                "format": "parquet",
                "location": {"bucket": "fluid-localstack-e2e"},
            },
        }
    ],
}

# A LocalStack provider override, written alongside the plugin's
# ``main.tf.json`` — `tofu` merges every ``.tf.json`` in the directory.
_LOCALSTACK_PROVIDER = {
    "provider": {
        "aws": {
            "region": "us-east-1",
            "access_key": "test",
            "secret_key": "test",
            "skip_credentials_validation": True,
            "skip_metadata_api_check": True,
            "skip_requesting_account_id": True,
            "s3_use_path_style": True,
            "endpoints": {"s3": _LOCALSTACK, "iam": _LOCALSTACK, "glue": _LOCALSTACK},
        }
    }
}


@pytest.mark.skipif(_SKIP, reason="needs `tofu` + LocalStack reachable on localhost:4566")
def test_apply_destroy_cycle_against_localstack(tmp_path):
    """contract → .tf.json → tofu init/plan/apply → real resource → destroy."""
    (tmp_path / "main.tf.json").write_text(build_module(get_iac_plugin("aws"), _S3_CONTRACT))
    (tmp_path / "provider.tf.json").write_text(json.dumps(_LOCALSTACK_PROVIDER))

    env = build_tofu_env()
    env.update(
        {
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
    )
    workdir = str(tmp_path)

    init = runner.tofu_init(workdir, backend=False, env=env)
    assert init.ok, init.stderr or init.stdout

    plan = runner.tofu_plan(workdir, env=env)
    assert plan.ok, plan.stderr or plan.stdout
    assert runner.change_summary(plan)["add"] >= 1

    try:
        applied = runner.tofu_apply(workdir, env=env)
        assert applied.ok, applied.stderr or applied.stdout
        assert runner.change_summary(applied)["add"] >= 1
    finally:
        destroyed = runner.tofu_destroy(workdir, env=env)
        assert destroyed.ok, destroyed.stderr or destroyed.stdout
