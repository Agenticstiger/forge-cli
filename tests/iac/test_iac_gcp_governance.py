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

"""GCP governance: retention, CMEK, column restrictions and mapped principals, rendered.

Before this, a gcp binding's ``lifecycle.expire``, ``binding.encryption`` and
``policy.authz.columnRestrictions`` produced no resource and no warning (the
module was byte-identical with and without them), and the contract's principals
were written as given into the dataset's AUTHORITATIVE ``access`` list. These
tests pin what the emitter writes now, what it refuses, and that ``tofu validate``
accepts each shape (``test_iac_gcp_governance_plan.py`` runs the real provider's
plan on a live table).
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.governance_validation import validate_governance
from fluid_build.iac.providers import gcp_governance as gov

CID = "gold_retention_candidates"
TABLE = f"google_bigquery_table.{CID}_retention_candidates"
PLATFORM = "group:data-platform@northwind.example"
ANALYSTS = "group:analysts@northwind.example"
PIPELINE = "serviceAccount:fluid-pipeline@northwind.example"
MAPPING = {
    PLATFORM: "group:data-platform@northwind.com",
    ANALYSTS: "group:analysts@northwind.com",
    PIPELINE: "serviceAccount:fluid-pipeline@northwind-demo.iam.gserviceaccount.com",
}


def _contract(
    *,
    principals: Optional[Dict[str, Any]] = None,
    lifecycle: Optional[Dict[str, Any]] = None,
    encryption: Optional[Dict[str, Any]] = None,
    restrictions: Optional[List[Dict[str, Any]]] = None,
    partition_by: Optional[List[str]] = None,
    fmt: str = "bigquery_table",
    region: str = "europe-west1",
) -> Dict[str, Any]:
    location: Dict[str, Any] = {
        "project": "northwind-demo",
        "dataset": "demo_gold",
        "table": "retention_candidates",
        "region": region,
    }
    if partition_by is not None:
        location["partitionBy"] = partition_by
    binding: Dict[str, Any] = {"platform": "gcp", "format": fmt, "location": location}
    if principals is not None:
        binding["principals"] = principals
    if encryption is not None:
        binding["encryption"] = encryption
    exposure: Dict[str, Any] = {
        "exposeId": "candidates",
        "binding": binding,
        "contract": {
            "schema": [
                {"name": "customer_id", "type": "VARCHAR", "required": True},
                {"name": "status", "type": "VARCHAR"},
                {"name": "msisdn", "type": "VARCHAR", "description": "salted ${hash}"},
                {"name": "created_at", "type": "timestamp"},
                {"name": "cohort_size", "type": "BIGINT"},
            ]
        },
    }
    if lifecycle is not None:
        exposure["lifecycle"] = lifecycle
    if restrictions is not None:
        exposure["policy"] = {"authz": {"columnRestrictions": restrictions}}
    return {
        "fluidVersion": "0.7.6",
        "id": "gold.retention_candidates",
        "accessPolicy": {
            "grants": [
                {"principal": PLATFORM, "permissions": ["read", "select", "query"]},
                {"principal": ANALYSTS, "permissions": ["read"]},
                {"principal": PIPELINE, "permissions": ["read", "write", "insert"]},
            ]
        },
        "exposes": [exposure],
    }


def _module(contract: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(build_module(get_iac_plugin("gcp"), contract))


def _resources(contract: Dict[str, Any]) -> Dict[str, Any]:
    return _module(contract).get("resource") or {}


def _refusal(contract: Dict[str, Any]) -> UnsupportedBindingError:
    with pytest.raises(UnsupportedBindingError) as raised:
        build_module(get_iac_plugin("gcp"), contract)
    return raised.value


# ── D9: logical principals, mapped; non-authoritative dataset grants ─────


class TestPrincipals:
    def test_the_demo_placeholders_are_refused_not_written_into_iam(self):
        """The verification's finding: ``@northwind.example`` became an authoritative ACL."""
        error = _refusal(_contract())
        assert error.kind == "principal-placeholder"
        assert "northwind.example" in str(error)
        assert "binding.principals" in " ".join(error.remediation)

    def test_mapped_principals_become_non_authoritative_dataset_members(self):
        res = _resources(_contract(principals=MAPPING))
        dataset = res["google_bigquery_dataset"][f"{CID}_demo_gold"]
        assert "access" not in dataset
        pairs = {
            (m["role"], m["member"]) for m in res["google_bigquery_dataset_iam_member"].values()
        }
        assert pairs == {
            ("roles/bigquery.dataViewer", "group:data-platform@northwind.com"),
            ("roles/bigquery.dataViewer", "group:analysts@northwind.com"),
            (
                "roles/bigquery.dataViewer",
                "serviceAccount:fluid-pipeline@northwind-demo.iam.gserviceaccount.com",
            ),
            (
                "roles/bigquery.dataEditor",
                "serviceAccount:fluid-pipeline@northwind-demo.iam.gserviceaccount.com",
            ),
        }
        assert "northwind.example" not in json.dumps(res)
        for member in res["google_bigquery_dataset_iam_member"].values():
            assert member["project"] == "northwind-demo"
            assert (
                member["dataset_id"] == f"${{google_bigquery_dataset.{CID}_demo_gold.dataset_id}}"
            )

    def test_an_unmapped_principal_is_refused_once_a_binding_maps_principals(self):
        mapping = {k: v for k, v in MAPPING.items() if k != ANALYSTS}
        error = _refusal(_contract(principals=mapping))
        assert error.kind == "principal-unmapped"
        assert ANALYSTS in str(error)

    def test_a_principal_mapped_to_nothing_is_granted_nothing(self):
        res = _resources(_contract(principals={**MAPPING, ANALYSTS: []}))
        members = {m["member"] for m in res["google_bigquery_dataset_iam_member"].values()}
        assert "group:analysts@northwind.com" not in members
        assert len(members) == 2

    def test_a_principal_may_map_to_several_identities(self):
        res = _resources(
            _contract(
                principals={
                    **MAPPING,
                    PLATFORM: ["group:dp-eu@northwind.com", "group:dp-us@northwind.com"],
                }
            )
        )
        members = {m["member"] for m in res["google_bigquery_dataset_iam_member"].values()}
        assert {"group:dp-eu@northwind.com", "group:dp-us@northwind.com"} <= members

    @pytest.mark.parametrize(
        "identity",
        [
            "arn:aws:iam::111111111111:role/analyst",
            "data-platform@northwind.com",
            "group:",
            "group:not-an-email",
        ],
    )
    def test_a_mapped_identity_that_is_not_an_iam_member_is_refused(self, identity):
        error = _refusal(_contract(principals={**MAPPING, PLATFORM: identity}))
        assert error.kind == "principal-invalid"

    def test_an_env_template_in_an_identity_validates_as_it_will_emit(self):
        """``fluid validate`` does not resolve ``{{ env.* }}``; apply resolves it first."""
        templated = (
            "serviceAccount:fluid-pipeline@{{ env.FLUID_DEMO_GCP_PROJECT }}.iam.gserviceaccount.com"
        )
        contract = _contract(principals={**MAPPING, PIPELINE: templated})
        assert validate_governance(contract) == ([], [])
        members = {
            m["member"] for m in _resources(contract)["google_bigquery_dataset_iam_member"].values()
        }
        assert templated in members

    def test_a_placeholder_mapped_to_a_placeholder_is_still_refused(self):
        error = _refusal(_contract(principals={**MAPPING, PLATFORM: "group:x@corp.test"}))
        assert error.kind == "principal-placeholder"

    def test_a_contract_without_a_mapping_keeps_its_real_principals(self):
        contract = _contract()
        contract["accessPolicy"]["grants"] = [
            {"principal": "group:analysts@company.com", "permissions": ["read"]}
        ]
        res = _resources(contract)
        (member,) = res["google_bigquery_dataset_iam_member"].values()
        assert member["member"] == "group:analysts@company.com"

    def test_a_logical_key_matches_whatever_case_its_prefix_is_written_in(self):
        contract = _contract(
            principals={
                "ServiceAccount:fluid-pipeline@northwind.example": MAPPING[PIPELINE],
                PLATFORM: MAPPING[PLATFORM],
                ANALYSTS: MAPPING[ANALYSTS],
            }
        )
        assert _resources(contract)["google_bigquery_dataset_iam_member"]

    def test_gcs_grants_are_mapped_too(self):
        contract = _contract(principals=MAPPING, fmt="gcs_bucket")
        contract["exposes"][0]["binding"]["location"] = {"bucket": "northwind-demo-lake"}
        res = _resources(contract)
        members = {m["member"] for m in res["google_storage_bucket_iam_member"].values()}
        assert "group:analysts@northwind.com" in members
        assert "northwind.example" not in json.dumps(res)

    def test_a_pubsub_expose_carries_no_grant_and_is_not_refused(self):
        contract = _contract(fmt="pubsub_topic")
        contract["exposes"][0]["binding"]["location"] = {"topic": "events"}
        assert "google_pubsub_topic" in _resources(contract)


