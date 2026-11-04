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

"""ODCS export names where each output column of a stream mapping is read from."""

from __future__ import annotations

from fluid_build.providers.odcs import OdcsProvider


def col(name, type_, **kw):
    return {"name": name, "type": type_, **kw}


EVENT_ID = col("event_id", "string", required=True)
EVENT_TIME = col("event_time", "timestamp_ltz(3)")


def _props(contract):
    return contract["builds"][0]["properties"]


def _output(contract, columns, mapping=None):
    contract["exposes"][0]["contract"]["schema"] = columns
    if mapping is not None:
        _props(contract)["mapping"] = mapping
    return contract


def test_odcs_export_carries_each_columns_source(contract):
    _output(
        contract,
        [
            EVENT_ID,
            col("country", "string"),
            col("customer", "struct", fields=[col("id", "string")]),
            col("sku", "string"),
            EVENT_TIME,
        ],
        {
            "explode": {"path": "items", "as": "item"},
            "columns": [
                {"name": "country", "from": "customer.address.country"},
                {"name": "customer.id", "from": "customer.id"},
                {"name": "sku", "from": "item.sku"},
            ],
        },
    )
    provider = OdcsProvider()
    ((expose_id, odcs),) = provider.render_all_ports(contract)
    (schema,) = odcs["schema"]
    assert schema["dataGranularityDescription"] == (
        "One row per element of items in each record of topic orders"
    )
    lineage = {
        p["name"]: (p.get("transformSourceObjects"), p.get("transformLogic"))
        for p in schema["properties"]
    }
    assert lineage["event_id"] == (["orders"], "event_id")
    assert lineage["country"] == (["orders"], "customer.address.country")
    assert lineage["sku"] == (["orders"], "item.sku")
    assert lineage["customer"] == (None, None)  # built field by field: its field says
    (customer_id,) = next(p for p in schema["properties"] if p["name"] == "customer")["properties"]
    assert customer_id["transformLogic"] == "customer.id"
    assert provider.roundtrip_check(odcs)["equal"]


def test_contract_sql_has_no_lineage_to_report(contract):
    _props(contract)["sql"] = "SELECT * FROM source_events"
    ((_, odcs),) = OdcsProvider().render_all_ports(contract)
    assert not any("transformLogic" in p for p in odcs["schema"][0]["properties"])
