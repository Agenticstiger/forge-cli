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

"""FLUID → ODCS → FLUID fidelity, the direction PR #373's title claims.

``tests/test_odcs_roundtrip.py`` proves the *ODCS* document is a fixed point
(``export(import(export(x))) == export(x)``). That is a weaker property than
the one the round-trip claim implies, and it held even while the FLUID leg was
losing 88 of 114 leaf fields: an importer can drop everything it does not
understand and still be a fixed point, because the second export drops the same
things again.

These tests pin the FLUID leg:

* the importer's output is a document ``fluid validate`` accepts;
* a Snowflake-bound contract stays Snowflake-bound;
* parameterized column types keep their precision/scale/length;
* every leaf of the source contract survives the trip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest
import yaml

from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.mappers.normalize import (
    _MIN_EXTENSIONS_VERSION as MIN_EXTENSIONS_VERSION,
)
from fluid_build.providers.odcs.mappers.normalize import _supports_extensions
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


def _version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in str(version).split("."))


SNOWFLAKE_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "gold.retail.customer_360_v1",
    "name": "Customer 360 (Snowflake)",
    "description": "Single customer view for retail analytics.",
    "domain": "retail",
    "tags": ["snowflake", "customer", "gold-candidate"],
    "metadata": {
        "layer": "Gold",
        "productType": "CDP",
        "classification": "internal",
        "owner": {
            "team": "data-platform",
            "email": "data-platform@example.com",
            "slack": "#data-platform",
            "oncall": "data-platform-oncall",
        },
        "businessContext": {"domain": "retail", "subdomain": "customer"},
        "tags": ["tier1"],
    },
    "lifecycle": {"state": "retired"},
    "builds": [
        {
            "id": "seed_customer_360",
            "pattern": "embedded-logic",
            "engine": "sql",
            "properties": {"sql": "SELECT 1"},
        }
    ],
    "exposes": [
        {
            "exposeId": "customer_360",
            "kind": "table",
            "title": "Customer 360",
            "version": "1.2.0",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {
                    "account": "ACME-TEST",
                    "database": "FLUID_TEST",
                    "schema": "GOLD",
                    "table": "CUSTOMER_360",
                },
                "properties": {"cluster_by": ["CUSTOMER_ID"], "comment": "Managed by FLUID."},
            },
            "qos": {"availability": "99.9%", "freshnessSLO": "PT1H"},
            "contract": {
                "schema": [
                    {"name": "CUSTOMER_ID", "type": "decimal(38,0)", "required": True},
                    {"name": "CUSTOMER_NAME", "type": "varchar(255)"},
                    {"name": "ACCOUNT_BALANCE", "type": "decimal(18,4)"},
                    {"name": "PHONE", "type": "varchar(32)"},
                    {"name": "LAST_REFRESHED_AT", "type": "timestamp"},
                    {"name": "BARE_DECIMAL", "type": "decimal"},
                ]
            },
        }
    ],
}


def _leaves(obj: Any, path: str = "") -> Iterator[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _leaves(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _leaves(value, f"{path}[{index}]")
    else:
        yield path, obj


@pytest.fixture(scope="module")
def provider() -> OdcsProvider:
    return OdcsProvider()


@pytest.fixture(scope="module")
def exported(provider: OdcsProvider) -> Dict[str, Any]:
    return provider.render(SNOWFLAKE_CONTRACT)


@pytest.fixture(scope="module")
def reimported(provider: OdcsProvider, exported: Dict[str, Any]) -> Dict[str, Any]:
    return provider.import_contract(exported)


# ---------------------------------------------------------------------------
# The importer must emit a document the FLUID schema accepts
# ---------------------------------------------------------------------------


def _validate_fluid(document: Dict[str, Any]) -> list:
    manager = FluidSchemaManager()
    version = document.get("fluidVersion") or manager.latest_bundled_version()
    schema = manager.get_schema(version)
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    return sorted(validator.iter_errors(document), key=lambda e: list(e.path))


def test_imported_contract_is_schema_valid(reimported: Dict[str, Any]) -> None:
    """`fluid odcs import` printed ✓ while writing a document `fluid validate`
    rejected with 18+ errors: a top-level ``contract:``/``expects:``/``owner:``,
    no ``fluidVersion``/``kind``/``id``/``name``, ``odcs_passthrough`` buckets
    parked next to schema-closed objects, and a ``metadata.status`` the FLUID
    schema forbids."""
    errors = _validate_fluid(reimported)
    assert not errors, "imported contract is not valid FLUID:\n" + "\n".join(
        f"  {list(e.path)}: {e.message}" for e in errors
    )


def test_imported_contract_has_the_required_root_fields(reimported: Dict[str, Any]) -> None:
    for key in ("fluidVersion", "kind", "id", "name", "metadata", "exposes"):
        assert key in reimported, f"missing required FLUID root key {key!r}"
    assert "contract" not in reimported
    assert "expects" not in reimported
    assert "owner" not in reimported


# ---------------------------------------------------------------------------
# Snowflake binding fidelity
# ---------------------------------------------------------------------------


def test_snowflake_binding_is_not_rewritten_to_bigquery(reimported: Dict[str, Any]) -> None:
    """``physicalType: table`` used to map unconditionally to ``bigquery``,
    overriding ``servers[].type: snowflake`` two blocks away in the same file."""
    binding = reimported["exposes"][0]["binding"]
    assert binding["platform"] == "snowflake"
    assert binding["format"] == "snowflake_table"
    assert binding["location"]["table"] == "CUSTOMER_360"
    assert binding["location"]["database"] == "FLUID_TEST"


def test_external_snowflake_odcs_imports_as_snowflake(provider: OdcsProvider) -> None:
    """The same guarantee for a hand-written ODCS document FLUID never emitted."""
    odcs = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "acme.orders",
        "version": "1.0.0",
        "status": "active",
        "servers": [
            {
                "server": "sf_prod",
                "type": "snowflake",
                "account": "ACME-TEST",
                "database": "FLUID_TEST",
                "schema": "GOLD",
                "warehouse": "COMPUTE_WH",
            }
        ],
        "schema": [
            {
                "name": "orders",
                "logicalType": "object",
                "physicalType": "table",
                "physicalName": "ORDERS",
                "properties": [{"name": "ORDER_ID", "logicalType": "integer"}],
            }
        ],
    }
    fluid = provider.import_contract(odcs)
    binding = fluid["exposes"][0]["binding"]
    assert binding["platform"] == "snowflake"
    assert binding["location"]["account"] == "ACME-TEST"
    assert binding["location"]["table"] == "ORDERS"
    assert not _validate_fluid(fluid)


def test_schema_object_carries_the_physical_object_name(exported: Dict[str, Any]) -> None:
    """Without ``physicalName`` no consumer can reconstruct a fully-qualified
    object: ``name`` is the lowercase exposeId, and on Snowflake
    ``customer_360`` and ``CUSTOMER_360`` are not interchangeable."""
    assert exported["schema"][0]["physicalName"] == "CUSTOMER_360"


# ---------------------------------------------------------------------------
# Type fidelity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "logical", "physical"),
    [
        ("CUSTOMER_ID", "number", "DECIMAL(38,0)"),
        ("ACCOUNT_BALANCE", "number", "DECIMAL(18,4)"),
        ("CUSTOMER_NAME", "string", "VARCHAR(255)"),
        ("PHONE", "string", "VARCHAR(32)"),
        ("LAST_REFRESHED_AT", "timestamp", "TIMESTAMP_NTZ"),
        ("BARE_DECIMAL", "number", "DECIMAL"),
    ],
)
def test_parameterized_types_keep_their_parameters(
    exported: Dict[str, Any], column: str, logical: str, physical: str
) -> None:
    """Every parameterized type degraded to ``logicalType: string`` with no
    ``physicalType`` — a decimal(18,4) money column and a decimal(38,0) key
    published as strings — because the lookup was a whole-string dict hit and
    only bare type names are in the table."""
    prop = next(p for p in exported["schema"][0]["properties"] if p["name"] == column)
    assert prop["logicalType"] == logical
    assert prop.get("physicalType") == physical


def test_string_length_lands_in_logical_type_options(exported: Dict[str, Any]) -> None:
    prop = next(p for p in exported["schema"][0]["properties"] if p["name"] == "CUSTOMER_NAME")
    assert prop["logicalTypeOptions"] == {"maxLength": 255}


def test_number_precision_is_not_put_in_logical_type_options(exported: Dict[str, Any]) -> None:
    """ODCS v3.1.0 closes ``logicalTypeOptions`` per logicalType and the
    number/integer branches have no precision/scale slot (only a Rust-float
    ``format`` enum). Emitting them there fails the published schema; the
    spec's home for a parameterized type is ``physicalType``."""
    prop = next(p for p in exported["schema"][0]["properties"] if p["name"] == "ACCOUNT_BALANCE")
    assert "precision" not in prop.get("logicalTypeOptions", {})


