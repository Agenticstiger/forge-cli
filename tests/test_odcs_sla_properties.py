# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
