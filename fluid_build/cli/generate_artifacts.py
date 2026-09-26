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

"""``fluid generate artifacts`` — pipeline stage 3.

Fanout wrapper that takes a stage-1 bundle (.tgz) and emits catalog-ready
artifacts (ODCS, ODPS-Bitol, OPDS v4.1 LF/ODPI, schedule DAGs, policy
bindings) into a single directory with a unified MANIFEST.json. ``opds`` is
the canonical LF/ODPI Open Data Product Specification key; the bare ``odps``
emit key is a deprecated alias that resolves to ``opds``. Delegates all
emission to existing per-format commands; orchestration lives in
``fluid_build.forge.core.artifact_fanout``.

Registered as a subcommand of ``fluid generate``:

    fluid generate artifacts <bundle.tgz> [--out dir] [--emit csv] [--manifest path]
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from fluid_build.cli._common import CLIError
from fluid_build.cli.console import cprint
from fluid_build.observability.tracing import traced_stage as _traced_stage


def register_subcommand(subparsers: argparse._SubParsersAction) -> None:
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "artifacts",
        help="Fanout bundle → catalog artifacts (ODCS, OPDS LF/ODPI, ODPS-Bitol, schedule, policies)",
        description=(
            "Stage-3 of the 11-stage pipeline. Reads a Phase-2 bundle and emits "
            "ODCS per-port, ODPS-Bitol, OPDS v4.1 (LF/ODPI), schedule DAGs, and "
            "compiled policy bindings into <out>/, with a unified MANIFEST.json "
            "hashed over every emitted file. ``opds`` is the LF/ODPI Open Data "
            "Product Specification key; the bare ``odps`` emit key is a deprecated "
            "alias that resolves to ``opds``."
        ),
        epilog=(
            "Examples:\n"
            "  fluid generate artifacts dist/product.fluid.bundle.tgz \\\n"
            "      --out dist/artifacts/\n"
            "  fluid generate artifacts bundle.tgz --emit odps-bitol,odcs\n"
            "  fluid generate artifacts contract.fluid.yaml --out /tmp/art  # dev shortcut\n"
            "  # schedule DAGs that run `fluid apply --env aws` on the Airflow worker\n"
            "  fluid generate artifacts runtime/bundle.tgz --env aws \\\n"
            "      --contract-path contracts/orders/contract.fluid.yaml\n\n"
            "Note: --emit dbt is NOT supported. dbt projects are execution artifacts;\n"
            "use `fluid generate speed-transformation` instead.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "bundle",
        help=(
            "Path to a Phase-2 bundle (.tgz) OR a raw resolved contract "
            "(.yaml/.yml). Bundle input is the CI path (MANIFEST re-verified); "
            "contract input is a local-dev shortcut."
        ),
    )
    p.add_argument(
        "--out",
        default="dist/artifacts",
        help="Output directory for emitted artifacts. Default: dist/artifacts",
    )
    p.add_argument(
        "--emit",
        default=None,
        help=(
            "Comma-separated emit selector. Valid: opds, odps-bitol, odcs, schedule, "
            "policies. ``odps`` is accepted as a deprecated alias of ``opds`` (the "
            "LF/ODPI Open Data Product Specification v4.1 target). Default: all five. "
            "``dbt`` is NOT a valid emit key — dbt projects are execution artifacts "
            "(see `fluid generate speed-transformation`)."
        ),
    )
    p.add_argument(
        "--manifest",
        default=None,
        help=(
            "Path to write MANIFEST.json. Default: <out>/MANIFEST.json. "
            "The manifest carries SHA-256 per emitted file plus a merkle root "
            "that stage-4 ``fluid validate artifacts`` re-verifies."
        ),
    )
    p.add_argument(
        "--env",
        default=None,
        help=(
            "Environment the artifacts are for. A bundle must have been built for "
            "it (``fluid bundle --env <env>``); a bundle built for another env is "
            "refused, because a bundle is never re-overlaid. A raw contract gets the "
            "overlay applied, exactly as ``fluid bundle --env <env>`` would. Every "
            "schedule DAG runs ``fluid apply --env <env>``; for the DAGs the default "
            "is $FLUID_ENV, the variable the generated pipelines apply with; when "
            "that is unset too, no --env (with a warning if the contract has "
            "overlays). An empty --env '' means no --env."
        ),
    )
    p.add_argument(
        "--contract-path",
        default=None,
        help=(
            "The contract's path relative to the project directory, which schedule "
            "DAGs append to $FLUID_PROJECT_DIR on the Airflow worker. Default: the "
            "input's path relative to the current directory for a raw contract; "
            "contract.fluid.yaml (with a warning) for a bundle."
        ),
    )
    p.set_defaults(generate_sub="artifacts", func=_run_from_generate)


def _fanout_input_for_env(
    input_path: Path, env: Optional[str], tmpdir: Path, logger: logging.Logger
) -> Path:
    """The file ``run_fanout`` should read so the artifacts describe ``env``.

    * No ``env``: ``input_path`` unchanged (historical behaviour).
    * A bundle: unchanged, after :func:`check_bundle_env` proves it was built
      for ``env``. The bundle already carries the overlay-applied contract.
    * A raw contract: the contract with its ``env`` overlay applied, written
      as a deterministic bundle under ``tmpdir``, so stage 3 on a contract
      sees the same merged document ``fluid bundle --env`` would have frozen
      (the policy bindings of a cloud overlay, for one, only exist there).
    """
    if not env:
        return input_path
    from fluid_build._contract_loader import (
        _is_bundle_path,
        check_bundle_env,
        load_contract_with_overlay,
    )

    if _is_bundle_path(str(input_path)):
        check_bundle_env(str(input_path), env, logger)
        return input_path
    from fluid_build.forge.core.bundle import build_bundle_tgz

    try:
        merged = load_contract_with_overlay(str(input_path), env, logger)
    except CLIError:
        raise
    except Exception as exc:  # noqa: BLE001 — loader raises several types
        raise CLIError(
            1, "contract_load_failed", {"path": str(input_path), "env": env, "error": str(exc)}
        )
    materialised = tmpdir / "contract.bundle.tgz"
    build_bundle_tgz(merged, materialised, contract_id=str(merged.get("id") or ""))
    return materialised


def _run_from_generate(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate artifacts``."""
    return run(args, logger)


