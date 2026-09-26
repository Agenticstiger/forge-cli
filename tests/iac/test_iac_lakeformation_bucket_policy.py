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

"""``binding.governance.lakeFormation.bucketPolicy`` — who gets an S3 bucket-policy statement.

A bucket-policy ``Allow`` lets its principal read the Parquet straight from S3,
which skips Lake Formation's column / row / cell filters and its revocations.
Before this field every ``arn:`` grantee got one, same-account principals
included. Now:

* ``cross-account`` (default) — only grantees in another account than the one
  applying. The account is only known at plan time, so the emit carries the
  filter as HCL (``data.aws_arn`` per grantee vs
  ``data.aws_caller_identity``); the rendering is pinned here and its
  evaluation by a real ``tofu plan`` in
  ``test_iac_lakeformation_bucket_policy_plan.py``.
* ``none`` — no bucket policy.
* ``all-grantees`` — the old emit, byte for byte.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import fluid_build
from fluid_build.iac import build_module
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.packaging import PackagingError
from fluid_build.iac.providers.aws import AwsIacPlugin
from fluid_build.schema_manager import FluidSchemaManager

from ._lf_bucket_policy import only_policy, policy_statements

pytestmark = [pytest.mark.unit, pytest.mark.provider]

SAME = "arn:aws:iam::111111111111:role/same-account-reader"
OTHER = "arn:aws:iam::222222222222:role/other-account-reader"
CALLER = "data.aws_caller_identity.fluid_lf_caller.account_id"
SCHEMAS = Path(fluid_build.__file__).resolve().parent / "schemas"


def _contract(
    grantees: List[str],
    *,
    bucket_policy: Optional[str] = None,
    packaging: Optional[Dict[str, Any]] = None,
    path: Optional[str] = "orders/",
    register: bool = True,
) -> Dict[str, Any]:
    lake_formation: Dict[str, Any] = {
        "grants": [{"principal": g, "permissions": ["SELECT", "DESCRIBE"]} for g in grantees]
    }
    if register:
        lake_formation["registerLocation"] = True
    if bucket_policy is not None:
        lake_formation["bucketPolicy"] = bucket_policy
    location: Dict[str, Any] = {"bucket": "acme-lake", "database": "sales", "table": "orders"}
    if path is not None:
        location["path"] = path
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "lf.bucket.policy",
        "name": "LF bucket policy",
        "metadata": {"layer": "Silver", "owner": {"team": "data", "email": "d@example.com"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": location,
                    "governance": {"lakeFormation": lake_formation},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }
    if packaging is not None:
        contract["packaging"] = packaging
    return contract


def _emit(contract: Dict[str, Any]):
    plugin = AwsIacPlugin()
    return plugin.emit(contract), plugin.emit_data(contract)


# ---------------------------------------------------------------------------
# Default — cross-account
# ---------------------------------------------------------------------------


class TestDefaultKeepsOnlyOtherAccountGrantees:
    def test_no_grantee_is_written_into_the_bucket_policy_literally(self):
        # The resource carries no statement of its own, so nothing on it can
        # name the same-account grantee; its policy is the plan-time document.
        resources, _ = _emit(_contract([SAME, OTHER]))
        policy = only_policy(resources)
        assert set(policy) == {"bucket", "count", "policy"}
        assert policy["policy"] == (
            "${data.aws_iam_policy_document.lf_bucket_policy_lf_bucket_policy_acme_lake.json}"
        )
        assert SAME not in json.dumps(policy) and OTHER not in json.dumps(policy)

    def test_every_grantee_is_compared_with_the_applying_account(self):
        resources, data = _emit(_contract([SAME, OTHER]))
        policy = only_policy(resources)
        arns = data["aws_arn"]
        assert [body["arn"] for body in arns.values()] == [SAME, OTHER]
        keep = (
            "{for i, g in [data.aws_arn.lf_bucket_policy_lf_bucket_policy_acme_lake_grantee_0, "
            "data.aws_arn.lf_bucket_policy_lf_bucket_policy_acme_lake_grantee_1] : "
            f"tostring(i) => g.arn if g.account != {CALLER}}}"
        )
        # No instance at all when no grantee is left: an empty authoritative
        # policy would still wipe the bucket's other statements.
        assert policy["count"] == "${length(" + keep + ") > 0 ? 1 : 0}"
        document = data["aws_iam_policy_document"]["lf_bucket_policy_lf_bucket_policy_acme_lake"]
        assert [block["for_each"] for block in document["dynamic"]["statement"]] == [
            "${" + keep + "}"
        ] * 2
        assert "fluid_lf_caller" in data["aws_caller_identity"]

    def test_the_candidates_are_exactly_the_all_grantees_statements(self):
        # The default is a FILTER over the old statements, never a widening:
        # expanded for every grantee, the plan-time document yields the very
        # statements all-grantees writes out (same Sids, principals, actions,
        # resources).
        resources, data = _emit(_contract([SAME, OTHER]))
        default = policy_statements(only_policy(resources), data)
        legacy_resources, legacy_data = _emit(
            _contract([SAME, OTHER], bucket_policy="all-grantees")
        )
        assert default == policy_statements(only_policy(legacy_resources), legacy_data)

    def test_a_pool_bucket_keeps_its_prefix_scoping(self):
        contract = _contract([OTHER], packaging={"mode": "shared", "pool": "acme-pool"})
        resources, data = _emit(contract)
        policy = only_policy(resources)
        assert policy["bucket"] == "${data.aws_s3_bucket.lf_bucket_policy_acme_lake.id}"
        statements = {s["Sid"]: s for s in policy_statements(policy, data)}
        assert statements["FluidLfBucketList0"]["Condition"] == {
            "StringLike": {"s3:prefix": ["orders/*"]}
        }
        assert statements["FluidLfBucketGet0"]["Resource"] == "arn:aws:s3:::acme-lake/orders/*"

    def test_a_pool_bucket_without_a_path_still_fails_closed(self):
        contract = _contract(
            [OTHER], packaging={"mode": "shared", "pool": "acme-pool"}, path=None, register=False
        )
        with pytest.raises(PackagingError) as excinfo:
            AwsIacPlugin().emit(contract)
        assert excinfo.value.kind == "shared-bucket-requires-path"

    def test_every_reference_resolves_including_data_to_data(self):
        resources, data = _emit(_contract([SAME, OTHER]))
        rendered = build_module(AwsIacPlugin(), _contract([SAME, OTHER]))
        referenced = set(re.findall(r"data\.(aws_[a-z_]+)\.([A-Za-z0-9_]+)", rendered))
        declared = {(dtype, name) for dtype, block in data.items() for name in block}
        assert referenced <= declared, f"dangling: {sorted(referenced - declared)}"
        # ...and nothing is declared that nobody reads.
        assert declared <= referenced, f"unused: {sorted(declared - referenced)}"

    def test_contract_text_never_reaches_an_expression(self):
        # SECURITY: the grantee ARNs, bucket and path stay plain strings, which
        # the renderer escapes; only emitter-built text is interpolated. A
        # principal carrying an interpolation must come out inert.
        evil = 'arn:aws:iam::222222222222:role/${file("/etc/passwd")}'
        contract = _contract([evil], path='orders/${file("/etc/hosts")}/')
        rendered = build_module(AwsIacPlugin(), contract)
        live = rendered.replace("$${", "")
        assert "${file(" not in live
        assert 'role/$${file(\\"/etc/passwd\\")}' in rendered

    def test_a_non_arn_principal_gets_no_statement(self):
        # Unchanged from before the field: only ``arn:`` principals are
        # bucket-policy candidates.
        resources, _ = _emit(_contract(["IAM_ALLOWED_PRINCIPALS"]))
        assert "aws_s3_bucket_policy" not in resources


# ---------------------------------------------------------------------------
# all-grantees — the previous emit, restored
# ---------------------------------------------------------------------------


class TestAllGranteesRestoresThePreviousEmit:
    def test_the_policy_is_the_previous_literal_document(self):
        resources, data = _emit(_contract([SAME, OTHER], bucket_policy="all-grantees"))
        policy = only_policy(resources)
        assert set(policy) == {"bucket", "policy"}, "no count: always one instance"
        statements = []
        for index, principal in enumerate([SAME, OTHER]):
            statements.append(
                {
                    "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                    "Effect": "Allow",
                    "Principal": {"AWS": principal},
                    "Resource": "arn:aws:s3:::acme-lake",
                    "Sid": f"FluidLfBucketList{index}",
                }
            )
            statements.append(
                {
                    "Action": ["s3:GetObject"],
                    "Effect": "Allow",
                    "Principal": {"AWS": principal},
                    "Resource": "arn:aws:s3:::acme-lake/orders/*",
                    "Sid": f"FluidLfBucketGet{index}",
                }
            )
        assert policy["policy"] == json.dumps(
            {"Statement": statements, "Version": "2012-10-17"},
            sort_keys=True,
            separators=(",", ":"),
        )
        assert "aws_arn" not in data and "aws_iam_policy_document" not in data

    def test_the_rendered_module_matches_the_default_everywhere_else(self):
        # Only the bucket policy (and the contract echoed into the Glue table
        # parameters) differs between the modes; every other resource is the
        # same emit.
        def without_policy(contract):
            document = json.loads(build_module(AwsIacPlugin(), contract))
            document["resource"].pop("aws_s3_bucket_policy", None)
            for dtype in ("aws_arn", "aws_iam_policy_document"):
                document["data"].pop(dtype, None)
            for table in document["resource"]["aws_glue_catalog_table"].values():
                table["parameters"].pop("fluid_contract", None)
            return document

        base = _contract([SAME, OTHER])
        assert without_policy(base) == without_policy(
            _contract([SAME, OTHER], bucket_policy="all-grantees")
        )
        assert without_policy(base) == without_policy(
            _contract([SAME, OTHER], bucket_policy="none")
        )


# ---------------------------------------------------------------------------
# none
# ---------------------------------------------------------------------------


class TestNoneEmitsNoBucketPolicy:
    def test_no_policy_and_no_policy_data_sources(self):
        resources, data = _emit(_contract([SAME, OTHER], bucket_policy="none"))
        assert "aws_s3_bucket_policy" not in resources
        assert "aws_arn" not in data and "aws_iam_policy_document" not in data
        # The Lake Formation side is untouched.
        assert len(resources["aws_lakeformation_permissions"]) == 2
        assert len(resources["aws_lakeformation_resource"]) == 1

    def test_a_pool_without_a_path_is_fine_when_nothing_bucket_level_is_emitted(self):
        # The fail-closed rule exists because the bucket policy would widen to
        # the whole pool. With no bucket policy and no registration there is
        # no bucket-level control to widen.
        contract = _contract(
            [OTHER],
            bucket_policy="none",
            packaging={"mode": "shared", "pool": "acme-pool"},
            path=None,
            register=False,
        )
        resources, _ = _emit(contract)
        assert "aws_s3_bucket_policy" not in resources
        assert "aws_lakeformation_permissions" in resources


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestBucketPolicyValue:
    @pytest.mark.parametrize("method", ["emit", "emit_data"])
    def test_an_unknown_value_fails_closed(self, method):
        contract = _contract([OTHER], bucket_policy="all_grantees")
        with pytest.raises(UnsupportedBindingError) as excinfo:
            getattr(AwsIacPlugin(), method)(contract)
        assert excinfo.value.kind == "lakeformation-bucket-policy"
        assert "'all_grantees'" in str(excinfo.value)
        assert excinfo.value.remediation

    @pytest.mark.parametrize("value", ["cross-account", "none", "all-grantees"])
    def test_the_0_7_6_schema_accepts_each_value(self, value):
        result = FluidSchemaManager().validate_contract(_contract([OTHER], bucket_policy=value))
        assert result.is_valid, "\n".join(result.errors)

    def test_the_0_7_6_schema_rejects_an_unknown_value(self):
        result = FluidSchemaManager().validate_contract(
            _contract([OTHER], bucket_policy="all_grantees")
        )
        assert not result.is_valid

    def test_the_field_is_0_7_6_only(self):
        # New fields land in the preview schema; the GA 0.7.5 keeps
        # additionalProperties: false. A 0.7.5 contract gets the default.
        contract = _contract([OTHER], bucket_policy="none")
        contract["fluidVersion"] = "0.7.5"
        assert not FluidSchemaManager().validate_contract(contract).is_valid
        del contract["exposes"][0]["binding"]["governance"]["lakeFormation"]["bucketPolicy"]
        assert FluidSchemaManager().validate_contract(contract).is_valid

    def test_the_schema_documents_the_bypass_and_the_default(self):
        schema = json.loads((SCHEMAS / "fluid-schema-0.7.6.json").read_text(encoding="utf-8"))
        field = schema["$defs"]["bindingGovernance"]["properties"]["lakeFormation"]["properties"][
            "bucketPolicy"
        ]
        assert field["default"] == "cross-account"
        assert field["enum"] == ["cross-account", "none", "all-grantees"]
        assert "straight from S3" in field["description"]
        assert "authoritative" in field["description"]
