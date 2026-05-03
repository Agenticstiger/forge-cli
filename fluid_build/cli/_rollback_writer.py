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

"""Rollback-state file writer for destructive applies.

Reads / creates / appends to ``.fluid/rollback-state.json`` so the
``fluid rollback`` command can find a snapshot to restore. Called by
``cli/apply.py`` after a successful destructive apply (mode=replace
or replace-and-build) when the provider's plan contained one or more
``rollback_snapshot`` markers (emitted by the per-provider planner —
see ``providers/snowflake/plan/planner.py::_plan_replace_snapshots``).

Schema (matches what ``cli/rollback.py`` reads):

.. code-block:: json

    {
      "version": "1",
      "snapshots": [
        {
          "backup_name": "BACKUP_<table>_<ts>",
          "product_id": "telco.subscriber360_v1",
          "env": "dev",
          "captured_at": "2026-05-02T10:30:00Z",
          "provider": "snowflake",
          "location": {
            "database": "TELCO_LAB",
            "schema": "BRONZE",
            "table": "SUBSCRIBER360",
            "backup_table": "BACKUP_SUBSCRIBER360_1777672000"
          },
          "ddl": [
            "CREATE OR REPLACE TABLE TELCO_LAB.BRONZE.SUBSCRIBER360 CLONE TELCO_LAB.BRONZE.BACKUP_SUBSCRIBER360_1777672000"
          ]
        }
      ]
    }

The writer is append-only — new snapshots accumulate at the end of
the array. ``cli/rollback.py`` looks up by ``backup_name`` (or by
``env`` + ``product_id`` for the most-recent match).
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

LOG = logging.getLogger("fluid.cli.rollback_writer")

_STATE_FILE_VERSION = "1"
# Path resolved via fluid_build.paths so the workspace layout is one
# source of truth. Kept as a string constant for back-compat callers
# that pass ``state_file=str(...)`` — the helper handles both shapes.
_STATE_FILE_NAME = ".fluid/rollback-state.json"


def _default_state_file_path(workspace_root: Optional[Path]) -> Path:
    """Resolve ``rollback-state.json`` via the centralised paths module."""
    from fluid_build import paths as _paths

    return _paths.rollback_state_file(root=workspace_root)


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _restore_ddl_for_snapshot(snapshot_meta: Mapping[str, Any], provider: str) -> List[str]:
    """Build the DDL ``cli/rollback.py`` will run to restore the snapshot.

    Delegates to ``provider.restore_ddl(snapshot)`` — every in-tree
    provider owns its restore semantics (Snowflake CLONE, BigQuery
    CTAS, AWS S3-prefix copy, Redshift transactional CTAS). Adding a
    new provider is a single subclass override, not a central
    if/elif edit here.

    Returns ``[]`` when no DDL applies — the rollback CLI surfaces
    that as a typed ``rollback_provider_unsupported`` error.
    """
    prov = (provider or "").lower()
    delegated = _delegate_to_provider(prov, "restore_ddl", snapshot_meta)
    if delegated is _DISPATCH_MISS:
        # Provider not registered / failed to load. The rollback CLI
        # surfaces this as ``rollback_provider_unsupported``.
        LOG.warning(
            "rollback_provider_unsupported",
            extra={"provider": prov},
        )
        return []
    return delegated or []


_DISPATCH_MISS = object()


def _delegate_to_provider(provider_name: str, method: str, *args) -> Any:
    """Best-effort lookup of a provider class + call ``method`` on it.

    Returns the method's result, or the ``_DISPATCH_MISS`` sentinel
    when the provider can't be loaded / instantiated / the method
    isn't implemented. The sentinel lets callers distinguish "method
    returned None as success" from "couldn't dispatch at all".

    Lazy import: providers carry heavy SDK dependencies; we don't
    want to load them just to ask "which DDL would you emit?" when
    we already know the answer (legacy fallback). Providers that
    can be instantiated with no args participate; the rest fall
    through to the caller's fallback path.
    """
    try:
        if provider_name == "snowflake":
            from fluid_build.providers.snowflake import SnowflakeProvider

            cls = SnowflakeProvider
            init_kwargs: Dict[str, Any] = {}
        elif provider_name in ("gcp", "bigquery"):
            from fluid_build.providers.gcp import GcpProvider

            cls = GcpProvider
            # GcpProvider.__init__ requires a project; pass a dummy
            # since restore_ddl / cleanup_backups don't actually use
            # the provider's project (they read from snapshot.location).
            init_kwargs = {"project": "_rollback_dispatch_dummy_"}
        elif provider_name == "aws":
            from fluid_build.providers.aws import AwsProvider as cls

            init_kwargs = {}
        elif provider_name == "redshift":
            # Redshift owns its own restore semantics (DROP + CTAS in a
            # transaction) which differ from the rest of AWS (S3
            # prefix-copy). Routed to a thin standalone provider class
            # so the rollback dispatch stays modular.
            from fluid_build.providers.aws.redshift_provider import (
                RedshiftProvider,
            )

            cls = RedshiftProvider
            init_kwargs = {}
        else:
            return _DISPATCH_MISS
    except Exception:
        return _DISPATCH_MISS

    try:
        instance = cls(**init_kwargs)
        fn = getattr(instance, method, None)
        if fn is None:
            return _DISPATCH_MISS
        return fn(*args)
    except Exception:
        return _DISPATCH_MISS


def collect_snapshots_from_actions(
    actions: Iterable[Mapping[str, Any]],
    *,
    product_id: str,
    env: Optional[str],
    provider: str,
    results: Optional[Iterable[Mapping[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Walk a plan's action list and return any ``rollback_snapshot``
    markers shaped into the rollback-state schema.

    Per-action markers (emitted by
    ``providers/snowflake/plan/planner.py::_plan_replace_snapshots``)
    carry: ``backup_name``, ``product_id``, ``expose_id``, ``location``.
    This function adds ``env``, ``provider``, ``captured_at``, and the
    restore DDL.

    When ``results`` is provided, only snapshots whose corresponding
    action SUCCEEDED are recorded — actions that soft-failed (e.g.
    first-run replace where the source table didn't exist yet) are
    skipped because there's no actual backup table to restore from.
    """
    out: List[Dict[str, Any]] = []
    captured_at = _utc_now_iso()

    # Build action_id → result-status map so we can skip soft-failed
    # snapshot actions.
    by_id: Dict[str, str] = {}
    if results:
        for r in results:
            if not isinstance(r, Mapping):
                continue
            aid = r.get("action_id")
            if aid:
                by_id[aid] = str(r.get("status", ""))

    for action in actions or []:
        marker = action.get("rollback_snapshot") if isinstance(action, Mapping) else None
        if not marker:
            continue
        action_id = action.get("id")
        if results is not None:
            status = by_id.get(action_id, "")
            # ``ok`` / ``changed`` are success; everything else (skipped
            # / error / soft-failed) means no backup landed on disk.
            if status not in ("ok", "changed"):
                continue
        snapshot = {
            "backup_name": marker.get("backup_name"),
            "product_id": marker.get("product_id") or product_id,
            "env": env or "dev",
            "captured_at": captured_at,
            "provider": provider,
            "location": dict(marker.get("location") or {}),
            "ddl": _restore_ddl_for_snapshot(marker, provider),
        }
        out.append(snapshot)
    return out