# ── F7: retention as partition expiration ────────────────────────────────


class TestRetention:
    def test_expire_becomes_daily_ingestion_time_partitions_that_expire(self):
        res = _resources(
            _contract(principals=MAPPING, lifecycle={"retention": "P90D", "expire": True})
        )
        table = res["google_bigquery_table"][f"{CID}_retention_candidates"]
        assert table["time_partitioning"] == {"type": "DAY", "expiration_ms": 90 * 86_400_000}
        # Never a whole-table TTL, which would delete the product.
        assert "expiration_time" not in table
        trigger = f"{CID}_retention_candidates_partitioning"
        assert table["lifecycle"] == {"replace_triggered_by": [f"terraform_data.{trigger}"]}
        assert res["terraform_data"][trigger] == {"input": {"type": "DAY", "field": ""}}

    def test_a_declared_partition_column_is_the_partition_field(self):
        res = _resources(
            _contract(
                principals=MAPPING,
                lifecycle={"retention": "P7Y", "expire": True},
                partition_by=["created_at"],
            )
        )
        table = res["google_bigquery_table"][f"{CID}_retention_candidates"]
        assert table["time_partitioning"] == {
            "type": "DAY",
            "field": "created_at",
            "expiration_ms": 7 * 365 * 86_400_000,
        }
        (trigger,) = res["terraform_data"].values()
        assert trigger["input"] == {"type": "DAY", "field": "created_at"}

    def test_the_trigger_holds_the_shape_not_the_period(self):
        """A new retention must be an in-place change, not a replacement."""
        a = _resources(
            _contract(principals=MAPPING, lifecycle={"retention": "P30D", "expire": True})
        )
        b = _resources(
            _contract(principals=MAPPING, lifecycle={"retention": "P90D", "expire": True})
        )
        assert a["terraform_data"] == b["terraform_data"]

    def test_a_part_day_rounds_up_so_nothing_expires_early(self):
        res = _resources(
            _contract(principals=MAPPING, lifecycle={"retention": "PT36H", "expire": True})
        )
        table = res["google_bigquery_table"][f"{CID}_retention_candidates"]
        assert table["time_partitioning"]["expiration_ms"] == 2 * 86_400_000

    def test_retention_without_expire_stays_a_declaration(self):
        res = _resources(_contract(principals=MAPPING, lifecycle={"retention": "P90D"}))
        table = res["google_bigquery_table"][f"{CID}_retention_candidates"]
        assert "time_partitioning" not in table and "lifecycle" not in table
        assert "terraform_data" not in res

    @pytest.mark.parametrize(
        "partition_by, why",
        [
            (["status"], "not a DATE, TIMESTAMP or DATETIME"),
            (["created_at", "status"], "at most one column"),
            (["missing"], "not a DATE, TIMESTAMP or DATETIME"),
        ],
    )
    def test_an_unusable_partition_column_is_refused(self, partition_by, why):
        error = _refusal(
            _contract(
                principals=MAPPING,
                lifecycle={"retention": "P30D", "expire": True},
                partition_by=partition_by,
            )
        )
        assert error.kind == "retention-partition-column"
        assert why in str(error)

    @pytest.mark.parametrize("lifecycle", [{"expire": True}, {"expire": True, "retention": "30"}])
    def test_expire_without_a_usable_period_is_refused(self, lifecycle):
        assert (
            _refusal(_contract(principals=MAPPING, lifecycle=lifecycle)).kind == "retention-period"
        )

    def test_expire_on_a_view_is_refused(self):
        contract = _contract(principals=MAPPING, lifecycle={"retention": "P30D", "expire": True})
        contract["exposes"][0]["binding"]["format"] = "bigquery_view"
        assert _refusal(contract).kind == "retention-view"

    def test_expire_on_a_gcs_expose_is_refused_not_dropped(self):
        contract = _contract(
            principals=MAPPING, lifecycle={"retention": "P30D", "expire": True}, fmt="gcs_bucket"
        )
        contract["exposes"][0]["binding"]["location"] = {"bucket": "lake"}
        error = _refusal(contract)
        assert error.kind == "gcp-governance-unsupported-target"
        assert "lifecycle.expire" in str(error)


