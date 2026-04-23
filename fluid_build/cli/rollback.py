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

"""``fluid rollback`` — restore a data product from a prior snapshot.

Closes principle 05 of the 11-stage pipeline design ("operators get
explicit control over destruction"). When ``fluid apply --mode
replace*`` creates an auto-snapshot before destructive DDL, that
snapshot is recorded in ``.fluid/rollback-state.json``. This command
reads the state file and restores the product from the most recent
snapshot (or a named one).

CLI surface::

    fluid rollback --env <env> --product <product-id>
    fluid rollback --env <env> --product <product-id> --snapshot <name>
    fluid rollback --env <env> --product <product-id> --dry-run

Per-provider restore semantics:
- **Snowflake** — ``CREATE OR REPLACE DATABASE <db> CLONE <backup_name>``
  (zero-copy, reverses the original CLONE used for backup).
- **BigQuery** — NotImplementedError with actionable message. Follow-up
  work: ``bq cp --force <backup_dataset> <dataset>`` per table.
- **Redshift** — NotImplementedError with actionable message. Follow-up
  work: ``TRUNCATE <table>; INSERT INTO <table> SELECT * FROM
  <backup_table>;`` inside a transaction.

The state-file format is stable across fluid versions so it can be
committed to the product repo as an audit trail:

.. code-block:: json

    {
      "version": "1",
      "snapshots": [
        {
          "timestamp": "2026-04-23T10:00:00Z",
          "env": "dev",
          "product_id": "silver.telco.subscriber360_v1",
          "backup_name": "backup_silver_telco_subscriber360_v1_1714000000",
          "provider": "snowflake",
          "mode": "replace",
          "original_digest": "sha256:..."
        }
      ]
    }

Dry-run prints the target backup + the DDL that would execute, but
runs no provider call. Use this before every production rollback to
confirm the right snapshot is about to be restored.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli._common import CLIError
from fluid_build.cli.console import cprint

COMMAND = "rollback"
logger = logging.getLogger(__name__)

_STATE_FILE = ".fluid/rollback-state.json"
_STATE_VERSION = "1"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="Restore a data product from a prior apply --mode replace snapshot",
        description=(
            "Reads ``.fluid/rollback-state.json`` in the current working "
            "directory and restores the named product from the most recent "
            "snapshot (or a specific one via --snapshot). Each snapshot was "
            "created automatically by an earlier ``fluid apply --mode "
            "replace*`` invocation; see the state-file format documented "
            "in fluid_build/cli/rollback.py's module docstring."
        ),
        epilog=(
            "Examples:\n"
            "  # Dry-run: inspect which snapshot would be restored\n"
            "  fluid rollback --env dev --product silver.telco.subscriber360_v1 --dry-run\n\n"
            "  # Restore the most recent snapshot\n"
            "  fluid rollback --env dev --product silver.telco.subscriber360_v1\n\n"
            "  # Restore a specific named snapshot\n"
            "  fluid rollback --env dev --product silver.telco.subscriber360_v1 \\\n"
            "      --snapshot backup_silver_telco_subscriber360_v1_1714000000\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--env",
        required=True,
        help="Environment the product was applied to (dev | staging | prod).",
    )
    p.add_argument(
        "--product",
        required=True,
        help="Product ID (matches the ``id`` field in the contract).",
    )
    p.add_argument(
        "--snapshot",
        default=None,
        help=(
            "Named snapshot to restore. Default: most recent snapshot "
            "matching --env + --product."
        ),
    )
    p.add_argument(
        "--state-file",
        default=_STATE_FILE,
        help=(
            "Path to the rollback state file. Default: "
            "``.fluid/rollback-state.json`` relative to CWD."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Print the target snapshot + restore DDL without executing. "
            "Use before every production rollback."
        ),
    )
    p.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help=(
            "Confirm the destructive restore. Required when not --dry-run "
            "(rollback overwrites the current state of the product)."
        ),
    )
    p.set_defaults(func=run)


# ---------------------------------------------------------------------------
# State-file I/O
# ---------------------------------------------------------------------------


def _validate_product_id(raw: str) -> str:
    """Reject obviously malformed product IDs.

    Product IDs are contract-level identifiers (``silver.telco.subscriber360_v1``).
    They flow into provider DDL downstream (backup-name construction) so
    we apply the same strict-identifier regex used by schedule-sync:
    alphanumeric + ``_.-``, no shell metacharacters, no path separators.
    Empty values and values with spaces are refused.
    """
    import re

    if not raw or not raw.strip():
        raise CLIError(2, "rollback_product_id_empty", {"value": raw})
    pattern = re.compile(r"^[A-Za-z0-9_.\-]{1,256}$")
    if not pattern.fullmatch(raw):
        raise CLIError(
            2,
            "rollback_product_id_invalid",
            {
                "value": raw,
                "hint": (
                    "product IDs must match ^[A-Za-z0-9_.-]{1,256}$ "
                    "(alphanumeric + underscore/dot/hyphen; no spaces or "
                    "shell metacharacters)"
                ),
            },
        )
    return raw


def _read_state(path: Path) -> Dict[str, Any]:
    """Read + validate the rollback state file.

    Raises CLIError on:
    - File missing (caller should check this separately with an
      actionable "did you ever run apply --mode replace?" hint)
    - Invalid JSON
    - Missing ``version`` field
    - Unsupported ``version`` (future-proof)
    - Missing ``snapshots`` list
    """
    if not path.exists():
        raise CLIError(
            2,
            "rollback_state_file_missing",
            {
                "path": str(path),
                "hint": (
                    "no rollback state found. Did you run ``fluid apply "
                    "--mode replace`` (or replace-and-build) previously? "
                    "Rollback only works for products with at least one "
                    "prior destructive apply."
                ),
            },
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CLIError(
            2,
            "rollback_state_file_invalid_json",
            {"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise CLIError(
            2,
            "rollback_state_file_wrong_shape",
            {
                "path": str(path),
                "hint": "state file must contain a JSON object at the top level",
            },
        )
    version = data.get("version")
    if version != _STATE_VERSION:
        raise CLIError(
            2,
            "rollback_state_file_unsupported_version",
            {
                "path": str(path),
                "version": version,
                "expected": _STATE_VERSION,
                "hint": (
                    f"this fluid supports state-file version {_STATE_VERSION} "
                    "only. Upgrade fluid or restore the state file from an "
                    "earlier commit."
                ),
            },
        )
    snaps = data.get("snapshots")
    if not isinstance(snaps, list):
        raise CLIError(
            2,
            "rollback_state_file_no_snapshots",
            {
                "path": str(path),
                "hint": "state file missing top-level ``snapshots`` list",
            },
        )
    return data


def _select_snapshot(
    snapshots: List[Dict[str, Any]],
    *,
    env: str,
    product: str,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Pick the right snapshot entry for the restore.

    Algorithm:
    1. Filter snapshots by (env, product_id).
    2. If ``name`` is supplied, match exactly; raise if not found.
    3. Otherwise, return the most recent (lexicographically highest
       timestamp — ISO-8601 sorts correctly).
    """
    matching = [s for s in snapshots if s.get("env") == env and s.get("product_id") == product]
    if not matching:
        raise CLIError(
            2,
            "rollback_no_matching_snapshot",
            {
                "env": env,
                "product_id": product,
                "hint": (
                    "no snapshot found for this env + product. Check "
                    "--env / --product values match what apply recorded."
                ),
            },
        )
    if name is not None:
        for s in matching:
            if s.get("backup_name") == name:
                return s
        raise CLIError(
            2,
            "rollback_snapshot_name_not_found",
            {
                "name": name,
                "env": env,
                "product_id": product,
                "available": [s.get("backup_name") for s in matching],
            },
        )
    # Sort by timestamp descending; ISO-8601 sorts correctly as strings.
    matching.sort(key=lambda s: s.get("timestamp", ""), reverse=True)
    return matching[0]


