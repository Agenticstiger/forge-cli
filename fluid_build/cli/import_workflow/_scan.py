# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Scan a directory of PySpark jobs and a directory of Airflow DAGs, and join them.

:func:`scan_jobs` reads every Python file under a jobs directory with :mod:`._spark_io` and
keeps the files that do Spark IO (a job). :func:`scan_dags` reads every file under a DAGs
directory that might hold a DAG (:func:`._pysource.might_contain_dag`) with
:mod:`._airflow_dags`. :func:`link_tasks` joins what a DAG task submits to the job file it runs.

How a submitted script is matched to a job file, in order:

1. the conventions' ``script_roots``: ``s3://code/jobs/orders.py`` under the root
   ``s3://code/jobs/ -> jobs`` is the local file ``jobs/orders.py``;
2. a relative path that exists inside the project (the DAGs directory's parent);
3. the path's trailing segments: the job file whose path shares the most trailing segments with
   the script, when one does and the file name is the same. A match on the file name alone is
   marked as such in the report, and a tie is no match.

Files are only ever read from the scanned directories, the directories the conventions name and
the project directory the DAGs sit in; a script path in a DAG file that points anywhere else is
not read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ._airflow_dags import Dag, Submit, Task, read_dags
from ._conventions import Conventions
from ._fold import ModuleIndex
from ._pysource import (
    SourceFile,
    SourceUnreadable,
    find_python_files,
    might_contain_dag,
    parse_module,
    read_source,
)
from ._spark_io import DEFAULT_SESSION_NAMES, JobFacts, read_job


@dataclass
class JobRecord:
    """A job file and what is known about it."""

    #: Path of the file relative to the directory it was found in (POSIX).
    rel: str
    #: The file, absolute.
    path: Path
    #: Path shown in reports and written to ``properties.model``: relative to the working
    #: directory when the file is under it, else ``rel``.
    shown: str
    facts: JobFacts
    #: The tasks that submit it, once joined.
    tasks: List["LinkedTask"] = field(default_factory=list)


@dataclass(frozen=True)
class LinkedTask:
    dag: Dag
    task: Task
    submit: Submit
    #: ``script_root``, ``project``, ``path`` (more than the file name matched) or ``name``.
    how: str


@dataclass
class JobScan:
    root: Path
    jobs: List[JobRecord] = field(default_factory=list)
    #: Python files read that are not jobs, by relative path (helpers, libraries).
    other_files: int = 0
    notes: List[str] = field(default_factory=list)
    #: Unreadable files and why, by relative path.
    unreadable: Dict[str, str] = field(default_factory=dict)


@dataclass
class DagScan:
    root: Path
    dags: List[Dag] = field(default_factory=list)
    files: int = 0
    notes: List[str] = field(default_factory=list)
    unreadable: Dict[str, str] = field(default_factory=dict)


def _display(path: Path, rel: str) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (ValueError, OSError):
        return rel


def scan_jobs(root: Path, conventions: Conventions) -> JobScan:
    """Read the job files under ``root``.

    Raises:
        NotADirectoryError: ``root`` is not a directory.
    """
    walk = find_python_files(root, ignore=conventions.ignore_files)
    scan = JobScan(root=Path(root), notes=list(walk.notes))
    index = ModuleIndex(
        walk.files, variables=conventions.variables, extra=conventions.config_modules
    )
    sessions = set(DEFAULT_SESSION_NAMES) | set(conventions.session_names)
    for source in walk.files:
        module = index.load(source)
        if module is None:
            scan.unreadable[source.rel] = index.unreadable.get(source.rel, "it cannot be read")
            continue
        shown = _display(source.path, source.rel)
        facts = read_job(
            module,
            index.folder(module),
            helpers=conventions.helpers,
            session_names=sessions,
            shown=shown,
        )
        if facts.has_io:
            scan.jobs.append(
                JobRecord(
                    rel=source.rel,
                    path=source.path.resolve(),
                    shown=shown,
                    facts=facts,
                )
            )
        else:
            scan.other_files += 1
            # a helper's note (a name from a parameter) still matters to the person reading
            scan.notes.extend(facts.notes)
    return scan


def scan_dags(root: Path, conventions: Conventions) -> DagScan:
    """Read the DAG files under ``root`` (never importing them).

    Raises:
        NotADirectoryError: ``root`` is not a directory.
    """
    walk = find_python_files(root, ignore=conventions.ignore_files)
    scan = DagScan(root=Path(root), notes=list(walk.notes))
    index = ModuleIndex(
        walk.files, variables=conventions.variables, extra=conventions.config_modules
    )
    for source in walk.files:
        try:
            data = read_source(source.path)
        except SourceUnreadable as exc:
            scan.unreadable[source.rel] = str(exc)
            continue
        if not might_contain_dag(data):
            continue
        try:
            module = parse_module(source.rel, data)
        except SourceUnreadable as exc:
            scan.unreadable[source.rel] = str(exc)
            continue
        scan.files += 1
        found = read_dags(module, index.folder(module), operators=conventions.operators)
        scan.dags.extend(found.dags)
        scan.notes.extend(found.notes)
    scan.dags.sort(key=lambda d: (d.file, d.line))
    return scan


# ── joining a task to the job file it runs ────────────────────────────


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == r or r in resolved.parents for r in roots)


