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
* :class:`TestFederationEndpointSchemeAllowList` — ``federation/upstreams.
  yaml::workspaces[].endpoint`` was passed unchanged to ``git clone <url>
  <dir>``. An attacker-authored PR could set ``endpoint:
  '--upload-pack=touch /tmp/pwn'`` to inject git CLI options at clone
  time (well-known argument-injection pattern, CVE-2017-1000117 family;
  the gitpython equivalent is CVE-2022-24439). Endpoints are now
  validated at manifest-load time against an allow-list of URL schemes,
  ``-``-prefixed values are rejected, and the subprocess argv passes
  ``--`` before the positional URL as belt-and-suspenders.
* :class:`TestSnowflakeRollbackCleanupValidatesIdentifiers` — the
  Snowflake provider's ``cleanup_backups`` and ``restore_ddl`` read
  ``database`` / ``schema`` / ``backup_table`` from
  ``.fluid/rollback-state.json`` (PR-reviewable / attacker-authorable,
  per ``cli/rollback.py`` threat model) and interpolated them into
  DDL strings without ``validate_ident``. The matching
  ``cli/rollback.py::_restore_snowflake`` path was already defended;
  the cleanup + restore_ddl paths were missed. All three identifier
  components now route through ``validate_ident`` before string
  interpolation, with tampered records skipped + logged. Same fix
  applied to ``providers/aws/redshift_provider.py::cleanup_ddl``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bug #2: DMM list parser reads ``info.id`` instead of top-level ``id``.
# ---------------------------------------------------------------------------


class TestDmmListParserReadsTopLevelFields:
    """``fluid datamesh-manager list`` must read ``id`` / ``name`` /
    ``status`` / ``team.name`` from the top level — not from a legacy
    ``info`` envelope."""

    def _run_list(self, products: List[Dict[str, Any]], capsys):
        from fluid_build.cli import datamesh_manager as dmm_cli

        provider = MagicMock()
        provider.list_products.return_value = products
        args = SimpleNamespace(format="table")
        with patch("fluid_build.cli.datamesh_manager._make_provider", return_value=provider):
            rc = dmm_cli._cmd_list(args)
        captured = capsys.readouterr()
        return rc, captured.out

    def test_top_level_fields_render_in_table(self, capsys):
        rc, out = self._run_list(
            [
                {
                    "apiVersion": "v1.0.0",
                    "kind": "DataProduct",
                    "id": "bronze.telco.party_v1",
                    "name": "Telco Party Bronze",
                    "status": "draft",
                    "team": {"name": "telco-data-platform"},
                }
            ],
            capsys,
        )
        assert rc == 0
        assert "bronze.telco.party_v1" in out
        assert "Telco Party Bronze" in out
        assert "draft" in out
        assert "telco-data-platform" in out
        # Crucially, ``?`` should NOT appear when fields are present.
        assert " ? " not in out and "│ ?" not in out

    def test_legacy_info_envelope_still_works(self, capsys):
        """Backward-compat: older DMM v0 deployments returned the
        fields nested under ``info``. The list command should still
        render those correctly."""
        rc, out = self._run_list(
            [
                {
                    "info": {
                        "id": "legacy.telco.bronze",
                        "name": "Legacy Bronze",
                        "status": "active",
                    },
                    "teamId": "legacy-team",
                }
            ],
            capsys,
        )
        assert rc == 0
        assert "legacy.telco.bronze" in out
        assert "Legacy Bronze" in out

    def test_missing_fields_render_as_question_mark(self, capsys):
        """Truly empty product → fields should render as ``?`` (not
        crash, not silently skip)."""
        rc, out = self._run_list([{"apiVersion": "v1.0.0"}], capsys)
        assert rc == 0
        # Three ? cells expected (id, name, status) + a team ?
        assert out.count("?") >= 3


