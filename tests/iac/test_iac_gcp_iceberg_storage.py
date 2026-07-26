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

"""GCS storage for BigQuery Iceberg, the prerequisite dbt does not create.

dbt materializes BigQuery Iceberg through ``catalogs.yml`` with
``catalog_type: biglake_metastore``. Its docs are explicit that the metastore
needs no setup because it is built into BigQuery, so the one prerequisite dbt
names and refuses to create is the storage bucket.

Before this, an ``iceberg`` expose on a GCP binding emitted nothing at all.
"""

from __future__ import annotations

import pytest

from fluid_build.engines.dbt.catalogs_yml import generate_catalogs_yml
from fluid_build.iac import get_iac_plugin
from fluid_build.providers._iceberg_catalog import iceberg_bucket_name, iceberg_storage_uri

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _gcp():
    return get_iac_plugin("gcp")


def _contract(**location):
    location.setdefault("project", "p")
    location.setdefault("dataset", "d")
    location.setdefault("table", "T")
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "gold.events",
        "name": "events",
        "metadata": {"layer": "Gold", "name": "events"},
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "binding": {"platform": "gcp", "format": "iceberg", "location": location},
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def _bq_build():
    return {"engine": "dbt", "execution": {"runtime": {"platform": "gcp"}}}


class TestStorageUriHelpers:
    def test_bucket_and_path_compose_a_uri(self):
        binding = {"location": {"bucket": "lake", "path": "products/events"}}
        assert iceberg_storage_uri(binding) == "gs://lake/products/events"
        assert iceberg_bucket_name(binding) == "lake"

    def test_bucket_without_path(self):
        assert iceberg_storage_uri({"location": {"bucket": "lake"}}) == "gs://lake"

    def test_explicit_warehouse_uri_wins_for_BOTH_helpers(self):
        """Security review finding: the two helpers had opposite precedence.

        warehouse-first for the URI, bucket-first for the name, meant a
        binding carrying both had dbt writing into one bucket while the IaC
        created and governed a different one. The name is now derived from
        the URI, so they cannot diverge.
        """
        binding = {"location": {"warehouse": "gs://other/prefix", "bucket": "lake"}}
        assert iceberg_storage_uri(binding) == "gs://other/prefix"
        assert iceberg_bucket_name(binding) == "other"

    def test_bucket_parsed_out_of_a_warehouse_uri(self):
        assert iceberg_bucket_name({"location": {"warehouse": "gs://from-uri/deep/path"}}) == (
            "from-uri"
        )

    def test_foreign_scheme_is_not_usable_on_the_gs_path(self):
        """An s3:// warehouse under biglake_metastore is unusable, so both
        emitters must skip rather than one emitting storage the other
        cannot back."""
        binding = {"location": {"warehouse": "s3://aws-bucket/p", "bucket": "lake"}}
        assert iceberg_storage_uri(binding, scheme="gs") == ""
        assert iceberg_bucket_name(binding, scheme="gs") == ""

    @pytest.mark.parametrize("warehouse", ["gs://", "gs:///x"])
    def test_scheme_without_a_bucket_component_is_underivable(self, warehouse):
        """`gs://` was previously truthy, yielding an external_volume with
        no bucket behind it."""
        binding = {"location": {"warehouse": warehouse}}
        assert iceberg_storage_uri(binding) == ""
        assert iceberg_bucket_name(binding) == ""

    def test_nothing_derivable_returns_empty(self):
        assert iceberg_storage_uri({"location": {}}) == ""
        assert iceberg_bucket_name({"location": {}}) == ""
        assert iceberg_storage_uri(None) == ""


class TestGcsBucketEmission:
    def test_iceberg_expose_now_emits_a_bucket(self):
        """Previously the GCP dispatch ignored iceberg entirely."""
        res = _gcp().emit(_contract(bucket="my-lake", path="products/events", region="US"))
        assert "google_storage_bucket" in res
        assert next(iter(res["google_storage_bucket"].values()))["name"] == "my-lake"

    def test_bucket_inherits_the_standard_gcs_settings(self):
        """Reusing _emit_gcs keeps settings identical to a gcs_bucket expose."""
        res = _gcp().emit(_contract(bucket="my-lake", region="EU"))
        body = next(iter(res["google_storage_bucket"].values()))
        assert body["uniform_bucket_level_access"] is True
        assert body["location"] == "EU"

    def test_no_derivable_bucket_emits_nothing(self):
        res = _gcp().emit(_contract())
        assert "google_storage_bucket" not in res

    def test_non_iceberg_gcp_exposes_are_untouched(self):
        contract = _contract(bucket="my-lake")
        contract["exposes"][0]["binding"]["format"] = "bigquery_table"
        res = _gcp().emit(contract)
        assert "google_bigquery_table" in res


