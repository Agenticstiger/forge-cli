# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ODCS exporter writes v3.1.0 unless asked for v3.2.0, and v3.2.0 has an Iceberg server.

Bitol's ODCS v3.2.0 adds server types (``iceberg`` among them); every document a v3.2.0
validator takes is a v3.1.0 one too, but not the other way round: a v3.1.0 validator
refuses ``apiVersion: v3.2.0`` and a type outside its closed list. So the version is the
caller's choice, ``v3.1.0`` stays the default (the Data Mesh Manager publish, the artifact
fan-out and ``fluid generate standard`` write it), and the document an export writes is
held to the schema of the version it declares.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Optional

import pytest
import yaml

from fluid_build.providers.base import ProviderError
from fluid_build.providers.odcs.mappers.servers import _without_userinfo
from fluid_build.providers.odcs.provider import OdcsProvider
from fluid_build.providers.odcs.validation import collect_errors, load_schema, load_schema_for
from fluid_build.providers.odcs.versions import (
    DEFAULT_VERSION,
    SUPPORTED_VERSIONS,
    OdcsVersionError,
    at_least,
    normalize_version,
    schema_filename,
    version_of,
)
from tests.test_schema_076_preview_additions import _base_contract

pytestmark = pytest.mark.unit

V31, V32 = "v3.1.0", "v3.2.0"


def _expose(binding: Dict[str, Any], expose_id: str = "orders") -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": binding,
        "contract": {"schema": [{"name": "id", "type": "string", "required": True}]},
    }


def _contract(*exposes: Dict[str, Any], expects: Optional[list] = None) -> Dict[str, Any]:
    contract = _base_contract("0.7.7")
    contract["exposes"] = list(exposes)
    if expects:
        contract["expects"] = expects
    return contract


GLUE_ICEBERG = {
    "platform": "aws",
    "format": "iceberg",
    "location": {
        "database": "sales",
        "table": "orders",
        "bucket": "acme-lake",
        "path": "gold/orders/",
        "region": "eu-north-1",
    },
}


def _rest_iceberg(kind: str, **location: Any) -> Dict[str, Any]:
    return {
        "platform": "gcp",
        "format": "iceberg",
        "location": {"catalog": kind, "dataset": "sales", "table": "orders", **location},
    }


def _servers(contract: Dict[str, Any], version: Optional[str]) -> list:
    return OdcsProvider(version=version).render(copy.deepcopy(contract))["servers"]


# ── The versions ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, V31),
        ("", V31),
        ("3.1.0", V31),
        ("v3.1.0", V31),
        ("V3.1.0", V31),
        ("3.2.0", V32),
        ("v3.2.0", V32),
        ("  3.2.0 ", V32),
    ],
)
def test_a_version_is_spelled_with_or_without_the_v(value, expected):
    assert normalize_version(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "3.3.0",
        "3.0.2",
        "2.2.2",
        "3.2",
        "3",
        "latest",
        "v3.2.0-rc1",
        "3.2.0.1",
        "../3.2.0",
        "x" * 90,
    ],
)
def test_a_version_the_exporter_does_not_write_names_the_ones_it_does(value):
    with pytest.raises(OdcsVersionError) as raised:
        normalize_version(value)

    message = str(raised.value)
    assert "supported: 3.1.0, 3.2.0" in message and "the default is 3.1.0" in message
    assert len(message) < 200  # a hostile value is not echoed whole
    assert isinstance(raised.value, ProviderError)


def test_the_default_is_v3_1_0_and_both_versions_have_a_vendored_schema():
    assert DEFAULT_VERSION == V31
    assert SUPPORTED_VERSIONS == (V31, V32)
    for version in SUPPORTED_VERSIONS:
        schema = load_schema(version)
        assert schema is not None, schema_filename(version)
        assert schema["properties"]["apiVersion"]["default"] == version
        assert schema["properties"]["apiVersion"]["enum"][0] == version


