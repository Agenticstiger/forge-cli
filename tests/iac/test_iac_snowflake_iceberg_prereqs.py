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

"""Snowflake Iceberg prerequisites: EXTERNAL VOLUME and Glue CATALOG INTEGRATION.

dbt materializes Iceberg tables through catalogs.yml but refuses to create the
infrastructure behind it. These tests pin the IaC half of that loop, and above
all the cross-emitter naming contract: the volume created here must carry
exactly the name the dbt engine's catalogs.yml references.
"""

from __future__ import annotations

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.providers._iceberg_catalog import iceberg_external_volume_name

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _sf():
    return get_iac_plugin("snowflake")


def _contract(exposes):
    return {"id": "silver.demo", "name": "Demo", "exposes": exposes}


def _iceberg_exposure(**location):
    location.setdefault("database", "DB")
    location.setdefault("schema", "PUBLIC")
    location.setdefault("table", "T")
    return {
        "exposeId": "t",
        "binding": {"platform": "snowflake", "format": "iceberg", "location": location},
        "contract": {"schema": [{"name": "ID", "type": "integer"}]},
    }


class TestExternalVolume:
    """Snowflake-managed (Horizon) catalog: dbt's built_in path."""

    def test_emitted_for_managed_iceberg_with_s3_warehouse(self):
        res = _sf().emit(
            _contract(
                [
                    _iceberg_exposure(
                        warehouse="s3://lake/products/demo/",
                        iam_role_arn="arn:aws:iam::123456789012:role/snowflake",
                    )
                ]
            )
        )
        volume = next(iter(res["snowflake_external_volume"].values()))
        location = volume["storage_location"][0]
        assert location["storage_provider"] == "S3"
        assert location["storage_base_url"] == "s3://lake/products/demo/"
        assert location["storage_aws_role_arn"] == "arn:aws:iam::123456789012:role/snowflake"

    def test_volume_name_matches_the_dbt_catalogs_yml_side(self):
        """THE cross-emitter contract. If this drifts, dbt's catalogs.yml
        references a volume that fluid apply never created."""
        contract = _contract(
            [
                _iceberg_exposure(
                    warehouse="s3://lake/p/",
                    iam_role_arn="arn:aws:iam::123456789012:role/r",
                )
            ]
        )
        res = _sf().emit(contract)
        volume = next(iter(res["snowflake_external_volume"].values()))
        binding = contract["exposes"][0]["binding"]
        assert volume["name"] == iceberg_external_volume_name(contract, binding)

    def test_allow_writes_is_pinned_true(self):
        """Snowflake requires TRUE for Iceberg tables it catalogs; the
        provider's tri-state default would leave it unset."""
        res = _sf().emit(
            _contract(
                [
                    _iceberg_exposure(
                        warehouse="s3://lake/p/",
                        iam_role_arn="arn:aws:iam::123456789012:role/r",
                    )
                ]
            )
        )
        volume = next(iter(res["snowflake_external_volume"].values()))
        assert volume["allow_writes"] == "true"

    def test_gcs_warehouse_needs_no_role_arn(self):
        res = _sf().emit(_contract([_iceberg_exposure(warehouse="gs://lake/products/demo/")]))
        location = next(iter(res["snowflake_external_volume"].values()))["storage_location"][0]
        assert location["storage_provider"] == "GCS"
        assert "storage_aws_role_arn" not in location

    def test_bucket_fallback_builds_an_s3_url(self):
        res = _sf().emit(
            _contract(
                [
                    _iceberg_exposure(
                        bucket="lake",
                        path="products/demo",
                        iam_role_arn="arn:aws:iam::123456789012:role/r",
                    )
                ]
            )
        )
        location = next(iter(res["snowflake_external_volume"].values()))["storage_location"][0]
        assert location["storage_base_url"] == "s3://lake/products/demo"

    def test_s3_without_role_arn_emits_no_volume(self):
        """Snowflake rejects an S3 volume without storage_aws_role_arn, so
        emitting one would just move the failure to apply time."""
        res = _sf().emit(_contract([_iceberg_exposure(warehouse="s3://lake/p/")]))
        assert "snowflake_external_volume" not in res

    def test_no_storage_location_emits_no_volume(self):
        res = _sf().emit(_contract([_iceberg_exposure()]))
        assert "snowflake_external_volume" not in res

    def test_storage_location_name_derives_from_the_volume(self):
        contract = _contract(
            [
                _iceberg_exposure(
                    warehouse="s3://lake/p/",
                    iam_role_arn="arn:aws:iam::123456789012:role/r",
                )
            ]
        )
        res = _sf().emit(contract)
        volume = next(iter(res["snowflake_external_volume"].values()))
        # The provider forbids `|`, `.` and `"` in location names.
        name = volume["storage_location"][0]["storage_location_name"]
        assert name == f"{volume['name']}_LOC"
        assert not any(c in name for c in '|."')


