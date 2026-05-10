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

"""CLI wrapper for the consumer MCP output-port server.

Adds the ``output-port`` action group under the existing
``fluid mcp`` subcommand. The actual JSON-RPC dispatcher lives in
:mod:`fluid_build.output_ports.mcp.server`; this module is the
argparse + policy-builder + contract-loader glue so the dispatcher
stays pure Python and easy to reuse from tests / scripts.

Why a separate module instead of folding into ``cli/mcp.py``:

* The consumer-side surface has a different threat model and
  different flags (``--allow-sql``, ``--max-sample-rows``,
  ``--expose-id``); merging them would double the flag count of one
  subcommand and confuse operators.
* Keeping the module self-contained lets unit tests import only what
  they need without dragging in the staged-forge data-model stack.

Surface:

* ``fluid mcp output-port serve <contract> --expose-id <id>``
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from fluid_build.output_ports.mcp import (
    OutputPortPolicy,
    find_expose,
    list_exposes,
    resolve_expose_paths,
    run_stdio,
)

from ._common import CLIError, load_contract_with_overlay


def attach_to_mcp_subparsers(sp: argparse._SubParsersAction) -> None:
    """Register the ``output-port`` action group under ``fluid mcp``.

    Called from :func:`fluid_build.cli.mcp.register` so the surface
    is wired through the existing ``mcp`` subcommand without bumping
    the top-level command count.
    """
    parent = sp.add_parser(
        "output-port",
        help="Serve a FLUID expose to MCP consumers (Claude, Cursor, agents).",
        description=(
            "Consumer-side MCP server: bind one expose from a FLUID contract "
            "and serve `describe`, `sample`, `query`, and (optionally) "
            "`query_sql` tools to MCP clients. Distinct from `fluid mcp serve`, "
            "which exposes forge authoring tools."
        ),
    )
    parent.set_defaults(func=_route_with_default_help)
    sub = parent.add_subparsers(dest="output_port_action", required=False)

    serve = sub.add_parser(
        "serve",
        help="Run the MCP stdio server bound to one expose.",
        description=(
            "Start a stdio MCP server bound to a single expose from "
            "a FLUID contract. Pipe stdin/stdout to an MCP client "
            "(Claude Code, Cursor, MCP Inspector). The server is "
            "read-only by default; pass --allow-sql to enable the "
            "free-form query_sql tool."
        ),
    )
    serve.add_argument(
        "contract",
        help="Path to the FLUID contract YAML or fragment to serve.",
    )
    serve.add_argument(
        "--expose-id",
        default=None,
        metavar="EXPOSE_ID",
        help=(
            "exposeId (from contract.exposes[].exposeId) to bind the "
            "server to. Optional when the contract contains exactly "
            "one expose — the server picks it automatically and logs "
            "the choice."
        ),
    )
    serve.add_argument(
        "--env",
        default=None,
        metavar="ENVIRONMENT",
        help=(
            "Optional environment overlay name passed to the contract "
            "loader so per-environment overrides are applied (matches "
            "fluid plan / fluid apply)."
        ),
    )
    serve.add_argument(
        "--allow-tools",
        default=None,
        metavar="TOOL[,TOOL...]",
        help=(
            "Comma-separated allowlist of tool names. Tools outside the "
            "list are blocked and hidden from tools/list. Default: all "
            "tools allowed (subject to other gates)."
        ),
    )
    serve.add_argument(
        "--deny-tools",
        default=None,
        metavar="TOOL[,TOOL...]",
        help=(
            "Comma-separated blocklist of tool names. Evaluated before "
            "--allow-tools so denial wins."
        ),
    )
    serve.add_argument(
        "--readable-paths",
        default=None,
        metavar="PATH[,PATH...]",
        help=(
            "Comma-separated filesystem roots the server may read from "
            "(today only the contract YAML; field reserved for future "
            "tools). Defaults to the directory of the contract argument."
        ),
    )
    serve.add_argument(
        "--allow-sql",
        action="store_true",
        help=(
            "Enable the free-form query_sql tool. OFF by default — when "
            "enabled, every SQL statement is still passed through the "
            "SQL-safety allowlist before execution. Use only with "
            "trusted internal copilots."
        ),
    )
    serve.add_argument(
        "--max-sample-rows",
        type=int,
        default=100,
        metavar="N",
        help=(
            "Hard cap on rows returned by the sample tool. Asking for "
            "more than the cap silently returns the cap. Default: 100."
        ),
    )
    serve.add_argument(
        "--query-timeout-seconds",
        type=float,
        default=60.0,
        metavar="SEC",
        help=(
            "Statement timeout passed to the engine driver where "
            "supported (Snowflake, BigQuery). Default: 60s."
        ),
    )
    # Transport selection. ``stdio`` is the default — what Claude
    # Desktop / Cursor / MCP Inspector use over a child-process
    # pipe. ``http`` switches to MCP-SSE on the chosen host:port for
    # network-served deployments. The gateway has no built-in HTTP
    # auth today; pair with a reverse proxy that enforces mTLS or
    # OAuth until the auth phase ships.
    serve.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help=(
            "MCP transport. `stdio` (default) for desktop tool "
            "integrations; `http` for MCP-SSE on `--host:--port` "
            "(no built-in auth — front with mTLS/OAuth proxy)."
        ),
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for `--transport http`. Default: 127.0.0.1.",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind port for `--transport http`. Default: 8765.",
    )
    # NEW in v0.7.4 — agentPolicy runtime gates (model + use-case).
    # The contract's `expose.policy.agentPolicy` is the source of
    # truth; these flags are operational overrides for ops/incident
    # response and audit-trace scenarios. When set, they REPLACE the
    # contract value entirely (not merged) so the override is
    # intentional and grep-able in the audit trail (policy_source=cli).
    serve.add_argument(
        "--allow-models",
        default=None,
        metavar="MODEL[,MODEL...]",
        help=(
            "Override agentPolicy.allowedModels for this run. Caller "
            "model_id (declared at MCP initialize) must be in this list. "
            "When unset, the contract value is used."
        ),
    )
    serve.add_argument(
        "--deny-models",
        default=None,
        metavar="MODEL[,MODEL...]",
        help=(
            "Override agentPolicy.deniedModels. Evaluated before the "
            "allowlist so denial wins. When unset, the contract value "
            "is used."
        ),
    )
    serve.add_argument(
        "--allow-use-cases",
        default=None,
        metavar="USE_CASE[,USE_CASE...]",
        help=(
            "Override agentPolicy.allowedUseCases. Caller useCase "
            "(declared at MCP initialize) must be in this list. When "
            "unset, the contract value is used."
        ),
    )
    serve.add_argument(
        "--deny-use-cases",
        default=None,
        metavar="USE_CASE[,USE_CASE...]",
        help=(
            "Override agentPolicy.deniedUseCases. Evaluated before the "
            "allowlist so denial wins. When unset, the contract value "
            "is used."
        ),
    )
    serve.set_defaults(func=run)

    list_parser = sub.add_parser(
        "list",
        help="List the exposes in a contract that this server can serve.",
        description=(
            "Print a table of every expose in the contract with its "
            "kind, binding platform/format, and resolved table "
            "reference. Use this to find the right --expose-id without "
            "reading the YAML by hand."
        ),
    )
    list_parser.add_argument("contract", help="Path to the FLUID contract YAML.")
    list_parser.add_argument(
        "--env",
        default=None,
        metavar="ENVIRONMENT",
        help="Optional environment overlay name.",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human table.",
    )
    list_parser.set_defaults(func=run)

    doctor = sub.add_parser(
        "doctor",
        help="Preflight: load the driver, run health_check, print the resolved binding.",
        description=(
            "Loads the driver for one expose and runs its health "
            "check (cheap engine round-trip). Use BEFORE wiring "
            "`fluid mcp output-port serve` to an MCP client so "
            "credential / network / binding issues surface as a "
            "clear preflight failure rather than a failed tools/call."
        ),
    )
    doctor.add_argument("contract", help="Path to the FLUID contract YAML.")
    doctor.add_argument(
        "--expose-id",
        default=None,
        metavar="EXPOSE_ID",
        help=(
            "exposeId to probe. Optional when the contract has exactly "
            "one expose; the doctor picks it automatically."
        ),
    )
    doctor.add_argument(
        "--env",
        default=None,
        metavar="ENVIRONMENT",
        help="Optional environment overlay name.",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-friendly report.",
    )
    doctor.set_defaults(func=run)


# ---------------------------------------------------------------------
# Run-time entry points
# ---------------------------------------------------------------------


def run(args, logger: logging.Logger) -> int:
    action = getattr(args, "output_port_action", None)
    if action is None:
        return _render_output_port_guide()
    if action == "serve":
        return _run_serve(args, logger)
    if action == "list":
        return _run_list(args, logger)
    if action == "doctor":
        return _run_doctor(args, logger)
    logger.error("unknown_output_port_action: %s", action)
    return 1


def _route_with_default_help(
    args, logger: logging.Logger
) -> int:  # pragma: no cover - argparse glue
    return run(args, logger)


def _resolve_contract_path(args) -> Path:
    contract_arg = getattr(args, "contract", None)
    if not contract_arg:
        raise CLIError(2, "missing_contract", {})
    contract_path = Path(contract_arg).expanduser().resolve()
    if not contract_path.exists():
        raise CLIError(
            2,
            "contract_not_found",
            {"contract": str(contract_path)},
        )
    return contract_path


def _run_serve(args, logger: logging.Logger) -> int:
    contract_path = _resolve_contract_path(args)
    contract = load_contract_with_overlay(str(contract_path), args.env, logger)
    expose = find_expose(contract, getattr(args, "expose_id", None) or None)
    expose_id = str(expose.get("exposeId"))
    if not getattr(args, "expose_id", None):
        # We auto-picked the only expose. Surface the choice so the
        # operator (and MCP client logs) see exactly what got bound.
        sys.stderr.write(
            f"fluid mcp output-port: auto-selected expose {expose_id!r} "
            f"(only expose in contract).\n"
        )
        sys.stderr.flush()
    # Fail-loud guard for unsupported agentPort kinds. The schema
    # accepts ``a2a`` and ``iatp`` as reserved values for future
    # versions, but the gateway only implements ``mcp`` today. A
    # contract that opts into an unimplemented kind would silently
    # serve the MCP surface, defeating the operator's intent.
    agent_port = expose.get("agentPort") or {}
    port_kind = agent_port.get("kind") if isinstance(agent_port, dict) else None
    if port_kind and port_kind != "mcp":
        raise CLIError(
            f"expose.agentPort.kind={port_kind!r} is reserved but not yet "
            "implemented by `fluid mcp output-port serve`. Only `kind: mcp` "
            "is supported in this release. Remove the agentPort block or "
            "set kind=mcp explicitly."
        )
    policy = _build_policy(args, contract_path=contract_path, expose=expose)
    # Boot-time auditRequired enforcement. The contract can declare
    # ``policy.agentPolicy.auditRequired: true``; the gateway always
    # writes to ``~/.fluid/store/audit/`` so a local sink is implicit,
    # but operators on shared infra need the chance to point at a
    # custom audit root via ``FLUID_AUDIT_ROOT``. When auditRequired
    # is true and the operator hasn't set FLUID_AUDIT_ROOT, we surface
    # the implicit location loud rather than silently using $HOME.
    audit_required = bool(
        ((expose.get("policy") or {}).get("agentPolicy") or {}).get("auditRequired")
    )
    if audit_required and not os.environ.get("FLUID_AUDIT_ROOT"):
        sys.stderr.write(
            "fluid mcp output-port: agentPolicy.auditRequired=true; "
            "writing audit events to ~/.fluid/store/audit/ (set "
            "FLUID_AUDIT_ROOT to redirect to a SIEM-forwarded path).\n"
        )
        sys.stderr.flush()
    # Audit-log rotation on boot — protects long-running gateways
    # from unbounded disk growth. Tunable via FLUID_AUDIT_MAX_AGE_DAYS
    # (default 30) and FLUID_AUDIT_MAX_TOTAL_MB (default 256).
    from pathlib import Path as _Path

    from fluid_build.copilot.store.audit_trail import rotate_audit_directory

    audit_root_env = os.environ.get("FLUID_AUDIT_ROOT")
    rotation_root = _Path(audit_root_env) if audit_root_env else None
    rot_counters = rotate_audit_directory(root=rotation_root, logger=logger)
    if rot_counters.get("removed_age", 0) or rot_counters.get("removed_size", 0):
        sys.stderr.write(
            f"fluid mcp output-port: rotated audit dir — "
            f"removed {rot_counters['removed_age']} aged + "
            f"{rot_counters['removed_size']} oversize files; "
            f"{rot_counters['kept']} retained.\n"
        )
        sys.stderr.flush()
    # canStore advisory: surface to operators that the gateway
    # cannot enforce 'do not store' once data crosses the wire.
    can_store = ((expose.get("policy") or {}).get("agentPolicy") or {}).get("canStore")
    if can_store is False:
        sys.stderr.write(
            "fluid mcp output-port: agentPolicy.canStore=false is advisory — "
            "the gateway emits a `do-not-store` hint in the describe payload, "
            "but cannot prevent the receiving model from storing data once it "
            "crosses the wire. Combine with cloud-IAM ephemeral credentials "
            "for a real guarantee.\n"
        )
        sys.stderr.flush()
    # Retention warning: same — the contract can declare a retention
    # window but the gateway is not the data owner; surface so the
    # operator wires up a separate retention sweeper.
    retention = ((expose.get("policy") or {}).get("agentPolicy") or {}).get("retentionPolicy") or {}
    if isinstance(retention, dict) and retention.get("requireDeletion"):
        sys.stderr.write(
            "fluid mcp output-port: agentPolicy.retentionPolicy.requireDeletion=true — "
            "the gateway honours `auditRequired` and `canStore` advisory hints "
            "but does NOT delete data on the engine side. Pair with a Snowflake "
            "TASK / BigQuery scheduled query to enforce retention at the source.\n"
        )
        sys.stderr.flush()
    # Self-attestation warning — caller model_id comes from the MCP
    # initialize handshake's clientInfo. A malicious or buggy client
    # can lie. Surface this loudly at startup so operators don't
    # mistake the gateway for cryptographic identity. P3 will land
    # OAuth/SPIFFE/mTLS via the MCP session-auth borrow target.
    if (
        policy.allowed_models is not None
        or policy.denied_models
        or policy.allowed_use_cases is not None
        or policy.denied_use_cases
    ):
        sys.stderr.write(
            "⚠️  fluid mcp output-port: caller model_id is self-attested via "
            "MCP clientInfo. Do not expose this gateway over an untrusted "
            "network until P3 (OAuth/SPIFFE/mTLS identity) ships. See "
            "https://github.com/Agenticstiger/forge-docs#agent-policy.\n"
        )
        sys.stderr.flush()
    if sys.stderr.isatty():
        sys.stderr.write(
            f"fluid mcp output-port: serving expose={expose_id!r} "
            f"contract={contract_path.name} — ready for MCP client on stdio.\n"
        )
        sys.stderr.flush()
    logger.info(
        "output_port_serve_start",
        extra={
            "contract": str(contract_path),
            "exposeId": expose_id,
            "allowFreeFormSql": policy.allow_free_form_sql,
            "maxSampleRows": policy.max_sample_rows,
            "policySource": policy.policy_source,
            "auditRequired": audit_required,
            "agentPortKind": port_kind or "implicit-mcp",
        },
    )
    transport = getattr(args, "transport", "stdio")
    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", 8765) or 8765)
    if transport == "http":
        sys.stderr.write(
            f"⚠️  fluid mcp output-port: HTTP transport binds {host}:{port} with "
            "NO built-in auth. Front with a reverse proxy that enforces "
            "mTLS/OAuth before exposing to an untrusted network.\n"
        )
        sys.stderr.flush()
    return run_stdio(
        contract=contract,
        expose=expose,
        policy=policy,
        logger=logger,
        transport=transport,
        host=host,
        port=port,
    )


def _run_list(args, logger: logging.Logger) -> int:
    contract_path = _resolve_contract_path(args)
    contract = load_contract_with_overlay(str(contract_path), args.env, logger)
    summaries = list_exposes(contract)
    if getattr(args, "json", False):
        import json as _json

        sys.stdout.write(_json.dumps({"exposes": summaries}, indent=2, default=str))
        sys.stdout.write("\n")
        return 0
    if not summaries:
        sys.stdout.write(f"No exposes in {contract_path}.\n")
        return 0
    sys.stdout.write(f"Exposes in {contract_path.name} ({len(summaries)} total):\n\n")
    for entry in summaries:
        flags = []
        if entry["hasSemantics"]:
            flags.append("semantics")
        if entry["hasMcpOverrides"]:
            flags.append("expose.mcp")
        flag_text = f"  [{', '.join(flags)}]" if flags else ""
        sys.stdout.write(
            f"  • {entry['exposeId']!s}  ({entry['kind']})\n"
            f"      title:  {entry['title'] or '<unset>'}\n"
            f"      engine: {entry['platform']}/{entry['format']} → "
            f"{entry['tableReference']}\n"
            f"      tools:  describe, sample"
            + (", query" if entry["hasSemantics"] else "")
            + f"{flag_text}\n\n"
        )
    return 0


def _run_doctor(args, logger: logging.Logger) -> int:
    contract_path = _resolve_contract_path(args)
    contract = load_contract_with_overlay(str(contract_path), args.env, logger)
    try:
        expose = find_expose(contract, getattr(args, "expose_id", None) or None)
    except ValueError as exc:
        msg = f"contract: {exc}"
        if getattr(args, "json", False):
            import json as _json

            sys.stdout.write(_json.dumps({"status": "fail", "detail": msg}) + "\n")
        else:
            sys.stdout.write(f"FAIL: {msg}\n")
        return 1
    expose_id = str(expose.get("exposeId"))
    report = _build_doctor_report(
        contract_path=contract_path, contract=contract, expose=expose, logger=logger
    )
    if getattr(args, "json", False):
        import json as _json

        sys.stdout.write(_json.dumps(report, indent=2, default=str) + "\n")
        return 0 if report["status"] == "ok" else 1
    _print_doctor_report(report, expose_id=expose_id)
    return 0 if report["status"] == "ok" else 1


def _build_doctor_report(
    *,
    contract_path: Path,
    contract,
    expose,
    logger: logging.Logger,
):
    from fluid_build.output_ports.mcp.drivers import (
        UnsupportedBindingError,
        build_driver,
    )

    expose_id = str(expose.get("exposeId"))
    binding = expose.get("binding") or {}
    semantics = expose.get("semantics") or {}
    has_semantics = bool(
        semantics.get("metrics") or semantics.get("measures") or semantics.get("dimensions")
    )
    report = {
        "status": "ok",
        "exposeId": expose_id,
        "contract": str(contract_path),
        "binding": {
            "platform": binding.get("platform"),
            "format": binding.get("format"),
        },
        "tools": ["describe", "sample"] + (["query"] if has_semantics else []),
        "semanticsPresent": has_semantics,
        "checks": [],
    }
    expose_for_driver = resolve_expose_paths(expose, contract_dir=contract_path.parent)
    try:
        driver = build_driver(expose=expose_for_driver, contract=contract, logger=logger)
        descriptor = driver.descriptor()
        report["checks"].append(
            {"name": "driver_load", "status": "ok", "detail": descriptor.dialect}
        )
        report["binding"]["tableReference"] = descriptor.table_reference
        report["binding"]["dialect"] = descriptor.dialect
    except UnsupportedBindingError as exc:
        report["status"] = "fail"
        report["checks"].append({"name": "driver_load", "status": "fail", "detail": str(exc)})
        return report
    except Exception as exc:  # noqa: BLE001
        report["status"] = "fail"
        report["checks"].append({"name": "driver_load", "status": "fail", "detail": str(exc)})
        return report
    try:
        health = driver.health_check()
        check_status = "ok" if health.get("status") == "ok" else "fail"
        report["checks"].append({"name": "engine_health", "status": check_status, "detail": health})
        if check_status != "ok":
            report["status"] = "fail"
    except Exception as exc:  # noqa: BLE001
        report["status"] = "fail"
        report["checks"].append({"name": "engine_health", "status": "fail", "detail": str(exc)})
    return report


def _print_doctor_report(report, *, expose_id: str) -> None:
    icon = "✅" if report["status"] == "ok" else "❌"
    sys.stdout.write(
        f"{icon} fluid mcp output-port doctor: expose={expose_id!r} "
        f"({report['status'].upper()})\n"
    )
    sys.stdout.write(f"  contract: {report['contract']}\n")
    binding = report["binding"]
    sys.stdout.write(f"  binding:  {binding.get('platform')}/{binding.get('format')}")
    if "tableReference" in binding:
        sys.stdout.write(f" → {binding['tableReference']}")
    sys.stdout.write("\n")
    sys.stdout.write(f"  tools:    {', '.join(report['tools'])}\n")
    if not report.get("semanticsPresent"):
        sys.stdout.write(
            "  note:     no expose.semantics — the 'query' tool is not "
            "advertised. Add measures/metrics to the contract to enable it.\n"
        )
    for check in report["checks"]:
        check_icon = "✓" if check["status"] == "ok" else "✗"
        sys.stdout.write(f"  {check_icon} {check['name']}: ")
        detail = check.get("detail")
        if isinstance(detail, dict):
            short = detail.get("detail") or detail.get("status") or ""
            sys.stdout.write(str(short))
        else:
            sys.stdout.write(str(detail))
        sys.stdout.write("\n")


def _build_policy(args, *, contract_path: Path, expose) -> OutputPortPolicy:
    readable_paths_raw = _csv(getattr(args, "readable_paths", None))
    if readable_paths_raw:
        readable_paths: Tuple[Path, ...] = tuple(
            Path(p).expanduser().resolve() for p in readable_paths_raw
        )
    else:
        readable_paths = (contract_path.parent.resolve(),)
    allow_tools = _csv(getattr(args, "allow_tools", None))
    deny_tools = _csv(getattr(args, "deny_tools", None))
    # CLI agentPolicy overrides — None passes through to the
    # contract value via the factory; an empty CSV resolves to ()
    # (empty tuple), which is the operator saying "deny everything".
    cli_allow_models = _opt_csv_tuple(getattr(args, "allow_models", None))
    cli_deny_models = _opt_csv_tuple(getattr(args, "deny_models", None))
    cli_allow_use_cases = _opt_csv_tuple(getattr(args, "allow_use_cases", None))
    cli_deny_use_cases = _opt_csv_tuple(getattr(args, "deny_use_cases", None))
    return OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        contract_path=contract_path,
        read_only=True,
        allowed_tools=tuple(allow_tools) if allow_tools else None,
        denied_tools=tuple(deny_tools),
        readable_paths=readable_paths,
        allow_free_form_sql=bool(getattr(args, "allow_sql", False)),
        max_sample_rows=int(getattr(args, "max_sample_rows", 100) or 100),
        cli_allowed_models=cli_allow_models,
        cli_denied_models=cli_deny_models,
        cli_allowed_use_cases=cli_allow_use_cases,
        cli_denied_use_cases=cli_deny_use_cases,
    )


def _opt_csv_tuple(value: Optional[str]) -> Optional[Tuple[str, ...]]:
    """Convert CLI CSV (or None) into the optional tuple shape the
    factory expects.

    None means "fall through to contract value"; an empty CSV
    string yields the empty tuple () which is the operator saying
    "deny everything by intent" — both shapes are preserved.
    """
    if value is None:
        return None
    parts = _csv(value)
    return tuple(parts)


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _render_output_port_guide() -> int:
    """Render a friendly walkthrough when an operator runs
    ``fluid mcp output-port`` with no further subcommand."""
    try:
        from fluid_build.cli._subcommand_guide import (
            SubcommandEntry,
            SubcommandGuide,
            render_subcommand_guide,
        )
    except ImportError:  # pragma: no cover
        sys.stdout.write("Usage: fluid mcp output-port serve <contract> --expose-id <id>\n")
        return 0
    entries = [
        SubcommandEntry(
            name="serve",
            description=(
                "Run the consumer-side MCP stdio server bound to one expose. "
                "Use --allow-sql for free-form SQL or --max-sample-rows to "
                "tune the sample cap."
            ),
            example=(
                "fluid mcp output-port serve contract.fluid.yaml " "--expose-id customer_profiles"
            ),
        ),
    ]
    guide = SubcommandGuide(
        command_path="fluid mcp output-port",
        headline=(
            "Serve a FLUID data product expose to MCP consumers — Claude "
            "Code, Cursor, internal copilots, custom agents."
        ),
        entries=entries,
        hint_provider=None,
        quick_start=(
            "fluid mcp output-port serve contract.fluid.yaml "
            "--expose-id <id>  (Phase-1 stdio server)"
        ),
    )
    return render_subcommand_guide(guide)
