# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Propose FLUID data products from what jobs and DAGs say.

The rules, in one place:

* **One product per output.** Each table, path or Kafka topic a job writes is a product
  (``--split-by table``, the default). ``job`` makes one product per job file, ``dag`` one per
  Airflow DAG, ``folder`` one per top-level folder of the jobs directory and ``project`` one for
  everything: the coarser splits are for teams that think of a pipeline, not a table, as the
  product.
* **A single writer.** An output written by two job files is proposed once, with the first
  writer by path, and the report says so (``unsupported``): one product has one writer, and
  which job to keep is a decision for a person.
* **One owner.** From the owner of the tasks that submit the job (the task's own ``owner``, else
  the DAG's ``default_args``), mapped through the conventions' ``owners``. Differing owners are
  reported and the first (by DAG, then task) is used; none found is defaulted and reported.
* **Reads become ``consumes[]``.** A table or topic another proposed product writes is a
  ``consumes`` entry for that product. One nobody in the scanned code writes is a ``consumes``
  entry too, under the id the conventions' id rule gives it, so the product that later adopts it
  lines up; the report lists these. A job's reads of its own output, and temporary views, are
  not dependencies.
* **The job stays where it is.** Its build is a record, not something forge-cli runs:
  ``pattern: hybrid-reference``, ``engine: spark``, ``properties.model`` the script, a ``manual``
  trigger (``continuous`` for a long-running stream), and the DAG facts under
  ``execution.orchestration`` with ``mode: manual`` and ``generateOnChange: false``. A
  ``schedule`` trigger is never written: ``fluid generate schedule`` would render a second DAG
  for it, next to the one that already runs the job.
* **Nothing is invented.** Columns are not in the code, so ``contract.schema`` is empty with a
  TODO that ``fluid validate --strict`` reports (or the columns of the topic's registry subject,
  when ``--registry`` is given); a binding the code and conventions do not decide is ``other``.
"""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from ._airflow_dags import Dag, Task
from ._catalogs import CatalogKinds
from ._conventions import Conventions, PathRule
from ._redact import safe_args, safe_command, safe_text
from ._scan import JobRecord, LinkedTask
from ._spark_io import Ref, Write
from .registry import ImportReport

SPLITS = ("table", "job", "dag", "folder", "project")
#: Distinct outputs, and products, one proposal holds. Every product is validated against the
#: schema and written as a file; a project has hundreds, and a directory of generated code that
#: names a table per line is not what this reads.
MAX_OUTPUTS = 50_000
MAX_PRODUCTS = 10_000
#: Every product of a job lists the job's reads (the code does not say which read feeds which
#: output): a product lists at most ``MAX_CONSUMES_PER_PRODUCT`` of them (the rest are counted in
#: the report), a proposal lists ``MAX_FANOUT`` in all, and it goes through ``MAX_READ_VISITS``
#: reads in all (products times reads).
MAX_FANOUT = 200_000
MAX_READ_VISITS = 1_000_000
MAX_CONSUMES_PER_PRODUCT = 500
_SPLIT_BY_JOB = (
    "Propose one product per job with `--split-by job` (every product of a job repeats the job's "
    "reads and tasks), or import a smaller directory."
)
#: Every product of a job also lists the DAG tasks that submit the job, with their arguments: a
#: task lists at most this many arguments (the rest are counted in the report), a proposal writes
#: at most ``MAX_TASK_TEXT`` characters of tasks in all, and it goes through ``MAX_LINK_VISITS``
#: task links (products times the links of their jobs).
MAX_ARGS_PER_TASK = 100
MAX_TASK_TEXT = 16_000_000
MAX_LINK_VISITS = 1_000_000
#: Tasks of one product named one by one in the report; the rest are counted.
MAX_TASK_LINES = 20
#: Reads of one product named one by one in the report; the rest are counted.
MAX_READ_LINES = 20


class ProposalTooLarge(Exception):
    """The code writes more outputs, or makes more products, than one proposal holds. ``hint``
    is the way out that fits (the caller has a general one)."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


#: The id pattern of ``airflowOrchestration.dagId`` and ``airflowTask.taskId`` (lower case only).
_AIRFLOW_ID = re.compile(r"[a-z0-9_][a-z0-9_.-]*[a-z0-9_]|[a-z0-9_]")
_BAD = re.compile(r"[^a-z0-9_.-]+")
_SCHEME_PLATFORM = {
    "s3": "aws",
    "s3a": "aws",
    "s3n": "aws",
    "gs": "gcp",
    "abfs": "azure",
    "abfss": "azure",
}
_FORMATS = {
    "iceberg": "iceberg",
    "parquet": "parquet",
    "csv": "csv",
    "json": "json",
    "delta": "delta_table",
}
_SCHEME_FILE_FORMAT = {"aws": "s3_file", "gcp": "gcs_file"}
_RUNTIME_PLATFORM = {
    "emr-step": "aws",
    "emr-eks": "aws",
    "emr-serverless": "aws",
    "glue": "aws",
    "databricks": "databricks",
}


#: Longest identifier written: a table or job name from the code is not trusted to be short.
_MAX_IDENT = 120


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def ident(text: str, fallback: str = "x") -> str:
    """``text`` as a FLUID identifier: lower case, ``[a-z0-9_.-]``, starting and ending with a
    letter, digit or underscore, at most 120 characters. Accents are folded (``café`` is
    ``cafe``); a name with no ASCII letter or digit in it, or one that is cut, ends in a short
    digest of the text, so two such names stay two names."""
    raw = str(text).strip()
    folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    cleaned = _BAD.sub("-", folded.lower())
    cleaned = re.sub(r"\.{2,}", ".", re.sub(r"-{2,}", "-", cleaned)).strip(".-")
    if not cleaned:
        return f"{fallback}-{_digest(raw)}" if raw else fallback
    if len(cleaned) > _MAX_IDENT:
        cleaned = cleaned[: _MAX_IDENT - 9].rstrip(".-") + "-" + _digest(raw)
    return cleaned


class _Names(set):
    """Names handed out, and for each base the number to try next: ten thousand products that
    share a name do not each count up from 2."""

    def __init__(self) -> None:
        super().__init__()
        self.next: Dict[str, int] = {}


def _unique(base: str, taken: Set[str], separator: str) -> str:
    """``base``, or ``base`` with a number after it, whichever is not in ``taken`` yet: a name
    that already ends in ``_2`` is not a reason to hand ``_2`` out twice."""
    candidate = base
    counters = taken.next if isinstance(taken, _Names) else None
    number = counters.get(base, 2) if counters is not None else 2
    while candidate in taken:
        candidate = ident(f"{base}{separator}{number}")
        number += 1
    if counters is not None:
        counters[base] = number
    taken.add(candidate)
    return candidate


@dataclass(frozen=True)
class Identity:
    """What a table, path or topic is, once the conventions have said what they say."""

    kind: str  # "table" | "topic" | "path"
    name: str
    catalog: Optional[str] = None
    database: Optional[str] = None
    #: A path, for a table that lives at one (a path rule matched) and for a path.
    location: Optional[str] = None
    rule: Optional[PathRule] = None

    @property
    def key(self) -> Tuple[str, ...]:
        if self.kind == "table":
            return ("table", self.database or "", self.name)
        if self.kind == "topic":
            return ("topic", self.name)
        # case-sensitive: on an object store ``s3://b/Orders`` and ``s3://b/orders`` are two paths
        return ("path", self.location or self.name)

    @property
    def shown(self) -> str:
        if self.kind == "table":
            return ".".join(p for p in (self.catalog, self.database, self.name) if p)
        return self.location or self.name


def _normalise_path(path: str) -> str:
    return path.split("?")[0].split("#")[0].rstrip("/")


def _path_segments(path: str) -> List[str]:
    text = path.split("://", 1)[1] if "://" in path else path
    return [s for s in text.split("/") if s and s not in {".", ".."}]


def identity_of(ref: Ref, conv: Conventions) -> Optional[Identity]:
    """The identity of a resolved ref, or ``None`` for an unresolved one, one the conventions
    ignore, or a topic pattern."""
    if not ref.resolved:
        return None
    if ref.kind == "table":
        parts = list(ref.parts)
        if conv.ignores_table(parts):
            return None
        catalog: Optional[str] = None
        if len(parts) >= 3:
            catalog, database, name = parts[0], parts[-2], parts[-1]
        elif len(parts) == 2 and parts[0] in conv.catalogs:
            catalog, database, name = parts[0], conv.default_database, parts[1]
        elif len(parts) == 2:
            database, name = parts
        else:
            database, name = conv.default_database, parts[0]
        return Identity("table", name, catalog=catalog, database=database)
    if ref.kind == "topic":
        return Identity("topic", ref.name)
    if ref.kind == "path":
        path = _normalise_path(ref.name)
        if not path or conv.ignores_path(path):
            return None
        for rule in conv.paths:
            if path.startswith(rule.prefix.rstrip("/") + "/") or path == rule.prefix.rstrip("/"):
                rest = path[len(rule.prefix.rstrip("/")) :].strip("/")
                segment = rest.split("/")[0] if rest else ""
                table = rule.table or segment
                if not table:
                    return None
                where = rule.prefix.rstrip("/") + (
                    "/" + segment if segment and not rule.table else ""
                )
                return Identity(
                    "table",
                    table.lower(),
                    catalog=rule.catalog,
                    database=(rule.database or conv.default_database),
                    location=where,
                    rule=rule,
                )
        segments = _path_segments(path)
        if not segments:
            return None
        return Identity("path", ".".join(s.lower() for s in segments), location=path)
    return None


def product_id(identity: Identity, conv: Conventions) -> str:
    values = {
        "catalog": identity.catalog or "",
        "database": identity.database or "",
        "table": identity.name,
    }
    text = conv.id_template.format(**values)
    text = re.sub(r"\.{2,}", ".", text).strip(".")
    return ident(text, fallback=ident(identity.name))


def expose_id(identity: Identity) -> str:
    return ident(identity.name.replace(".", "_"))


# ── binding ──────────────────────────────────────────────────────────


def catalog_kind(alias: str, catalogs: CatalogKinds) -> Optional[str]:
    """The ``binding.location.catalog`` of a table in the Spark catalog ``alias``: the kind the
    scanned code configures it as (``spark.sql.catalog.<alias>.type``, or a ``catalog-impl``), else
    the alias itself when it is a kind FLUID knows; ``None`` when nothing says. An alias is no
    kind (``glue_catalog`` is a name the job gave a Glue catalog), and a binding that names one is
    a contract ``fluid validate`` refuses."""
    configured = catalogs.kind(alias)
    if configured or catalogs.conflicting(alias):
        return configured
    from fluid_build.providers._iceberg_catalog import (
        FAMILY_UNKNOWN,
        canonical_catalog_kind,
        catalog_kind_info,
    )

    known = canonical_catalog_kind(alias)
    return alias if known and catalog_kind_info(known).family != FAMILY_UNKNOWN else None


def _binding(
    identity: Identity,
    write: Optional[Write],
    conv: Conventions,
    catalogs: Optional[CatalogKinds] = None,
) -> Tuple[Dict[str, Any], bool, Optional[str]]:
    """``(binding, defaulted, unmapped)``: the expose's binding, whether platform and format had
    to be defaulted to ``other``, and the Spark catalog alias whose kind nothing says (the binding
    then has no ``catalog``: a person sets it)."""
    unmapped: Optional[str] = None
    fmt = _FORMATS.get((write.fmt or "") if write else "", None)
    if identity.kind == "topic":
        location: Dict[str, Any] = {"topic": identity.name}
        if write is not None and write.host:
            location["host"] = write.host
        return {"platform": "kafka", "format": "kafka_topic", "location": location}, False, None
    if identity.kind == "table":
        platform = None
        rule_format = None
        location = {"table": identity.name}
        if identity.database:
            location["database"] = identity.database
        catalog_rule = conv.catalogs.get(identity.catalog or "")
        if catalog_rule is not None:
            platform, rule_format = catalog_rule.platform, catalog_rule.format
            if catalog_rule.catalog:
                location["catalog"] = catalog_rule.catalog
        if identity.catalog and identity.rule is None and "catalog" not in location:
            # a rule that gives no kind leaves it to the code, as no rule does (a path rule's
            # catalog is the person's own word, below)
            kind = catalog_kind(identity.catalog, catalogs or CatalogKinds())
            if kind:
                location["catalog"] = kind
            else:
                unmapped = identity.catalog
        if identity.rule is not None:
            platform = identity.rule.platform or platform
            rule_format = identity.rule.format or rule_format
            if identity.rule.catalog:
                location["catalog"] = identity.rule.catalog
            if identity.location:
                location["path"] = identity.location
        platform = platform or "other"
        chosen = rule_format or fmt or "other"
        return (
            {"platform": platform, "format": chosen, "location": location},
            platform == "other" and chosen == "other",
            unmapped,
        )
    # a path no rule explains
    path = identity.location or identity.name
    scheme = path.split("://", 1)[0] if "://" in path else ""
    platform = _SCHEME_PLATFORM.get(scheme, "other")
    chosen = fmt or _SCHEME_FILE_FORMAT.get(platform, "other")
    location = {"path": path}
    if "://" in path:
        bucket, _, rest = path.split("://", 1)[1].partition("/")
        location = {"bucket": bucket, "path": rest} if rest else {"bucket": bucket}
    return {"platform": platform, "format": chosen, "location": location}, platform == "other", None


# ── the proposal ─────────────────────────────────────────────────────


@dataclass
class Output:
    identity: Identity
    writer: JobRecord
    write: Write
    #: Other job files that write the same output.
    others: List[Tuple[JobRecord, Write]] = field(default_factory=list)


@dataclass
class Group:
    label: str
    outputs: List[Output] = field(default_factory=list)


@dataclass
class Proposal:
    contracts: List[Dict[str, Any]]
    report: ImportReport


TopicSchema = Callable[[str], Union[List[Dict[str, Any]], str]]


@dataclass
class _Ctx:
    """What every product of one proposal shares."""

    version: str
    conv: Conventions
    report: ImportReport
    #: output key -> (product id, expose id), for every product proposed.
    produced: Dict[Tuple[str, ...], Tuple[str, str]]
    #: ``(product id, expose id) -> shown`` for reads nobody writes.
    external: Dict[Tuple[str, str], str] = field(default_factory=dict)
    topic_schema: Optional[TopicSchema] = None
    #: What the scanned code says each Spark catalog is.
    catalogs: CatalogKinds = field(default_factory=CatalogKinds)
    #: ``(file, line, shown)`` of unresolved reads already reported (a job feeds several products).
    reported: set = field(default_factory=set)
    #: The entry of each task link as a build lists it, by ``id(link)``: a job that feeds a
    #: hundred products is rendered (and read for credentials) once, not a hundred times.
    entries: Dict[int, Optional[Dict[str, Any]]] = field(default_factory=dict)


def _job_labels(jobs: Sequence[JobRecord]) -> Dict[str, str]:
    """A label for each job, by its absolute path: its file name without ``.py``, or, when two of
    the jobs would share that, its path under the scanned directory (``a-main`` and ``b-main``,
    not both ``main``)."""
    stems: Dict[str, Set[str]] = {}
    for job in jobs:
        stems.setdefault(ident(PurePosixPath(job.rel).stem, fallback="job"), set()).add(
            str(job.path)
        )
    labels: Dict[str, str] = {}
    for job in jobs:
        stem = ident(PurePosixPath(job.rel).stem, fallback="job")
        if len(stems[stem]) == 1:
            labels[str(job.path)] = stem
        else:
            parts = PurePosixPath(job.rel).with_suffix("").parts
            labels[str(job.path)] = ident("-".join(parts), fallback="job")
    return labels


def _groups(outputs: List[Output], split_by: str, conv: Conventions, root_name: str) -> List[Group]:
    """The products: outputs grouped by what the split is about. Groups are keyed by what they
    are (a job's absolute path, a DAG id, a folder, ``None`` for files at the top), never by
    the label made from it, so two jobs named alike are two products."""
    keyed: Dict[Tuple[Any, ...], Group] = {}
    writers = list({str(o.writer.path): o.writer for o in outputs}.values())
    labels = _job_labels(writers)
    for output in outputs:
        job = output.writer
        key: Tuple[Any, ...]
        if split_by == "table":
            key, label = ("table", *output.identity.key), product_id(output.identity, conv)
        elif split_by == "job":
            key, label = ("job", str(job.path)), labels[str(job.path)]
        elif split_by == "dag":
            dag_ids = sorted({link.dag.dag_id for link in job.tasks if link.dag.dag_id})
            if dag_ids:
                key, label = ("dag", dag_ids[0]), ident(dag_ids[0])
            else:
                key, label = ("job", str(job.path)), labels[str(job.path)]
        elif split_by == "folder":
            head = job.rel.split("/")[0] if "/" in job.rel else None
            key = ("folder", head)
            label = ident(head if head is not None else root_name, fallback="root")
        else:  # project
            key, label = ("project",), ident(root_name, fallback="project")
        if conv.domain and split_by != "table":
            label = ident(f"{conv.domain}.{label}")
        group = keyed.get(key)
        if group is None:
            group = keyed[key] = Group(label=label)
        group.outputs.append(output)
    return list(keyed.values())


def _first_links(job: JobRecord) -> List[LinkedTask]:
    """The tasks that submit the job, in file and line order. Kept on the job: every product of
    the job asks."""
    kept = getattr(job, "_first_links", None)
    if kept is None or kept[0] != len(job.tasks):
        ordered = sorted(job.tasks, key=lambda t: (t.dag.file, t.dag.line, t.task.line))
        kept = (len(job.tasks), ordered)
        job._first_links = kept  # type: ignore[attr-defined]
    return kept[1]


class _DagIndex:
    """What the tasks and the dependencies of one DAG answer, built once: every product of every
    job the DAG submits asks."""

    def __init__(self, dag: Dag) -> None:
        self.task_by_key: Dict[str, Task] = {}
        for task in dag.tasks:
            self.task_by_key.setdefault(task.key, task)
        #: ``task key -> the keys of the tasks upstream of it``, in the order the DAG lists them.
        self.upstream: Dict[str, List[str]] = {}
        for up, down in dag.edges:
            self.upstream.setdefault(down, []).append(up)


def _dag_index(dag: Dag) -> _DagIndex:
    kept = getattr(dag, "_fluid_index", None)
    shape = (len(dag.tasks), len(dag.edges))
    if kept is None or kept[0] != shape:
        kept = (shape, _DagIndex(dag))
        dag._fluid_index = kept  # type: ignore[attr-defined]
    return kept[1]


def _link_cost(link: LinkedTask) -> int:
    """About how many characters the entry of this task link writes on a build."""
    submit = link.submit
    args = sum(min(len(str(arg)), 200) + 20 for arg in submit.args[:MAX_ARGS_PER_TASK])
    upstream = len(_dag_index(link.dag).upstream.get(link.task.key, ()))
    return (
        200
        + args
        + min(len(submit.command or ""), 2_000)
        + min(len(submit.script or ""), 300)
        + 24 * upstream
    )


def _task_text(links: Sequence[LinkedTask], costs: Dict[int, int]) -> int:
    """Characters a build writes for the tasks of its first DAG (the ones it lists)."""
    if not links:
        return 0
    first = links[0].dag
    total = 0
    for link in links:
        if link.dag is first:
            cost = costs.get(id(link))
            if cost is None:
                cost = costs[id(link)] = _link_cost(link)
            total += cost
    return total


def _owner_block(label: str, conv: Conventions) -> Dict[str, str]:
    mapped = conv.owners.get(label)
    if mapped is not None:
        return dict(mapped)
    clean = safe_text(label, 120)
    return {"email": clean} if re.fullmatch(r"[^@\s]+@[^@\s]+", clean) else {"team": clean}


def _owner(
    jobs: Sequence[JobRecord], conv: Conventions, who: str, report: ImportReport
) -> Dict[str, str]:
    labels: List[str] = []
    for job in jobs:
        for link in _first_links(job):
            label = link.task.owner or link.dag.owner
            if label and label not in labels:
                labels.append(label)
    if not labels:
        report.required_defaults.append(
            f"{who}: owner defaulted to team 'imported' (no Airflow owner found for the job): "
            "set the real owner, or map the Airflow owner label under `owners` in the conventions"
        )
        return {"team": "imported"}
    if len(labels) > 1:
        report.unsupported.append(
            f"{who}: its tasks name {len(labels)} owners ({', '.join(safe_text(x, 40) for x in labels)}); a "
            f"product has one owner, so the proposal uses {safe_text(labels[0], 40)}"
        )
    return _owner_block(labels[0], conv)


def _trigger(writes: Sequence[Write]) -> str:
    long_running = [
        w for w in writes if w.streaming and (w.trigger or "") not in {"availableNow", "once"}
    ]
    return "continuous" if long_running else "manual"


def _stream_text(writes: Sequence[Write]) -> str:
    parts = []
    for w in writes:
        if not w.streaming:
            continue
        bits = []
        if w.checkpoint:
            bits.append(f"checkpoint {safe_text(w.checkpoint, 120)}")
        if w.trigger:
            bits.append(f"trigger {safe_text(w.trigger, 60)}")
        if w.query_name:
            bits.append(f"query {safe_text(w.query_name, 60)}")
        parts.append(f"{safe_text(w.shown, 80)}: " + (", ".join(bits) or "streaming write"))
    return "; ".join(parts)


def build_proposal(
    *,
    tool: str,
    version: str,
    jobs: Sequence[JobRecord],
    conv: Conventions,
    split_by: str,
    root_name: str,
    topic_schema: Optional[TopicSchema] = None,
    report: Optional[ImportReport] = None,
    catalogs: Optional[CatalogKinds] = None,
) -> Proposal:
    """The contracts the jobs (and the DAG tasks joined to them) propose, and the report.

    ``jobs`` are the job files, in path order, each with its ``tasks`` joined already.
    ``catalogs`` is what the scanned code says each Spark catalog is (the kind a table's
    ``binding.location.catalog`` names).
    """
    report = report or ImportReport()
    ordered = sorted(jobs, key=lambda j: j.shown)

    # ── outputs, and what has no identity ─────────────────────────
    outputs: Dict[Tuple[str, ...], Output] = {}
    seen_notes: Set[Tuple[Any, ...]] = set()
    ignored = 0
    for job in ordered:
        for write in job.facts.writes:
            if not write.resolved:
                report.unsupported.append(
                    f"{write.file}:{write.line} writes {safe_text(write.shown or '(no name)', 80)}: "
                    f"{safe_text(write.why)}; name it in the conventions, or declare the product by hand"
                )
                continue
            identity = identity_of(write, conv)
            if identity is None:
                ignored += 1
                continue
            existing = outputs.get(identity.key)
            if existing is None:
                if len(outputs) >= MAX_OUTPUTS:
                    raise ProposalTooLarge(
                        f"the code writes more than {MAX_OUTPUTS:,} distinct outputs"
                    )
                outputs[identity.key] = Output(identity, job, write)
            elif existing.writer is job:
                mark = ("catalogs", job.shown, identity.key)
                if existing.identity.catalog != identity.catalog and mark not in seen_notes:
                    seen_notes.add(mark)
                    report.notes.append(
                        f"{safe_text(job.shown, 80)} writes {safe_text(existing.identity.shown, 80)} "
                        f"and {safe_text(identity.shown, 80)}, which differ only by catalog: they "
                        "are proposed as one output"
                    )
                continue
            else:
                existing.others.append((job, write))
    if ignored:
        report.notes.append(f"{ignored} write(s) ignored by the conventions' ignore rules")
    for output in outputs.values():
        if output.others:
            names = ", ".join(sorted({safe_text(j.shown, 80) for j, _ in output.others}))
            report.unsupported.append(
                f"{safe_text(output.identity.shown, 80)} is written by more than one job "
                f"({safe_text(output.writer.shown, 80)}, {names}); a product has a single writer, "
                f"so it is proposed with {safe_text(output.writer.shown, 80)}: choose the writer, or split the output"
            )

    flat = sorted(outputs.values(), key=lambda o: (o.writer.shown, o.write.line, o.identity.shown))
    groups = _groups(flat, split_by, conv, root_name)
    if len(groups) > MAX_PRODUCTS:
        raise ProposalTooLarge(
            f"the proposal would hold {len(groups):,} products (the most is {MAX_PRODUCTS:,})"
        )
    fanout = visits = 0
    for group in groups:
        jobs = {str(output.writer.path): output.writer for output in group.outputs}
        reads = sum(len(job.facts.reads) for job in jobs.values())
        visits += reads
        fanout += min(reads, MAX_CONSUMES_PER_PRODUCT)
    if fanout > MAX_FANOUT:
        raise ProposalTooLarge(
            f"the products would list {fanout:,} reads between them (the most is {MAX_FANOUT:,}): "
            "every product of a job lists all the job's reads",
            hint=_SPLIT_BY_JOB,
        )
    if visits > MAX_READ_VISITS:
        raise ProposalTooLarge(
            f"the products would go through {visits:,} reads between them (the most is "
            f"{MAX_READ_VISITS:,}): every product of a job goes through all the job's reads",
            hint=_SPLIT_BY_JOB,
        )
    link_visits = task_text = 0
    costs: Dict[int, int] = {}
    for group in groups:
        for job in {str(output.writer.path): output.writer for output in group.outputs}.values():
            links = _first_links(job)
            link_visits += len(links)
            task_text += _task_text(links, costs)
    if link_visits > MAX_LINK_VISITS:
        raise ProposalTooLarge(
            f"the products would go through {link_visits:,} DAG task links between them (the "
            f"most is {MAX_LINK_VISITS:,}): every product of a job goes through the tasks that "
            "submit the job",
            hint=_SPLIT_BY_JOB,
        )
    if task_text > MAX_TASK_TEXT:
        raise ProposalTooLarge(
            f"the products would write {task_text:,} characters of DAG tasks between them (the "
            f"most is {MAX_TASK_TEXT:,}): every product of a job lists the tasks that submit the "
            "job, with their arguments",
            hint=_SPLIT_BY_JOB,
        )

    # who produces what, for consumes[]: an id is handed out once, whatever the names look like
    produced: Dict[Tuple[str, ...], Tuple[str, str]] = {}
    group_ids: Dict[int, str] = {}
    taken_ids: Set[str] = _Names()
    groups_of_job: Dict[str, Set[int]] = {}
    for index, group in enumerate(groups):
        pid = _unique(group.label, taken_ids, "-")
        group_ids[index] = pid
        taken_eids: Set[str] = _Names()
        for output in group.outputs:
            eid = _unique(expose_id(output.identity), taken_eids, "_")
            produced[output.identity.key] = (pid, eid)
            groups_of_job.setdefault(str(output.writer.path), set()).add(index)
    for job in ordered:
        if len(groups_of_job.get(str(job.path), ())) > 1 and any(
            r.resolved for r in job.facts.reads
        ):
            report.notes.append(
                f"{safe_text(job.shown, 80)} writes outputs that are in different products: the "
                "code does not say which input feeds which output, so each of those products "
                "lists all of the job's inputs in consumes[]"
            )

    ctx = _Ctx(
        version=version,
        conv=conv,
        report=report,
        produced=produced,
        topic_schema=topic_schema,
        catalogs=catalogs or CatalogKinds(),
    )
    contracts = [_product(group, group_ids[index], ctx) for index, group in enumerate(groups)]

    external = ctx.external
    if external:
        listed = ", ".join(
            f"{pid} ({safe_text(shown, 60)})"
            for (pid, _eid), shown in sorted(external.items())[:12]
        )
        more = f", and {len(external) - 12} more" if len(external) > 12 else ""
        report.notes.append(
            f"{len(external)} upstream table(s) or topic(s) are read but written by no scanned job; "
            f"consumes[] names them under the id the id rule gives: {listed}{more}"
        )
    return Proposal(contracts=contracts, report=report)


def _unmapped_catalog(pid: str, alias: str, eids: Sequence[str], catalogs: CatalogKinds) -> str:
    """The report line for a table in a Spark catalog whose kind nothing says: its binding has no
    ``catalog``, and how to set one."""
    shown = safe_text(alias, 60)
    listed = ", ".join(eids[:3]) + (f" and {len(eids) - 3} more" if len(eids) > 3 else "")
    clash = catalogs.conflicting(alias)
    why = (
        f"the code sets it to more than one kind ({', '.join(clash)})"
        if clash
        else "nothing in the scanned code or the conventions says which kind of catalog it is"
    )
    return (
        f"{pid}: exposes[{listed}] binding.location.catalog is not set: {shown} is the name of a "
        f"Spark catalog, not a kind of catalog, and {why}. Set binding.location.catalog "
        f"(glue, rest, hive, nessie, hadoop, ...) by hand, map {shown} under `catalogs` in the "
        f"conventions, or set spark.sql.catalog.{shown}.type in the scanned code"
    )


def _product(group: Group, pid: str, ctx: _Ctx) -> Dict[str, Any]:
    conv, report, produced = ctx.conv, ctx.report, ctx.produced
    version, topic_schema = ctx.version, ctx.topic_schema
    external = ctx.external
    writer_jobs: List[JobRecord] = []
    for output in group.outputs:
        if output.writer not in writer_jobs:
            writer_jobs.append(output.writer)

    # exposes
    exposes: List[Dict[str, Any]] = []
    expose_by_key: Dict[Tuple[str, ...], str] = {}
    defaulted_binding = False
    unmapped: Dict[str, List[str]] = {}
    for output in group.outputs:
        identity = output.identity
        eid = produced[identity.key][1]
        binding, defaulted, alias = _binding(identity, output.write, conv, ctx.catalogs)
        defaulted_binding = defaulted_binding or defaulted
        if alias:
            unmapped.setdefault(alias, []).append(eid)
        columns: List[Dict[str, Any]] = []
        note = ""
        if identity.kind == "topic" and topic_schema is not None:
            got = topic_schema(identity.name)
            if isinstance(got, str):
                note = got
            else:
                columns = copy.deepcopy(got)  # a shared list would be written as a YAML anchor
        expose: Dict[str, Any] = {
            "exposeId": eid,
            "kind": {"table": "table", "topic": "topic", "path": "file"}[identity.kind],
        }
        if not columns:
            expose["description"] = (
                f"TODO: declare the columns of {safe_text(identity.shown, 80)}: the code that writes it "
                "does not state them" + (f" ({safe_text(note, 100)})" if note else "")
            )
        expose["binding"] = binding
        expose["contract"] = {"schema": columns}
        exposes.append(expose)
        expose_by_key[identity.key] = eid
        report.mapped_one_to_one.append(
            f"write {safe_text(identity.shown, 80)} ({output.write.file}:{output.write.line}) → exposes[{eid}] of {pid}"
        )
        if not columns:
            report.required_defaults.append(
                f"{pid}: exposes[{eid}] has an empty schema (the code does not state the columns)"
            )
        elif identity.kind == "topic":
            report.mapped_one_to_one.append(
                f"topic {identity.name} schema from the registry ({len(columns)} columns) → exposes[{eid}]"
            )
    if defaulted_binding:
        report.required_defaults.append(
            f"{pid}: binding platform and format default to other: name the catalog or path prefix "
            "under `catalogs` or `paths` in the conventions, or set them by hand"
        )
    for alias, eids in unmapped.items():
        report.required_defaults.append(_unmapped_catalog(pid, alias, eids, ctx.catalogs))

    # builds
    builds: List[Dict[str, Any]] = []
    build_ids: Dict[int, str] = {}
    taken: Set[str] = _Names()
    labels = _job_labels(writer_jobs)
    for job in writer_jobs:
        build_ids[id(job)] = _unique(labels[str(job.path)], taken, "-")
    task_build: Dict[Tuple[str, int, str], str] = {}
    for job in writer_jobs:
        for link in job.tasks:
            task_build[(link.dag.file, link.dag.line, link.task.key)] = build_ids[id(job)]
    for job in writer_jobs:
        builds.append(
            _build(job, group, build_ids, task_build, expose_by_key, conv, report, pid, ctx)
        )

    # consumes
    consumes: List[Dict[str, Any]] = []
    seen: set = set()
    unlisted = more_reads = 0
    for job in writer_jobs:
        for read in job.facts.reads:
            seen_read = (read.file, read.line, read.shown, read.kind)
            if not read.resolved:
                if seen_read not in ctx.reported:
                    ctx.reported.add(seen_read)
                    report.unsupported.append(
                        f"{read.file}:{read.line} reads {safe_text(read.shown or '(no name)', 80)}: "
                        f"{safe_text(read.why)}; add the consumes[] entry by hand"
                    )
                continue
            if read.kind == "topic_pattern":
                if seen_read not in ctx.reported:
                    ctx.reported.add(seen_read)
                    report.unsupported.append(
                        f"{read.file}:{read.line} reads the topics matching {safe_text(read.name, 80)}: "
                        "consumes[] cannot name a pattern; list the topics by hand"
                    )
                continue
            identity = identity_of(read, conv)
            if identity is None:
                continue
            if identity.key in expose_by_key:
                continue  # the product's own output, read back
            if identity.key in produced:
                other_pid, other_eid = produced[identity.key]
                purpose = f"read by {safe_text(job.shown, 80)}"
                known = True
            else:
                other_pid = product_id(identity, conv)
                other_eid = expose_id(identity)
                purpose = f"read by {safe_text(job.shown, 80)}; no scanned job writes it"
                known = False
                external.setdefault((other_pid, other_eid), identity.shown)
            if other_pid == pid:
                mark = ("own-id", pid, other_eid)
                if mark not in ctx.reported:
                    ctx.reported.add(mark)
                    report.notes.append(
                        f"{pid}: reads {safe_text(identity.shown, 80)}, which the id rule gives "
                        "this product's own id: not listed in consumes[]"
                    )
                continue
            if (other_pid, other_eid) in seen:
                continue
            seen.add((other_pid, other_eid))
            if len(consumes) >= MAX_CONSUMES_PER_PRODUCT:
                unlisted += 1
                continue
            consumes.append({"productId": other_pid, "exposeId": other_eid, "purpose": purpose})
            if len(consumes) <= MAX_READ_LINES:
                report.mapped_one_to_one.append(
                    f"read {safe_text(identity.shown, 80)} ({read.file}:{read.line}) → consumes[{other_pid}]"
                    + ("" if known else " (no producer in the scanned code)")
                )
            else:
                more_reads += 1
    if more_reads:
        report.mapped_one_to_one.append(
            f"{pid}: {more_reads} more read(s) → consumes[] (not listed one by one)"
        )
    if unlisted:
        report.notes.append(
            f"{pid}: lists {MAX_CONSUMES_PER_PRODUCT} reads in consumes[]; {unlisted} more are "
            "not listed"
        )

    # metadata
    metadata: Dict[str, Any] = {"owner": _owner(writer_jobs, conv, pid, report)}
    layers = {conv.layers.get(o.identity.database or "") for o in group.outputs}
    layers.discard(None)
    if len(layers) == 1:
        metadata["layer"] = next(iter(layers))
        from fluid_build.forge.product_types import normalize_metadata_in_place

        normalize_metadata_in_place(metadata)

    shown = safe_text(group.outputs[0].identity.shown, 80) if len(group.outputs) == 1 else pid
    sources = ", ".join(safe_text(j.shown, 80) for j in writer_jobs)
    contract: Dict[str, Any] = {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": pid,
        "name": f"{shown} (proposed from existing code)",
        "domain": conv.domain or "imported",
        "description": (
            f"Proposed from {sources}. The job keeps running where it runs today; review the owner, "
            "binding and schema before use."
        ),
        "metadata": metadata,
        "builds": builds,
        "exposes": exposes,
    }
    if consumes:
        contract["consumes"] = consumes
    for job in writer_jobs:
        report.mapped_one_to_one.append(
            f"job {safe_text(job.shown, 80)} → builds[{build_ids[id(job)]}] of {pid}"
        )
    return contract


def _build(
    job: JobRecord,
    group: Group,
    build_ids: Dict[int, str],
    task_build: Dict[Tuple[str, int, str], str],
    expose_by_key: Dict[Tuple[str, ...], str],
    conv: Conventions,
    report: ImportReport,
    pid: str,
    ctx: _Ctx,
) -> Dict[str, Any]:
    writes = [w for w in job.facts.writes if w.resolved]
    mine = [o for o in group.outputs if o.writer is job]
    description = f"Existing job {safe_text(job.shown, 120)}; it keeps running where it runs today."
    stream = _stream_text(writes)
    if stream:
        description += f" Structured Streaming: {stream}."
    build: Dict[str, Any] = {
        "id": build_ids[id(job)],
        "description": description,
        "pattern": "hybrid-reference",
        "engine": "spark",
        "properties": {"model": safe_text(job.shown, 300)},
        "outputs": [expose_by_key[o.identity.key] for o in mine],
    }
    execution: Dict[str, Any] = {"trigger": {"type": _trigger(writes)}}
    links = _first_links(job)
    platform = conv.runtime_platform
    if platform is None and links:
        platform = _RUNTIME_PLATFORM.get(links[0].submit.runtime)
    if platform:
        execution["runtime"] = {"platform": platform}
    if links:
        orchestration = _orchestration(links, task_build, build_ids[id(job)], report, pid, ctx)
        if orchestration:
            execution["orchestration"] = orchestration
    build["execution"] = execution
    deps = _dependencies(links, task_build, build_ids[id(job)])
    if deps:
        build["dependencies"] = deps
    return build


def _dependencies(
    links: Sequence[LinkedTask], task_build: Dict[Tuple[str, int, str], str], own: str
) -> List[Dict[str, str]]:
    deps: List[str] = []
    for link in links:
        for up in _dag_index(link.dag).upstream.get(link.task.key, ()):
            upstream = task_build.get((link.dag.file, link.dag.line, up))
            if upstream and upstream != own and upstream not in deps:
                deps.append(upstream)
    return [{"buildId": d, "condition": "success"} for d in deps]


def _task_entry(link: LinkedTask, report: ImportReport, ctx: _Ctx) -> Optional[Dict[str, Any]]:
    """The entry a build lists for one task link: ``None`` when the task cannot be recorded
    (its id does not fit the contract's pattern; said once). Read once per link, whatever the
    number of products the job feeds, and copied by the caller."""
    if id(link) in ctx.entries:
        return ctx.entries[id(link)]
    dag, task, reported = link.dag, link.task, ctx.reported
    entry: Optional[Dict[str, Any]] = None
    if not task.task_id or not _AIRFLOW_ID.fullmatch(task.task_id):
        mark = ("task-id", task.file, task.line)
        if mark not in reported:
            reported.add(mark)
            report.unsupported.append(
                f"{safe_text(task.file, 80)}:{task.line} the task id "
                f"{safe_text(task.shown, 60)!r} in DAG {safe_text(dag.dag_id, 60)} does not "
                "match the contract's task id pattern, so the task is not recorded"
            )
    else:
        params: Dict[str, Any] = {
            "runtime": link.submit.runtime,
            "script": safe_text(link.submit.script or "", 300),
        }
        args = link.submit.args
        if args:
            params["args"] = safe_args(args[:MAX_ARGS_PER_TASK])
            if len(args) > MAX_ARGS_PER_TASK:
                report.notes.append(
                    f"{safe_text(task.file, 80)}:{link.submit.line} task {task.task_id} passes "
                    f"{len(args):,} arguments: the first {MAX_ARGS_PER_TASK} are listed"
                )
        if link.submit.command:
            # the schema's own rule for a Bash task asks for the command
            params["bash_command"] = safe_command(link.submit.command)
        # ``type`` and ``operator`` at the task level: the schema's task rules (fluid_execute,
        # bash, python) key on them, and a task that lacks either is held to the fluid_execute
        # rule and refused for want of a contract_path. ``custom`` is the type that names
        # nothing forge-cli runs.
        entry = {
            "taskId": task.task_id,
            "type": "custom",
            "operator": task.operator,
            "params": params,
        }
        index = _dag_index(dag)
        upstream_ids = []
        for up in index.upstream.get(task.key, ()):
            up_task = index.task_by_key.get(up)
            if up_task and up_task.task_id and _AIRFLOW_ID.fullmatch(up_task.task_id):
                upstream_ids.append(up_task.task_id)
        if upstream_ids:
            entry["dependencies"] = upstream_ids
    ctx.entries[id(link)] = entry
    return entry


def _copy_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of a task entry that shares nothing with it (two contracts are dumped as YAML
    separately: a shared list would come out as an anchor and an alias)."""
    copy = dict(entry)
    params = dict(entry["params"])
    if "args" in params:
        params["args"] = list(params["args"])
    copy["params"] = params
    if "dependencies" in copy:
        copy["dependencies"] = list(copy["dependencies"])
    return copy


def _orchestration(
    links: Sequence[LinkedTask],
    task_build: Dict[Tuple[str, int, str], str],
    build_id: str,
    report: ImportReport,
    pid: str,
    ctx: _Ctx,
) -> Optional[Dict[str, Any]]:
    first = links[0]
    dag, reported = first.dag, ctx.reported
    if not dag.dag_id or not _AIRFLOW_ID.fullmatch(dag.dag_id):
        mark = ("dag-id", dag.file, dag.line)
        if mark not in reported:  # one DAG feeds several jobs: say it once
            reported.add(mark)
            report.unsupported.append(
                f"{safe_text(dag.file, 80)}:{dag.line} the DAG id "
                f"{safe_text(dag.dag_id or dag.shown, 60)!r} does not match the contract's DAG id "
                "pattern (lower case letters, digits, '_', '.', '-'), so the DAG is not recorded "
                "on the builds it runs"
            )
        return None
    others = sorted({link.dag.dag_id for link in links if link.dag is not dag and link.dag.dag_id})
    if others:
        report.notes.append(
            f"{pid}: the job is submitted from more than one DAG; the build records {dag.dag_id}, "
            f"not {', '.join(safe_text(o, 40) for o in others)}"
        )
    config: Dict[str, Any] = {}
    if dag.schedule_kind in {"cron", "expression"} and dag.schedule:
        config["schedule"] = safe_text(dag.schedule, 120)
    if dag.tags:
        config["tags"] = [safe_text(t, 60) for t in dag.tags]
    if dag.catchup is not None:
        config["catchup"] = dag.catchup
    airflow: Dict[str, Any] = {"dagId": dag.dag_id}
    if config:
        airflow["dagConfig"] = config
    tasks: List[Dict[str, Any]] = []
    recorded: set = set()
    more_lines = 0
    for link in links:
        if link.dag is not dag:
            continue
        task = link.task
        entry = _task_entry(link, report, ctx)
        if entry is None:
            continue
        if task.task_id in recorded:
            mark = ("task-twice", task.file, task.line)
            if mark not in reported:
                reported.add(mark)
                report.notes.append(
                    f"{pid}: task {task.task_id} submits this job more than once: its first "
                    "submit is recorded on the build"
                )
            continue
        recorded.add(task.task_id)
        tasks.append(_copy_entry(entry))
        if len(tasks) <= MAX_TASK_LINES:
            report.mapped_one_to_one.append(
                f"DAG {dag.dag_id} task {task.task_id} ({task.operator}) → builds[{build_id}].execution.orchestration of {pid}"
            )
        else:
            more_lines += 1
    if more_lines:
        report.mapped_one_to_one.append(
            f"{pid}: {more_lines} more DAG task(s) → builds[{build_id}].execution.orchestration "
            "(not listed one by one)"
        )
    if tasks:
        airflow["tasks"] = tasks
    return {
        "engine": "airflow",
        "mode": "manual",
        "generateOnChange": False,
        "airflow": airflow,
    }
