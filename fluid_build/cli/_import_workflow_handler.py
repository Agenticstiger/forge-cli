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

Every importer, with or without ``--force``, leaves alone a path that is not a regular file (a
symbolic link, a directory, a device): it is looked at with ``lstat`` and never read through or
written through. A file that is written is written whole to a temporary file next to it and
renamed over the target, so a failed write leaves the old file as it was. An existing file is
read only as far as it could equal what would be written.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Optional

import yaml

from fluid_build.cli._errors import FluidUserError, SchemaValidationError
from fluid_build.cli.console import cprint, cprint_literal
from fluid_build.cli.import_workflow import coerce_report, get_importer, importer_names
from fluid_build.cli.import_workflow._pysource import clean_chars
from fluid_build.cli.import_workflow.airbyte import SERVER_URL_ENV as AIRBYTE_SERVER_URL_ENV
from fluid_build.util.atomic_write import atomic_write_bytes
from fluid_build.util.safe_yaml import read_bounded_file

#: The importers that propose products from code (one per output by default).
PROPOSING_TOOLS = ("airflow", "pyspark")
#: The ``--split-by`` values that are about code, and so for those importers only.
CODE_SPLITS = ("table", "job", "dag", "task")
#: File states ``--force`` replaces: a file that differs, and one that cannot be read.
_REPLACEABLE = ("different", "unreadable")


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

    split = getattr(args, "split_by", None)
    if split in CODE_SPLITS and (
        tool is None or (tool in BUILTIN_IMPORTERS and tool not in PROPOSING_TOOLS)
    ):
        raise _flag_refusal(
            f"--split-by {split} applies to `fluid import airflow` and `fluid import pyspark`",
            f"{split} splits by what code writes (a table, a job file, a DAG, a DAG task); "
            + (
                "the directory scan has no such thing."
                if tool is None
                else f"`fluid import {tool}` reads a configuration, not code."
            ),
            "Use --split-by project or folder"
            + (" or group (dbt)" if tool in (None, "dbt") else "")
            + ", or name airflow or pyspark.",
            flag="--split-by",
        )
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


def _require_source(tool: str, source: Optional[str]) -> str:
    if source:
        return source
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


def _importer_for(tool: str):
    importer = get_importer(tool)
    if importer is not None:
        return importer
    known = importer_names()
    raise SchemaValidationError(
        what=f"unknown importer tool: {tool}",
        why=f"`{tool}` is not in the registered importer set.",
        fix=f"Use one of: {' | '.join(known)}.",
        doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
        extras={"tool": tool, "supported": known},
    )


def _importer_options(args, tool: str) -> dict:
    """What the importer is given: the boundary, the conventions and DAGs directory when the
    user named them, the Airbyte endpoint, and the registry flags for the tools that read one."""
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
    return options


def _run_importer(importer, tool: str, source: str, options: dict) -> tuple:
    """``(contracts, report)`` from the importer, or a typed refusal."""
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
        return contracts, coerce_report(report)
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


def _write_planned(planned: list) -> list[Path]:
    """Write every planned file that is not already what it should be; the paths written."""
    written: list[Path] = []
    for path, text, state in planned:
        if state == "unchanged":
            _line(f"= Unchanged {path}")
            continue
        _write_file(path, text, state)
        written.append(path)
        _line(f"✓ Wrote {path}")
    return written


def run_import_from_tool(args, logger: logging.Logger, *, tool: str, source: Optional[str]) -> int:
    source = _require_source(tool, source)
    importer = _importer_for(tool)
    options = _importer_options(args, tool)
    cprint(f"📥 Importing {tool} configuration from {source}…")
    contracts, report = _run_importer(importer, tool, source, options)

    # An importer that proposes contracts for a person to complete (it sets `guards_overwrite`)
    # never replaces a file that differs from what it would write; the others overwrite, as they
    # always have.
    guarded = bool(getattr(importer, "guards_overwrite", False))
    force = bool(getattr(args, "force", False))
    planned = _plan_writes(contracts, getattr(args, "out_path", None), guarded=guarded)
    if getattr(args, "dry_run", False):
        _print_proposal(contracts, planned, force=force)
        _print_report(report)
        _print_dry_run_summary(planned, force=force)
        return 0
    _refuse_overwrites(planned, force=force)

    written = _write_planned(planned)
    _print_report(report)
    if written:
        cprint("")
        target = written[0] if len(written) == 1 else "<each written contract>"
        _line(f"Next: `fluid validate {target}` and review before applying.")
    return 0


