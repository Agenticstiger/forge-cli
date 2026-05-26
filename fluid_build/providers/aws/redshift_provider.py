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

"""Standalone Redshift provider for rollback / DDL emission.

The full AWS provider (``fluid_build.providers.aws.AwsProvider``) handles
Redshift planning. Redshift's rollback semantics are
SQL-DDL-based (``DROP`` + ``CREATE TABLE AS SELECT`` inside a transaction)
while the rest of AWS (S3 / Glue / Athena) uses prefix-copy semantics.

Splitting Redshift out as its own provider class keeps the rollback
surface modular: ``cli/_rollback_writer.py::_delegate_to_provider``
asks each provider for its restore DDL by name, and Redshift owns its
SQL while ``AwsProvider`` returns ``[]`` (S3 prefix-copy is non-DDL).

Why a thin standalone class instead of folding into ``AwsProvider``?
``AwsProvider.restore_ddl`` already handles S3 / Glue / Athena snapshots
correctly with ``[]`` (the rollback CLI's S3 path runs the prefix copy
directly). Mixing Redshift's transactional DDL into that method would
require a snapshot-source switch inside the AWS provider, which couples
two unrelated rollback semantics. One class per restore-semantics is
cleaner than one class with two switches.

Registered as ``"redshift"`` in the provider registry so
``_delegate_to_provider("redshift", "restore_ddl", snapshot)`` succeeds
without falling through to the legacy lookup.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional

from ..base import ApplyResult, BaseProvider


class RedshiftProvider(BaseProvider):
    """Thin Redshift provider — owns rollback semantics only.

    ``plan`` delegates to the full ``AwsProvider``; this class exists
    primarily so ``restore_ddl`` and ``cleanup_backups`` participate in
    the modular rollback dispatch instead of living in a
    ``_legacy_restore_ddl`` if/elif chain.
    """

    name = "redshift"

    @classmethod
    def get_provider_info(cls):  # type: ignore[override]
        from ..base import ProviderMetadata

        return ProviderMetadata(
            name="redshift",
            display_name="Amazon Redshift",
            description="Redshift-only provider — rollback DDL surface for the AWS provider.",
            version="0.7.3",
            author="Agentics Transformation Ltd",
            supported_platforms=["redshift"],
            tags=["aws", "redshift", "rollback"],
        )

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        region: Optional[str] = None,
        logger=None,
        **kwargs: Any,
    ) -> None:
        super().__init__(project=project, region=region, logger=logger, **kwargs)

    def plan(
        self,
        contract: Mapping[str, Any],
        *,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Delegate Redshift planning to the full AWS provider.

        Redshift contracts plan through ``AwsProvider``; rebuilding the
        full plan dispatcher here would duplicate ~600 LOC for no gain.
        """
        from .provider import AwsProvider

        delegate = AwsProvider(
            project=self.project,
            region=self.region,
            logger=self.logger,
        )
        return delegate.plan(contract, mode=mode)

    def apply(self, actions: Iterable[Mapping[str, Any]]) -> ApplyResult:
        """Delegate Redshift apply to the full AWS provider."""
        from .provider import AwsProvider

        delegate = AwsProvider(
            project=self.project,
            region=self.region,
            logger=self.logger,
        )
        return delegate.apply(list(actions))

    # ── Rollback surface (the reason this class exists) ───────────────

    def restore_ddl(self, snapshot: Mapping[str, Any]) -> List[str]:
        """Emit the Redshift DDL to restore ``snapshot``'s backup table.

        Redshift uses transactional ``CREATE TABLE AS SELECT`` from the
        backup. ``BEGIN`` / ``COMMIT`` brackets the swap so a failure
        leaves the live table untouched.

        Returns ``[]`` when the snapshot is missing a location field —
        the rollback CLI surfaces that as a typed
        ``rollback_invalid_snapshot`` error.
        """
        location = snapshot.get("location") or {}
        db = location.get("database")
        sch = location.get("schema")
        tbl = location.get("table")
        backup = location.get("backup_table") or snapshot.get("backup_name")
        if not (db and sch and tbl and backup):
            return []
        return [
            "BEGIN",
            f"DROP TABLE IF EXISTS {db}.{sch}.{tbl}",
            f"CREATE TABLE {db}.{sch}.{tbl} AS SELECT * FROM {db}.{sch}.{backup}",
            "COMMIT",
        ]

    def cleanup_backups(self, snapshots: List[Mapping[str, Any]]) -> None:
        """Drop aged-out Redshift backup tables.

        The DDL is emitted as a list — actual execution happens through
        the AWS provider's connection pool when the rollback CLI runs
        the cleanup. We don't open a connection here because this method
        is also called from the rollback writer's retention pass at
        plan-time, where credentials may not be plumbed.

        For pure-DDL emission (the common path), the connection-less
        execution returns the DROP statements as a list via
        ``cleanup_ddl(snapshots)`` — the rollback CLI calls that when
        ``cleanup_backups`` is unavailable.
        """
        # Connection-less mode: return None — the rollback CLI's
        # cleanup pass uses ``cleanup_ddl`` (below) when no connection
        # is established. Providers with native SDK clients (BigQuery,
        # S3) override this method to delete directly.
        return None

    def cleanup_ddl(self, snapshots: List[Mapping[str, Any]]) -> List[str]:
        """Emit ``DROP TABLE`` statements for the rollback CLI to run.

        Used when the rollback CLI has a Redshift connection but the
        retention pass doesn't (typical: writer schedules cleanup,
        rollback CLI runs it later with credentials).

        Security: identifiers come from ``.fluid/rollback-state.json``
        which is documented as PR-reviewable / attacker-authorable —
        every component is routed through ``validate_ident`` before
        being interpolated into the DDL string. Mirrors the defence
        applied at ``providers/snowflake/provider_enhanced.py::
        cleanup_backups`` and ``cli/rollback.py::_restore_snowflake``.
        Records with invalid identifiers are skipped silently so a
        single tampered entry doesn't poison the whole cleanup pass.
        """
        from fluid_build.providers._sql_safety import validate_ident

        ddl: List[str] = []
        for snap in snapshots or []:
            loc = snap.get("location") or {}
            db = loc.get("database")
            sch = loc.get("schema")
            backup = loc.get("backup_table") or snap.get("backup_name")
            if not (db and sch and backup):
                continue
            try:
                db_v = validate_ident(str(db))
                sch_v = validate_ident(str(sch))
                backup_v = validate_ident(str(backup))
            except ValueError:
                # Skip tampered records; the legitimate ones still get
                # their DROP statements emitted.
                continue
            ddl.append(f"DROP TABLE IF EXISTS {db_v}.{sch_v}.{backup_v}")
        return ddl


__all__ = ["RedshiftProvider"]