# ── F6: customer-managed encryption ──────────────────────────────────────


class TestEncryption:
    def test_a_product_key_is_created_granted_and_used_by_dataset_and_table(self):
        contract = _contract(principals=MAPPING, encryption={"kms": "product"})
        module = _module(contract)
        res = module["resource"]
        ident = f"{CID}_demo_gold_kms"
        assert res["google_kms_key_ring"][ident] == {
            "name": "fluid-gold_retention_candidates-demo_gold",
            "location": "europe-west1",
            "project": "northwind-demo",
        }
        key = res["google_kms_crypto_key"][ident]
        assert key["rotation_period"] == "7776000s"
        assert key["purpose"] == "ENCRYPT_DECRYPT"
        assert key["key_ring"] == f"${{google_kms_key_ring.{ident}.id}}"
        grant = res["google_kms_crypto_key_iam_member"][ident]
        assert grant["role"] == "roles/cloudkms.cryptoKeyEncrypterDecrypter"
        assert grant["member"] == (
            f"serviceAccount:${{data.google_bigquery_default_service_account.{ident}.email}}"
        )
        assert module["data"]["google_bigquery_default_service_account"][ident] == {
            "project": "northwind-demo"
        }
        ref = f"${{google_kms_crypto_key.{ident}.id}}"
        dataset = res["google_bigquery_dataset"][f"{CID}_demo_gold"]
        table = res["google_bigquery_table"][f"{CID}_retention_candidates"]
        assert dataset["default_encryption_configuration"] == {"kms_key_name": ref}
        assert table["encryption_configuration"] == {"kms_key_name": ref}
        assert table["depends_on"] == [f"google_kms_crypto_key_iam_member.{ident}"]
        assert dataset["depends_on"] == [f"google_kms_crypto_key_iam_member.{ident}"]

    @pytest.mark.parametrize("region, location", [("EU", "europe"), ("US", "us")])
    def test_a_multi_region_dataset_uses_the_matching_key_location(self, region, location):
        res = _resources(
            _contract(principals=MAPPING, encryption={"kms": "product"}, region=region)
        )
        (ring,) = res["google_kms_key_ring"].values()
        assert ring["location"] == location

    def test_an_existing_key_is_named_as_given(self):
        key = "projects/sec/locations/europe-west1/keyRings/shared/cryptoKeys/gold"
        module = _module(_contract(principals=MAPPING, encryption={"kms": key}))
        res = module["resource"]
        assert "google_kms_key_ring" not in res and "data" not in module
        table = res["google_bigquery_table"][f"{CID}_retention_candidates"]
        assert table["encryption_configuration"] == {"kms_key_name": key}

    def test_a_region_that_is_not_a_location_never_reaches_the_key_import_id(self):
        contract = _contract(
            principals=MAPPING,
            encryption={"kms": "product"},
            region="europe-west1/keyRings/other/../../../projects/victim",
        )
        assert _refusal(contract).kind == "encryption-kms-location"
        with pytest.raises(UnsupportedBindingError):
            get_iac_plugin("gcp").discover_imports(contract)

    def test_an_existing_key_in_another_location_is_refused(self):
        key = "projects/sec/locations/us-central1/keyRings/shared/cryptoKeys/gold"
        error = _refusal(_contract(principals=MAPPING, encryption={"kms": key}))
        assert error.kind == "encryption-kms-location"

    @pytest.mark.parametrize(
        "kms", ["alias/fluid/gold", "arn:aws:kms:eu-north-1:111111111111:key/abc", "gold-key"]
    )
    def test_an_aws_or_malformed_key_is_refused_on_gcp(self, kms):
        error = _refusal(_contract(principals=MAPPING, encryption={"kms": kms}))
        assert error.kind == "encryption-kms"
        if kms.startswith(("alias/", "arn:")):
            assert "an AWS KMS key, on a gcp binding" in str(error)

    def test_none_means_google_managed_keys(self):
        res = _resources(_contract(principals=MAPPING, encryption={"kms": "none"}))
        assert "google_kms_crypto_key" not in res
        assert (
            "encryption_configuration"
            not in res["google_bigquery_table"][f"{CID}_retention_candidates"]
        )

    def test_a_product_key_ring_and_key_are_adopted_on_re_apply(self):
        """Neither can be deleted on GCP, so a re-apply after a destroy must import them."""
        blocks = get_iac_plugin("gcp").discover_imports(
            _contract(principals=MAPPING, encryption={"kms": "product"})
        )
        ids = {b.to: b.id for b in blocks}
        ring = "projects/northwind-demo/locations/europe-west1/keyRings/fluid-gold_retention_candidates-demo_gold"
        assert ids[f"google_kms_key_ring.{CID}_demo_gold_kms"] == ring
        assert ids[f"google_kms_crypto_key.{CID}_demo_gold_kms"] == f"{ring}/cryptoKeys/bigquery"

    def test_encryption_on_a_gcs_expose_is_refused_not_dropped(self):
        contract = _contract(principals=MAPPING, encryption={"kms": "product"}, fmt="gcs_bucket")
        contract["exposes"][0]["binding"]["location"] = {"bucket": "lake"}
        assert _refusal(contract).kind == "gcp-governance-unsupported-target"


# ── F8: column restrictions as policy tags ───────────────────────────────


