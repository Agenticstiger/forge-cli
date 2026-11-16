# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid spark {run,status}``: start a Spark build's run, or show its runs.

``fluid apply`` provisions a Spark build and keeps a streaming one running. A scheduled build
(batch, or a stream with ``trigger.availableNow``) is started by its scheduler instead:

    fluid spark run contract.fluid.yaml --build-id daily_orders

starts one run on the build's runtime (EMR Serverless, Glue, EMR on EC2 or EMR on EKS),
waits for it to end and exits 0 only when it succeeded, so an Airflow ``BashOperator`` task
fails when the run does. Ctrl-C, SIGTERM or SIGHUP (a task killed) cancel the run. On a
streaming build it reconciles the job as ``fluid apply`` does; ``--restart`` starts one whose
last run failed.

Exit codes: 0 the run succeeded (``status``: every build read); 1 the run failed, was
cancelled or could not be started or read; 2 the contract or the operator's policy refuses
the build, so nothing was started. With ``--json`` the command prints one JSON line, a
refusal included. Neither it nor an error carries a credential: the job config holds the
mask key's Secrets Manager ARN, never the key, and AWS text is passed through the secret
redactor.

Light at import: only the parser. ``build_runners.spark`` and boto3 load when a subcommand
runs, so the ``fluid --help`` cold path never imports them.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List

COMMAND = "spark"
#: The exit code of a build the contract or the operator's policy refuses.
EXIT_REFUSED = 2
_EPILOG = (
    "exit codes: 0 succeeded; 1 failed, cancelled, or not started or read; "
    "2 refused by the contract or the operator's policy (nothing started)"
)


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        COMMAND,
        help="Start a Spark build's run (for a scheduler) or show its runs",
        description=(
            "Start a Spark build's run, or show its runs. fluid apply keeps a streaming "
            "build running; a scheduler starts a scheduled one with fluid spark run."
        ),
        epilog=_EPILOG,
    )
    sub = parser.add_subparsers(dest="spark_command", metavar="{run,status}")
    sub.required = True

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("contract", help="the contract (contract.fluid.yaml)")
        p.add_argument("--env", help="the environment overlay to apply")
        p.add_argument("--json", action="store_true", help="print JSON instead of lines")

    run = sub.add_parser(
        "run", help="Start one run of a Spark build and wait for it to end", epilog=_EPILOG
    )
    common(run)
    run.add_argument("--build-id", required=True, help="the build to run")
    run.add_argument("--no-wait", action="store_true", help="return once the run is started")
    run.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="seconds to wait for the run to end before cancelling it (default 21600)",
    )
    run.add_argument(
        "--restart",
        action="store_true",
        help="streaming build: start it again although its last run failed",
    )
    run.set_defaults(func=run_run)

    status = sub.add_parser("status", help="Show each Spark build's recent runs")
    common(status)
    status.add_argument("--build-id", help="only this build")
    status.set_defaults(func=run_status)


def _runs(args: argparse.Namespace, logger: logging.Logger, command: str) -> List[Any]:
    from pathlib import Path

    from fluid_build.build_runners.spark._apply import _desired
    from fluid_build.cli._common import CLIError, hydrate_dotenv, load_contract_with_overlay
    from fluid_build.cli.plan import _gate_contract_for_plan_or_apply

    # As fluid apply does: the project's .env may hold AWS_REGION and the operator's lists.
    hydrate_dotenv(Path.cwd(), environment=getattr(args, "env", None))
    build_id = getattr(args, "build_id", None)
    try:
        contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        _gate_contract_for_plan_or_apply(contract, logger, command=f"spark_{command}")
        runs = _desired(contract, f"spark_{command}", build_id=build_id)
    except CLIError as exc:
        exc.exit_code = EXIT_REFUSED  # nothing was started: the contract or policy refused it
        raise
    if not runs:
        raise CLIError(
            EXIT_REFUSED,
            f"spark_{command}_no_build",
            {
                "error": (
                    f"no Spark build {build_id!r}"
                    if build_id
                    else "the contract has no Spark build (engine: spark with parameters.spark)"
                ),
                "path": args.contract,
            },
        )
    return runs


def run_run(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.build_runners.spark._reconcile import DEFAULT_RUN_WAIT_S, run_once
    from fluid_build.cli._common import CLIError
    from fluid_build.cli.console import cprint

    try:
        run = _runs(args, logger, "run")[0]
    except CLIError as exc:
        if not args.json:
            raise
        _json_line(_refusal(args, exc))
        return exc.exit_code
    say = (lambda _text: None) if args.json else (lambda text: cprint(text, markup=False))
    outcome = run_once(
        run,
        wait=not args.no_wait,
        timeout=args.timeout or DEFAULT_RUN_WAIT_S,
        restart=args.restart,
        say=say,
    )
    if args.json:
        _json_line(outcome.as_dict())
    else:
        cprint(outcome.line(), markup=False)
    return 1 if outcome.error else 0


def _refusal(args: argparse.Namespace, exc: Any) -> Dict[str, Any]:
    from fluid_build.observability.secret_redactor import redact_secret_text

    detail = exc.context.get("error") if isinstance(exc.context, dict) else None
    return {
        "build": getattr(args, "build_id", None),
        "runtime": None,
        "action": "refused",
        "event": exc.event,
        "runId": None,
        "state": None,
        "message": redact_secret_text(str(detail or exc.event)),
        "error": True,
    }


def _json_line(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def run_status(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.build_runners.spark._reconcile import status
    from fluid_build.cli.console import cprint

    rows = status(_runs(args, logger, "status"))
    if args.json:
        _json_line(rows)
    else:
        for row in rows:
            head = f"Spark {row['build']} ({row['runtime']})"
            if "error" in row:
                cprint(f"{head}: {row['error']}", markup=False)
                continue
            runs = row.get("runs") or []
            cprint(f"{head} on {row.get('where')}: {len(runs) or 'no'} recent runs", markup=False)
            for r in runs:
                mark = "current config" if r["current"] else "older config"
                attempt = f", attempt {r['attempt']}" if r.get("attempt") else ""
                detail = f": {r['details']}" if r.get("details") else ""
                cprint(f"  {r['runId']} {r['state']} ({mark}{attempt}){detail}", markup=False)
    return 1 if any("error" in r for r in rows) else 0