def _file_state(path: Path, data: bytes, *, guarded: bool) -> str:
    """What is at ``path``, looked at without following a link:

    * ``new``: nothing is there;
    * ``special``: a link, a directory, a device or anything that is not a regular file: it is
      never read and never written, whatever the flags;
    * ``inaccessible``: the path cannot be looked at (a folder above it that this process may not
      enter, a name in the path that is a file): nobody can say what is there, so it is never
      written, whatever the flags;
    * ``overwrite``: a regular file, for an importer that always replaces it;
    * ``unchanged`` / ``different``: a regular file, for a guarded importer, holding the bytes
      it would write (line endings aside) or other bytes. The file is read as far as it could
      equal ``data``: a bigger one is ``different`` without being read in full;
    * ``unreadable``: a regular file this process may not read: nobody can say whether it
      differs, and ``--force`` replaces it like a file that does.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "new"
    except OSError:
        return "inaccessible"
    if not stat.S_ISREG(info.st_mode):
        return "special"
    if not guarded:
        return "overwrite"
    limit = 2 * len(data) + 1  # room for a copy whose line endings were converted
    if info.st_size > limit:
        return "different"
    try:
        existing = read_bounded_file(path, limit)
    except PermissionError:
        return "unreadable"
    except OSError:
        return "special"
    if existing is None:
        return "different"
    return "unchanged" if existing.replace(b"\r\n", b"\n") == data else "different"


def _plan_writes(contracts: list, out_arg: Optional[str], *, guarded: bool = True) -> list:
    """``[(path, yaml text, state)]``: where each contract goes, and what is there now (see
    :func:`_file_state`).

    One contract: ``--out`` names the file. Several: ``--out`` names the directory.
    """
    if not contracts:
        return []
    paths: list[Path] = []
    if len(contracts) == 1:
        paths.append(Path(out_arg or _default_out_path(contracts[0])))
    else:
        out_dir = Path(out_arg) if out_arg else Path(".")
        if out_arg and os.path.lexists(out_dir) and not out_dir.is_dir():
            raise _flag_refusal(
                f"--out names a file, but this import writes {len(contracts)} files",
                f"With several products --out is the directory they are written to, and "
                f"{clean_chars(out_arg)} is an existing file.",
                "Pass a directory as --out (it is created when it is missing).",
                out=clean_chars(out_arg),
            )
        paths.extend(out_dir / _default_out_path(contract) for contract in contracts)
    planned = []
    for path, contract in zip(paths, contracts, strict=True):
        text = yaml.safe_dump(contract, sort_keys=False)
        planned.append((path, text, _file_state(path, text.encode("utf-8"), guarded=guarded)))
    return planned


def _write_file(path: Path, text: str, state: str) -> None:
    """Write ``text`` to ``path``: whole, to a temporary file beside it, renamed over the target.
    A file that is replaced keeps its permission bits; a new one gets what the umask allows."""
    try:
        replaces = state in ("overwrite", *_REPLACEABLE)
        if replaces:
            # the permission bits, not setuid, setgid or sticky, and readable by its owner
            mode = (stat.S_IMODE(os.lstat(path).st_mode) & 0o777) | 0o400
        else:
            current = os.umask(0)
            os.umask(current)
            mode = 0o666 & ~current
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, text.encode("utf-8"), mode=mode)
        if replaces:
            os.chmod(path, mode)  # the umask had narrowed the mode the temporary file was made with
    except OSError as exc:
        raise SchemaValidationError(
            what=f"cannot write {clean_chars(str(path))}",
            why=f"{type(exc).__name__}: {exc.strerror or exc}",
            fix="Check that the directory can be written to, or choose another --out.",
            doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
            extras={"file": clean_chars(str(path))},
        ) from exc


def _shown(paths, limit: int = 5) -> str:
    names = [clean_chars(str(p)) for p in paths]
    return ", ".join(names[:limit]) + (
        f", and {len(names) - limit} more" if len(names) > limit else ""
    )


def _refuse_overwrites(planned: list, *, force: bool) -> None:
    blind = [path for path, _text, state in planned if state == "inaccessible"]
    if blind:
        raise SchemaValidationError(
            what=f"cannot look at {len(blind)} path(s) this import would write",
            why=(
                f"{_shown(blind)}: a folder above it cannot be entered, or a name in the path is "
                "a file, so nobody can say what is there. Nothing was written, with or without "
                "--force."
            ),
            fix="Fix the permissions of the folder, or choose another --out.",
            doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
            extras={"files": [clean_chars(str(p)) for p in blind]},
        )
    special = [path for path, _text, state in planned if state == "special"]
    if special:
        raise SchemaValidationError(
            what=f"refusing to write over {len(special)} path(s) that are not regular files",
            why=(
                f"{_shown(special)}: a symbolic link, a directory or a device is never written "
                "through or replaced, with or without --force. Nothing was written."
            ),
            fix="Remove it yourself, or choose another --out.",
            doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
            extras={"files": [clean_chars(str(p)) for p in special]},
        )
    clash = [path for path, _text, state in planned if state in _REPLACEABLE]
    if not clash or force:
        return
    raise SchemaValidationError(
        what=f"refusing to overwrite {len(clash)} existing file(s) that differ from this import",
        why=(
            f"{_shown(clash)}: a contract that was completed by hand is not replaced by a second "
            "import, and nothing was written."
        ),
        fix=(
            "Write to a new directory with --out, or pass --force to overwrite them. "
            "`--dry-run` shows what would be written."
        ),
        doc="https://agenticstiger.github.io/forge_docs/cli/import.html",
        extras={"files": [clean_chars(str(p)) for p in clash]},
    )


def _line(text: str) -> None:
    """One line of what the import found, printed as data: no markup, no control or
    bidirectional characters, and never wrapped by the console (a long path stays one line)."""
    cprint_literal(clean_chars(text), soft_wrap=True)


def _state_label(state: str, force: bool) -> str:
    if state in _REPLACEABLE:
        if force:
            return "replaces the existing file (--force)"
        if state == "unreadable":
            return "cannot be read: refused without --force"
        return "differs from the existing file: refused without --force"
    return {
        "new": "new",
        "unchanged": "unchanged",
        "overwrite": "replaces the existing file",
        "special": "refused: not a regular file",
        "inaccessible": "refused: cannot be looked at",
    }.get(state, state)


def _print_proposal(contracts: list, planned: list, *, force: bool = False) -> None:
    """What the import would write, one product per block."""
    cprint(f"\n  Proposal ({len(contracts)} product(s)):")
    for (path, _text, state), contract in zip(planned, contracts, strict=True):
        owner = (contract.get("metadata") or {}).get("owner") or {}
        who = owner.get("team") or owner.get("email") or "-"
        _line(f"    {contract.get('id')}  (owner {who}) → {path} [{_state_label(state, force)}]")
        for build in contract.get("builds") or []:
            model = (build.get("properties") or {}).get("model")
            if model:
                _line(f"        build {build.get('id')}: {model}")
        exposes = ", ".join(
            f"{e.get('exposeId')} ({e.get('kind')})" for e in contract.get("exposes") or []
        )
        if exposes:
            _line(f"        exposes: {exposes}")
        consumes = ", ".join(
            f"{c.get('productId')}.{c.get('exposeId')}" for c in contract.get("consumes") or []
        )
        if consumes:
            _line(f"        consumes: {consumes}")


def _print_dry_run_summary(planned: list, *, force: bool) -> None:
    states = [state for _path, _text, state in planned]
    replaceable = sum(1 for s in states if s in _REPLACEABLE)
    writes = sum(1 for s in states if s in ("new", "overwrite")) + (replaceable if force else 0)
    same = states.count("unchanged")
    refused = states.count("special") + states.count("inaccessible")
    refused += 0 if force else replaceable
    parts = [f"{writes} file(s) would be written"]
    if same:
        parts.append(f"{same} unchanged")
    if refused:
        parts.append(f"{refused} refused (see the labels above)")
    cprint("")
    _line(f"Dry run: nothing was written. {', '.join(parts)}.")


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
            _line(f"    • {x}")
    if report.required_defaults:
        cprint(f"\n  Used defaults ({len(report.required_defaults)}):")
        for x in report.required_defaults:
            _line(f"    • {x}")
    if report.unsupported:
        cprint(f"\n  Unsupported (must be re-authored, {len(report.unsupported)}):")
        for x in report.unsupported:
            _line(f"    ⚠ {x}")
    if report.notes:
        cprint("\n  Notes:")
        for x in report.notes:
            _line(f"    • {x}")
