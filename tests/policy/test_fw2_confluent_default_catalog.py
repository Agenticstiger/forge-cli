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

"""CON-5: a Confluent Tableflow expose with no catalog reads as Glue everywhere.

The Tableflow IaC publishes such a table to AWS Glue, while the kind table
used to call it ``rest``: policy compile dropped every grant with a false
"cataloged in 'rest'" warning, and dbt-snowflake lost the Glue-linked
database type. Absent and ``catalog: glue`` must now compile the same.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest
import yaml

from fluid_build.engines.dbt.catalogs_yml import generate_catalogs_yml
from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.iac.providers.confluent import validate_confluent_binding
from fluid_build.policy.compiler import compile_policy

pytestmark = [pytest.mark.unit]


def _contract(catalog: Optional[str]) -> Dict[str, Any]:
    loc: Dict[str, Any] = {
        "environment_id": "env-123",
        "kafka_cluster_id": "lkc-123",
        "topic": "orders",
        "database": "sales_glue",
        "bucket": "acme-tableflow",
        "confluent_role_arn": "arn:aws:iam::123456789012:role/tableflow",
        "region": "eu-west-1",
    }
    if catalog is not None:
        loc["catalog"] = catalog
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.tableflow",
        "name": "Tableflow orders",
        "metadata": {"layer": "Bronze"},
        "accessPolicy": {
            "grants": [{"principal": "group:analysts@example.com", "permissions": ["read"]}]
        },
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": "confluent", "format": "iceberg", "location": loc},
                "contract": {"schema": [{"name": "order_id", "type": "integer"}]},
            }
        ],
    }


def test_policy_compile_grants_the_glue_table_when_catalog_is_absent():
    bindings, warnings = compile_policy(_contract(None))
    assert not any("cataloged in" in w for w in warnings), warnings
    assert [(b["provider"], b["resource_type"], b["resource_id"]) for b in bindings] == [
        ("aws", "s3.bucket", "acme-tableflow"),
        ("aws", "glue.table", "sales_glue"),
    ]


def test_policy_compile_is_the_same_for_absent_and_explicit_glue():
    assert compile_policy(_contract(None)) == compile_policy(_contract("glue"))


def test_dbt_snowflake_links_the_glue_database_when_catalog_is_absent():
    build = {"engine": "dbt", "execution": {"runtime": {"platform": "snowflake"}}}
    absent = generate_catalogs_yml(_contract(None), build)
    assert absent == generate_catalogs_yml(_contract("glue"), build)
    integration = yaml.safe_load(absent)["catalogs"][0]["write_integrations"][0]
    assert integration["adapter_properties"]["catalog_linked_database_type"] == "glue"


def test_tableflow_iac_still_publishes_an_absent_catalog_to_glue():
    plugin = get_iac_plugin("confluent")
    absent = json.loads(build_module(plugin, _contract(None)))["resource"]
    glue = next(iter(absent["confluent_catalog_integration"].values()))["aws_glue"]
    assert glue["custom_database"] == "sales_glue"
    assert build_module(plugin, _contract(None)) == build_module(plugin, _contract("glue"))
    assert validate_confluent_binding(_contract(None)) == validate_confluent_binding(
        _contract("glue")
    )


def test_a_non_glue_confluent_catalog_is_still_refused():
    errors, _ = validate_confluent_binding(_contract("lakekeeper"))
    assert errors and "lakekeeper" in errors[0]
