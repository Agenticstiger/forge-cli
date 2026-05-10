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

"""``fluid validate-artifacts`` — pipeline stage 4.

Reads the directory produced by stage 3 (``fluid generate artifacts``),
re-verifies every file's SHA-256 against MANIFEST.json, then dispatches
per-format validators (ODCS v3.1.0, ODPS-Bitol v1.0.0, py_compile for
DAGs, key-check for bindings). Optional hooks: OPA conftest on the
bindings, ``dbt parse`` on any emitted dbt project.

Registered as a top-level command (not a subcommand of ``validate``) to
avoid argparse clash with ``validate <contract>``. CLI surface:

    fluid validate-artifacts dist/artifacts/ [--report R.json] [--strict]
                             [--fail-fast] [--opa-policy-dir tests/policies/]
                             [--manifest dist/artifacts/MANIFEST.json]
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from fluid_build.cli._common import CLIError
from fluid_build.cli.console import cprint
from fluid_build.observability.tracing import traced_stage as _traced_stage

COMMAND = "validate-artifacts"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="Re-verify MANIFEST + per-format validators on stage-3 output",
        description=(
            "Stage-4 of the 11-stage pipeline. Reads the directory produced by "
            "``fluid generate artifacts``, re-verifies every file's SHA-256 "
            "against MANIFEST.json (tamper gate), then dispatches per-format "
            "validators: ODCS against vendored v3.1.0 schema, ODPS-Bitol "
            "against vendored v1.0.0 schema, ``python -m py_compile`` for DAG "
            "files, key-check for policy bindings. Optional: OPA conftest "
            "against ``tests/policies/*.rego`` (if present) and ``dbt parse`` "
            "against ``<artifacts>/dbt/`` (if present)."
        ),
        epilog=(
            "Examples:\n"
            "  fluid validate-artifacts dist/artifacts/\n"
            "  fluid validate-artifacts dist/artifacts/ --report /tmp/va.json --strict\n"
            "  fluid validate-artifacts dist/artifacts/ --opa-policy-dir tests/opa/\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "artifacts_dir",
        help="Directory containing stage-3 output (MANIFEST.json + subdirs)",
    )
    p.add_argument(
        "--manifest",
        default=None,
        help="Path to MANIFEST.json. Default: <artifacts_dir>/MANIFEST.json",
    )
    p.add_argument(
        "--opa-policy-dir",
        default="tests/policies",
        help=(
            "Directory containing ``*.rego`` files for OPA conftest. Default: "
            "tests/policies. Skipped silently if the dir is absent or empty. "
            "Missing conftest binary: INFO (non-strict) or ERROR (--strict)."
        ),
    )
    p.add_argument(
        "--report",
        metavar="PATH",
        default=None,
        help=(
            "Write a structured JSON report to PATH. Same shape as "
            "``fluid validate <tgz> --report`` (bundleDigest, input, strict, "
            "status, summary, issues[])."
        ),
    )
    p.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help=(
            "Treat warnings as errors in the final status; hard-fail when "
            "optional tools (jsonschema, conftest, dbt) are absent."
        ),
    )
    p.add_argument(
        "--fail-fast",
        action="store_true",
        default=False,
        help="Stop at the first error-severity issue instead of collect-all.",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Print every issue, not just the summary",
    )
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=False,
        help="Suppress status output; rely on exit code and --report",
    )
    p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="stdout format (text = human summary, json = full report)",
    )

    p.set_defaults(cmd=COMMAND, func=run)


@_traced_stage("validate_artifacts")
def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.forge.core.artifact_validators import validate_artifacts

    artifacts_dir = Path(args.artifacts_dir)
    if not artifacts_dir.exists():
        raise CLIError(2, "validate_artifacts_input_missing", {"path": str(artifacts_dir)})
    if not artifacts_dir.is_dir():
        raise CLIError(
            2,
            "validate_artifacts_not_a_directory",
            {"path": str(artifacts_dir)},
        )

    manifest_path = Path(args.manifest) if args.manifest else None
    opa_dir = Path(args.opa_policy_dir) if args.opa_policy_dir else None

    try:
        report = validate_artifacts(
            artifacts_dir,
            manifest_path=manifest_path,
            opa_policy_dir=opa_dir,
            strict=bool(args.strict),
            fail_fast=bool(getattr(args, "fail_fast", False)),
        )
    except Exception as exc:
        raise CLIError(
            2,
            "validate_artifacts_failed",
            {"path": str(artifacts_dir), "error": str(exc)},
        )

    # stdout presentation
    out_format = getattr(args, "format", "text")
    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))

    if out_format == "json":
        cprint(json.dumps(report.to_dict(), indent=2))
    elif not quiet:
        icon = "✅" if report.status == "pass" else "❌"
        cprint(f"{icon} Artifacts {report.status}: {artifacts_dir}")
        cprint(f"   digest: {report.bundle_digest}")
        s = report.summary
        cprint(
            f"   issues: {s['total']} total "
            f"({s.get('error', 0)} error, {s.get('warning', 0)} warning, "
            f"{s.get('info', 0)} info)"
        )
        if verbose or report.status == "fail":
            for issue in report.issues:
                loc = ""
                if issue.line is not None:
                    loc = f" (L{issue.line}"
                    if issue.column is not None:
                        loc += f":C{issue.column}"
                    loc += ")"
                cprint(
                    f"   [{issue.severity}] {issue.validator}: {issue.file}{loc}: {issue.message}"
                )

    # Structured report file (atomic write — matches stage-2 pattern).
    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        tmp = rp.with_suffix(rp.suffix + ".tmp")
        tmp.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(rp)
        if verbose and not quiet:
            cprint(f"   report written: {rp}")

    return 0 if report.status == "pass" else 1
