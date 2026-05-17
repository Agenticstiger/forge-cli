# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Integration: emitted .tf.json is accepted by the real ``tofu`` binary.

Skipped unless ``tofu`` is on PATH. ``tofu validate`` checks config
syntax and provider-schema correctness — it needs no cloud credentials
(only registry network access during ``tofu init`` to fetch providers).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from fluid_build.iac import IAC_PLUGINS, build_module

pytestmark = [pytest.mark.integration, pytest.mark.provider]

_TOFU = shutil.which("tofu")

# One representative contract per cloud. New plugins add an entry here.
_SAMPLE_CONTRACTS = {
    "aws": {
        "id": "demo.aws",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "database": "demo",
                        "table": "orders",
                        "bucket": "demo-fluid-lake",
                        "path": "orders/",
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
        ],
    },
    "gcp": {
        "id": "demo.gcp",
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "format": "bigquery_table",
                    "location": {"dataset": "demo", "table": "events"},
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
        ],
    },
    "snowflake": {
        "id": "demo.snowflake",
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "DEMO_DB", "schema": "PUBLIC", "table": "EVENTS"},
                },
                "contract": {
                    "schema": [
                        {"name": "ID", "type": "integer", "required": True},
                        {"name": "MSG", "type": "string"},
                    ]
                },
            }
        ],
    },
}


@pytest.mark.skipif(_TOFU is None, reason="tofu binary not installed")
@pytest.mark.parametrize("cloud", sorted(_SAMPLE_CONTRACTS))
def test_emitted_tfjson_passes_tofu_validate(cloud, tmp_path):
    plugin = IAC_PLUGINS.get(cloud)
    if plugin is None:
        pytest.skip(f"no IaC plugin registered for {cloud}")

    (tmp_path / "main.tf.json").write_text(build_module(plugin, _SAMPLE_CONTRACTS[cloud]))

    init = subprocess.run(
        [_TOFU, "init", "-backend=false", "-input=false", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, init.stderr or init.stdout

    validate = subprocess.run(
        [_TOFU, "validate", "-no-color"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert validate.returncode == 0, validate.stderr or validate.stdout
