# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The import pipeline behind ``fluid import airflow`` and ``fluid import pyspark``.

Scan the code, join DAG tasks to the job files they run, propose products
(:mod:`._proposal`), report. Everything is static: no file is imported or run, and the only
network access is the optional schema-registry read for a topic's columns, which happens only
when ``--registry`` is given.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ._catalogs import CatalogKinds
from ._conventions import Conventions, load_conventions
from ._fold import missing_value
from ._proposal import (
    SPLITS,
    ProposalTooLarge,
    TopicSchema,
    build_proposal,
    safe_text,
)
from ._pysource import SourceBudget
from ._redact import sensitive_name
from ._scan import (
    DagScan,
    JobScan,
    LinkedTask,
    budget_note,
    link_tasks,
    path_is,
    scan_dags,
    scan_jobs,
)
from .registry import Importer, ImportReport

DOC = "https://agenticstiger.github.io/forge_docs/cli/import.html"


@dataclass
class CodeImporter(Importer):
    """What ``fluid import airflow`` and ``fluid import pyspark`` share: they read a directory,
    propose products with :func:`run_import`, and never replace a proposal a person has edited
    (``--force`` does)."""

    #: The tool name ``run_import`` is called with.
    tool: ClassVar[str] = ""
    name: str = ""
    guards_overwrite: bool = True

    def can_import(self, source: str) -> bool:
        return Path(source).is_dir()

    def import_to_contracts(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> Tuple[List[Dict[str, Any]], ImportReport]:
        return run_import(self.tool, source, options or {})

    def import_to_contract(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> Tuple[Dict[str, Any], ImportReport]:
        """The single-contract protocol: everything in one product (``project`` boundary)."""
        merged = dict(options or {})
        merged["split_by"] = "project"
        contracts, report = self.import_to_contracts(source, options=merged)
        return (contracts[0] if contracts else {}), report


def refusal(what: str, why: str, fix: str, **extras: Any) -> Exception:
    from fluid_build._errors import SchemaValidationError

    return SchemaValidationError(what=what, why=why, fix=fix, doc=DOC, extras=extras)


def contract_version() -> str:
    """The ``fluidVersion`` a proposal declares: the newest stable bundled schema."""
    from fluid_build.schema_manager import FluidSchemaManager

    return FluidSchemaManager.latest_bundled_version()


def _split(tool: str, options: Mapping[str, Any]) -> str:
    value = str(options.get("split_by") or "table").strip().lower()
    if value not in SPLITS:
        raise refusal(
            f"--split-by {value} does not apply to `fluid import {tool}`",
            f"`{tool}` proposes products by {', '.join(SPLITS)}; `group` is a dbt boundary.",
            f"Use one of: {' | '.join(SPLITS)} (the default is table: one product per output).",
            split_by=value,
        )
    return value


def _directory(path: str, what: str, flag: str) -> Path:
    directory = Path(path)
    if not directory.is_dir():
        raise refusal(
            f"{what} {safe_text(path, 80)!r} is not a directory",
            f"`{flag}` reads the Python files under a directory.",
            f"Pass the directory that holds the {what.split()[0].lower()} files.",
            path=safe_text(path, 120),
        )
    return directory


def _named_directories(tool: str, source: str, options: Mapping[str, Any]) -> List[str]:
    """The directories the user named on the command line, which a path in the conventions file
    may reach into as well as the file's own: the one to scan and, for pyspark, ``--dags``. The
    conventions are read before these are checked, so each is only a name here."""
    named = [source]
    if tool == "pyspark" and options.get("dags"):
        named.append(str(options["dags"]))
    return named


def _referenced_roots(dag_scan: DagScan, conv: Conventions) -> List[Path]:
    """The local directories the DAGs' scripts live in, by the conventions' ``script_roots``:
    only those a task actually submits from (and that exist: one that does not is reported by
    :func:`_report_conventions_paths`), outer directories before the ones inside them."""
    used: Dict[Path, None] = {}
    for dag in dag_scan.dags:
        for task in dag.tasks:
            for submit in task.submits:
                if not submit.script:
                    continue
                for prefix, directory in conv.script_roots:
                    if submit.script.startswith(prefix):
                        if path_is(directory, True):
                            used.setdefault(directory, None)
                        break
    roots = sorted(used, key=lambda p: len(p.parts))
    kept: List[Path] = []
    for root in roots:
        if not any(root == other or other in root.parents for other in kept):
            kept.append(root)
    return kept


#: Directories above the ones named on the command line looked in for a path of the conventions
#: file that names nothing where it was written: the folder that holds the repositories, often.
_HINT_LEVELS = 4
#: Tasks named on the line of a ``script_roots`` entry that names nothing.
MAX_PATH_TASKS = 3


def _where_it_is(written: str, named: Sequence[Path], want_dir: bool) -> Optional[Path]:
    """The folder above a directory named on the command line (or that directory) from which the
    path ``written`` names a directory (``want_dir``) or a file: where the conventions file was
    written for. Only ``is_dir``/``is_file`` is asked, of paths below the named directories'
    parents; nothing is read."""
    if not written or written.startswith("..") or Path(written).is_absolute():
        return None
    seen: Dict[Path, None] = {}
    for directory in named:
        for above in [directory, *list(directory.parents)[:_HINT_LEVELS]]:
            if above in seen:
                continue
            seen[above] = None
            if path_is(above / written, want_dir):
                return above
    return None


def _shown(path: Path) -> str:
    """A path as the report shows it: from the working directory when it is under it."""
    try:
        text = path.relative_to(Path.cwd().resolve()).as_posix() or "."
    except (ValueError, OSError):
        text = str(path)
    return safe_text(text, 400)


def _report_conventions_paths(
    conv: Conventions, dag_scan: Optional[DagScan], named: Sequence[Path], report: ImportReport
) -> None:
    """A line for each directory or file the conventions name that is not there (a
    ``script_roots`` directory, a ``config_modules`` file, a ``drivers`` directory): a path in the
    file is relative to the file's own folder, so a file moved away from the repositories it
    describes, or written for another folder, names nothing and the joins it was meant for are
    not made. The line names the entry, the path as written and as resolved, the tasks it leaves
    unjoined, and, when the path names something from a folder above a directory named on the
    command line, that folder."""
    if conv.source is None:
        return
    home = conv.source.resolve().parent
    name = safe_text(conv.source.name, 80)
    uses: Dict[str, Dict[str, None]] = {}
    for dag in dag_scan.dags if dag_scan is not None else ():
        for task in dag.tasks:
            for submit in task.submits:
                for prefix, _directory in conv.script_roots if submit.script else ():
                    if (submit.script or "").startswith(prefix):
                        label = f"DAG {safe_text(dag.shown, 60)} task {safe_text(task.shown, 60)}"
                        uses.setdefault(prefix, {})[label] = None
                        break

    def line(entry: str, target: Path, want_dir: bool, consequence: str) -> str:
        try:
            written = os.path.relpath(target, home)
        except ValueError:  # another drive
            written = str(target)
        found = _where_it_is(written, named, want_dir)
        kind = "directory" if want_dir else "file"
        fix = (
            f"`{safe_text(written, 120)}` is in {_shown(found)}: put the conventions file there, "
            "in the folder that holds the repositories it names"
            if found is not None
            else "write the path from the conventions file's folder"
        )
        return (
            f"{entry} in {name} names `{safe_text(written, 120)}`, which is {_shown(target)}: no "
            f"such {kind}. A path in the conventions file is relative to the file's own folder "
            f"({_shown(home)}){consequence}. "
            f"{fix}, or name the {kind} on the command line (`fluid import pyspark <jobs-dir> "
            "--dags <dags-dir>` names the jobs directory)"
        )

    for prefix, directory in conv.script_roots:
        if path_is(directory, True):
            continue
        tasks = list(uses.get(prefix, {}))
        consequence = ""
        if tasks:
            shown = ", ".join(tasks[:MAX_PATH_TASKS])
            if len(tasks) > MAX_PATH_TASKS:
                shown += f", and {len(tasks) - MAX_PATH_TASKS} more"
            consequence = (
                f"; {len(tasks)} task(s) submit a script under {safe_text(prefix, 120)} and are "
                f"not joined through it ({shown})"
            )
        report.unsupported.append(
            line(f"script_roots `{safe_text(prefix, 120)}`", directory, True, consequence)
        )
    for module, path in conv.config_modules.items():
        if not path_is(path, False):
            report.unsupported.append(
                line(
                    f"config_modules `{safe_text(module, 120)}`",
                    path,
                    False,
                    "; what the scanned code imports from it is not read",
                )
            )
    for index, driver in enumerate(conv.drivers):
        for what, directory in (("job_lists", driver.lists_dir), ("jobs", driver.jobs_dir)):
            if not path_is(directory, True):
                report.unsupported.append(
                    line(
                        f"drivers[{index}].{what}.dir",
                        directory,
                        True,
                        f"; a task that runs {safe_text(driver.script, 80)} is not joined to its jobs",
                    )
                )


def topic_schema_reader(options: Mapping[str, Any]) -> Optional[TopicSchema]:
    """A reader of topic columns from the schema registry the user named, or ``None``.

    Reuses :class:`SchemaRegistryImporter` (subject ``<topic>-value``, then ``<topic>``). A topic
    whose subject cannot be read gets no columns and the reason; nothing aborts the import.
    """
    kind = options.get("registry")
    if kind is None:
        return None
    from fluid_build._errors import FluidUserError

    from .schema_registry import SchemaRegistryImporter

    importer = SchemaRegistryImporter()
    registry_options = {
        key: options.get(key) for key in ("registry", "url", "registry_name") if options.get(key)
    }
    cache: Dict[str, Union[List[Dict[str, Any]], str]] = {}

    def read(topic: str) -> Union[List[Dict[str, Any]], str]:
        if topic in cache:
            return cache[topic]
        first: Optional[str] = None
        result: Union[List[Dict[str, Any]], str] = "no subject was read"
        for subject in (f"{topic}-value", topic):
            try:
                contract, _report = importer.import_to_contract(subject, options=registry_options)
            except FluidUserError as exc:
                first = first or exc.what
                continue
            except (
                Exception
            ) as exc:  # noqa: BLE001 - a registry that is down must not stop the import
                first = first or type(exc).__name__
                continue
            columns = contract["exposes"][0]["contract"]["schema"]
            result = columns if columns else "the subject declares no fields"
            first = None
            break
        if first is not None:
            result = f"schema registry: {first}"
        cache[topic] = result
        return result

    return read


#: Conventions values left out for their names, listed one by one in the report; the rest counted.
MAX_WITHHELD_NAMES = 10


def run_import(
    tool: str, source: str, options: Mapping[str, Any]
) -> Tuple[List[Dict[str, Any]], ImportReport]:
    """The contracts ``fluid import <tool> <source>`` proposes, and the report."""
    split_by = _split(tool, options)
    conv = load_conventions(
        options.get("conventions"), roots=_named_directories(tool, source, options)
    )
    report = ImportReport()
    if conv.withheld:
        shown = ", ".join(safe_text(name, 80) for name in conv.withheld[:MAX_WITHHELD_NAMES])
        more = len(conv.withheld) - MAX_WITHHELD_NAMES
        report.required_defaults.append(
            f"{len(conv.withheld)} value(s) in the conventions are not used, because their names "
            f"say they are credentials ({shown}{f', and {more} more' if more > 0 else ''}): "
            "the code's own reference (`{{ var.value.name }}`, `Variable.get(...)`, "
            "`os.environ[...]`) is kept as written, so set the real value where the job runs "
            "(a secret store or the environment), not in a contract"
        )
    root = _directory(
        source, "DAGs directory" if tool == "airflow" else "jobs directory", f"fluid import {tool}"
    )

    budget = SourceBudget()  # the source this import reads, in all its scans
    scans: List[JobScan] = []
    dag_scan: Optional[DagScan] = None
    linked: List[LinkedTask] = []
    unlinked: List[Tuple[Any, Any, Any, str]] = []

    if tool == "pyspark":
        jobs_scan = scan_jobs(root, conv, budget)
        scans = [jobs_scan]
        dags_dir = options.get("dags")
        if dags_dir:
            dags_root = _directory(str(dags_dir), "DAGs directory", "--dags")
            dag_scan = scan_dags(dags_root, conv, budget)
            linked, unlinked = link_tasks(
                dag_scan.dags,
                scans,
                conv,
                project_root=dags_root.resolve().parent,
                notes=report.notes,
                budget=budget,
            )
    else:
        dag_scan = scan_dags(root, conv, budget)
        roots = _referenced_roots(dag_scan, conv)
        scans = [scan_jobs(directory, conv, budget) for directory in roots]
        linked, unlinked = link_tasks(
            dag_scan.dags, scans, conv, project_root=None, notes=report.notes, budget=budget
        )

    named = [Path(p).resolve() for p in _named_directories(tool, source, options)]
    _report_conventions_paths(conv, dag_scan, named, report)
    _scan_report(tool, scans, dag_scan, linked, unlinked, report)
    budget_note(budget.skipped, report.notes, budget.sql_skipped)
    jobs = [job for scan in scans for job in scan.jobs]
    if not jobs and not any(scan.other_files for scan in scans) and tool == "pyspark":
        raise refusal(
            f"no Python files under {safe_text(str(root), 80)!r}",
            "A PySpark job is a Python file; the directory holds none (tests, build output and "
            "hidden directories are skipped).",
            "Pass the directory that holds the jobs, or check --conventions' ignore.files.",
            path=safe_text(str(root), 120),
        )

    try:
        proposal = build_proposal(
            tool=tool,
            version=contract_version(),
            jobs=jobs,
            conv=conv,
            split_by=split_by,
            root_name=root.resolve().name,
            topic_schema=topic_schema_reader(options),
            report=report,
            catalogs=CatalogKinds.of(
                *(scan.catalogs for scan in scans), dag_scan.catalogs if dag_scan else []
            ),
        )
    except ProposalTooLarge as exc:
        raise refusal(
            f"too much to propose from {safe_text(str(root), 80)!r}",
            f"{exc}, which is more than a proposal holds: every product is validated and written "
            "as a file.",
            exc.hint
            or "Import a smaller directory (one domain at a time), or ignore generated code under "
            "`ignore.files` in the conventions.",
            path=safe_text(str(root), 120),
        ) from exc
    _check_valid(proposal.contracts, proposal.report)
    return proposal.contracts, proposal.report


def _drop_tasks(contract: Dict[str, Any]) -> bool:
    """Remove the recorded DAG tasks from the builds of ``contract``; True if there were any."""
    dropped = False
    for build in contract.get("builds") or []:
        airflow = ((build.get("execution") or {}).get("orchestration") or {}).get("airflow")
        if isinstance(airflow, dict) and airflow.pop("tasks", None) is not None:
            dropped = True
    return dropped


#: ``fluid validate`` findings listed one by one in the report; the rest are counted, and the
#: products they are on are named (up to ``MAX_UNLISTED_PRODUCTS`` of them).
MAX_RULE_LINES = 100
MAX_UNLISTED_PRODUCTS = 30


def _check_valid(contracts: Sequence[Dict[str, Any]], report: ImportReport) -> None:
    """Refuse, before anything is written, a proposal the schema does not accept, and report what
    ``fluid validate``'s contract rules find in the rest.

    A schema error is a bug in the importer or a value in the conventions: nothing is written. A
    rule finding is not always one: a Databricks or Snowflake binding asks for columns, a schema or
    a warehouse that no code holds, so the contract is written and the report says what
    ``fluid validate`` will ask a person for (one product in a directory of hundreds must not
    stop the other ones).

    One exception to the first: the DAG tasks recorded under a build are what the schema's own task
    rules are strictest about (they key on the operator's name). A proposal that is valid without
    them is kept without them, and the report says so.
    """
    from fluid_build.cli.validate import contract_rule_findings
    from fluid_build.schema_manager import FluidSchemaManager

    manager = FluidSchemaManager()  # one for every product

    def schema(contract: Dict[str, Any]) -> Any:
        return manager.validate_contract(
            contract, schema_version=contract["fluidVersion"], offline_only=True
        )

    listed = more = 0
    unlisted: Dict[str, None] = {}  # the products with findings that are not listed, in order
    for contract in contracts:
        result = schema(contract)
        if not result.is_valid:
            errors = "; ".join(result.errors)
            if _drop_tasks(contract):
                retry = schema(contract)
                if retry.is_valid:
                    report.unsupported.append(
                        f"{contract['id']}: the DAG tasks are not recorded on its builds, because "
                        f"the schema refuses them ({safe_text(errors, 200)})"
                    )
                    result = retry
                else:
                    errors = "; ".join(retry.errors)
            if not result.is_valid:
                raise refusal(
                    f"the proposal for {safe_text(contract['id'], 80)} is not a valid contract",
                    safe_text(errors, 600),
                    "This is a bug in the importer or a value in the conventions the schema "
                    "refuses: check the conventions file, or report it with the DAG or job that "
                    "causes it.",
                    product=safe_text(contract["id"], 120),
                )
        # no plugin validators: they are the project's own rules about blocks this command never
        # writes, and finding them costs about 0.1 s a contract
        for message in contract_rule_findings(contract, manager=manager, plugins=False).errors:
            if listed >= MAX_RULE_LINES:
                more += 1
                unlisted[str(contract["id"])] = None
                continue
            listed += 1
            report.required_defaults.append(
                f"{safe_text(contract['id'], 120)}: `fluid validate` reports an error until a "
                f"person completes it: {safe_text(message, 400)}"
            )
    if more:
        names = ", ".join(safe_text(pid, 80) for pid in list(unlisted)[:MAX_UNLISTED_PRODUCTS])
        rest = len(unlisted) - MAX_UNLISTED_PRODUCTS
        report.required_defaults.append(
            f"{more} more `fluid validate` finding(s) are not listed, on {len(unlisted)} "
            f"product(s): {names}" + (f" and {rest} more" if rest > 0 else "")
        )


def _report_joins(linked: Sequence[LinkedTask], not_joined: int, report: ImportReport) -> None:
    """The note on how many submitted scripts were joined to a job file, and by what, and a line
    for each task joined by the file name alone."""
    by_how: Dict[str, int] = {}
    seen_submits: set = set()
    for link in linked:
        if id(link.submit) not in seen_submits:  # a driver's one script joins several jobs
            seen_submits.add(id(link.submit))
            by_how[link.how] = by_how.get(link.how, 0) + 1
        if link.how == "name":
            report.unsupported.append(
                f"DAG {safe_text(link.dag.shown, 60)} task {safe_text(link.task.shown, 60)} submits "
                f"{safe_text(link.submit.script or '', 100)}: joined to a job file by file name only"
            )
    through_driver = sum(1 for link in linked if link.how == "driver")
    report.notes.append(
        f"{len(seen_submits)} submitted script(s) joined to a job file"
        + (
            f" ({', '.join(f'{n} by {how}' for how, n in sorted(by_how.items()))})"
            if by_how
            else ""
        )
        + (f"; {through_driver} job file(s) run through a driver" if through_driver else "")
        + f"; {not_joined} not joined"
    )


def _report_unlinked(
    tool: str, unlinked: Sequence[Tuple[Any, Any, Any, str]], report: ImportReport
) -> None:
    """A line for each task whose script was not joined to a job file."""
    for dag, task, submit, why in unlinked:
        line = (
            f"DAG {safe_text(dag.shown, 60)} task {safe_text(task.shown, 60)} "
            f"({task.file}:{submit.line}) submits "
            f"{safe_text(submit.script or submit.shown or '(no script)', 100)}: not joined ({safe_text(why, 120)})"
        )
        if tool == "airflow" and submit.script:
            report.unsupported.append(
                line + "; map its prefix under `script_roots` in the conventions, or run "
                "`fluid import pyspark <jobs-dir> --dags <dags-dir>`"
            )
        elif tool == "airflow":
            report.unsupported.append(line)
        else:
            report.notes.append(line)


#: Functions Python tasks run that are not read, named one by one in the report; the rest counted.
MAX_UNREAD_CALLABLES = 10
#: Values submitted scripts and config files wait for, listed one by one; the rest counted. Each
#: line names at most ``MAX_WAITING_TASKS`` of the tasks that wait for it.
MAX_WAITING_LINES = 20
MAX_WAITING_TASKS = 3
_WAITING_HINT = {
    "module": "name the file of its module under `config_modules` in the conventions",
    "environment": "set what it holds under `environments` in the conventions",
    "airflow_variable": "set what it holds under `airflow_variables` in the conventions",
}


def _report_unread_callables(dag_scan: DagScan, report: ImportReport) -> None:
    """The note on the functions Python tasks run that are not read: the ones that submit Spark
    work can be described in the conventions."""
    unread = dag_scan.unread_callables
    if not unread:
        return
    ranked = sorted(unread.items(), key=lambda item: (-item[1], item[0]))
    shown = ", ".join(
        f"{safe_text(name, 80)} ({count})" for name, count in ranked[:MAX_UNREAD_CALLABLES]
    )
    more = len(ranked) - MAX_UNREAD_CALLABLES
    report.notes.append(
        f"{sum(unread.values())} Python task(s) run a function that is not read here: {shown}"
        + (f", and {more} more" if more > 0 else "")
        + ". Name one that submits Spark work under `submit_helpers` in the conventions, with "
        "where its script is among its arguments"
    )


def _waiting_for(submit: Any) -> List[Tuple[str, str]]:
    """The values a submit's script and config file wait for that the conventions can give. A name
    that says it is a credential is left out: the conventions do not use such a value."""
    whys = (submit.why if submit.script is None else "", submit.config_why)
    found = [missing_value(why) for why in whys if why]
    return [m for m in found if m is not None and not sensitive_name(m[1].rsplit(".", 1)[-1])]


def _waiting_line(kind: str, name: str, tasks: Sequence[str]) -> str:
    what = {
        "module": f"`{safe_text(name, 120)}`, which is not in the scanned files",
        "environment": f"the environment variable {safe_text(name, 80)}",
        "airflow_variable": f"the Airflow Variable {safe_text(name, 80)}",
    }[kind]
    named = ", ".join(tasks[:MAX_WAITING_TASKS])
    if len(tasks) > MAX_WAITING_TASKS:
        named += f", and {len(tasks) - MAX_WAITING_TASKS} more"
    return (
        f"{len(tasks)} task(s) submit a script or name a config file that depends on {what} "
        f"({named}): {_WAITING_HINT[kind]}"
    )


def _report_waiting(dag_scan: DagScan, report: ImportReport) -> None:
    """A line for each value that a submitted script or a config file waits for and the conventions
    can give (a constant of a module outside the scan, an environment variable, an Airflow
    Variable), with the tasks that wait for it."""
    waiting: Dict[Tuple[str, str], Dict[str, None]] = {}
    for dag in dag_scan.dags:
        for task in dag.tasks:
            label = f"DAG {safe_text(dag.shown, 60)} task {safe_text(task.shown, 60)}"
            for submit in task.submits:
                for missing in _waiting_for(submit):
                    waiting.setdefault(missing, {})[label] = None
    ranked = sorted(waiting.items(), key=lambda item: (-len(item[1]), item[0]))
    for (kind, name), tasks in ranked[:MAX_WAITING_LINES]:
        report.required_defaults.append(_waiting_line(kind, name, list(tasks)))
    if len(ranked) > MAX_WAITING_LINES:
        report.required_defaults.append(
            f"{len(ranked) - MAX_WAITING_LINES} more value(s) that submitted scripts or config "
            "files depend on are not listed"
        )


def _scan_report(
    tool: str,
    scans: Sequence[JobScan],
    dag_scan: Optional[DagScan],
    linked: Sequence[LinkedTask],
    unlinked: Sequence[Tuple[Any, Any, Any, str]],
    report: ImportReport,
) -> None:
    jobs = [job for scan in scans for job in scan.jobs]
    for scan in scans:
        read = len(scan.jobs) + scan.other_files + len(scan.unreadable)
        report.notes.append(
            f"read {read} Python file(s) under {safe_text(str(scan.root), 100)}: "
            f"{len(scan.jobs)} do Spark reads or writes (jobs), {scan.other_files} do not"
        )
        report.notes.extend(f"{safe_text(n, 200)}" for n in scan.notes)
        for rel, why in sorted(scan.unreadable.items()):
            report.unsupported.append(f"{safe_text(rel, 100)}: not read ({safe_text(why, 120)})")
    for job in jobs:
        for note in job.facts.notes:
            report.unsupported.append(safe_text(note, 300))
        report.notes.extend(safe_text(n, 300) for n in job.facts.remarks)

    if dag_scan is not None:
        tasks = [t for d in dag_scan.dags for t in d.tasks]
        submitting = [t for t in tasks if t.submits]
        report.notes.append(
            f"read {dag_scan.files} DAG file(s) under {safe_text(str(dag_scan.root), 100)}: "
            f"{len(dag_scan.dags)} DAG(s), {len(tasks)} task(s), {len(submitting)} submit a script"
        )
        report.notes.extend(safe_text(n, 200) for n in dag_scan.notes)
        for rel, why in sorted(dag_scan.unreadable.items()):
            report.unsupported.append(f"{safe_text(rel, 100)}: not read ({safe_text(why, 120)})")
        for dag in dag_scan.dags:
            for note in dag.notes:
                report.unsupported.append(
                    f"DAG {safe_text(dag.shown, 60)} ({dag.file}:{dag.line}): {safe_text(note, 200)}"
                )
            for task in dag.tasks:
                for note in task.notes:
                    report.unsupported.append(
                        f"DAG {safe_text(dag.shown, 60)} task {safe_text(task.shown, 60)} "
                        f"({task.file}:{task.line}): {safe_text(note, 200)}"
                    )
        _report_joins(linked, len(unlinked), report)
        _report_unlinked(tool, unlinked, report)
        _report_unread_callables(dag_scan, report)
        _report_waiting(dag_scan, report)
        no_dag = [j for j in jobs if not j.tasks]
        if tool == "pyspark" and no_dag:
            report.notes.append(f"{len(no_dag)} job(s) are submitted by no DAG task read here")