class TestGlueCatalogIntegration:
    def test_emitted_when_role_and_account_present(self):
        res = _sf().emit(
            _contract(
                [
                    _iceberg_exposure(
                        catalog="glue",
                        account="123456789012",
                        iam_role_arn="arn:aws:iam::123456789012:role/snowflake-glue",
                    )
                ]
            )
        )
        integration = next(iter(res["snowflake_catalog_integration_aws_glue"].values()))
        assert integration["enabled"] is True
        assert integration["glue_catalog_id"] == "123456789012"
        assert integration["glue_aws_role_arn"].endswith("role/snowflake-glue")

    def test_glue_catalog_emits_no_external_volume(self):
        """The volume is a Snowflake-as-catalog concern; a Glue-cataloged
        table reads through the integration instead."""
        res = _sf().emit(
            _contract(
                [
                    _iceberg_exposure(
                        catalog="glue",
                        account="123456789012",
                        iam_role_arn="arn:aws:iam::123456789012:role/r",
                    )
                ]
            )
        )
        assert "snowflake_external_volume" not in res

    def test_missing_role_or_account_emits_nothing(self):
        for location in (
            {"catalog": "glue", "account": "123456789012"},
            {"catalog": "glue", "iam_role_arn": "arn:aws:iam::123456789012:role/r"},
        ):
            res = _sf().emit(_contract([_iceberg_exposure(**location)]))
            assert "snowflake_catalog_integration_aws_glue" not in res


class TestVolumeConflicts:
    """Security-review findings F1/F2: silent first-expose-wins and
    predicate divergence between the two emitters."""

    def _second_exposure(self, **location):
        exposure = _iceberg_exposure(**location)
        return {**exposure, "exposeId": "t2"}

    def test_two_exposes_same_warehouse_coalesce_into_one_volume(self):
        loc = dict(warehouse="s3://lake/p/", iam_role_arn="arn:aws:iam::123456789012:role/r")
        res = _sf().emit(_contract([_iceberg_exposure(**loc), self._second_exposure(**loc)]))
        assert len(res["snowflake_external_volume"]) == 1

    def test_same_volume_name_with_different_storage_raises(self):
        """First-expose-wins would silently route the second expose's data
        into the first one's bucket. Compliance isolation must fail loud."""
        first = _iceberg_exposure(
            warehouse="s3://bucket-a/p/", iam_role_arn="arn:aws:iam::123456789012:role/r"
        )
        second = self._second_exposure(
            warehouse="s3://bucket-b/p/", iam_role_arn="arn:aws:iam::123456789012:role/r"
        )
        with pytest.raises(ValueError, match="different storage locations"):
            _sf().emit(_contract([first, second]))

    def test_explicit_override_emits_no_create(self):
        """Override semantics are 'I already have a volume': the dbt side
        references it; emitting a CREATE here would collide with the
        operator's own object at apply time."""
        exposure = _iceberg_exposure(
            warehouse="s3://lake/p/", iam_role_arn="arn:aws:iam::123456789012:role/r"
        )
        exposure["binding"]["icebergConfig"] = {"properties": {"external_volume": "MY_VOL"}}
        res = _sf().emit(_contract([exposure]))
        assert "snowflake_external_volume" not in res

    def test_unlisted_catalog_value_is_managed_to_both_emitters(self):
        """Predicate alignment: `catalog: snowflake` is not in the shared
        external set, so dbt emits built_in AND the IaC emits the volume.
        Before the fix the IaC skipped on any truthy catalog value."""
        from fluid_build.engines.dbt.catalogs_yml import generate_catalogs_yml

        loc = dict(
            catalog="snowflake",
            warehouse="s3://lake/p/",
            iam_role_arn="arn:aws:iam::123456789012:role/r",
        )
        contract = _contract([_iceberg_exposure(**loc)])
        res = _sf().emit(contract)
        assert "snowflake_external_volume" in res

        build = {"engine": "dbt", "execution": {"runtime": {"platform": "snowflake"}}}
        content = generate_catalogs_yml(contract, build)
        assert "catalog_type: built_in" in content
        volume = next(iter(res["snowflake_external_volume"].values()))
        assert volume["name"] in content


