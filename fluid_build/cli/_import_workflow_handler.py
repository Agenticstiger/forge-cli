# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Handler for ``fluid import <tool> <source>``.

Dispatches to the matching importer in :mod:`fluid_build.cli.import_workflow` (a built-in, or a
plugin of the ``fluid_build.importers`` group), writes the resulting contract YAML to disk, and
prints the translation report so the user knows exactly what mapped 1:1, what got defaulted, and
what's unsupported.

``--dry-run`` prints the proposal and the report and writes nothing. An importer that proposes
contracts for a person to complete (``airflow``, ``pyspark``; a plugin sets ``guards_overwrite``)
never replaces a file that already exists and differs from what it would write, unless ``--force``
is given: a contract completed by hand is not lost to a second import. The other importers
overwrite, as they always have.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

from fluid_build.cli._errors import FluidUserError, SchemaValidationError
from fluid_build.cli.console import cprint, cprint_literal
from fluid_build.cli.import_workflow import coerce_report, get_importer, importer_names
from fluid_build.cli.import_workflow.airbyte import SERVER_URL_ENV as AIRBYTE_SERVER_URL_ENV

#: The importers that propose products from code (one per output by default).
PROPOSING_TOOLS = ("airflow", "pyspark")


def _flag_refusal(what: str, why: str, fix: str, **extras) -> Exception:
    return SchemaValidationError(
        what=what,
        why=why,
        fix=fix,
        doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
        extras=extras,
    )


def check_import_flags(args, tool: Optional[str]) -> None:
    """Refuse the flags of ``fluid import`` that do not apply to ``tool``.

    ``--conventions`` and ``--dags`` belong to the importers that read code (and to an importer
    plugin, which receives them); ``--dry-run`` and ``--force`` belong to ``fluid import <tool>``
    and mean nothing to the legacy directory scan.
    """
    from fluid_build.cli.import_workflow import BUILTIN_IMPORTERS

    if tool is None:
        for dest, flag in (
            ("conventions", "--conventions"),
            ("dags", "--dags"),
            ("dry_run", "--dry-run"),
            ("force", "--force"),
        ):
            if getattr(args, dest, None):
                raise _flag_refusal(
                    f"{flag} applies to `fluid import <tool> <source>`",
                    "The directory scan (`fluid import` with no tool) asks before it writes, "
                    "and has no conventions or DAGs.",
                    f"Remove {flag}, or name an importer: "
                    "`fluid import pyspark <jobs-dir> --dry-run`.",
                    flag=flag,
                )
        return
    if tool in BUILTIN_IMPORTERS and tool not in PROPOSING_TOOLS:
        for dest, flag in (("conventions", "--conventions"), ("dags", "--dags")):
            if getattr(args, dest, None) is not None:
                raise _flag_refusal(
                    f"{flag} does not apply to `fluid import {tool}`",
                    f"{flag} belongs to the importers that read code (airflow, pyspark).",
                    f"Remove {flag}.",
                    flag=flag,
                )
    if tool == "airflow" and getattr(args, "dags", None) is not None:
        raise _flag_refusal(
            "--dags does not apply to `fluid import airflow`",
            "`fluid import airflow` reads the DAGs directory it is given as its source.",
            "Remove --dags, or use `fluid import pyspark <jobs-dir> --dags <dags-dir>`.",
            flag="--dags",
        )