def _resolve_keep_last_n() -> int:
    """Read the per-product snapshot retention from env / unified config.

    ``$FLUID_ROLLBACK_KEEP_LAST_N`` (operator override per invocation) wins
    over any config-file value. Default is 20 — enough history to roll
    back through a few iterations, bounded enough that the file doesn't
    grow unbounded across a long-lived workspace.
    """
    import os

    raw = os.environ.get("FLUID_ROLLBACK_KEEP_LAST_N")
    if raw:
        try:
            n = int(raw)
            return n if n > 0 else 20
        except (TypeError, ValueError):
            pass
    return 20


def _prune_snapshots(snapshots: List[Dict[str, Any]], *, keep_last_n: int) -> tuple:
    """Keep the last N snapshots per (env, product_id) pair.

    Order-preserving: walks the list once and drops older snapshots
    once a per-key bucket exceeds the cap. ``captured_at`` is the
    natural ordering key but we trust insertion order — newer
    snapshots appended at the end stay; older ones at the head get
    pruned first.

    Returns ``(kept_snapshots, dropped_snapshots)`` so the caller can
    issue cleanup DDL (e.g. Snowflake DROP TABLE) for the backups
    being aged out.
    """
    if keep_last_n <= 0:
        return list(snapshots), []
    # Walk in reverse so the most-recent N (per key) survive.
    seen_per_key: Dict[tuple, int] = {}
    keep_indices: set = set()
    for idx in range(len(snapshots) - 1, -1, -1):
        snap = snapshots[idx]
        if not isinstance(snap, dict):
            keep_indices.add(idx)
            continue
        key = (snap.get("env"), snap.get("product_id"))
        seen_per_key.setdefault(key, 0)
        if seen_per_key[key] < keep_last_n:
            keep_indices.add(idx)
            seen_per_key[key] += 1
    kept = [s for i, s in enumerate(snapshots) if i in keep_indices]
    dropped = [s for i, s in enumerate(snapshots) if i not in keep_indices]
    return kept, dropped


