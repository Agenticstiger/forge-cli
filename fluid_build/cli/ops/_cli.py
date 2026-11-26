# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Argparse shims for the day-2 ops modules.

The ops modules (``status``, ``logs``, ``run_diff``, ``retention``) ship pure
report-builder functions that take a ``StateStore`` and return dataclasses.
This module wires them to argparse subparsers and renders the dataclasses to
human or JSON output.

Naming policy: avoids collision with the existing top-level
``fluid status / doctor / auth`` commands by grouping run-record introspection
under ``fluid runs <verb>`` and using ``fluid retention`` standalone.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

from fluid_build.cli.console import cprint, cprint_literal

# ── Common helpers ─────────────────────────────────────────────────────


def _state_root(args: argparse.Namespace) -> Path:
    """Resolve the state-store root from CLI args or convention.

    Convention: ``./.fluid`` under the current working directory. Override via
    ``--state-root <path>``.
    """
    raw = getattr(args, "state_root", None)
    return Path(raw).resolve() if raw else (Path.cwd() / ".fluid").resolve()


def _build_state_store(state_root: Path):
    """Build a FileStateStore rooted at ``<state_root>``.

    The runner already lays down records under
    ``<state_root>/runs/<product>/<build>/runs/<run-id>.json`` —
    ``FileStateStore.__init__`` takes the parent (``.fluid/`` by convention)
    and the store appends ``runs/...`` itself.

    Lazy import — keeps the cli help fast and lets ``fluid runs --help`` work
    even if the build_runners subtree is broken.
    """
    from fluid_build.build_runners._state import FileStateStore

    return FileStateStore(state_root)


def _emit(args: argparse.Namespace, payload: Any) -> int:
    """Render ``payload`` (dataclass or dict) as JSON when ``--json`` is set,
    or as a human-readable plain block otherwise.
    """
    if is_dataclass(payload):
        payload = asdict(payload)
    if getattr(args, "json", False):
        json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
        return 0
    _print_human(payload)
    return 0


def _print_human(d: Any, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, (dict, list)) and v:
                cprint(f"{pad}{k}:")
                _print_human(v, indent + 1)
            else:
                cprint(f"{pad}{k}: {v}")
    elif isinstance(d, list):
        for item in d:
            if isinstance(item, dict):
                cprint(f"{pad}-")
                _print_human(item, indent + 1)
            else:
                cprint(f"{pad}- {item}")
    else:
        cprint(f"{pad}{d}")


# ── `fluid runs` umbrella ──────────────────────────────────────────────


