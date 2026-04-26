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

"""Unified staged memory command."""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.cli.console import cprint
from fluid_build.cli.forge_copilot_memory import CopilotMemoryStore
from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory
from fluid_build.cli.forge_team_memory import load_team_memory, scaffold_team_memory
from fluid_build.copilot.store.base import Store
from fluid_build.copilot.store.factory import resolve_store

COMMAND = "memory"

# Pattern for ``--older-than`` values: ``<int><unit>`` where unit is one
# of ``s|m|h|d|w``. The plan-promised default vocabulary; days are by
# far the most common in real ops scripts.
_DURATION_PATTERN = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_TO_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86_400,
    "w": 7 * 86_400,
}


def _parse_duration(value: str) -> timedelta:
    """Parse ``--older-than`` arguments like ``30d`` or ``2w``.

    Raises :class:`argparse.ArgumentTypeError` on malformed input so
    the parser surfaces a clean error to the user instead of failing
    deep inside the clear path.
    """
    match = _DURATION_PATTERN.match(value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"--older-than must look like '30d' / '2w' / '12h'; got {value!r}"
        )
    quantity = int(match.group(1))
    unit = match.group(2).lower()
    return timedelta(seconds=quantity * _UNIT_TO_SECONDS[unit])


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(COMMAND, help="Inspect and manage staged memory/cache state")
    # ``required=False`` so a bare ``fluid memory`` doesn't blow up
    # with the bare-bones argparse "the following arguments are
    # required: memory_action" error.  ``run`` catches the
    # ``memory_action is None`` case and renders a Rich-friendly
    # panel listing the subcommands instead.
    parser.set_defaults(func=run)
    sp = parser.add_subparsers(dest="memory_action", required=False)

    sp.add_parser("status", help="Show store status").set_defaults(func=run)
    show = sp.add_parser("show", help="Show a memory scope or namespace listing")
    show.add_argument(
        "scope",
        choices=["project", "team", "personal", "episodic", "semantic", "history"],
        help=(
            "project|team|personal — load the named scope from disk. "
            "episodic|semantic|history — list the corresponding store "
            "namespace (record keys + metadata, decay-ordered for "
            "episodic, version-ordered for history)."
        ),
    )
    show.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max records to list for episodic / semantic / history (default: 20).",
    )
    show.set_defaults(func=run)

    save = sp.add_parser("save", help="Sync a memory scope into the staged store")
    save.add_argument("--scope", choices=["project", "team", "personal"], required=True)
    save.set_defaults(func=run)

    clear = sp.add_parser("clear", help="Clear staged store namespaces")
    clear.add_argument("--ns", default=None, help="Namespace or root to clear")
    clear.add_argument(
        "--older-than",
        type=_parse_duration,
        default=None,
        metavar="DURATION",
        help=(
            "Only clear records older than this duration "
            "(e.g. '30d', '2w', '12h'). "
            "Without this flag, every record in the namespace is removed."
        ),
    )
    clear.set_defaults(func=run)

    search = sp.add_parser("search", help="Search a staged store namespace")
    search.add_argument("query")
    search.add_argument("--ns", default="memory/semantic")
    search.add_argument(
        "--mode", choices=["exact", "keyword", "vector", "hybrid"], default="hybrid"
    )
    search.set_defaults(func=run)


def run(args, logger: logging.Logger) -> int:
    action = getattr(args, "memory_action", None)
    if action is None:
        # Bare ``fluid memory`` — render an intuitive guide instead
        # of the legacy argparse "the following arguments are
        # required" error.
        return _render_memory_guide()
    store = resolve_store(workspace_root=Path.cwd())
    if action == "status":
        return _run_status(store)
    if action == "show":
        return _run_show(args.scope, store, limit=getattr(args, "limit", 20))
    if action == "save":
        return _run_save(args.scope, store)
    if action == "clear":
        return _run_clear(
            store,
            ns=getattr(args, "ns", None),
            older_than=getattr(args, "older_than", None),
        )
    if action == "search":
        results = store.search(args.ns, args.query, mode=args.mode, limit=10)
        cprint(json.dumps([record.value for record in results], indent=2, default=str))
        return 0
    return 1


def _render_memory_guide() -> int:
    """Render an intuitive guide for ``fluid memory`` with no
    subcommand.  Detects existing ``~/.fluid/store/`` state and
    promotes ``status`` when memory has already been written."""

    from fluid_build.cli._subcommand_guide import (
        SubcommandEntry,
        SubcommandGuide,
        SubcommandHint,
        render_subcommand_guide,
    )

    entries = [
        SubcommandEntry(
            name="status",
            description="Show staged-store backend + per-namespace record counts.",
            example="fluid memory status",
        ),
        SubcommandEntry(
            name="show",
            description=(
                "Show a memory scope (project / team / personal) or list the "
                "records in a store namespace (episodic / semantic / history)."
            ),
            example="fluid memory show project",
        ),
        SubcommandEntry(
            name="save",
            description="Sync a memory scope (project / team / personal) into the staged store.",
            example="fluid memory save --scope project",
        ),
        SubcommandEntry(
            name="search",
            description="Search a staged store namespace (exact / keyword / vector / hybrid).",
            example='fluid memory search "<query>" --ns memory/semantic --mode hybrid',
        ),
        SubcommandEntry(
            name="clear",
            description="Clear staged store namespaces (optionally older-than a duration).",
            example="fluid memory clear --ns memory/episodic --older-than 30d",
        ),
    ]

    def _detect_hint() -> Any:
        store_dir = Path.home() / ".fluid" / "store"
        if store_dir.is_dir():
            return SubcommandHint(
                subcommand="status",
                rationale="you already have memory state in ~/.fluid/store/.",
            )
        return None

    guide = SubcommandGuide(
        command_path="fluid memory",
        headline=(
            "Inspect and manage staged memory + cache state — project / team / "
            "personal scopes plus the episodic / semantic / history namespaces."
        ),
        entries=entries,
        hint_provider=_detect_hint,
        quick_start="fluid memory status",
    )
    return render_subcommand_guide(guide)


