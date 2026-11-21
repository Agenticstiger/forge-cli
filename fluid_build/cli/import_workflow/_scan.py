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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ._airflow_dags import Dag, Submit, Task, read_dags
from ._conventions import Conventions
from ._fold import ModuleIndex
from ._pysource import (
    MAX_TOTAL_BYTES,
    MAX_TOTAL_SQL_CHARS,
    SourceBudget,
    SourceUnreadable,
    clean_chars,
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
    """The path as reports and contracts show it: relative to the working directory when the
    file is under it, else relative to the scanned directory. A file name can hold a new line or
    an escape sequence: it is cleaned here, once, for everything that shows it."""
    try:
        shown = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (ValueError, OSError, RuntimeError):
        shown = rel
    return clean_chars(shown)


#: Reads and writes kept from one scan, in all the jobs it reads: past it the rest of the files
#: are left unread (a project has tens of thousands, not millions).
MAX_REFS_PER_SCAN = 200_000
#: Tasks recorded against one job file: a script that hundreds of tasks submit is one job.
MAX_TASKS_PER_JOB = 1_000


def budget_note(skipped: int, notes: List[str], sql_skipped: int = 0) -> None:
    """Say how many files the source budget left unread, and how many SQL statements the SQL
    budget left alone (nothing when it left none)."""
    if skipped:
        notes.append(
            f"stopped reading after {MAX_TOTAL_BYTES // (1024 * 1024)} MB of source: {skipped} "
            "file(s) not read"
        )
    if sql_skipped:
        notes.append(
            f"stopped parsing SQL after {MAX_TOTAL_SQL_CHARS:,} characters: {sql_skipped} "
            "statement(s) not read"
        )


def scan_jobs(
    root: Path, conventions: Conventions, budget: Optional[SourceBudget] = None
) -> JobScan:
    """Read the job files under ``root``. ``budget`` is the source one import reads in all; a scan
    that is given none has its own and says what it left unread.

    Raises:
        NotADirectoryError: ``root`` is not a directory.
    """
    walk = find_python_files(root, ignore=conventions.ignore_files)
    scan = JobScan(root=Path(root), notes=list(walk.notes))
    index = ModuleIndex(
        walk.files,
        variables=conventions.variables,
        extra=conventions.config_modules,
        budget=budget,
    )
    sessions = set(DEFAULT_SESSION_NAMES) | set(conventions.session_names)
    kept = 0
    for position, source in enumerate(walk.files):
        if kept > MAX_REFS_PER_SCAN:
            scan.notes.append(
                f"stopped after {kept:,} reads and writes: {len(walk.files) - position} file(s) "
                "not read"
            )
            break
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
        kept += len(facts.reads) + len(facts.writes)
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
    if budget is None:
        budget_note(index.budget.skipped, scan.notes, index.budget.sql_skipped)
    return scan


def scan_dags(
    root: Path, conventions: Conventions, budget: Optional[SourceBudget] = None
) -> DagScan:
    """Read the DAG files under ``root`` (never importing them), against ``budget`` as
    :func:`scan_jobs` does.

    Raises:
        NotADirectoryError: ``root`` is not a directory.
    """
    walk = find_python_files(root, ignore=conventions.ignore_files)
    scan = DagScan(root=Path(root), notes=list(walk.notes))
    index = ModuleIndex(
        walk.files,
        variables=conventions.variables,
        extra=conventions.config_modules,
        budget=budget,
    )
    for source in walk.files:
        try:
            data = read_source(source.path, index.budget)
        except SourceUnreadable as exc:
            scan.unreadable[source.rel] = str(exc)
            continue
        if not might_contain_dag(data):
            continue
        try:
            # the path as the job files show theirs, so a DAG's notes name a file a person can open
            module = parse_module(_display(source.path, source.rel), data)
        except SourceUnreadable as exc:
            scan.unreadable[source.rel] = str(exc)
            continue
        scan.files += 1
        found = read_dags(module, index.folder(module), operators=conventions.operators)
        scan.dags.extend(found.dags)
        scan.notes.extend(found.notes)
    scan.dags.sort(key=lambda d: (d.file, d.line))
    if budget is None:
        budget_note(index.budget.skipped, scan.notes, index.budget.sql_skipped)
    return scan


# ── joining a task to the job file it runs ────────────────────────────


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop, before Python 3.13
        return False
    return any(resolved == r or r in resolved.parents for r in roots)


def _stays_inside(relative: str) -> bool:
    """Whether ``relative``, joined to a directory, stays under it, judged on the text alone and
    before any file is looked at: not absolute, no drive letter, no ``..`` part, no NUL. A path
    in a DAG file is the DAG file's text, so it does not get to ask the file system about
    anything outside the directories the scan is about."""
    if not relative or "\x00" in relative:
        return False
    text = relative.replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return False
    return ".." not in text.split("/")


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
            rest = script[len(prefix) :]
            if not _stays_inside(rest):
                return None
            candidate = directory / rest
            if _inside(candidate, [directory]) and candidate.is_file():
                return candidate
            return None
    if "://" not in script and project_root is not None and _stays_inside(script):
        candidate = project_root / script
        if _inside(candidate, [project_root]) and candidate.is_file():
            return candidate
    return None


def link_tasks(
    dags: Sequence[Dag],
    scans: Sequence[JobScan],
    conventions: Conventions,
    project_root: Optional[Path],
    notes: Optional[List[str]] = None,
) -> Tuple[List[LinkedTask], List[Tuple[Dag, Task, Submit, str]]]:
    """Join every submit of every task to a job file of ``scans``. Returns ``(linked,
    unlinked)``; an unlinked submit comes with why. A job records at most
    :data:`MAX_TASKS_PER_JOB` tasks (the rest are counted in ``notes``)."""
    jobs = [job for scan in scans for job in scan.jobs]
    by_path = {job.path: job for job in jobs}
    by_name: Dict[str, List[JobRecord]] = {}
    for job in jobs:
        by_name.setdefault(job.rel.rsplit("/", 1)[-1], []).append(job)
    #: A script text answers the same every time: the tasks of a project share a few.
    answers: Dict[str, "Tuple[JobRecord, str] | str"] = {}
    over: Dict[str, int] = {}
    linked: List[LinkedTask] = []
    unlinked: List[Tuple[Dag, Task, Submit, str]] = []
    for dag in dags:
        for task in dag.tasks:
            for submit in task.submits:
                if submit.script is None:
                    unlinked.append((dag, task, submit, submit.why or "no script"))
                    continue
                found = answers.get(submit.script)
                if found is None:
                    found = answers[submit.script] = _match(
                        submit.script, by_name, by_path, conventions, project_root
                    )
                if isinstance(found, str):
                    unlinked.append((dag, task, submit, found))
                    continue
                job, how = found
                if len(job.tasks) >= MAX_TASKS_PER_JOB:
                    over[job.shown] = over.get(job.shown, 0) + 1
                    continue
                link = LinkedTask(dag, task, submit, how)
                job.tasks.append(link)
                linked.append(link)
    if notes is not None:
        for shown, count in sorted(over.items()):
            notes.append(
                f"{count} more task(s) submit {clean_chars(shown)}: the first "
                f"{MAX_TASKS_PER_JOB:,} are recorded on it"
            )
    return linked, unlinked


def _match(
    script: str,
    by_name: Dict[str, List[JobRecord]],
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
    for job in by_name.get(wanted[-1], ()):  # a match shares the last segment, at least
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