def test_exported_document_passes_the_vendored_odcs_schema(
    provider: OdcsProvider, exported: Dict[str, Any]
) -> None:
    provider.validate_contract(exported)


# ---------------------------------------------------------------------------
# lifecycle.state drives the published status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("preview", "draft"),
        ("active", "active"),
        ("deprecated", "deprecated"),
        ("retired", "retired"),
    ],
)
def test_lifecycle_state_drives_odcs_status(
    provider: OdcsProvider, state: str, expected: str
) -> None:
    """Both exporters read ``metadata.status`` — a key the FLUID schema forbids
    — so every export shipped the hard-coded default and a retired product was
    published to catalogs as an ``active`` contract."""
    contract = yaml.safe_load(yaml.safe_dump(SNOWFLAKE_CONTRACT))
    contract["lifecycle"]["state"] = state
    assert provider.render(contract)["status"] == expected


# ---------------------------------------------------------------------------
# Whole-document fidelity
# ---------------------------------------------------------------------------


def test_no_leaf_is_lost_or_mutated(reimported: Dict[str, Any]) -> None:
    """FLUID → ODCS → FLUID used to lose 88 of 114 leaves and mutate 7 more."""
    source = dict(_leaves(SNOWFLAKE_CONTRACT))
    result = dict(_leaves(reimported))
    lost = sorted(set(source) - set(result))
    mutated = sorted(k for k in set(source) & set(result) if source[k] != result[k])
    assert not lost, f"leaves lost in the round-trip: {lost}"
    assert not mutated, "leaves mutated in the round-trip: " + "; ".join(
        f"{k}: {source[k]!r} -> {result[k]!r}" for k in mutated
    )


