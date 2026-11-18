# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The files of a dbt project that dbt reads, and the check that binds them.

One definition of "what dbt reads", for every place forge reads a dbt project
as a whole:

* ``fluid bundle --format tgz`` packs these files under
  ``sources/dbt/<build id>/`` (:func:`collect_bundle_projects`), ``fluid plan``
  binds them, and ``fluid apply`` refuses a checkout whose files differ
  (:func:`check_project`);
* the Databricks job emitter uploads them as workspace files
  (``iac/providers/databricks_jobs.py``).

The selection (:func:`select_project_files`): the top-level files in
:data:`PROJECT_FILES`, plus every regular, visible file under the directories
the ``dbt_project.yml`` ``*-paths`` keys name (dbt-core's defaults when a key
is absent, :data:`PATH_KEYS`). Never a ``profiles.yml`` (a generated one can
hold a password, ``profiles.py``), a hidden file or directory (``.user.yml``,
``.git``), ``__pycache__``, a symbolic link, or a directory dbt writes into
(``target-path``, ``log-path``, ``packages-install-path``), even one nested in
a ``*-paths`` directory. ``target/``, ``logs/`` and ``dbt_packages/`` at the
project root sit outside every path key, so they are never walked. That is the
set astronomer-cosmos leaves out when it stages a project for a run
(``cosmos/dbt/project.py``: logs, target, profiles.yml, dbt_packages); the lock
file stays in, since it pins the packages.

