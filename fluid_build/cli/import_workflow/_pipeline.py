# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The import pipeline behind ``fluid import airflow`` and ``fluid import pyspark``.

Scan the code, join DAG tasks to the job files they run, propose products
(:mod:`._proposal`), report. Everything is static: no file is imported or run, and the only
network access is the optional schema-registry read for a topic's columns, which happens only
when ``--registry`` is given.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ._conventions import Conventions, load_conventions
from ._proposal import (
    SPLITS,
    ProposalTooLarge,
    TopicSchema,
    build_proposal,
    safe_command,
    safe_text,
)
from ._pysource import SourceBudget
from ._scan import DagScan, JobScan, LinkedTask, budget_note, link_tasks, scan_dags, scan_jobs
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


def _referenced_roots(dag_scan: DagScan, conv: Conventions) -> Tuple[List[Path], List[str]]:
    """The local directories the DAGs' scripts live in, by the conventions' ``script_roots``:
    only those a task actually submits from, outer directories before the ones inside them."""
    used: Dict[Path, None] = {}
    missing: List[str] = []
    for dag in dag_scan.dags:
        for task in dag.tasks:
            for submit in task.submits:
                if not submit.script:
                    continue
                for prefix, directory in conv.script_roots:
                    if submit.script.startswith(prefix):
                        if directory.is_dir():
                            used.setdefault(directory, None)
                        elif str(directory) not in missing:
                            missing.append(str(directory))
                        break
    roots = sorted(used, key=lambda p: len(p.parts))
    kept: List[Path] = []
    for root in roots:
        if not any(root == other or other in root.parents for other in kept):
            kept.append(root)
    return kept, missing


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


def run_import(
    tool: str, source: str, options: Mapping[str, Any]
) -> Tuple[List[Dict[str, Any]], ImportReport]:
    """The contracts ``fluid import <tool> <source>`` proposes, and the report."""
    split_by = _split(tool, options)
    conv = load_conventions(options.get("conventions"))
    report = ImportReport()
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
                dag_scan.dags, scans, conv, project_root=dags_root.resolve().parent
            )
    else:
        dag_scan = scan_dags(root, conv, budget)
        roots, missing = _referenced_roots(dag_scan, conv)
        for path in missing:
            report.unsupported.append(
                f"script_roots names the directory {safe_text(path, 100)}, which does not exist"
            )
        scans = [scan_jobs(directory, conv, budget) for directory in roots]
        linked, unlinked = link_tasks(dag_scan.dags, scans, conv, project_root=None)

    _scan_report(tool, scans, dag_scan, linked, unlinked, report)
    budget_note(budget.skipped, report.notes)
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
        )
    except ProposalTooLarge as exc:
        raise refusal(
            f"too much to propose from {safe_text(str(root), 80)!r}",
            f"{exc}, which is more than a proposal holds: every product is validated and written "
            "as a file.",
            "Import a smaller directory (one domain at a time), or ignore generated code under "
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


def _check_valid(contracts: Sequence[Dict[str, Any]], report: ImportReport) -> None:
    """Refuse, before anything is written, a proposal the schema does not accept.

    One exception: the DAG tasks recorded under a build are what the schema's own task rules are
    strictest about (they key on the operator's name). A proposal that is valid without them is
    kept without them, and the report says so.
    """
    from fluid_build.schema_manager import FluidSchemaManager

    manager = FluidSchemaManager()
    for contract in contracts:
        version = contract["fluidVersion"]
        result = manager.validate_contract(contract, schema_version=version, offline_only=True)
        if result.is_valid:
            continue
        errors = "; ".join(result.errors)
        if _drop_tasks(contract):
            retry = manager.validate_contract(contract, schema_version=version, offline_only=True)
            if retry.is_valid:
                report.unsupported.append(
                    f"{contract['id']}: the DAG tasks are not recorded on its builds, because the "
                    f"schema refuses them ({safe_text(errors, 200)})"
                )
                continue
            errors = "; ".join(retry.errors)
        raise refusal(
            f"the proposal for {safe_text(contract['id'], 80)} is not a valid contract",
            safe_text(errors, 600),
            "This is a bug in the importer or a value in the conventions the schema refuses: "
            "check the conventions file, or report it with the DAG or job that causes it.",
            product=safe_text(contract["id"], 120),
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
        by_how: Dict[str, int] = {}
        for link in linked:
            by_how[link.how] = by_how.get(link.how, 0) + 1
            if link.how == "name":
                report.unsupported.append(
                    f"DAG {safe_text(link.dag.shown, 60)} task {safe_text(link.task.shown, 60)} submits "
                    f"{safe_command(link.submit.script or '', 100)}: joined to a job file by file name only"
                )
        report.notes.append(
            f"{len(linked)} submitted script(s) joined to a job file"
            + (
                f" ({', '.join(f'{n} by {how}' for how, n in sorted(by_how.items()))})"
                if by_how
                else ""
            )
            + f"; {len(unlinked)} not joined"
        )
        for dag, task, submit, why in unlinked:
            line = (
                f"DAG {safe_text(dag.shown, 60)} task {safe_text(task.shown, 60)} "
                f"({task.file}:{submit.line}) submits "
                f"{safe_command(submit.script or submit.shown or '(no script)', 100)}: not joined ({safe_text(why, 120)})"
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
        no_dag = [j for j in jobs if not j.tasks]
        if tool == "pyspark" and no_dag:
            report.notes.append(f"{len(no_dag)} job(s) are submitted by no DAG task read here")
