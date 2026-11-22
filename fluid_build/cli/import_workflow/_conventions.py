# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The conventions file: an organisation's layout, as data.

``fluid import airflow|pyspark --conventions conventions.yaml`` reads one YAML file that says
how a project lays out its code and data, so no organisation's rules live in forge-cli. It holds
data only: no code in it is ever run, a key forge-cli does not know is refused (a typo must not
be silently ignored), and a size cap and the alias limits of
:func:`fluid_build.util.safe_yaml.load_yaml_safe` bound what it can cost.

.. code-block:: yaml

    version: 1
    script_roots:                    # where a submitted script lives in this checkout
      "s3://code-bucket/jobs/": jobs   #   deployed prefix -> local directory
      "/opt/airflow/jobs/": jobs       #   (relative to this file)
    paths:                           # what a data path prefix is
      - prefix: "s3://lake/curated/"
        database: curated
        platform: aws
        format: iceberg
        # table: orders              #   a fixed table name; default: the first segment after the prefix
    helpers:                         # project functions that wrap Spark IO
      read_table:  {reads: table, arg: 0}
      write_table: {writes: table, arg: 1, format: iceberg}
    operators:                       # project operator classes that submit Spark work
      AcmeSparkOperator: spark_submit
    submit_helpers:                  # project functions a Python task calls to submit Spark work
      run_spark_step:                #   its name (or the end of its dotted path: emr.run_spark_step)
        script: script_path          #   where the script is among its arguments
        args: job_args               #   where the script's arguments are (a list), if anywhere
        config: "s3://{bucket}/{key}"  #   where the job's config file is, if anywhere
        kind: emr_add_steps          #   what it submits to, as under operators (spark_submit)
    drivers:                         # a script a task submits that runs jobs by name
      - script: driver.py            #   (its file name, or its last segments)
        argument: --job-list         #   the flag whose value names the list (or a position, 0..)
        job_lists: {dir: libs/job_lists, file: "{value}.yaml", key: jobs}
        jobs: {dir: libs/jobs, file: "{job}.py"}
    session_names: [spark, session]  # names whose .sql() and .table() are Spark's
    config_modules:                  # constants kept outside the scanned tree
      - {module: acme_config.settings, path: libs/acme_config/settings.py}
    environment: prod                # which environment the lookups below belong to
    environments:                    # what os.environ[...] holds in each
      prod: {ENV: prod}
      dev:  {ENV: dev}
    airflow_variables:               # what Variable.get("x") and {{ var.value.x }} hold in each
      prod: {environment: prod}
      dev:  {environment: dev}
    defaults: {database: retail, domain: retail, platform: aws}
    catalogs:                        # the first part of a three-part table name
      glue: {platform: aws, format: iceberg, catalog: glue}
    layers: {raw: Bronze, curated: Silver, marts: Gold}   # database -> layer
    owners:                          # the owner label a DAG uses -> the contract's owner
      data-eng: {team: data-engineering, email: data-eng@example.com}
    external_consumers: [gold.revenue_daily]  # read outside the scanned code (a dashboard, a team)
    ignore:
      tables: ["^tmp_", "_staging$"]   # regular expressions
      paths: ["^s3://lake/tmp/"]
      files: ["jobs/legacy/*"]         # globs, relative to the scanned directory
    products: {id: "{database}.{table}"}   # how a product id is made

Every key is optional. A path in the file is relative to the file and must end up inside the
file's own directory or inside a directory the user named on the command line (the one to scan,
and ``--dags``): a path that leaves them all (``..``, an absolute path, a link that points out) is
refused, checked on the resolved path. So a conventions file cannot make the scan read a file from
somewhere the user did not point it at, and it may sit in a folder of its own beside the
repositories it describes (``config/conventions.yaml``).

