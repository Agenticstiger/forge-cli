# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The dbt tests a Databricks dbt job runs for its contract's data-quality rules.

Each check of the rules on the exposes a build writes (:func:`fluid_build.util.dq_checks.
read_rules`) becomes one singular dbt test, ``<test path>/fluid_dq/<model>/<check>.sql``, in the
copy of the dbt project the job uploads (``databricks_jobs.py``). The user's own files upload
byte for byte: dbt describes a model in one properties entry only (a second entry fails the
parse), and that entry is the user's, so the tests are files of their own rather than entries
in it.

``dbt build`` runs each test right after the model it tests, so the job's run page lists every
one by its check name (the rule id), passed, warned or failed. A test of severity ``error`` that
fails skips the models below it and fails the task, the run and ``fluid apply``.

What runs:

* ``not_null``, ``unique``, ``accepted_values`` and ``reference``: dbt's own built-in test SQL,
  called as a ``unique:`` test in YAML calls it (``adapter.dispatch('test_unique', 'dbt')``). A
  reference is dbt's ``relationships`` against the source table the project declares for the
  consumed expose (the table the model reads), found by the expose's id.
* ``regex``, ``bounds``, ``freshness``, ``schema`` and every share: forge-cli's own SQL, after
  dbt_utils' ``recency`` and ``not_null_proportion`` and dbt_expectations'
  ``expect_column_values_to_match_regex`` and ``expect_table_columns_to_match_set`` (both
  Apache-2.0). No dbt package is installed: a serverless job's ``dbt deps`` may not reach
  hub.getdbt.com (Databricks Free Edition limits outbound traffic to trusted domains), and
  nothing is merged into the user's ``packages.yml``.

Each file sets ``config(severity=..., enabled=true)``, which wins over ``dbt_project.yml``, and
the tag ``fluid_dq``. A name or value from the contract or the project reaches the file only as
an escaped Jinja string (:func:`_jinja_str`) or a validated, backtick-quoted identifier, and an
SQL literal only through ``databricks_access.string_literal``.

After a run, :func:`report` reads dbt's own result line for each test (``N of M PASS <name>``)
from the task's output for ``fluid apply`` to print.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...util.contract import builds_writing_expose
from ...util.dq_checks import DqCheck, describe, has_rules, read_rules
from ..base import UnsupportedBindingError

#: The folder, below the project's first test path, that holds the generated tests.
TEST_DIR = "fluid_dq"
#: The dbt tag every generated test carries (``dbt build --select tag:fluid_dq``).
TAG = "fluid_dq"

_SQL_OPERATORS = {">=": ">=", ">": ">", "<=": "<=", "<": "<", "==": "=", "!=": "!="}
#: The file names dbt itself reads at the top of a project, never a properties file.
_PROJECT_YAML = frozenset(
    {"dbt_project.yml", "packages.yml", "dependencies.yml", "package-lock.yml", "selectors.yml"}
)
#: dbt's own line for a finished test: ``12 of 20 PASS name``, ``FAIL 3 name``, ``WARN 1``,
#: ``ERROR name``, ``SKIP test name`` (``format_fancy_output_line``, dbt-core).
_RESULT_RE = re.compile(
    r"\b\d+ of \d+ (PASS|FAIL|WARN|ERROR|SKIP)(?: (\d+))?(?: (?:test|relation))? ([0-9A-Za-z_]+)"
)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class DqTest:
    """One generated test: the check it runs and the file that runs it."""

    check: DqCheck
    model: str
    path: str

    @property
    def name(self) -> str:
        return self.check.name


@dataclass
class CompiledTests:
    """The test files for one build's job, its tests, and every refusal."""

    files: List[Tuple[str, bytes]] = field(default_factory=list)
    tests: List[DqTest] = field(default_factory=list)
    refusals: List[UnsupportedBindingError] = field(default_factory=list)


