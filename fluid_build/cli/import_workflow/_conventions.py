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
    session_names: [spark, session]  # names whose .sql() and .table() are Spark's
    config_modules:                  # constants kept outside the scanned tree
      - {module: acme_config.settings, path: libs/acme_config/settings.py}
    environment: prod                # which environment the lookups below belong to
    environments:                    # what os.environ[...] holds in each
      prod: {ENV: prod}
      dev:  {ENV: dev}
    defaults: {database: retail, domain: retail, platform: aws}
    catalogs:                        # the first part of a three-part table name
      glue: {platform: aws, format: iceberg, catalog: glue}
    layers: {raw: Bronze, curated: Silver, marts: Gold}   # database -> layer
    owners:                          # the owner label a DAG uses -> the contract's owner
      data-eng: {team: data-engineering, email: data-eng@example.com}
    ignore:
      tables: ["^tmp_", "_staging$"]   # regular expressions
      paths: ["^s3://lake/tmp/"]
      files: ["jobs/legacy/*"]         # globs, relative to the scanned directory
    products: {id: "{database}.{table}"}   # how a product id is made

Every key is optional. A path in the file is relative to the file and stays inside the file's
directory: a path that leaves it (``..``, an absolute path, a link that points out) is refused,
so a conventions file cannot make the scan read a file from somewhere else.

The file is the operator's own, and is trusted as input to the tool: its regular expressions
are compiled as written (a pattern that backtracks badly is the operator's to avoid), and the
text each is matched against is cut at 256 characters.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ._airflow_dags import SUBMIT_KINDS
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
        "session_names",
        "config_modules",
        "environment",
        "environments",
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
class Conventions:
    """The parsed conventions. ``Conventions()`` is the file with nothing in it."""

    #: The file's own path, when there is one.
    source: Optional[Path] = None
    #: ``(deployed prefix, local directory)``, longest prefix first.
    script_roots: Tuple[Tuple[str, Path], ...] = ()
    paths: Tuple[PathRule, ...] = ()
    helpers: Mapping[str, HelperSpec] = field(default_factory=dict)
    operators: Mapping[str, str] = field(default_factory=dict)
    session_names: Tuple[str, ...] = ()
    #: ``{dotted module name: file}`` for constants kept outside the scanned tree.
    config_modules: Mapping[str, Path] = field(default_factory=dict)
    environment: Optional[str] = None
    #: What ``os.environ[...]`` holds in the selected environment.
    variables: Mapping[str, str] = field(default_factory=dict)
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
    return ConventionsError(f"{at}{where}: {why}")


def _text(
    value: Any, path: Optional[Path], where: str, *, pattern: Optional["re.Pattern[str]"] = None
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, where, "expected a non-empty string")
    if len(value) > MAX_TEXT:
        raise _fail(path, where, f"longer than {MAX_TEXT} characters")
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


def _local(base: Path, value: Any, path: Optional[Path], where: str) -> Path:
    """A path in the file: relative to the file's directory, and inside it."""
    text = _text(value, path, where)
    try:
        root = base.resolve()
        resolved = (base / text).resolve()
    except (OSError, RuntimeError) as exc:  # a link loop
        raise _fail(path, where, f"{text!r} cannot be resolved ({type(exc).__name__})") from None
    if resolved != root and root not in resolved.parents:
        raise _fail(
            path,
            where,
            f"{text!r} leaves the directory of the conventions file: keep what the scan reads "
            "inside it",
        )
    return resolved


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


def parse_conventions(document: Any, source: Optional[Path] = None) -> Conventions:
    """The :class:`Conventions` a loaded YAML document describes.

    Raises:
        ConventionsError: a key is unknown or a value has the wrong shape.
    """
    base = source.parent if source is not None else Path.cwd()
    if document is None:
        return Conventions(source=source)
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
                _local(base, local, source, f"script_roots.{prefix}"),
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
            base, entry["path"], source, f"{where}.path"
        )

    environments: Dict[str, Dict[str, str]] = {}
    for name, raw in _mapping(top.get("environments", {}), source, "environments").items():
        env = _mapping(raw, source, f"environments.{name}")
        environments[name] = {
            _text(k, source, f"environments.{name}", pattern=_IDENT): _text(
                v if isinstance(v, str) else str(v), source, f"environments.{name}.{k}"
            )
            for k, v in env.items()
        }
    environment = top.get("environment")
    if environment is not None:
        environment = _text(environment, source, "environment")
        if environment not in environments:
            raise _fail(source, "environment", f"{environment!r} is not named under environments")
    elif len(environments) == 1:
        environment = next(iter(environments))
    elif environments:
        raise _fail(
            source, "environment", f"name the one to use ({', '.join(sorted(environments))})"
        )

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
        session_names=sessions,
        config_modules=modules,
        environment=environment,
        variables=dict(environments.get(environment or "", {})),
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


def load_conventions(path: Optional[Union[str, Path]]) -> Conventions:
    """Read and parse a conventions file; with no path, the empty conventions.

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
    try:
        document = load_yaml_safe(data.decode("utf-8"), max_bytes=MAX_BYTES)
    except Exception as exc:  # noqa: BLE001 - UnsafeYamlError, yaml's own errors, bad UTF-8
        raise refusal(
            f"the conventions file {file.name} is not valid YAML", type(exc).__name__
        ) from exc
    try:
        return parse_conventions(document, file.resolve())
    except ConventionsError as exc:
        raise refusal(f"the conventions file {file.name} has a problem", str(exc)) from exc