class TestColumnRestrictions:
    DENY = [{"principal": ANALYSTS, "columns": ["customer_id", "msisdn"], "access": "deny"}]

    def test_denied_columns_get_a_policy_tag_read_by_every_other_reader(self):
        res = _resources(_contract(principals=MAPPING, restrictions=self.DENY))
        taxonomy = res["google_data_catalog_taxonomy"][f"{CID}_demo_gold_taxonomy"]
        assert taxonomy["activated_policy_types"] == ["FINE_GRAINED_ACCESS_CONTROL"]
        assert taxonomy["region"] == "europe-west1"
        assert taxonomy["project"] == "northwind-demo"
        (tag_key,) = res["google_data_catalog_policy_tag"]
        tag = res["google_data_catalog_policy_tag"][tag_key]
        assert tag["taxonomy"] == f"${{google_data_catalog_taxonomy.{CID}_demo_gold_taxonomy.id}}"
        readers = {
            (m["role"], m["member"], m["policy_tag"])
            for m in res["google_data_catalog_policy_tag_iam_member"].values()
        }
        ref = f"${{google_data_catalog_policy_tag.{tag_key}.name}}"
        assert readers == {
            (
                "roles/datacatalog.categoryFineGrainedReader",
                "group:data-platform@northwind.com",
                ref,
            ),
            (
                "roles/datacatalog.categoryFineGrainedReader",
                "serviceAccount:fluid-pipeline@northwind-demo.iam.gserviceaccount.com",
                ref,
            ),
        }
        schema = json.loads(res["google_bigquery_table"][f"{CID}_retention_candidates"]["schema"])
        tagged = {f["name"]: f.get("policyTags") for f in schema}
        assert tagged["customer_id"] == {"names": [ref]}
        assert tagged["msisdn"] == {"names": [ref]}
        assert tagged["status"] is None and tagged["cohort_size"] is None

    def test_the_schema_still_escapes_contract_text_while_the_tag_is_interpolated(self):
        text = build_module(
            get_iac_plugin("gcp"), _contract(principals=MAPPING, restrictions=self.DENY)
        )
        schema = json.loads(text)["resource"]["google_bigquery_table"][
            f"{CID}_retention_candidates"
        ]["schema"]
        # A description's ${...} stays literal ($${) as everywhere else in the module.
        assert "salted $${hash}" in schema
        assert "${google_data_catalog_policy_tag." in schema

    def test_columns_with_different_readers_get_different_tags(self):
        restrictions = [
            {"principal": ANALYSTS, "columns": ["msisdn"], "access": "deny"},
            {"principal": ANALYSTS, "columns": ["customer_id"], "access": "deny"},
            {"principal": PIPELINE, "columns": ["customer_id"], "access": "deny"},
        ]
        res = _resources(_contract(principals=MAPPING, restrictions=restrictions))
        assert len(res["google_data_catalog_policy_tag"]) == 2

    def test_an_allow_makes_its_principals_the_only_readers(self):
        restrictions = [{"principal": PLATFORM, "columns": ["msisdn"], "access": "allow"}]
        res = _resources(_contract(principals=MAPPING, restrictions=restrictions))
        members = {m["member"] for m in res["google_data_catalog_policy_tag_iam_member"].values()}
        assert members == {"group:data-platform@northwind.com"}

    def test_a_deny_beats_an_allow(self):
        restrictions = [
            {"principal": PLATFORM, "columns": ["msisdn"], "access": "allow"},
            {"principal": PLATFORM, "columns": ["msisdn"], "access": "deny"},
        ]
        res = _resources(_contract(principals=MAPPING, restrictions=restrictions))
        assert "google_data_catalog_policy_tag_iam_member" not in res
        assert len(res["google_data_catalog_policy_tag"]) == 1

    def test_an_allow_never_grants_access_to_a_non_reader(self, caplog):
        contract = _contract(
            principals={**MAPPING, "group:outsiders@northwind.example": "group:o@northwind.com"},
            restrictions=[
                {
                    "principal": "group:outsiders@northwind.example",
                    "columns": ["msisdn"],
                    "access": "allow",
                }
            ],
        )
        with caplog.at_level("WARNING"):
            res = _resources(contract)
        assert "google_data_catalog_policy_tag_iam_member" not in res
        assert "column_restriction_allow_not_a_reader" in caplog.text

    @pytest.mark.parametrize(
        "restriction, fragment",
        [
            ({"principal": ANALYSTS, "columns": ["nope"], "access": "deny"}, "does not declare"),
            ({"principal": ANALYSTS, "columns": ["msisdn"]}, "must be 'allow', 'deny' or 'mask'"),
            ({"columns": ["msisdn"], "access": "deny"}, "names no principal"),
            ({"principal": ANALYSTS, "columns": [], "access": "deny"}, "must list the columns"),
        ],
    )
    def test_a_malformed_restriction_is_refused(self, restriction, fragment):
        error = _refusal(_contract(principals=MAPPING, restrictions=[restriction]))
        assert error.kind == "column-restriction"
        assert fragment in str(error)

    def test_a_restriction_on_an_unmapped_principal_is_refused(self):
        restrictions = [
            {"principal": "group:ghosts@northwind.example", "columns": ["msisdn"], "access": "deny"}
        ]
        assert _refusal(_contract(principals=MAPPING, restrictions=restrictions)).kind == (
            "principal-unmapped"
        )

    def test_a_restriction_on_a_view_is_refused(self):
        contract = _contract(principals=MAPPING, restrictions=self.DENY)
        contract["exposes"][0]["binding"]["format"] = "bigquery_view"
        assert _refusal(contract).kind == "column-restriction-view"


# ── what did not change ──────────────────────────────────────────────────


def test_a_contract_declaring_none_of_it_emits_what_it_did_minus_the_acl():
    """No governance field: no key, no tag, no partition; only the grants moved."""
    contract = _contract(principals=MAPPING)
    res = _resources(contract)
    assert set(res) == {
        "google_bigquery_dataset",
        "google_bigquery_dataset_iam_member",
        "google_bigquery_table",
    }
    table = res["google_bigquery_table"][f"{CID}_retention_candidates"]
    assert set(table) == {
        "dataset_id",
        "table_id",
        "labels",
        "deletion_protection",
        "project",
        "schema",
    }


def test_the_derivations_are_shared_with_verify():
    exposure = _contract(principals=MAPPING, restrictions=TestColumnRestrictions.DENY)["exposes"][0]
    groups = gov.tag_groups(_contract(principals=MAPPING), exposure, CID, "demo_gold", "t")
    assert [g.columns for g in groups] == [("customer_id", "msisdn")]
    assert gov.kms_location("EU") == "europe"
    assert gov.product_key_name("p", "europe-west1", "r").endswith(
        "/keyRings/r/cryptoKeys/bigquery"
    )


def test_fluid_validate_reports_each_refusal_at_stage_2():
    errors, _ = validate_governance(_contract())
    assert any("placeholder" in e and "binding.principals" in e for e in errors)
    errors, _ = validate_governance(_contract(principals=MAPPING, encryption={"kms": "alias/x"}))
    assert any("an AWS KMS key, on a gcp binding" in e for e in errors)
    assert validate_governance(
        _contract(
            principals=MAPPING,
            lifecycle={"retention": "P30D", "expire": True},
            encryption={"kms": "product"},
            restrictions=TestColumnRestrictions.DENY,
        )
    ) == ([], [])