``external_consumers`` names the outputs that something outside the scanned code reads (a
dashboard, another team's job): a table (``db.table`` or ``catalog.db.table``, in any case), a topic or a path as the
code names it, or the product id the proposal gives it. The report flags an output that no scanned
job reads and no consumer is declared for ("retire it, or name its consumer"); declaring one clears
the flag on the next import.

``drivers`` says how a submitted script that runs jobs by name is read: ``driver.py --job-list
silver_orders`` runs the jobs the file ``silver_orders.yaml`` lists, each the module ``{job}.py``.
Only data is read: the list is a YAML file in ``dir`` (bounded, safe, never run) whose ``key`` holds
the job names (without ``key``, the file is the list), and each name is a file name in the jobs
``dir``. The task is then joined to every job file the list names. A name or a value that is not a
plain file name (a ``/``, a ``..``) and a path that leaves its ``dir`` are refused.

``submit_helpers`` names the project's own functions that submit Spark work, which a
``PythonOperator`` runs (``python_callable=emr.run_spark_step``, ``op_kwargs={...}``) or another
function of the scan calls. Their own code is not read (it is usually in a library outside the
scan); each entry says where the script is among the arguments the function is given: a keyword
(``script_path``), a position (``0``), or a key path into one (``additional_params.script_path``,
for a function given a whole dict of settings), and ``config`` likewise for the config file the job
reads. Either may be a text with arguments in braces (``"s3://{bucket}/{key}"``). ``kind`` is what
the function submits to, one of the operators' kinds (``spark_submit`` when not given). A name
matches the function a task names by its last parts: ``run_spark_step`` matches
``tasks.emr.run_spark_step``. A function of the DAG files themselves needs no entry: its body is
read (an EMR step list, a ``spark-submit`` it runs).

``airflow_variables`` is read like ``environments``: the names of an Airflow Variable and what each
holds in an environment. ``Variable.get("x")``, ``Variable.get("x", default_var=...)`` (Airflow 2),
``Variable.get("x", default=...)`` (Airflow 3, ``airflow.sdk``) and ``{{ var.value.x }}`` in an
operator's arguments are read as what the selected environment holds for ``x``. A Variable the file
does not name stays unresolved: the default of ``get`` is not assumed, as it is not for
``os.environ.get``. A Variable read as JSON is not read.

An ``environments`` value is text and may be empty (``PREFIX: ""``, a table prefix that is empty
in production): a variable the file names with an empty value has that value, and one it does not
name is unresolved. A YAML null is refused, as it is neither of the two.

The file is the operator's own, and is trusted as input to the tool: its regular expressions
are compiled as written (a pattern that backtracks badly is the operator's to avoid), and the
text each is matched against is cut at 256 characters.
"""

from __future__ import annotations

import functools
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ._airflow_dags import SUBMIT_KINDS
from ._callables import HELPER_KINDS, Place, SubmitHelper, Template
from ._pysource import clean_chars
from ._redact import sensitive_name
from ._spark_io import HelperSpec

DOC = "https://agenticstiger.github.io/forge_docs/cli/import.html"
MAX_BYTES = 256 * 1024
MAX_ENTRIES = 500
MAX_TEXT = 500
MAX_PATTERN = 200
#: Longest text an ``ignore`` pattern is matched against: a table or path name from the code.
MAX_SUBJECT = 256

_TOP_KEYS = frozenset(
    {
        "version",
        "script_roots",
        "paths",
        "helpers",
        "operators",
        "submit_helpers",
        "drivers",
        "external_consumers",
        "session_names",
        "config_modules",
        "environment",
        "environments",
        "airflow_variables",
        "defaults",
        "catalogs",
        "layers",
        "owners",
        "ignore",
        "products",
    }
)
_LAYERS = {"bronze": "Bronze", "silver": "Silver", "gold": "Gold"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_NAME = re.compile(r"[A-Za-z0-9_.\-]+")
_ID_FIELDS = frozenset({"catalog", "database", "table"})
_OWNER_KEYS = ("team", "email", "slack", "oncall")


class ConventionsError(Exception):
    """Raised by this module; :func:`load_conventions` turns it into the CLI's typed error."""


@dataclass(frozen=True)
class PathRule:
    """What a data path prefix is: a table of a database, on a platform, in a format."""

    prefix: str
    database: Optional[str] = None
    table: Optional[str] = None
    platform: Optional[str] = None
    format: Optional[str] = None
    catalog: Optional[str] = None


@dataclass(frozen=True)
class CatalogRule:
    platform: Optional[str] = None
    format: Optional[str] = None
    catalog: Optional[str] = None


@dataclass(frozen=True)
class Driver:
    """A script that runs jobs by name, picked by an argument (see the module docstring)."""

    #: The submitted script's file name, or its trailing segments (``driver.py``, ``pkg/driver.py``).
    script: str
    #: The flag whose value names the job list (``--job-list``), or the argument's position.
    argument: Union[int, str]
    #: The directory of the job lists, and a list's file name with ``{value}`` where the argument's
    #: value goes; ``key`` is where the names are in the file (the file itself when ``None``).
    lists_dir: Path
    list_file: str
    list_key: Optional[str]
    #: The directory of the job files, and a job file's name with ``{job}`` for a name.
    jobs_dir: Path
    job_file: str


@dataclass(frozen=True)
class Conventions:
    """The parsed conventions. ``Conventions()`` is the file with nothing in it."""

    #: The file's own path, when there is one.
    source: Optional[Path] = None
    #: ``(deployed prefix, local directory)``, longest prefix first.
    script_roots: Tuple[Tuple[str, Path], ...] = ()
    paths: Tuple[PathRule, ...] = ()
    helpers: Mapping[str, HelperSpec] = field(default_factory=dict)
    operators: Mapping[str, str] = field(default_factory=dict)
    #: A project's own functions that submit Spark work, by name (see the module docstring).
    submit_helpers: Mapping[str, SubmitHelper] = field(default_factory=dict)
    drivers: Tuple[Driver, ...] = ()
    #: Outputs something outside the scanned code reads, lower case (see the module docstring).
    external_consumers: Tuple[str, ...] = ()
    session_names: Tuple[str, ...] = ()
    #: ``{dotted module name: file}`` for constants kept outside the scanned tree.
    config_modules: Mapping[str, Path] = field(default_factory=dict)
    environment: Optional[str] = None
    #: What ``os.environ[...]`` holds in the selected environment.
    variables: Mapping[str, str] = field(default_factory=dict)
    #: What each Airflow Variable holds in the selected environment.
    airflow_variables: Mapping[str, str] = field(default_factory=dict)
    #: The ``environments`` and ``airflow_variables`` entries of the selected environment that are
    #: not used, as ``section.name`` (see :func:`_not_credentials`).
    withheld: Tuple[str, ...] = ()
    default_database: Optional[str] = None
    domain: Optional[str] = None
    runtime_platform: Optional[str] = None
    catalogs: Mapping[str, CatalogRule] = field(default_factory=dict)
    layers: Mapping[str, str] = field(default_factory=dict)
    owners: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    ignore_tables: Tuple["re.Pattern[str]", ...] = ()
    ignore_paths: Tuple["re.Pattern[str]", ...] = ()
    ignore_files: Tuple[str, ...] = ()
    id_template: str = "{database}.{table}"

    def ignores_table(self, parts: Sequence[str]) -> bool:
        if not parts:
            return False
        dotted, last = ".".join(parts)[:MAX_SUBJECT], parts[-1][:MAX_SUBJECT]
        return any(p.search(dotted) or p.search(last) for p in self.ignore_tables)

    def ignores_path(self, path: str) -> bool:
        subject = path[:MAX_SUBJECT]
        return any(p.search(subject) for p in self.ignore_paths)


def _fail(path: Optional[Path], where: str, why: str) -> ConventionsError:
    at = f"{path.name}: " if path is not None else ""
    # ``where`` holds the names the file gives: they are text from a file that came with a
    # repository, an escape sequence in one would reach the terminal, and a key of 250 KB would
    # make a message of that size. The names are cut so that the reason stays in the message.
    named = where if len(where) <= 200 else where[:197] + "..."
    message = clean_chars(f"{at}{named}: {why}")
    return ConventionsError(message if len(message) <= 400 else message[:397] + "...")


def _text(
    value: Any, path: Optional[Path], where: str, *, pattern: Optional["re.Pattern[str]"] = None
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, where, "expected a non-empty string")
    if len(value) > MAX_TEXT:
        raise _fail(path, where, f"longer than {MAX_TEXT} characters")
    if clean_chars(value) != value:  # these values reach the contracts and the terminal
        raise _fail(path, where, "has a control or invisible character in it")
    if pattern is not None and not pattern.fullmatch(value.strip()):
        raise _fail(path, where, f"{value!r} is not an acceptable name")
    return value.strip()


def _mapping(
    value: Any, path: Optional[Path], where: str, allowed: Optional[frozenset] = None
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(path, where, "expected a mapping")
    if len(value) > MAX_ENTRIES:
        raise _fail(path, where, f"more than {MAX_ENTRIES} entries")
    out: Dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise _fail(path, where, f"the key {key!r} is not text")
        if allowed is not None and key not in allowed:
            raise _fail(
                path, f"{where}.{key}", f"unknown key (allowed: {', '.join(sorted(allowed))})"
            )
        out[key] = item
    return out


def _list(value: Any, path: Optional[Path], where: str) -> List[Any]:
    if not isinstance(value, list):
        raise _fail(path, where, "expected a list")
    if len(value) > MAX_ENTRIES:
        raise _fail(path, where, f"more than {MAX_ENTRIES} entries")
    return value


def _binding_enums() -> Tuple[frozenset, frozenset]:
    """The binding platforms and formats the newest bundled schema accepts."""
    import importlib.resources
    import json

    from fluid_build.schema_manager import FluidSchemaManager

    version = FluidSchemaManager.latest_bundled_version()
    text = (
        importlib.resources.files("fluid_build.schemas")
        .joinpath(f"fluid-schema-{version}.json")
        .read_text(encoding="utf-8")
    )
    binding = json.loads(text)["$defs"]["binding"]["properties"]
    return frozenset(binding["platform"]["enum"]), frozenset(binding["format"]["enum"])


def _runtime_platforms() -> frozenset:
    """The platforms ``execution.runtime.platform`` accepts in the newest bundled schema."""
    import importlib.resources
    import json

    from fluid_build.schema_manager import FluidSchemaManager

    version = FluidSchemaManager.latest_bundled_version()
    text = (
        importlib.resources.files("fluid_build.schemas")
        .joinpath(f"fluid-schema-{version}.json")
        .read_text(encoding="utf-8")
    )
    return frozenset(json.loads(text)["$defs"]["runtime"]["properties"]["platform"]["enum"])


def _enum(value: Any, allowed: frozenset, path: Optional[Path], where: str) -> str:
    text = _text(value, path, where)
    if text not in allowed:
        raise _fail(path, where, f"{text!r} is not one of {', '.join(sorted(allowed))}")
    return text


def _pattern(value: Any, path: Optional[Path], where: str) -> "re.Pattern[str]":
    text = _text(value, path, where)
    if len(text) > MAX_PATTERN:
        raise _fail(path, where, f"a pattern longer than {MAX_PATTERN} characters")
    try:
        return re.compile(text)
    except re.error as exc:
        raise _fail(path, where, f"not a regular expression ({exc})") from None


def _named(directories: Sequence[Union[str, Path]]) -> Tuple[Path, ...]:
    """The directories the user named on the command line, resolved: one named through a link is
    the directory it points to. One that cannot be resolved is left out (the import refuses a
    directory that is not one where it reads it)."""
    found: List[Path] = []
    for item in directories:
        try:
            found.append(Path(item).resolve())
        except (OSError, RuntimeError, ValueError):  # a link loop, a name the system refuses
            continue
    return tuple(found)


def _local(
    base: Path, value: Any, path: Optional[Path], where: str, named: Sequence[Path] = ()
) -> Path:
    """A path in the file: relative to the file's directory, and (once resolved, so a link counts
    for where it points) inside that directory or one of ``named``."""
    text = _text(value, path, where)
    try:
        root = base.resolve()
        resolved = (base / text).resolve()
    except (OSError, RuntimeError, ValueError) as exc:  # a link loop, a NUL in the name
        raise _fail(path, where, f"{text!r} cannot be resolved ({type(exc).__name__})") from None
    if not any(resolved == inside or inside in resolved.parents for inside in (root, *named)):
        raise _fail(
            path,
            where,
            f"{text!r} leaves the directory of the conventions file and the directories named on "
            "the command line: keep what the scan reads inside one of them",
        )
    return resolved


def _env_value(value: Any, path: Optional[Path], where: str) -> str:
    """What an environment variable or an Airflow Variable holds, as written. The empty string is
    a value (a prefix that is empty in production) and not the same as one the file does not name,
    which is unresolved. It must be text: YAML reads an unquoted ``1.10``, ``007``, ``yes`` or
    ``2020-1-1`` as a number, a bool or a date, and the text it was written as is gone, so a
    value that is not text is refused (a null, a list and a mapping too: none of them is a value
    the code could have read)."""
    if not isinstance(value, str):
        raise _fail(
            path,
            where,
            'expected text: write "" for an empty value, put anything YAML would read as a number, '
            "a yes/no or a date in quotes, or leave the variable out",
        )
    if len(value) > MAX_TEXT:
        raise _fail(path, where, f"longer than {MAX_TEXT} characters")
    return value


#: Names of ``DEFAULT_SENSITIVE_FIELDS`` in Airflow's secrets masker that the importer's own
#: judgement of a name (:func:`sensitive_name`) does not know. Airflow hides a name that contains
#: one of them.
_AIRFLOW_HIDDEN = ("keyfile_dict", "service_account")


def _not_credentials(values: Mapping[str, str], section: str) -> Tuple[Dict[str, str], List[str]]:
    """``values`` without the ones whose name says they are a credential (``db_password``,
    ``API_TOKEN``: the judgement the rest of the importer uses for a name, which covers the names
    Airflow itself hides in its logs), and ``section.name`` of each one left out. A value kept in
    the conventions is written into the contracts where the code reads it; a credential is not,
    so the code's own reference (``{{ var.value.db_password }}``, ``Variable.get(...)``) stays
    as it is and the report says what to set."""
    kept: Dict[str, str] = {}
    left: List[str] = []
    for name, value in values.items():
        if sensitive_name(name) or any(part in name.lower() for part in _AIRFLOW_HIDDEN):
            left.append(f"{section}.{name}")
        else:
            kept[name] = value
    return kept, left


_SCRIPT = re.compile(r"[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*")
_FLAG = re.compile(r"--?[A-Za-z0-9][A-Za-z0-9_\-]*")
_FILE_TEMPLATE = re.compile(r"[A-Za-z0-9_.\-]*\{(?P<name>value|job)\}[A-Za-z0-9_.\-]*")


def _file_template(value: Any, path: Optional[Path], where: str, placeholder: str) -> str:
    """A file name with ``{placeholder}`` in it once: a plain name, never a path."""
    text = _text(value, path, where)
    found = _FILE_TEMPLATE.fullmatch(text)
    if found is None or found.group("name") != placeholder or ".." in text:
        raise _fail(
            path,
            where,
            f"{text!r} is not a file name with {{{placeholder}}} in it (letters, digits, "
            "'_', '-' and '.', no '/')",
        )
    return text


def _argument(value: Any, path: Optional[Path], where: str) -> Union[int, str]:
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0 or value > 1_000:
            raise _fail(path, where, "a position is 0 or more")
        return value
    text = _text(value, path, where)
    if not _FLAG.fullmatch(text):
        raise _fail(path, where, f"{text!r} is not a flag such as --job-list, or a position")
    return text


def _driver(
    raw: Any, base: Path, source: Optional[Path], where: str, named: Sequence[Path]
) -> Driver:
    entry = _mapping(raw, source, where, frozenset({"script", "argument", "job_lists", "jobs"}))
    missing = sorted({"script", "argument", "job_lists", "jobs"} - set(entry))
    if missing:
        raise _fail(source, where, f"give {', '.join(missing)}")
    script = _text(entry["script"], source, f"{where}.script", pattern=_SCRIPT)
    if ".." in script.split("/"):
        raise _fail(source, f"{where}.script", f"{script!r} has a '..' in it")
    lists = _mapping(
        entry["job_lists"], source, f"{where}.job_lists", frozenset({"dir", "file", "key"})
    )
    jobs = _mapping(entry["jobs"], source, f"{where}.jobs", frozenset({"dir", "file"}))
    for block, needed in ((lists, "job_lists"), (jobs, "jobs")):
        if not {"dir", "file"} <= set(block):
            raise _fail(source, f"{where}.{needed}", "give dir and file")
    return Driver(
        script=script,
        argument=_argument(entry["argument"], source, f"{where}.argument"),
        lists_dir=_local(base, lists["dir"], source, f"{where}.job_lists.dir", named),
        list_file=_file_template(lists["file"], source, f"{where}.job_lists.file", "value"),
        list_key=(
            _text(lists["key"], source, f"{where}.job_lists.key", pattern=_IDENT)
            if "key" in lists
            else None
        ),
        jobs_dir=_local(base, jobs["dir"], source, f"{where}.jobs.dir", named),
        job_file=_file_template(jobs["file"], source, f"{where}.jobs.file", "job"),
    )


#: Where a submit helper's value is: a keyword or a position, then keys into it.
_PLACE = re.compile(r"(?:[0-9]{1,4}|[A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z0-9_\-]+)*")
_PLACE_FIELD = re.compile(r"\{([^{}]*)\}")
_PLACE_HELP = (
    "expected an argument: a keyword (script_path), a position (0, 1, ...) or a key path into "
    "one (additional_params.script_path)"
)


def _place(value: Any, path: Optional[Path], where: str) -> Place:
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value <= 1_000:
            raise _fail(path, where, "a position is 0 or more")
        return value, ()
    text = _text(value, path, where)
    if not _PLACE.fullmatch(text):
        raise _fail(path, where, f"{text!r}: {_PLACE_HELP}")
    head, *keys = text.split(".")
    return (int(head) if head.isdigit() else head), tuple(keys)


def _helper_text(value: Any, path: Optional[Path], where: str) -> Template:
    """A submit helper's script or config: an argument, or a text with arguments in braces
    (``s3://{bucket}/{key}``)."""
    if not isinstance(value, str) or ("{" not in value and "}" not in value):
        return (_place(value, path, where),)
    text = _text(value, path, where)
    parts: List[Any] = []
    at = 0
    for found in _PLACE_FIELD.finditer(text):
        literal = text[at : found.start()]
        if "{" in literal or "}" in literal:
            break
        if literal:
            parts.append(literal)
        parts.append(_place(found.group(1), path, f"{where} {{{found.group(1)}}}"))
        at = found.end()
    else:
        if "{" not in text[at:] and "}" not in text[at:]:
            if text[at:]:
                parts.append(text[at:])
            return tuple(parts)
    raise _fail(path, where, f"{text!r} has a brace that is not around an argument")


def _submit_helper(raw: Any, path: Optional[Path], where: str) -> SubmitHelper:
    entry = _mapping(raw, path, where, frozenset({"script", "args", "config", "kind"}))
    if "script" not in entry:
        raise _fail(path, where, "the script is required: where it is among the arguments")
    kind = _text(entry.get("kind", "spark_submit"), path, f"{where}.kind")
    if kind not in HELPER_KINDS:
        raise _fail(
            path, f"{where}.kind", f"{kind!r} is not one of {', '.join(sorted(HELPER_KINDS))}"
        )
    return SubmitHelper(
        script=_helper_text(entry["script"], path, f"{where}.script"),
        runtime=HELPER_KINDS[kind],
        args=_place(entry["args"], path, f"{where}.args") if "args" in entry else None,
        config=(
            _helper_text(entry["config"], path, f"{where}.config") if "config" in entry else None
        ),
    )


def _template(value: Any, path: Optional[Path], where: str) -> str:
    text = _text(value, path, where)
    try:
        fields = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise _fail(path, where, f"not a template ({exc})") from None
    for _literal, name, spec, conversion in fields:
        if name is None:
            continue
        if name not in _ID_FIELDS or spec or conversion:
            raise _fail(
                path,
                where,
                f"only {{catalog}}, {{database}} and {{table}} may be used, not {{{name}}}",
            )
    return text


def parse_conventions(
    document: Any,
    source: Optional[Path] = None,
    *,
    roots: Sequence[Union[str, Path]] = (),
) -> Conventions:
    """The :class:`Conventions` a loaded YAML document describes.

    ``roots`` are the directories the user named on the command line: a path in the document may
    point into one of them as well as into the directory of ``source``.

    Raises:
        ConventionsError: a key is unknown or a value has the wrong shape.
    """
    base = source.parent if source is not None else Path.cwd()
    if document is None:
        return Conventions(source=source)
    named = _named(roots)
    top = _mapping(document, source, "the file", _TOP_KEYS)
    if "version" in top and top["version"] != 1:
        raise _fail(
            source, "version", f"{top['version']!r} is not a version this forge-cli reads (1)"
        )

    platforms: Optional[frozenset] = None
    formats: Optional[frozenset] = None

    def enums() -> Tuple[frozenset, frozenset]:
        nonlocal platforms, formats
        if platforms is None or formats is None:
            platforms, formats = _binding_enums()
        return platforms, formats

    script_roots = []
    for prefix, local in _mapping(top.get("script_roots", {}), source, "script_roots").items():
        script_roots.append(
            (
                _text(prefix, source, "script_roots"),
                _local(base, local, source, f"script_roots.{prefix}", named),
            )
        )
    script_roots.sort(key=lambda item: -len(item[0]))

    rules = []
    for index, raw in enumerate(_list(top.get("paths", []), source, "paths")):
        where = f"paths[{index}]"
        entry = _mapping(
            raw,
            source,
            where,
            frozenset({"prefix", "database", "table", "platform", "format", "catalog"}),
        )
        if "prefix" not in entry:
            raise _fail(source, where, "the prefix is required")
        rules.append(
            PathRule(
                prefix=_text(entry["prefix"], source, f"{where}.prefix"),
                database=(
                    _text(entry["database"], source, f"{where}.database", pattern=_NAME)
                    if "database" in entry
                    else None
                ),
                table=(
                    _text(entry["table"], source, f"{where}.table", pattern=_NAME)
                    if "table" in entry
                    else None
                ),
                platform=(
                    _enum(entry["platform"], enums()[0], source, f"{where}.platform")
                    if "platform" in entry
                    else None
                ),
                format=(
                    _enum(entry["format"], enums()[1], source, f"{where}.format")
                    if "format" in entry
                    else None
                ),
                catalog=(
                    _text(entry["catalog"], source, f"{where}.catalog", pattern=_NAME)
                    if "catalog" in entry
                    else None
                ),
            )
        )
    rules.sort(key=lambda rule: -len(rule.prefix))

    helpers: Dict[str, HelperSpec] = {}
    for name, raw in _mapping(top.get("helpers", {}), source, "helpers").items():
        where = f"helpers.{name}"
        _text(name, source, where, pattern=_IDENT)
        entry = _mapping(raw, source, where, frozenset({"reads", "writes", "arg", "format"}))
        if ("reads" in entry) == ("writes" in entry):
            raise _fail(source, where, "give exactly one of reads and writes")
        direction = "reads" if "reads" in entry else "writes"
        kind = _text(entry[direction], source, f"{where}.{direction}")
        if kind not in {"table", "path", "topic"}:
            raise _fail(source, f"{where}.{direction}", "expected table, path or topic")
        arg = entry.get("arg", 0)
        if isinstance(arg, bool) or not (
            isinstance(arg, int) and arg >= 0 or isinstance(arg, str) and _IDENT.fullmatch(arg)
        ):
            raise _fail(source, f"{where}.arg", "expected a position (0, 1, ...) or a keyword name")
        fmt = (
            _text(entry["format"], source, f"{where}.format", pattern=_NAME).lower()
            if "format" in entry
            else None
        )
        helpers[name] = HelperSpec(direction, kind, arg, fmt)

    operators: Dict[str, str] = {}
    for name, kind in _mapping(top.get("operators", {}), source, "operators").items():
        _text(name, source, f"operators.{name}", pattern=_IDENT)
        text = _text(kind, source, f"operators.{name}")
        if text not in SUBMIT_KINDS:
            raise _fail(
                source,
                f"operators.{name}",
                f"{text!r} is not one of {', '.join(sorted(SUBMIT_KINDS))}",
            )
        operators[name] = text

    submit_helpers: Dict[str, SubmitHelper] = {}
    for name, raw in _mapping(top.get("submit_helpers", {}), source, "submit_helpers").items():
        where = f"submit_helpers.{name}"
        submit_helpers[_text(name, source, where, pattern=_DOTTED)] = _submit_helper(
            raw, source, where
        )

    drivers = tuple(
        _driver(raw, base, source, f"drivers[{index}]", named)
        for index, raw in enumerate(_list(top.get("drivers", []), source, "drivers"))
    )

    external = tuple(
        dict.fromkeys(
            _text(item, source, f"external_consumers[{i}]").lower().rstrip("/")
            for i, item in enumerate(
                _list(top.get("external_consumers", []), source, "external_consumers")
            )
        )
    )

    sessions = tuple(
        _text(item, source, f"session_names[{i}]", pattern=_IDENT)
        for i, item in enumerate(_list(top.get("session_names", []), source, "session_names"))
    )

    modules: Dict[str, Path] = {}
    for index, raw in enumerate(_list(top.get("config_modules", []), source, "config_modules")):
        where = f"config_modules[{index}]"
        entry = _mapping(raw, source, where, frozenset({"module", "path"}))
        if set(entry) != {"module", "path"}:
            raise _fail(source, where, "give both module and path")
        modules[_text(entry["module"], source, f"{where}.module", pattern=_DOTTED)] = _local(
            base, entry["path"], source, f"{where}.path", named
        )

    environments: Dict[str, Dict[str, str]] = {}
    for name, raw in _mapping(top.get("environments", {}), source, "environments").items():
        env = _mapping(raw, source, f"environments.{name}")
        environments[name] = {
            _text(k, source, f"environments.{name}", pattern=_IDENT): _env_value(
                v, source, f"environments.{name}.{k}"
            )
            for k, v in env.items()
        }
    airflow_variables: Dict[str, Dict[str, str]] = {}
    for name, raw in _mapping(
        top.get("airflow_variables", {}), source, "airflow_variables"
    ).items():
        found = _mapping(raw, source, f"airflow_variables.{name}")
        airflow_variables[name] = {
            _text(k, source, f"airflow_variables.{name}", pattern=_NAME): _env_value(
                v, source, f"airflow_variables.{name}.{k}"
            )
            for k, v in found.items()
        }
    named = sorted({*environments, *airflow_variables})
    environment = top.get("environment")
    if environment is not None:
        environment = _text(environment, source, "environment")
        if environment not in named:
            raise _fail(
                source,
                "environment",
                f"{environment!r} is not named under environments or airflow_variables",
            )
    elif len(named) == 1:
        environment = named[0]
    elif named:
        raise _fail(source, "environment", f"name the one to use ({', '.join(named)})")

    chosen = environment or ""
    used_variables, withheld = _not_credentials(
        environments.get(chosen, {}), f"environments.{chosen}"
    )
    used_airflow, withheld_airflow = _not_credentials(
        airflow_variables.get(chosen, {}), f"airflow_variables.{chosen}"
    )
    withheld = [*withheld, *withheld_airflow]

    defaults = _mapping(
        top.get("defaults", {}), source, "defaults", frozenset({"database", "domain", "platform"})
    )
    default_platform = (
        _enum(defaults["platform"], _runtime_platforms(), source, "defaults.platform")
        if "platform" in defaults
        else None
    )

    catalogs: Dict[str, CatalogRule] = {}
    for name, raw in _mapping(top.get("catalogs", {}), source, "catalogs").items():
        entry = _mapping(
            raw, source, f"catalogs.{name}", frozenset({"platform", "format", "catalog"})
        )
        catalogs[_text(name, source, "catalogs", pattern=_NAME).lower()] = CatalogRule(
            platform=(
                _enum(entry["platform"], enums()[0], source, f"catalogs.{name}.platform")
                if "platform" in entry
                else None
            ),
            format=(
                _enum(entry["format"], enums()[1], source, f"catalogs.{name}.format")
                if "format" in entry
                else None
            ),
            catalog=(
                _text(entry["catalog"], source, f"catalogs.{name}.catalog", pattern=_NAME)
                if "catalog" in entry
                else None
            ),
        )

    layers: Dict[str, str] = {}
    for database, layer in _mapping(top.get("layers", {}), source, "layers").items():
        text = _text(layer, source, f"layers.{database}").lower()
        if text not in _LAYERS:
            raise _fail(source, f"layers.{database}", "expected Bronze, Silver or Gold")
        layers[database.lower()] = _LAYERS[text]

    owners: Dict[str, Dict[str, str]] = {}
    for label, raw in _mapping(top.get("owners", {}), source, "owners").items():
        entry = _mapping(raw, source, f"owners.{label}", frozenset(_OWNER_KEYS))
        if not entry:
            raise _fail(source, f"owners.{label}", "name at least a team or an email")
        block = {key: _text(value, source, f"owners.{label}.{key}") for key, value in entry.items()}
        if "email" in block and not re.fullmatch(r"[^@\s]+@[^@\s]+", block["email"]):
            raise _fail(source, f"owners.{label}.email", "not an email address")
        owners[label] = block

    ignore = _mapping(
        top.get("ignore", {}), source, "ignore", frozenset({"tables", "paths", "files"})
    )
    ignore_tables = tuple(
        _pattern(p, source, f"ignore.tables[{i}]")
        for i, p in enumerate(_list(ignore.get("tables", []), source, "ignore.tables"))
    )
    ignore_paths = tuple(
        _pattern(p, source, f"ignore.paths[{i}]")
        for i, p in enumerate(_list(ignore.get("paths", []), source, "ignore.paths"))
    )
    ignore_files = tuple(
        _text(p, source, f"ignore.files[{i}]")
        for i, p in enumerate(_list(ignore.get("files", []), source, "ignore.files"))
    )

    products = _mapping(top.get("products", {}), source, "products", frozenset({"id"}))
    id_template = (
        _template(products["id"], source, "products.id")
        if "id" in products
        else "{database}.{table}"
    )

    return Conventions(
        source=source,
        script_roots=tuple(script_roots),
        paths=tuple(rules),
        helpers=helpers,
        operators=operators,
        submit_helpers=submit_helpers,
        drivers=drivers,
        external_consumers=external,
        session_names=sessions,
        config_modules=modules,
        environment=environment,
        variables=used_variables,
        airflow_variables=used_airflow,
        withheld=tuple(withheld),
        default_database=(
            _text(defaults["database"], source, "defaults.database", pattern=_NAME).lower()
            if "database" in defaults
            else None
        ),
        domain=(
            _text(defaults["domain"], source, "defaults.domain", pattern=_NAME)
            if "domain" in defaults
            else None
        ),
        runtime_platform=default_platform,
        catalogs=catalogs,
        layers=layers,
        owners=owners,
        ignore_tables=ignore_tables,
        ignore_paths=ignore_paths,
        ignore_files=ignore_files,
        id_template=id_template,
    )


@functools.lru_cache(maxsize=1)
def unique_key_loader() -> Any:
    """A YAML loader that refuses a mapping that gives a key twice (YAML keeps the last, so a
    declared consumer or a driver would go unseen), in the one pass that builds the value: its
    ``DuplicateKey`` is what it raises, with the key and the line. A merge key (``<<``) may come
    twice; a quoted ``"<<"`` is an ordinary key, and ``1`` and ``"1"`` are two."""
    import yaml

    class DuplicateKey(yaml.constructor.ConstructorError):
        pass

    class UniqueKeys(yaml.SafeLoader):
        def construct_mapping(self, node: Any, deep: bool = False) -> Any:
            if isinstance(node, yaml.MappingNode):
                seen: Set[Any] = set()
                for key_node, _value in node.value:
                    if key_node.tag == "tag:yaml.org,2002:merge":
                        continue
                    try:
                        key = self.construct_object(key_node, deep=True)
                        known = key in seen
                    except Exception:  # noqa: BLE001 - not a key; the loader says why
                        continue
                    if known:
                        raise DuplicateKey(
                            None, None, f"the key {key!r} is given twice", key_node.start_mark
                        )
                    seen.add(key)
            return super().construct_mapping(node, deep=deep)

    UniqueKeys.DuplicateKey = DuplicateKey  # type: ignore[attr-defined]
    return UniqueKeys


def duplicate_key_text(exc: Exception) -> str:
    """What a :meth:`unique_key_loader` refusal says: the key, and the line it is on."""
    mark = getattr(exc, "problem_mark", None)
    line = f" (line {mark.line + 1})" if mark is not None else ""
    return clean_chars(f"{getattr(exc, 'problem', 'a key is given twice')}{line}")[:200]


def load_conventions(
    path: Optional[Union[str, Path]], *, roots: Sequence[Union[str, Path]] = ()
) -> Conventions:
    """Read and parse a conventions file; with no path, the empty conventions.

    ``roots`` are the directories the user named on the command line (the one to scan, and
    ``--dags``): a path in the file may point into one of them as well as into the directory of
    the file.

    Raises:
        FluidUserError: (a ``SchemaValidationError``) the file cannot be read, is not safe YAML
            or has a key or value that is not allowed. The message names the key.
    """
    if path is None:
        return Conventions()
    from fluid_build._errors import SchemaValidationError
    from fluid_build.util.safe_yaml import load_yaml_safe, read_bounded_file

    file = Path(path)

    def refusal(what: str, why: str) -> Exception:
        return SchemaValidationError(
            what=what,
            why=why,
            fix="Fix the conventions file (see the keys under `fluid import airflow --help`), "
            "or run without --conventions.",
            doc=DOC,
            extras={"conventions": file.name},
        )

    try:
        data = read_bounded_file(file, MAX_BYTES)
    except OSError as exc:
        raise refusal(
            f"cannot read the conventions file {file.name}",
            f"{type(exc).__name__}: {exc.strerror or exc}",
        ) from exc
    if data is None:
        raise refusal(
            f"the conventions file {file.name} is too large", f"it is over {MAX_BYTES:,} bytes"
        )
    loader = unique_key_loader()
    try:
        document = load_yaml_safe(data.decode("utf-8"), max_bytes=MAX_BYTES, loader=loader)
    except loader.DuplicateKey as exc:
        raise refusal(
            f"the conventions file {file.name} has a problem",
            f"{duplicate_key_text(exc)}: YAML would keep the last",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - UnsafeYamlError, yaml's own errors, bad UTF-8
        raise refusal(
            f"the conventions file {file.name} is not valid YAML", type(exc).__name__
        ) from exc
    try:
        return parse_conventions(document, file.resolve(), roots=roots)
    except ConventionsError as exc:
        raise refusal(f"the conventions file {file.name} has a problem", str(exc)) from exc
