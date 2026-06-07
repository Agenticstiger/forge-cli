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
            "  # Discovery: list all snapshots (newest first)\n"
            "  fluid rollback --list\n\n"
            "  # Discovery: narrow to one env + product\n"
            "  fluid rollback --list --env dev --product silver.telco.subscriber360_v1\n\n"
            "  # Dry-run: inspect which snapshot would be restored\n"
            "  fluid rollback --env dev --product silver.telco.subscriber360_v1 --dry-run\n\n"
            "  # Restore the most recent snapshot\n"
            "  fluid rollback --env dev --product silver.telco.subscriber360_v1 --yes\n\n"
            "  # Restore a specific named snapshot\n"
            "  fluid rollback --env dev --product silver.telco.subscriber360_v1 --yes \\\n"
            "      --snapshot backup_silver_telco_subscriber360_v1_1714000000\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--list",
        dest="list_snapshots",
        action="store_true",
        default=False,
        help=(
            "List available snapshots from ``.fluid/rollback-state.json`` "
            "without performing any restore. Optionally narrow with "
            "``--env`` and/or ``--product``. Mirrors ``terraform state "
            "list`` / ``git reflog`` — always check what's available "
            "before running a destructive restore. Implies no state "
            "mutation; safe to run in any env."
        ),
    )
    p.add_argument(
        "--env",
        # Not ``required=True`` any more — ``--list`` is a discovery
        # verb that works across all envs. For restore mode, we
        # enforce the requirement inside ``run()`` below so the
        # argparse error doesn't fire for ``rollback --list`` alone.
        help="Environment the product was applied to (dev | staging | prod).",
    )
    p.add_argument(
        "--product",
        # Same as ``--env`` — optional for ``--list``, required for
        # restore. Enforced in ``run()``.
        help="Product ID (matches the ``id`` field in the contract).",
    )
    p.add_argument(
        "--snapshot",
        default=None,
        help=(
            "Named snapshot to restore. Default: most recent snapshot matching --env + --product."
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
    # SECURITY: both ``database`` and ``backup_name`` are read from the
    # on-disk state file, which the rollback docstring explicitly invites
    # operators to commit to the product repo. That makes the state file
    # attacker-authorable via a PR — reviewed as a YAML-style data blob,
    # not byte-level audited. Without validation, a crafted
    # ``backup_name`` like
    #   "ok; DROP DATABASE production; CREATE DATABASE pwn CLONE ok"
    # would smuggle arbitrary DDL into the f-string + ``executescript``
    # (which splits on ';'). Route BOTH fields through the canonical
    # identifier validator (``^[A-Za-z_][A-Za-z0-9_]*$``) BEFORE the
    # f-string construction to reject every semicolon / whitespace /
    # quote / backtick / wildcard-based payload at the validation layer.
    #
    # Legitimate backup names (e.g.
    # ``backup_silver_telco_subscriber360_v1_1714000000``) + target db
    # names (e.g. ``TELCO_LAB``) match the regex cleanly, so the defence
    # has zero false-positive cost on valid inputs.
    from fluid_build.providers._sql_safety import validate_ident

    raw_backup_name = snapshot.get("backup_name")
    location = snapshot.get("location", {})
    raw_database = location.get("database") or snapshot.get("database")
    if not raw_database:
        raise CLIError(
            2,
            "rollback_snowflake_missing_database",
            {
                "snapshot": raw_backup_name,
                "hint": (
                    "snapshot record is missing location.database — the "
                    "snapshot writer (apply.py replace-path) should set "
                    "this. File a bug if you see this."
                ),
            },
        )
    if not raw_backup_name:
        raise CLIError(
            2,
            "rollback_snowflake_missing_backup_name",
            {
                "hint": ("snapshot record is missing backup_name — state file is malformed."),
            },
        )
    try:
        database = validate_ident(raw_database)
        backup_name = validate_ident(raw_backup_name)
    except ValueError as exc:
        # Raise as a CLIError with a diagnostic event slug so CI log
        # parsers and operators see a specific "rollback refused to
        # run because state-file values don't look like identifiers".
        raise CLIError(
            2,
            "rollback_snowflake_invalid_identifier",
            {
                "error": str(exc),
                "hint": (
                    "state-file values must match "
                    "^[A-Za-z_][A-Za-z0-9_]*$ (alphanumeric + "
                    "underscore). A value containing spaces, "
                    "semicolons, quotes, or shell metacharacters "
                    "indicates a corrupt or attacker-crafted state "
                    "file."
                ),
            },
        ) from exc

    # The rollback writer always records table-level CLONE DDL
    # in ``snapshot.ddl[]``. A snapshot without that field is
    # malformed (or written by a pre-T3 build that's no longer
    # supported); reject it loudly rather than silently emitting a
    # database-level CLONE that could overwrite unrelated tables.
    snapshot_ddl_list = snapshot.get("ddl") if isinstance(snapshot.get("ddl"), list) else []
    if not snapshot_ddl_list:
        raise CLIError(
            2,
            "rollback_snowflake_missing_ddl",
            {
                "backup_name": backup_name,
                "database": database,
                "hint": (
                    "snapshot is missing the ``ddl`` array. "
                    "Re-snapshot via ``fluid apply --mode replace`` and "
                    "retry, or restore manually with the Snowflake "
                    "Time Travel CLI."
                ),
            },
        )
    ddl_statements = [str(s).rstrip(";") + ";" for s in snapshot_ddl_list if s]
    ddl = "\n".join(ddl_statements)
    cprint(
        "[rollback] snowflake CLONE plan:\n    " + "\n    ".join(ddl_statements),
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
    last_result = None
    for stmt in ddl_statements:
        sql_action = {
            "op": "sf.sql.execute",
            "id": "rollback_restore",
            "sql": stmt.rstrip(";"),
            "account": provider.account,
            "database": database,
            "schema": location.get("schema") or "PUBLIC",
        }
        try:
            last_result = provider._execute_sql_action(sql_action)
        except Exception as exc:  # pragma: no cover — defensive
            raise CLIError(
                2,
                "rollback_snowflake_execute_failed",
                {"error": str(exc), "ddl": stmt},
            ) from exc
        if isinstance(last_result, dict) and last_result.get("status") == "error":
            raise CLIError(
                2,
                "rollback_snowflake_execute_failed",
                {"error": last_result.get("error", "unknown"), "ddl": stmt},
            )
    result = last_result or {"status": "ok"}

    return {
        "status": "restored",
        "provider": "snowflake",
        "ddl": ddl,
        "database": database,
        "backup_name": backup_name,
        "provider_result": result if isinstance(result, dict) else {"raw": str(result)},
    }


def _restore_bigquery(snapshot: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    """Restore a BigQuery product by replaying the snapshot's baked DDL.

    BigQuery has no zero-copy CLONE for arbitrary backups, so the
    rollback writer bakes a CTAS restore statement
    (``CREATE OR REPLACE TABLE <orig> AS SELECT * FROM <backup>``) into
    ``snapshot.ddl[]`` at apply time via
    ``providers/gcp/provider.py::GcpProvider.restore_ddl``. This
    function replays that DDL through the BigQuery client.
    ``CREATE OR REPLACE TABLE`` is atomic, so the live table is never
    left half-restored.

    Mirrors ``_restore_snowflake`` exactly: signature, dry-run early
    return, ``snapshot.ddl[]`` consumption, identifier validation,
    per-statement execution, and the ``{status, provider, ddl, ...}``
    return contract.

    SECURITY: ``location`` is read from the on-disk state file which the
    rollback docstring invites operators to commit (attacker-authorable
    via PR). Route ``project`` / ``dataset`` / ``table`` /
    ``backup_table`` through the same FQN validator the writer used
    (``_validated_bq_fqn``) BEFORE trusting the baked DDL — a tampered
    record is refused at the validation layer, not executed.
    """
    from fluid_build.providers.gcp.plan.planner import _validated_bq_fqn

    location = snapshot.get("location") or {}
    raw_project = location.get("database") or snapshot.get("database")
    raw_dataset = location.get("schema")
    raw_table = location.get("table")
    raw_backup = location.get("backup_table") or snapshot.get("backup_name")
    if not raw_project:
        raise CLIError(
            2,
            "rollback_bigquery_missing_project",
            {
                "snapshot": raw_backup,
                "hint": (
                    "snapshot record is missing location.database (the GCP "
                    "project) — the snapshot writer should set this. File "
                    "a bug if you see this."
                ),
            },
        )
    if not (raw_dataset and raw_table and raw_backup):
        raise CLIError(
            2,
            "rollback_bigquery_missing_location",
            {
                "snapshot": raw_backup,
                "hint": (
                    "snapshot record is missing location.schema / table / "
                    "backup_table — the snapshot writer should set these. "
                    "File a bug if you see this."
                ),
            },
        )
    # Re-validate the FQN components even though the writer already did,
    # because the state file is attacker-authorable between write and
    # restore. Refuse a tampered record before executing baked DDL.
    try:
        _validated_bq_fqn(raw_project, raw_dataset, raw_table)
        _validated_bq_fqn(raw_project, raw_dataset, raw_backup)
    except ValueError as exc:
        raise CLIError(
            2,
            "rollback_bigquery_invalid_identifier",
            {
                "error": str(exc),
                "hint": (
                    "state-file project/dataset/table values must be valid "
                    "BigQuery identifiers. A value containing spaces, "
                    "semicolons, quotes, or shell metacharacters indicates "
                    "a corrupt or attacker-crafted state file."
                ),
            },
        ) from exc

    snapshot_ddl_list = snapshot.get("ddl") if isinstance(snapshot.get("ddl"), list) else []
    if not snapshot_ddl_list:
        raise CLIError(
            2,
            "rollback_bigquery_missing_ddl",
            {
                "backup_name": raw_backup,
                "project": raw_project,
                "hint": (
                    "snapshot is missing the ``ddl`` array. Re-snapshot via "
                    "``fluid apply --mode replace`` and retry, or restore "
                    "manually with ``bq cp --restore --force "
                    "<backup_table> <live_table>``."
                ),
            },
        )
    ddl_statements = [str(s).rstrip(";") for s in snapshot_ddl_list if s]
    ddl = "\n".join(stmt + ";" for stmt in ddl_statements)
    cprint(
        "[rollback] bigquery CTAS restore plan:\n    "
        + "\n    ".join(stmt + ";" for stmt in ddl_statements),
        markup=False,
    )
    if dry_run:
        return {
            "status": "dry_run",
            "provider": "bigquery",
            "ddl": ddl,
            "project": raw_project,
            "backup_name": raw_backup,
        }
    # Execute via the BigQuery client. Imported lazily so non-BigQuery
    # rollbacks don't pay the google-cloud-bigquery import cost (mirrors
    # the lazy SnowflakeProvider import in _restore_snowflake and the
    # client usage in GcpProvider.cleanup_backups).
    try:
        from google.cloud import bigquery  # type: ignore
    except ImportError as exc:
        raise CLIError(
            2,
            "rollback_bigquery_provider_unavailable",
            {
                "error": str(exc),
                "hint": (
                    "google-cloud-bigquery is not installed. Install it "
                    "(``pip install google-cloud-bigquery``) or run the "
                    "restore manually with ``bq``."
                ),
            },
        ) from exc

    client = bigquery.Client(project=raw_project)
    last_result: Any = None
    for stmt in ddl_statements:
        try:
            job = client.query(stmt)
            last_result = job.result()  # blocks until the statement completes
        except Exception as exc:  # pragma: no cover - defensive
            raise CLIError(
                2,
                "rollback_bigquery_execute_failed",
                {"error": str(exc), "ddl": stmt},
            ) from exc

    return {
        "status": "restored",
        "provider": "bigquery",
        "ddl": ddl,
        "project": raw_project,
        "backup_name": raw_backup,
        "provider_result": {"status": "ok", "statements": len(ddl_statements)},
    }


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
    # BigQuery snapshots are written with provider="bigquery" by synthetic
    # callers, but a REAL ``fluid apply`` records provider=GcpProvider.name
    # == "gcp" (apply.py writes ``getattr(provider, "name", ...)``). Alias
    # "gcp" to the BigQuery restorer so the live apply->rollback round trip
    # actually reaches it instead of failing with rollback_unknown_provider.
    "gcp": _restore_bigquery,
    "bigquery": _restore_bigquery,
    # NOTE: Redshift is NOT aliased from "aws" on purpose. A real Redshift
    # apply plans through AwsProvider (provider name "aws") and the snapshot
    # writer bakes its DDL via AwsProvider.restore_ddl -> [] (S3 prefix-copy,
    # non-DDL). Routing "aws" here would only surface rollback_redshift_
    # missing_ddl because the transactional BEGIN/DROP/CTAS/COMMIT is never
    # baked into a real "aws" snapshot. Redshift rollback needs the writer/
    # planner to record provider="redshift" + the transactional ddl first
    # (follow-up); only then add the alias.
    "redshift": _restore_redshift,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _print_snapshot_list(
    snapshots: List[Dict[str, Any]],
    *,
    env_filter: Optional[str],
    product_filter: Optional[str],
) -> None:
    """Print a human-readable table of snapshots, newest first.

    Optionally narrows to ``env_filter`` / ``product_filter`` matches.
    Prints to stdout — not through ``logger`` — because this is a
    discovery command meant to be consumed by humans + piped to
    ``grep`` / ``head``; structured log output would hinder both.

    Columns: ``timestamp``, ``env``, ``product_id``, ``mode``,
    ``provider``, ``backup_name``. These are the same five fields
    needed to run ``fluid rollback ... --snapshot <name>`` so the
    list output doubles as a worksheet for the subsequent restore
    command.
    """
    filtered = [
        s
        for s in snapshots
        if (env_filter is None or s.get("env") == env_filter)
        and (product_filter is None or s.get("product_id") == product_filter)
    ]
    # Snapshots are appended in chronological order; newest last.
    # Reverse so the most recent (and most likely target for
    # restore) is printed first — matches ``git reflog`` behaviour.
    filtered = list(reversed(filtered))

    if not filtered:
        if env_filter or product_filter:
            cprint(
                f"[rollback] no snapshots found for env={env_filter!r} "
                f"product={product_filter!r}. Either no "
                f"``apply --mode replace*`` has run, or the filters "
                f"don't match the recorded state.",
                markup=False,
            )
        else:
            cprint(
                "[rollback] no snapshots recorded yet. The state "
                "file is created on the first ``apply --mode "
                "replace`` / ``replace-and-build`` invocation.",
                markup=False,
            )
        return

    # Plain ASCII table; Rich is optional + we don't want to require
    # it for a read-only discovery path that may run in hardened CI
    # environments where Rich isn't installed.
    header = f"{'#':>3}  {'timestamp':<26}  {'env':<8}  {'mode':<20}  {'product_id':<42}  {'backup_name'}"
    cprint(header, markup=False)
    cprint("-" * len(header), markup=False)
    for idx, snap in enumerate(filtered):
        # ``idx`` is shown as a 1-based row number because that's
        # what operators see in human output; ``--snapshot`` still
        # takes the backup_name string, not the index (indexes
        # shift as new snapshots are appended).
        cprint(
            f"{idx + 1:>3}  "
            f"{snap.get('timestamp', '—'):<26}  "
            f"{snap.get('env', '—'):<8}  "
            f"{snap.get('mode', '—'):<20}  "
            f"{snap.get('product_id', '—'):<42}  "
            f"{snap.get('backup_name', '—')}",
            markup=False,
        )
    cprint(
        f"\n[rollback] {len(filtered)} snapshot(s). "
        "Restore with: "
        "fluid rollback --env <ENV> --product <ID> "
        "--snapshot <BACKUP_NAME> --yes",
        markup=False,
    )


def run(args: argparse.Namespace, _logger: Optional[logging.Logger] = None) -> int:
    state_path = Path(args.state_file).resolve()

    # --list: discovery-only path. No env/product required; no
    # destructive action taken. Read state, filter, print. Returns
    # 0 even when the state file doesn't exist — "nothing to list"
    # is not a failure.
    if getattr(args, "list_snapshots", False):
        if not state_path.exists():
            cprint(
                f"[rollback] no state file at {state_path}. "
                "The file is created on the first ``apply --mode "
                "replace`` / ``replace-and-build`` invocation.",
                markup=False,
            )
            return 0
        data = _read_state(state_path)
        _print_snapshot_list(
            data.get("snapshots", []),
            env_filter=args.env,
            product_filter=args.product,
        )
        return 0

    # Restore path: --env and --product are required. Enforce here
    # rather than via ``required=True`` on the argparse call so that
    # ``rollback --list`` works without them. This mirrors how
    # kubectl / terraform enforce required args in subcommand
    # dispatcher bodies for flags that are mode-dependent.
    if not args.env:
        raise CLIError(
            2,
            "rollback_env_required",
            {
                "hint": (
                    "--env is required for restore. Use "
                    "``fluid rollback --list`` for read-only discovery."
                )
            },
        )
    if not args.product:
        raise CLIError(
            2,
            "rollback_product_required",
            {
                "hint": (
                    "--product is required for restore. Use "
                    "``fluid rollback --list`` for read-only discovery."
                )
            },
        )
    product = _validate_product_id(args.product)

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
