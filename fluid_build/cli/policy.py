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

"""``fluid policy {check,compile,apply}`` — unified policy subcommand group.

Umbrella command introduced to close the naming-collision gap between
``policy-check`` (lint the contract for policy violations) and
``policy-apply`` (deploy IAM/GRANT bindings to the warehouse). Before
this change, both verbs lived at the top level with near-identical
hyphenated names — an operator reading ``fluid --help`` could easily
mistake ``policy-check`` for "validate deployed policies" instead of
"lint the contract".

The new subcommand layout mirrors the ``fluid auth {login,status,
logout}`` / ``fluid generate {speed-transformation,schedule,ci,…}``
pattern already used elsewhere in the CLI. A user who types
``fluid policy --help`` discovers all three verbs at once — check,
compile, apply — in the order they appear in the pipeline. kubectl's
``kubectl auth {can-i, reconcile, ...}`` follows the same shape.

**Backward compatibility:** the legacy top-level hyphenated forms
``fluid policy-check``, ``fluid policy-compile``, and
``fluid policy-apply`` continue to work — each is registered via its
own module in ``bootstrap.py`` with a deprecation notice emitted at
run time (see ``_deprecation_warning`` below). The deprecation
window is one release; thereafter the hyphenated forms may be
removed.

Internally this module delegates to the existing per-verb modules
via their ``_add_arguments(parser)`` helper, so there's one source
of truth for each verb's argument surface. Adding an option to
``fluid policy check`` requires changing only ``policy_check.py``.
"""

from __future__ import annotations

import argparse
import logging

from . import policy_apply, policy_check, policy_compile
from .console import cprint

COMMAND = "policy"


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``fluid policy`` umbrella + its three subcommands.

    Positions:

    - ``fluid policy check CONTRACT``      — delegates to policy_check.run
    - ``fluid policy compile CONTRACT``    — delegates to policy_compile.run
    - ``fluid policy apply BINDINGS``      — delegates to policy_apply.run

    Each subcommand's argument surface is pulled from the underlying
    module's ``_add_arguments`` helper so there's exactly one
    definition per option — the legacy ``fluid policy-*`` top-level
    commands share the same helpers, so adding a flag to either
    surface automatically surfaces on the other.
    """
    parser = subparsers.add_parser(
        COMMAND,
        help="Policy operations: check · compile · apply  (pipeline stage 8 + static lint)",
        description=(
            "Unified entry point for all policy-related verbs. "
            "``check`` lints the contract for policy violations "
            "(static, no deployment). ``compile`` transforms the "
            "contract's accessPolicy section into provider-specific "
            "IAM / GRANT bindings (writes bindings.json). ``apply`` "
            "deploys those bindings to the target warehouse — "
            "stage 8 of the 11-stage pipeline.\n\n"
            "Pipeline ordering: apply runs AFTER stage 7 apply "
            "(GRANTs need the target objects) and BEFORE stage 9 "
            "verify (so under-authorised objects surface as a "
            "policy failure, not as a masked build error)."
        ),
    )
    # ``required=False`` so a bare ``fluid policy`` doesn't blow up
    # with the bare-bones argparse "the following arguments are
    # required: SUBCOMMAND" error.  ``_dispatch`` catches the
    # ``policy_cmd is None`` case and renders a Rich-friendly panel
    # listing the verbs.
    sub = parser.add_subparsers(dest="policy_cmd", required=False, metavar="SUBCOMMAND")

    # ── check ───────────────────────────────────────────────────────
    # Static linter — no cloud calls, no state mutation. Safe to run
    # from any branch, any env. Use in pre-commit hooks or as a
    # stage-2 gate alongside ``fluid validate``.
    check_p = sub.add_parser(
        "check",
        help="Lint contract for policy violations (static, no deployment)",
        description=(
            "Validates the contract's accessPolicy / dataClassification / "
            "lifecycle / schema-evolution policies against the FLUID "
            "schema. Exit 0 on pass; exit 1 on violation. No cloud calls."
        ),
    )
    policy_check._add_arguments(check_p)

    # ── compile ─────────────────────────────────────────────────────
    # Transforms the contract's accessPolicy → bindings.json for a
    # specific provider. Pure-function shape: contract in, JSON out;
    # no cloud calls. Part of stage 3 (generate artifacts) but
    # available as a standalone verb.
    compile_p = sub.add_parser(
        "compile",
        help="Compile accessPolicy → provider IAM bindings (writes bindings.json)",
        description=(
            "Reads the contract's accessPolicy section and emits a "
            "provider-specific bindings.json (Snowflake GRANTs, BigQuery "
            "IAM roles, AWS bucket policies + Glue grants). Output "
            "consumed by ``fluid policy apply`` in stage 8 of the pipeline."
        ),
    )
    policy_compile._add_arguments(compile_p)

    # ── apply ───────────────────────────────────────────────────────
    # Stage 8 of the 11-stage pipeline. Dispatches GRANT / IAM
    # bindings to the target warehouse. Destructive by default in
    # ``--mode enforce``; ``--mode check`` is a dry-run preview.
    apply_p = sub.add_parser(
        "apply",
        help="Deploy compiled IAM bindings to the target warehouse (pipeline stage 8)",
        description=(
            "Stage 8 of the 11-stage pipeline. Consumes bindings.json "
            "from ``fluid policy compile`` (or stage 3 generate-"
            "artifacts) and dispatches GRANT / IAM role-binding "
            "statements to the warehouse. ``--mode check`` is a "
            "dry-run preview; ``--mode enforce`` deploys."
        ),
    )
    policy_apply._add_arguments(apply_p)

    parser.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Dispatch ``fluid policy <sub>`` to the appropriate run() function.

    Each sub-parser sets ``args.func`` to its own run function via
    ``_add_arguments``, so this dispatcher is mostly ceremonial —
    argparse has already wired ``args.func`` to ``policy_check.run``
    / ``policy_compile.run`` / ``policy_apply.run`` by the time we
    get here. This function exists because the top-level
    ``fluid policy`` parser itself needs a ``func`` default for the
    dispatcher in ``cli/__init__.py`` to resolve without crashing
    when the user runs ``fluid policy`` with no subcommand.
    """
    sub = getattr(args, "policy_cmd", None)
    if not sub:
        # Bare ``fluid policy`` — render an intuitive guide instead of
        # the old one-line "specify a subcommand" message.
        return _render_policy_guide()
    # The sub-parser's ``_add_arguments`` already set args.func to
    # the right per-verb run function. If it didn't (defensive
    # guard — can happen if a subcommand's module is re-registered
    # incorrectly), surface a clear error rather than silent no-op.
    if not hasattr(args, "func") or args.func is _dispatch:
        cprint(
            f"[policy] subcommand '{sub}' is not wired to a run() handler. "
            "This is a FLUID CLI bug — file an issue.",
            markup=False,
        )
        return 2
    return args.func(args, logger)