# ---------------------------------------------------------------------------
# Per-provider restore dispatchers
# ---------------------------------------------------------------------------


def _restore_snowflake(snapshot: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    """Restore a Snowflake product via zero-copy CLONE.

    DDL: ``CREATE OR REPLACE DATABASE <db> CLONE <backup_name>``. This
    reverses the backup pattern used by ``apply --mode replace`` — the
    original CLONE created the backup_name from the live db; this CLONE
    creates the live db from the backup. Zero-copy so it costs nothing
    beyond metadata bookkeeping.

    Assumes the backup database still exists. If it was manually
    dropped, cosign — I mean, Snowflake — will surface a "does not
    exist" error from the client which we surface to the operator.
    """
    backup_name = snapshot["backup_name"]
    location = snapshot.get("location", {})
    database = location.get("database") or snapshot.get("database")
    if not database:
        raise CLIError(
            2,
            "rollback_snowflake_missing_database",
            {
                "snapshot": backup_name,
                "hint": (
                    "snapshot record is missing location.database — the "
                    "snapshot writer (apply.py replace-path) should set "
                    "this. File a bug if you see this."
                ),
            },
        )

    ddl = f"CREATE OR REPLACE DATABASE {database} CLONE {backup_name};"
    cprint(
        f"[rollback] snowflake CLONE plan:\n    {ddl}",
        markup=False,
    )
    if dry_run:
        return {
            "status": "dry_run",
            "provider": "snowflake",
            "ddl": ddl,
            "database": database,
            "backup_name": backup_name,
        }
    # Execute via the Snowflake provider's connection pool. Imported
    # lazily so non-Snowflake rollbacks don't pay the snowflake
    # connector import cost.
    try:
        from fluid_build.providers.snowflake import SnowflakeProvider
    except ImportError as exc:
        raise CLIError(
            2,
            "rollback_snowflake_provider_unavailable",
            {"error": str(exc)},
        ) from exc

    provider = SnowflakeProvider(database=database)
    try:
        result = provider.execute_sql(ddl)
    except AttributeError:
        # SnowflakeProvider.execute_sql is the public helper; if absent
        # fall back to the internal _execute_sql method which the
        # enhanced provider exposes.
        try:
            sql_action = {"op": "sf.sql.execute", "params": {"sql": ddl}}
            result = provider._execute_sql_action(sql_action)
        except Exception as exc:  # pragma: no cover — defensive
            raise CLIError(
                2,
                "rollback_snowflake_execute_failed",
                {"error": str(exc), "ddl": ddl},
            ) from exc

    return {
        "status": "restored",
        "provider": "snowflake",
        "ddl": ddl,
        "database": database,
        "backup_name": backup_name,
        "provider_result": result if isinstance(result, dict) else {"raw": str(result)},
    }


def _restore_bigquery(snapshot: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    """BigQuery rollback — not yet implemented.

    Follow-up work sketch (for whoever picks this up):
    - Read backup-dataset name from ``snapshot["backup_name"]``.
    - For each table in the backup dataset, run ``bq cp --force
      <backup>.<table> <live>.<table>``.
    - Wrap in a script that short-circuits on first failure; partial
      restores are worse than no restore.
    """
    raise CLIError(
        2,
        "rollback_bigquery_not_implemented",
        {
            "snapshot": snapshot.get("backup_name"),
            "hint": (
                "BigQuery rollback is not yet wired. Workaround: "
                "``bq cp --force <backup_dataset>.<table> "
                "<live_dataset>.<table>`` per table. Track under the "
                "follow-up issue for this feature."
            ),
        },
    )


def _restore_redshift(snapshot: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    """Redshift rollback — not yet implemented.

    Follow-up work sketch:
    - Enumerate tables in the schema.
    - For each: ``TRUNCATE <live>; INSERT INTO <live> SELECT * FROM
      <backup>;`` inside a single transaction so a failure rolls back.
    - Alternative: ``ALTER TABLE <live> RENAME TO <old>; ALTER TABLE
      <backup> RENAME TO <live>;`` — atomic, but loses the backup.
    """
    raise CLIError(
        2,
        "rollback_redshift_not_implemented",
        {
            "snapshot": snapshot.get("backup_name"),
            "hint": (
                "Redshift rollback is not yet wired. Workaround: manual "
                "``TRUNCATE`` + ``INSERT INTO <live> SELECT * FROM "
                "<backup>`` per table inside a transaction."
            ),
        },
    )


_RESTORE_DISPATCH = {
    "snowflake": _restore_snowflake,
    "bigquery": _restore_bigquery,
    "redshift": _restore_redshift,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    product = _validate_product_id(args.product)
    state_path = Path(args.state_file).resolve()

    data = _read_state(state_path)
    snapshot = _select_snapshot(
        data["snapshots"],
        env=args.env,
        product=product,
        name=args.snapshot,
    )
    provider = snapshot.get("provider", "snowflake")
    if provider not in _RESTORE_DISPATCH:
        raise CLIError(
            2,
            "rollback_unknown_provider",
            {
                "provider": provider,
                "supported": sorted(_RESTORE_DISPATCH.keys()),
            },
        )

    cprint(
        f"[rollback] env={args.env} product={product} "
        f"provider={provider} snapshot={snapshot.get('backup_name')} "
        f"timestamp={snapshot.get('timestamp')} dry-run={args.dry_run}",
        markup=False,
    )

    if not args.dry_run and not args.yes:
        raise CLIError(
            2,
            "rollback_confirmation_required",
            {
                "hint": (
                    "rollback is destructive (overwrites the current product "
                    "state). Re-run with --yes to confirm, or --dry-run to "
                    "preview the plan."
                )
            },
        )

    result = _RESTORE_DISPATCH[provider](snapshot, dry_run=args.dry_run)
    if args.dry_run:
        cprint(
            "[rollback] \u2714 dry-run complete; no state mutated.",
            markup=False,
        )
    else:
        cprint(
            f"[rollback] \u2714 restore complete: "
            f"{snapshot.get('product_id')} \u2190 {snapshot.get('backup_name')}",
            markup=False,
        )
    # Structured log for CI parsers.
    logger.info("rollback_complete", extra={"result": result})
    return 0
