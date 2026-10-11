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

"""The Snowflake EXTERNAL VOLUME guard names both causes, and blames neither.

JRN-707-6: the volume is keyed by the contract id, so state cannot say which
expose it was created for. A contract applied only by the current release, with
a Snowflake-managed expose (which gets the volume) beside a Lakekeeper one,
leaves the same volume in state when the managed expose is removed. The guard
still blocks that apply (what it detects is unchanged), but its message said
"forge-cli no longer creates an EXTERNAL VOLUME ..." and listed the Lakekeeper
expose as the table the volume was held for. It now names both possible causes
and keeps the ``tofu state rm`` remediation.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

import pytest

from fluid_build.iac.catalog_moves import (
    CatalogMoveError,
    detect_catalog_moves,
    guard_catalog_moves,
)
from fluid_build.iac.providers.aws import AwsIacPlugin
from fluid_build.iac.providers.snowflake import SnowflakeIacPlugin

pytestmark = [pytest.mark.unit, pytest.mark.provider]

SCHEMA = [{"name": "order_id", "type": "string"}]
VOLUME = "snowflake_external_volume.analytics_lake_vol_FLUID_ANALYTICS_LAKE_VOL"
SF_TABLE = "snowflake_table.analytics_lake_ANALYTICS_SALES_ORDERS"
WORKDIR = "/w/snowflake/analytics.lake"


def _sf_expose(catalog: Optional[str], expose_id: str, table: str) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "database": "ANALYTICS",
        "schema": "SALES",
        "table": table,
        "warehouse": "s3://lake/warehouse",
        "iam_role_arn": "arn:aws:iam::123456789012:role/snowflake-lake",
    }
    if catalog is not None:
        location.update(catalog=catalog, uri="http://lakekeeper:8181/catalog")
    return {
        "exposeId": expose_id,
        "binding": {"platform": "snowflake", "format": "iceberg", "location": location},
        "contract": {"schema": copy.deepcopy(SCHEMA)},
    }


def _contract(*exposes: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": "analytics.lake", "name": "Lake", "exposes": list(exposes)}


#: After the change: the managed expose is gone, the Lakekeeper one stays.
_AFTER = _contract(_sf_expose("lakekeeper", "orders_iceberg", "ORDERS"))


def _blocked(contract: Dict[str, Any], state) -> CatalogMoveError:
    with pytest.raises(CatalogMoveError) as excinfo:
        guard_catalog_moves(SnowflakeIacPlugin(), contract, state, workdir=WORKDIR)
    return excinfo.value


def test_the_current_release_s_own_volume_is_still_blocked():
    """Detection is unchanged: the volume a removed managed expose used is
    flagged, as it was before the message changed."""
    plugin = SnowflakeIacPlugin()
    before = _contract(
        _sf_expose(None, "orders_managed", "ORDERS_MANAGED"),
        _sf_expose("lakekeeper", "orders_iceberg", "ORDERS"),
    )
    emitted = plugin.emit(before)["snowflake_external_volume"]
    assert {f"snowflake_external_volume.{key}" for key in emitted} == {VOLUME}

    assert detect_catalog_moves(plugin, _AFTER, [SF_TABLE, VOLUME]) == (VOLUME,)
    exc = _blocked(_AFTER, [SF_TABLE, VOLUME])
    assert exc.addresses == (VOLUME,)
    assert exc.remediation == (f"tofu -chdir={WORKDIR} state rm {VOLUME}",)


def test_the_message_does_not_say_an_earlier_release_created_it():
    message = str(_blocked(_AFTER, [SF_TABLE, VOLUME]))

    assert "no longer creates" not in message
    assert "an earlier dbt run" not in message
    assert "for Iceberg table(s) that now live in another catalog" not in message
    assert (
        "holds 1 Snowflake EXTERNAL VOLUME(s) that this contract's configuration no longer "
        "declares:\n\n  " + VOLUME
    ) in message


def test_the_message_names_both_causes_before_the_exposes():
    message = str(_blocked(_AFTER, [SF_TABLE, VOLUME]))

    causes = message.index("Possible causes:")
    upgrade = message.index("applied by a forge-cli release that gave an Iceberg table")
    removal = message.index("this change removed a Snowflake-managed Iceberg expose")
    recatalog = message.index("or moved one to another catalog")
    expose = message.index("  exposes[orders_iceberg]: location.catalog lakekeeper")
    assert "does not say which expose it was created for" in message
    assert causes < upgrade < removal < recatalog < expose


def test_the_remediation_is_kept():
    message = str(_blocked(_AFTER, [SF_TABLE, VOLUME]))

    assert f"  tofu -chdir={WORKDIR} state rm {VOLUME}" in message
    assert "the resources stay in Snowflake" in message
    assert "DROP EXTERNAL VOLUME" in message


def test_the_aws_message_still_attributes_the_glue_resources():
    """AWS has per-expose evidence (the Glue table), so its message is as before."""
    location = {"database": "sales", "table": "orders", "bucket": "lake", "path": "orders/"}
    location["catalog"] = "lakekeeper"
    contract = _contract(
        {
            "exposeId": "orders",
            "binding": {"platform": "aws", "format": "iceberg", "location": location},
            "contract": {"schema": copy.deepcopy(SCHEMA)},
        }
    )
    db = "aws_glue_catalog_database.analytics_lake_sales"
    table = "aws_glue_catalog_table.analytics_lake_sales_orders"
    with pytest.raises(CatalogMoveError) as excinfo:
        guard_catalog_moves(AwsIacPlugin(), contract, [db, table])

    assert str(excinfo.value).startswith(
        "iceberg catalog move blocked — this contract's OpenTofu state holds 2 Glue catalog "
        "resource(s) for Iceberg table(s) that now live in another catalog:\n\n"
        f"  exposes[orders]: location.catalog lakekeeper\n\n  {db}\n  {table}\n\n"
        "forge-cli no longer creates a Glue table"
    )