def test_the_version_a_document_declares():
    assert version_of({"apiVersion": "v3.2.0"}) == V32
    assert version_of({"apiVersion": "v3.1.0"}) == V31
    assert version_of({"apiVersion": " v3.2.0 "}) == V32
    for document in (
        {},
        {"apiVersion": "v3.0.2"},
        {"apiVersion": "v2.2.2"},
        {"apiVersion": 3.2},
        None,
    ):
        assert version_of(document) is None


def test_at_least():
    assert at_least(V32, V31) and at_least(V32, V32) and at_least(V31, V31)
    assert not at_least(V31, V32)


# ── The provider ────────────────────────────────────────────────────────


def test_an_export_is_v3_1_0_unless_asked_for_another():
    contract = _contract(_expose(GLUE_ICEBERG))

    default = OdcsProvider()
    assert default.odcs_version == V31
    assert default.schema["properties"]["apiVersion"]["default"] == V31
    assert default.render(copy.deepcopy(contract))["apiVersion"] == V31
    assert OdcsProvider(version="3.1.0").render(copy.deepcopy(contract))["apiVersion"] == V31


def test_a_provider_asked_for_v3_2_0_writes_it_and_loads_its_schema():
    provider = OdcsProvider(version="3.2.0")

    assert provider.odcs_version == V32
    assert provider.schema["properties"]["apiVersion"]["default"] == V32
    assert provider.render(_contract(_expose(GLUE_ICEBERG)))["apiVersion"] == V32


def test_one_call_can_ask_for_another_version_than_its_provider():
    contract = _contract(_expose(GLUE_ICEBERG))
    provider = OdcsProvider()

    doc = provider.render(copy.deepcopy(contract), version="3.2.0")

    assert doc["apiVersion"] == V32 and doc["servers"][0]["type"] == "iceberg"
    assert (
        provider.render(copy.deepcopy(contract))["apiVersion"] == V31
    )  # the provider is unchanged
    assert (
        OdcsProvider(version="3.2.0").render(copy.deepcopy(contract), version="3.1.0")["servers"][
            0
        ]["type"]
        == "s3"
    )


def test_an_unsupported_version_is_refused_by_the_provider_and_by_a_call():
    with pytest.raises(OdcsVersionError):
        OdcsProvider(version="4.0.0")
    with pytest.raises(OdcsVersionError):
        OdcsProvider().render(_contract(_expose(GLUE_ICEBERG)), version="3.9.9")


def test_every_port_of_a_contract_takes_the_version():
    contract = _contract(_expose(GLUE_ICEBERG, "a"), _expose(GLUE_ICEBERG, "b"))

    docs = OdcsProvider(version="3.2.0").render_all_ports(contract)

    assert [(eid, doc["apiVersion"], doc["servers"][0]["type"]) for eid, doc in docs] == [
        ("a", V32, "iceberg"),
        ("b", V32, "iceberg"),
    ]


# ── The Iceberg server ──────────────────────────────────────────────────


def test_a_glue_iceberg_table_is_an_iceberg_server_from_v3_2_0():
    [server] = _servers(_contract(_expose(GLUE_ICEBERG)), "3.2.0")

    assert server == {
        "server": "orders",
        "type": "iceberg",
        "catalog": "glue",
        "catalogUrl": "https://glue.eu-north-1.amazonaws.com/iceberg",
        "namespace": "sales",
        "warehouse": "s3://acme-lake/gold/orders/",
    }


def test_the_same_table_is_what_it_always_was_in_v3_1_0():
    [server] = _servers(_contract(_expose(GLUE_ICEBERG)), None)

    assert server == {
        "server": "orders",
        "type": "s3",
        "location": "s3://acme-lake/gold/orders/",
        "format": "iceberg",
    }
    assert _servers(_contract(_expose(GLUE_ICEBERG)), "3.1.0") == [server]


def test_the_binding_names_what_the_server_may_not_guess():
    binding = copy.deepcopy(GLUE_ICEBERG)
    del binding["location"]["region"]
    binding["location"]["warehouse"] = "s3://acme-lake/warehouse/"

    [server] = _servers(_contract(_expose(binding)), "3.2.0")

    assert server["catalogUrl"] == "${ICEBERG_CATALOG_URL}"  # no region, no endpoint to claim
    assert server["warehouse"] == "s3://acme-lake/warehouse/"  # the binding's own wins


