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

"""``fluid test`` on AWS reads Glue, so it must not look for a table that
``fluid apply`` deliberately keeps out of Glue.

Before the catalog-kind table, AWS IaC created a (metadata-less) Glue table
for every Iceberg expose, so this lookup "passed" against the phantom. Now a
Lakekeeper or REST-catalog table has no Glue twin, and the lookup would fail
every such contract with "does not exist in AWS Glue catalog".
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fluid_build.cli.contract_validation import ContractValidator, ValidationReport

pytestmark = pytest.mark.unit


def _validator() -> ContractValidator:
    v = ContractValidator(Path("c.yaml"), provider_name="aws", use_cache=False, check_data=True)
    v.report = ValidationReport(
        contract_path="c.yaml",
        contract_id="sales.orders",
        contract_version="1",
        validation_time=datetime(2026, 10, 1),
        duration=0.0,
    )
    v.validation_provider = MagicMock()
    v.validation_provider.get_resource_schema.return_value = None
    v.validation_provider.validate_resource.return_value = MagicMock(issues=[], success=True)
    v.cache = None
    v.history = None
    return v


def _expose(fmt: str, catalog=None):
    location = {"database": "sales", "table": "orders", "bucket": "lake"}
    if catalog:
        location["catalog"] = catalog
    return {
        "exposeId": "orders",
        "binding": {"platform": "aws", "format": fmt, "location": location},
    }


@pytest.mark.parametrize("catalog", ["lakekeeper", "Lakekeeper", "rest", "iceberg_rest", "polaris"])
def test_an_iceberg_table_in_another_catalog_is_not_looked_up_in_glue(catalog):
    v = _validator()
    v._validate_against_actual_resource(_expose("iceberg", catalog), "exposes[0]")
    v.validation_provider.get_resource_schema.assert_not_called()
    assert not v.report.get_errors()
    info = [i for i in v.report.issues if i.severity == "info"]
    assert len(info) == 1
    assert "not AWS Glue" in info[0].message
    assert catalog.lower().replace("_", "-").replace("iceberg-rest", "rest") in info[0].message


@pytest.mark.parametrize(
    "fmt,catalog",
    [("iceberg", None), ("iceberg", "glue"), ("parquet", None), ("parquet", "lakekeeper")],
)
def test_glue_tables_are_still_checked(fmt, catalog):
    v = _validator()
    v._validate_against_actual_resource(_expose(fmt, catalog), "exposes[0]")
    v.validation_provider.get_resource_schema.assert_called_once()