class TestScopeBoundaries:
    def test_non_iceberg_exposes_emit_no_prereqs(self):
        exposure = _iceberg_exposure(
            warehouse="s3://lake/p/", iam_role_arn="arn:aws:iam::123456789012:role/r"
        )
        exposure["binding"]["format"] = "snowflake_table"
        res = _sf().emit(_contract([exposure]))
        assert "snowflake_external_volume" not in res
        assert "snowflake_catalog_integration_aws_glue" not in res

    @pytest.mark.parametrize("catalog", ["rest", "polaris", "unity", "nessie"])
    def test_secret_bearing_catalogs_are_a_documented_follow_up(self, catalog):
        """Their integrations authenticate with OAuth secrets or bearer
        tokens, and the emitted .tf.json is credential-free by invariant."""
        res = _sf().emit(_contract([_iceberg_exposure(catalog=catalog)]))
        assert "snowflake_catalog_integration_iceberg_rest" not in res

    def test_emitted_module_stays_credential_free(self):
        import json

        res = _sf().emit(
            _contract(
                [
                    _iceberg_exposure(
                        warehouse="s3://lake/p/",
                        iam_role_arn="arn:aws:iam::123456789012:role/r",
                    )
                ]
            )
        )
        serialised = json.dumps(res, default=str).lower()
        for needle in ("password", "secret", "token", "private_key"):
            assert needle not in serialised

    def test_hostile_interpolation_in_location_is_escaped_at_render(self):
        """Contract strings land in .tf.json, which tofu interpolation-
        evaluates. The central render chokepoint must neutralise ${...}
        smuggled through the new volume fields."""
        from fluid_build.iac.module import render_tofu_json

        res = _sf().emit(
            _contract(
                [
                    _iceberg_exposure(
                        warehouse='s3://lake/${file("/etc/passwd")}/',
                        iam_role_arn="arn:aws:iam::1:role/${var.evil}",
                    )
                ]
            )
        )
        import json

        rendered = json.loads(render_tofu_json({"resource": res}))
        volume = next(iter(rendered["resource"]["snowflake_external_volume"].values()))
        location = volume["storage_location"][0]
        # $${ is OpenTofu's literal escape: tofu renders it as a plain ${
        # instead of evaluating it.
        assert location["storage_base_url"] == 's3://lake/$${file("/etc/passwd")}/'
        assert location["storage_aws_role_arn"] == "arn:aws:iam::1:role/$${var.evil}"

    def test_existing_table_emission_is_untouched(self):
        res = _sf().emit(
            _contract(
                [
                    _iceberg_exposure(
                        warehouse="s3://lake/p/",
                        iam_role_arn="arn:aws:iam::123456789012:role/r",
                    )
                ]
            )
        )
        # The database/schema/table shape from _emit_snowflake still emits
        # alongside the new prerequisite resources.
        assert "snowflake_database" in res
        assert "snowflake_schema" in res