@pytest.mark.parametrize("region", ["{{ env.AWS_REGION }}", "${AWS_REGION}", "eu north 1", "../x"])
def test_a_region_that_is_not_a_region_makes_no_endpoint(region):
    binding = copy.deepcopy(GLUE_ICEBERG)
    binding["location"]["region"] = region

    [server] = _servers(_contract(_expose(binding)), "3.2.0")

    assert server["catalogUrl"] == "${ICEBERG_CATALOG_URL}"


def test_a_bucket_still_a_template_gives_no_warehouse():
    binding = copy.deepcopy(GLUE_ICEBERG)
    binding["location"]["bucket"] = "{{ env.LAKE_BUCKET }}"

    [server] = _servers(_contract(_expose(binding)), "3.2.0")

    assert "warehouse" not in server


@pytest.mark.parametrize(
    "kind, location, catalog",
    [
        # the warehouse is the catalog's registered name
        ("lakekeeper", {"uri": "https://lk.example.com/catalog", "warehouse": "prod"}, "prod"),
        (
            "polaris",
            {"uri": "https://polaris.example.com/api/catalog", "warehouse": "main"},
            "main",
        ),
        (
            "unity",
            {"uri": "https://uc.example.com/api/2.1/unity-catalog/iceberg", "warehouse": "uc"},
            "uc",
        ),
        # a generic REST catalog: the kind is the best name the binding holds
        ("rest", {"uri": "https://rest.example.com", "warehouse": "s3://lake/wh/"}, "rest"),
    ],
)
def test_a_rest_catalog_is_an_iceberg_server_whatever_the_platform(kind, location, catalog):
    [server] = _servers(_contract(_expose(_rest_iceberg(kind, **location))), "3.2.0")

    assert server["type"] == "iceberg"
    assert server["catalog"] == catalog
    assert server["catalogUrl"] == location["uri"]
    assert server["namespace"] == "sales"
    assert server["warehouse"] == location["warehouse"]


# Built apart from the URLs: a credential-shaped URL literal is what the secret scan looks for.
CRED = "user:" + "s3cr3t"
CRED_WITH_AT = "user:p@" + "s3cr3t"


@pytest.mark.parametrize(
    "uri, written",
    [
        ("https://{c}@lk.example.com/catalog", "https://lk.example.com/catalog"),
        ("https://s3cr3t@lk.example.com:8181/catalog", "https://lk.example.com:8181/catalog"),
        ("https://{c}@[::1]:8181/catalog", "https://[::1]:8181/catalog"),
        # a password with an unescaped @ ends at the last @ of the authority
        ("https://{a}@lk.example.com/catalog", "https://lk.example.com/catalog"),
        # a URL no parser reads still loses its credential: the part is cut by text
        ("https://{c}@lk.example.com:99999/catalog", "https://lk.example.com:99999/catalog"),
        ("https://{c}@lk.example.com:abc/catalog", "https://lk.example.com:abc/catalog"),
        ("https://{c}@[::1/catalog", "https://[::1/catalog"),
        ("{c}@lk.example.com/catalog", "lk.example.com/catalog"),
        ("//{c}@lk.example.com/catalog", "//lk.example.com/catalog"),
        # a character a URL parser ignores does not hide the part from the cut
        ("\x01https://{c}@lk.example.com/catalog", "https://lk.example.com/catalog"),
        ("ht\ttp://{c}@lk.example.com/catalog", "http://lk.example.com/catalog"),
        # cutting the part must not leave an authority of its own behind
        ("a@//{c}@lk.example.com/catalog", "//lk.example.com/catalog"),
        # an @ outside the authority is not a credential
        ("https://lk.example.com/catalog?owner=a@b", "https://lk.example.com/catalog?owner=a@b"),
        ("https://lk.example.com/a@b/catalog", "https://lk.example.com/a@b/catalog"),
        ("https://lk.example.com/catalog", "https://lk.example.com/catalog"),
    ],
)
def test_a_credential_in_the_catalog_uri_is_not_written_to_the_catalog_url(uri, written, caplog):
    uri = uri.replace("{c}", CRED).replace("{a}", CRED_WITH_AT)

    with caplog.at_level(logging.WARNING):
        [server] = _servers(
            _contract(_expose(_rest_iceberg("rest", uri=uri, warehouse="w"))), "3.2.0"
        )

    assert server["catalogUrl"] == written
    assert "s3cr3t" not in str(server)
    assert ("not written to catalogUrl" in caplog.text) == (written != uri)
    assert _without_userinfo(written) == written  # cutting twice cuts nothing more