def _run_status(store) -> int:
    root = getattr(store, "root", None)
    counts: Dict[str, int] = {}
    if root is not None:
        for path in root.rglob("*.json"):
            namespace = str(path.parent.relative_to(root)).replace("\\", "/")
            counts[namespace] = counts.get(namespace, 0) + 1
    else:
        for namespace in (
            "llm/logical",
            "llm/builder",
            "llm/readme",
            "llm/transformation",
            "llm/validator",
            "memory/project",
            "memory/personal",
            "memory/episodic",
            "memory/semantic",
            "discovery",
            "skills",
            "history",
            "audit",
        ):
            results = store.query(namespace, limit=1000)
            if results:
                counts[namespace] = len(results)
    payload = {
        "backend": store.__class__.__name__,
        "root": str(root) if root is not None else None,
        "namespaces": counts,
    }
    cprint(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _run_show(scope: str, store: Any = None, *, limit: int = 20) -> int:
    if scope == "project":
        memory = CopilotMemoryStore(Path.cwd()).load()
        cprint(json.dumps(memory.to_dict() if memory else {}, indent=2, default=str))
        return 0
    if scope == "team":
        memory = load_team_memory(Path.cwd())
        cprint(json.dumps(memory.to_prompt_payload() if memory else {}, indent=2, default=str))
        return 0
    if scope == "personal":
        cprint(json.dumps(load_personal_memory() or {}, indent=2, default=str))
        return 0
    # Namespace listings — episodic / semantic / history.
    # Each shows the record's metadata + a compact value preview so a
    # quick ``fluid memory show semantic`` is readable on a 80-col
    # terminal without dumping multi-megabyte payloads.
    namespace_map = {
        "episodic": "memory/episodic",
        "semantic": "memory/semantic",
        "history": "history",
    }
    namespace = namespace_map.get(scope)
    if namespace is None:
        return 1
    if store is None:
        store = resolve_store(workspace_root=Path.cwd())
    records = store.query(namespace, limit=limit)
    payload = [
        {
            "key": r.key,
            "metadata": r.metadata or {},
            "value_preview": _preview(r.value),
        }
        for r in records
    ]
    cprint(json.dumps(payload, indent=2, default=str))
    return 0


def _preview(value: Any, *, max_chars: int = 200) -> Any:
    """Compact a record value for ``fluid memory show`` output.

    Returns the value unchanged when it's short enough; otherwise a
    truncated string representation. Keeps long OSI payloads from
    dominating the output without losing the key signal.
    """
    if isinstance(value, dict):
        # For dict values, keep the top-level keys and stringify the
        # top-level values to ``max_chars`` each.
        return {
            k: (v if len(str(v)) <= max_chars else str(v)[:max_chars] + "…")
            for k, v in list(value.items())[:10]
        }
    text = str(value)
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def _run_clear(store, *, ns: Optional[str] = None, older_than: Optional[timedelta] = None) -> int:
    """Clear store records, optionally only those older than ``older_than``.

    The default (``older_than=None``) preserves the v1.0 behaviour:
    every record under ``ns`` (or the entire store when ``ns`` is
    ``None``) is removed. With ``older_than``, the helper walks the
    file backend's tree and deletes only files whose mtime is older
    than ``now - older_than``. Non-FileBackend stores fall through to
    the unconditional clear with a stderr warning so the caller knows
    the TTL filter wasn't applied.
    """
    if older_than is None:
        count = store.clear(ns)
        cprint(f"Cleared {count} record(s).")
        return 0

    root = getattr(store, "root", None)
    if root is None:
        # Non-FileBackend (e.g., NullBackend in tests) — log and bail.
        # Postgres / Sqlite TTL pruning is a v1.6+ extension on the
        # backend ABC; today we only support the filesystem path.
        cprint(
            f"--older-than is only supported by FileBackend today; "
            f"got {store.__class__.__name__}. Skipping.",
            file_stream="stderr",
        )
        return 1

    cutoff = datetime.now(timezone.utc).timestamp() - older_than.total_seconds()
    target_root: Path = root if ns is None else (root / Path(*ns.split("/")))
    if not target_root.exists():
        cprint(f"No records under {target_root}.")
        return 0
    removed = 0
    for path in target_root.rglob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    cprint(f"Cleared {removed} record(s) older than {older_than}.")
    return 0


def _run_save(scope: str, store: Store) -> int:
    if scope == "project":
        memory = CopilotMemoryStore(Path.cwd()).load()
        if memory is None:
            cprint("No project memory found.")
            return 1
        store.put("memory/project", Path.cwd().resolve().as_posix(), memory.to_dict())
        cprint("Saved project memory into the staged store.")
        return 0
    if scope == "team":
        memory = load_team_memory(Path.cwd())
        if memory is None:
            path = scaffold_team_memory(Path.cwd())
            cprint(f"Scaffolded team memory at {path}")
            memory = load_team_memory(Path.cwd())
        store.put(
            "memory/team",
            Path.cwd().resolve().as_posix(),
            memory.to_prompt_payload() if memory else {},
        )
        cprint("Saved team memory into the staged store.")
        return 0
    if scope == "personal":
        memory = load_personal_memory()
        if memory is None:
            cprint("No personal memory found.")
            return 1
        store.put("memory/personal", "default", memory)
        cprint("Saved personal memory into the staged store.")
        return 0
    return 1