# ── What BigQuery deletes with a table is recreated with it ───────────────

CONSENT = {"principal": ANALYSTS, "name": "analysts_consented", "where": "status = 'active'"}
_TRIGGER = {"replace_triggered_by": [f"{TABLE}.id"]}


def _row_filtered(**kwargs: Any) -> Dict[str, Any]:
    contract = _contract(principals=MAPPING, **kwargs)
    contract["exposes"][0].setdefault("policy", {}).setdefault("authz", {})["rowFilters"] = [
        dict(CONSENT)
    ]
    return contract


def _shared_dataset() -> Dict[str, Any]:
    contract = _contract(principals=MAPPING)
    contract["packaging"] = {"mode": "shared", "pool": "northwind-pool"}
    return contract


def test_each_row_access_policy_is_recreated_with_its_table():
    """BigQuery deletes a table's row access policies with it; tofu, seeing the same
    ``table_id``, kept them in state and the new table stood unfiltered."""
    policies = _resources(_row_filtered())["google_bigquery_row_access_policy"]
    assert len(policies) == 2
    for policy in policies.values():
        assert policy["lifecycle"] == _TRIGGER


def test_each_table_grant_in_a_shared_dataset_is_recreated_with_its_table():
    members = _resources(_shared_dataset())["google_bigquery_table_iam_member"]
    assert members
    for member in members.values():
        assert member["lifecycle"] == _TRIGGER


def test_a_planned_table_grant_follows_the_table_only_when_this_module_declares_it():
    """``iam.bind_bq_table`` names its table as plain strings; it is tied to the table
    resource only when that resource is this module's table of the same dataset."""
    plugin = get_iac_plugin("gcp")
    policies = {"readers": {"principals": ["analyst@example.com"], "permissions": ["read"]}}

    def members(dataset: str, table: str) -> List[Dict[str, Any]]:
        action = {"op": "iam.bind_bq_table", "dataset": dataset, "table": table}
        resources = plugin.emit(_contract(principals=MAPPING), [{**action, "policies": policies}])
        return list(resources["google_bigquery_table_iam_member"].values())

    (own,) = members("demo_gold", "retention_candidates")
    assert own["lifecycle"] == _TRIGGER
    for dataset, table in (("demo_gold", "other_table"), ("other_dataset", "retention_candidates")):
        (other,) = members(dataset, table)
        assert "lifecycle" not in other, (dataset, table)


# ── tofu validate ─────────────────────────────────────────────────────────

_TOFU = shutil.which("tofu")


