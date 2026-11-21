# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Expand contract arguments (files, glob patterns, folders) into the contract files they name.

One expansion for every command that takes several contracts (``fluid genie apply``,
``fluid datamesh-manager publish``), so a folder means the same thing everywhere: searched at
any depth for :data:`CONTRACT_SUFFIXES`, past no symlink and into no ignored or build-output
folder.

Light at import: stdlib only. The CLI-error and path-validator imports happen when a command
runs, so the ``fluid --help`` cold path never loads them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence, Tuple

__all__ = ["CONTRACT_SUFFIXES", "contract_paths"]

#: The file names a folder argument is searched for.
CONTRACT_SUFFIXES = (".fluid.yaml", ".fluid.yml", ".fluid.json")
_GLOB_CHARS = frozenset("*?[")


def contract_paths(items: Sequence[str], *, empty_event: str) -> List[Tuple[str, Path]]:
    """``(label, path)`` per contract: each item a file, a glob pattern (expanded as ``fluid
    publish`` expands one, ``**`` a single level) or a folder searched for
    :data:`CONTRACT_SUFFIXES` at any depth, in that order, each file once. A folder or
    a glob reaches no symlink and no ignored or build-output folder (``.git``, ``node_modules``,
    ``.venv``, ``target``, ...) below where it starts; a file named outright is read as named.
    Every path passes the CLI's path validator (``cli/security.py``) before it is read.

    A folder that holds no contract raises ``CLIError(1, empty_event, ...)``, so each command
    keeps its own stable error slug.
    """
    from fluid_build.cli._common import CLIError
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.util.upstream_discovery import BUILD_OUTPUT_DIRS, IGNORED_DIRS

    skip = IGNORED_DIRS | BUILD_OUTPUT_DIRS
    found: List[Path] = []
    for item in items:
        for match in _matches(item, skip):
            found += (
                _folder_contracts(match, skip, empty_event)
                if Path(match).is_dir()
                else [Path(match)]
            )
    missing = [str(p) for p in found if not p.is_file()]
    if missing:
        raise CLIError(1, "contract_file_not_found", {"path": ", ".join(missing)})
    unique: Dict[Path, Path] = {}
    for path in found:
        unique.setdefault(path.resolve(), path)
    return [
        (str(given), validate_cli_path(given, mode="read", file_type="contract"))
        for given in unique.values()
    ]


def _matches(item: str, skip: frozenset) -> List[str]:
    """What one argument names: itself, or what its glob matches that a walk would reach."""
    from glob import glob

    from fluid_build.cli._common import CLIError

    if _GLOB_CHARS & set(item):
        base = _glob_base(item)
        # As ``fluid publish`` expands one: ``**`` is one folder level, never a walk that
        # follows symlinked folders (two symlink loops made a recursive glob run forever).
        # A folder argument is the recursive search.
        matches = [m for m in sorted(glob(item)) if _reachable(Path(m), base, skip)]
    else:
        matches = [item]
    if not matches:
        raise CLIError(1, "contract_file_not_found", {"path": item})
    return matches


def _folder_contracts(folder: str, skip: frozenset, empty_event: str) -> List[Path]:
    """The contract files a folder argument holds; none is an error that names it."""
    from fluid_build.cli._common import CLIError

    inside = _contracts_in(Path(folder), skip)
    if not inside:
        raise CLIError(
            1,
            empty_event,
            {"path": folder, "error": f"{folder} holds no {' / '.join(CONTRACT_SUFFIXES)} file"},
        )
    return inside


def _glob_base(pattern: str) -> Path:
    """The folder a glob pattern starts from: its parts before the first with a wildcard."""
    plain: List[str] = []
    for part in Path(pattern).parts:
        if _GLOB_CHARS & set(part):
            break
        plain.append(part)
    return Path(*plain) if plain else Path(".")


def _reachable(path: Path, base: Path, skip: frozenset) -> bool:
    """Whether a glob's match is one a folder walk from ``base`` reaches: no symlink on the way
    and no ignored folder below ``base``."""
    try:
        below = path.relative_to(base).parts
    except ValueError:
        below = path.parts
    current = base
    for part in below:
        current = current / part
        if current.is_symlink():
            return False
    folders = below if path.is_dir() else below[:-1]
    return not any(part in skip for part in folders)


def _contracts_in(folder: Path, skip: frozenset) -> List[Path]:
    """The contract files under ``folder``, in name order, past no symlink and into no
    ignored or build-output folder."""
    out: List[Path] = []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.name not in skip:
                out += _contracts_in(entry, skip)
        elif entry.is_file() and entry.name.endswith(CONTRACT_SUFFIXES):
            out.append(entry)
    return out
