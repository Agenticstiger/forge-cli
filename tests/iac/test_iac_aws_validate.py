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

"""Integration: emitted AWS ``.tf.json`` is accepted by real ``tofu``.

The contract-shape gate for the AWS / Iceberg / Redshift mesh path. Each
representative contract — Iceberg-on-Glue, Redshift Serverless, the
external-schema bridge, and the full data-mesh dual-port scenario — must
compile to a module the real provider schemas (``hashicorp/aws ~> 5.0``
+ ``hashicorp/null ~> 3.0``) accept.

Needs ``tofu`` on PATH and registry network access (``tofu init`` downloads
the provider). No AWS credentials required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.iac import build_module, runner

pytestmark = [pytest.mark.integration, pytest.mark.provider, pytest.mark.aws]


# ---------------------------------------------------------------------------
# Representative contracts — one per scenario the AWS plugin must accept
# ---------------------------------------------------------------------------

_ICEBERG_CONTRACT: Dict[str, Any] = {
    "id": "silver.mesh.events",
    "name": "Mesh Silver Events",
    "exposes": [
        {
            "exposeId": "events",
            "binding": {
                "platform": "aws",
                "format": "iceberg",  # → Glue Catalog table with table_type=ICEBERG
                "location": {
                    "database": "silver_events",
                    "table": "events",
                    "bucket": "fluid-mesh-lake",
                    "path": "silver/events/",
                },
            },
            "contract": {
                "schema": [
                    {"name": "event_id", "type": "string", "required": True},
                    {"name": "occurred_at", "type": "timestamp"},
                    {"name": "amount", "type": "decimal(12,2)"},
                ]
            },
        }
    ],
}

_REDSHIFT_SERVERLESS_CONTRACT: Dict[str, Any] = {
    "id": "platform.redshift.mesh",
    "name": "Mesh Redshift Compute",
    "exposes": [
        {
            "exposeId": "compute",
            "binding": {
                "platform": "aws",
                "format": "redshift_serverless",
                "location": {
                    "namespace": "fluid_mesh",
                    "workgroup": "fluid_mesh_wg",
                    "database": "fluid",
                    "base_capacity": 8,
                    "iam_role_arn": "arn:aws:iam::123456789012:role/MeshSpectrum",
                },
            },
        }
    ],
}

_REDSHIFT_EXTERNAL_SCHEMA_CONTRACT: Dict[str, Any] = {
    "id": "silver.redshift.bridge",
    "name": "Silver Events via Redshift",
    "exposes": [
        {
            "exposeId": "silver_via_redshift",
            "binding": {
                "platform": "aws",
                "format": "redshift_external_schema",
                "location": {
                    "workgroup": "fluid_mesh_wg",  # external — pre-existing
                    "database": "fluid",
                    "external_schema": "ext_silver_events",
                    "glue_database": "silver_events",  # the mesh interface
                    "iam_role_arn": "arn:aws:iam::123456789012:role/MeshSpectrum",
                    "region": "us-east-1",
                },
            },
        }
    ],
}

# The data-mesh dual-port scenario: a single contract emits the Iceberg
# table in the Glue catalog (Athena reads natively), provisions a Redshift
# Serverless workgroup, and registers an external schema in the workgroup
# pointing at the same Glue database — both query engines read one physical
# Iceberg table over S3.
_MESH_DUAL_PORT_CONTRACT: Dict[str, Any] = {
    "id": "silver.mesh.subscriber360",
    "name": "Mesh Silver Subscriber 360",
    "exposes": [
        {
            "exposeId": "subscriber360_iceberg",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "silver_subscriber360",
                    "table": "subscriber360",
                    "bucket": "fluid-mesh-lake",
                    "path": "silver/subscriber360/",
                },
            },
            "contract": {
                "schema": [
                    {"name": "subscriber_id", "type": "string", "required": True},
                    {"name": "name", "type": "string"},
                    {"name": "lifetime_value", "type": "decimal(15,2)"},
                    {"name": "as_of", "type": "timestamp"},
                ]
            },
        },
        {
            "exposeId": "subscriber360_compute",
            "binding": {
                "platform": "aws",
                "format": "redshift_serverless",
                "location": {
                    "namespace": "fluid_mesh",
                    "workgroup": "fluid_mesh_wg",
                    "database": "fluid",
                    "iam_role_arn": "arn:aws:iam::123456789012:role/MeshSpectrum",
                },
            },
        },
        {
            "exposeId": "subscriber360_via_redshift",
            "binding": {
                "platform": "aws",
                "format": "redshift_external_schema",
                "location": {
                    "workgroup": "fluid_mesh_wg",
                    "database": "fluid",
                    "external_schema": "ext_subscriber360",
                    # Same Glue database the Iceberg table above is cataloged
                    # in — the mesh interface for both query engines.
                    "glue_database": "silver_subscriber360",
                    "iam_role_arn": "arn:aws:iam::123456789012:role/MeshSpectrum",
                },
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plugin():
    from fluid_build.iac import get_iac_plugin

    return get_iac_plugin("aws")


def _init_and_validate(workdir: Path, env: Dict[str, str]) -> None:
    """``tofu init -backend=false`` then ``tofu validate`` — assert both pass."""
    init = runner.tofu_init(str(workdir), backend=False, env=env)
    assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"
    result = runner.tofu_validate(str(workdir), env=env)
    assert result.ok, f"tofu validate failed:\n{result.stderr or result.stdout}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_iceberg_on_glue_validates(tofu_binary, tofu_env, tmp_path):
    """The Iceberg-on-Glue path emits a module the real Glue provider
    schema accepts — including the ``table_type=ICEBERG`` parameter."""
    text = build_module(_plugin(), _ICEBERG_CONTRACT)
    (tmp_path / "main.tf.json").write_text(text, encoding="utf-8")
    _init_and_validate(tmp_path, tofu_env)

    # The module must carry the Iceberg hint — guards against a silent regression.
    table = next(iter(json.loads(text)["resource"]["aws_glue_catalog_table"].values()))
    assert table["parameters"]["table_type"] == "ICEBERG"


def test_redshift_serverless_contract_validates(tofu_binary, tofu_env, tmp_path):
    """Namespace + workgroup compile to a module the Redshift Serverless
    provider schema accepts."""
    text = build_module(_plugin(), _REDSHIFT_SERVERLESS_CONTRACT)
    (tmp_path / "main.tf.json").write_text(text, encoding="utf-8")
    _init_and_validate(tmp_path, tofu_env)

    resources = json.loads(text)["resource"]
    assert "aws_redshiftserverless_namespace" in resources
    assert "aws_redshiftserverless_workgroup" in resources


def test_redshift_external_schema_contract_validates(tofu_binary, tofu_env, tmp_path):
    """The ``null_resource`` bridge compiles to a module the ``hashicorp/null``
    provider accepts. The ``CREATE EXTERNAL SCHEMA`` SQL and the workgroup /
    database are passed via the local-exec ``environment`` (data, never spliced
    into the shell command) — see the injection fix in
    ``_emit_redshift_external_schema``; the ``command`` itself is a static
    template referencing ``"$FLUID_REDSHIFT_*"`` env vars."""
    text = build_module(_plugin(), _REDSHIFT_EXTERNAL_SCHEMA_CONTRACT)
    (tmp_path / "main.tf.json").write_text(text, encoding="utf-8")
    _init_and_validate(tmp_path, tofu_env)

    resources = json.loads(text)["resource"]
    assert "null_resource" in resources
    local_exec = next(iter(resources["null_resource"].values()))["provisioner"][0]["local-exec"]
    sql = local_exec["environment"]["FLUID_REDSHIFT_SQL"]
    assert "CREATE EXTERNAL SCHEMA" in sql
    assert "FROM DATA CATALOG" in sql


def test_mesh_dual_port_contract_validates(tofu_binary, tofu_env, tmp_path):
    """The dual-port mesh scenario — Iceberg-in-Glue + Redshift Serverless +
    external schema bridge over the same Glue database — compiles to a single
    valid module. The external schema's ``depends_on`` must reference real
    addresses (workgroup + Glue database both emitted in the same module)."""
    text = build_module(_plugin(), _MESH_DUAL_PORT_CONTRACT)
    (tmp_path / "main.tf.json").write_text(text, encoding="utf-8")
    _init_and_validate(tmp_path, tofu_env)

    resources = json.loads(text)["resource"]
    # Every resource type the mesh dual-port architecture needs.
    for required in (
        "aws_s3_bucket",
        "aws_glue_catalog_database",
        "aws_glue_catalog_table",
        "aws_redshiftserverless_namespace",
        "aws_redshiftserverless_workgroup",
        "null_resource",
    ):
        assert required in resources, f"{required} missing from the dual-port module"

    # The null_resource bridge depends on both the workgroup AND the Glue
    # database — the post-emit dep wiring resolves them to real addresses,
    # otherwise `tofu validate` would have failed "undeclared reference".
    bridge = next(iter(resources["null_resource"].values()))
    deps = bridge.get("depends_on") or []
    assert any(d.startswith("aws_redshiftserverless_workgroup.") for d in deps)
    assert any(d.startswith("aws_glue_catalog_database.") for d in deps)
