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

    @pytest.mark.parametrize("catalog", ["polaris", "unity", "rest", "nessie"])
    def test_deferred_catalogs_warn_rather_than_error(self, catalog):
        """Understood but not emitted, because their auth is secret-bearing."""
        errors, warnings = validate_iceberg_bindings(_contract("snowflake", catalog=catalog))
        assert not errors
        assert warnings and "credential-free" in warnings[0]

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


class TestGateMatchesEmitterBothWays:
    """The pairing invariant, asserted in both directions."""

    SNOWFLAKE_CASES = [
        {},
        {"warehouse": "s3://lake/p"},
        {"warehouse": "s3://lake/p", "iam_role_arn": "arn:aws:iam::1:role/r"},
        {"warehouse": "gs://lake/p"},
        # F1: a gs:// warehouse ALONGSIDE a bucket. The emitter resolves
        # scheme-first so this is a GCS volume needing no role; the gate
        # used to OR the two and demand one.
        {"warehouse": "gs://lake/p", "bucket": "lake"},
        {"warehouse": "s3://lake/p", "bucket": "other"},
        {"bucket": "lake", "iam_role_arn": "arn:aws:iam::1:role/r"},
        {"catalog": "glue"},
        {"catalog": "glue", "account": "1", "iam_role_arn": "arn:aws:iam::1:role/r"},
    ]

    GCP_CASES = [
        {},
        {"bucket": "lake"},
        {"warehouse": "gs://lake/p"},
        {"warehouse": "s3://aws/p"},
    ]

    @pytest.mark.parametrize("location", SNOWFLAKE_CASES)
    def test_snowflake_error_iff_no_prereq_emitted(self, location):
        contract = _contract("snowflake", **location)
        errors, _ = validate_iceberg_bindings(contract)
        emitted = _emits_prereq(contract, "snowflake")
        assert bool(errors) != emitted, (
            f"gate and emitter disagree for {location}: " f"errors={bool(errors)} emitted={emitted}"
        )

    @pytest.mark.parametrize("location", GCP_CASES)
    def test_gcp_error_iff_no_bucket_emitted(self, location):
        contract = _contract("gcp", **location)
        errors, _ = validate_iceberg_bindings(contract)
        emitted = _emits_prereq(contract, "gcp")
        assert bool(errors) != emitted, (
            f"gate and emitter disagree for {location}: " f"errors={bool(errors)} emitted={emitted}"
        )


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