@pytest.mark.integration
@pytest.mark.provider
@pytest.mark.skipif(_TOFU is None, reason="tofu not on PATH")
@pytest.mark.parametrize(
    "shape",
    [
        "all",
        "existing-key-partition-column",
        "grants-only",
        "multi-region-product-key",
        "row-filters",
        "shared-dataset",
    ],
)
def test_governed_modules_pass_tofu_validate(shape, tmp_path):
    from tests.iac.test_iac_tofu_validate import _tofu_init_or_skip

    contract = {
        "all": lambda: _contract(
            principals=MAPPING,
            lifecycle={"retention": "P90D", "expire": True},
            encryption={"kms": "product"},
            restrictions=TestColumnRestrictions.DENY,
        ),
        "existing-key-partition-column": lambda: _contract(
            principals=MAPPING,
            lifecycle={"retention": "P7Y", "expire": True},
            partition_by=["created_at"],
            encryption={"kms": "projects/sec/locations/europe-west1/keyRings/r/cryptoKeys/k"},
            restrictions=[{"principal": PLATFORM, "columns": ["msisdn"], "access": "allow"}],
        ),
        "grants-only": lambda: _contract(principals=MAPPING),
        "multi-region-product-key": lambda: _contract(
            principals=MAPPING, encryption={"kms": "product"}, region="EU"
        ),
        # The row access policies' and the table grants' replace_triggered_by:
        # ``tofu validate`` checks its attribute against the carrying resource.
        "row-filters": lambda: _row_filtered(
            lifecycle={"retention": "P90D", "expire": True},
            restrictions=TestColumnRestrictions.DENY,
        ),
        "shared-dataset": _shared_dataset,
    }[shape]()
    (tmp_path / "main.tf.json").write_text(
        build_module(get_iac_plugin("gcp"), copy.deepcopy(contract))
    )
    _tofu_init_or_skip(tmp_path)
    done = subprocess.run(
        [_TOFU, "validate", "-no-color"], cwd=tmp_path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr or done.stdout


# ── contract text never becomes an interpolation ─────────────────────────


def test_every_interpolation_is_one_the_emitter_built():
    import re

    contract = _contract(
        principals=MAPPING,
        lifecycle={"retention": "P90D", "expire": True},
        encryption={"kms": "product"},
        restrictions=TestColumnRestrictions.DENY,
    )
    exposure = contract["exposes"][0]
    exposure["contract"]["schema"][0]["description"] = 'x ${file("/etc/passwd")} %{ if 1 }'
    exposure["binding"]["location"]["dataset"] = 'ds${file("/etc/hosts")}'
    rendered = build_module(get_iac_plugin("gcp"), contract)
    live = re.findall(r"(?<!\$)\$\{([^}]*)\}", rendered.replace("$${", ""))
    allowed = re.compile(r"^(data\.)?(google|terraform)_[a-z_]+\.[A-Za-z0-9_]+\.[a-z_]+$")
    assert live and all(allowed.match(expr) for expr in live), live
    assert "file(" not in "".join(live)


# ── schema: fluid-schema 0.7.6 only ──────────────────────────────────────


class TestSchema:
    @staticmethod
    def _schema_contract(**kwargs: Any) -> Dict[str, Any]:
        contract = _contract(**kwargs)
        contract.update(
            kind="DataProduct",
            name="Retention candidates",
            metadata={"owner": {"team": "data-platform"}},
        )
        contract["exposes"][0]["kind"] = "table"
        return contract

    @staticmethod
    def _errors(contract: Dict[str, Any]) -> List[str]:
        from fluid_build.schema_manager import FluidSchemaManager

        return list(FluidSchemaManager().validate_contract(contract).errors)

    def test_the_preview_schema_accepts_the_gcp_fields(self):
        contract = self._schema_contract(
            principals={**MAPPING, ANALYSTS: ["group:a@northwind.com", "group:b@northwind.com"]},
            lifecycle={"retention": "P90D", "expire": True},
            encryption={"kms": "projects/sec/locations/europe-west1/keyRings/r/cryptoKeys/k"},
            restrictions=TestColumnRestrictions.DENY,
            partition_by=["created_at"],
        )
        assert self._errors(contract) == []

    def test_the_aws_shapes_still_validate(self):
        contract = self._schema_contract(
            principals={ANALYSTS: "arn:aws:iam::111111111111:role/analyst"},
            encryption={"kms": "alias/fluid/gold"},
        )
        contract["exposes"][0]["binding"]["platform"] = "aws"
        contract["exposes"][0]["binding"]["format"] = "parquet"
        assert self._errors(contract) == []

    @pytest.mark.parametrize(
        "platform, kms",
        [
            ("gcp", "alias/fluid/gold"),
            ("gcp", "arn:aws:kms:eu-north-1:111111111111:key/abc"),
            ("aws", "projects/sec/locations/europe-west1/keyRings/r/cryptoKeys/k"),
        ],
    )
    def test_a_key_reference_validates_only_on_its_own_cloud(self, platform, kms):
        contract = self._schema_contract(principals=MAPPING, encryption={"kms": kms})
        contract["exposes"][0]["binding"]["platform"] = platform
        if platform == "aws":
            contract["exposes"][0]["binding"]["principals"] = {}
        errors = self._errors(contract)
        assert errors and all("kms" in e or "encryption" in e for e in errors), errors

    @pytest.mark.parametrize(
        "platform, identity",
        [
            ("gcp", "arn:aws:iam::111111111111:role/analyst"),
            ("aws", "group:analysts@northwind.com"),
        ],
    )
    def test_a_mapped_identity_validates_only_on_its_own_cloud(self, platform, identity):
        contract = self._schema_contract(principals={ANALYSTS: identity})
        contract["exposes"][0]["binding"]["platform"] = platform
        errors = self._errors(contract)
        assert errors and all("principals" in e for e in errors), errors

    def test_a_templated_identity_passes_the_schema(self):
        contract = self._schema_contract(
            principals={
                PIPELINE: "serviceAccount:p@{{ env.FLUID_DEMO_GCP_PROJECT }}.iam.gserviceaccount.com"
            }
        )
        assert self._errors(contract) == []

    def test_principals_are_0_7_6_only(self):
        contract = self._schema_contract(principals=MAPPING)
        contract["fluidVersion"] = "0.7.5"
        errors = self._errors(contract)
        assert errors and any("principals" in e for e in errors), errors
        del contract["exposes"][0]["binding"]["principals"]
        assert self._errors(contract) == []


class TestTheGateIsWiredIntoFluidValidate:
    """The real ``fluid validate``, so the wiring in ``cli/validate.py`` is exercised.

    Its ``try/except`` around each binding check would swallow an import error
    into a verbose-only note; only a real run shows the refusal reaches stage 2.
    """

    _CONTRACT = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: gold.retention_candidates
name: demo
metadata:
  owner:
    team: data-platform
accessPolicy:
  grants:
    - principal: group:data-platform@northwind.example
      permissions: [read]
exposes:
  - exposeId: candidates
    kind: table
    binding:
      platform: gcp
      format: bigquery_table
      location:
        project: northwind-demo
        dataset: demo_gold
        table: retention_candidates
        region: europe-west1
{extra}
    contract:
      schema:
        - name: id
          type: string
"""

    def _validate(self, tmp_path, extra=""):
        import sys

        path = tmp_path / "contract.fluid.yaml"
        path.write_text(self._CONTRACT.format(extra=extra), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", "validate", str(path)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

    def test_a_placeholder_principal_fails_validate(self, tmp_path):
        result = self._validate(tmp_path)
        assert result.returncode != 0
        assert "reserved top-level domain" in result.stdout

    def test_a_mapped_principal_passes_validate(self, tmp_path):
        extra = (
            "      principals:\n"
            "        group:data-platform@northwind.example: group:data-platform@northwind.com"
        )
        result = self._validate(tmp_path, extra)
        assert result.returncode == 0, result.stdout + result.stderr


# ── One base contract, two overlays: fluid validate --strict on each cloud ──


class TestOneContractValidatesStrictOnBothClouds:
    """The demo's shape: accessPolicy in the base for GCP, Lake Formation on AWS.

    ``fluid validate --strict`` is stage 2 of every generated Jenkinsfile
    (``VALIDATE_STRICT:-true``) and fails on any warning. A warning that
    ``accessPolicy`` is not emitted on aws, given for every aws binding, failed the
    demo's three AWS pipelines while the aws overlay's Lake Formation grants were
    the AWS form of the same access.
    """

    _BASE = """\
fluidVersion: "0.7.6"
kind: DataProduct
id: bronze.customer_subscriptions
name: Customer Subscriptions
metadata:
  owner:
    team: data-platform
accessPolicy:
  grants:
    - principal: group:data-platform@northwind.example
      permissions: [read, select, query]
    - principal: serviceAccount:fluid-pipeline@northwind.example
      permissions: [read, write, insert]
exposes:
  - exposeId: subscriptions
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: data/customer_subscriptions.parquet
    contract:
      schema:
        - name: subscription_id
          type: VARCHAR
          required: true
        - name: msisdn
          type: VARCHAR
"""

    _AWS = """\
exposes:
  - binding:
      platform: aws
      format: parquet
      location:
        database: demo_bronze
        table: customer_subscriptions
        bucket: northwind-demo-lake
        path: bronze/customer_subscriptions/
        region: eu-north-1
{governance}
"""

    _LF = """\
      governance:
        lakeFormation:
          registerLocation: true
          grants:
            - principal: arn:aws:iam::111111111111:role/fluid-demo-lab-steward
              permissions: [SELECT, DESCRIBE]
            - principal: arn:aws:iam::111111111111:role/fluid-demo-lab-analyst
              permissions: [SELECT, DESCRIBE]
              excludedColumns: [msisdn]"""

    _GCP = """\
exposes:
  - binding:
      platform: gcp
      format: bigquery_table
      location:
        project: northwind-demo
        dataset: demo_bronze
        table: customer_subscriptions
        region: europe-west1
      principals:
        group:data-platform@northwind.example: group:data-platform@northwind.com
        serviceAccount:fluid-pipeline@northwind.example: serviceAccount:fluid-pipeline@northwind-demo.iam.gserviceaccount.com
"""

    def _validate(self, tmp_path, env: str, governance: str = _LF):
        import sys

        (tmp_path / "overlays").mkdir()
        (tmp_path / "contract.fluid.yaml").write_text(self._BASE, encoding="utf-8")
        (tmp_path / "overlays" / "aws.yaml").write_text(
            self._AWS.format(governance=governance), encoding="utf-8"
        )
        (tmp_path / "overlays" / "gcp.yaml").write_text(self._GCP, encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "fluid_build.cli",
                "validate",
                str(tmp_path / "contract.fluid.yaml"),
                "--env",
                env,
                "--strict",
            ],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

    @pytest.mark.parametrize("env", ["aws", "gcp"])
    def test_the_base_contract_passes_strict_on_each_cloud(self, tmp_path, env):
        result = self._validate(tmp_path, env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "not enforced" not in result.stdout

    def test_an_aws_binding_with_no_lake_formation_grant_is_warned(self, tmp_path):
        """The one case where the contract's access intent is unenforced on AWS."""
        result = self._validate(tmp_path, "aws", governance="")
        out = " ".join(result.stdout.split())
        assert result.returncode != 0
        assert "not enforced on aws binding(s) subscriptions" in out


# ── Column restrictions: the expose's own readers ────────────────────────


def _restricted(
    *, readers: Optional[List[str]] = None, grants: Optional[List[Dict[str, Any]]] = None, **kw
) -> Dict[str, Any]:
    contract = _contract(
        restrictions=[
            {"principal": "group:interns@company.com", "columns": ["msisdn"], "access": "deny"}
        ],
        **kw,
    )
    contract["accessPolicy"]["grants"] = grants or []
    if readers is not None:
        contract["exposes"][0]["policy"]["authz"]["readers"] = readers
    return contract


def _tag_readers(res: Dict[str, Any]) -> set:
    return {
        m["member"] for m in (res.get("google_data_catalog_policy_tag_iam_member") or {}).values()
    }


class TestRestrictionReaders:
    def test_a_deny_for_one_group_leaves_the_exposes_other_readers_reading(self):
        """``policy.authz.readers`` are readers too; a deny for interns was a tag with none."""
        res = _resources(
            _restricted(readers=["group:analytics@company.com", "group:finance@company.com"])
        )
        assert _tag_readers(res) == {"group:analytics@company.com", "group:finance@company.com"}

    def test_authz_readers_and_access_policy_readers_are_both_readers(self):
        res = _resources(
            _restricted(
                readers=["group:analytics@company.com"],
                grants=[{"principal": "group:bi@company.com", "permissions": ["read"]}],
            )
        )
        assert _tag_readers(res) == {"group:analytics@company.com", "group:bi@company.com"}

    def test_authz_readers_are_mapped_through_binding_principals(self):
        res = _resources(
            _restricted(
                readers=[ANALYSTS],
                principals={
                    ANALYSTS: "group:analysts@northwind.com",
                    "group:interns@company.com": [],
                },
            )
        )
        assert _tag_readers(res) == {"group:analysts@northwind.com"}

    def test_a_restriction_with_no_reader_at_all_is_refused_not_a_locked_column(self):
        error = _refusal(_restricted())
        assert error.kind == "column-restriction-no-readers"
        errors, _ = validate_governance(_restricted())
        assert any("unreadable by everyone" in e for e in errors)

    @pytest.mark.parametrize(
        "example, readers",
        [
            (
                "examples/policy-examples/customer-profiles-contract.yaml",
                {"group:data-analysts@company.com", "group:data-scientists@company.com"},
            ),
            (
                "examples/bitcoin-price-api-declarative-part-c/contract.fluid.yaml",
                {"group:data-analysts@company.com", "group:data-engineers@company.com"},
            ),
            (
                # Mapped through binding.principals; the denied interns are no reader.
                "examples/bitcoin-price-api-declarative-part-b/contract.fluid.yaml",
                {
                    "group:data-analytics@example.com",
                    "group:finance-team@example.com",
                    "group:trading-desk@example.com",
                    "serviceAccount:looker@example.com",
                },
            ),
        ],
    )
    def test_the_shipped_examples_keep_their_readers(self, example, readers):
        import pathlib

        import yaml

        path = pathlib.Path(__file__).resolve().parents[2] / example
        res = _resources(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert res["google_data_catalog_policy_tag"]
        assert _tag_readers(res) == readers

    def test_an_example_reader_left_as_a_placeholder_is_refused(self):
        """part-b's looker reader is ``serviceAccount:looker@<<YOUR_PROJECT_HERE>>...``.

        The example maps it in ``binding.principals``; without that block it is
        emitted as written, which is the placeholder the emitter refuses.
        """
        import pathlib

        import yaml

        path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "examples/bitcoin-price-api-declarative-part-b/contract.fluid.yaml"
        )
        contract = yaml.safe_load(path.read_text(encoding="utf-8"))
        binding = contract["exposes"][0]["binding"]
        assert "serviceAccount:looker@<<YOUR_PROJECT_HERE>>.iam.gserviceaccount.com" in (
            binding["principals"]
        )
        del binding["principals"]
        error = _refusal(contract)
        assert error.kind == "principal-placeholder"
        assert "<<YOUR_PROJECT_HERE>>" in str(error)


# ── IAM resource names: one per member, whatever it folds to ─────────────


def test_principals_that_fold_to_one_name_keep_one_grant_each():
    """``safe_ident`` gave data.eng@, data-eng@ and data_eng@ one resource name."""
    groups = [f"group:data{sep}eng@corp.com" for sep in (".", "-", "_")]
    contract = _contract(
        restrictions=[
            {"principal": "group:blocked@corp.com", "columns": ["msisdn"], "access": "deny"}
        ]
    )
    contract["accessPolicy"]["grants"] = [
        {"principal": g, "permissions": ["read"]} for g in groups + ["group:blocked@corp.com"]
    ]
    res = _resources(contract)
    dataset_members = [m["member"] for m in res["google_bigquery_dataset_iam_member"].values()]
    assert sorted(dataset_members) == sorted(groups + ["group:blocked@corp.com"])
    assert _tag_readers(res) == set(groups)
    assert len(res["google_data_catalog_policy_tag_iam_member"]) == 3


# ── Platform first: an AWS Iceberg binding is the AWS emitter's ──────────


def _aws_iceberg(**expose: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "id": "sales.lakehouse",
        "accessPolicy": {"grants": [{"principal": PLATFORM, "permissions": ["read"]}]},
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "bucket": "acme-sales-lakehouse",
                        "database": "sales",
                        "table": "orders",
                        "path": "iceberg/sales/orders/",
                        "region": "eu-west-1",
                    },
                    "encryption": {"kms": "product"},
                    **expose.pop("binding", {}),
                },
                "lifecycle": {"retention": "P30D", "expire": True},
                "contract": {"schema": [{"name": "id", "type": "string"}]},
                **expose,
            }
        ],
    }


