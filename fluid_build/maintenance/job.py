# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A backend-spark table's maintenance job config: what forge's Spark runner runs.

It is the writer build's own job config cut down to the table (``sink`` without its ``create``
statements: maintenance never creates a table) plus ``maintenance.steps``, which the runner
takes in order (``fluid_build/spark/runner/forge_spark_runner.py::run_maintenance``)::

    {"configVersion": 1, "product": "...", "build": "<writer>", "mode": "maintenance",
     "sink": {...},
     "maintenance": {"expose": "orders", "steps": [
        {"step": "statement", "name": "erase", "sql": "DELETE FROM fluid.silver.orders ..."},
        {"step": "rewrite_data_files", "strategy": "binpack", "options": {...}},
        {"step": "rewrite_manifests"},
        {"step": "expire_snapshots", "olderThanSeconds": 432000, "retainLast": 1},
        {"step": "remove_orphan_files", "olderThanSeconds": 259200}]}}

A plugin action's statements are asked for here, when the config is compiled (``fluid plan``,
``fluid apply`` and ``fluid validate``), and land in the config: its digest, which the plan binds
and the runner checks, covers the SQL that runs, and the runner loads no plugin.

Compaction always commits with partial progress: each file group is its own commit, retried on
a concurrent commit, so a stream's appends never wait for it and a group that a concurrent commit
did conflict with fails alone (the next run takes it again).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from fluid_build.maintenance.spec import TableMaintenance, option_text

#: The steps a maintenance config may list, in the order the runner takes them.
STEPS: Tuple[str, ...] = (
    "statement",
    "rewrite_data_files",
    "rewrite_manifests",
    "expire_snapshots",
    "remove_orphan_files",
)


def table_name(build_config: Mapping[str, Any]) -> str:
    """The table as the runner's catalog names it (``fluid.<database>.<table>``)."""
    sink = build_config["sink"]
    return f"{sink['catalog']}.{sink['table']}"


def action_statements(
    tm: TableMaintenance, table: str, *, load: Any = None
) -> List[Tuple[str, List[str]]]:
    """``(action name, statements)`` for each of ``tm``'s plugin actions, in order: each kind
    loaded by name (``fluid_build.maintenance_actions``), its ``check`` run, its statements
    asked for and checked (:mod:`fluid_build.maintenance.statements`).

    Raises:
        MaintenanceActionError: a kind cannot be used, its ``check`` names a problem, or its
            statements fail the check (the SQL is never quoted).
    """
    from fluid_build.maintenance.plugins import MaintenanceActionError, action_for
    from fluid_build.maintenance.statements import checked_statements

    loader = load or action_for
    out: List[Tuple[str, List[str]]] = []
    for action in tm.actions:
        where = f"exposes[{tm.expose_id}].iceberg.maintenance.actions[{action.name}]"
        try:
            kind = loader(action.kind)
        except MaintenanceActionError as exc:
            raise MaintenanceActionError(f"{where}: {exc}") from None
        options = option_text(action.options)
        problems = _call(kind, "check", where, dict(options))
        if problems:
            if isinstance(problems, str) or not isinstance(problems, Sequence):
                raise MaintenanceActionError(f"{where}: its check() returned no list of problems")
            raise MaintenanceActionError(f"{where}: {str(problems[0])[:300]}")
        raw = _call(kind, "statements", where, table, dict(options))
        try:
            out.append((action.name, checked_statements(raw, table, where)))
        except ValueError as exc:
            raise MaintenanceActionError(str(exc)) from None
    return out


def _call(kind: Any, method: str, where: str, *args: Any) -> Any:
    from fluid_build.maintenance.plugins import MaintenanceActionError

    fn = getattr(kind, method, None)
    if fn is None:
        if method == "check":
            return ()
        raise MaintenanceActionError(f"{where}: the kind has no {method}()")
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - a plugin's failure, by its type only
        raise MaintenanceActionError(
            f"{where}: its {method}() raised {type(exc).__name__}"
        ) from None


def _rewrite_options(tm: TableMaintenance) -> Dict[str, str]:
    """Iceberg's ``RewriteDataFiles`` options, as strings, as Spark actions take them."""
    c = tm.compaction
    if c is None:
        return {}
    options = {
        "partial-progress.enabled": "true",
        "partial-progress.max-commits": str(c.max_commits),
    }
    if c.target_file_size_mb is not None:
        options["target-file-size-bytes"] = str(c.target_file_size_mb * 1024 * 1024)
    if c.min_input_files is not None:
        options["min-input-files"] = str(c.min_input_files)
    if c.delete_file_threshold is not None:
        options["delete-file-threshold"] = str(c.delete_file_threshold)
    if c.max_concurrent_rewrites is not None:
        options["max-concurrent-file-group-rewrites"] = str(c.max_concurrent_rewrites)
    return options


def maintenance_config(
    tm: TableMaintenance,
    build_config: Mapping[str, Any],
    statements: Sequence[Tuple[str, Sequence[str]]] = (),
) -> Dict[str, Any]:
    """The maintenance job config of ``tm``, from its writer build's compiled job config."""
    steps: List[Dict[str, Any]] = [
        {"step": "statement", "name": name, "sql": sql} for name, sqls in statements for sql in sqls
    ]
    if tm.compaction is not None:
        steps.append(
            {
                "step": "rewrite_data_files",
                "strategy": tm.compaction.strategy,
                "options": _rewrite_options(tm),
            }
        )
    if tm.rewrite_manifests:
        steps.append({"step": "rewrite_manifests"})
    if tm.expire is not None:
        steps.append(
            {
                "step": "expire_snapshots",
                "olderThanSeconds": tm.expire.older_than_seconds,
                "retainLast": tm.expire.retain_last,
            }
        )
    if tm.orphans is not None:
        steps.append(
            {"step": "remove_orphan_files", "olderThanSeconds": tm.orphans.older_than_seconds}
        )
    sink = {k: v for k, v in dict(build_config["sink"]).items() if k != "create"}
    return {
        "configVersion": build_config["configVersion"],
        "product": build_config["product"],
        "build": build_config["build"],
        "mode": "maintenance",
        "sink": sink,
        "maintenance": {"expose": tm.expose_id, "steps": steps},
    }


__all__ = ["STEPS", "action_statements", "maintenance_config", "table_name"]
