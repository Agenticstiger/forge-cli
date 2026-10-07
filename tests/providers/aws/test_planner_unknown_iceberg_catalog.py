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

"""The native AWS planner refuses an Iceberg catalog it does not know.

An unknown ``location.catalog`` classifies as "not Glue", so ``catalog: glu``
used to plan two S3 buckets and no Glue database or table, with no word, while
the AWS IaC refused the same value (``unknown-iceberg-catalog``). Whether a
Glue table exists hangs on the answer and nothing guarantees ``fluid
validate`` ran first, so the planner refuses it as the IaC does.
"""

from __future__ import annotations

import pytest

from fluid_build.providers._iceberg_catalog import known_catalog_kinds
from fluid_build.providers.aws.plan.planner import plan_actions

pytestmark = [pytest.mark.unit, pytest.mark.provider]

ACCT = "123456789012"
REGION = "us-east-1"


def _contract(catalog, *, fmt="iceberg"):
    location = {"database": "sales", "table": "orders", "bucket": "lake", "region": REGION}
    if catalog is not None:
        location["catalog"] = catalog
    return {
        "id": "gold.orders",
        "name": "orders",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": "aws", "format": fmt, "location": location},
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def test_a_typo_is_refused_not_planned_without_glue():
    with pytest.raises(ValueError) as exc:
        plan_actions(_contract("glu"), ACCT, REGION)
    message = str(exc.value)
    # The field, the value, the cause, the consequence and the remedy.
    assert "exposes[orders]" in message
    assert "'glu' (location.catalog)" in message
    assert "does not know" in message
    assert "no Glue database or table" in message
    assert "Use one of: " + ", ".join(known_catalog_kinds()) + "." in message
    assert "remove location.catalog" in message


def test_the_corrected_spelling_plans_the_glue_table():
    ops = [a["op"] for a in plan_actions(_contract("glue"), ACCT, REGION)]
    assert "glue.ensure_database" in ops
    assert "glue.ensure_iceberg_table" in ops


@pytest.mark.parametrize("spelling", ["Lakekeeper", "ICEBERG_REST", "iceberg-rest", "Snowflake"])
def test_a_known_spelling_is_not_refused(spelling):
    ops = [a["op"] for a in plan_actions(_contract(spelling), ACCT, REGION)]
    assert "glue.ensure_iceberg_table" not in ops


def test_catalog_is_ignored_off_iceberg_as_the_iac_ignores_it():
    """``location.catalog`` means nothing to a plain Glue table format, so a
    stray value there is not refused (the IaC's refusal is format-gated too)."""
    ops = [a["op"] for a in plan_actions(_contract("glu", fmt="parquet"), ACCT, REGION)]
    assert "glue.ensure_database" in ops


def test_the_provider_plan_surfaces_the_remedy():
    """``AwsProvider.plan`` wraps the planner's error text and drops the rest,
    so the remedy has to travel in the message."""
    from fluid_build.providers.aws.provider import AwsProvider
    from fluid_build.providers.base import ProviderError

    provider = AwsProvider(account_id=ACCT, region=REGION)
    with pytest.raises(ProviderError, match=r"'glu'.*remove location\.catalog"):
        provider.plan(_contract("glu"))