def test_a_url_that_still_holds_a_credential_after_every_cut_is_not_written(monkeypatch, caplog):
    from fluid_build.providers.odcs.mappers import servers

    monkeypatch.setattr(servers, "_USERINFO_PASSES", 1)  # one cut, and no pass left to check

    with caplog.at_level(logging.WARNING):
        [server] = _servers(
            _contract(
                _expose(
                    _rest_iceberg(
                        "rest", uri=f"https://{CRED}@lk.example.com/catalog", warehouse="w"
                    )
                )
            ),
            "3.2.0",
        )

    assert server["catalogUrl"] == "${ICEBERG_CATALOG_URL}"
    assert "cannot be cut" in caplog.text and "s3cr3t" not in caplog.text


def test_a_rest_catalog_without_a_uri_gets_the_placeholder():
    [server] = _servers(_contract(_expose(_rest_iceberg("rest", warehouse="w"))), "3.2.0")

    assert server["catalogUrl"] == "${ICEBERG_CATALOG_URL}"


@pytest.mark.parametrize(
    "binding, server_type",
    [
        pytest.param(
            {
                "platform": "snowflake",
                "format": "iceberg",
                "location": {"database": "DB", "schema": "S", "table": "T"},
            },
            "snowflake",
            id="snowflake-managed",
        ),
        pytest.param(
            {
                "platform": "gcp",
                "format": "iceberg",
                "location": {"catalog": "bigquery", "project": "p", "dataset": "d", "table": "t"},
            },
            "bigquery",
            id="bigquery",
        ),
        pytest.param(
            {
                "platform": "aws",
                "format": "parquet",
                "location": {"database": "d", "table": "t", "bucket": "b", "path": "p/"},
            },
            "s3",
            id="not-iceberg",
        ),
    ],
)
def test_a_table_in_a_warehouses_own_catalog_keeps_its_platforms_server(binding, server_type):
    [server] = _servers(_contract(_expose(binding)), "3.2.0")

    assert server["type"] == server_type
    assert _servers(_contract(_expose(binding)), None)[0]["type"] == server_type


def test_an_iceberg_dependency_is_an_iceberg_server_too():
    contract = _contract(
        _expose({"platform": "local", "format": "csv", "location": {"path": "./o.csv"}}),
        expects=[{"id": "upstream", "binding": GLUE_ICEBERG}],
    )

    servers = _servers(contract, "3.2.0")

    assert [(s["server"], s["type"]) for s in servers] == [
        ("orders", "local"),
        ("upstream", "iceberg"),
    ]


@pytest.mark.parametrize("platform", ["gcp", "azure", "databricks", "local"])
def test_a_binding_that_names_no_catalog_does_not_get_one_invented(platform):
    binding = {
        "platform": platform,
        "format": "iceberg",
        "location": {"database": "sales", "table": "orders", "dataset": "sales"},
    }

    [server] = _servers(_contract(_expose(binding)), "3.2.0")

    assert server["type"] != "iceberg"
    assert server == _servers(_contract(_expose(binding)), None)[0]


def test_a_uri_is_a_rest_catalog_the_binding_names_on_any_platform():
    binding = {
        "platform": "gcp",
        "format": "iceberg",
        "location": {"uri": "https://rest.example.com", "warehouse": "w", "dataset": "sales"},
    }

    [server] = _servers(_contract(_expose(binding)), "3.2.0")

    assert (server["type"], server["catalog"], server["catalogUrl"]) == (
        "iceberg",
        "rest",
        "https://rest.example.com",
    )