def _segments(script: str) -> List[str]:
    text = script.split("?")[0]
    if "://" in text:
        text = text.split("://", 1)[1]
    return [s for s in text.replace("\\", "/").split("/") if s and s not in {".", ".."}]


def local_script(
    script: str, conventions: Conventions, project_root: Optional[Path]
) -> Optional[Path]:
    """The local file a submitted script is, through the conventions' ``script_roots`` or as a
    relative path inside the project; ``None`` when neither names an existing file."""
    for prefix, directory in conventions.script_roots:
        if script.startswith(prefix):
            candidate = directory / script[len(prefix) :]
            if candidate.is_file() and _inside(candidate, [directory]):
                return candidate
            return None
    if "://" not in script and not os.path.isabs(script) and project_root is not None:
        candidate = project_root / script
        if candidate.is_file() and _inside(candidate, [project_root]):
            return candidate
    return None


def link_tasks(
    dags: Sequence[Dag],
    scans: Sequence[JobScan],
    conventions: Conventions,
    project_root: Optional[Path],
) -> Tuple[List[LinkedTask], List[Tuple[Dag, Task, Submit, str]]]:
    """Join every submit of every task to a job file of ``scans``. Returns ``(linked,
    unlinked)``; an unlinked submit comes with why."""
    jobs = [job for scan in scans for job in scan.jobs]
    by_path = {job.path: job for job in jobs}
    linked: List[LinkedTask] = []
    unlinked: List[Tuple[Dag, Task, Submit, str]] = []
    for dag in dags:
        for task in dag.tasks:
            for submit in task.submits:
                if submit.script is None:
                    unlinked.append((dag, task, submit, submit.why or "no script"))
                    continue
                found = _match(submit.script, jobs, by_path, conventions, project_root)
                if isinstance(found, str):
                    unlinked.append((dag, task, submit, found))
                    continue
                job, how = found
                link = LinkedTask(dag, task, submit, how)
                job.tasks.append(link)
                linked.append(link)
    return linked, unlinked


def _match(
    script: str,
    jobs: Sequence[JobRecord],
    by_path: Dict[Path, JobRecord],
    conventions: Conventions,
    project_root: Optional[Path],
) -> "Tuple[JobRecord, str] | str":
    local = local_script(script, conventions, project_root)
    if local is not None:
        job = by_path.get(local.resolve())
        if job is not None:
            mapped = any(script.startswith(prefix) for prefix, _ in conventions.script_roots)
            return job, "script_root" if mapped else "project"
    wanted = _segments(script)
    if not wanted:
        return "the script path is empty"
    scored: List[Tuple[int, JobRecord]] = []
    for job in jobs:
        have = job.rel.split("/")
        common = 0
        while common < min(len(wanted), len(have)) and wanted[-1 - common] == have[-1 - common]:
            common += 1
        if common:
            scored.append((common, job))
    if not scored:
        return "no job file has that name"
    best = max(common for common, _ in scored)
    winners = [job for common, job in scored if common == best]
    if len(winners) > 1:
        names = ", ".join(sorted(j.shown for j in winners)[:3])
        return f"several job files match it equally ({names})"
    return winners[0], ("name" if best == 1 else "path")