def _render_policy_guide() -> int:
    """Render an intuitive guide for ``fluid policy`` with no
    subcommand.  Detects ``contract.fluid.yaml`` in the cwd and
    promotes ``check`` when one is present (the canonical
    starting move for an existing contract).
    """

    from pathlib import Path

    from fluid_build.cli._subcommand_guide import (
        SubcommandEntry,
        SubcommandGuide,
        SubcommandHint,
        render_subcommand_guide,
    )

    entries = [
        SubcommandEntry(
            name="check",
            description=(
                "Lint contract for accessPolicy / dataClassification / "
                "lifecycle / schema-evolution violations.  No cloud calls."
            ),
            example="fluid policy check contract.fluid.yaml",
        ),
        SubcommandEntry(
            name="compile",
            description=(
                "Compile accessPolicy → provider IAM bindings "
                "(Snowflake GRANTs, BigQuery IAM, AWS bucket policy + Glue grants)."
            ),
            example="fluid policy compile contract.fluid.yaml -o bindings.json",
        ),
        SubcommandEntry(
            name="apply",
            description=(
                "Deploy compiled IAM bindings to the warehouse "
                "(stage 8 of the 11-stage pipeline)."
            ),
            example="fluid policy apply bindings.json --mode check",
        ),
    ]

    def _detect_hint() -> "SubcommandHint | None":
        contract_in_cwd = Path.cwd() / "contract.fluid.yaml"
        if contract_in_cwd.is_file():
            return SubcommandHint(
                subcommand="check",
                rationale=(
                    "found contract.fluid.yaml in cwd — start by linting it "
                    "before compiling / applying bindings."
                ),
            )
        return None

    guide = SubcommandGuide(
        command_path="fluid policy",
        headline=(
            "Lint, compile, and deploy access-policy + IAM bindings declared "
            "in a Fluid contract — static check first, then compile, then apply."
        ),
        entries=entries,
        hint_provider=_detect_hint,
        quick_start="fluid policy check contract.fluid.yaml",
    )
    return render_subcommand_guide(guide)
