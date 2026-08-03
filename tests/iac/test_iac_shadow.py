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

"""Unit tests for shadow-compare — native ↔ OpenTofu engine parity.

Pure-function tests: no credentials, no network, no ``tofu``.
"""

from __future__ import annotations

import pytest

from fluid_build.iac import get_iac_plugin, shadow_compare
from fluid_build.iac.shadow import (
    LogicalResource,
    native_logical_resources,
    opentofu_logical_resources,
)

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _aws_contract():
    return {
        "id": "analytics.lake",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {"database": "sales", "table": "orders", "bucket": "lake"},
                },
            }
        ],
    }


# Native plan that fully matches the AWS contract above.
_AWS_NATIVE_MATCH = [
    {"op": "glue.ensure_database", "database": "sales"},
    {"op": "glue.create_table", "table": "orders"},
    {"op": "s3.create_bucket", "bucket": "lake"},
]


class TestOpentofuLogicalResources:
    def test_extracts_kind_and_identity(self):
        res = opentofu_logical_resources(_aws_contract(), get_iac_plugin("aws"))
        assert {r.kind for r in res} == {"database", "table", "bucket"}
        assert LogicalResource("bucket", "lake") in res
        assert LogicalResource("table", "orders") in res

    def test_bigquery_view_detected_as_view_not_table(self):
        contract = {
            "id": "p",
            "exposes": [
                {
                    "exposeId": "v",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_view",
                        "location": {"dataset": "d", "view": "v", "query": "SELECT 1"},
                    },
                }
            ],
        }
        res = opentofu_logical_resources(contract, get_iac_plugin("gcp"))
        assert any(r.kind == "view" for r in res)


class TestNativeLogicalResources:
    def test_maps_namespaced_ops_by_keyword(self):
        res = native_logical_resources(_AWS_NATIVE_MATCH)
        assert LogicalResource("table", "orders") in res
        assert LogicalResource("bucket", "lake") in res
        assert LogicalResource("database", "sales") in res

    def test_imperative_ops_are_skipped(self):
        # publishEvent / custom have no declarative form — not resources.
        assert native_logical_resources([{"op": "publishEvent"}, {"op": "custom"}]) == set()

    def test_action_type_field_is_a_fallback_for_op(self):
        res = native_logical_resources([{"action_type": "ensure_table", "name": "t"}])
        assert LogicalResource("table", "t") in res


class TestShadowCompare:
    def test_full_parity_is_cutover_safe(self):
        report = shadow_compare(
            _aws_contract(), plugin=get_iac_plugin("aws"), native_actions=_AWS_NATIVE_MATCH
        )
        assert report.ok is True
        assert report.parity_pct == 100.0
        assert not report.native_only

    def test_native_only_gap_blocks_cutover(self):
        # native plans a Redshift schema the AWS emitter has no resource for.
        actions = _AWS_NATIVE_MATCH + [{"op": "redshift.ensure_schema", "schema": "reporting"}]
        report = shadow_compare(
            _aws_contract(), plugin=get_iac_plugin("aws"), native_actions=actions
        )
        assert report.ok is False
        assert LogicalResource("schema", "reporting") in report.native_only

    def test_opentofu_ahead_of_native_is_still_safe(self):
        # Emitter emits resources native does not — extras never block a cutover.
        report = shadow_compare(_aws_contract(), plugin=get_iac_plugin("aws"), native_actions=[])
        assert report.ok is True
        assert report.opentofu_only
        assert report.parity_pct == 0.0

    def test_summary_names_provider_and_verdict(self):
        report = shadow_compare(
            _aws_contract(), plugin=get_iac_plugin("aws"), native_actions=_AWS_NATIVE_MATCH
        )
        assert "aws" in report.summary()
        assert "cutover-safe" in report.summary()