def _refusal(kind: str, message: str, fix: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-job-dq-{kind}", message, (fix,))


def written_tables(build: Mapping[str, Any], builds: Sequence[Any], tables: Sequence[Any]):
    """The Databricks tables (``databricks.py::_Table``) ``build`` writes, in contract order."""
    return [
        table
        for table in tables
        if any(b is build for b in builds_writing_expose(table.exposure.get("exposeId"), builds))
    ]


def compile_tests(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    where: str,
    tables: Sequence[Any],
    files: Optional[Sequence[Tuple[str, bytes]]],
    *,
    git: bool = False,
) -> CompiledTests:
    """The test files for the rules on ``tables`` (the Databricks tables ``build`` writes).

    ``files`` is the project as it is uploaded, ``(path, bytes)``; ``None`` when no file can
    be read (``fluid validate`` without a contract location), when only the rules themselves
    are checked. ``git``: the job runs a Git checkout, which no file can be added to.
    """
    out = CompiledTests()
    exposures = [table.exposure for table in tables]
    if not has_rules(exposures):
        return out
    rules = read_rules(contract, exposures)
    for refused in rules.refusals:
        out.refusals.append(
            _refusal(
                "rule", f"{where} expose {refused.expose_id!r}: {refused.message}.", refused.fix
            )
        )
    if git:
        out.refusals.append(
            _refusal(
                "git-source",
                f"{where} runs its dbt project from Git, and the contract rules on "
                f"{_exposes(tables)} run as dbt tests in the job: forge-cli adds them to the "
                "project it uploads, and a Git checkout runs as it was committed.",
                "Point repository at the local dbt project (fluid apply uploads it).",
            )
        )
        return out
    if files is None or out.refusals:
        return out

    project = _project_paths(files)
    present = {rel for rel, _ in files}
    sources = None
    models: Dict[str, str] = {}
    for table in tables:
        model = _model(build, table, project["model-paths"], present, where, out.refusals)
        if model is not None:
            models[table.expose_id] = model
    if out.refusals:
        return out
    if not project["test-paths"]:
        out.refusals.append(
            _refusal(
                "test-path",
                f"{where} dbt_project.yml sets test-paths to an empty list, so dbt reads no "
                "test file and the contract rules would not run.",
                "Leave test-paths out (dbt reads tests/), or name a directory.",
            )
        )
        return out
    folder = PurePosixPath(project["test-paths"][0]) / TEST_DIR
    for check in rules.checks:
        model = models[check.expose_id]
        source = None
        if check.reference is not None:
            if sources is None:
                sources = _declared_sources(files)
            source = _source_for(check, sources, where, out.refusals)
            if source is None:
                continue
        try:
            text = render(check, model, source)
        except ValueError:
            out.refusals.append(
                _refusal(
                    "column",
                    f"{where} rule {check.rule_id!r} on expose {check.expose_id!r} reads a "
                    "column whose name is not a plain identifier (letters, digits and _), and "
                    "the tests name columns only that way.",
                    "Rename the column in the contract and the model.",
                )
            )
            continue
        path = str(folder / model / f"{check.name}.sql")
        if path in present:
            out.refusals.append(
                _refusal(
                    "path",
                    f"{where} the dbt project already holds {path}, where forge-cli writes the "
                    f"test for rule {check.rule_id!r}: {folder}/ is the generated tests' folder.",
                    f"Move the file out of {folder}/.",
                )
            )
            continue
        out.files.append((path, text.encode("utf-8")))
        out.tests.append(DqTest(check=check, model=model, path=path))
    if out.refusals:
        out.files, out.tests = [], []
    return out


def expected_tests(contract: Mapping[str, Any], build: Mapping[str, Any]) -> List[DqCheck]:
    """The checks a build's job tests, from the contract alone: what :func:`report` looks for
    in a run's output. The rules on each Databricks expose the build writes."""
    builds = [b for b in contract.get("builds") or [] if isinstance(b, Mapping)]
    exposures = []
    for exposure in contract.get("exposes") or []:
        if not isinstance(exposure, Mapping):
            continue
        binding = exposure.get("binding")
        platform = binding.get("platform") if isinstance(binding, Mapping) else None
        if str(platform or "").strip().lower() != "databricks":
            continue
        if any(b is build for b in builds_writing_expose(exposure.get("exposeId"), builds)):
            exposures.append(exposure)
    return read_rules(contract, exposures).checks


# ── project ──────────────────────────────────────────────────────────────────


def _project_paths(files: Sequence[Tuple[str, bytes]]) -> Dict[str, List[str]]:
    """``model-paths`` and ``test-paths`` of the uploaded ``dbt_project.yml``, with dbt's
    defaults; the upload has already read it, so it parses."""
    import yaml

    config: Any = {}
    for rel, data in files:
        if rel == "dbt_project.yml":
            try:
                config = yaml.safe_load(data.decode("utf-8")) or {}
            except (UnicodeDecodeError, yaml.YAMLError):
                config = {}
    config = config if isinstance(config, Mapping) else {}
    out: Dict[str, List[str]] = {}
    for key, default in (("model-paths", ["models"]), ("test-paths", ["tests"])):
        raw = config.get(key, default)
        entries = [raw] if isinstance(raw, str) else list(raw or [])
        out[key] = [str(PurePosixPath(str(e))) for e in entries if str(e).strip()]
    return out


def _model(
    build: Mapping[str, Any],
    table: Any,
    model_paths: Sequence[str],
    present: Sequence[str],
    where: str,
    refusals: List[UnsupportedBindingError],
) -> Optional[str]:
    """The dbt model that writes ``table``: the build's ``properties.model`` when it writes one
    output (as ``build_selectors`` selects it), else the model named after the table."""
    props = build.get("properties") if isinstance(build.get("properties"), Mapping) else {}
    outputs = build.get("outputs") if isinstance(build.get("outputs"), list) else []
    named = props.get("model")
    model = str(named) if named and len(outputs) <= 1 else str(table.name)
    found = any(
        PurePosixPath(rel).stem == model
        and PurePosixPath(rel).suffix in (".sql", ".py")
        and any(rel.startswith(f"{path}/") for path in model_paths)
        for rel in present
    )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", model) or not found:
        refusals.append(
            _refusal(
                "model",
                f"{where} writes expose {table.expose_id!r}, whose contract rules run as dbt "
                f"tests on the model that builds it: model {model!r}, which the dbt project's "
                f"model-paths ({', '.join(model_paths) or 'none'}) do not hold.",
                "Name the model in builds[].properties.model (one output per build), or name it "
                "after the expose's binding.location.table.",
            )
        )
        return None
    return model


def _declared_sources(files: Sequence[Tuple[str, bytes]]) -> Dict[str, List[Tuple[str, str]]]:
    """``{source table name: [(source name, table name)]}`` from every properties file the
    upload carries. A file that does not parse is passed over: dbt reports it itself."""
    import yaml

    found: Dict[str, List[Tuple[str, str]]] = {}
    for rel, data in files:
        if PurePosixPath(rel).suffix not in (".yml", ".yaml") or rel in _PROJECT_YAML:
            continue
        try:
            doc = yaml.safe_load(data.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            continue
        for source in doc.get("sources") or [] if isinstance(doc, Mapping) else []:
            if not isinstance(source, Mapping) or not isinstance(source.get("name"), str):
                continue
            for table in source.get("tables") or []:
                if isinstance(table, Mapping) and isinstance(table.get("name"), str):
                    pair = (source["name"], table["name"])
                    found.setdefault(table["name"].lower(), []).append(pair)
    return found


def _source_for(
    check: DqCheck,
    sources: Mapping[str, List[Tuple[str, str]]],
    where: str,
    refusals: List[UnsupportedBindingError],
) -> Optional[Tuple[str, str]]:
    reference = check.reference
    assert reference is not None
    if reference.expose_id is None:
        refusals.append(
            _refusal(
                "reference",
                f"{where} rule {check.rule_id!r} on expose {check.expose_id!r} checks that every "
                f"{check.column} exists in {reference.product_id}.{reference.column}, and "
                f"consumes[] does not list {reference.product_id}: a dbt test reads the other "
                "product from the dbt source the project declares for the expose it consumes.",
                f"List {reference.product_id} in consumes[] with the exposeId of its table, and "
                "declare that table as a dbt source the model reads.",
            )
        )
        return None
    candidates = sorted(set(sources.get(reference.expose_id.lower(), [])))
    if len(candidates) == 1:
        return candidates[0]
    listed = ", ".join(f"{s}.{t}" for s, t in candidates)
    refusals.append(
        _refusal(
            "reference",
            f"{where} rule {check.rule_id!r} on expose {check.expose_id!r} checks that every "
            f"{check.column} exists in {reference.product_id}.{reference.column}, read from the "
            f"dbt source table named {reference.expose_id!r} (the expose this product consumes "
            "it by), and the project declares "
            + (f"{len(candidates)} ({listed})." if candidates else "none."),
            f"Declare exactly one dbt source table named {reference.expose_id} for "
            f"{reference.product_id}, as the model reads it.",
        )
    )
    return None


def _exposes(tables: Sequence[Any]) -> str:
    return ", ".join(repr(t.expose_id) for t in tables)


# ── rendering ────────────────────────────────────────────────────────────────


def _jinja_str(text: str) -> str:
    """``text`` as a Jinja string literal holding no ``{``, ``}``, ``%`` or ``#``: JSON, whose
    escapes Jinja's string literals decode, with those four escaped too, so no value can open
    or close a Jinja block or comment."""
    encoded = json.dumps(text, ensure_ascii=True)
    for char, escape in (("{", "\\u007b"), ("}", "\\u007d"), ("%", "\\u0025"), ("#", "\\u0023")):
        encoded = encoded.replace(char, escape)
    return encoded


def _text(sql: str) -> str:
    """Literal SQL text, emitted through a Jinja expression so the template never reads it."""
    return "{{ " + _jinja_str(sql) + " }}"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _databricks_type(fluid_type: str) -> str:
    """The type Databricks reports for a column the contract declares as ``fluid_type``,
    lower case, no spaces: the type table the table and the model share
    (``engines/dbt/_types.py``), ``decimal`` at Databricks' default ``decimal(10,0)``."""
    from ...engines.dbt._types import sql_type

    resolved = sql_type(fluid_type, "databricks").lower().replace(" ", "")
    return "decimal(10,0)" if resolved == "decimal" else resolved


def render(check: DqCheck, model: str, source: Optional[Tuple[str, str]] = None) -> str:
    """The singular test file for ``check`` on ``model``. ``source`` is the ``(source,
    table)`` a reference reads. Raises ``ValueError`` for a column that is no plain
    identifier."""
    from .databricks_access import quote

    config = (
        f"{{{{ config(severity={_jinja_str(check.severity)}, enabled=true, "
        f"tags=[{_jinja_str(TAG)}], meta={{'fluid_rule_id': {_jinja_str(check.rule_id)}, "
        f"'fluid_expose_id': {_jinja_str(check.expose_id)}}}) }}}}"
    )
    header = (
        f"-- Contract rule {check.name} ({check.rule_type}): written by fluid apply from the "
        "contract. Change the contract, not this file.\n" + config + "\n"
    )
    ref = f"ref({_jinja_str(model)})"
    if check.kind == "schema":
        return header + _schema_sql(check, ref)
    column = quote(str(check.column))
    parent = None
    if check.kind == "reference":
        assert source is not None and check.reference is not None
        parent = (
            f"source({_jinja_str(source[0])}, {_jinja_str(source[1])})",
            quote(check.reference.column),
        )
    if check.share is not None:
        return header + _share_sql(check, ref, column, parent)
    return header + _row_sql(check, ref, column, parent)


def _builtin(test: str, *args: str) -> str:
    return f"{{{{ adapter.dispatch('{test}', 'dbt')({', '.join(args)}) }}}}\n"


def _literals(check: DqCheck) -> List[str]:
    from .databricks_access import string_literal

    return [string_literal(value) for value in check.values]


def _predicate(check: DqCheck, column: str) -> str:
    """The SQL text of the row condition an allowed value meets (regex, bounds, values)."""
    from .databricks_access import string_literal

    if check.kind == "regex":
        return f"cast({column} as string) rlike {_text(string_literal(str(check.pattern)))}"
    if check.kind == "bounds":
        return " and ".join(
            f"{column} {_SQL_OPERATORS[op]} {_number(value)}" for op, value in check.bounds
        )
    if check.kind == "accepted_values":
        return f"{column} in ({', '.join(_text(lit) for lit in _literals(check))})"
    raise AssertionError(check.kind)


def _row_sql(check: DqCheck, ref: str, column: str, parent: Optional[Tuple[str, str]]) -> str:
    """Every row: a failing row is one the check rejects."""
    col = _jinja_str(column)
    if check.kind == "not_null":
        return _builtin("test_not_null", ref, col)
    if check.kind == "unique":
        return _builtin("test_unique", ref, col)
    if check.kind == "accepted_values":
        values = ", ".join(_jinja_str(lit) for lit in _literals(check))
        return _builtin("test_accepted_values", ref, col, f"[{values}]", "false")
    if check.kind == "reference":
        assert parent is not None
        return _builtin("test_relationships", ref, col, parent[0], _jinja_str(parent[1]))
    if check.kind in ("regex", "bounds"):
        return (
            f"select {column} as failing_value\n"
            f"from {{{{ {ref} }}}}\n"
            f"where {column} is not null and not ({_predicate(check, column)})\n"
        )
    if check.kind == "freshness":
        older = "<=" if check.strict else "<"
        return (
            "select newest_value\n"
            f"from (select max({column}) as newest_value from {{{{ {ref} }}}}) as newest\n"
            "where newest_value is null\n"
            f"   or newest_value {older} timestampadd(SECOND, -{int(check.max_age_seconds or 0)}, "
            "current_timestamp())\n"
        )
    raise AssertionError(check.kind)


def _share_sql(check: DqCheck, ref: str, column: str, parent: Optional[Tuple[str, str]]) -> str:
    """A share: one failing row, holding the share measured, when it misses the threshold.
    An empty table (no share to measure) passes, as every per-row test does."""
    operator, threshold = check.share or (">=", 1.0)
    source = f"{{{{ {ref} }}}}"
    if check.kind == "not_null":
        passing, total = f"count({column})", "count(*)"
    elif check.kind == "unique":
        passing, total = f"count(distinct {column})", f"count({column})"
    elif check.kind in ("accepted_values", "regex", "bounds"):
        passing, total = f"count_if({_predicate(check, column)})", f"count({column})"
    elif check.kind == "reference":
        assert parent is not None
        passing, total = "count(parent.reference_value)", f"count(child.{column})"
        source = (
            f"{{{{ {ref} }}}} as child\n"
            f"  left join (select distinct {parent[1]} as reference_value "
            f"from {{{{ {parent[0]} }}}}) as parent\n"
            f"    on child.{column} = parent.reference_value"
        )
    else:
        raise AssertionError(check.kind)
    limit = _number(threshold)
    return (
        f"select passing_share, {limit} as required_share\n"
        f"from (\n  select try_divide({passing}, {total}) as passing_share\n  from {source}\n"
        ") as measured\n"
        f"where passing_share is not null and not (passing_share {_SQL_OPERATORS[operator]} "
        f"{limit})\n"
    )


def _schema_sql(check: DqCheck, ref: str) -> str:
    """One failing row per contract column the table lacks or types otherwise, read from the
    built table (``adapter.get_columns_in_relation``, as dbt_expectations reads it).

    ``ref`` is called outside the ``execute`` guard: dbt records a test's parents while it
    parses (``execute`` false then), and selects a test by them, so a ``ref`` only inside the
    guard leaves the test without a parent and ``dbt build --select +model+`` never runs it
    (seen live: 11 tests found, 10 run)."""
    from .databricks_access import quote

    for name, _kind in check.columns:
        quote(name)  # a plain identifier, or ValueError
    expected = ", ".join(
        f"{_jinja_str(name.lower())}: {_jinja_str(_databricks_type(kind))}"
        for name, kind in check.columns
    )
    return (
        f"{{%- set relation = {ref} -%}}\n"
        f"{{%- set expected = {{{expected}}} -%}}\n"
        "{%- set problems = [] -%}\n"
        "{%- if execute -%}\n"
        "  {%- set actual = {} -%}\n"
        "  {%- for column in adapter.get_columns_in_relation(relation) -%}\n"
        "    {%- do actual.update({column.name | lower: column.dtype | lower"
        " | replace(' ', '')}) -%}\n"
        "  {%- endfor -%}\n"
        "  {%- for name, kind in expected.items() -%}\n"
        "    {%- if name not in actual -%}\n"
        "      {%- do problems.append([name, 'missing', kind, '']) -%}\n"
        "    {%- elif actual[name] != kind -%}\n"
        "      {%- do problems.append([name, 'type', kind, actual[name]]) -%}\n"
        "    {%- endif -%}\n"
        "  {%- endfor -%}\n"
        "{%- endif -%}\n"
        "{%- for problem in problems %}\n"
        "select '{{ problem[0] }}' as column_name, '{{ problem[1] }}' as problem,"
        " '{{ problem[2] }}' as expected_type,"
        ' \'{{ problem[3] | replace("\\\\", "\\\\\\\\") | replace("\'", "\\\\\'") }}\''
        " as actual_type\n"
        "{%- if not loop.last %}\nunion all{% endif %}\n"
        "{%- else %}\n"
        "select cast(null as string) as column_name, cast(null as string) as problem,"
        " cast(null as string) as expected_type, cast(null as string) as actual_type\n"
        "where 1 = 0\n"
        "{%- endfor %}\n"
    )


# ── results ──────────────────────────────────────────────────────────────────


def results(logs: str) -> Dict[str, Tuple[str, Optional[int]]]:
    """``{test name: (status, failures)}`` from dbt's output; the last line wins (a retried
    task prints its tests again)."""
    found: Dict[str, Tuple[str, Optional[int]]] = {}
    for match in _RESULT_RE.finditer(_ANSI_RE.sub("", logs or "")):
        status, count, name = match.group(1), match.group(2), match.group(3)
        found[name] = (status, int(count) if count else None)
    return found


def report(logs: str, checks: Sequence[DqCheck], *, truncated: bool = False) -> List[str]:
    """Lines for ``fluid apply``: a summary, then one line per contract rule's test with what
    dbt printed for it. Empty when the build tests no rule."""
    if not checks:
        return []
    found = results(logs)
    if not any(check.name in found for check in checks):
        kept = " (Databricks kept only its end)" if truncated else ""
        return [
            f"   contract rules as dbt tests: none of the {len(checks)} reported in the task's "
            f"output{kept}; dbt stopped before running them, or the run page has the rest"
        ]
    lines: List[str] = []
    tally: Dict[str, int] = {}
    marks = {"PASS": "✓", "WARN": "!", "FAIL": "✗", "ERROR": "✗", "SKIP": "-"}
    for check in checks:
        status, failures = found.get(check.name, (None, None))
        key = status or "MISSING"
        tally[key] = tally.get(key, 0) + 1
        if status is None:
            outcome = "not in the task's output" + (
                " (Databricks kept only its end)" if truncated else ""
            )
        elif failures is not None:
            unit = "row" if failures == 1 else "rows"
            outcome = f"{status} {failures} {unit}"
        else:
            outcome = status
        lines.append(
            f"     {marks.get(key, '?')} {check.name}: {outcome} - {describe(check)}, "
            f"severity {check.severity}"
        )
    order = ("PASS", "WARN", "FAIL", "ERROR", "SKIP", "MISSING")
    words = {"MISSING": "not reported"}
    summary = ", ".join(f"{tally[k]} {words.get(k, k.lower())}" for k in order if tally.get(k))
    return [f"   contract rules as dbt tests: {summary}"] + lines


__all__ = [
    "CompiledTests",
    "DqTest",
    "TAG",
    "TEST_DIR",
    "compile_tests",
    "expected_tests",
    "render",
    "report",
    "results",
    "written_tables",
]
