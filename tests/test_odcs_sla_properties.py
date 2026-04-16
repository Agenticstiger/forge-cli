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

from fluid_build.providers.odcs.odcs import OdcsProvider


def _find_sla_value(sla_properties, property_name):
    for item in sla_properties:
        if item.get("property") == property_name:
            return item.get("value")
    return None


def test_extract_sla_properties_from_expose_qos():
    provider = OdcsProvider()
    contract = {
        "id": "bronze.sales.orders",
        "metadata": {"name": "Orders", "status": "active"},
        "exposes": [
            {
                "id": "orders_port",
                "qos": {
                    "availability": "99.5%",
                    "freshnessSLO": "PT15M",
                    "labels": {"tier": "gold"},
                },
            }
        ],
    }

    sla_properties = provider._extract_sla_properties(contract)

    assert isinstance(sla_properties, list)
    assert _find_sla_value(sla_properties, "availability") == 0.995
    assert _find_sla_value(sla_properties, "interval") == "PT15M"
    assert _find_sla_value(sla_properties, "label:tier") == "gold"


def test_extract_sla_properties_returns_none_without_qos_or_metadata_sla():
    provider = OdcsProvider()
    contract = {
        "id": "bronze.sales.orders",
        "metadata": {"name": "Orders", "status": "active"},
        "exposes": [{"id": "orders_port"}],
    }

    assert provider._extract_sla_properties(contract) is None


def test_extract_sla_properties_multi_expose_prefers_first_seen_values():
    provider = OdcsProvider()
    contract = {
        "id": "bronze.sales.orders",
        "metadata": {"name": "Orders", "status": "active"},
        "exposes": [
            {"id": "orders_port_a", "qos": {"availability": "99%", "freshnessSLO": "PT30M"}},
            {"id": "orders_port_b", "qos": {"availability": "95%", "freshnessSLO": "PT1H"}},
        ],
    }

    sla_properties = provider._extract_sla_properties(contract)

    assert _find_sla_value(sla_properties, "availability") == 0.99
    assert _find_sla_value(sla_properties, "interval") == "PT30M"
