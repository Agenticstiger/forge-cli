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

"""A Kafka binding says where its brokers are and what its messages are.

Two defects in the ODCS ``servers[]`` export of a Kafka expose:

* The only schema-valid way to name the bootstrap servers was
  ``binding.location.account``, a field named for something else, and with
  neither it the exported server was the placeholder
  ``${KAFKA_BOOTSTRAP_SERVERS}``. 0.7.6 declares ``binding.location.host``.
* The server ``format`` came out as ``kafka_topic`` because the mapper lifted
  ``binding.format`` onto it. ODCS v3.1.0 defines a Kafka server's ``format``
  as the format of the *messages* (json, avro, protobuf, xml), so a consumer
  could not tell Avro from JSON. 0.7.6 declares ``binding.serialization``.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from fluid_build.loader import load_with_overlay
from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.mappers.extras import FLUID_EXTRAS_PROPERTY
from fluid_build.providers.odcs.mappers.servers import (
    _expect_to_server,
    _server_details_from_location,
)
from fluid_build.providers.odcs.validation import collect_errors, load_schema
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

PLACEHOLDER = "${KAFKA_BOOTSTRAP_SERVERS}"
EXPOSE_ID = "orders_topic"


def _contract(
    *,
    location: Optional[Dict[str, Any]] = None,
    serialization: Optional[Dict[str, Any]] = None,
    binding_format: str = "kafka_topic",
) -> Dict[str, Any]:
    binding: Dict[str, Any] = {
        "platform": "kafka",
        "format": binding_format,
        "location": {"topic": "orders"} if location is None else location,
    }
    if serialization is not None:
        binding["serialization"] = serialization
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.orders_stream",
        "name": "Orders stream",
        "domain": "sales",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "owner": {"team": "sales-data", "email": "sales-data@example.com"},
        },
        "exposes": [
            {
                "exposeId": EXPOSE_ID,
                "kind": "stream",
                "binding": binding,
                "contract": {"schema": [{"name": "order_id", "type": "string"}]},
            }
        ],
    }


def _export(contract: Dict[str, Any]) -> Dict[str, Any]:
    return OdcsProvider().render(copy.deepcopy(contract))


def _kafka_server(odcs: Dict[str, Any]) -> Dict[str, Any]:
    servers = [s for s in odcs["servers"] if s["type"] == "kafka"]
    assert len(servers) == 1, odcs["servers"]
    return servers[0]


class TestFixtureIsAValidContract:
    """Every case below is built from this fixture, so pin that it is real."""

    def test_the_fixture_with_all_three_fields_validates_as_0_7_6(self) -> None:
        contract = _contract(
            location={"host": "broker-1:9092", "topic": "orders"},
            serialization={"value": "avro-confluent", "key": "json"},
        )
        result = FluidSchemaManager().validate_contract(contract, "0.7.6", offline_only=True)
        assert result.is_valid, result.errors


class TestHost:
    def test_host_is_the_exported_bootstrap_server_and_the_placeholder_is_gone(self) -> None:
        odcs = _export(_contract(location={"host": "broker-1:9092,broker-2:9092", "topic": "o"}))

        server = _kafka_server(odcs)
        assert server["host"] == "broker-1:9092,broker-2:9092"
        assert PLACEHOLDER not in json.dumps(odcs)

    def test_host_set_by_an_overlay_reaches_the_exported_server(self, tmp_path: Path) -> None:
        # The case the field exists for: the base contract cannot know the
        # brokers, each environment's overlay names its own.
        base = _contract(serialization={"value": "avro"})
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
        overlays = tmp_path / "overlays"
        overlays.mkdir()
        (overlays / "prod.yaml").write_text(
            yaml.safe_dump(
                {
                    "exposes": [
                        {
                            "exposeId": EXPOSE_ID,
                            "binding": {"location": {"host": "broker.prod.internal:9093"}},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        path = tmp_path / "contract.fluid.yaml"

        prod = load_with_overlay(path, "prod", resolve_refs=False)
        assert prod["exposes"][0]["binding"]["location"]["topic"] == "orders"
        prod_odcs = _export(prod)
        assert _kafka_server(prod_odcs)["host"] == "broker.prod.internal:9093"
        assert PLACEHOLDER not in json.dumps(prod_odcs["servers"])

        base_only = load_with_overlay(path, None, resolve_refs=False)
        assert _kafka_server(_export(base_only))["host"] == PLACEHOLDER

    def test_account_still_names_the_brokers_for_contracts_written_before_host(self) -> None:
        odcs = _export(_contract(location={"account": "broker-legacy:9092", "topic": "orders"}))

        assert _kafka_server(odcs)["host"] == "broker-legacy:9092"
        assert PLACEHOLDER not in json.dumps(odcs["servers"])

    def test_host_wins_when_a_contract_sets_both(self) -> None:
        odcs = _export(_contract(location={"host": "new:9092", "account": "old:9092"}))

        assert _kafka_server(odcs)["host"] == "new:9092"

    def test_neither_host_nor_account_keeps_the_documented_placeholder(self) -> None:
        # ODCS requires a Kafka server host and an empty one would fail
        # validation, so the placeholder is the documented stand-in until a
        # deploy-time value (or an overlay) replaces it.
        odcs = _export(_contract(location={"topic": "orders"}))

        assert _kafka_server(odcs)["host"] == PLACEHOLDER


class TestSerializationValueIsTheMessageFormat:
    @pytest.mark.parametrize(
        ("declared", "odcs_format"),
        [
            ("json", "json"),
            ("avro", "avro"),
            ("protobuf", "protobuf"),
            ("xml", "xml"),
            # The registry-carried values are those same wire formats, which
            # is what the ODCS field means.
            ("avro-confluent", "avro"),
            ("json-confluent", "json"),
        ],
    )
    def test_value_maps_to_the_format_odcs_documents(self, declared: str, odcs_format: str) -> None:
        odcs = _export(
            _contract(
                location={"host": "broker-1:9092", "topic": "orders"},
                serialization={"value": declared},
            )
        )

        server = _kafka_server(odcs)
        assert server["format"] == odcs_format
        assert server["format"] != "kafka_topic"

    def test_a_declared_value_replaces_a_non_default_binding_format_too(self) -> None:
        odcs = _export(
            _contract(
                location={"host": "b:9092"},
                serialization={"value": "protobuf"},
                binding_format="json",
            )
        )

        assert _kafka_server(odcs)["format"] == "protobuf"

    def test_key_serialization_is_not_exported_to_the_server(self) -> None:
        # ODCS's Kafka server has one ``format`` and it means the message
        # value. Putting the key's format there, or anywhere on the server,
        # would make a field say something it does not.
        odcs = _export(
            _contract(
                location={"host": "b:9092"},
                serialization={"value": "avro", "key": "json"},
            )
        )

        server = _kafka_server(odcs)
        assert server["format"] == "avro"
        assert set(server) == {"server", "type", "host", "format"}

    def test_a_key_alone_says_nothing_about_the_messages(self) -> None:
        odcs = _export(_contract(location={"host": "b:9092"}, serialization={"key": "avro"}))

        assert _kafka_server(odcs)["format"] == "kafka_topic"

    def test_an_undocumented_value_is_neither_crashed_on_nor_repeated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The exporter does not run ``fluid validate`` first, so a value from
        # a later schema (or a typo) must not become an ODCS format.
        with caplog.at_level(logging.WARNING, logger="fluid_build.providers.odcs.mappers.servers"):
            odcs = _export(
                _contract(location={"host": "b:9092"}, serialization={"value": "thrift"})
            )

        assert _kafka_server(odcs)["format"] == "kafka_topic"
        assert "thrift" in caplog.text
        assert "avro-confluent" in caplog.text

    def test_a_non_string_value_does_not_crash_the_mapper(self) -> None:
        details = _server_details_from_location(
            {"host": "b:9092", "format": "kafka_topic", "serialization": {"value": ["avro"]}},
            "kafka",
        )

        assert details == {"host": "b:9092", "format": "kafka_topic"}

    def test_expects_get_the_same_treatment_as_exposes(self) -> None:
        server = _expect_to_server(
            {
                "id": "upstream_orders",
                "binding": {
                    "platform": "kafka",
                    "format": "kafka_topic",
                    "location": {"host": "up:9092"},
                    "serialization": {"value": "json-confluent"},
                },
            }
        )

        assert server == {
            "server": "upstream_orders",
            "type": "kafka",
            "host": "up:9092",
            "format": "json",
        }


class TestTheRegistryFactIsKept:
    def test_confluent_framing_and_the_key_format_survive_in_fluid_extras(self) -> None:
        # ODCS has no Kafka server field for "this goes through a schema
        # registry", and the server ``format`` collapses avro-confluent to
        # avro. The binding rides along verbatim in the existing ``fluidExtras``
        # custom property, so the fact is retained, not dropped.
        odcs = _export(
            _contract(
                location={"host": "b:9092", "topic": "orders"},
                serialization={"value": "avro-confluent", "key": "json"},
            )
        )

        extras = next(
            p["value"] for p in odcs["customProperties"] if p["property"] == FLUID_EXTRAS_PROPERTY
        )
        assert extras["exposes"][EXPOSE_ID]["binding"]["serialization"] == {
            "value": "avro-confluent",
            "key": "json",
        }


class TestExportedServerIsValidOdcs:
    @pytest.fixture(scope="class")
    def odcs_schema(self) -> Dict[str, Any]:
        pytest.importorskip("jsonschema")
        schema = load_schema()
        assert schema is not None, "the vendored ODCS v3.1.0 schema must be present"
        return schema

    @pytest.mark.parametrize(
        "serialization",
        [
            None,
            {"value": "json"},
            {"value": "avro", "key": "json"},
            {"value": "avro-confluent"},
            {"value": "json-confluent"},
        ],
    )
    def test_the_exported_document_validates_against_odcs_3_1_0(
        self, odcs_schema: Dict[str, Any], serialization: Optional[Dict[str, Any]]
    ) -> None:
        odcs = _export(
            _contract(
                location={"host": "broker-1:9092", "topic": "orders"}, serialization=serialization
            )
        )

        assert collect_errors(odcs, odcs_schema) == []

    def test_the_validation_is_not_vacuous(self, odcs_schema: Dict[str, Any]) -> None:
        # A Kafka server without a host is exactly what ODCS rejects; if this
        # stopped failing, the checks above would prove nothing.
        odcs = _export(_contract(location={"host": "b:9092"}, serialization={"value": "avro"}))
        broken = copy.deepcopy(odcs)
        del _kafka_server(broken)["host"]

        errors = collect_errors(broken, odcs_schema)

        assert any("host" in e["message"] for e in errors), errors


class TestWithoutSerializationTheExportIsUnchanged:
    """A contract that has not adopted ``binding.serialization`` exports as before."""

    def test_legacy_kafka_topic_binding_exports_byte_for_byte_as_it_did(self) -> None:
        odcs = _export(_contract(location={"host": "broker-1:9092", "topic": "orders"}))

        assert _kafka_server(odcs) == {
            "server": EXPOSE_ID,
            "type": "kafka",
            "host": "broker-1:9092",
            "format": "kafka_topic",
        }

    def test_legacy_binding_with_no_brokers_exports_the_placeholder_and_the_token(self) -> None:
        odcs = _export(_contract(location={"topic": "orders"}))

        assert _kafka_server(odcs) == {
            "server": EXPOSE_ID,
            "type": "kafka",
            "host": PLACEHOLDER,
            "format": "kafka_topic",
        }

    def test_a_binding_format_that_is_already_a_message_format_is_untouched(self) -> None:
        odcs = _export(_contract(location={"host": "b:9092"}, binding_format="json"))

        assert _kafka_server(odcs)["format"] == "json"

    def test_a_location_with_no_format_at_all_still_defaults_to_the_odcs_default(self) -> None:
        assert _server_details_from_location({"host": "b:9092"}, "kafka") == {
            "host": "b:9092",
            "format": "json",
        }

    def test_the_new_fields_leave_no_trace_when_unused(self) -> None:
        with_fields = _export(
            _contract(
                location={"host": "b:9092", "topic": "orders"}, serialization={"value": "json"}
            )
        )
        without = _export(_contract(location={"account": "b:9092", "topic": "orders"}))

        assert _kafka_server(with_fields)["host"] == _kafka_server(without)["host"]
        assert set(_kafka_server(with_fields)) == set(_kafka_server(without))


class TestSerializationStaysOffOtherServerTypes:
    def test_a_custom_server_does_not_pick_up_serialization(self) -> None:
        # ``serialization`` is lifted next to ``format`` so the Kafka branch can
        # read it; the catch-all branch copies by whitelist and must not echo it.
        odcs = _export(
            {
                **_contract(location={"host": "h:1"}),
                "exposes": [
                    {
                        "exposeId": "other_port",
                        "kind": "stream",
                        "binding": {
                            "platform": "other",
                            "format": "other",
                            "location": {"host": "h:1"},
                            "serialization": {"value": "avro"},
                        },
                        "contract": {"schema": [{"name": "id", "type": "string"}]},
                    }
                ],
            }
        )

        server = next(s for s in odcs["servers"] if s["server"] == "other_port")
        assert "serialization" not in server
        assert server["host"] == "h:1"
