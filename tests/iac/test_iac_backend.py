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

"""Unit tests for OpenTofu state-backend generation."""

from __future__ import annotations

import pytest

from fluid_build.iac import assemble_tofu_document
from fluid_build.iac.backend import parse_backend

pytestmark = pytest.mark.unit


class TestParseBackend:
    def test_none_or_empty_means_local_state(self):
        assert parse_backend(None) is None
        assert parse_backend("") is None

    def test_s3_backend(self):
        block = parse_backend("s3://my-state-bucket/fluid/prod.tfstate")
        assert block == {"s3": {"bucket": "my-state-bucket", "key": "fluid/prod.tfstate"}}

    def test_s3_backend_supplies_a_default_key(self):
        block = parse_backend("s3://my-state-bucket")
        assert block["s3"]["bucket"] == "my-state-bucket"
        assert block["s3"]["key"]

    def test_gcs_backend(self):
        block = parse_backend("gcs://my-state-bucket/fluid")
        assert block == {"gcs": {"bucket": "my-state-bucket", "prefix": "fluid"}}

    def test_unsupported_scheme_raises(self):
        with pytest.raises(ValueError):
            parse_backend("azurerm://container/key")

    def test_bucketless_spec_raises(self):
        with pytest.raises(ValueError):
            parse_backend("s3://")


class TestPerContractDefault:
    """``per_contract_default``: what ``fluid apply`` asks for when the spec
    came from ``FLUID_STATE_BACKEND``."""

    CONTRACT = {"id": "bronze.customer_subscriptions", "exposes": []}

    def test_s3_default_key_is_per_contract_without_packaging(self):
        block = parse_backend("s3://state", self.CONTRACT, per_contract_default=True)
        assert block == {
            "s3": {
                "bucket": "state",
                "key": "fluid/bronze.customer_subscriptions/terraform.tfstate",
            }
        }

    def test_gcs_default_prefix_is_per_contract_without_packaging(self):
        block = parse_backend("gcs://state", self.CONTRACT, per_contract_default=True)
        assert block == {
            "gcs": {"bucket": "state", "prefix": "fluid/bronze.customer_subscriptions"}
        }

    #: Schema-valid ids that differ only in ``.``, ``-`` and ``_``, or in a
    #: leading or trailing ``_``. Measured before: all four got
    #: ``fluid/bronze_customer_subscriptions/terraform.tfstate``, one state.
    NEAR_IDS = (
        "bronze.customer-subscriptions",
        "bronze.customer_subscriptions",
        "bronze_customer.subscriptions",
        "_bronze.customer_subscriptions_",
    )

    @pytest.mark.parametrize("spec", ["s3://state", "gcs://state"])
    def test_ids_that_differ_only_in_separators_get_different_states(self, spec):
        blocks = [
            parse_backend(spec, {"id": cid, "exposes": []}, per_contract_default=True)
            for cid in self.NEAR_IDS
        ]
        places = [b.get("s3", {}).get("key") or b["gcs"]["prefix"] for b in blocks]
        assert len(set(places)) == len(self.NEAR_IDS), places

    @pytest.mark.parametrize(
        "bad_id",
        [None, "", "a/b", "../x", ".hidden", "trailing-", "a b", "a\n", "id\u00e9", 7],
    )
    def test_an_id_that_cannot_name_a_state_is_refused(self, bad_id):
        with pytest.raises(ValueError, match="cannot name its own state"):
            parse_backend("s3://state", {"id": bad_id}, per_contract_default=True)
        with pytest.raises(ValueError, match="cannot name its own state"):
            parse_backend("gcs://state", {"id": bad_id}, per_contract_default=True)

    def test_an_explicit_key_needs_no_usable_id(self):
        contract = {"id": "a/b"}
        assert parse_backend("s3://state/k.tfstate", contract, per_contract_default=True) == {
            "s3": {"bucket": "state", "key": "k.tfstate"}
        }
        assert parse_backend("gcs://state/p", contract, per_contract_default=True) == {
            "gcs": {"bucket": "state", "prefix": "p"}
        }

    def test_an_explicit_key_or_prefix_still_wins(self):
        assert parse_backend("s3://state/k.tfstate", self.CONTRACT, per_contract_default=True) == {
            "s3": {"bucket": "state", "key": "k.tfstate"}
        }
        assert parse_backend("gcs://state/p", self.CONTRACT, per_contract_default=True) == {
            "gcs": {"bucket": "state", "prefix": "p"}
        }

    def test_without_it_a_contract_without_packaging_keeps_the_legacy_key(self):
        assert parse_backend("s3://state", self.CONTRACT)["s3"]["key"] == "fluid/terraform.tfstate"
        assert parse_backend("gcs://state", self.CONTRACT) == {"gcs": {"bucket": "state"}}

    def test_a_malformed_packaging_block_still_gets_a_per_contract_key(self):
        contract = dict(self.CONTRACT, packaging="not-a-mapping")
        block = parse_backend("s3://state", contract, per_contract_default=True)
        assert block["s3"]["key"] == "fluid/bronze.customer_subscriptions/terraform.tfstate"


class TestBackendInDocument:
    def test_backend_block_lands_in_terraform(self):
        doc = assemble_tofu_document(
            required_providers={"google": {"source": "hashicorp/google", "version": "~> 6.0"}},
            resources={},
            backend={"gcs": {"bucket": "b"}},
        )
        assert doc["terraform"]["backend"] == {"gcs": {"bucket": "b"}}

    def test_no_backend_keeps_local_state(self):
        doc = assemble_tofu_document(
            required_providers={"aws": {"source": "hashicorp/aws", "version": "~> 5.0"}},
            resources={},
        )
        assert "backend" not in doc["terraform"]