def test_round_trip_is_idempotent(provider: OdcsProvider, reimported: Dict[str, Any]) -> None:
    """A second trip must be a no-op — otherwise the first one only looked lossless."""
    again = provider.import_contract(provider.render(reimported))
    assert again == reimported


_SHIPPED_EXAMPLES = [
    "examples/customer360/contract.fluid.yaml",
    "examples/snowflake/billing_history/contract.fluid.yaml",
    "examples/snowflake/smoke/contract.fluid.yaml",
    "examples/01-hello-world/contract.fluid.yaml",
    "examples/mcp-output-port/contract.fluid.yaml",
    "examples/aws-medallion-lake/contract.fluid.yaml",
]


@pytest.mark.parametrize("rel_path", _SHIPPED_EXAMPLES)
def test_shipped_examples_round_trip_without_loss(provider: OdcsProvider, rel_path: str) -> None:
    """No leaf may be lost, and none may be mutated except the one the schema
    forces — see ``test_only_fluid_version_may_move_and_only_upward``."""
    path = REPO_ROOT / rel_path
    if not path.exists():
        pytest.skip(f"example contract not present: {rel_path}")
    with open(path) as handle:
        contract = yaml.safe_load(handle)

    result = provider.import_contract(provider.render(contract))
    source = dict(_leaves(contract))
    landed = dict(_leaves(result))
    lost = sorted(k for k in source if k not in landed)
    mutated = sorted(
        k for k in source if k in landed and source[k] != landed[k] and k != "fluidVersion"
    )
    assert not lost, f"{rel_path}: leaves lost: {lost}"
    assert not mutated, f"{rel_path}: leaves mutated: {mutated}"