@_traced_stage("generate_artifacts")
def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    from fluid_build.forge.core.artifact_fanout import FanoutError, run_fanout

    bundle_path = Path(args.bundle)
    out_dir = Path(args.out)
    manifest_path = Path(args.manifest) if args.manifest else None

    if not bundle_path.exists():
        raise CLIError(2, "generate_artifacts_input_missing", {"path": str(bundle_path)})

    env = getattr(args, "env", None)
    try:
        with tempfile.TemporaryDirectory(prefix="fluid-artifacts-env-") as tmpdir:
            fanout_input = _fanout_input_for_env(bundle_path, env, Path(tmpdir), logger)
            manifest = run_fanout(
                fanout_input,
                out_dir,
                emit_raw=args.emit,
                manifest_path=manifest_path,
                logger=logger,
                env=_schedule_env(args, logger),
                contract_path=_schedule_contract_path(args, bundle_path, fanout_input),
            )
    except FanoutError as exc:
        # Surface emit-key context so the operator knows which generator failed.
        meta = {"error": str(exc)}
        if exc.key:
            meta["emit_key"] = exc.key
        raise CLIError(1, "generate_artifacts_failed", meta)

    if env:
        cprint(f"   env: {env}")
    cprint(f"✅ Artifacts written to {out_dir}")
    cprint(f"   MANIFEST digest: {manifest['digest']}")
    cprint(f"   files: {len(manifest.get('files', {}))}")
    return 0


def _schedule_contract_path(
    args: argparse.Namespace, input_path: Path, fanout_input: Path
) -> Optional[str]:
    """The ``--contract-path`` for schedule DAGs: the flag, else ``None``
    (``run_fanout`` defaults it from its input), except that a raw contract
    fanned out through a temporary ``--env`` bundle keeps its own
    project-relative path; the temporary bundle records no location.
    """
    explicit = getattr(args, "contract_path", None)
    if explicit is not None or fanout_input == input_path:
        return explicit
    from fluid_build.forge.core.artifact_fanout import project_relative_path

    return project_relative_path(input_path)


def _schedule_env(args: argparse.Namespace, logger: logging.Logger) -> Optional[str]:
    """The ``--env`` baked into schedule DAGs: the flag, else ``$FLUID_ENV``.

    The generated pipelines apply with ``--env "${FLUID_ENV:-dev}"`` (stage
    7), so reading the same variable keeps a scheduled run on the target the
    pipeline applied to when stage 3 is not given ``--env``. An empty
    ``FLUID_ENV`` counts as unset, as it does in ``${FLUID_ENV:-dev}``. An
    empty ``--env ''`` is returned as ``""``: no ``--env``, on purpose, so the
    fanout does not warn about it.
    """
    explicit = getattr(args, "env", None)
    if explicit is not None:
        return str(explicit)
    from_env = os.environ.get("FLUID_ENV") or None
    if from_env is not None:
        logger.info("generate_artifacts_schedule_env_from_fluid_env", extra={"env": from_env})
    return from_env