The bytes are taken as they are on disk: no newline or Unicode normalisation,
since dbt reads them as they are, and the binding compares the same raw bytes.
"""

from __future__ import annotations

import contextvars
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

#: Top-level project files dbt reads; ``profiles.yml`` is never one of them.
PROJECT_FILES: Tuple[str, ...] = (
    "dbt_project.yml",
    "packages.yml",
    "dependencies.yml",
    "package-lock.yml",
    "selectors.yml",
)

#: ``dbt_project.yml`` path keys and dbt-core's defaults for them.
PATH_KEYS: Dict[str, Tuple[str, ...]] = {
    "model-paths": ("models",),
    "macro-paths": ("macros",),
    "seed-paths": ("seeds",),
    "snapshot-paths": ("snapshots",),
    "test-paths": ("tests",),
    "analysis-paths": ("analyses",),
    "docs-paths": (),
    "asset-paths": (),
}

#: The directories dbt writes into, and their defaults: skipped wherever a walk meets one.
_OUTPUT_KEYS: Dict[str, str] = {
    "target-path": "target",
    "log-path": "logs",
    "packages-install-path": "dbt_packages",
    "modules-path": "dbt_modules",
}

#: File names never read from a project, compared without case (a case-insensitive
#: file system opens ``Profiles.yml`` for ``profiles.yml``).
_PROFILE_NAMES: FrozenSet[str] = frozenset({"profiles.yml", "profiles.yaml"})

#: The largest file a bundle can carry: the bundle reader refuses a bigger member
#: (``forge/core/bundle.py::MAX_BUNDLE_MEMBER_BYTES``).
MAX_FILE_BYTES = 64 * 1024 * 1024
#: The most dbt project bytes one bundle packs, every build's project together.
MAX_PROJECT_BYTES = 128 * 1024 * 1024


class DbtProjectFilesError(ValueError):
    """A dbt project whose files cannot be read as one tree: what is wrong, and what to do."""


@dataclass
class ProjectSelection:
    """The files dbt reads in one project, and what the walk refused."""

    #: Path relative to the project (POSIX) to the file, sorted by path.
    files: Dict[str, Path] = field(default_factory=dict)
    #: Symbolic links met, relative to the project: never followed.
    links: List[str] = field(default_factory=list)
    #: ``(key, entry, problem)`` for each ``*-paths`` entry that is not a plain
    #: directory inside the project.
    bad_paths: List[Tuple[str, str, str]] = field(default_factory=list)


def select_project_files(project: Path, config: Mapping[str, Any]) -> ProjectSelection:
    """The files dbt reads in ``project``, given its parsed ``dbt_project.yml``.

    Nothing is read; the caller reads the files and decides what a link or a
    bad path key means for it.
    """
    selection = ProjectSelection()
    found: Dict[str, Path] = {}
    for name in PROJECT_FILES:
        path = project / name
        if path.is_symlink():
            selection.links.append(name)
        elif path.is_file():
            found[name] = path
    skip = _output_dirs(project, config)
    for key, default in PATH_KEYS.items():
        entries = config.get(key, list(default))
        if isinstance(entries, str):
            entries = [entries]
        if not isinstance(entries, (list, tuple)):
            selection.bad_paths.append((key, str(entries), "not a list of directories"))
            continue
        for entry in entries:
            rel = PurePosixPath(str(entry))
            problem = path_key_problem(project, rel)
            if problem is not None:
                selection.bad_paths.append((key, str(entry), problem))
                continue
            _walk(project, project / rel, found, selection.links, skip)
    selection.files = dict(sorted(found.items()))
    return selection


def path_key_problem(project: Path, rel: PurePosixPath) -> Optional[str]:
    """Why a ``*-paths`` entry is not a plain directory inside the project, or ``None``.

    Every component is checked, not just the last: ``lnk/leak`` with ``lnk`` a
    link would walk wherever ``lnk`` points, and ``.git`` holds the checkout's
    credentials (``actions/checkout`` persists a token in ``.git/config``).
    """
    if rel.is_absolute() or ".." in rel.parts or "\\" in str(rel):
        return "outside the project"
    if not rel.parts:
        return "the project root, which would take everything in it"
    hidden = [part for part in rel.parts if part.startswith(".")]
    if hidden:
        return f"the hidden directory {hidden[0]!r}"
    for depth in range(1, len(rel.parts) + 1):
        if project.joinpath(*rel.parts[:depth]).is_symlink():
            return f"through the symbolic link {'/'.join(rel.parts[:depth])!r}"
    return None


def _output_dirs(project: Path, config: Mapping[str, Any]) -> FrozenSet[Path]:
    """The resolved directories dbt writes into, configured or by default."""
    dirs = set()
    for key, default in _OUTPUT_KEYS.items():
        value = config.get(key, default)
        if isinstance(value, str) and value:
            try:
                dirs.add((project / value).resolve())
            except (OSError, RuntimeError):
                continue
    return frozenset(dirs)


def _walk(
    project: Path,
    base: Path,
    found: Dict[str, Path],
    links: List[str],
    skip: FrozenSet[Path],
) -> None:
    """Add every regular, visible file under ``base``; collect the links met.

    A file is added only if it resolves inside the project, whatever the walk
    met on the way (:func:`path_key_problem` checks the entry itself first).
    """
    root = project.resolve()
    if base.is_symlink():
        links.append(base.relative_to(project).as_posix())
        return
    if not base.is_dir() or base.resolve() in skip:
        return  # dbt tolerates a missing default directory
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        here = Path(dirpath)
        # Sorted, so every file system walks alike and reports the same first link.
        dirnames[:] = [n for n in sorted(dirnames) if _descend(project, here / n, links, skip)]
        for name in sorted(filenames):
            path = here / name
            if name.startswith(".") or name.lower() in _PROFILE_NAMES:
                continue
            rel = path.relative_to(project).as_posix()
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                links.append(rel)
            elif path.is_file():
                found[rel] = path


def _descend(project: Path, child: Path, links: List[str], skip: FrozenSet[Path]) -> bool:
    """Whether the walk enters ``child``: not hidden, a cache, a link (collected) or a
    directory dbt writes into."""
    if child.name.startswith(".") or child.name == "__pycache__":
        return False
    if child.is_symlink():
        links.append(child.relative_to(project).as_posix())
        return False
    return child.resolve() not in skip


def load_project_config(project: Path) -> Mapping[str, Any]:
    """``dbt_project.yml`` parsed; a mapping that is not one reads as empty, as dbt's defaults."""
    import yaml

    try:
        config = yaml.safe_load((project / "dbt_project.yml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DbtProjectFilesError(
            f"dbt_project.yml in {project} could not be read ({type(exc).__name__}); "
            "fix it, dbt reads it the same way"
        ) from exc
    return config if isinstance(config, Mapping) else {}


def read_project_files(project: Path, *, budget: int = MAX_PROJECT_BYTES) -> Dict[str, bytes]:
    """The bytes of every file dbt reads in ``project``, by path relative to it, sorted.

    Raises :class:`DbtProjectFilesError` for a project that cannot be read as one
    tree: an unreadable ``dbt_project.yml``, a ``*-paths`` entry outside it, a
    symbolic link, a file over :data:`MAX_FILE_BYTES`, or more than ``budget``
    bytes in all.
    """
    selection = select_project_files(project, load_project_config(project))
    if selection.bad_paths:
        key, entry, problem = selection.bad_paths[0]
        raise DbtProjectFilesError(
            f"dbt_project.yml {key} names {entry!r}, {problem}; name a plain directory "
            "inside the project"
        )
    if selection.links:
        raise DbtProjectFilesError(
            f"{selection.links[0]} in the dbt project is a symbolic link, which is never "
            "followed: it could reach a file outside the project; replace the link with "
            "the file itself"
        )
    files: Dict[str, bytes] = {}
    total = 0
    for rel, path in selection.files.items():
        data = _read_bounded(path, rel)
        total += len(data)
        if total > budget:
            raise DbtProjectFilesError(
                f"its files pass {MAX_PROJECT_BYTES} bytes, the most one bundle packs for all "
                "its dbt projects together. Load data that size as a table, not a dbt seed"
            )
        files[rel] = data
    return files


def _read_bounded(path: Path, rel: str) -> bytes:
    """A file's bytes, refusing one over :data:`MAX_FILE_BYTES` without reading it all."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise DbtProjectFilesError(f"{rel} could not be read ({exc.strerror or exc})") from exc
    if len(data) > MAX_FILE_BYTES:
        raise DbtProjectFilesError(
            f"{rel} is over {MAX_FILE_BYTES} bytes, the largest file a bundle carries. Load "
            "data that size as a table, not a dbt seed"
        )
    return data


def file_digests(files: Mapping[str, bytes]) -> Dict[str, str]:
    """``sha256:<hex>`` of each file, by path: the form a bundle MANIFEST lists them in."""
    return {rel: "sha256:" + hashlib.sha256(files[rel]).hexdigest() for rel in sorted(files)}


# ---------------------------------------------------------------------------
# The builds whose project is bound, and the bundle's copy of each
# ---------------------------------------------------------------------------


def binds_dbt_project(build: Mapping[str, Any]) -> bool:
    """True for a build whose dbt project forge reads from disk.

    A dbt engine (``dbt`` or ``dbt-<adapter>``), no inline SQL (such a build
    runs on the local DuckDB engine), not a job of another platform's engine,
    and a ``repository`` that is not a Git URL: a Git source is pinned by its
    ref, and no file of it is on disk here.
    """
    from ...util.contract import is_externally_run_build
    from ..base import is_dbt_build, is_embedded_sql_build

    if not isinstance(build, Mapping):
        return False
    as_dict = dict(build)
    if not is_dbt_build(as_dict) or is_embedded_sql_build(as_dict):
        return False
    if is_externally_run_build(build):
        return False
    repository = str(build.get("repository") or "").strip()
    return not ("://" in repository or repository.startswith("git@"))


def collect_bundle_projects(
    contract: Mapping[str, Any], contract_path: Path
) -> Dict[str, Dict[str, bytes]]:
    """The dbt project of every build that binds one, by build id: ``fluid bundle``'s input.

    Each project is found as the dbt runner finds it
    (:func:`~fluid_build.build_runners.dbt.runner.resolve_dbt_project_path`,
    anchored at ``contract_path`` and confined to the contract's workspace), so
    the bundle holds the files ``fluid apply`` will run. Raises
    :class:`DbtProjectFilesError` naming the build for a build id that cannot
    be a bundle path, two builds with one id, a project that is not there, and
    any reason :func:`read_project_files` refuses a tree.
    """
    from .._ids import IdentifierViolation, validate_identifier
    from .runner import resolve_dbt_project_path

    projects: Dict[str, Dict[str, bytes]] = {}
    budget = MAX_PROJECT_BYTES
    builds = contract.get("builds")
    for index, build in enumerate(builds if isinstance(builds, list) else []):
        if not isinstance(build, Mapping) or not binds_dbt_project(build):
            continue
        build_id = build.get("id")
        try:
            validate_identifier(build_id, kind="build id")
        except IdentifierViolation as exc:
            raise DbtProjectFilesError(
                f"builds[{index}]: a dbt build needs an id to bind its project as "
                f"sources/dbt/<id>/ in the bundle ({exc})"
            ) from exc
        if build_id in projects:
            raise DbtProjectFilesError(
                f"builds[{index}]: two dbt builds share the id {build_id!r}; give each its own id"
            )
        project = resolve_dbt_project_path(contract_path, dict(build))
        if project is None:
            repository = build.get("repository", "./")
            expected = (contract_path.parent / str(repository)).resolve()
            raise DbtProjectFilesError(
                f"build {build_id!r}: no dbt_project.yml at {expected} (repository "
                f"{repository!r}), or it resolves outside the contract's workspace. The bundle "
                "packs the dbt project a build runs: check it out beside the contract, or "
                "point repository at it"
            )
        try:
            files = read_project_files(project, budget=budget)
        except DbtProjectFilesError as exc:
            raise DbtProjectFilesError(f"build {build_id!r}: {exc}") from exc
        budget -= sum(len(data) for data in files.values())
        projects[build_id] = files
    return projects


# ---------------------------------------------------------------------------
# The binding: the checkout against the files a bundle binds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TreeDiff:
    """How a project on disk differs from the files a bundle binds."""

    changed: Tuple[str, ...] = ()
    added: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.changed or self.added or self.removed)

    def describe(self) -> str:
        """``changed a.sql, b.sql; added c.sql; removed d.sql``: a few of each, then a count.

        Listed as the state-drift report lists paths (``iac/drift.py::shown``).
        """
        from fluid_build.iac.drift import shown

        kinds = (("changed", self.changed), ("added", self.added), ("removed", self.removed))
        return "; ".join(f"{label} {shown(list(paths))}" for label, paths in kinds if paths)


def diff_trees(expected: Mapping[str, str], actual: Mapping[str, str]) -> TreeDiff:
    """The files whose digest changed, the files only on disk, the files only bound."""
    return TreeDiff(
        changed=tuple(sorted(p for p in expected if p in actual and actual[p] != expected[p])),
        added=tuple(sorted(p for p in actual if p not in expected)),
        removed=tuple(sorted(p for p in expected if p not in actual)),
    )


def check_project(project: Path, expected: Mapping[str, str]) -> Optional[str]:
    """Why ``project`` on disk is not the tree ``expected`` binds, or ``None`` when it is.

    ``expected`` maps each path relative to the project to its ``sha256:<hex>``
    (as a bundle MANIFEST lists it). The project is read with the same selection
    and limits ``fluid bundle`` packed it with, so a link or an oversized file
    that appeared since is a difference too.
    """
    try:
        actual = file_digests(read_project_files(project))
    except DbtProjectFilesError as exc:
        return str(exc)
    difference = diff_trees(expected, actual)
    return difference.describe() if difference else None


@dataclass(frozen=True)
class BoundProject:
    """The files one build's dbt project must hold for this ``fluid apply`` to run it."""

    build_id: str
    #: Path relative to the project to its ``sha256:<hex>``.
    files: Mapping[str, str]
    #: The bundle that binds them, as the error names it.
    bundle: str


_BOUND: contextvars.ContextVar[Optional[Mapping[str, BoundProject]]] = contextvars.ContextVar(
    "fluid_bound_dbt_projects", default=None
)


def bound_project(build_id: str) -> Optional[BoundProject]:
    """The bound files of ``build_id``'s project in the running ``fluid apply``, or ``None``."""
    bound = _BOUND.get()
    return None if bound is None else bound.get(build_id)


def set_bound_projects(
    projects: Optional[Mapping[str, BoundProject]],
) -> "contextvars.Token[Optional[Mapping[str, BoundProject]]]":
    """Bind ``projects`` for the builds this context runs; reset with :func:`reset_bound_projects`."""
    return _BOUND.set(dict(projects) if projects is not None else None)


def reset_bound_projects(token: "contextvars.Token[Optional[Mapping[str, BoundProject]]]") -> None:
    _BOUND.reset(token)


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_PROJECT_BYTES",
    "PATH_KEYS",
    "PROJECT_FILES",
    "BoundProject",
    "DbtProjectFilesError",
    "ProjectSelection",
    "TreeDiff",
    "binds_dbt_project",
    "bound_project",
    "check_project",
    "collect_bundle_projects",
    "diff_trees",
    "file_digests",
    "load_project_config",
    "path_key_problem",
    "read_project_files",
    "reset_bound_projects",
    "select_project_files",
    "set_bound_projects",
]