@pytest.mark.parametrize("rel_path", _SHIPPED_EXAMPLES)
def test_only_fluid_version_may_move_and_only_upward(provider: OdcsProvider, rel_path: str) -> None:
    """``fluidVersion`` is the single leaf the round-trip is allowed to change,
    and only to make the output *valid*.

    Root ``extensions`` — the one open bucket in the FLUID schema and so the
    only legal home for ODCS round-trip state — was added in 0.7.3. Writing it
    into a contract that declares 0.7.1/0.7.2 produced "root: Additional
    properties are not allowed ('extensions' was unexpected)": an importer
    reporting success while emitting a contract its own validator rejects.

    Dropping the bucket instead is not available: it is what makes the ODCS leg
    a fixed point (``test_fluid_emitted_odcs_roundtrips_zero_diff``), so a
    0.7.1 source loses ``metadata.tenant``, the team members and a whole server
    entry. The document therefore has to declare a version that can hold what it
    carries. This test pins every part of that bargain so it cannot quietly widen
    into "the importer rewrites versions".
    """
    path = REPO_ROOT / rel_path
    if not path.exists():
        pytest.skip(f"example contract not present: {rel_path}")
    with open(path) as handle:
        contract = yaml.safe_load(handle)

    result = provider.import_contract(provider.render(contract))
    source_version = contract["fluidVersion"]
    landed_version = result["fluidVersion"]

    # 1. The output is valid FLUID — the whole point of moving the version.
    errors = _validate_fluid(result)
    assert not errors, f"{rel_path}: not valid FLUID:\n" + "\n".join(
        f"  {list(e.path)}: {e.message}" for e in errors
    )

    if landed_version == source_version:
        # Untouched versions must be ones that could hold the bucket anyway.
        assert _supports_extensions(source_version)
        return

    # 2. It only ever moves for a version that genuinely cannot hold the bucket,
    #    only to the LOWEST version that can, and only upward.
    assert not _supports_extensions(source_version), (
        f"{rel_path}: {source_version} can hold `extensions`; nothing justified "
        f"rewriting it to {landed_version}"
    )
    assert landed_version == MIN_EXTENSIONS_VERSION
    assert _version_key(landed_version) > _version_key(source_version)

    # 3. It only happens when the bucket is actually present and non-empty.
    assert result.get("extensions", {}).get("odcs"), (
        f"{rel_path}: version moved to {landed_version} without writing the "
        f"round-trip state that was the only reason to move it"
    )

    # 4. The authored version is recorded, and re-export republishes it — so the
    #    ODCS document keeps naming the version the human wrote.
    assert result["extensions"]["odcs"]["authored_version"] == source_version
    republished = provider.render(result)
    blob = next(
        p["value"] for p in republished["customProperties"] if p["property"] == "fluidExtras"
    )
    assert blob["root"]["fluidVersion"] == source_version


def test_min_extensions_version_is_really_the_lowest_that_supports_it() -> None:
    """``MIN_EXTENSIONS_VERSION`` is a claim about the bundled schemas, so check
    it against them rather than trusting the constant."""
    supporting = [v for v in FluidSchemaManager.BUNDLED_VERSIONS if _supports_extensions(v)]
    assert supporting, "no bundled schema declares root `extensions`"
    assert min(supporting, key=_version_key) == MIN_EXTENSIONS_VERSION
    # And the versions below it genuinely cannot carry it.
    for version in FluidSchemaManager.BUNDLED_VERSIONS:
        if _version_key(version) < _version_key(MIN_EXTENSIONS_VERSION):
            assert not _supports_extensions(version), version


def test_opting_out_of_the_extras_blob_is_the_documented_trade(
    provider: OdcsProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ODCS_FLUID_EXTRAS=false`` publishes a lean contract; the flag names the
    trade (a lossy FLUID leg), so assert the flag actually does something."""
    monkeypatch.setenv("ODCS_FLUID_EXTRAS", "false")
    lean = provider.render(SNOWFLAKE_CONTRACT)
    assert not [p for p in lean.get("customProperties", []) if p.get("property") == "fluidExtras"]
    # ...and the ODCS-native fields are all still there.
    assert lean["name"] == "Customer 360 (Snowflake)"
    assert lean["domain"] == "retail"
    assert lean["status"] == "retired"


@pytest.mark.parametrize(
    ("declared", "expected"),
    [("99.9%", 0.999), ("99.95%", 0.9995), ("99%", 0.99), ("99.999%", 0.99999)],
)
def test_availability_percent_does_not_pick_up_a_float_artifact(
    provider: OdcsProvider, declared: str, expected: float
) -> None:
    """``99.9%`` published as ``availability: 0.9990000000000001`` — binary
    floating point leaking into an SLA a human wrote."""
    contract = yaml.safe_load(yaml.safe_dump(SNOWFLAKE_CONTRACT))
    contract["exposes"][0]["qos"] = {"availability": declared}
    sla = {p["property"]: p["value"] for p in provider.render(contract)["slaProperties"]}
    assert sla["availability"] == expected
    assert repr(sla["availability"]) == repr(expected)
