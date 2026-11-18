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

The selection (:func:`select_project_files`) follows dbt-core's own reading
(``dbt/config/project.py``, ``dbt_common/clients/system.py::find_matching``):
the top-level files in :data:`PROJECT_FILES`, plus every regular file under the
directories the ``dbt_project.yml`` ``*-paths`` keys name (dbt's defaults when a
key is absent or null, :data:`PATH_KEYS`; the deprecated ``source-paths`` and
``data-paths`` where dbt still reads them). Never a ``profiles.yml`` (a
generated one can hold a password, ``profiles.py``), ``__pycache__``, a
symbolic link, or a directory dbt writes into (``target-path``, ``log-path``,
``packages-install-path``), even one nested in a ``*-paths`` directory.
``target/``, ``logs/`` and ``dbt_packages/`` at the project root sit outside
every path key, so they are never walked. That is the set astronomer-cosmos
leaves out when it stages a project for a run (``cosmos/dbt/project.py``: logs,
target, profiles.yml, dbt_packages); the lock file stays in.

Hidden names are skipped (``.git``, ``.DS_Store``), except where dbt would read
them: a hidden file with an extension dbt parses (:data:`DBT_EXTENSIONS`), or a
hidden directory holding one, is reported in :attr:`ProjectSelection.hidden`, so
the bundle refuses it rather than bind a tree that leaves out code dbt runs.