class TestAwsIcebergIsValidatedAsAws:
    def test_retention_encryption_and_a_logical_principal_are_not_refused_as_gcp(self):
        errors, warnings = validate_governance(_aws_iceberg())
        assert errors == []
        assert any("not enforced on aws binding(s) orders" in w for w in warnings)

    def test_a_column_restriction_goes_through_the_lake_formation_check(self):
        contract = _aws_iceberg(
            policy={
                "authz": {
                    "columnRestrictions": [
                        {"principal": "arn:aws:iam::1:role/x", "columns": ["id"], "access": "deny"}
                    ]
                }
            }
        )
        errors, _ = validate_governance(contract)
        assert any("declares no governance.lakeFormation.grants" in e for e in errors)
        assert not any("GCP" in e for e in errors)


# ── Unmapped principals that are not IAM members ─────────────────────────


@pytest.mark.parametrize(
    "principal",
    ["group:data-platform", "analysts", "role:analyst"],
    ids=["no-domain", "bare", "role"],
)
def test_an_unmapped_principal_that_is_not_an_iam_member_is_refused(principal):
    contract = _contract()
    contract["accessPolicy"]["grants"] = [{"principal": principal, "permissions": ["read"]}]
    assert _refusal(contract).kind == "principal-placeholder"
    errors, _ = validate_governance(contract)
    assert any("is not a GCP IAM member" in e for e in errors)