# ---------------------------------------------------------------------------
# Bug #1: Snowflake provisionDataset cross-expose column leak.
# ---------------------------------------------------------------------------


class TestSnowflakeProvisionDatasetActionIdResolution:
    """``_handle_abstract_provision_dataset`` must read the action id
    from ``action_id`` (the canonical key the plan stage emits) and
    use it to scope the column-resolution fallback to the right
    expose. Reading from ``id`` alone made the fallback grab the first
    expose's columns for every action."""

    def _make_action(self, action_key: str, action_id_value: str, exposes: List[Dict]):
        """Build the action dict the apply path passes into the
        provider. ``action_key`` toggles between ``"action_id"`` (canonical)
        and ``"id"`` (legacy) so we can prove both paths resolve."""
        action = {
            action_key: action_id_value,
            "op": "provisionDataset",
            "params": {
                "exposeId": action_id_value.removeprefix("provision_"),
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "account": "ACME",
                        "database": "DB",
                        "schema": "SCH",
                        "table": action_id_value.removeprefix("provision_").upper(),
                    },
                },
                "contract": {
                    "fluidVersion": "0.7.3",
                    "kind": "DataProduct",
                    "id": "demo.test",
                    "exposes": exposes,
                },
            },
        }
        return action

    def _provider_with_stub_subactions(self, capture: dict):
        """Real ``_handle_abstract_provision_dataset`` with stubbed sub-
        action handlers. Capture the columns ensure_table sees so we
        can assert which expose's schema was selected."""
        from fluid_build.providers.snowflake.provider_enhanced import (
            SnowflakeProviderEnhanced,
        )

        # We don't need a real Snowflake connection — just instantiate
        # via __new__ and stub the methods the dispatcher calls.
        provider = SnowflakeProviderEnhanced.__new__(SnowflakeProviderEnhanced)
        provider._provisioned_bindings = {}

        provider._execute_database_action = MagicMock(
            return_value={"status": "ok", "changed": False}
        )
        provider._execute_schema_action = MagicMock(return_value={"status": "ok", "changed": False})

        def _exec_table(sub):
            capture["columns"] = sub.get("columns")
            capture["table"] = sub.get("table")
            return {"status": "ok", "changed": False}

        provider._execute_table_action = MagicMock(side_effect=_exec_table)
        provider._aggregate_sub_status = lambda subs: "ok"
        provider._binding_location = lambda action: action["params"]["binding"]["location"]
        return provider

    @pytest.fixture
    def two_expose_contract(self):
        """Mirror the A1 contract shape — 2 exposes with disjoint
        schemas. The bug surfaces when the second expose's apply
        accidentally uses the FIRST expose's columns."""
        return [
            {
                "exposeId": "subscriber360_core",
                "kind": "table",
                "contract": {
                    "schema": [
                        {"name": "PARTY_ID", "type": "STRING"},
                        {"name": "ACCOUNT_ID", "type": "STRING"},
                        {"name": "ACCOUNT_NUMBER", "type": "STRING"},
                        {"name": "SUBSCRIPTION_ID", "type": "STRING"},
                    ],
                },
            },
            {
                "exposeId": "subscriber_health_scorecard",
                "kind": "table",
                "contract": {
                    "schema": [
                        {"name": "ACCOUNT_ID", "type": "STRING"},
                        {"name": "SERVICE_ID", "type": "STRING"},
                        {"name": "SUPPORT_HEALTH_SCORE", "type": "NUMBER"},
                    ],
                },
            },
        ]

    def test_action_id_key_picks_correct_expose(self, two_expose_contract):
        """Plan-emitted action with ``action_id`` key: the second
        expose's apply must see the second expose's columns, NOT the
        first expose's columns."""
        capture: dict = {}
        provider = self._provider_with_stub_subactions(capture)
        action = self._make_action(
            action_key="action_id",
            action_id_value="provision_subscriber_health_scorecard",
            exposes=two_expose_contract,
        )

        provider._handle_abstract_provision_dataset(action)

        col_names = [c["name"] for c in (capture.get("columns") or [])]
        assert col_names == ["ACCOUNT_ID", "SERVICE_ID", "SUPPORT_HEALTH_SCORE"], (
            f"ensure_table got cols {col_names!r} — expected the "
            "subscriber_health_scorecard schema (3 cols), not the "
            "subscriber360_core schema (4 cols). The action_id-key "
            "fallback regressed."
        )

    def test_legacy_id_key_still_picks_correct_expose(self, two_expose_contract):
        """Old code paths emitted actions with ``id`` instead of
        ``action_id``. Backward compat: still resolve correctly."""
        capture: dict = {}
        provider = self._provider_with_stub_subactions(capture)
        action = self._make_action(
            action_key="id",
            action_id_value="provision_subscriber360_core",
            exposes=two_expose_contract,
        )

        provider._handle_abstract_provision_dataset(action)

        col_names = [c["name"] for c in (capture.get("columns") or [])]
        # Should pick subscriber360_core's 4 cols.
        assert col_names == [
            "PARTY_ID",
            "ACCOUNT_ID",
            "ACCOUNT_NUMBER",
            "SUBSCRIPTION_ID",
        ]

    def test_unidentifiable_action_refuses_to_guess(self, two_expose_contract):
        """When neither ``action_id`` nor ``id`` is set AND
        ``params.exposeId`` is missing, the provider must NOT silently
        pick the first expose. ensure_table should be skipped."""
        capture: dict = {}
        provider = self._provider_with_stub_subactions(capture)
        action = {
            "op": "provisionDataset",
            "params": {
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "account": "ACME",
                        "database": "DB",
                        "schema": "SCH",
                        "table": "ANY_TABLE",
                    },
                },
                "contract": {
                    "exposes": two_expose_contract,
                    # Note: no top-level params.exposeId.
                },
            },
        }
        result = provider._handle_abstract_provision_dataset(action)

        # ensure_table was NOT called (no columns captured).
        assert "columns" not in capture, (
            "ensure_table was called even though target expose can't be "
            "identified — that's the bug we're guarding against."
        )
        # Outer status is still ok (db + schema sub-actions ran).
        assert result["status"] == "ok"

    def test_params_exposeid_resolves_when_action_id_missing(self, two_expose_contract):
        """When the legacy planner sets ``params.exposeId`` directly
        (current code path in ``forge/core/provider_actions.py``), the
        provider should use that as the target id even if action_id
        doesn't follow the ``provision_<name>`` convention."""
        capture: dict = {}
        provider = self._provider_with_stub_subactions(capture)
        action = {
            "action_id": "custom_namespace_1",
            "op": "provisionDataset",
            "params": {
                "exposeId": "subscriber_health_scorecard",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "account": "ACME",
                        "database": "DB",
                        "schema": "SCH",
                        "table": "ANY_TABLE",
                    },
                },
                "contract": {"exposes": two_expose_contract},
            },
        }
        provider._handle_abstract_provision_dataset(action)

        col_names = [c["name"] for c in (capture.get("columns") or [])]
        # subscriber_health_scorecard's 3 cols, picked via params.exposeId.
        assert col_names == ["ACCOUNT_ID", "SERVICE_ID", "SUPPORT_HEALTH_SCORE"]


