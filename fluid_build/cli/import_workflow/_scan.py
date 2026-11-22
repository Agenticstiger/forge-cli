# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Scan a directory of PySpark jobs and a directory of Airflow DAGs, and join them.

:func:`scan_jobs` reads every Python file under a jobs directory with :mod:`._spark_io` and
keeps the files that do Spark IO (a job). :func:`scan_dags` reads every file under a DAGs
directory that might hold a DAG (:func:`._pysource.might_contain_dag`) with
:mod:`._airflow_dags`. :func:`link_tasks` joins what a DAG task submits to the job file it runs.

How a submitted script is matched to a job file, in order:

0. the conventions' ``drivers``: a script that runs jobs by name (``driver.py --job-list
   silver_orders``) is joined to every job file the named list holds (:mod:`._drivers`);
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
from ._catalogs import module_settings
from ._conventions import Conventions, Driver
from ._drivers import Lists, argument_value, driver_for, job_file
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
    #: ``script_root``, ``project``, ``path`` (more than the file name matched), ``name``, or
    #: ``driver`` (the task runs a driver whose job list names this job).
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
    #: ``(alias, kind)`` of each Spark catalog any file of the scan sets to a kind of catalog.
    catalogs: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class DagScan:
    root: Path
    dags: List[Dag] = field(default_factory=list)
    files: int = 0
    notes: List[str] = field(default_factory=list)
    unreadable: Dict[str, str] = field(default_factory=dict)
    #: ``(alias, kind)`` of each Spark catalog any DAG file sets to a kind of catalog.
    catalogs: List[Tuple[str, str]] = field(default_factory=list)


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
        airflow_variables=conventions.airflow_variables,
        extra=conventions.config_modules,
        budget=budget,
    )
    sessions = set(DEFAULT_SESSION_NAMES) | set(conventions.session_names)
    path_rules = tuple((rule.prefix, rule.table is not None) for rule in conventions.paths)
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
        folder = index.folder(module)
        facts = read_job(
            module,
            folder,
            helpers=conventions.helpers,
            session_names=sessions,
            shown=shown,
            path_rules=path_rules,
        )
        scan.catalogs.extend(module_settings(module, folder))
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
        airflow_variables=conventions.airflow_variables,
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
        folder = index.folder(module)
        found = read_dags(module, folder, operators=conventions.operators)
        scan.catalogs.extend(module_settings(module, folder))
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


def _driven(
    driver: Driver,
    submit: Submit,
    lists: Lists,
    by_path: Dict[Path, JobRecord],
    notes: Optional[List[str]],
    reported: set,
) -> "List[JobRecord] | str":
    """The job files the job list a driver is given names, or why the task is not joined."""
    value = argument_value(driver, submit.args)
    if isinstance(value, tuple):
        return value[0]
    names = lists.names(driver, value)
    if isinstance(names, str):
        return names
    found: Dict[Path, JobRecord] = {}
    missing: List[str] = []
    for name in dict.fromkeys(names):
        job = by_path.get(job_file(driver, name))
        if job is None:
            missing.append(name)
        else:
            found.setdefault(job.path, job)
    if missing and notes is not None and (driver.script, value) not in reported:
        reported.add((driver.script, value))
        shown = ", ".join(clean_chars(m) for m in missing[:10]) + (
            ", ..." if len(missing) > 10 else ""
        )
        notes.append(
            f"the job list {clean_chars(value)} names {len(missing)} job(s) with no job file that "
            f"does Spark reads or writes here: {shown}"
        )
    if not found:
        return f"no job of the list {clean_chars(value)} is a job file read here"
    return list(found.values())


class _Resolver:
    """What a submit runs: the job files it names and how it was matched, or why it is not joined.
    A script text answers the same every time (the tasks of a project share a few), and so does a
    driver run, so each is worked out once."""

    def __init__(
        self,
        jobs: Sequence[JobRecord],
        conventions: Conventions,
        project_root: Optional[Path],
        notes: Optional[List[str]],
        budget: Optional[SourceBudget],
    ) -> None:
        self._conventions = conventions
        self._project_root = project_root
        self._notes = notes
        self._by_path = {job.path: job for job in jobs}
        self._by_name: Dict[str, List[JobRecord]] = {}
        for job in jobs:
            self._by_name.setdefault(job.rel.rsplit("/", 1)[-1], []).append(job)
        self._lists = Lists(budget) if conventions.drivers else None
        self._answers: Dict[str, "Tuple[JobRecord, str] | str"] = {}
        self._driven: Dict[Tuple[str, Tuple[str, ...]], "List[JobRecord] | str"] = {}
        self._reported: set = set()

    def resolve(self, submit: Submit) -> "Tuple[Sequence[JobRecord], str] | str":
        script = submit.script
        if script is None:
            return submit.why or "no script"
        driver = driver_for(script, self._conventions.drivers) if self._lists else None
        if driver is not None and self._lists is not None:
            key = (script, submit.args)
            runs = self._driven.get(key)
            if runs is None:
                runs = self._driven[key] = _driven(
                    driver, submit, self._lists, self._by_path, self._notes, self._reported
                )
            return runs if isinstance(runs, str) else (runs, "driver")
        found = self._answers.get(script)
        if found is None:
            found = self._answers[script] = _match(
                script, self._by_name, self._by_path, self._conventions, self._project_root
            )
        return found if isinstance(found, str) else ([found[0]], found[1])


def link_tasks(
    dags: Sequence[Dag],
    scans: Sequence[JobScan],
    conventions: Conventions,
    project_root: Optional[Path],
    notes: Optional[List[str]] = None,
    budget: Optional[SourceBudget] = None,
) -> Tuple[List[LinkedTask], List[Tuple[Dag, Task, Submit, str]]]:
    """Join every submit of every task to the job files of ``scans`` it runs. Returns ``(linked,
    unlinked)``; an unlinked submit comes with why. A job records at most
    :data:`MAX_TASKS_PER_JOB` tasks (the rest are counted in ``notes``). ``budget`` is what the
    job lists the conventions' ``drivers`` name are read against."""
    resolver = _Resolver(
        [job for scan in scans for job in scan.jobs], conventions, project_root, notes, budget
    )
    over: Dict[str, int] = {}
    linked: List[LinkedTask] = []
    unlinked: List[Tuple[Dag, Task, Submit, str]] = []
    for dag in dags:
        for task in dag.tasks:
            for submit in task.submits:
                found = resolver.resolve(submit)
                if isinstance(found, str):
                    unlinked.append((dag, task, submit, found))
                    continue
                targets, how = found
                for job in targets:
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
