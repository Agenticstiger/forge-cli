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

"""The Iceberg catalog-kind table, and every emitter agreeing with it.

``catalog: lakekeeper`` used to stream over REST while dbt's ``catalogs.yml``
wrote a Snowflake-managed table and the AWS IaC created a Glue table for the
same name: four emitters each classified the free-string ``catalog`` by hand.
The unit tests pin the table; the agreement matrix runs EVERY emitter over
every kind and checks each answer against the kind's row, so a fifth
hand-rolled classification fails here rather than in a user's lake.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
import yaml

from fluid_build.build_runners.debezium.iceberg_sink import emit_debezium_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.providers import _iceberg_catalog as ic
from fluid_build.providers._iceberg_catalog import (
    EXTERNAL_ICEBERG_CATALOGS,
    FAMILY_GLUE,
    FAMILY_UNKNOWN,
    binding_catalog_kind,
    canonical_catalog_kind,
    catalog_kind_info,
    default_catalog_kind,
    find_iceberg_expose_binding,
    iceberg_catalog_kind,
    iceberg_sink_exposes,
    is_glue_cataloged,
    is_object_store_uri,
    known_catalog_kinds,
    resolve_iceberg_catalog,
)

pytestmark = pytest.mark.unit

#: Apache Iceberg ``CatalogUtil.buildIcebergCatalog`` accepts exactly these
#: ``type`` values (1.10.0; ``bigquery`` from 1.10) and throws on any other.
_CATALOG_UTIL_TYPES = {"hadoop", "hive", "rest", "glue", "nessie", "jdbc", "bigquery"}

ALL_KINDS = sorted(ic._CATALOG_KINDS)


class TestCanonicalKind:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("lakekeeper", "lakekeeper"),
            ("Lakekeeper", "lakekeeper"),
            (" LAKEKEEPER ", "lakekeeper"),
            ("iceberg_rest", "rest"),
            ("iceberg-rest", "rest"),
            ("ICEBERG_REST", "rest"),
            ("snowflake", "snowflake-managed"),
            ("Snowflake_Managed", "snowflake-managed"),
            ("glue", "glue"),
            (None, ""),
            ("", ""),
            ("gravitino", "gravitino"),
        ],
    )
    def test_folding_and_aliases(self, raw, expected):
        assert canonical_catalog_kind(raw) == expected

    def test_unknown_value_gets_the_unknown_row_with_historic_fallbacks(self):
        info = catalog_kind_info("gravitino")
        assert info.family == FAMILY_UNKNOWN
        # The sink keeps talking REST and dbt keeps built_in; fluid validate
        # is what refuses the value.
        assert info.runtime_type == "rest"
        assert info.snowflake_catalog_type == "built_in"

    def test_known_kinds_lists_canonical_names_and_aliases(self):
        known = known_catalog_kinds()
        assert "lakekeeper" in known and "iceberg-rest" in known and "snowflake" in known


class TestTableIntegrity:
    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_wire_value_is_one_iceberg_accepts_and_never_both(self, kind):
        info = catalog_kind_info(kind)
        # catalog-impl XOR type: CatalogUtil throws when both are set.
        assert bool(info.runtime_type) != bool(info.catalog_impl)
        if info.runtime_type:
            assert info.runtime_type in _CATALOG_UTIL_TYPES

    def test_no_vendor_name_ever_reaches_the_wire(self):
        # No engine or SDK has a ``lakekeeper``/``polaris``/``unity`` type.
        for vendor in ("lakekeeper", "polaris", "unity", "snowflake-managed"):
            assert catalog_kind_info(vendor).runtime_type == "rest"

    def test_dynamodb_is_reached_by_impl_only(self):
        info = catalog_kind_info("dynamodb")
        assert info.runtime_type is None
        assert info.catalog_impl == "org.apache.iceberg.aws.dynamodb.DynamoDbCatalog"

    def test_external_set_is_derived_from_the_table(self):
        assert "lakekeeper" in EXTERNAL_ICEBERG_CATALOGS
        assert "iceberg_rest" in EXTERNAL_ICEBERG_CATALOGS
        for name in EXTERNAL_ICEBERG_CATALOGS:
            assert catalog_kind_info(name).snowflake_catalog_type == "iceberg_rest"
        for kind in ALL_KINDS:
            external = catalog_kind_info(kind).snowflake_catalog_type == "iceberg_rest"
            assert (kind in EXTERNAL_ICEBERG_CATALOGS) == external

    @pytest.mark.parametrize("kind", ["rest", "lakekeeper", "polaris", "unity"])
    def test_rest_catalogs_need_uri_and_warehouse(self, kind):
        info = catalog_kind_info(kind)
        assert info.speaks_rest
        assert set(info.sink_requires) == {"uri", "warehouse"}

    def test_generic_rest_accepts_a_uri_warehouse(self):
        # iceberg-rest-fixture and Tabular take an s3:// warehouse.
        assert not catalog_kind_info("rest").warehouse_is_name


class TestKindPrecedence:
    @pytest.mark.parametrize(
        "platform,expected",
        [
            ("aws", "glue"),
            ("glue", "glue"),  # a cloud alias of aws
            ("athena", "glue"),
            ("snowflake", "snowflake-managed"),
            ("gcp", "rest"),
            ("local", "rest"),
            ("", "rest"),
        ],
    )
    def test_platform_default(self, platform, expected):
        assert default_catalog_kind({"platform": platform}) == expected

    def test_location_catalog_beats_the_platform_default(self):
        binding = {"platform": "aws", "location": {"catalog": "Lakekeeper"}}
        assert binding_catalog_kind(binding) == "lakekeeper"

    def test_sink_catalog_beats_the_location(self):
        binding = {"platform": "aws", "location": {"catalog": "glue"}}
        assert iceberg_catalog_kind(binding, {"catalog": "lakekeeper"}) == "lakekeeper"

    def test_sink_may_be_a_spec_object(self):
        class _Sink:
            catalog = "iceberg_rest"
            partition_by = None

        assert iceberg_catalog_kind({"platform": "aws"}, _Sink()) == "rest"

    def test_absent_everywhere_falls_to_the_platform(self):
        assert iceberg_catalog_kind({"platform": "aws"}, {}) == "glue"


class TestGlueCataloged:
    @pytest.mark.parametrize(
        "fmt,catalog,platform,expected",
        [
            ("iceberg", None, "aws", True),
            ("iceberg", "glue", "aws", True),
            ("iceberg", "lakekeeper", "aws", False),
            ("iceberg", "iceberg-rest", "aws", False),
            ("iceberg_table", "polaris", "aws", False),
            # location.catalog is meaningless for a plain Glue table format.
            ("parquet", "lakekeeper", "aws", True),
            ("csv", None, "aws", True),
            # an Iceberg expose off AWS defaults to a REST catalog
            ("iceberg", None, "gcp", False),
        ],
    )
    def test_predicate(self, fmt, catalog, platform, expected):
        location: Dict[str, Any] = {"database": "d", "table": "t"}
        if catalog:
            location["catalog"] = catalog
        binding = {"platform": platform, "format": fmt, "location": location}
        assert is_glue_cataloged(binding) is expected


class TestSinkExposeSelection:
    def test_confluent_and_non_iceberg_exposes_are_not_sink_targets(self):
        contract = {
            "exposes": [
                {"exposeId": "a", "binding": {"platform": "confluent", "format": "iceberg"}},
                {"exposeId": "b", "binding": {"platform": "aws", "format": "parquet"}},
                {"exposeId": "c", "binding": {"platform": "aws", "format": "Iceberg_Table"}},
            ]
        }
        assert [e["exposeId"] for e in iceberg_sink_exposes(contract)] == ["c"]
        assert find_iceberg_expose_binding(contract) == {
            "platform": "aws",
            "format": "Iceberg_Table",
        }

    def test_no_target(self):
        assert find_iceberg_expose_binding({"exposes": []}) is None


class TestObjectStoreSchemes:
    @pytest.mark.parametrize(
        "value", ["s3://b/w", "s3a://b", "s3n://b", "gs://b", "gcs://b", "abfss://c@a/x"]
    )
    def test_object_store(self, value):
        assert is_object_store_uri(value)

    @pytest.mark.parametrize("value", ["analytics", "0190-uuid/analytics", "", None])
    def test_names(self, value):
        assert not is_object_store_uri(value)


def _binding(catalog: Optional[str], platform: str = "aws", **location) -> Dict[str, Any]:
    loc: Dict[str, Any] = {"database": "sales", "table": "orders", **location}
    if catalog is not None:
        loc["catalog"] = catalog
    return {"platform": platform, "format": "iceberg", "location": loc}


class TestResolver:
    def test_lakekeeper_streams_over_rest_with_no_file_io(self):
        r = resolve_iceberg_catalog(
            _binding("lakekeeper", uri="http://lk:8181/catalog", warehouse="analytics")
        )
        assert r.catalog_type == "rest"
        assert r.catalog_impl is None
        assert r.kind == "lakekeeper"
        # A warehouse NAME: the catalog vends the table's FileIO config.
        assert r.io_impl is None
        assert r.uri == "http://lk:8181/catalog"
        assert r.warehouse == "analytics"

    def test_glue_is_unchanged(self):
        r = resolve_iceberg_catalog(_binding(None, bucket="lake", region="eu-west-1"))
        assert r.catalog_type == "glue"
        assert r.catalog_impl == ic.GLUE_CATALOG_IMPL
        assert r.kind == "glue"

    def test_unknown_kind_keeps_the_rest_fallback(self):
        assert resolve_iceberg_catalog(_binding("gravitino")).catalog_type == "rest"


@pytest.mark.parametrize("kind", ALL_KINDS + ["gravitino"])
def test_both_sink_derivers_emit_impl_xor_type(kind):
    """THE startup crash: a Glue sink once carried both keys and Iceberg's
    CatalogUtil refused it before writing a record."""
    resolved = resolve_iceberg_catalog(
        _binding(kind, uri="http://c/catalog", warehouse="w", bucket="lake", region="us-east-1")
    )
    kc = emit_iceberg_sink_config(resolved, product_id="p.x", topics=["t"])
    assert ("iceberg.catalog.type" in kc) != ("iceberg.catalog.catalog-impl" in kc)
    if "iceberg.catalog.type" in kc:
        assert kc["iceberg.catalog.type"] in _CATALOG_UTIL_TYPES

    dbz = emit_debezium_iceberg_sink_config(resolved)
    assert ("type" in dbz) != ("catalog-impl" in dbz)
    if "type" in dbz:
        assert dbz["type"] in _CATALOG_UTIL_TYPES


# ---------------------------------------------------------------------------
# Cross-emitter agreement: every emitter answers from the kind's row
# ---------------------------------------------------------------------------

#: Every kind, plus absent and an unknown value.
MATRIX = ALL_KINDS + [None, "gravitino"]


def _row(kind: Optional[str], platform: str):
    effective = canonical_catalog_kind(kind) or default_catalog_kind({"platform": platform})
    return catalog_kind_info(effective)


def _contract(binding: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "gold.orders",
        "name": "orders",
        "metadata": {"layer": "Gold"},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": binding,
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


@pytest.mark.parametrize("kind", MATRIX)
def test_dbt_snowflake_catalogs_yml_follows_the_row(kind):
    from fluid_build.engines.dbt.catalogs_yml import generate_catalogs_yml

    binding = _binding(
        kind,
        platform="snowflake",
        uri="http://c/catalog",
        warehouse="s3://lake/p/",
        iam_role_arn="arn:aws:iam::123456789012:role/sf",
    )
    build = {"engine": "dbt", "execution": {"runtime": {"platform": "snowflake"}}}
    content = generate_catalogs_yml(_contract(binding), build)
    expected = _row(kind, "snowflake").snowflake_catalog_type
    if expected is None:
        # Snowflake has no catalog integration for it: the expose is skipped
        # (and fluid validate says why), never written as Snowflake-managed.
        assert content is None
        return
    assert content is not None
    integration = yaml.safe_load(content)["catalogs"][0]["write_integrations"][0]
    assert integration["catalog_type"] == expected


@pytest.mark.parametrize("kind", MATRIX)
def test_snowflake_iac_creates_a_volume_only_for_snowflake_managed(kind):
    from fluid_build.iac import get_iac_plugin

    binding = _binding(
        kind,
        platform="snowflake",
        warehouse="s3://lake/p/",
        iam_role_arn="arn:aws:iam::123456789012:role/sf",
        schema="PUBLIC",
    )
    res = get_iac_plugin("snowflake").emit(_contract(binding))
    has_volume = bool(res.get("snowflake_external_volume"))
    assert has_volume == (_row(kind, "snowflake").snowflake_catalog_type == "built_in")


@pytest.mark.parametrize("kind", MATRIX)
def test_aws_iac_creates_a_glue_table_only_for_glue(kind):
    from fluid_build.iac.providers.aws import AwsIacPlugin

    binding = _binding(kind, platform="aws", bucket="lake", region="us-east-1")
    if _row(kind, "aws").family == FAMILY_UNKNOWN:
        # Whether a Glue table exists hangs on the answer, and apply does not
        # run fluid validate: an unknown value is refused, not guessed.
        from fluid_build.iac.base import UnsupportedBindingError

        with pytest.raises(UnsupportedBindingError, match="unknown-iceberg-catalog|does not know"):
            AwsIacPlugin().emit(_contract(binding))
        return
    res = AwsIacPlugin().emit(_contract(binding))
    has_glue_table = bool(res.get("aws_glue_catalog_table"))
    assert has_glue_table == (_row(kind, "aws").family == FAMILY_GLUE)
    # The bucket is the catalog's storage either way.
    assert res.get("aws_s3_bucket")


@pytest.mark.parametrize("kind", MATRIX)
def test_sink_validator_demands_exactly_what_the_row_requires(kind):
    from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
        validate_iceberg_sink,
    )

    contract = _contract(_binding(kind, platform="gcp"))
    contract["builds"] = [
        {
            "id": "ingest",
            "engine": "kafka-connect",
            "properties": {"sink": {"format": "iceberg"}},
        }
    ]
    errors, _ = validate_iceberg_sink(contract)
    row = _row(kind, "gcp")
    if row.family == FAMILY_UNKNOWN:
        assert any("gravitino" in e for e in errors)
        return
    for key in ("uri", "warehouse"):
        demanded = any(f"binding.location.{key}" in e for e in errors)
        assert demanded == (key in row.sink_requires), (key, errors)