The bytes are taken as they are on disk: no newline or Unicode normalisation,
since dbt reads them as they are, and the binding compares the same raw bytes.
"""

from __future__ import annotations

import contextvars
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

#: Top-level project files dbt reads; ``profiles.yml`` is never one of them.
#: ``.dbtignore`` is the one hidden file taken: dbt reads it to skip files, so an
#: edit to it changes what runs as surely as an edit to a model.
PROJECT_FILES: Tuple[str, ...] = (
    "dbt_project.yml",
    "packages.yml",
    "dependencies.yml",
    "package-lock.yml",
    "selectors.yml",
    ".dbtignore",
)

#: ``dbt_project.yml`` path keys and dbt-core's defaults for them (dbt-core 1.11).
PATH_KEYS: Dict[str, Tuple[str, ...]] = {
    "model-paths": ("models",),
    "macro-paths": ("macros",),
    "seed-paths": ("seeds",),
    "snapshot-paths": ("snapshots",),
    "test-paths": ("tests",),
    "analysis-paths": ("analyses",),
    "function-paths": ("functions",),
    "docs-paths": (),
    "asset-paths": (),
}

#: The deprecated spelling dbt still reads when the current key is absent.
_DEPRECATED_KEYS: Dict[str, str] = {"model-paths": "source-paths", "seed-paths": "data-paths"}

#: The directories dbt writes into, and their defaults: skipped wherever a walk meets one.
_OUTPUT_KEYS: Dict[str, str] = {
    "target-path": "target",
    "log-path": "logs",
    "packages-install-path": "dbt_packages",
    "modules-path": "dbt_modules",
}

#: The file extensions dbt parses under its paths (``dbt/parser/read_files.py``),
#: compared without case as dbt compares them.
DBT_EXTENSIONS: FrozenSet[str] = frozenset({".sql", ".py", ".csv", ".md", ".yml", ".yaml"})

#: File names never read from a project, compared without case (a case-insensitive
#: file system opens ``Profiles.yml`` for ``profiles.yml``).
_PROFILE_NAMES: FrozenSet[str] = frozenset({"profiles.yml", "profiles.yaml"})

#: Why a ``*-paths`` or output entry written as Jinja is refused.
_JINJA = (
    "a Jinja expression, which dbt renders when it runs and fluid bundle does not, so the "
    "files under it could not be bound"
)

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
    #: ``(key, entry, problem)`` for each ``*-paths`` or output entry that is not a
    #: plain directory inside the project.
    bad_paths: List[Tuple[str, str, str]] = field(default_factory=list)
    #: Hidden files dbt would read, relative to the project: not taken.
    hidden: List[str] = field(default_factory=list)
    #: Directories the walk could not list, relative to the project.
    unlisted: List[str] = field(default_factory=list)


def select_project_files(project: Path, config: Mapping[str, Any]) -> ProjectSelection:
    """The files dbt reads in ``project``, given its parsed ``dbt_project.yml``.

    Nothing is read; the caller reads the files and decides what a link, a bad
    path key or a hidden file dbt would read means for it.
    """
    selection = ProjectSelection()
    found: Dict[str, Path] = {}
    for name in PROJECT_FILES:
        path = project / name
        if path.is_symlink():
            selection.links.append(name)
        elif path.is_file():
            found[name] = path
    skip, linked = _output_dirs(project, config, selection.bad_paths)
    for key, default in PATH_KEYS.items():
        used, entries = _path_entries(config, key, default)
        if entries is None:
            selection.bad_paths.append((used, str(config.get(used)), "not a list of directories"))
            continue
        for entry in entries:
            rel = PurePosixPath(entry)
            problem = _JINJA if ("{{" in entry or "{%" in entry) else path_key_problem(project, rel)
            base_id = None if problem else _dir_id(project / rel)
            if problem is None and base_id is not None and base_id in skip | linked:
                problem = "the directory dbt writes its output into"
            if problem is not None:
                selection.bad_paths.append((used, entry, problem))
                continue
            _walk(project, project / rel, found, selection, skip, linked)
    selection.files = dict(sorted(found.items()))
    return selection


def _path_entries(
    config: Mapping[str, Any], key: str, default: Tuple[str, ...]
) -> Tuple[str, Optional[List[str]]]:
    """The key dbt reads for ``key`` and its entries, as dbt-core reads them.

    The deprecated spelling when only it is set; the default when the key is absent
    or null (dbt's ``value_or``). ``None`` entries for a value that is not a string or
    a list of strings.
    """
    used = key
    if key not in config and _DEPRECATED_KEYS.get(key) in config:
        used = _DEPRECATED_KEYS[key]
    value = config.get(used)
    if value is None:
        return used, list(default)
    if isinstance(value, str):
        return used, [value]
    if isinstance(value, (list, tuple)) and all(isinstance(entry, str) for entry in value):
        return used, list(value)
    return used, None


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
    try:
        for depth in range(1, len(rel.parts) + 1):
            if project.joinpath(*rel.parts[:depth]).is_symlink():
                return f"through the symbolic link {'/'.join(rel.parts[:depth])!r}"
    except (OSError, ValueError):
        return "not a usable path"
    return None


#: A directory by identity, ``(st_dev, st_ino)``: a case-insensitive file system
#: names ``Target`` and ``target`` differently and opens the same directory.
_DirId = Tuple[int, int]


def _dir_id(path: Path) -> Optional[_DirId]:
    try:
        st = path.stat()
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino) if stat.S_ISDIR(st.st_mode) else None


def _output_dirs(
    project: Path, config: Mapping[str, Any], bad: List[Tuple[str, str, str]]
) -> Tuple[FrozenSet[_DirId], FrozenSet[_DirId]]:
    """The directories dbt writes into, by identity: ``(real ones, ones reached by a link)``.

    A real one nested in a read directory is skipped; one a link points at is
    refused where a walk meets it, since dbt writes into it through the link while
    its files may be read under another name. An entry dbt cannot name literally
    (Jinja, not a string) is recorded in ``bad``.
    """
    real, linked = set(), set()
    for key, default in _OUTPUT_KEYS.items():
        value = config.get(key)
        value = default if value is None else value
        if not isinstance(value, str) or not value:
            bad.append((key, str(value), "not a directory name"))
            continue
        if "{{" in value or "{%" in value:
            bad.append((key, value, _JINJA))
            continue
        found = _dir_id(project / value)
        if found is None:
            continue
        try:
            is_link = (project / value).is_symlink()
        except (OSError, ValueError):
            is_link = False
        (linked if is_link else real).add(found)
    return frozenset(real), frozenset(linked)


def _walk(
    project: Path,
    base: Path,
    found: Dict[str, Path],
    selection: ProjectSelection,
    skip: FrozenSet[_DirId],
    linked: FrozenSet[_DirId],
) -> None:
    """Add every regular file dbt reads under ``base``; record what it refuses.

    A file is added only if it resolves inside the project, whatever the walk
    met on the way (:func:`path_key_problem` checks the entry itself first).
    """
    root = project.resolve()
    if base.is_symlink():
        selection.links.append(base.relative_to(project).as_posix())
        return
    if not base.is_dir():
        return  # dbt tolerates a missing default directory

    def unlisted(error: OSError) -> None:
        name = Path(error.filename) if error.filename else base
        selection.unlisted.append(_rel(project, name))

    for dirpath, dirnames, filenames in os.walk(base, onerror=unlisted, followlinks=False):
        here = Path(dirpath)
        # Sorted, so every file system walks alike and reports the same first link.
        dirnames[:] = [
            n for n in sorted(dirnames) if _descend(project, here / n, selection, skip, linked)
        ]
        for name in sorted(filenames):
            path = here / name
            rel = path.relative_to(project).as_posix()
            if name.lower() in _PROFILE_NAMES:
                continue
            if name.startswith("."):
                if _dbt_reads(name):
                    selection.hidden.append(rel)
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                selection.links.append(rel)
            elif path.is_file():
                found[rel] = path


def _descend(
    project: Path,
    child: Path,
    selection: ProjectSelection,
    skip: FrozenSet[_DirId],
    linked: FrozenSet[_DirId],
) -> bool:
    """Whether the walk enters ``child``: not a cache, a link (recorded), a directory dbt
    writes into, or hidden (a hidden one holding a file dbt reads is recorded)."""
    rel = child.relative_to(project).as_posix()
    if child.name == "__pycache__":
        return False
    if child.is_symlink():
        selection.links.append(rel)
        return False
    if child.name.startswith("."):
        selection.hidden.extend(_dbt_files_under(project, child))
        return False
    identity = _dir_id(child)
    if identity in linked:
        selection.bad_paths.append(
            ("output", rel, "a directory dbt writes into through a symbolic link")
        )
        return False
    return identity not in skip


def _dbt_reads(name: str) -> bool:
    return Path(name).suffix.lower() in DBT_EXTENSIONS


def _dbt_files_under(project: Path, directory: Path) -> List[str]:
    """The files under a hidden ``directory`` dbt would read (it walks hidden ones too)."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(directory, followlinks=False):
        found.extend(_rel(project, Path(dirpath) / n) for n in sorted(filenames) if _dbt_reads(n))
    return sorted(found)


def _rel(project: Path, path: Path) -> str:
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return path.as_posix()


def load_project_config(project: Path) -> Mapping[str, Any]:
    """``dbt_project.yml`` parsed; a mapping that is not one reads as empty, as dbt's defaults."""
    import yaml

    try:
        config = yaml.safe_load((project / "dbt_project.yml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError, yaml.YAMLError) as exc:
        raise DbtProjectFilesError(
            f"dbt_project.yml in {project} could not be read ({type(exc).__name__}); "
            "fix it, dbt reads it the same way"
        ) from exc
    return config if isinstance(config, Mapping) else {}


def read_project_files(project: Path, *, budget: int = MAX_PROJECT_BYTES) -> Dict[str, bytes]:
    """The bytes of every file dbt reads in ``project``, by path relative to it, sorted.

    Raises :class:`DbtProjectFilesError` for a project that cannot be read as one
    tree: an unreadable ``dbt_project.yml``, a ``*-paths`` entry outside it or
    written as Jinja, a symbolic link, a hidden file dbt would read, a directory
    that cannot be listed, a file over :data:`MAX_FILE_BYTES`, or more than
    ``budget`` bytes in all.
    """
    selection = select_project_files(project, load_project_config(project))
    _refuse(selection)
    files: Dict[str, bytes] = {}
    total = 0
    for rel, path in selection.files.items():
        data = read_project_file(path, rel)
        total += len(data)
        if total > budget:
            raise DbtProjectFilesError(
                f"its files pass {MAX_PROJECT_BYTES} bytes, the most one bundle packs for all "
                "its dbt projects together. Load data that size as a table, not a dbt seed"
            )
        files[rel] = data
    return files


def _refuse(selection: ProjectSelection) -> None:
    """Raise for the first thing in ``selection`` that keeps it from being one bound tree."""
    if selection.bad_paths:
        key, entry, problem = selection.bad_paths[0]
        where = f"dbt_project.yml {key} names {entry!r}" if key != "output" else entry
        raise DbtProjectFilesError(f"{where}, {problem}; name a plain directory inside the project")
    if selection.links:
        raise DbtProjectFilesError(
            f"{selection.links[0]} in the dbt project is a symbolic link, which is never "
            "followed: it could reach a file outside the project; replace the link with "
            "the file itself"
        )
    if selection.hidden:
        raise DbtProjectFilesError(
            f"{selection.hidden[0]} is a hidden file dbt reads, which the bundle does not "
            "pack; rename it, or delete it if it is a stray copy"
        )
    if selection.unlisted:
        raise DbtProjectFilesError(
            f"{selection.unlisted[0]} in the dbt project could not be listed, so its files "
            "could not be bound; make it readable"
        )


def read_project_file(path: Path, rel: str) -> bytes:
    """A file's bytes, refusing one over :data:`MAX_FILE_BYTES` without reading it all.

    Opened without following a link (``O_NOFOLLOW``, where the platform has it) and
    read only when it is a regular file, so a file swapped for a link or a FIFO after
    the walk selected it is refused rather than read.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)  # Windows: no newline translation
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or exc
        raise DbtProjectFilesError(f"{rel} could not be read ({reason})") from exc
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise DbtProjectFilesError(f"{rel} is no longer a regular file; it was not read")
        try:
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
    and a ``repository`` that is a path, not a Git URL: a Git source is pinned by
    its ref, and no file of it is on disk here. A build whose fields are not of
    the types the schema gives them binds nothing here (``fluid validate`` and
    ``fluid apply`` refuse it).
    """
    from ...util.contract import is_externally_run_build
    from ..base import is_dbt_build, is_embedded_sql_build

    if not isinstance(build, Mapping):
        return False
    engine, pattern = build.get("engine"), build.get("pattern")
    repository = build.get("repository", "./")
    properties = build.get("properties")
    if not isinstance(engine, str) or not isinstance(repository, str):
        return False
    if (pattern is not None and not isinstance(pattern, str)) or not isinstance(
        properties if properties is not None else {}, Mapping
    ):
        return False
    as_dict = dict(build)
    if not is_dbt_build(as_dict) or is_embedded_sql_build(as_dict):
        return False
    if is_externally_run_build(build):
        return False
    return not ("://" in repository or repository.strip().startswith("git@"))


def collect_bundle_projects(
    contract: Mapping[str, Any], contract_path: Path
) -> Dict[str, Dict[str, bytes]]:
    """The dbt project of every build that binds one, by build id: ``fluid bundle``'s input.

    Each project is found as the dbt runner finds it
    (:func:`~fluid_build.build_runners.dbt.runner.resolve_dbt_project_path`,
    anchored at ``contract_path`` and confined to the contract's workspace), so
    the bundle holds the files ``fluid apply`` will run. Raises
    :class:`DbtProjectFilesError` naming the build for a build id that cannot
    be a bundle path, two builds with one id, a templated or missing project,
    and any reason :func:`read_project_files` refuses a tree.
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
        repository = str(build.get("repository", "./"))
        if "{{" in repository:
            raise DbtProjectFilesError(
                f"build {build_id!r}: repository {repository!r} is a template, so the dbt "
                "project it names is only known when the build runs; name the project's "
                "directory literally"
            )
        project = resolve_dbt_project_path(contract_path, dict(build))
        if project is None:
            expected = (contract_path.parent / repository).resolve()
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


def binding_active() -> bool:
    """Whether the running ``fluid apply`` bound dbt projects (its bundle packs some)."""
    return _BOUND.get() is not None


def set_bound_projects(
    projects: Optional[Mapping[str, BoundProject]],
) -> "contextvars.Token[Optional[Mapping[str, BoundProject]]]":
    """Bind ``projects`` for the builds this context runs; reset with :func:`reset_bound_projects`."""
    return _BOUND.set(dict(projects) if projects is not None else None)


def reset_bound_projects(token: "contextvars.Token[Optional[Mapping[str, BoundProject]]]") -> None:
    _BOUND.reset(token)


__all__ = [
    "DBT_EXTENSIONS",
    "MAX_FILE_BYTES",
    "MAX_PROJECT_BYTES",
    "PATH_KEYS",
    "PROJECT_FILES",
    "BoundProject",
    "DbtProjectFilesError",
    "ProjectSelection",
    "TreeDiff",
    "binding_active",
    "binds_dbt_project",
    "bound_project",
    "check_project",
    "collect_bundle_projects",
    "diff_trees",
    "file_digests",
    "load_project_config",
    "path_key_problem",
    "read_project_file",
    "read_project_files",
    "reset_bound_projects",
    "select_project_files",
    "set_bound_projects",
]