class TestCrossEmitterContract:
    """The bucket fluid creates must be the bucket dbt writes into."""

    def test_iac_bucket_matches_the_dbt_external_volume(self):
        contract = _contract(bucket="my-lake", path="products/events")
        res = _gcp().emit(contract)
        bucket = next(iter(res["google_storage_bucket"].values()))["name"]

        content = generate_catalogs_yml(contract, _bq_build())
        binding = contract["exposes"][0]["binding"]
        expected_uri = iceberg_storage_uri(binding, scheme="gs")
        assert f"external_volume: {expected_uri}" in content
        assert expected_uri.startswith(f"gs://{bucket}")

    def test_bigquery_shape_matches_dbt_docs(self):
        content = generate_catalogs_yml(_contract(bucket="my-lake"), _bq_build())
        assert "catalog_type: biglake_metastore" in content
        # BigQuery requires file_format; Snowflake has no such key.
        assert "file_format: parquet" in content
        assert "table_format: iceberg" in content

    def test_bigquery_external_volume_is_a_uri_not_a_name(self):
        """Snowflake's external_volume is an object NAME; BigQuery's is a URI.

        Emitting the Snowflake-style derived name here would point dbt at a
        string BigQuery cannot resolve.
        """
        content = generate_catalogs_yml(_contract(bucket="my-lake"), _bq_build())
        assert "external_volume: gs://my-lake" in content
        assert "FLUID_" not in content

    def test_bigquery_without_a_bucket_writes_no_catalog(self):
        assert generate_catalogs_yml(_contract(), _bq_build()) is None

    def test_snowflake_shape_is_unchanged(self):
        """Regression guard: the BigQuery branch must not leak into Snowflake."""
        contract = _contract(database="DB", table="T", bucket="lake")
        contract["exposes"][0]["binding"]["platform"] = "snowflake"
        content = generate_catalogs_yml(
            contract, {"engine": "dbt", "execution": {"runtime": {"platform": "snowflake"}}}
        )
        assert "catalog_type: built_in" in content
        assert "biglake_metastore" not in content
        assert "file_format" not in content


class TestSecurityReviewFindings:
    """Regression pins for the three findings on this change."""

    def test_shared_packaging_declares_the_data_source_it_references(self):
        """emit and emit_data must dispatch identically.

        Under shared packaging _emit_gcs references
        ${data.google_storage_bucket...} for each grant. Omitting the lookup
        in emit_data made every apply fail tofu validate with "Reference to
        undeclared resource".
        """
        contract = _contract(bucket="pool-lake", path="products/events")
        contract["packaging"] = {"mode": "shared", "pool": "pool-lake"}
        contract["accessPolicy"] = {"grants": [{"principal": "user:a@b.c", "permission": "read"}]}
        plugin = _gcp()
        resources = plugin.emit(contract)
        data = plugin.emit_data(contract)

        referenced = [
            v
            for body in resources.get("google_storage_bucket_iam_member", {}).values()
            for v in [str(body.get("bucket", ""))]
            if "data.google_storage_bucket." in v
        ]
        if referenced:
            declared = set(data.get("google_storage_bucket", {}))
            for ref in referenced:
                key = ref.split("data.google_storage_bucket.")[1].split(".")[0]
                assert key in declared, f"referenced but never declared: {key}"

    def test_prefix_owning_product_does_not_take_force_destroy(self):
        """A declared path means the product owns a prefix of a shared
        warehouse root, so whole-bucket force_destroy would let one
        product's destroy take another's data."""
        res = _gcp().emit(_contract(bucket="shared-warehouse", path="products/events"))
        body = next(iter(res["google_storage_bucket"].values()))
        assert "force_destroy" not in body

    def test_whole_bucket_owner_keeps_the_default(self):
        res = _gcp().emit(_contract(bucket="my-own-lake"))
        body = next(iter(res["google_storage_bucket"].values()))
        assert body.get("force_destroy") is True

    def test_iac_and_dbt_agree_when_both_keys_are_present(self):
        """The end-to-end form of the precedence finding."""
        contract = _contract(bucket="lake", path="p", warehouse="gs://elsewhere/prefix")
        res = _gcp().emit(contract)
        created = {b["name"] for b in res.get("google_storage_bucket", {}).values()}
        content = generate_catalogs_yml(contract, _bq_build())
        uri = iceberg_storage_uri(contract["exposes"][0]["binding"], scheme="gs")
        assert f"external_volume: {uri}" in content
        assert uri.split("://", 1)[1].split("/", 1)[0] in created

    def test_foreign_scheme_makes_both_sides_skip_together(self):
        contract = _contract(bucket="lake", warehouse="s3://aws-bucket/p")
        assert "google_storage_bucket" not in _gcp().emit(contract)
        assert generate_catalogs_yml(contract, _bq_build()) is None
