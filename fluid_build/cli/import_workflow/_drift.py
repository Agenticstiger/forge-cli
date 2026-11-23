# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The drift gate behind ``fluid import airflow|pyspark <source> --check``.

The import proposes contracts from code and a person completes them, so a proposal is never
compared whole: a description, an owner or a column list written by hand is not drift. What is
compared is what the code decides, and only that (the *projection* of a contract):

* **jobs**: the builds the import writes (``pattern: hybrid-reference``, ``engine: spark``), by id;
* **outputs**: the exposes the builds list in ``outputs``, by id, and for each the facts the code
  and the conventions decide: its ``kind``, its binding's ``platform`` and ``format`` (unless the
  import left them at ``other``, for a person to name) and every key of its ``location``;
* **inputs**: the ``consumes[]`` entries the import wrote (their purpose begins ``read by``), by
  product and expose id;
* **schedule**: the DAG that runs a job (``dagId``), its ``schedule`` and ``catchup``, and the ids
  of the tasks recorded on the build.

Whatever else a contract holds (a read, an output or a build a person added, every description,
owner, column and tag) is outside it. Both sides are reduced to plain data and compared through
the canonical JSON the contract digests use, so key order and YAML comments never count.

The committed contracts are the files the import itself would write (the caller gives their
paths, worked out with the importer's own rule for ``--out``), never the result of a glob: a
contract of another product in the same folder is not read. A product the code no longer has
leaves its file where it is and this does not see it.

Drift is typed (:data:`KINDS`). A job gone from the code (``job_removed``) and something the import
used to find and no longer does (``coverage_regressed``) are warnings; ``--strict`` makes them
drift too. Exit codes follow diff(1): 0 in sync, 1 drift, 2 trouble (:data:`EXIT_ERROR`).
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from fluid_build._errors import FluidUserError, SchemaValidationError
from fluid_build.cli._ci_output import escape_data, escape_property
from fluid_build.forge.core.plan_digest import _canonical_json, coerce_keys_to_str
from fluid_build.observability.secret_redactor import redact_secret_text
from fluid_build.util.safe_yaml import (
    MAX_YAML_BYTES,
    UnsafeYamlError,
    load_yaml_safe,
    read_bounded_file,
)

from ._pipeline import DOC, refusal
from ._proposal import READ_PURPOSE_PREFIX, SourceKey
from ._redact import safe_text

#: ``--check`` exit codes. They follow diff(1), as ``fluid diff --exit-on-drift`` does: 0 no
#: differences, 1 differences, 2 trouble. OpenTofu's ``-detailed-exitcode`` spells drift 2; this
#: keeps the 1 the other drift flag of this CLI returns, so a gate reads the same everywhere, and
#: a mistyped flag (an argparse usage error is also 2) never reads as drift.
EXIT_IN_SYNC = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

FORMATS = ("text", "json", "github")
#: The ``schema`` of the JSON report.
SCHEMA_VERSION = 1
#: Items listed one by one; the rest are counted (the way the import report counts its own lines).
MAX_ITEMS_LISTED = 100
#: The most a committed contract may hold. A bigger file is refused, not parsed.
MAX_BASELINE_BYTES = MAX_YAML_BYTES

#: In the order a report lists them within one contract.
KINDS = (
    "job_added",
    "job_removed",
    "output_added",
    "output_removed",
    "schedule_changed",
    "table_changed",
    "coverage_regressed",
)
#: Drift only with ``--strict``: otherwise a warning. Both are a loss the contract may still be
#: right about (a job retired on purpose, a DAG the scan was not pointed at).
STRICT_KINDS = frozenset({"job_removed", "coverage_regressed"})

_PHRASES = {
    "job_added": "the code runs a job that no build of the contract records",
    "job_removed": "the contract records a job the code no longer has",
    "output_added": "the code writes an output the contract does not expose",
    "output_removed": "the contract exposes an output the code no longer writes",
    "schedule_changed": "the DAG that runs the job differs from the contract",
    "table_changed": "a table or topic the job touches differs from the contract",
    "coverage_regressed": "the import no longer finds what the contract records",
}

#: What the import writes for a job it proposes, and so what a build must look like to be one.
_IMPORTED_BUILD = {"pattern": "hybrid-reference", "engine": "spark"}
#: A schedule the code gives as a literal: a cron expression (five to seven fields) or a preset
#: such as ``@daily``. Anything else the import copies into ``dagConfig.schedule`` is the DAG's
#: expression as written (``Variable.get(...)``, ``timedelta(...)``), which a person replaces
#: with the real value.
_LITERAL_SCHEDULE = re.compile(r"@[A-Za-z]+|[A-Za-z0-9*/,\-?#]+(?: +[A-Za-z0-9*/,\-?#]+){4,6}")


def _is_imported(build: Mapping[str, Any]) -> bool:
    return all(build.get(key) == value for key, value in _IMPORTED_BUILD.items())


# ── the projection ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutputFacts:
    kind: Optional[str]
    platform: Optional[str]
    format: Optional[str]
    location: Mapping[str, Any]
    #: Listed in the ``outputs`` of a build of the shape the import writes: the import links every
    #: expose it writes to its job, and an expose (or a build) a person added is not linked.
    built: bool


@dataclass(frozen=True)
class JobFacts:
    model: Optional[str]
    #: A build of the shape the import writes: another one was added by a person.
    imported: bool
    dag: Optional[str]
    schedule: Optional[str]
    catchup: Optional[bool]
    tasks: Tuple[str, ...]


@dataclass(frozen=True)
class Projection:
    outputs: Mapping[str, OutputFacts]
    #: ``<productId>.<exposeId>`` of each ``consumes[]`` entry, with its purpose text.
    inputs: Mapping[str, str]
    jobs: Mapping[str, JobFacts]


def _plain(value: Any) -> Any:
    """``value`` as plain JSON data, which ``_canonical_json`` takes: a YAML date, for one, is not."""
    return json.loads(json.dumps(coerce_keys_to_str(value), default=str))


def _same(left: Any, right: Any) -> bool:
    return _canonical_json(_plain(left)) == _canonical_json(_plain(right))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _ident(value: Any) -> Optional[str]:
    """An id as text; ``None`` for what is not one (a number is: YAML reads ``123`` as one)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text or None


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def project(contract: Mapping[str, Any]) -> Projection:
    """What the code decides in ``contract``, from a proposal or from a file a person edited."""
    builds = [_mapping(b) for b in _list(contract.get("builds"))]
    built = {
        text
        for build in builds
        if _is_imported(build)
        for text in (_ident(item) for item in _list(build.get("outputs")))
        if text
    }
    outputs: Dict[str, OutputFacts] = {}
    for raw in _list(contract.get("exposes")):
        expose = _mapping(raw)
        eid = _ident(expose.get("exposeId"))
        if eid is None or eid in outputs:
            continue
        binding = _mapping(expose.get("binding"))
        platform, fmt = _text(binding.get("platform")), _text(binding.get("format"))
        outputs[eid] = OutputFacts(
            kind=_text(expose.get("kind")),
            platform=platform,
            format=fmt,
            location=_plain(_mapping(binding.get("location"))),
            built=eid in built,
        )
    inputs: Dict[str, str] = {}
    for raw in _list(contract.get("consumes")):
        entry = _mapping(raw)
        pid, eid = _ident(entry.get("productId")), _ident(entry.get("exposeId"))
        if pid and eid:
            inputs.setdefault(f"{pid}.{eid}", _text(entry.get("purpose")) or "")
    jobs: Dict[str, JobFacts] = {}
    for build in builds:
        bid = _ident(build.get("id"))
        if bid is None or bid in jobs:
            continue
        airflow = _mapping(
            _mapping(_mapping(build.get("execution")).get("orchestration")).get("airflow")
        )
        config = _mapping(airflow.get("dagConfig"))
        catchup = config.get("catchup")
        tasks = tuple(
            task
            for task in (_ident(_mapping(t).get("taskId")) for t in _list(airflow.get("tasks")))
            if task
        )
        jobs[bid] = JobFacts(
            model=_text(_mapping(build.get("properties")).get("model")),
            imported=_is_imported(build),
            dag=_ident(airflow.get("dagId")),
            schedule=_text(config.get("schedule")),
            catchup=catchup if isinstance(catchup, bool) else None,
            tasks=tasks,
        )
    return Projection(outputs=outputs, inputs=inputs, jobs=jobs)


# ── typed drift ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DriftItem:
    """One difference between a committed contract and what the code says now."""

    kind: str
    product_id: str
    contract_path: str
    #: The field it is about, as the contract spells it: ``exposes[orders].binding.format``.
    what: str
    #: What the contract holds, and what the code says now; ``None``: nothing.
    before: Any = None
    after: Any = None
    #: Where in the code, ``file:line`` (a job is its file alone); ``None`` when the code no longer
    #: has it.
    source_ref: Optional[str] = None


@dataclass
class DriftReport:
    tool: str
    source: str
    strict: bool = False
    #: The products the code proposes, each compared with its file (set by :func:`check`).
    products: int = 0
    items: List[DriftItem] = field(default_factory=list)

    def blocking(self, item: DriftItem) -> bool:
        return self.strict or item.kind not in STRICT_KINDS

    @property
    def errors(self) -> List[DriftItem]:
        return [item for item in self.items if self.blocking(item)]

    @property
    def warnings(self) -> List[DriftItem]:
        return [item for item in self.items if not self.blocking(item)]

    @property
    def status(self) -> str:
        return "drift" if self.errors else "in_sync"

    @property
    def exit_code(self) -> int:
        return EXIT_DRIFT if self.errors else EXIT_IN_SYNC

    def listed(self) -> Tuple[List[DriftItem], int]:
        """The items a report lists (errors first, then by file) and how many it does not."""
        ordered = sorted(
            self.items,
            key=lambda i: (not self.blocking(i), i.contract_path, KINDS.index(i.kind), i.what),
        )
        return ordered[:MAX_ITEMS_LISTED], max(0, len(ordered) - MAX_ITEMS_LISTED)


def _output_diffs(fresh: OutputFacts, held: OutputFacts) -> List[Tuple[str, Any, Any]]:
    """``(field, held, fresh)`` for each fact of an output the code decides and the contract holds
    otherwise. A binding the import left at ``other`` is the person's to name, location included
    (its keys are a guess until the platform is known); so is any key of the location the import
    did not write."""
    decided: List[Tuple[str, Any, Any]] = [("kind", fresh.kind, held.kind)]
    if fresh.platform not in (None, "other"):
        decided.append(("binding.platform", fresh.platform, held.platform))
        if fresh.format not in (None, "other"):
            decided.append(("binding.format", fresh.format, held.format))
        for key in sorted(fresh.location):
            decided.append((f"binding.location.{key}", fresh.location[key], held.location.get(key)))
    return [
        (name, then, now) for name, now, then in decided if now is not None and not _same(now, then)
    ]


@dataclass(frozen=True)
class _Scope:
    """What every item of one product shares."""

    product_id: str
    contract_path: str
    sources: Mapping[SourceKey, str]

    def item(
        self,
        kind: str,
        what: str,
        before: Any = None,
        after: Any = None,
        where: Optional[Tuple[str, str]] = None,
    ) -> DriftItem:
        ref = self.sources.get((self.product_id, *where)) if where else None
        return DriftItem(kind, self.product_id, self.contract_path, what, before, after, ref)


def _location(facts: OutputFacts) -> Any:
    return dict(facts.location) or None


def _dag_items(scope: _Scope, bid: str, job: JobFacts, held: JobFacts) -> List[DriftItem]:
    """The DAG that runs a job: which one, and what the code states about it. A schedule the
    code does not state as a literal (an expression, or none), and a ``catchup`` it does not set,
    are the person's to write down: they are not compared."""
    airflow = f"builds[{bid}].execution.orchestration.airflow"
    if job.dag is None:
        if held.dag is None:
            return []
        return [scope.item("coverage_regressed", f"{airflow}.dagId", held.dag, None)]
    items: List[DriftItem] = []
    if held.dag is None or not _same(job.dag, held.dag):
        items.append(
            scope.item("schedule_changed", f"{airflow}.dagId", held.dag, job.dag, ("schedule", bid))
        )
    if held.dag is None:
        return items
    schedule = f"{airflow}.dagConfig.schedule"
    if job.schedule and _LITERAL_SCHEDULE.fullmatch(job.schedule):
        if not _same(job.schedule, held.schedule):
            items.append(
                scope.item(
                    "schedule_changed", schedule, held.schedule, job.schedule, ("schedule", bid)
                )
            )
    if job.catchup is not None and not _same(job.catchup, held.catchup):
        items.append(
            scope.item(
                "schedule_changed",
                f"{airflow}.dagConfig.catchup",
                held.catchup,
                job.catchup,
                ("schedule", bid),
            )
        )
    if _same(job.dag, held.dag):
        items.extend(
            scope.item("coverage_regressed", f"{airflow}.tasks[{task}]", task, None)
            for task in held.tasks
            if task not in job.tasks
        )
    return items


def _job_items(scope: _Scope, fresh: Projection, held: Optional[Projection]) -> List[DriftItem]:
    items: List[DriftItem] = []
    for bid, job in fresh.jobs.items():
        held_job = held.jobs.get(bid) if held else None
        if held_job is None:
            items.append(
                scope.item("job_added", f"builds[{bid}]", None, job.model, ("builds", bid))
            )
        else:
            items.extend(_dag_items(scope, bid, job, held_job))
    if held:
        items.extend(
            scope.item("job_removed", f"builds[{bid}]", held_job.model, None)
            for bid, held_job in held.jobs.items()
            if bid not in fresh.jobs and held_job.imported
        )
    return items


def _output_items(scope: _Scope, fresh: Projection, held: Optional[Projection]) -> List[DriftItem]:
    items: List[DriftItem] = []
    for eid, out in fresh.outputs.items():
        held_out = held.outputs.get(eid) if held else None
        if held_out is None:
            items.append(
                scope.item(
                    "output_added", f"exposes[{eid}]", None, _location(out), ("exposes", eid)
                )
            )
            continue
        items.extend(
            scope.item("table_changed", f"exposes[{eid}].{name}", before, after, ("exposes", eid))
            for name, before, after in _output_diffs(out, held_out)
        )
    if held:
        items.extend(
            scope.item("output_removed", f"exposes[{eid}]", _location(held_out), None)
            for eid, held_out in held.outputs.items()
            if eid not in fresh.outputs and held_out.built
        )
    return items


def _input_items(scope: _Scope, fresh: Projection, held: Optional[Projection]) -> List[DriftItem]:
    if not held:
        return []
    added = [
        scope.item("table_changed", f"consumes[{key}]", None, key, ("consumes", key))
        for key in fresh.inputs
        if key not in held.inputs
    ]
    lost = [
        scope.item("coverage_regressed", f"consumes[{key}]", key, None)
        for key, purpose in held.inputs.items()
        if key not in fresh.inputs and purpose.startswith(READ_PURPOSE_PREFIX)
    ]
    return [*added, *lost]


def compare_product(
    *,
    product_id: str,
    contract_path: str,
    fresh: Projection,
    held: Optional[Projection],
    sources: Mapping[SourceKey, str],
) -> List[DriftItem]:
    """The drift of one product: ``fresh`` is what the code says, ``held`` what its committed
    contract holds (``None``: there is no such file, so every job and output is new)."""
    scope = _Scope(product_id, contract_path, sources)
    return [
        *_job_items(scope, fresh, held),
        *_output_items(scope, fresh, held),
        *_input_items(scope, fresh, held),
    ]


# ── reading the committed contracts ──────────────────────────────────────────────────────────


def _display(value: Any, limit: int = 160) -> str:
    """``value`` as one printable line: no control or bidirectional character, a credential
    masked, cut at ``limit``. Everything the report prints about a repository goes through it."""
    text = value if isinstance(value, str) else _canonical_json(_plain(value))
    return redact_secret_text(safe_text(text, limit))


def read_baseline(path: Path) -> Optional[Dict[str, Any]]:
    """The contract committed at ``path`` as plain data, or ``None`` when nothing is there.

    Looked at without following a link, as the import looks at what it would overwrite: a link,
    a directory or a device is no contract. Read to a cap, parsed under the caps of
    :func:`fluid_build.util.safe_yaml.load_yaml_safe`, and never quoted back: an error names the
    file and a position, not what the file says.
    """
    shown = _display(str(path))
    fix = "Pass the --out (and --split-by) of the import that wrote the contracts."
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise refusal(
            f"cannot look at the contract {shown}",
            f"{type(exc).__name__}: a folder above it cannot be entered, or a name in the path "
            "is a file.",
            "Fix the permissions of the folder. " + fix,
            file=shown,
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise refusal(
            f"the contract {shown} is not a regular file",
            "A symbolic link, a directory or a device is never read as a contract.",
            "Put the contract file there. " + fix,
            file=shown,
        )
    try:
        data = read_bounded_file(path, MAX_BASELINE_BYTES)
    except OSError as exc:
        raise refusal(
            f"cannot read the contract {shown}",
            f"{type(exc).__name__}: this process may not read it.",
            "Fix its permissions.",
            file=shown,
        ) from exc
    if data is None:
        raise refusal(
            f"the contract {shown} is over {MAX_BASELINE_BYTES:,} bytes",
            "A contract this big is not read.",
            "Check that it is the file the import wrote. " + fix,
            file=shown,
        )
    try:
        document = load_yaml_safe(data, max_bytes=MAX_BASELINE_BYTES)
    except UnsafeYamlError as exc:
        raise refusal(
            f"the contract {shown} is refused: {_display(str(exc), 120)}",
            "It nests too deep, or repeats anchors past the caps a contract is read under.",
            "Rewrite it without YAML aliases.",
            file=shown,
        ) from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise refusal(
            f"the contract {shown} is not valid YAML",
            f"The parser stops{where}.",
            "Fix the file: `fluid validate` shows the details.",
            file=shown,
        ) from exc
    except ValueError as exc:  # not UTF-8
        raise refusal(
            f"the contract {shown} cannot be read as text",
            type(exc).__name__,
            "Save it as UTF-8.",
            file=shown,
        ) from exc
    if not isinstance(document, dict):
        raise refusal(
            f"the file {shown} is not a contract",
            "A contract is a YAML mapping; this holds something else (or nothing).",
            "Check that it is the file the import wrote. " + fix,
            file=shown,
        )
    plain: Dict[str, Any] = _plain(document)
    return plain


def check(
    contracts: Sequence[Mapping[str, Any]],
    sources: Mapping[SourceKey, str],
    paths: Sequence[Path],
    report: DriftReport,
) -> DriftReport:
    """Compare each contract the code proposes with the file at its path, into ``report``. Reads,
    and writes nothing; raises a typed refusal when nothing can be decided."""
    if not contracts:
        raise refusal(
            f"`fluid import {report.tool}` proposes no product from {_display(report.source, 100)!r}",
            "There is nothing to compare the committed contracts with, and a gate that passes "
            "because it read nothing would pass whatever the directory held.",
            "Check the source directory and the conventions.",
            source=_display(report.source, 120),
        )
    report.products = len(contracts)
    found = 0
    for contract, path in zip(contracts, paths, strict=True):
        document = read_baseline(path)
        found += document is not None
        report.items.extend(
            compare_product(
                product_id=str(contract["id"]),
                contract_path=str(path),
                fresh=project(contract),
                held=project(document) if document is not None else None,
                sources=sources,
            )
        )
    if not found:
        shown = ", ".join(_display(str(p), 100) for p in paths[:3])
        raise refusal(
            "no committed contract is where this import would write one",
            f"None of the {len(paths)} file(s) exists ({shown}"
            f"{', ...' if len(paths) > 3 else ''}): there is nothing to compare the code with.",
            "Write them once with `fluid import` (without --check), and pass the same --out, "
            "--split-by, --conventions and --dags here.",
            files=[_display(str(p), 160) for p in paths[:10]],
        )
    return report


# ── the report ───────────────────────────────────────────────────────────────────────────────


def _quote(value: Any) -> str:
    """A value as a report shows it: text in quotes, anything else as data, nothing as ``(none)``."""
    if value is None:
        return "(none)"
    shown = _display(value, 120)
    return json.dumps(shown, ensure_ascii=False) if isinstance(value, str) else shown


def _change(item: DriftItem) -> str:
    if item.before is None and item.after is None:
        return ""
    if item.before is None:
        return f"now {_quote(item.after)}"
    if item.after is None:
        return f"was {_quote(item.before)}"
    return f"{_quote(item.before)} → {_quote(item.after)}"


def _message(item: DriftItem) -> str:
    """One line, with no path in it that a reader cannot find in the contract."""
    text = f"{_PHRASES[item.kind]}: {_display(item.what)}"
    change = _change(item)
    if change:
        text += f" ({change})"
    if item.source_ref:
        text += f" at {_display(item.source_ref, 200)}"
    return text


def _counts(report: DriftReport) -> str:
    return f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"


def _render_text(report: DriftReport) -> str:
    lines = [
        f"Import drift check: {report.tool} {_display(report.source, 100)}: "
        f"{report.products} product(s) compared"
    ]
    if not report.items:
        lines.append("✓ In sync: the committed contracts match the code.")
        return "\n".join(lines)
    lines.append(("✗ Drift: " if report.errors else "✓ In sync, with warnings: ") + _counts(report))
    listed, omitted = report.listed()
    current = None
    for item in listed:
        if item.contract_path != current:
            current = item.contract_path
            lines.append(f"\n  {_display(current, 200)} ({_display(item.product_id, 100)})")
        mark = "✗" if report.blocking(item) else "⚠"
        change = _change(item)
        lines.append(
            f"    {mark} {item.kind}  {_display(item.what)}"
            + (f": {change}" if change else "")
            + (f"  [{_display(item.source_ref, 200)}]" if item.source_ref else "")
        )
    if omitted:
        lines.append(f"\n  {omitted} more item(s) like these are not listed")
    if report.errors:
        lines.append(
            "\nUpdate the contracts by hand, or write them again with `fluid import` and --force "
            "(it replaces what a person wrote)."
        )
    if report.warnings and not report.strict:
        lines.append("Warnings are drift with --strict.")
    return "\n".join(lines)


def _render_github(report: DriftReport) -> str:
    listed, omitted = report.listed()
    lines = []
    for item in listed:
        level = "error" if report.blocking(item) else "warning"
        lines.append(
            f"::{level} file={escape_property(_display(item.contract_path, 300))},line=1,"
            f"title={escape_property(item.kind)}::"
            f"{escape_data(_display(item.product_id, 100) + ': ' + _message(item))}"
        )
    summary = (
        f"Import drift check: {report.tool} {_display(report.source, 100)}: "
        f"{report.products} product(s) compared, "
        + ("in sync" if not report.items else _counts(report))
        + (f"; {omitted} more item(s) are not listed" if omitted else "")
    )
    return "\n".join([*lines, summary])


def _render_json(report: DriftReport) -> str:
    listed, omitted = report.listed()
    document = {
        "schema": SCHEMA_VERSION,
        "status": report.status,
        "tool": report.tool,
        "source": _display(report.source, 200),
        "strict": report.strict,
        "products": report.products,
        "counts": {"error": len(report.errors), "warning": len(report.warnings)},
        "omitted": omitted,
        "items": [
            {
                "kind": item.kind,
                "severity": "error" if report.blocking(item) else "warning",
                "product_id": _display(item.product_id, 120),
                "contract_path": _display(item.contract_path, 300),
                "what": _display(item.what, 200),
                "source_ref": _display(item.source_ref, 200) if item.source_ref else None,
                "before": None if item.before is None else _display(item.before),
                "after": None if item.after is None else _display(item.after),
            }
            for item in listed
        ],
    }
    return json.dumps(document, indent=2, ensure_ascii=False)


def render(report: DriftReport, fmt: str) -> str:
    """The report as ``text``, ``json`` or ``github`` (workflow annotations)."""
    return {"text": _render_text, "json": _render_json, "github": _render_github}[fmt](report)


def as_refusal(exc: BaseException, tool: Optional[str]) -> FluidUserError:
    """``exc`` as a typed refusal: a failure that is not one (a bug, a full disk) must not read as
    drift either, so it is reported in the same shape, without its traceback."""
    if isinstance(exc, FluidUserError):
        return exc
    return SchemaValidationError(
        what=f"`fluid import {tool or '<tool>'} --check` stopped before it could decide",
        why=f"{type(exc).__name__}: {_display(str(exc), 200)}",
        fix=(
            "Run it again with FLUID_LOG_LEVEL=DEBUG for the traceback; report it if the source "
            "is a valid directory of DAGs or jobs."
        ),
        doc=DOC,
    )


def render_error(exc: FluidUserError, fmt: str, *, color: bool = True) -> Tuple[str, bool]:
    """``(text, to_stderr)`` for a refusal: the typed panel on stderr for ``text``; a document
    on stdout for ``json``, an error annotation for ``github``, where the reader looks."""
    what, why, fix = (_display(part, 400) for part in (exc.what, exc.why, exc.fix))
    if fmt == "json":
        document = {
            "schema": SCHEMA_VERSION,
            "status": "error",
            "error": {"what": what, "why": why, "fix": fix, "doc": DOC},
        }
        return json.dumps(document, indent=2, ensure_ascii=False), False
    if fmt == "github":
        message = escape_data(f"{what}: {why} {fix}")
        return f"::error title={escape_property('import check')}::{message}", False
    return exc.render(color=color), True