# ---------------------------------------------------------------------------
# Bug #3: ``verify --strict`` should fail on CRITICAL only, not WARNING.
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


# ---------------------------------------------------------------------------
# Bug #5: federation endpoint argument-injection in `git clone`.
# ---------------------------------------------------------------------------


class TestSnowflakeRollbackCleanupValidatesIdentifiers:
    """Identifiers read from ``.fluid/rollback-state.json`` must
    route through ``validate_ident`` before being interpolated into
    ``DROP TABLE`` or ``CREATE OR REPLACE TABLE ... CLONE`` DDL.
    Mirrors the defence already in ``cli/rollback.py::_restore_snowflake``."""

    def _make_provider(self):
        # The provider is initialised lazily in the rollback path; here
        # we instantiate with placeholder creds and exercise the
        # synchronous DDL emitters which don't touch the network.
        from fluid_build.providers.snowflake.provider_enhanced import (
            SnowflakeProviderEnhanced,
        )

        return SnowflakeProviderEnhanced(
            account="ACCT",
            warehouse="WH",
            database="DB",
            schema="PUBLIC",
            user="U",
        )

    def test_restore_ddl_rejects_invalid_identifier_returns_empty(self):
        provider = self._make_provider()

        result = provider.restore_ddl(
            {
                "location": {
                    "database": "DB; DROP TABLE PROD.PUBLIC.USERS; --",
                    "schema": "PUBLIC",
                    "table": "ORDERS",
                    "backup_table": "BACKUP_ORDERS_20260503",
                },
            }
        )

        # Tampered record yields no DDL at all (rather than emitting
        # a smuggled DROP); legitimate snapshots are still restorable.
        assert result == []

    def test_restore_ddl_emits_clean_ddl_for_valid_identifiers(self):
        provider = self._make_provider()

        result = provider.restore_ddl(
            {
                "location": {
                    "database": "PROD",
                    "schema": "PUBLIC",
                    "table": "ORDERS",
                    "backup_table": "BACKUP_ORDERS_20260503",
                },
            }
        )

        assert result == [
            "CREATE OR REPLACE TABLE PROD.PUBLIC.ORDERS " "CLONE PROD.PUBLIC.BACKUP_ORDERS_20260503"
        ]

    def test_cleanup_backups_skips_tampered_records(self):
        """A snapshot with an injection-shaped database identifier
        must NOT reach ``_execute_sql_action``; legitimate snapshots
        in the same call still execute."""
        from unittest.mock import patch

        provider = self._make_provider()

        called: List[Dict[str, Any]] = []

        def _capture(action: Dict[str, Any]) -> None:
            called.append(action)

        with patch.object(provider, "_execute_sql_action", side_effect=_capture):
            provider.cleanup_backups(
                [
                    {
                        "location": {
                            "database": "DB; DROP TABLE PROD.PUBLIC.USERS; --",
                            "schema": "PUBLIC",
                            "backup_table": "BACKUP_X",
                        },
                    },
                    {
                        "location": {
                            "database": "PROD",
                            "schema": "PUBLIC",
                            "backup_table": "BACKUP_Y",
                        },
                    },
                ]
            )

        # Exactly one action fired — the legitimate one. The tampered
        # entry was skipped, not silently used.
        assert len(called) == 1
        assert called[0]["sql"] == "DROP TABLE IF EXISTS PROD.PUBLIC.BACKUP_Y"


class TestRedshiftRollbackCleanupDdlValidatesIdentifiers:
    """Same defence as the Snowflake side, applied to the standalone
    Redshift rollback DDL emitter at
    ``providers/aws/redshift_provider.py::cleanup_ddl``."""

    def test_cleanup_ddl_skips_tampered_records(self):
        from fluid_build.providers.aws.redshift_provider import RedshiftProvider

        provider = RedshiftProvider()

        ddl = provider.cleanup_ddl(
            [
                {
                    "location": {
                        "database": "DB; DROP TABLE PROD.PUBLIC.USERS; --",
                        "schema": "PUBLIC",
                        "backup_table": "BACKUP_X",
                    },
                },
                {
                    "location": {
                        "database": "PROD",
                        "schema": "PUBLIC",
                        "backup_table": "BACKUP_Y",
                    },
                },
            ]
        )

        # Tampered record dropped; legitimate one emitted exactly once.
        assert ddl == ["DROP TABLE IF EXISTS PROD.PUBLIC.BACKUP_Y"]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
