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

"""The IaC access-grant surface must be expressible in a *valid* contract.

Regression cover for a gap where the GCP plugin's documented cross-project
mechanism could not be written down: it read ``metadata.policies``, which no
shipped schema permits (``metadata`` is ``additionalProperties: false``), and
``fluid generate iac`` does not run schema validation — so the emit path
worked while the contract failed ``fluid validate``.

The load-bearing assertion here is the pairing: the *same* contract both
validates **and** emits the cross-project access entry.
"""

from __future__ import annotations

import pytest

from fluid_build.iac.access import (
    GROUP,
    SERVICE_ACCOUNT,
    USER,
    normalize_access_grants,
)
from fluid_build.iac.providers.gcp import _bq_access_entries
from fluid_build.schema_manager import FluidSchemaManager

# Every version whose schema carries `accessPolicy` and is still supported.
SCHEMA_VERSIONS = ("0.7.3", "0.7.4", "0.7.5", "0.7.6")

CONSUMER_SA = "consumer@other-project.iam.gserviceaccount.com"


def _contract(version: str = "0.7.6", **extra):
    """A minimal contract that validates on its own."""
    base = {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "gold.xproj_demo_v1",
        "name": "Cross-project demo",
        "description": "Fixture for the access-grant surface.",
        "domain": "Customer",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "Platform", "email": "platform@example.com"},
        },
        "exposes": [
            {
                "exposeId": "customers",
                "kind": "table",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"project": "producer", "dataset": "gold", "table": "customers"},
                },
                "contract": {"schema": [{"name": "customer_id", "type": "STRING"}]},
            }
        ],
    }
    base.update(extra)
    return base


def _validate(contract, version):
    return FluidSchemaManager().validate_contract(contract, version, offline_only=True)


class TestCrossProjectGrantIsExpressible:
    """The point of the fix: valid contract AND correct emit, together."""

    @pytest.mark.parametrize("version", SCHEMA_VERSIONS)
    def test_access_policy_cross_project_grant_validates_and_emits(self, version):
        contract = _contract(
            version,
            accessPolicy={
                "grants": [{"principal": f"serviceAccount:{CONSUMER_SA}", "permissions": ["read"]}]
            },
        )

        result = _validate(contract, version)
        assert result.is_valid, f"{version}: {result.errors}"

        entries = _bq_access_entries(normalize_access_grants(contract))
        assert {"role": "READER", "user_by_email": CONSUMER_SA} in entries

    def test_the_legacy_surface_is_still_schema_invalid(self):
        """Guards the premise — if this ever passes, the fix can be revisited."""
        contract = _contract()
        contract["metadata"]["policies"] = {
            "consumers": {"principals": [CONSUMER_SA], "permissions": ["read"]}
        }
        result = _validate(contract, "0.7.6")
        assert not result.is_valid
        assert any("policies" in str(e) for e in result.errors)

    def test_legacy_surface_still_emits_for_back_compat(self):
        """Out-of-tree contracts on the old surface must keep working."""
        contract = _contract()
        contract["metadata"]["policies"] = {
            "consumers": {"principals": [CONSUMER_SA], "permissions": ["read"]}
        }
        entries = _bq_access_entries(normalize_access_grants(contract))
        assert {"role": "READER", "user_by_email": CONSUMER_SA} in entries

    def test_both_surfaces_together_do_not_drop_grants(self):
        """A contract mid-migration keeps grants from both surfaces."""
        contract = _contract(
            accessPolicy={
                "grants": [{"principal": "user:new@example.com", "permissions": ["read"]}]
            }
        )
        contract["metadata"]["policies"] = {
            "old": {"principals": ["legacy@example.com"], "permissions": ["read"]}
        }
        emails = {
            e.get("user_by_email") for e in _bq_access_entries(normalize_access_grants(contract))
        }
        assert {"new@example.com", "legacy@example.com"} <= emails


class TestPrincipalTyping:
    """``accessPolicy`` declares the type; the legacy surface can only guess."""

    def test_declared_group_is_not_mis_filed_as_a_user(self):
        """The bug the explicit prefix fixes.

        A group address contains ``@``, so the legacy ``"@" in principal``
        heuristic classified every group as ``user_by_email``. Declaring
        ``group:`` produces the correct BigQuery field.
        """
        contract = _contract(
            accessPolicy={
                "grants": [{"principal": "group:data-team@company.com", "permissions": ["read"]}]
            }
        )
        assert _validate(contract, "0.7.6").is_valid
        assert _bq_access_entries(normalize_access_grants(contract)) == [
            {"role": "READER", "group_by_email": "data-team@company.com"}
        ]

    @pytest.mark.parametrize(
        "principal,expected_type",
        [
            (f"serviceAccount:{CONSUMER_SA}", SERVICE_ACCOUNT),
            ("group:team@company.com", GROUP),
            ("user:alice@company.com", USER),
            # Unprefixed input keeps the legacy inference, bit for bit.
            (CONSUMER_SA, SERVICE_ACCOUNT),
            ("alice@company.com", USER),
            ("bare-group-name", GROUP),
        ],
    )
    def test_principal_type_resolution(self, principal, expected_type):
        contract = _contract(
            accessPolicy={"grants": [{"principal": principal, "permissions": ["read"]}]}
        )
        grants = normalize_access_grants(contract)
        assert len(grants) == 1
        assert grants[0].principal_type == expected_type
        assert ":" not in grants[0].principal, "the prefix must be stripped from the identity"

    def test_service_account_uses_user_by_email(self):
        """BigQuery's own convention for SA identities — what makes x-project work."""
        contract = _contract(
            accessPolicy={
                "grants": [{"principal": f"serviceAccount:{CONSUMER_SA}", "permissions": ["read"]}]
            }
        )
        assert _bq_access_entries(normalize_access_grants(contract)) == [
            {"role": "READER", "user_by_email": CONSUMER_SA}
        ]


class TestNormalizationRules:
    def test_unmapped_permissions_are_skipped_not_emitted(self):
        contract = _contract(
            accessPolicy={"grants": [{"principal": "user:a@b.com", "permissions": ["manage"]}]}
        )
        # `manage` has no BigQuery dataset-role mapping.
        assert _bq_access_entries(normalize_access_grants(contract)) == []

    def test_permissions_collapsing_to_one_role_emit_once(self):
        contract = _contract(
            accessPolicy={
                "grants": [
                    {"principal": "user:a@b.com", "permissions": ["read", "select", "query"]}
                ]
            }
        )
        assert _bq_access_entries(normalize_access_grants(contract)) == [
            {"role": "READER", "user_by_email": "a@b.com"}
        ]

    def test_duplicate_grants_collapse(self):
        contract = _contract(
            accessPolicy={
                "grants": [
                    {"principal": "user:a@b.com", "permissions": ["read"]},
                    {"principal": "user:a@b.com", "permissions": ["read"]},
                ]
            }
        )
        assert len(normalize_access_grants(contract)) == 1

    def test_a_grant_without_permissions_is_dropped(self):
        contract = _contract(accessPolicy={"grants": [{"principal": "user:a@b.com"}]})
        assert normalize_access_grants(contract) == ()

    def test_no_access_surface_yields_no_grants(self):
        assert normalize_access_grants(_contract()) == ()