# ── One dataset, one key ─────────────────────────────────────────────────


def _two_in_one_dataset(
    first: Optional[Dict[str, Any]], second: Optional[Dict[str, Any]], *, view: bool = False
):
    contract = _contract(principals=MAPPING)
    one = contract["exposes"][0]
    other = copy.deepcopy(one)
    other["exposeId"] = "summary"
    other["binding"]["location"]["table"] = "summary"
    if view:
        other["binding"]["format"] = "bigquery_view"
        other["binding"]["location"]["query"] = "SELECT 1"
    for exposure, block in ((one, first), (other, second)):
        exposure["binding"].pop("encryption", None)
        if block is not None:
            exposure["binding"]["encryption"] = block
    contract["exposes"] = [other, one] if view else [one, other]
    return contract


class TestOneDatasetOneKey:
    @pytest.mark.parametrize("order", ["keyed-first", "unkeyed-first"])
    def test_a_keyed_and_an_unkeyed_table_in_one_dataset_are_refused(self, order):
        pair = ({"kms": "product"}, None) if order == "keyed-first" else (None, {"kms": "product"})
        contract = _two_in_one_dataset(*pair)
        assert _refusal(contract).kind == "encryption-kms-mixed-dataset"
        errors, _ = validate_governance(contract)
        assert any("different encryption" in e for e in errors)

    def test_tables_with_the_same_key_share_the_dataset_default(self):
        res = _resources(_two_in_one_dataset({"kms": "product"}, {"kms": "product"}))
        dataset = res["google_bigquery_dataset"][f"{CID}_demo_gold"]
        assert "default_encryption_configuration" in dataset
        keys = {
            json.dumps(t.get("encryption_configuration"))
            for t in res["google_bigquery_table"].values()
        }
        assert len(keys) == 1 and "null" not in keys

    def test_an_unkeyed_view_emitted_first_does_not_drop_the_datasets_key(self):
        """The dataset body used to be the first expose's, so its default key depended on order."""
        res = _resources(_two_in_one_dataset({"kms": "product"}, None, view=True))
        dataset = res["google_bigquery_dataset"][f"{CID}_demo_gold"]
        assert dataset["default_encryption_configuration"] == {
            "kms_key_name": f"${{google_kms_crypto_key.{CID}_demo_gold_kms.id}}"
        }
        assert dataset["depends_on"] == [f"google_kms_crypto_key_iam_member.{CID}_demo_gold_kms"]


# ── Retention on a partition column is event-time age ────────────────────


def test_retention_on_a_partition_column_is_said_to_count_from_its_date(caplog):
    """BigQuery expires a partition relative to its date, not to when rows landed."""
    import logging

    with caplog.at_level(logging.INFO, logger="fluid_build.iac.providers.gcp_governance"):
        _resources(
            _contract(
                principals=MAPPING,
                lifecycle={"retention": "P30D", "expire": True},
                partition_by=["created_at"],
            )
        )
    assert any("bigquery_retention_event_time" in r.getMessage() for r in caplog.records)
    import pathlib

    schema = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[2]
            / "fluid_build/schemas/fluid-schema-0.7.6.json"
        ).read_text(encoding="utf-8")
    )
    text = " ".join(
        [
            schema["$defs"]["bindingLocation"]["properties"]["partitionBy"]["description"],
            schema["$defs"]["exposeLifecycle"]["properties"]["expire"]["description"],
        ]
    )
    assert "backfill" in text and "date in that column" in text


# ── A restriction's tags and labels reach the policy tag ─────────────────


def test_restriction_tags_and_labels_are_written_on_the_policy_tag():
    contract = _contract(
        principals=MAPPING,
        restrictions=[
            {
                "principal": ANALYSTS,
                "columns": ["msisdn"],
                "access": "deny",
                "tags": ["sensitive-financial-data"],
                "labels": {"reason": "pii"},
            }
        ],
    )
    (tag,) = _resources(contract)["google_data_catalog_policy_tag"].values()
    assert "Tags: sensitive-financial-data." in tag["description"]
    assert "Labels: reason=pii." in tag["description"]


def test_a_restriction_label_cannot_become_an_interpolation():
    contract = _contract(
        principals=MAPPING,
        restrictions=[
            {
                "principal": ANALYSTS,
                "columns": ["msisdn"],
                "access": "deny",
                "labels": {"reason": '${file("/etc/passwd")}'},
            }
        ],
    )
    rendered = build_module(get_iac_plugin("gcp"), contract)
    assert '$${file(\\"/etc/passwd\\")}' in rendered
    assert '"${file' not in rendered and " ${file" not in rendered
