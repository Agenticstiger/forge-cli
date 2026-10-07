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

"""The Iceberg anti-no-op gate must agree with the emitters exactly.

The emitters are emit-when-derivable and therefore silent: a missing input
yields no resource rather than a broken one. This validator is the loud
half. The pairing is the whole point, so it is asserted in BOTH directions:

* every contract the validator rejects must genuinely emit no prerequisite
  (otherwise the gate blocks something that would have worked), and
* every contract the validator accepts must genuinely emit one (otherwise
  the gate waves through a silent no-op, which is the bug it exists for).

The one sanctioned third outcome is a WARNING: a catalog external to
Snowflake whose integration needs a secret, so nothing is emitted on purpose
and the gate says so instead of failing the contract.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.iceberg_validation import validate_iceberg_bindings

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _contract(platform: str, **location) -> Dict[str, Any]:
    location.setdefault("database", "DB")
    location.setdefault("schema", "PUBLIC")
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
                "binding": {"platform": platform, "format": "iceberg", "location": location},
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def _emits_prereq(contract: Dict[str, Any], platform: str) -> bool:
    """Did the emitter actually produce an Iceberg prerequisite resource?"""
    res = get_iac_plugin(platform).emit(contract)
    keys = ("snowflake_external_volume", "snowflake_catalog_integration_aws_glue")
    if platform == "snowflake":
        return any(k in res for k in keys)
    return "google_storage_bucket" in res


class TestSnowflakeGate:
    def test_missing_storage_is_an_error(self):
        errors, _ = validate_iceberg_bindings(_contract("snowflake"))
        assert errors and "warehouse" in errors[0]

    def test_s3_without_role_is_an_error(self):
        errors, _ = validate_iceberg_bindings(_contract("snowflake", warehouse="s3://lake/p"))
        assert errors and "iam_role_arn" in errors[0]

    def test_complete_s3_binding_is_clean(self):
        errors, warnings = validate_iceberg_bindings(
            _contract(
                "snowflake",
                warehouse="s3://lake/p",
                iam_role_arn="arn:aws:iam::123456789012:role/r",
            )
        )
        assert not errors and not warnings

    def test_gcs_backed_volume_needs_no_role(self):
        errors, _ = validate_iceberg_bindings(_contract("snowflake", warehouse="gs://lake/p"))
        assert not errors

    def test_glue_without_role_and_account_is_an_error(self):
        errors, _ = validate_iceberg_bindings(_contract("snowflake", catalog="glue"))
        assert errors
        assert "iam_role_arn" in errors[0] and "account" in errors[0]

    def test_complete_glue_binding_is_clean(self):
        errors, warnings = validate_iceberg_bindings(
            _contract(
                "snowflake",
                catalog="glue",
                account="123456789012",
                iam_role_arn="arn:aws:iam::123456789012:role/r",
            )
        )
        assert not errors and not warnings

    @pytest.mark.parametrize(
        "catalog",
        [
            "polaris",
            "unity",
            "rest",
            "nessie",
            "lakekeeper",
            "bigquery",
            # Spellings fold onto the same row.
            "iceberg_rest",
            "ICEBERG-REST",
            "LakeKeeper",
        ],
    )
    def test_deferred_catalogs_warn_rather_than_error(self, catalog):
        """Understood but not emitted, because their auth is secret-bearing."""
        errors, warnings = validate_iceberg_bindings(_contract("snowflake", catalog=catalog))
        assert not errors
        assert warnings and "credential-free" in warnings[0]

    def test_lakekeeper_warehouse_name_is_not_a_false_error(self):
        """A Lakekeeper warehouse is a catalog NAME. The gate used to send it
        down the Snowflake-managed checks and demand an s3:// or gs://
        warehouse for a table Lakekeeper owns, while the emitter built an
        EXTERNAL VOLUME nothing would ever write to."""
        contract = _contract("snowflake", catalog="lakekeeper", warehouse="demo")
        errors, warnings = validate_iceberg_bindings(contract)
        assert errors == []
        assert len(warnings) == 1 and "'lakekeeper'" in warnings[0]
        assert not _emits_prereq(contract, "snowflake")

    @pytest.mark.parametrize("catalog", ["hive", "jdbc", "hadoop", "dynamodb"])
    def test_catalog_snowflake_cannot_integrate_is_an_error(self, catalog):
        """No CATALOG_SOURCE exists for these, so there is nothing to defer.
        Storage that would satisfy the managed path must not rescue it."""
        contract = _contract(
            "snowflake",
            catalog=catalog,
            warehouse="s3://lake/p",
            iam_role_arn="arn:aws:iam::1:role/r",
        )
        errors, warnings = validate_iceberg_bindings(contract)
        assert warnings == []
        assert len(errors) == 1
        assert f"no catalog integration for a {catalog} catalog" in errors[0]
        assert not _emits_prereq(contract, "snowflake")

    def test_snowflake_alias_takes_the_managed_checks(self):
        """``catalog: snowflake`` is Snowflake-managed, so it needs storage."""
        errors, _ = validate_iceberg_bindings(_contract("snowflake", catalog="snowflake"))
        assert errors and "Snowflake-managed Iceberg table" in errors[0]

    def test_explicit_volume_override_is_clean(self):
        contract = _contract("snowflake")
        contract["exposes"][0]["binding"]["icebergConfig"] = {
            "properties": {"external_volume": "MY_VOL"}
        }
        errors, _ = validate_iceberg_bindings(contract)
        assert not errors


class TestGcpGate:
    def test_missing_bucket_is_an_error(self):
        errors, _ = validate_iceberg_bindings(_contract("gcp"))
        assert errors and "bucket" in errors[0]

    def test_foreign_scheme_names_the_actual_problem(self):
        errors, _ = validate_iceberg_bindings(_contract("gcp", warehouse="s3://aws-bucket/p"))
        assert errors and "backed by GCS" in errors[0]

    def test_bucket_is_clean(self):
        errors, warnings = validate_iceberg_bindings(_contract("gcp", bucket="lake"))
        assert not errors and not warnings

    def test_gs_warehouse_is_clean(self):
        errors, _ = validate_iceberg_bindings(_contract("gcp", warehouse="gs://lake/p"))
        assert not errors

    @pytest.mark.parametrize("catalog", ["lakekeeper", "rest", "polaris", "nessie"])
    def test_external_catalog_name_warehouse_is_clean(self, catalog):
        """An external catalog owns its storage and names a warehouse
        (``demo``). The BigLake "backed by GCS" error used to fire for it."""
        errors, warnings = validate_iceberg_bindings(
            _contract("gcp", catalog=catalog, warehouse="demo")
        )
        assert errors == [] and warnings == []

    @pytest.mark.parametrize("warehouse", ["s3://aws-bucket/p", "abfss://c@acct.dfs/p"])
    def test_external_catalog_on_foreign_storage_is_one_error(self, warehouse):
        errors, _ = validate_iceberg_bindings(
            _contract("gcp", catalog="lakekeeper", warehouse=warehouse)
        )
        assert len(errors) == 1
        assert "not Google Cloud Storage" in errors[0]
        assert "lakekeeper catalog" in errors[0]
        assert "BigQuery" not in errors[0]

    def test_external_catalog_on_gcs_still_gets_its_bucket(self):
        contract = _contract("gcp", catalog="lakekeeper", warehouse="gs://lake/p")
        assert validate_iceberg_bindings(contract) == ([], [])
        assert _emits_prereq(contract, "gcp")

    @pytest.mark.parametrize("location", [{}, {"catalog": "bigquery"}, {"catalog": "BigQuery"}])
    def test_biglake_messages_are_unchanged(self, location):
        """Absent and ``bigquery`` keep the BigLake checks byte for byte."""
        errors, _ = validate_iceberg_bindings(
            _contract("gcp", warehouse="s3://aws-bucket/p", **location)
        )
        assert errors == [
            "expose 'events': binding.location.warehouse is 's3://aws-bucket/p', but a "
            "BigQuery Iceberg table is backed by GCS. Use a gs:// warehouse or "
            "binding.location.bucket so FLUID can create the bucket dbt's "
            "catalogs.yml points at."
        ]


class TestCatalogKindGate:
    """An unknown kind is refused once, on every platform.

    Every emitter keeps a fallback for a value it does not know and the
    fallbacks disagree (REST for the streaming sink, Snowflake-managed for
    dbt and the Snowflake IaC), so the gate refuses the value itself.
    """

    @pytest.mark.parametrize("platform", ["snowflake", "gcp", "aws", "local"])
    def test_unknown_kind_is_one_error_listing_the_known_ones(self, platform):
        errors, warnings = validate_iceberg_bindings(_contract(platform, catalog="lakekeper"))
        assert warnings == []
        assert len(errors) == 1, errors
        assert "'lakekeper' is not a catalog kind" in errors[0]
        for known in ("lakekeeper", "glue", "rest", "snowflake-managed"):
            assert known in errors[0]

    def test_unknown_kind_on_snowflake_skips_the_managed_noise(self):
        """A mistyped external catalog needs no EXTERNAL VOLUME storage, so
        the managed "needs a warehouse" error would read as a second problem."""
        errors, _ = validate_iceberg_bindings(_contract("snowflake", catalog="horizon"))
        assert len(errors) == 1 and "not a catalog kind" in errors[0]

    def test_unknown_kind_on_gcp_skips_the_storage_noise(self):
        errors, _ = validate_iceberg_bindings(
            _contract("gcp", catalog="horizon", warehouse="s3://aws/p")
        )
        assert len(errors) == 1 and "not a catalog kind" in errors[0]

    @pytest.mark.parametrize(
        "catalog", ["glue", "rest", "iceberg_rest", "lakekeeper", "snowflake", "Snowflake_Managed"]
    )
    def test_known_kinds_and_aliases_pass(self, catalog):
        errors, _ = validate_iceberg_bindings(_contract("aws", catalog=catalog, bucket="lake"))
        assert errors == []

    def test_the_sinks_format_spelling_is_checked(self):
        """``iceberg-table`` is Iceberg to the streaming sink, which reads the
        catalog, so the gate reads it too."""
        contract = _contract("aws", catalog="horizon")
        contract["exposes"][0]["binding"]["format"] = "iceberg-table"
        errors, _ = validate_iceberg_bindings(contract)
        assert errors and "not a catalog kind" in errors[0]

    def test_non_iceberg_expose_is_not_checked(self):
        contract = _contract("aws", catalog="horizon")
        contract["exposes"][0]["binding"]["format"] = "parquet"
        assert validate_iceberg_bindings(contract) == ([], [])

    def test_confluent_is_left_to_its_own_gate(self):
        """validate_confluent_binding refuses every non-Glue catalog; listing
        the REST kinds here would suggest values that gate rejects too."""
        from fluid_build.iac.providers.confluent import validate_confluent_binding

        contract = _contract("confluent", catalog="horizon")
        assert validate_iceberg_bindings(contract) == ([], [])
        cf_errors, _ = validate_confluent_binding(contract)
        assert any("publishes only to AWS Glue" in e for e in cf_errors)


class TestGateMatchesEmitterBothWays:
    """The pairing invariant, asserted in both directions.

    Restated for the kind table: every KNOWN catalog kind lands in exactly
    one outcome. An error means nothing was emitted and the user can fix it;
    a warning means nothing was emitted on purpose (a catalog external to
    Snowflake whose integration needs a secret); clean means the
    prerequisite was emitted. Before the table, ``catalog: lakekeeper`` with
    a name warehouse was BOTH rejected by the gate and given a volume by the
    emitter once storage was added, and ``catalog: hive`` was waved through
    to a volume Snowflake could never use. An unknown kind is refused
    outright (see TestCatalogKindGate) and is not part of this pairing.
    """

    _ROLE = "arn:aws:iam::1:role/r"

    SNOWFLAKE_CASES = [
        {},
        {"warehouse": "s3://lake/p"},
        {"warehouse": "s3://lake/p", "iam_role_arn": _ROLE},
        {"warehouse": "gs://lake/p"},
        # F1: a gs:// warehouse ALONGSIDE a bucket. The emitter resolves
        # scheme-first so this is a GCS volume needing no role; the gate
        # used to OR the two and demand one.
        {"warehouse": "gs://lake/p", "bucket": "lake"},
        {"warehouse": "s3://lake/p", "bucket": "other"},
        {"bucket": "lake", "iam_role_arn": _ROLE},
        {"catalog": "glue"},
        {"catalog": "glue", "account": "1", "iam_role_arn": _ROLE},
        {"catalog": "GLUE", "account": "1", "iam_role_arn": _ROLE},
        # Deferred external kinds: warned, never emitted, storage or not.
        {"catalog": "lakekeeper", "warehouse": "demo"},
        {"catalog": "lakekeeper", "warehouse": "s3://lake/p", "iam_role_arn": _ROLE},
        {"catalog": "iceberg_rest", "warehouse": "s3://lake/p", "iam_role_arn": _ROLE},
        {"catalog": "polaris"},
        {"catalog": "bigquery", "warehouse": "gs://lake/p"},
        # No Snowflake integration exists: refused, never emitted.
        {"catalog": "hive", "warehouse": "s3://lake/p", "iam_role_arn": _ROLE},
        {"catalog": "dynamodb"},
        # ``snowflake`` is the Snowflake-managed alias: the volume path.
        {"catalog": "snowflake"},
        {"catalog": "snowflake", "warehouse": "s3://lake/p", "iam_role_arn": _ROLE},
    ]

    #: Absent and ``bigquery``: the BigLake path, error iff no bucket.
    GCP_BIGLAKE_CASES = [
        {},
        {"bucket": "lake"},
        {"warehouse": "gs://lake/p"},
        {"warehouse": "s3://aws/p"},
        {"catalog": "bigquery"},
        {"catalog": "bigquery", "warehouse": "gs://lake/p"},
    ]

    #: An external catalog owns its storage: ``(location, error, bucket)``.
    GCP_EXTERNAL_CASES = [
        ({"catalog": "lakekeeper", "warehouse": "demo"}, False, False),
        ({"catalog": "lakekeeper"}, False, False),
        ({"catalog": "lakekeeper", "warehouse": "gs://lake/p"}, False, True),
        ({"catalog": "rest", "bucket": "lake"}, False, True),
        ({"catalog": "lakekeeper", "warehouse": "s3://aws/p"}, True, False),
        ({"catalog": "nessie", "warehouse": "abfss://c@a.dfs/p"}, True, False),
    ]

    @pytest.mark.parametrize("location", SNOWFLAKE_CASES)
    def test_snowflake_exactly_one_outcome(self, location):
        contract = _contract("snowflake", **location)
        errors, warnings = validate_iceberg_bindings(contract)
        emitted = _emits_prereq(contract, "snowflake")
        outcomes = {"error": bool(errors), "warning": bool(warnings), "emitted": emitted}
        assert sum(outcomes.values()) == 1, f"gate and emitter disagree for {location}: {outcomes}"

    @pytest.mark.parametrize("location", GCP_BIGLAKE_CASES)
    def test_gcp_error_iff_no_bucket_emitted(self, location):
        contract = _contract("gcp", **location)
        errors, _ = validate_iceberg_bindings(contract)
        emitted = _emits_prereq(contract, "gcp")
        assert bool(errors) != emitted, (
            f"gate and emitter disagree for {location}: " f"errors={bool(errors)} emitted={emitted}"
        )

    @pytest.mark.parametrize("location,error,bucket", GCP_EXTERNAL_CASES)
    def test_gcp_external_catalog_never_errors_and_emits(self, location, error, bucket):
        contract = _contract("gcp", **location)
        errors, _ = validate_iceberg_bindings(contract)
        emitted = _emits_prereq(contract, "gcp")
        assert (bool(errors), emitted) == (error, bucket), location
        assert not (errors and emitted)

    def test_unknown_kind_is_refused_while_the_emitter_keeps_its_fallback(self):
        """The one case outside the pairing, by design. The Snowflake IaC
        keeps dbt's ``built_in`` fallback (so dbt never references a volume
        that does not exist), and the gate refuses the value, so a validated
        contract never reaches that fallback."""
        contract = _contract(
            "snowflake", catalog="horizon", warehouse="s3://lake/p", iam_role_arn=self._ROLE
        )
        errors, _ = validate_iceberg_bindings(contract)
        assert errors and "not a catalog kind" in errors[0]
        assert _emits_prereq(contract, "snowflake")


class TestScope:
    def test_non_iceberg_exposes_are_ignored(self):
        contract = _contract("snowflake")
        contract["exposes"][0]["binding"]["format"] = "snowflake_table"
        assert validate_iceberg_bindings(contract) == ([], [])

    def test_other_platforms_are_ignored(self):
        contract = _contract("aws", bucket="lake")
        assert validate_iceberg_bindings(contract) == ([], [])

    def test_contract_with_no_exposes(self):
        assert validate_iceberg_bindings({"id": "x", "exposes": []}) == ([], [])

    def test_every_message_names_the_expose(self):
        errors, _ = validate_iceberg_bindings(_contract("gcp"))
        assert all("expose 'events'" in m for m in errors)


class TestReviewFindings:
    """Regression pins for the divergences found by adversarial review."""

    def test_gs_warehouse_with_a_bucket_is_not_treated_as_s3(self):
        """F1, user-blocking false positive.

        The emitter resolves storage scheme-first, so a gs:// warehouse is a
        GCS volume however many bucket keys sit beside it. The gate ORed the
        two and demanded an iam_role_arn the emitter never uses, rejecting a
        contract that emits perfectly.
        """
        contract = _contract("snowflake", warehouse="gs://lake/p", bucket="lake")
        errors, _ = validate_iceberg_bindings(contract)
        assert not errors
        assert _emits_prereq(contract, "snowflake")

    def test_shared_scheme_table_is_the_single_source(self):
        """Both sides must read the same table, or a new scheme desyncs them."""
        from fluid_build.iac.providers.snowflake import _STORAGE_PROVIDERS
        from fluid_build.providers._iceberg_catalog import STORAGE_PROVIDERS

        assert _STORAGE_PROVIDERS is STORAGE_PROVIDERS

    def test_uppercase_format_is_honoured_by_gcp_emitter_and_gate(self):
        """F2: the GCP dispatch was case-sensitive while the gate lowercased."""
        contract = _contract("gcp", bucket="lake")
        contract["exposes"][0]["binding"]["format"] = "ICEBERG"
        errors, _ = validate_iceberg_bindings(contract)
        assert not errors
        assert _emits_prereq(contract, "gcp")

    def test_illegal_override_name_is_caught_at_validate(self):
        """F4a: the emitters raise on this mid-emit; catch it earlier."""
        contract = _contract("snowflake")
        contract["exposes"][0]["binding"]["icebergConfig"] = {
            "properties": {"external_volume": "bad-name!"}
        }
        errors, _ = validate_iceberg_bindings(contract)
        assert errors and "legal Snowflake identifier" in errors[0]

    def test_colliding_volumes_are_caught_at_validate(self):
        """F4b: the emitter raises, but only at apply. Its own comment says
        this failure must never be quiet, so reject it at validate."""
        contract = _contract(
            "snowflake", warehouse="s3://lake-a/p", iam_role_arn="arn:aws:iam::1:role/r"
        )
        second = {
            "exposeId": "events2",
            "kind": "table",
            "binding": {
                "platform": "snowflake",
                "format": "iceberg",
                "location": {
                    "database": "DB",
                    "schema": "PUBLIC",
                    "table": "T2",
                    "warehouse": "s3://lake-b/p",
                    "iam_role_arn": "arn:aws:iam::1:role/r",
                },
            },
            "contract": {"schema": [{"name": "id", "type": "string"}]},
        }
        contract["exposes"].append(second)
        errors, _ = validate_iceberg_bindings(contract)
        assert errors and "different storage" in errors[0]

    @staticmethod
    def _two_exposes(catalog: str) -> Dict[str, Any]:
        """Two exposes, one product id, so one derived volume name, on two buckets."""
        contract = _contract(
            "snowflake",
            catalog=catalog,
            warehouse="s3://lake-a/p",
            iam_role_arn="arn:aws:iam::1:role/r",
        )
        second = {
            "exposeId": "events2",
            "kind": "table",
            "binding": {
                "platform": "snowflake",
                "format": "iceberg",
                "location": {
                    "database": "DB",
                    "schema": "PUBLIC",
                    "table": "T2",
                    "catalog": catalog,
                    "warehouse": "s3://lake-b/p",
                    "iam_role_arn": "arn:aws:iam::1:role/r",
                },
            },
            "contract": {"schema": [{"name": "id", "type": "string"}]},
        }
        contract["exposes"].append(second)
        return contract

    def test_snowflake_alias_collision_is_caught_at_validate(self):
        """The collision check skipped ANY non-empty catalog, but the emitter
        builds volumes for ``catalog: snowflake``: validate passed and apply
        raised mid-emit. Both sides now read the same ``built_in`` row."""
        contract = self._two_exposes("snowflake")
        errors, _ = validate_iceberg_bindings(contract)
        assert any("different storage" in e for e in errors)
        with pytest.raises(ValueError, match="different storage locations"):
            get_iac_plugin("snowflake").emit(contract)

    def test_external_catalog_exposes_never_collide(self):
        """No volume is built for a Lakekeeper table, so there is nothing to
        collide: the gate must not invent the error the emitter cannot hit."""
        contract = self._two_exposes("lakekeeper")
        errors, _ = validate_iceberg_bindings(contract)
        assert not any("different storage" in e for e in errors)
        assert "snowflake_external_volume" not in get_iac_plugin("snowflake").emit(contract)

    def test_same_volume_same_storage_is_fine(self):
        contract = _contract(
            "snowflake", warehouse="s3://lake/p", iam_role_arn="arn:aws:iam::1:role/r"
        )
        second = dict(contract["exposes"][0])
        second = {**second, "exposeId": "events2"}
        contract["exposes"].append(second)
        errors, _ = validate_iceberg_bindings(contract)
        assert not errors

    def test_bucketless_gs_warehouse_says_what_is_wrong(self):
        """F6: the old message told the user to supply what they had supplied."""
        errors, _ = validate_iceberg_bindings(_contract("gcp", warehouse="gs://"))
        assert errors and "names no bucket" in errors[0]
