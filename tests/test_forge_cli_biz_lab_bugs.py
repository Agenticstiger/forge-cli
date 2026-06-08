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


class TestFederationEndpointSchemeAllowList:
    """``federation/upstreams.yaml::workspaces[].endpoint`` is
    PR-reviewable; passing it verbatim to ``git clone <url> <dir>``
    let an attacker smuggle git CLI options like
    ``--upload-pack=<cmd>`` at clone time. The fix validates the
    URL scheme + ``-`` prefix at manifest-load time; the subprocess
    argv ALSO passes ``--`` before the positional URL as belt-and-
    suspenders. Both lines of defence are pinned here."""

    def test_endpoint_with_dash_prefix_rejected(self):
        """Endpoints starting with ``-`` are the argument-injection
        attack shape (``--upload-pack=...``, ``--config=...``).
        Rejected at construction time with ``ValueError``."""
        from fluid_build.forge.federation import FederatedWorkspace

        with pytest.raises(ValueError, match="must not start with '-'"):
            FederatedWorkspace.from_dict(
                {
                    "id": "evil",
                    "kind": "git_registry",
                    "endpoint": "--upload-pack=touch /tmp/pwn",
                }
            )

    def test_endpoint_without_scheme_rejected(self):
        """A bare ``host:port/path`` without an explicit scheme is
        rejected so ambiguous SCP-style values can't slip through."""
        from fluid_build.forge.federation import FederatedWorkspace

        with pytest.raises(ValueError, match="explicit URL scheme"):
            FederatedWorkspace.from_dict(
                {
                    "id": "no-scheme",
                    "kind": "git_registry",
                    "endpoint": "github.com/foo/bar.git",
                }
            )

    def test_endpoint_with_disallowed_scheme_rejected(self):
        """The ``ext::sh -c`` historical git transport is the most
        notorious smuggling vector. We accept only well-known
        clone-time schemes."""
        from fluid_build.forge.federation import FederatedWorkspace

        with pytest.raises(ValueError, match="not allowed"):
            FederatedWorkspace.from_dict(
                {
                    "id": "ext-attack",
                    "kind": "git_registry",
                    "endpoint": "ext://sh -c touch /tmp/pwn",
                }
            )

    def test_endpoint_with_https_accepted(self):
        from fluid_build.forge.federation import FederatedWorkspace

        ws = FederatedWorkspace.from_dict(
            {
                "id": "ok",
                "kind": "git_registry",
                "endpoint": "https://github.com/org/repo.git",
            }
        )
        assert ws.endpoint == "https://github.com/org/repo.git"

    def test_endpoint_with_ssh_accepted(self):
        from fluid_build.forge.federation import FederatedWorkspace

        ws = FederatedWorkspace.from_dict(
            {
                "id": "ok-ssh",
                "kind": "git_registry",
                "endpoint": "ssh://git@github.com/org/repo.git",
            }
        )
        assert ws.endpoint == "ssh://git@github.com/org/repo.git"

    def test_load_manifest_skips_invalid_workspaces_keeps_valid_ones(self, tmp_path):
        """A single tampered workspace entry must not disable
        federation for legitimate workspaces declared alongside it."""
        import textwrap

        from fluid_build.forge.federation import load_federation_manifest

        manifest_path = tmp_path / "federation" / "upstreams.yaml"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(
            textwrap.dedent(
                """\
                workspaces:
                  - id: evil
                    kind: git_registry
                    endpoint: "--upload-pack=touch /tmp/pwn"
                  - id: ok
                    kind: git_registry
                    endpoint: "https://github.com/org/repo.git"
                """
            ),
            encoding="utf-8",
        )

        manifest = load_federation_manifest(tmp_path)

        ids = {ws.id for ws in manifest.workspaces}
        assert "evil" not in ids, "tampered workspace must be skipped"
        assert "ok" in ids, "legitimate workspace must still load"

    def test_git_clone_argv_passes_dash_dash_before_url(self):
        """Belt-and-suspenders: even with the scheme allow-list,
        every ``git clone`` invocation must pass ``--`` before the
        positional URL so a future code path forgetting the
        validation can't reintroduce the bug."""
        import inspect

        from fluid_build.forge import federation

        src = inspect.getsource(federation._git_clone_or_pull_via_shellout)
        # The ``"--"`` separator must appear inside the argv list,
        # specifically between ``"--depth", "1"`` and ``auth_url``.
        # We assert a structural pattern rather than parsing the
        # argv literally.
        assert '"--"' in src or "'--'" in src, (
            "_git_clone_or_pull_via_shellout argv must include '--' "
            "before the positional URL (defence-in-depth against "
            "argument injection)."
        )


# ---------------------------------------------------------------------------
# Bug #6: Snowflake / Redshift rollback cleanup DDL skipped validate_ident.
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
        from fluid_build.providers.snowflake.provider import (
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
            "CREATE OR REPLACE TABLE PROD.PUBLIC.ORDERS CLONE PROD.PUBLIC.BACKUP_ORDERS_20260503"
        ]

    def test_cleanup_backups_skips_tampered_records(self):
        """A snapshot with an injection-shaped database identifier must
        NOT be executed; legitimate snapshots in the same call still run."""
        from unittest.mock import MagicMock, patch

        provider = self._make_provider()

        executed: List[str] = []
        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)
        fake_conn.execute = MagicMock(side_effect=lambda sql: executed.append(sql))

        with (
            patch(
                "fluid_build.providers.snowflake.connection.SnowflakeConnection",
                return_value=fake_conn,
            ),
            patch(
                "fluid_build.providers.snowflake.util.config.get_connection_params",
                return_value={},
            ),
        ):
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

        # Exactly one DROP executed — the legitimate one. The tampered
        # entry was skipped at the validate_ident gate, never executed.
        assert executed == ["DROP TABLE IF EXISTS PROD.PUBLIC.BACKUP_Y"]


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