def _add_common_flags(p: argparse.ArgumentParser) -> None:
    """Attach ``--state-root`` + ``--json`` to a leaf subparser.

    These need to live on the leaf rather than the parent because argparse
    subparsers don't inherit parent args — putting ``--json`` on
    ``fluid runs`` makes it invalid as ``fluid runs status --json``.
    """
    p.add_argument(
        "--state-root",
        help="Path to the .fluid state directory (default: ./.fluid)",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON")


def _add_release_flags(p: argparse.ArgumentParser) -> None:
    """The flags that make ``fluid runs status`` read sealed releases instead of run records."""
    p.add_argument(
        "--release-dir",
        action="append",
        dest="release_dirs",
        metavar="DIR",
        help=(
            "Read the sealed releases `fluid ship --release-dir` published (DIR is one release "
            "or a directory of them; repeatable) and say which was in force: for jobs Forge "
            "does not run, which leave no run record. Replaces the run records."
        ),
    )
    p.add_argument(
        "--on",
        metavar="YYYY-MM-DD",
        help="With --release-dir: the UTC day to answer for (default: now)",
    )
    p.add_argument(
        "--env",
        metavar="NAME",
        help=(
            "With --release-dir: answer only for the releases of this environment "
            "(default: one answer per environment the releases name)"
        ),
    )


def register_runs(subparsers: argparse._SubParsersAction) -> None:
    """Register ``fluid runs {status,verify-release,logs,diff}``."""
    p = subparsers.add_parser(
        "runs",
        help="Inspect run history and sealed releases (status / verify-release / logs / diff)",
    )
    sub = p.add_subparsers(dest="runs_subcmd", required=True)

    s = sub.add_parser(
        "status",
        help="Show last-N runs of a product, or the release that was in force (--release-dir)",
    )
    s.add_argument("product_id", help="Product id (e.g. bronze.crm_orders)")
    s.add_argument("--build", default=None, help="Build id to inspect (default: first found)")
    s.add_argument("--last", type=int, default=5, help="Number of recent runs to show (default 5)")
    _add_release_flags(s)
    _add_common_flags(s)
    s.set_defaults(func=_run_runs_status)

    v = sub.add_parser(
        "verify-release",
        help="Check a sealed release (a directory or s3://) against its release.json",
        description=(
            "Read-only. Checks that release.json is a fluid-release/1 record, that its "
            "identity_digest is the digest of its identity, and that every file it lists has the "
            "sha256 it names. Exit 0 intact, 1 not intact, 2 cannot decide (unreadable, access "
            "denied, or boto3 missing for s3://)."
        ),
    )
    v.add_argument(
        "source",
        metavar="SRC",
        help="A release directory, or the s3://bucket/prefix/ fluid ship --release-to wrote",
    )
    v.add_argument("--json", action="store_true", help="Emit JSON")
    v.set_defaults(func=_run_verify_release)

    lg = sub.add_parser("logs", help="Fetch component logs for a product/run")
    lg.add_argument("product_id", help="Product id")
    lg.add_argument(
        "--component",
        choices=["build", "infra", "server", "worker", "dlq"],
        default="build",
    )
    lg.add_argument("--run-id", default=None)
    lg.add_argument("--grep", default=None)
    lg.add_argument("--limit", type=int, default=1000)
    _add_common_flags(lg)
    lg.set_defaults(func=_run_runs_logs)

    d = sub.add_parser("diff", help="Schema + row-count delta between two runs")
    d.add_argument("product_id", help="Product id")
    d.add_argument("--build", required=True, help="Build id")
    d.add_argument("--run-a", required=True, help="Baseline run id")
    d.add_argument("--run-b", required=True, help="Comparison run id")
    _add_common_flags(d)
    d.set_defaults(func=_run_runs_diff)


# ── `fluid retention` umbrella ─────────────────────────────────────────


def register_retention(subparsers: argparse._SubParsersAction) -> None:
    """Register ``fluid retention sweep`` — periodic state-root cleanup."""
    p = subparsers.add_parser("retention", help="Retention sweep over the state directory")
    sub = p.add_subparsers(dest="retention_subcmd", required=True)

    sw = sub.add_parser("sweep", help="Run a retention sweep with structured summary")
    _add_common_flags(sw)
    sw.set_defaults(func=_run_retention_sweep)


# ── Dispatchers ────────────────────────────────────────────────────────


def _run_release_status(args: argparse.Namespace, logger: logging.Logger) -> int:
    """``fluid runs status <product> --release-dir DIR [--on DAY] [--env NAME]``: the release in
    force, for each environment the releases name."""
    from datetime import datetime, timezone

    from fluid_build.cli.ops import release_status as rs

    on = None
    if args.on:
        on = rs.parse_day(args.on)
        if on is None:
            logger.error("--on %s is not a day (YYYY-MM-DD)", rs.clean(args.on, 40))
            return 2
    try:
        found = rs.find_releases(args.release_dirs, args.product_id)
    except (NotADirectoryError, PermissionError) as exc:
        logger.error("%s", exc)
        return 2
    if args.env is not None and not args.env.strip():
        logger.error("--env needs an environment name")
        return 2
    environment = rs.clean(args.env, 40) if args.env else None
    answers = rs.in_force_by_environment(
        found.releases, on, datetime.now(timezone.utc), environment
    )
    if getattr(args, "json", False):
        json.dump(
            {
                "product_id": args.product_id,
                "on": on.isoformat() if on else None,
                "answers": [
                    {
                        "environment": a.environment,
                        "in_force": a.at_start.as_dict() if a.at_start else None,
                        "during": [r.as_dict() for r in a.during],
                        "not_intact": [r.as_dict() for r in a.not_intact],
                    }
                    for a in answers
                ],
                "releases_read": len(found.releases),
                "not_read": found.skipped,
            },
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
    else:
        for line in rs.report(args.product_id, answers, found, on, environment):
            # soft_wrap: piped (a CI log), the console is 80 columns and would cut a digest
            cprint_literal(line, soft_wrap=True)
    return 0 if any(a.at_start or a.during for a in answers) else 1


def _run_verify_release(args: argparse.Namespace, logger: logging.Logger) -> int:
    """``fluid runs verify-release SRC``: 0 intact, 1 not intact, 2 cannot decide."""
    from fluid_build.api.release import INTACT, NOT_INTACT, verify_release

    result = verify_release(args.source)
    if getattr(args, "json", False):
        json.dump(result.as_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return result.exit_code
    head = {INTACT: "intact", NOT_INTACT: "NOT INTACT"}.get(result.status, "cannot decide")
    cprint_literal(f"{result.source}: {head}", soft_wrap=True)
    for key, value in (
        ("release", result.identity_digest),
        ("code", result.code_digest),
        ("product", result.product_id),
        ("env", result.environment),
    ):
        if value:
            cprint_literal(f"  {key:<8} {value}", soft_wrap=True)
    if result.identity_digest:
        cprint_literal(f"  files    {result.files_checked} checked", soft_wrap=True)
    for problem in result.problems:
        cprint_literal(f"  - {problem}", soft_wrap=True)
    return result.exit_code


def _run_runs_status(args: argparse.Namespace, logger: logging.Logger) -> int:
    if getattr(args, "release_dirs", None):
        return _run_release_status(args, logger)
    if getattr(args, "on", None) or getattr(args, "env", None):
        logger.error("--on and --env answer for --release-dir: pass the directory of releases too")
        return 2
    from fluid_build.cli.ops.status import build_status_report

    state_root = _state_root(args)
    store = _build_state_store(state_root)
    build_id = args.build or _autodiscover_build(state_root, args.product_id)
    if not build_id:
        logger.error(
            "no builds found for product %s under %s/runs/%s/",
            args.product_id,
            state_root,
            args.product_id,
        )
        return 1
    report = build_status_report(store, args.product_id, build_id, limit=args.last)
    return _emit(args, report)


def _run_runs_logs(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.cli.ops.logs import LogComponent, fetch_logs

    state_root = _state_root(args)
    lines = fetch_logs(
        state_root,
        args.product_id,
        component=LogComponent(args.component),
        run_id=args.run_id,
        grep=args.grep,
        limit=args.limit,
    )
    if getattr(args, "json", False):
        json.dump(
            [
                {
                    "timestamp": l.timestamp,
                    "level": l.level,
                    "component": l.component,
                    "message": l.message,
                }
                for l in lines
            ],
            sys.stdout,
            indent=2,
            default=str,
        )
        sys.stdout.write("\n")
        return 0
    if not lines:
        cprint(f"(no {args.component} logs for {args.product_id})")
        return 0
    for line in lines:
        ts = line.timestamp or "-"
        lvl = line.level or "-"
        cprint_literal(f"{ts}  {lvl:5s}  {line.message}")
    return 0


def _run_runs_diff(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.cli.ops.run_diff import run_diff as _run_diff

    state_root = _state_root(args)
    store = _build_state_store(state_root)
    diff = _run_diff(
        store,
        args.product_id,
        args.build,
        run_a=args.run_a,
        run_b=args.run_b,
    )
    return _emit(args, diff)


def _run_retention_sweep(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.cli.ops.retention import sweep_with_summary

    state_root = _state_root(args)
    summary = sweep_with_summary(state_root)
    return _emit(args, summary)


# ── Helpers ────────────────────────────────────────────────────────────


def _autodiscover_build(state_root: Path, product_id: str) -> Optional[str]:
    """Pick the first build directory under ``runs/<product_id>/`` if any."""
    runs_dir = state_root / "runs" / product_id
    if not runs_dir.is_dir():
        return None
    for child in sorted(runs_dir.iterdir()):
        if child.is_dir():
            return child.name
    return None
