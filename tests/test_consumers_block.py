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

"""Pin the v0.7.6 `consumers:` block — declared downstream consumers in the
dbt-exposures shape (name/label/type/owner/url/maturity/description) — with owner
mirroring metadata.owner's {team,email} rather than dbt's {name,email} — plus the
FLUID-native `exposeIds` port refinement. Additive and optional: contracts
without the block are untouched, and the block never affects plan/apply."""

from __future__ import annotations

import copy

from fluid_build.schema_manager import FluidSchemaManager


def _minimal_076_contract() -> dict:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "test.commerce.orders",
        "name": "Orders",
        "metadata": {"layer": "Gold", "owner": {"team": "commerce-data"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {"database": "gold", "table": "orders", "bucket": "fluid-lake"},
                },
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "string", "required": True},
                    ]
                },
            }
        ],
    }


def _with_consumers(consumers: list) -> dict:
    contract = _minimal_076_contract()
    contract["consumers"] = consumers
    return contract


FULL_CONSUMER = {
    "name": "weekly_revenue_dashboard",
    "label": "Weekly Revenue Dashboard",
    "type": "dashboard",
    "owner": {"team": "finance", "email": "dana@example.com"},
    "url": "https://bi.example.com/dashboards/weekly-revenue",
    "maturity": "high",
    "description": "The finance team's Monday-morning revenue view.",
    "exposeIds": ["orders"],
}


def _validate(contract: dict):
    return FluidSchemaManager().validate_contract(contract, offline_only=True)


# ---------------------------------------------------------------------------
# Additive: absence changes nothing
# ---------------------------------------------------------------------------


def test_contract_without_consumers_still_validates():
    vr = _validate(_minimal_076_contract())
    assert vr.is_valid is True


def test_empty_consumers_list_validates():
    vr = _validate(_with_consumers([]))
    assert vr.is_valid is True


# ---------------------------------------------------------------------------
# The dbt-exposures shape
# ---------------------------------------------------------------------------


def test_full_consumer_entry_validates():
    vr = _validate(_with_consumers([FULL_CONSUMER]))
    assert vr.is_valid is True


def test_minimal_consumer_needs_only_name_and_type():
    vr = _validate(_with_consumers([{"name": "adhoc_churn_analysis", "type": "analysis"}]))
    assert vr.is_valid is True


def test_consumer_missing_type_is_rejected():
    vr = _validate(_with_consumers([{"name": "mystery"}]))
    assert vr.is_valid is False


def test_consumer_missing_name_is_rejected():
    vr = _validate(_with_consumers([{"type": "dashboard"}]))
    assert vr.is_valid is False


def test_consumer_type_enum_is_closed():
    bad = dict(FULL_CONSUMER, type="spreadsheet")
    vr = _validate(_with_consumers([bad]))
    assert vr.is_valid is False


def test_maturity_enum_is_closed():
    bad = dict(FULL_CONSUMER, maturity="production")
    vr = _validate(_with_consumers([bad]))
    assert vr.is_valid is False


def test_unknown_consumer_key_is_rejected():
    bad = dict(FULL_CONSUMER, depends_on=["ref('orders')"])
    vr = _validate(_with_consumers([bad]))
    assert vr.is_valid is False


def test_unknown_owner_key_is_rejected():
    bad = copy.deepcopy(FULL_CONSUMER)
    bad["owner"]["slack"] = "#finance"
    vr = _validate(_with_consumers([bad]))
    assert vr.is_valid is False


def test_dbt_owner_name_is_rejected_in_favour_of_team():
    """The deliberate divergence from dbt exposures: owner is {team,email},
    matching metadata.owner, so 'owner' means one thing across the standard.
    A dbt importer maps exposure owner.name -> team rather than the schema
    carrying two spellings of the same idea forever."""
    bad = copy.deepcopy(FULL_CONSUMER)
    bad["owner"]["name"] = "Dana Finance"
    vr = _validate(_with_consumers([bad]))
    assert vr.is_valid is False


def test_owner_email_format_is_annotation_only():
    # The validator runs without a FormatChecker (matching metadata.owner), so
    # `format: email` documents intent but does not reject — pin that so a
    # future FormatChecker rollout is a deliberate, versioned change.
    loose = copy.deepcopy(FULL_CONSUMER)
    loose["owner"]["email"] = "not-an-email"
    vr = _validate(_with_consumers([loose]))
    assert vr.is_valid is True


# ---------------------------------------------------------------------------
# The FLUID-native port refinement
# ---------------------------------------------------------------------------


def test_expose_ids_accepts_identifier_list():
    ok = dict(FULL_CONSUMER, exposeIds=["orders"])
    vr = _validate(_with_consumers([ok]))
    assert vr.is_valid is True


def test_consumers_block_is_rejected_on_075_contracts():
    contract = _with_consumers([FULL_CONSUMER])
    contract["fluidVersion"] = "0.7.5"
    vr = _validate(contract)
    assert vr.is_valid is False