@pytest.mark.parametrize("location", ["oops", 5, ["a"], None])
def test_a_binding_location_that_is_not_a_mapping_renders_in_v3_2_0_as_in_v3_1_0(location):
    binding = {"platform": "aws", "format": "iceberg", "location": location}
    contract = _contract(_expose(binding))

    [server] = _servers(contract, "3.2.0")

    assert server["server"] == "orders"
    assert server["type"] in {"iceberg", "s3"}
    assert _servers(contract, None)[0]["type"] == "s3"


def test_the_catalog_is_read_from_the_same_location_as_the_rest_of_the_server():
    # no ``location`` on the binding: the expose's own carries the catalog, its uri and warehouse
    exposed = _expose({"platform": "aws", "format": "iceberg"})
    exposed["location"] = {
        "catalog": "lakekeeper",
        "uri": "https://lk.example.com/catalog",
        "warehouse": "prod",
        "database": "sales",
    }

    [server] = _servers(_contract(exposed), "3.2.0")

    assert server == {
        "server": "orders",
        "type": "iceberg",
        "catalog": "prod",
        "catalogUrl": "https://lk.example.com/catalog",
        "namespace": "sales",
        "warehouse": "prod",
    }


@pytest.mark.parametrize(
    "region, endpoint",
    [
        ("eu-north-1", True),
        ("ap-southeast-2", True),
        ("ca-central-1", True),
        ("me-south-1", True),
        ("il-central-1", True),
        ("cn-north-1", False),  # another partition, another domain
        ("cn-northwest-1", False),
        ("us-gov-west-1", False),
        ("us-iso-east-1", False),
        ("EU-NORTH-1", False),
    ],
)
def test_glues_endpoint_is_written_for_a_commercial_region_only(region, endpoint):
    binding = copy.deepcopy(GLUE_ICEBERG)
    binding["location"]["region"] = region

    [server] = _servers(_contract(_expose(binding)), "3.2.0")

    expected = (
        f"https://glue.{region}.amazonaws.com/iceberg" if endpoint else "${ICEBERG_CATALOG_URL}"
    )
    assert server["catalogUrl"] == expected


def test_the_extras_block_names_the_version_it_is_written_for():
    contract = _contract(_expose(GLUE_ICEBERG))

    for version in (V31, V32):
        doc = OdcsProvider(version=version).render(copy.deepcopy(contract))
        [extras] = [p for p in doc["customProperties"] if p["property"] == "fluidExtras"]

        assert f"no ODCS {version} field" in extras["description"]


# ── Valid under the version it declares ─────────────────────────────────