def append_snapshots_to_state_file(
    snapshots: List[Dict[str, Any]],
    *,
    workspace_root: Optional[Path] = None,
    state_file: Optional[Path] = None,
    keep_last_n: Optional[int] = None,
) -> Path:
    """Append snapshots to ``.fluid/rollback-state.json``.

    Creates the file (and parent dir) if missing. After append, prunes
    older snapshots so each (env, product_id) bucket holds at most
    ``keep_last_n`` entries. Default cap is 20; override via
    ``$FLUID_ROLLBACK_KEEP_LAST_N`` or by passing the kwarg explicitly.
    Pass ``keep_last_n=0`` to disable pruning (the file grows unbounded).

    Returns the resolved state-file path.
    """
    if not snapshots:
        return Path(state_file) if state_file else _default_state_file_path(workspace_root)
    target = Path(state_file) if state_file else _default_state_file_path(workspace_root)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — recover from corruption
            LOG.warning("rollback-state.json was malformed; rewriting from scratch.")
            data = {"version": _STATE_FILE_VERSION, "snapshots": []}
    else:
        data = {"version": _STATE_FILE_VERSION, "snapshots": []}

    if data.get("version") != _STATE_FILE_VERSION:
        # Forward-compat: keep the existing file but log so the user
        # knows this writer wrote a v1 entry into a non-v1 file.
        LOG.warning(
            "rollback-state.json version=%r (expected %r); appending anyway.",
            data.get("version"),
            _STATE_FILE_VERSION,
        )
    if not isinstance(data.get("snapshots"), list):
        data["snapshots"] = []

    data["snapshots"].extend(snapshots)
    cap = keep_last_n if keep_last_n is not None else _resolve_keep_last_n()
    kept, dropped = _prune_snapshots(data["snapshots"], keep_last_n=cap)
    if dropped:
        LOG.info(
            "rollback_state_pruned: kept %d of %d snapshots (cap=%d per env+product); "
            "%d backup table(s) eligible for cleanup",
            len(kept),
            len(kept) + len(dropped),
            cap,
            len(dropped),
        )
        # Best-effort cleanup of the dropped backup tables on the live
        # warehouse. Failures here are logged but don't abort the apply
        # — the worst case is orphaned backup tables that can be
        # cleaned up manually later.
        try:
            _cleanup_dropped_backups(dropped)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("backup_cleanup_failed: %s", exc, exc_info=True)
    data["snapshots"] = kept
    target.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return target


def _cleanup_dropped_backups(dropped: List[Dict[str, Any]]) -> None:
    """Delete backup tables / files for snapshots that aged out.

    Delegates to ``provider.cleanup_backups(snapshots)`` — each
    in-tree provider implements its own cleanup recipe (Snowflake
    DROP TABLE, BigQuery delete_table, AWS S3 prefix delete).
    Failures are logged and swallowed; orphaned backups can be
    cleaned up manually later.
    """
    if not dropped:
        return
    by_provider: Dict[str, List[Dict[str, Any]]] = {}
    for snap in dropped:
        prov = (snap.get("provider") or "").lower()
        if not prov:
            continue
        by_provider.setdefault(prov, []).append(snap)

    for prov, snaps in by_provider.items():
        # Lazy provider load + delegate. Providers without
        # ``cleanup_backups`` (or that fail to load) are silently
        # skipped — better an orphaned backup than an aborted apply.
        result = _delegate_to_provider(prov, "cleanup_backups", snaps)
        if result is _DISPATCH_MISS:
            LOG.debug(
                "backup_cleanup_skipped provider=%s snapshots=%d",
                prov,
                len(snaps),
            )


def write_snapshots_for_apply(
    actions: Iterable[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any],
    env: Optional[str],
    provider: str,
    workspace_root: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
    results: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Optional[Path]:
    """End-to-end: collect snapshots from a plan, append to state file.

    Returns the state-file path on success, ``None`` when no snapshots
    were emitted (e.g. additive apply mode, or all snapshot actions
    soft-failed because the source tables didn't exist yet). Failures
    log a warning but never raise — a snapshot-writer hiccup must
    not abort the apply.

    Pass ``results`` (the per-action result list returned by
    ``provider.apply``) so soft-failed snapshot actions are skipped —
    no backup table on disk means no restorable snapshot.
    """
    log = logger or LOG
    try:
        product_id = str(contract.get("id") or "unknown")
        snapshots = collect_snapshots_from_actions(
            actions,
            product_id=product_id,
            env=env,
            provider=provider,
            results=results,
        )
        if not snapshots:
            return None
        path = append_snapshots_to_state_file(snapshots, workspace_root=workspace_root)
        log.info(
            "rollback_state_written: path=%s snapshots=%d",
            path,
            len(snapshots),
        )
        return path
    except Exception as exc:  # noqa: BLE001
        log.warning("rollback_state_write_failed: %s", exc, exc_info=True)
        return None
