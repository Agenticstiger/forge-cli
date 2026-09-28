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

"""Retention and encryption at rest on an AWS S3 binding, as ``fluid apply`` emits them.

Before these fields no data retention and no encryption at rest reached AWS:
``_emit_s3`` set only the bucket, ``force_destroy`` and tags. Now:

* ``exposes[].lifecycle {retention, expire: true}`` → one prefix-scoped rule per
  expose in the bucket's ``aws_s3_bucket_lifecycle_configuration``;
* ``binding.encryption.kms`` → the bucket's default SSE-KMS, and for
  ``product`` a KMS key + alias per product and bucket, with a key policy that
  hands the decision to IAM plus the Lake Formation service-linked role.

The rendering is pinned here; ``tofu validate`` in ``test_iac_tofu_validate.py``,
and a real ``tofu plan`` / ``apply`` / ``destroy`` against moto in
``test_iac_aws_retention_encryption_moto.py``.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import fluid_build
from fluid_build.cli import _verify_athena
from fluid_build.iac import build_module
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.packaging import PackagingError
from fluid_build.iac.providers import aws_storage
from fluid_build.iac.providers.aws import AwsIacPlugin
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = [pytest.mark.unit, pytest.mark.provider]

OTHER = "arn:aws:iam::222222222222:role/other-account-reader"
CALLER = "${data.aws_caller_identity.fluid_lf_caller.account_id}"
SLR = (
    f"arn:aws:iam::{CALLER}:role/aws-service-role/lakeformation.amazonaws.com/"
    "AWSServiceRoleForLakeFormationDataAccess"
)
SCHEMAS = Path(fluid_build.__file__).resolve().parent / "schemas"
BUCKET_KEY = "retention_kms_acme_lake"
KEY = "retention_kms_acme_lake_kms"


def _expose(
    expose_id: str = "orders",
    *,
    path: Optional[str] = "bronze/orders/",
    retention: Optional[str] = "P30D",
    expire: Optional[bool] = True,
    encryption: Optional[Dict[str, Any]] = None,
    lake_formation: Optional[Dict[str, Any]] = None,
    bucket: Optional[str] = "acme-lake",
    platform: str = "aws",
) -> Dict[str, Any]:
    location: Dict[str, Any] = {"database": "sales", "table": expose_id}
    if bucket is not None:
        location["bucket"] = bucket
    if path is not None:
        location["path"] = path
    binding: Dict[str, Any] = {"platform": platform, "format": "parquet", "location": location}
    if encryption is not None:
        binding["encryption"] = encryption
    if lake_formation is not None:
        binding["governance"] = {"lakeFormation": lake_formation}
    exposure: Dict[str, Any] = {
        "exposeId": expose_id,
        "kind": "table",
        "binding": binding,
        "contract": {"schema": [{"name": "id", "type": "string"}]},
    }
    lifecycle: Dict[str, Any] = {}
    if retention is not None:
        lifecycle["retention"] = retention
    if expire is not None:
        lifecycle["expire"] = expire
    if lifecycle:
        exposure["lifecycle"] = lifecycle
    return exposure


def _contract(*exposes: Dict[str, Any], packaging: Optional[Dict[str, Any]] = None):
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "retention.kms",
        "name": "Retention and KMS",
        "metadata": {"layer": "Bronze", "owner": {"team": "data", "email": "d@example.com"}},
        "exposes": list(exposes) or [_expose()],
    }
    if packaging is not None:
        contract["packaging"] = packaging
    return contract


def _emit(contract: Dict[str, Any]):
    plugin = AwsIacPlugin()
    return plugin.emit(contract), plugin.emit_data(contract)


def _rules(resources: Dict[str, Any]) -> List[Dict[str, Any]]:
    configs = resources["aws_s3_bucket_lifecycle_configuration"]
    assert list(configs) == [BUCKET_KEY]
    return list(configs[BUCKET_KEY]["rule"])


def _grants(*principals: str, **extra: Any) -> Dict[str, Any]:
    return {
        "registerLocation": True,
        "grants": [{"principal": p, "permissions": ["SELECT"]} for p in principals],
        **extra,
    }


# ---------------------------------------------------------------------------
# Nothing changes for a contract that asks for neither
# ---------------------------------------------------------------------------


class TestNothingNewWithoutTheOptIn:
    def _module(self, contract: Dict[str, Any]) -> Dict[str, Any]:
        document = json.loads(build_module(AwsIacPlugin(), contract))
        for table in document["resource"]["aws_glue_catalog_table"].values():
            table["parameters"].pop("fluid_contract", None)
        return document

    def test_a_declared_retention_without_expire_emits_exactly_what_it_did(self):
        # lifecycle.retention has been in the schema since 0.7.1 as a
        # declaration; a contract carrying it must not start deleting objects.
        plain = self._module(_contract(_expose(retention=None, expire=None)))
        declared = self._module(_contract(_expose(expire=None)))
        explicit_false = self._module(_contract(_expose(expire=False)))
        assert declared == plain == explicit_false
        assert "aws_s3_bucket_lifecycle_configuration" not in declared["resource"]

    @pytest.mark.parametrize("encryption", [None, {"kms": "none"}])
    def test_no_encryption_block_or_none_emits_no_key(self, encryption):
        plain = self._module(_contract(_expose(retention=None, expire=None)))
        module = self._module(
            _contract(_expose(retention=None, expire=None, encryption=encryption))
        )
        assert module == plain
        for kind in (
            "aws_kms_key",
            "aws_kms_alias",
            "aws_s3_bucket_server_side_encryption_configuration",
        ):
            assert kind not in module["resource"]

    def test_expire_on_another_platform_is_not_emitted_here(self):
        resources, _ = _emit(_contract(_expose(platform="gcp")))
        assert "aws_s3_bucket_lifecycle_configuration" not in resources


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetentionRule:
    def test_one_rule_scoped_to_the_prefix_with_every_expiry(self):
        resources, _ = _emit(_contract())
        config = resources["aws_s3_bucket_lifecycle_configuration"][BUCKET_KEY]
        assert config["bucket"] == f"${{aws_s3_bucket.{BUCKET_KEY}.id}}"
        rule = config["rule"][0]
        assert rule == {
            "id": "fluid-retention-orders",
            "status": "Enabled",
            "filter": [{"prefix": "bronze/orders/"}],
            "expiration": [{"days": 30}],
            "noncurrent_version_expiration": [{"noncurrent_days": 1}],
            "abort_incomplete_multipart_upload": [{"days_after_initiation": 7}],
        }

    def test_no_rule_ever_covers_the_whole_bucket(self):
        resources, _ = _emit(
            _contract(_expose(), _expose("returns", path="bronze/returns", retention="P2D"))
        )
        for rule in _rules(resources):
            assert rule["filter"] == [{"prefix": rule["filter"][0]["prefix"]}]
            assert rule["filter"][0]["prefix"], f"whole-bucket rule: {rule}"
            assert rule["filter"][0]["prefix"].endswith("/")

    def test_verify_results_get_a_rule_of_their_own(self):
        # The configuration replaces every rule on the bucket, so forge-cli's own
        # Athena results prefix carries one, at the shortest declared period.
        resources, _ = _emit(
            _contract(_expose(), _expose("returns", path="bronze/returns/", retention="P2D"))
        )
        rules = {r["id"]: r for r in _rules(resources)}
        results = rules["fluid-verify-athena-results"]
        assert results["filter"] == [{"prefix": ".fluid/athena-results/"}]
        assert results["expiration"] == [{"days": 2}]
        assert aws_storage.VERIFY_RESULTS_PREFIX == _verify_athena.RESULTS_PREFIX

    @pytest.mark.parametrize(
        "path, prefix",
        [
            ("bronze/orders", "bronze/orders/"),
            ("/bronze/orders/", "bronze/orders/"),
            (None, "sales/orders/"),  # the Glue table's own default location
        ],
    )
    def test_the_prefix_is_the_tables_always_ending_in_a_slash(self, path, prefix):
        resources, _ = _emit(_contract(_expose(path=path)))
        assert _rules(resources)[0]["filter"] == [{"prefix": prefix}]

    @pytest.mark.parametrize(
        "period, days, abort",
        [("P30D", 30, 7), ("P2W", 14, 7), ("P7Y", 2555, 7), ("P1M", 30, 7), ("PT12H", 1, 1)],
    )
    def test_the_period_in_whole_days_never_shorter(self, period, days, abort):
        resources, _ = _emit(_contract(_expose(retention=period)))
        rule = _rules(resources)[0]
        assert rule["expiration"] == [{"days": days}]
        assert rule["abort_incomplete_multipart_upload"] == [{"days_after_initiation": abort}]

    def test_two_exposes_on_one_bucket_share_one_configuration(self):
        resources, _ = _emit(
            _contract(_expose(), _expose("returns", path="bronze/returns/", retention="P90D"))
        )
        assert [r["id"] for r in _rules(resources)] == [
            "fluid-retention-orders",
            "fluid-retention-returns",
            "fluid-verify-athena-results",
        ]

    def test_an_expose_without_expire_on_the_bucket_is_left_alone(self):
        resources, _ = _emit(
            _contract(_expose(), _expose("returns", path="bronze/returns/", expire=None))
        )
        assert [r["id"] for r in _rules(resources)] == [
            "fluid-retention-orders",
            "fluid-verify-athena-results",
        ]


class TestRetentionFailsClosed:
    def _kind(self, contract: Dict[str, Any]) -> str:
        with pytest.raises(UnsupportedBindingError) as excinfo:
            AwsIacPlugin().emit(contract)
        assert excinfo.value.remediation
        return excinfo.value.kind

    def test_no_prefix_to_scope_to_is_refused(self):
        exposure = _expose(path=None)
        del exposure["binding"]["location"]["database"]
        del exposure["binding"]["location"]["table"]
        assert self._kind(_contract(exposure)) == "retention-requires-prefix"

    def test_an_explicitly_empty_path_is_refused(self):
        assert self._kind(_contract(_expose(path="/"))) == "retention-requires-prefix"

    @pytest.mark.parametrize("period", [None, "", "thirty days", "P0D", "PT0S"])
    def test_a_missing_invalid_or_zero_period_is_refused(self, period):
        assert self._kind(_contract(_expose(retention=period))) == "retention-period"

    def test_no_bucket_is_refused(self):
        assert self._kind(_contract(_expose(bucket=None))) == "storage-requires-bucket"

    def test_a_rule_that_would_expire_another_exposes_objects_is_refused(self):
        # A rule on bronze/ also expires bronze/orders/, which keeps its data.
        parent = _expose("all", path="bronze/", retention="P7D")
        child = _expose("orders", path="bronze/orders/", expire=None)
        assert self._kind(_contract(parent, child)) == "retention-overlap"

    def test_one_prefix_two_periods_is_refused(self):
        first = _expose("a", path="bronze/orders/", retention="P7D")
        second = _expose("b", path="bronze/orders/", retention="P30D")
        assert self._kind(_contract(first, second)) == "retention-overlap"

    def test_disjoint_prefixes_with_a_shared_stem_are_fine(self):
        # bronze/orders/ does not contain bronze/orders_archive/.
        first = _expose("a", path="bronze/orders", retention="P7D")
        second = _expose("b", path="bronze/orders_archive", expire=None)
        resources, _ = _emit(_contract(first, second))
        assert _rules(resources)[0]["filter"] == [{"prefix": "bronze/orders/"}]


class TestRetentionOnASharedPool:
    def test_nothing_is_written_to_a_pool_and_the_gap_is_logged(self, caplog):
        contract = _contract(packaging={"mode": "shared", "pool": "acme-pool"})
        with caplog.at_level(logging.WARNING, logger="fluid_build.iac.providers.aws"):
            resources, _ = _emit(contract)
        assert "aws_s3_bucket_lifecycle_configuration" not in resources
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "aws_shared_bucket_retention_not_emitted" in messages
        assert "prefix=bronze/orders/ days=30" in messages


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


class TestProductKey:
    def test_key_alias_and_default_encryption(self):
        resources, _ = _emit(_contract(_expose(expire=None, encryption={})))
        key = resources["aws_kms_key"][KEY]
        assert key["enable_key_rotation"] is True
        assert key["deletion_window_in_days"] == 7
        assert key["policy"] == f"${{data.aws_iam_policy_document.{KEY}_policy.json}}"
        assert key["tags"] == {"managed_by": "fluid", "fluid_contract": "retention_kms"}
        assert resources["aws_kms_alias"][KEY] == {
            "name": "alias/fluid/retention_kms/acme-lake",
            "target_key_id": f"${{aws_kms_key.{KEY}.key_id}}",
        }
        assert resources["aws_s3_bucket_server_side_encryption_configuration"][BUCKET_KEY] == {
            "bucket": f"${{aws_s3_bucket.{BUCKET_KEY}.id}}",
            "rule": [
                {
                    "apply_server_side_encryption_by_default": [
                        {
                            "sse_algorithm": "aws:kms",
                            "kms_master_key_id": f"${{aws_kms_key.{KEY}.arn}}",
                        }
                    ],
                    "bucket_key_enabled": True,
                }
            ],
        }

    def test_the_alias_is_per_bucket_so_environments_do_not_collide(self):
        dev = aws_storage.product_key_alias("retention_kms", "acme-lake-dev")
        prod = aws_storage.product_key_alias("retention_kms", "acme.lake.prod")
        assert dev == "alias/fluid/retention_kms/acme-lake-dev"
        assert prod == "alias/fluid/retention_kms/acme-lake-prod"

    def test_the_key_policy_hands_the_decision_to_iam(self):
        _, data = _emit(_contract(_expose(expire=None, encryption={"kms": "product"})))
        statements = data["aws_iam_policy_document"][f"{KEY}_policy"]["statement"]
        assert statements == [
            {
                "sid": "EnableIamPolicies",
                "effect": "Allow",
                "principals": [{"type": "AWS", "identifiers": [f"arn:aws:iam::{CALLER}:root"]}],
                "actions": ["kms:*"],
                "resources": ["*"],
            }
        ]
        assert "fluid_lf_caller" in data["aws_caller_identity"]

    def test_lake_formation_gets_its_role_and_no_grantee_does(self):
        # The analyst reads through Athena with credentials LF vends, so only
        # the service-linked role is in the key policy, never the grantee.
        same = "arn:aws:iam::111111111111:role/analyst"
        _, data = _emit(
            _contract(
                _expose(
                    expire=None,
                    encryption={},
                    lake_formation=_grants(same, bucketPolicy="none"),
                )
            )
        )
        document = data["aws_iam_policy_document"][f"{KEY}_policy"]
        lf = document["statement"][1]
        assert lf == {
            "sid": "AllowLakeFormationDataAccess",
            "effect": "Allow",
            "principals": [{"type": "AWS", "identifiers": ["*"]}],
            "actions": [
                "kms:Encrypt",
                "kms:Decrypt",
                "kms:ReEncrypt*",
                "kms:GenerateDataKey*",
                "kms:DescribeKey",
            ],
            "resources": ["*"],
            "condition": [{"test": "ArnEquals", "variable": "aws:PrincipalArn", "values": [SLR]}],
        }
        assert "dynamic" not in document
        assert same not in json.dumps(document)

    def test_a_cross_account_bucket_policy_reader_may_decrypt_through_s3_only(self):
        _, data = _emit(
            _contract(_expose(expire=None, encryption={}, lake_formation=_grants(OTHER)))
        )
        document = data["aws_iam_policy_document"][f"{KEY}_policy"]
        bucket_policy = data["aws_iam_policy_document"]["retention_kms_lf_bucket_policy_acme_lake"]
        [reader] = document["dynamic"]["statement"]
        # The very grantee filter the bucket policy uses, evaluated at plan time.
        assert reader["for_each"] == bucket_policy["dynamic"]["statement"][0]["for_each"]
        assert reader["content"]["actions"] == ["kms:Decrypt"]
        assert reader["content"]["condition"] == {
            "test": "StringLike",
            "variable": "kms:ViaService",
            "values": ["s3.*.amazonaws.com"],
        }
        assert OTHER not in json.dumps(document), "the ARN stays in its data.aws_arn"

    def test_all_grantees_readers_are_named_in_the_key_policy(self):
        _, data = _emit(
            _contract(
                _expose(
                    expire=None,
                    encryption={},
                    lake_formation=_grants(OTHER, bucketPolicy="all-grantees"),
                )
            )
        )
        document = data["aws_iam_policy_document"][f"{KEY}_policy"]
        reader = document["statement"][-1]
        assert reader["sid"] == "AllowBucketPolicyReaders"
        assert reader["principals"] == [{"type": "AWS", "identifiers": [OTHER]}]
        assert reader["actions"] == ["kms:Decrypt"]
        assert "dynamic" not in document

    def test_every_reference_resolves_including_data_to_data(self):
        contract = _contract(_expose(encryption={}, lake_formation=_grants(OTHER)))
        _, data = _emit(contract)
        rendered = build_module(AwsIacPlugin(), contract)
        referenced = set(re.findall(r"data\.(aws_[a-z_]+)\.([A-Za-z0-9_]+)", rendered))
        declared = {(dtype, name) for dtype, block in data.items() for name in block}
        assert referenced <= declared, f"dangling: {sorted(referenced - declared)}"
        assert declared <= referenced, f"unused: {sorted(declared - referenced)}"
        resources = json.loads(rendered)["resource"]
        for address in re.findall(r"\$\{(aws_[a-z_0-9]+\.[A-Za-z0-9_]+)\.", rendered):
            rtype, name = address.split(".")
            assert name in resources.get(rtype, {}), f"dangling {address}"


class TestExistingKey:
    @pytest.mark.parametrize(
        "kms",
        [
            "alias/platform/lake",
            "arn:aws:kms:eu-north-1:111111111111:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            "arn:aws:kms:eu-north-1:111111111111:alias/platform/lake",
        ],
    )
    def test_the_named_key_becomes_the_default_by_its_arn_and_no_key_is_created(self, kms):
        resources, data = _emit(_contract(_expose(expire=None, encryption={"kms": kms})))
        assert "aws_kms_key" not in resources and "aws_kms_alias" not in resources
        # Looked up at plan time, as written; S3 is handed the key ARN, which
        # AWS recommends: an alias resolves in the account of whoever writes.
        assert data["aws_kms_key"] == {KEY: {"key_id": kms}}
        sse = resources["aws_s3_bucket_server_side_encryption_configuration"][BUCKET_KEY]
        default = sse["rule"][0]["apply_server_side_encryption_by_default"][0]
        assert default == {
            "sse_algorithm": "aws:kms",
            "kms_master_key_id": f"${{data.aws_kms_key.{KEY}.arn}}",
        }
        assert "aws_iam_policy_document" not in data

    @staticmethod
    def _preconditions(resources: Dict[str, Any]) -> Dict[str, str]:
        sse = resources["aws_s3_bucket_server_side_encryption_configuration"][BUCKET_KEY]
        return {p["condition"]: p["error_message"] for p in sse["lifecycle"]["precondition"]}

    def test_the_key_must_be_enabled_and_symmetric(self):
        resources, _ = _emit(
            _contract(_expose(expire=None, encryption={"kms": "alias/platform/lake"}))
        )
        checks = self._preconditions(resources)
        assert list(checks) == [
            f'${{data.aws_kms_key.{KEY}.key_state == "Enabled"}}',
            f'${{data.aws_kms_key.{KEY}.customer_master_key_spec == "SYMMETRIC_DEFAULT"}}',
        ]
        enabled = checks[f'${{data.aws_kms_key.{KEY}.key_state == "Enabled"}}']
        assert "alias/platform/lake" in enabled and "kms:CancelKeyDeletion" in enabled
        # Each refusal says what to do, not only what is wrong.
        symmetric = checks[
            f'${{data.aws_kms_key.{KEY}.customer_master_key_spec == "SYMMETRIC_DEFAULT"}}'
        ]
        assert "alias/platform/lake" in symmetric
        assert symmetric.endswith("Name a symmetric encryption key, or use kms: product.")

    def test_with_lake_formation_the_key_must_be_customer_managed(self):
        # The AWS managed key named by its key ARN passes the name check; the
        # plan-time lookup sees what the key is.
        aws_managed = "arn:aws:kms:eu-north-1:111111111111:key/1234abcd-12ab-34cd-56ef-1234567890ab"
        contract = _contract(
            _expose(expire=None, encryption={"kms": aws_managed}, lake_formation=_grants(OTHER))
        )
        resources, data = _emit(contract)
        checks = self._preconditions(resources)
        manager = f'${{data.aws_kms_key.{KEY}.key_manager == "CUSTOMER"}}'
        assert manager in checks
        assert "AWS managed key" in checks[manager] and "Lake Formation" in checks[manager]
        assert data["aws_kms_key"] == {KEY: {"key_id": aws_managed}}

    def test_a_product_key_is_not_looked_up(self):
        resources, data = _emit(_contract(_expose(expire=None, encryption={})))
        assert "aws_kms_key" not in data
        sse = resources["aws_s3_bucket_server_side_encryption_configuration"][BUCKET_KEY]
        assert "lifecycle" not in sse

    def test_the_aws_managed_key_is_refused_with_lake_formation(self):
        contract = _contract(
            _expose(expire=None, encryption={"kms": "alias/aws/s3"}, lake_formation=_grants(OTHER))
        )
        with pytest.raises(UnsupportedBindingError) as excinfo:
            AwsIacPlugin().emit(contract)
        assert excinfo.value.kind == "encryption-aws-managed-key"

    def test_the_aws_managed_key_is_fine_without_registration(self):
        resources, _ = _emit(_contract(_expose(expire=None, encryption={"kms": "alias/aws/s3"})))
        assert BUCKET_KEY in resources["aws_s3_bucket_server_side_encryption_configuration"]

    @pytest.mark.parametrize(
        "kms", ["product-key", "aws/s3", "arn:aws:kms:eu-north-1:1:key/x", 7, "alias/${x}"]
    )
    def test_an_unknown_value_fails_closed(self, kms):
        with pytest.raises(UnsupportedBindingError) as excinfo:
            AwsIacPlugin().emit(_contract(_expose(expire=None, encryption={"kms": kms})))
        assert excinfo.value.kind == "encryption-kms"

    def test_two_keys_for_one_bucket_are_refused(self):
        first = _expose("a", path="a/", expire=None, encryption={})
        second = _expose("b", path="b/", expire=None, encryption={"kms": "alias/other"})
        with pytest.raises(UnsupportedBindingError) as excinfo:
            AwsIacPlugin().emit(_contract(first, second))
        assert excinfo.value.kind == "encryption-conflict"


class TestEncryptionOnASharedPool:
    POOL = {"mode": "shared", "pool": "acme-pool"}

    @pytest.mark.parametrize("method", ["emit", "emit_data"])
    def test_a_product_key_on_a_pool_is_refused(self, method):
        contract = _contract(_expose(expire=None, encryption={}), packaging=self.POOL)
        with pytest.raises(PackagingError) as excinfo:
            getattr(AwsIacPlugin(), method)(contract)
        assert excinfo.value.kind == "shared-bucket-encryption"

    def test_the_pools_key_is_only_checked(self):
        contract = _contract(
            _expose(expire=None, encryption={"kms": "alias/pool/lake"}), packaging=self.POOL
        )
        resources, data = _emit(contract)
        assert "aws_s3_bucket_server_side_encryption_configuration" not in resources
        assert "aws_kms_key" not in data


# ---------------------------------------------------------------------------
# Brownfield: an adopted bucket's configurations are adopted with it
# ---------------------------------------------------------------------------


class TestBrownfieldImports:
    LIFECYCLE = f"aws_s3_bucket_lifecycle_configuration.{BUCKET_KEY}"
    SSE = f"aws_s3_bucket_server_side_encryption_configuration.{BUCKET_KEY}"

    @staticmethod
    def _imports(contract: Dict[str, Any]) -> Dict[str, str]:
        return {b.to: b.id for b in AwsIacPlugin().discover_imports(contract)}

    @pytest.fixture(autouse=True)
    def _no_catalog_lookup(self, monkeypatch):
        # discover_imports asks STS for the Glue catalog id otherwise.
        monkeypatch.setenv("AWS_ACCOUNT_ID", "111111111111")

    def test_the_lifecycle_and_encryption_are_imported_with_the_bucket(self):
        imports = self._imports(_contract(_expose(encryption={})))
        # By the bucket name, as the bucket itself is.
        assert imports[f"aws_s3_bucket.{BUCKET_KEY}"] == "acme-lake"
        assert imports[self.LIFECYCLE] == "acme-lake"
        assert imports[self.SSE] == "acme-lake"

    def test_every_import_is_a_resource_the_emit_writes(self):
        contract = _contract(_expose(encryption={"kms": "alias/platform/lake"}))
        resources, _ = _emit(contract)
        emitted = {f"{kind}.{name}" for kind, named in resources.items() for name in named}
        assert set(self._imports(contract)) <= emitted

    @pytest.mark.parametrize(
        "exposure, lifecycle, sse",
        [
            pytest.param(_expose(expire=None), False, False, id="neither"),
            pytest.param(_expose(), True, False, id="retention-only"),
            pytest.param(_expose(expire=None, encryption={}), False, True, id="encryption-only"),
            pytest.param(
                _expose(expire=None, encryption={"kms": "none"}), False, False, id="kms-none"
            ),
        ],
    )
    def test_only_what_the_emit_writes_is_imported(self, exposure, lifecycle, sse):
        imports = self._imports(_contract(exposure))
        assert (self.LIFECYCLE in imports) is lifecycle
        assert (self.SSE in imports) is sse

    def test_a_shared_pools_configurations_are_never_imported(self):
        # They are the pool owner's: importing them would re-own them.
        contract = _contract(
            _expose(encryption={"kms": "alias/pool/lake"}),
            packaging={"mode": "shared", "pool": "acme-pool"},
        )
        imports = self._imports(contract)
        assert not any("lifecycle" in a or "encryption" in a for a in imports)


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


class TestContractTextStaysInert:
    def test_a_path_or_bucket_carrying_an_interpolation_is_escaped(self):
        # SECURITY: the prefix and bucket name are contract text; only emitter
        # text (resource addresses, the caller-account token) is interpolated.
        exposure = _expose(path='orders/${file("/etc/hosts")}/', encryption={})
        exposure["binding"]["location"]["bucket"] = 'acme-${file("/etc/passwd")}'
        rendered = build_module(AwsIacPlugin(), _contract(exposure))
        live = rendered.replace("$${", "")
        assert "${file(" not in live
        assert '$${file(\\"/etc/hosts\\")}' in rendered

    @pytest.mark.parametrize("kms", ["product", "alias/platform/lake"])
    def test_every_interpolation_is_one_the_emitter_built(self, kms):
        rendered = build_module(
            AwsIacPlugin(),
            _contract(_expose(encryption={"kms": kms}, lake_formation=_grants(OTHER))),
        )
        live = re.findall(r"(?<!\$)\$\{([^}]*)\}", rendered.replace("$${", ""))
        allowed = re.compile(
            r"^(data\.)?aws_[a-z_0-9]+\.[A-Za-z0-9_]+\.[a-z_]+$|^statement\.(key|value)$"
            r"|^length\(|^\{for i, g in "
            # The existing key's preconditions: an attribute against a constant.
            r'|^data\.aws_kms_key\.[A-Za-z0-9_]+\.[a-z_]+ == \\"[A-Za-z_]+\\"$'
        )
        assert live and all(allowed.match(expr) for expr in live), live


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    @pytest.mark.parametrize(
        "encryption",
        [
            {},
            {"kms": "product"},
            {"kms": "none"},
            {"kms": "alias/platform/lake"},
            {"kms": "arn:aws:kms:eu-north-1:111111111111:key/1234abcd-12ab"},
        ],
    )
    def test_the_0_7_6_schema_accepts_the_fields(self, encryption):
        result = FluidSchemaManager().validate_contract(_contract(_expose(encryption=encryption)))
        assert result.is_valid, "\n".join(result.errors)

    @pytest.mark.parametrize("kms", ["product-key", "aws/s3", "alias/", "arn:aws:s3:::bucket"])
    def test_the_0_7_6_schema_rejects_an_unknown_kms(self, kms):
        contract = _contract(_expose(encryption={"kms": kms}))
        assert not FluidSchemaManager().validate_contract(contract).is_valid

    def test_the_fields_are_0_7_6_only(self):
        # New fields land in the preview schema; 0.7.5 GA keeps
        # additionalProperties: false and is not touched.
        for exposure in (_expose(expire=True), _expose(expire=None, encryption={})):
            contract = _contract(exposure)
            contract["fluidVersion"] = "0.7.5"
            assert not FluidSchemaManager().validate_contract(contract).is_valid
        declared_only = _contract(_expose(expire=None))
        declared_only["fluidVersion"] = "0.7.5"
        result = FluidSchemaManager().validate_contract(declared_only)
        assert result.is_valid, "\n".join(result.errors)

    def test_expire_is_an_expose_field_not_a_contract_root_one(self):
        contract = _contract(_expose())
        contract["lifecycle"] = {"state": "active", "retention": "P30D", "expire": True}
        assert not FluidSchemaManager().validate_contract(contract).is_valid

    def test_the_root_lifecycle_definition_is_unchanged(self):
        schema = json.loads((SCHEMAS / "fluid-schema-0.7.6.json").read_text(encoding="utf-8"))
        ga = json.loads((SCHEMAS / "fluid-schema-0.7.5.json").read_text(encoding="utf-8"))
        assert schema["$defs"]["lifecycle"] == ga["$defs"]["lifecycle"]
        expose_lifecycle = copy.deepcopy(schema["$defs"]["exposeLifecycle"])
        expire = expose_lifecycle["properties"].pop("expire")
        assert expire["default"] is False
        assert "AUTHORITATIVE" in expire["description"]
        kms = schema["$defs"]["binding"]["properties"]["encryption"]["properties"]["kms"]
        assert kms["default"] == "product"
        assert "7 days" in kms["description"]