@pytest.mark.parametrize("version", [V31, V32])
@pytest.mark.parametrize(
    "contract",
    [
        pytest.param(_contract(_expose(GLUE_ICEBERG)), id="glue-iceberg"),
        pytest.param(
            _contract(
                _expose(
                    _rest_iceberg(
                        "lakekeeper", uri="https://lk.example.com/catalog", warehouse="prod"
                    )
                )
            ),
            id="lakekeeper",
        ),
        pytest.param(
            _contract(
                _expose(
                    {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {"database": "D", "schema": "S", "table": "T"},
                    }
                )
            ),
            id="snowflake",
        ),
        pytest.param(
            _contract(
                _expose(
                    {
                        "platform": "aws",
                        "format": "parquet",
                        "location": {"database": "d", "table": "t", "bucket": "b", "path": "p/"},
                    }
                )
            ),
            id="s3",
        ),
        pytest.param(
            _contract(
                _expose({"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}})
            ),
            id="kafka",
        ),
    ],
)
def test_every_export_is_valid_under_the_schema_of_its_own_version(contract, version):
    doc = OdcsProvider(version=version).render(copy.deepcopy(contract))

    assert doc["apiVersion"] == version
    assert collect_errors(doc, load_schema(version)) == []


def test_the_iceberg_server_is_exactly_what_the_v3_2_0_schema_allows():
    [server] = _servers(_contract(_expose(GLUE_ICEBERG)), "3.2.0")

    iceberg = load_schema(V32)["$defs"]["ServerSource"]["IcebergServer"]
    common = set(load_schema(V32)["$defs"]["Server"]["properties"])
    assert set(iceberg["required"]) <= set(server)
    assert set(server) <= common | set(iceberg["properties"])


def test_a_v3_2_0_iceberg_document_fails_the_v3_1_0_schema_and_passes_its_own():
    doc = OdcsProvider(version="3.2.0").render(_contract(_expose(GLUE_ICEBERG)))

    assert collect_errors(doc, load_schema(V32)) == []
    messages = " ".join(error["message"] for error in collect_errors(doc, load_schema(V31)))
    assert "v3.2.0" in messages or "iceberg" in messages


# ── Validating a document by the version it declares ────────────────────


def test_the_schema_of_a_document_is_the_one_of_its_declared_version():
    assert load_schema_for({"apiVersion": "v3.2.0"})["properties"]["apiVersion"]["default"] == V32
    for document in ({"apiVersion": "v3.1.0"}, {"apiVersion": "v3.0.2"}, {}):
        assert load_schema_for(document)["properties"]["apiVersion"]["default"] == V31


def test_validate_contract_holds_a_document_to_its_own_version_whatever_the_provider():
    doc = OdcsProvider(version="3.2.0").render(_contract(_expose(GLUE_ICEBERG)))
    older = {**doc, "apiVersion": V31}

    OdcsProvider().validate_contract(doc)  # a default provider takes a v3.2.0 document
    with pytest.raises(ProviderError):
        OdcsProvider(version="3.2.0").validate_contract(
            older
        )  # and refuses a v3.1.0 one with iceberg in it


def test_importing_a_valid_v3_2_0_document_warns_of_nothing(caplog):
    doc = OdcsProvider(version="3.2.0").render(_contract(_expose(GLUE_ICEBERG)))

    with caplog.at_level(logging.WARNING):
        fluid = OdcsProvider().import_contract(doc)

    assert not [r for r in caplog.records if "validation" in r.getMessage().lower()]
    assert fluid["id"] == doc["id"]


def test_a_round_trip_is_lossless_at_the_version_the_document_declares():
    contract = _contract(_expose(GLUE_ICEBERG))
    for version in ("3.1.0", "3.2.0"):
        doc = OdcsProvider(version=version).render(copy.deepcopy(contract))

        assert OdcsProvider().roundtrip_check(doc)["equal"] is True, version


# ── The provider's own schema ───────────────────────────────────────────


def test_a_schema_the_caller_sets_on_the_provider_is_the_one_a_document_without_a_version_meets():
    provider = OdcsProvider()
    provider.schema = {"type": "object", "required": ["marker"]}

    with pytest.raises(ProviderError, match="marker"):
        provider.validate_contract({"apiVersion": V31})
    provider.validate_contract({"marker": 1})  # no version: the provider's schema, nothing else


def test_a_provider_without_a_schema_skips_validation_as_it_always_did(caplog):
    provider = OdcsProvider()
    provider.schema = None

    with caplog.at_level(logging.WARNING):
        provider.validate_contract({"anything": "goes"})

    assert "skipping validation" in caplog.text


def test_a_document_that_declares_another_vendored_version_is_held_to_that_one():
    provider = OdcsProvider(version="3.2.0")
    provider.schema = {"type": "object"}  # accepts anything
    doc = OdcsProvider(version="3.2.0").render(_contract(_expose(GLUE_ICEBERG)))

    provider.validate_contract({"apiVersion": V32})  # the provider's version: the provider's schema
    with pytest.raises(ProviderError):
        provider.validate_contract({**doc, "apiVersion": V31})  # an iceberg server is no v3.1.0 one


# ── vowl, which reads the versions it was released with ─────────────────


@pytest.fixture
def fake_vowl(monkeypatch):
    """A ``vowl`` that reads v3.1.0 and v3.0.2 only, and records what it was handed."""
    import sys
    import types

    handed = []

    class Contract:
        def __init__(self, data):
            handed.append(data)
            if data.get("apiVersion") not in ("v3.1.0", "v3.0.2"):
                raise ValueError(f"Unsupported API version: {data.get('apiVersion')}")
            self._data = data

        def get_check_references_by_schema(self):
            return {"orders": ["c1", "c2"]}

        def get_api_version(self):
            return self._data["apiVersion"]

        def get_schema_names(self):
            return ["orders"]

    vowl = types.ModuleType("vowl")
    vowl.Contract = Contract
    contracts = types.ModuleType("vowl.contracts")
    models = types.ModuleType("vowl.contracts.models")
    models.SUPPORTED_VERSIONS = ["v3.1.0", "v3.0.2"]
    vowl.contracts, contracts.models = contracts, models
    for name, module in (
        ("vowl", vowl),
        ("vowl.contracts", contracts),
        ("vowl.contracts.models", models),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return types.SimpleNamespace(handed=handed, models=models, contracts=contracts)


def test_vowl_is_not_asked_to_read_a_version_it_does_not_read(fake_vowl):
    from fluid_build.providers.odcs.validation import validate_via_vowl

    diag = validate_via_vowl({"apiVersion": V32})

    assert diag == {"skipped": "vowl reads ODCS v3.1.0, v3.0.2, not v3.2.0"}
    assert fake_vowl.handed == []


def test_vowl_still_reads_the_versions_it_reads(fake_vowl):
    from fluid_build.providers.odcs.validation import validate_via_vowl

    diag = validate_via_vowl({"apiVersion": V31})

    assert diag == {"api_version": V31, "schemas": ["orders"], "total_checks": 2}
    assert len(fake_vowl.handed) == 1


def test_a_version_that_is_not_one_the_exporter_writes_still_goes_to_vowl_to_say_so(fake_vowl):
    from fluid_build.providers.odcs.validation import validate_via_vowl

    with pytest.raises(ProviderError, match="Unsupported API version"):
        validate_via_vowl({"apiVersion": "v9.9.9"})


def test_a_vowl_that_does_not_list_its_versions_is_asked_anyway(fake_vowl, monkeypatch):
    import sys

    from fluid_build.providers.odcs.validation import validate_via_vowl

    monkeypatch.delattr(fake_vowl.models, "SUPPORTED_VERSIONS")
    monkeypatch.setitem(sys.modules, "vowl.contracts.models", None)  # not importable at all

    with pytest.raises(ProviderError, match="Unsupported API version"):
        validate_via_vowl({"apiVersion": V32})
    assert len(fake_vowl.handed) == 1


def test_a_v3_2_0_export_is_written_when_vowl_cannot_read_it(
    fake_vowl, monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("ODCS_VOWL_VALIDATE", "true")
    out = tmp_path / "out.yaml"

    with caplog.at_level(logging.INFO):
        doc = OdcsProvider(version="3.2.0").render(_contract(_expose(GLUE_ICEBERG)), out=str(out))

    assert out.exists() and doc["apiVersion"] == V32
    assert "vowl: skipped" in caplog.text
    assert fake_vowl.handed == []


def test_a_v3_1_0_export_still_goes_through_vowl(fake_vowl, monkeypatch, caplog):
    monkeypatch.setenv("ODCS_VOWL_VALIDATE", "true")

    with caplog.at_level(logging.INFO):
        OdcsProvider().render(_contract(_expose(GLUE_ICEBERG)))

    assert len(fake_vowl.handed) == 1
    assert "vowl: ODCS v" in caplog.text


def test_fluid_odcs_validate_says_vowl_skipped_a_version_it_cannot_read(
    fake_vowl, tmp_path, capsys
):
    from fluid_build.cli import main

    doc = tmp_path / "v32.odcs.yaml"
    doc.write_text(
        yaml.safe_dump(OdcsProvider(version="3.2.0").render(_contract(_expose(GLUE_ICEBERG)))),
        encoding="utf-8",
    )

    rc = main(["odcs", "validate", str(doc)])

    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0
    assert "jsonschema: clean" in out
    assert "vowl: skipped, vowl reads ODCS v3.1.0, v3.0.2, not v3.2.0" in out
    assert fake_vowl.handed == []