def run_import_from_tool(args, logger: logging.Logger, *, tool: str, source: Optional[str]) -> int:
    if not source:
        raise SchemaValidationError(
            what=f"`fluid import {tool}` requires a positional source argument",
            why=(
                f"The {tool} importer needs to know what to convert "
                f"(project dir / workspace id / pipeline name / tap config / "
                f"dbt manifest)."
            ),
            fix=(
                f"`fluid import {tool} <project-dir | workspace-id | "
                f"pipeline-name | tap-config.json | manifest.json>`"
            ),
            doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
            extras={"tool": tool},
        )

    importer = get_importer(tool)
    if importer is None:
        known = importer_names()
        raise SchemaValidationError(
            what=f"unknown importer tool: {tool}",
            why=f"`{tool}` is not in the registered importer set.",
            fix=f"Use one of: {' | '.join(known)}.",
            doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
            extras={"tool": tool, "supported": known},
        )

    # The boundary: the importers that propose products choose their own default (one product
    # per output); every other tool keeps `project`, the default it always had.
    chosen_split = getattr(args, "split_by", None)
    options = {"split_by": chosen_split if tool in PROPOSING_TOOLS else chosen_split or "project"}
    for dest in ("conventions", "dags"):
        if getattr(args, dest, None) is not None:
            options[dest] = getattr(args, dest)
    # Endpoint precedence for `fluid import airbyte`: the flag first, then the
    # environment. Resolved in the CLI layer because that is where both sources
    # live. The importer keeps a refusal of its own for callers that never come
    # through here, so neither layer can be the only thing standing between an
    # unset setting and a socket.
    server_url = getattr(args, "server_url", None) or os.environ.get(AIRBYTE_SERVER_URL_ENV)
    if server_url:
        options["server_url"] = server_url
    if tool == "schema-registry" or tool in PROPOSING_TOOLS:
        from fluid_build.cli.import_workflow.schema_registry import REGISTRY_FLAGS

        for dest in REGISTRY_FLAGS:
            # `--version` pins one subject's version: it means nothing for many topics.
            if dest == "registry_version" and tool != "schema-registry":
                continue
            if getattr(args, dest, None) is not None:
                options[dest] = getattr(args, dest)

    cprint(f"📥 Importing {tool} configuration from {source}…")
    try:
        # Split-capable importers (dbt) expose a plural API; the rest keep the
        # single-contract Protocol shape.
        if callable(getattr(importer, "import_to_contracts", None)):
            contracts, report = importer.import_to_contracts(source, options=options)
        else:
            contract, report = importer.import_to_contract(source, options=options)
            contracts = [contract] if contract else []
        # A plugin's own shapes: the contracts are a list of mappings, and the report is
        # any object with the report's four lists.
        if not isinstance(contracts, list) or not all(isinstance(c, dict) for c in contracts):
            raise TypeError("an importer returns a contract mapping, or a list of them")
        report = coerce_report(report)
    except FluidUserError:
        # Already a typed refusal in the house shape, and its `fix` is the
        # specific one — "set --server-url or FLUID_IMPORT_AIRBYTE_URL", say.
        # Re-wrapping would overwrite that with the generic "check the source
        # path" line below, sending the reader to inspect a workspace id that
        # was never the problem.
        raise
    except Exception as exc:  # noqa: BLE001
        raise SchemaValidationError(
            what=f"{tool} import failed for {source}",
            why=str(exc),
            fix=(
                "Check the source path/identifier and that the foreign tool's "
                "config is well-formed."
            ),
            doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
            extras={"tool": tool, "source": source},
        ) from exc

    # An importer that proposes contracts for a person to complete (it sets `guards_overwrite`)
    # never replaces a file that differs from what it would write; the others overwrite, as they
    # always have.
    guarded = bool(getattr(importer, "guards_overwrite", False))
    planned = _plan_writes(contracts, getattr(args, "out_path", None), guarded=guarded)
    if getattr(args, "dry_run", False):
        _print_proposal(contracts, planned)
        _print_report(report)
        cprint(
            f"\nDry run: nothing was written. Run again without --dry-run to write "
            f"{len(planned)} file(s)."
        )
        return 0
    _refuse_overwrites(planned, force=bool(getattr(args, "force", False)))

    written: list[Path] = []
    for path, text, state in planned:
        if state == "unchanged":
            cprint(f"= Unchanged {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
        cprint(f"✓ Wrote {path}")
    _print_report(report)
    if written:
        cprint(
            "\nNext: `fluid validate {0}` and review before applying.".format(
                written[0] if len(written) == 1 else "<each written contract>"
            )
        )
    return 0


def _plan_writes(contracts: list, out_arg: Optional[str], *, guarded: bool = True) -> list:
    """``[(path, yaml text, state)]``: where each contract goes, and whether the file is
    ``new``, ``unchanged`` (the same text) or ``different`` (a file is there with other text).
    Without ``guarded`` every file is ``new``: it is written over whatever is there.

    One contract: ``--out`` names the file. Several: ``--out`` names the directory.
    """
    paths: list[Path] = []
    if len(contracts) == 1:
        paths.append(Path(out_arg or _default_out_path(contracts[0])))
    else:
        out_dir = Path(out_arg) if out_arg else Path(".")
        paths.extend(out_dir / _default_out_path(contract) for contract in contracts)
    planned = []
    for path, contract in zip(paths, contracts, strict=True):
        text = yaml.safe_dump(contract, sort_keys=False)
        state = "new"
        if guarded and path.exists():
            try:
                state = "unchanged" if path.read_text(encoding="utf-8") == text else "different"
            except (OSError, UnicodeDecodeError):
                state = "different"
        planned.append((path, text, state))
    return planned


def _refuse_overwrites(planned: list, *, force: bool) -> None:
    clash = [path for path, _text, state in planned if state == "different"]
    if not clash or force:
        return
    shown = ", ".join(str(p) for p in clash[:5]) + (
        f", and {len(clash) - 5} more" if len(clash) > 5 else ""
    )
    raise SchemaValidationError(
        what=f"refusing to overwrite {len(clash)} existing file(s) that differ from this import",
        why=(
            f"{shown}: a contract that was completed by hand is not replaced by a second import, "
            "and nothing was written."
        ),
        fix=(
            "Write to a new directory with --out, or pass --force to overwrite them. "
            "`--dry-run` shows what would be written."
        ),
        doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
        extras={"files": [str(p) for p in clash]},
    )


def _print_proposal(contracts: list, planned: list) -> None:
    """What the import would write, one product per block."""
    cprint(f"\n  Proposal ({len(contracts)} product(s)):")
    for (path, _text, state), contract in zip(planned, contracts, strict=True):
        owner = (contract.get("metadata") or {}).get("owner") or {}
        who = owner.get("team") or owner.get("email") or "-"
        cprint_literal(f"    {contract.get('id')}  (owner {who}) → {path} [{state}]")
        for build in contract.get("builds") or []:
            model = (build.get("properties") or {}).get("model")
            if model:
                cprint_literal(f"        build {build.get('id')}: {model}")
        exposes = ", ".join(
            f"{e.get('exposeId')} ({e.get('kind')})" for e in contract.get("exposes") or []
        )
        if exposes:
            cprint_literal(f"        exposes: {exposes}")
        consumes = ", ".join(
            f"{c.get('productId')}.{c.get('exposeId')}" for c in contract.get("consumes") or []
        )
        if consumes:
            cprint_literal(f"        consumes: {consumes}")


def _default_out_path(contract: dict) -> str:
    cid = (contract.get("id") or "imported").replace("/", "_")
    return f"contract.{cid}.fluid.yaml"


def _print_report(report) -> None:
    # Each item quotes the imported project (model, entity and expose names, as in
    # ``exposes[orders].semantics``), so it prints literally: Rich markup would
    # swallow the brackets.
    if report.mapped_one_to_one:
        cprint(f"\n  Mapped 1:1 ({len(report.mapped_one_to_one)}):")
        for x in report.mapped_one_to_one:
            cprint_literal(f"    • {x}")
    if report.required_defaults:
        cprint(f"\n  Used defaults ({len(report.required_defaults)}):")
        for x in report.required_defaults:
            cprint_literal(f"    • {x}")
    if report.unsupported:
        cprint(f"\n  Unsupported (must be re-authored, {len(report.unsupported)}):")
        for x in report.unsupported:
            cprint_literal(f"    ⚠ {x}")
    if report.notes:
        cprint("\n  Notes:")
        for x in report.notes:
            cprint_literal(f"    – {x}")
