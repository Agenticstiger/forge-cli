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

"""Pin tests for forge-cli bugs surfaced by the snowflake-biz-lab E2E
run.

Each ``Test*`` class corresponds to one fix, with comments linking back
to the symptom the demo exposed:

* :class:`TestDmmListParserReadsTopLevelFields` — ``fluid datamesh-manager
  list`` rendered every row as ``?`` because the parser still expected
  the v0 ``info.id`` / ``info.name`` shape; the live Entropy Data API
  returns those fields at the top level.
* :class:`TestSnowflakeProvisionDatasetActionIdResolution` — the snowflake
  provider's ``_handle_abstract_provision_dataset`` read
  ``action.get("id")`` while the plan stage emits actions with an
  ``action_id`` key, so ``target_id`` was always ``None`` and the
  fallback silently grabbed the FIRST expose's columns. This leaked 7
  cross-expose columns onto the ``subscriber_health_scorecard`` table on
  every A1 apply (cols from ``subscriber360_core``).
* :class:`TestVerifyStrictCriticalOnly` — ``fluid verify --strict``
  failed builds for any mismatch, including the constraint-only
  WARNING-level drift dbt-built tables produce by default (nullable
  cols vs ``required: true`` in the contract). Now strict only fails
  on CRITICAL severity (missing fields, type mismatches, region drift)
  + errors.
* :class:`TestDmmPublishDefaultsToOdps` — ``fluid datamesh-manager publish``
  used to default ``dataProductSpecification`` to DPS ``0.0.1``, but
  Entropy Data has migrated to ODPS-only and rejects DPS payloads with
  HTTP 400 ("Specification type 'dps' is not supported in this
  organization"). The catalog provider path
  (``fluid publish --target datamesh-manager``) auto-falls-back via
  ``_should_retry_with_odps``; the direct CLI path silently failed.
  Default is now ODPS for both surfaces; legacy DPS shape stays
  reachable via ``--data-product-spec 0.0.1`` or
  ``provider_hint='dps'``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bug #2: DMM list parser reads ``info.id`` instead of top-level ``id``.
# ---------------------------------------------------------------------------


class TestDmmPublishDefaultsToOdps:
    """``fluid datamesh-manager publish`` (the direct CLI subcommand)
    must produce an ODPS-shape payload by default, matching what
    Entropy Data accepts. The legacy DPS shape (``0.0.1``) stays
    reachable via explicit ``--data-product-spec 0.0.1`` or
    ``provider_hint='dps'``."""

    def _make_provider(self):
        from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

        return DataMeshManagerProvider(api_key="dummy", api_url="https://api.entropy-data.com")

    def _sample_contract(self) -> Dict[str, Any]:
        return {
            "id": "demo.product",
            "metadata": {
                "name": "Demo Product",
                "description": "demo",
                "status": "active",
                "owner": {"team": "demo-team"},
            },
            "owner": {"team": "demo-team"},
            "exposes": [],
            "expects": [],
        }

    def test_resolver_default_is_odps(self):
        """``_resolve_data_product_specification(None)`` returns ODPS,
        not the legacy DPS ``0.0.1``."""
        provider = self._make_provider()

        resolved = provider._resolve_data_product_specification(None)

        assert resolved == provider.DATA_PRODUCT_SPEC_ODPS == "odps"

    def test_apply_dry_run_emits_odps_shape_by_default(self):
        """No explicit spec, no provider hint → ODPS-Bitol payload
        (``apiVersion: v1.0.0`` + ``kind: DataProduct`` + no ``info``
        block)."""
        provider = self._make_provider()

        result = provider.apply(self._sample_contract(), dry_run=True)
        payload = result["payload"]

        assert payload["apiVersion"] == "v1.0.0"
        assert payload["kind"] == "DataProduct"
        assert "info" not in payload
        # Critical: no top-level ``dataProductSpecification: 0.0.1`` —
        # that's the field DMM rejected with HTTP 400.
        assert payload.get("dataProductSpecification") != "0.0.1"

    def test_legacy_dps_reachable_via_explicit_spec(self):
        """Out-of-tree callers that still need the DPS ``0.0.1`` shape
        opt in by passing ``data_product_specification='0.0.1'``
        (the CLI form is ``--data-product-spec 0.0.1``)."""
        provider = self._make_provider()

        result = provider.apply(
            self._sample_contract(),
            dry_run=True,
            data_product_specification="0.0.1",
        )

        assert result["payload"]["dataProductSpecification"] == "0.0.1"

    def test_legacy_dps_reachable_via_provider_hint(self):
        """``provider_hint='dps'`` is the symmetric inverse of the
        existing ``provider_hint='odps'`` form."""
        provider = self._make_provider()

        result = provider.apply(self._sample_contract(), dry_run=True, provider_hint="dps")

        assert result["payload"]["dataProductSpecification"] == "0.0.1"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
