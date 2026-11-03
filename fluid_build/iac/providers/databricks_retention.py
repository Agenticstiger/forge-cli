# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``lifecycle.expire`` on a Databricks table: limit the Delta history.

Nothing on Databricks deletes rows by age. What can make a retention promise
true is the table's shape: a table rebuilt **in full** on every run holds no row
copy older than the last run, except in its Delta history (the old data files
Delta keeps for time travel). ``VACUUM`` removes those files once they are older
than the table's retention, and predictive optimization runs ``VACUUM`` on Unity
Catalog managed tables. So ``expire: true`` is accepted when

1. every build that writes the expose rewrites the whole table:

   * a dbt build whose model for the table is materialized as ``table``
     (``CREATE OR REPLACE`` each run). ``incremental`` and ``snapshot`` keep old
     rows, a ``view`` stores none, and anything dbt cannot be shown to have
     resolved to ``table`` is refused, not guessed;
   * forge-cli's own DuckDB acquisition. The landing it does into Databricks is
     an ``INSERT OVERWRITE`` of the whole table, and this module relies on that:
     an append there would keep every run's rows and make this acceptance false;

2. the contract's ``lifecycle.retention`` is at least the table's history
   retention, the larger of ``delta.logRetentionDuration`` (default 30 days) and
   ``delta.deletedFileRetentionDuration`` (default 7 days)
   (https://docs.databricks.com/aws/en/delta/history,
   https://docs.delta.io/latest/delta-batch.html#data-retention). A retention
   under the 30-day default is refused for now: this module does not set the
   table's properties, since dbt owns a dbt-written table's definition.

``fluid verify`` is the gate on the live table (``cli/_verify_databricks.py``).

dbt's configuration is resolved statically, as dbt resolves it
(https://docs.getdbt.com/reference/model-configs,
https://docs.getdbt.com/reference/dbt-jinja-functions/config): the ``config()``
call in the model's SQL beats the model's ``config:`` in a properties YAML, which
beats ``dbt_project.yml`` (``models:``, the most specific path winning); the
default materialization is ``view``. The model is the one whose relation name
(its ``alias``, else its file name) is the expose's table. A value that is not a
plain literal (``var()``, a conditional ``config()``) is *undeterminable* and
refused, with the fix.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from ...util.contract import builds_writing_expose, is_duckdb_acquisition_build
from ..base import UnsupportedBindingError
from ..source import current_contract_source

DAY = 86400

#: Delta's table properties for how long history is kept, and their defaults
#: (Databricks "Work with Delta Lake table history").
LOG_RETENTION = "delta.logRetentionDuration"
DELETED_FILE_RETENTION = "delta.deletedFileRetentionDuration"
DEFAULT_LOG_SECONDS = 30 * DAY
DEFAULT_DELETED_FILE_SECONDS = 7 * DAY

#: The most a project read here may hold: the same bounds the job upload keeps.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 1000


def _refusal(kind: str, message: str, *remediation: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-retention-{kind}", message, tuple(remediation))


# ── Durations ───────────────────────────────────────────────────────────────

#: The schema's ``isoDuration`` pattern (``fluid-schema-0.7.7.json``), as groups. The ``T``
#: needs a time part after it (``PT``, ``P1DT`` are not durations), digits are bounded,
#: and a fraction, a sign or a lower-case letter is not accepted.
_ISO_RE = re.compile(
    r"^P(?!$)(?:(\d{1,12})Y)?(?:(\d{1,12})M)?(?:(\d{1,12})W)?(?:(\d{1,12})D)?"
    r"(?:T(?=\d)(?:(\d{1,12})H)?(?:(\d{1,12})M)?(?:(\d{1,12})S)?)?$"
)

#: A year and a month are not fixed lengths. The *shortest* is taken, so a contract's
#: retention is never read as longer than it is, and the check that it covers the
#: history never passes one that does not: P1Y = 365 days (not 366), P1M = 28 days (not 30).
_YEAR_DAYS = 365
_MONTH_DAYS = 28


def iso_duration_seconds(text: Any) -> int:
    """An ISO 8601 duration in seconds, years and months at their shortest.

    Raises ``ValueError`` for anything the schema's ``isoDuration`` does not allow.
    """
    if not isinstance(text, str) or not (match := _ISO_RE.match(text.strip())):
        raise ValueError(f"{text!r} is not an ISO 8601 duration")
    years, months, weeks, days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return (
        (years * _YEAR_DAYS + months * _MONTH_DAYS + weeks * 7 + days) * DAY
        + hours * 3600
        + minutes * 60
        + seconds
    )


_INTERVAL_PART = re.compile(
    r"(\d{1,12})\s*(week|day|hour|minute|second|millisecond|microsecond)s?\b", re.IGNORECASE
)
_UNIT_SECONDS = {
    "week": 7 * DAY,
    "day": DAY,
    "hour": 3600,
    "minute": 60,
    "second": 1,
    "millisecond": 0.001,
    "microsecond": 0.000001,
}


def interval_seconds(text: Any) -> int:
    """A Delta interval property (``interval 30 days``, ``interval 1 week 12 hours``) in
    seconds, rounded up. Delta takes no month or year here; ``ValueError`` for anything
    else, so an unreadable property is never taken as a short one."""
    if not isinstance(text, str):
        raise ValueError(f"{text!r} is not an interval")
    body = re.sub(r"^\s*interval\b", "", text.strip(), flags=re.IGNORECASE).strip()
    parts = _INTERVAL_PART.findall(body)
    if not parts or _INTERVAL_PART.sub("", body).strip():
        raise ValueError(f"{text!r} is not a Delta interval such as 'interval 30 days'")
    total = sum(int(n) * _UNIT_SECONDS[unit.lower()] for n, unit in parts)
    return int(-(-total // 1))


def history_seconds(properties: Mapping[str, Any]) -> int:
    """The Delta history retention of a table with these properties: the larger of the log
    retention and the deleted-file retention, each at its default when unset.

    Property names are matched case-insensitively, as Delta does. ``ValueError`` when one
    is set to something that is not an interval.
    """
    lowered = {str(k).lower(): v for k, v in properties.items()}
    log = lowered.get(LOG_RETENTION.lower())
    deleted = lowered.get(DELETED_FILE_RETENTION.lower())
    return max(
        DEFAULT_LOG_SECONDS if log is None else interval_seconds(log),
        DEFAULT_DELETED_FILE_SECONDS if deleted is None else interval_seconds(deleted),
    )


def days_text(seconds: int) -> str:
    """``30 days``, ``36 hours``: how a message names a span."""
    if seconds % DAY == 0:
        days = seconds // DAY
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hours"
    return f"{seconds} seconds"


# ── dbt: which materialization does the model get? ──────────────────────────


class DbtUnresolved(Exception):
    """The model's materialization cannot be worked out; the message says why."""


class _Unknown:
    """A config value that is not a plain literal (``var()``, a name, a ``**``)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<not a literal>"


UNKNOWN = _Unknown()


@dataclass(frozen=True)
class DbtModel:
    """What dbt would do for the model that writes a table."""

    name: str
    materialized: str
    #: The Delta ``tblproperties`` it sets, as written (strings).
    tblproperties: Mapping[str, Any] = field(default_factory=dict)
    #: dbt's fully qualified name (project, folders below the model path, name), and the
    #: file's path below the project, which dbt's selectors match.
    fqn: Tuple[str, ...] = ()
    path: str = ""


def _jinja(value: Any) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _yaml(path: Path) -> Any:
    import yaml

    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise DbtUnresolved(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except DbtUnresolved:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DbtUnresolved(f"{path.name} could not be read ({type(exc).__name__})") from None


def _literal(node: ast.AST) -> Any:
    """A Python or Jinja literal (``true`` and ``none`` too); ``UNKNOWN`` otherwise."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id.lower() in ("true", "false", "none"):
        return {"true": True, "false": False, "none": None}[node.id.lower()]
    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_literal(item) for item in node.elts]
        return UNKNOWN if any(item is UNKNOWN for item in items) else items
    if isinstance(node, ast.Dict) and all(key is not None for key in node.keys):
        out = {}
        for key, value in zip(node.keys, node.values, strict=True):
            k, v = _literal(key), _literal(value)  # type: ignore[arg-type]
            if k is UNKNOWN or not isinstance(k, str):
                return UNKNOWN
            out[k] = v
        return out
    return UNKNOWN


_RAW_OPEN_RE = re.compile(r"\{%-?\s*raw\s*-?%\}")
_RAW_CLOSE_RE = re.compile(r"\{%-?\s*endraw\s*-?%\}")


# The scans below match what the lazy regexes ``\{#.*?#\}``, ``{% raw %}.*?{% endraw %}`` and
# ``\{\{(.*?)\}\}|\{%(.*?)%\}`` would, in linear time: an opener with no closer after it means no
# later opener of that kind has one either, so the scan stops looking for that kind instead of
# re-reading the rest of the file from every opener (quadratic on a file of unclosed ``{{``).
def _without_comments_and_raw(text: str) -> str:
    out: List[str] = []
    pos = 0
    while (start := text.find("{#", pos)) >= 0 and (end := text.find("#}", start + 2)) >= 0:
        out.append(text[pos:start])
        pos = end + 2
    text = "".join(out) + text[pos:]
    out, pos = [], 0
    while (opened := _RAW_OPEN_RE.search(text, pos)) and (
        closed := _RAW_CLOSE_RE.search(text, opened.end())
    ):
        out.append(text[pos : opened.start()])
        pos = closed.end()
    return "".join(out) + text[pos:]


def _tags(text: str) -> Iterator[Tuple[Optional[str], Optional[str]]]:
    """``(expression, None)`` for each ``{{ ... }}`` and ``(None, statement)`` for each
    ``{% ... %}``, in order."""
    closer = {"{{": "}}", "{%": "%}"}
    nxt: Dict[str, int] = {}
    pos = 0
    while True:
        for opener in list(closer):
            if nxt.get(opener, -1) < pos:
                nxt[opener] = text.find(opener, pos)
                if nxt[opener] < 0:
                    del closer[opener]
        if not closer:
            return
        start, opener = min((nxt[o], o) for o in closer)
        end = text.find(closer[opener], start + 2)
        if end < 0:
            del closer[opener]  # no closer after this opener, so none after a later one
            continue
        inner = text[start + 2 : end]
        yield (inner, None) if opener == "{{" else (None, inner)
        pos = end + 2


_BLOCKS = frozenset({"if", "for", "macro", "call", "filter", "with", "block"})
_ENDS = frozenset({"endif", "endfor", "endmacro", "endcall", "endfilter", "endwith", "endblock"})


def sql_config(text: str) -> Dict[str, Any]:
    """The configs a model's SQL sets with ``{{ config(...) }}``, later calls over earlier.

    Only a call outside every ``{% if %}`` / ``{% for %}`` / ``{% macro %}`` block, whose
    keyword values are literals, is read. Raises :class:`DbtUnresolved` for a ``config()``
    inside a block, or one with a ``**`` expansion or positional argument. A keyword whose
    value is not a literal is :data:`UNKNOWN`.
    """
    text = _without_comments_and_raw(text)
    found: Dict[str, Any] = {}
    depth = 0
    for expression, statement in _tags(text):
        if statement is not None:
            words = statement.strip(" \t\r\n-+").split(None, 1)
            word = words[0] if words else ""
            if word in _BLOCKS or (word == "set" and "=" not in statement):
                depth += 1
            elif word in _ENDS or word == "endset":
                depth = max(0, depth - 1)
            continue
        inner = expression.strip(" \t\r\n-+")
        if not re.match(r"config\s*\(", inner):
            continue
        if depth:
            raise DbtUnresolved("its config() is inside a Jinja block, so it may not apply")
        try:
            call = ast.parse(inner, mode="eval").body
        except SyntaxError:
            raise DbtUnresolved("its config() call could not be parsed") from None
        if (
            not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Name)
            or call.func.id != "config"
            or call.args
            or any(keyword.arg is None for keyword in call.keywords)
        ):
            raise DbtUnresolved("its config() call is not plain keyword arguments")
        for keyword in call.keywords:
            found[str(keyword.arg)] = _literal(keyword.value)
    return found


def _tree_configs(node: Any) -> Dict[str, Any]:
    """The configs set directly on one level of ``dbt_project.yml``'s ``models:`` tree:
    a ``+key``, or a key whose value is not a mapping (a folder holds a mapping)."""
    out: Dict[str, Any] = {}
    if not isinstance(node, Mapping):
        return out
    for key, value in node.items():
        key = str(key)
        if key.startswith("+"):
            out[key[1:].replace("-", "_")] = UNKNOWN if _jinja(value) else value
        elif not isinstance(value, Mapping):
            out[key.replace("-", "_")] = UNKNOWN if _jinja(value) else value
    return out


def _project_configs(models: Any, project: str, fqn: Sequence[str]) -> Dict[str, Any]:
    """``dbt_project.yml``'s configs for the node at ``fqn`` (folders, then the model),
    the most specific last: root ``+`` configs, the project's, then each folder's."""
    merged = dict(_tree_configs(models))
    node = models.get(project) if isinstance(models, Mapping) else None
    for level in [None, *fqn]:
        if level is not None:
            node = node.get(level) if isinstance(node, Mapping) else None
        merged.update(_tree_configs(node))
        if not isinstance(node, Mapping):
            break
    return merged


def _model_paths(config: Mapping[str, Any]) -> List[PurePosixPath]:
    raw = config.get("model-paths", config.get("source-paths", ["models"]))
    out = []
    for entry in [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []:
        rel = PurePosixPath(str(entry))
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise DbtUnresolved(f"model-paths names {str(entry)!r}, outside the project")
        out.append(rel)
    return out


def _walk_models(
    project: Path, roots: Sequence[PurePosixPath]
) -> Iterator[Tuple[Path, Tuple[str, ...]]]:
    """``(file, folders below its model path)`` for every file under the model paths,
    never through a link."""
    count = 0
    for rel in roots:
        base = project / rel
        if base.is_symlink() or not base.is_dir():
            continue
        if not base.resolve().is_relative_to(project.resolve()):
            continue  # a folder on the way is a link out of the project
        for here, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if not d.startswith(".") and not (Path(here) / d).is_symlink()
            )
            for name in sorted(names):
                path = Path(here) / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                count += 1
                if count > MAX_FILES:
                    raise DbtUnresolved(f"the model paths hold more than {MAX_FILES} files")
                yield path, Path(here).relative_to(base).parts


def resolve_model(project: Path, relation: str) -> DbtModel:
    """The dbt model of ``project`` that creates the relation ``relation``, with the
    materialization and Delta properties dbt resolves for it.

    Raises :class:`DbtUnresolved` when there is no such model, more than one, or a value
    that decides the outcome is not a plain literal.
    """
    config = _yaml(project / "dbt_project.yml")
    if not isinstance(config, Mapping) or not isinstance(config.get("name"), str):
        raise DbtUnresolved("dbt_project.yml has no project name")
    name = config["name"]
    models = config.get("models") or {}
    sql: Dict[str, Tuple[Path, Tuple[str, ...]]] = {}
    properties: Dict[str, Mapping[str, Any]] = {}
    for path, folders in _walk_models(project, _model_paths(config)):
        if path.suffix == ".sql":
            if path.stem in sql:
                raise DbtUnresolved(f"two models are named {path.stem!r}")
            sql[path.stem] = (path, folders)
        elif path.suffix == ".py":
            sql.setdefault(path.stem, (path, folders))
        elif path.suffix in (".yml", ".yaml"):
            document = _yaml(path)
            for entry in (document.get("models") if isinstance(document, Mapping) else None) or []:
                if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
                    if entry.get("versions"):
                        raise DbtUnresolved(f"model {entry['name']!r} is versioned")
                    block = entry.get("config")
                    properties[entry["name"]] = block if isinstance(block, Mapping) else {}

    wanted = relation.lower()
    candidates: Dict[str, Dict[str, Any]] = {}
    for model, (path, folders) in sql.items():
        merged = _project_configs(models, name, (*folders, model))
        merged.update(
            {
                str(k).replace("-", "_"): (UNKNOWN if _jinja(v) else v)
                for k, v in properties.get(model, {}).items()
            }
        )
        text = ""
        if path.suffix == ".sql":
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise DbtUnresolved(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
                text = path.read_text(encoding="utf-8")
                merged.update(sql_config(text))
            except (OSError, UnicodeDecodeError):
                raise DbtUnresolved(f"{path.name} could not be read") from None
            except DbtUnresolved as exc:
                if "alias" in text:
                    raise DbtUnresolved(
                        f"model {model!r}: {exc}, so it cannot be ruled out as the writer of "
                        f"{relation!r}"
                    ) from None
                merged["_sql_error"] = str(exc)
        alias = merged.get("alias")
        if alias is UNKNOWN:
            raise DbtUnresolved(
                f"the alias of model {model!r} is not a plain value, so it cannot be ruled "
                f"out as the writer of {relation!r}"
            )
        if str(alias if alias else model).lower() == wanted:
            candidates[model] = merged
    if not candidates:
        raise DbtUnresolved(f"no model in the project creates a relation named {relation!r}")
    if len(candidates) > 1:
        raise DbtUnresolved(f"models {', '.join(sorted(candidates))} all create {relation!r}")
    model, merged = next(iter(candidates.items()))
    if sql[model][0].suffix != ".sql":
        raise DbtUnresolved(f"model {model!r} is a Python model")
    if merged.get("_sql_error"):
        raise DbtUnresolved(f"model {model!r}: {merged['_sql_error']}")
    if merged.get("enabled") is UNKNOWN or merged.get("enabled") in (False, "false", "False"):
        raise DbtUnresolved(f"model {model!r} is disabled or its enabled is not a plain value")
    for key in ("schema", "database", "catalog"):
        if merged.get(key) not in (None, ""):
            raise DbtUnresolved(
                f"model {model!r} sets {key}, so its relation may not be in the expose's "
                f"{'schema' if key == 'schema' else 'catalog'}"
            )
    materialized = merged.get("materialized", "view")
    if not isinstance(materialized, str):
        raise DbtUnresolved(f"model {model!r}: materialized is not a plain value")
    props = merged.get("tblproperties") or {}
    if not isinstance(props, Mapping):
        raise DbtUnresolved(f"model {model!r}: tblproperties is not a plain mapping")
    path, folders = sql[model]
    return DbtModel(
        model,
        materialized,
        {str(k): v for k, v in props.items()},
        fqn=(name, *folders, model),
        path=path.relative_to(project).as_posix(),
    )


# ── dbt: does the build select the model? ───────────────────────────────────

#: Selection methods this module does not evaluate (a tag, a config, the state of a
#: previous run): a selector using one cannot be said to include the model.
_METHOD_RE = re.compile(r"^([A-Za-z_.]+):")
_GRAPH_RE = re.compile(r"^(@)?(\d*\+)?(.*?)(\+\d*)?$")


def _part_selects(part: str, model: DbtModel) -> Optional[bool]:
    """Whether one selector (no space, no comma) includes ``model``: ``True``, ``False``, or
    ``None`` when it cannot be told without dbt's graph or run state (a graph operator on
    another node, a tag or config method).

    Only what is decidable from the project's files is: a node's own name or fully qualified
    name (prefixes and ``*`` included), and ``path:``. A graph operator (``+``, ``N+``, ``@``)
    includes the named node, and nodes around it that only dbt's graph knows.
    """
    if part == "*":
        return True
    method = _METHOD_RE.match(part)
    value = part
    if method:
        name = method.group(1).lower()
        value = part[method.end() :]
        if name not in ("fqn", "path", "model"):
            return None
    else:
        name = "path" if "/" in part or part.endswith(".sql") else "fqn"
    graph = _GRAPH_RE.match(value)
    operators = bool(graph and (graph.group(1) or graph.group(2) or graph.group(4)))
    value = graph.group(3) if graph else value
    if name == "path":
        wanted = PurePosixPath(value.rstrip("/"))
        have = PurePosixPath(model.path)
        hit = len(wanted.parts) <= len(have.parts) and all(
            fnmatch.fnmatchcase(h, w) for h, w in zip(have.parts, wanted.parts, strict=False)
        )
    else:
        wanted_parts = value.split(".")
        if len(wanted_parts) == 1:
            hit = fnmatch.fnmatchcase(model.name, value)
        else:
            hit = len(wanted_parts) <= len(model.fqn) and all(
                fnmatch.fnmatchcase(h, w) for h, w in zip(model.fqn, wanted_parts, strict=False)
            )
    if hit:
        return True
    return None if operators else False


def selects_model(selectors: Sequence[str], model: DbtModel) -> Optional[bool]:
    """Whether ``dbt build --select <selectors>`` builds ``model``: ``True``, ``False``, or
    ``None`` when it cannot be told. No selector builds every model. Space-separated
    selectors are a union, a comma joins an intersection (dbt's selection syntax,
    https://docs.getdbt.com/reference/node-selection/syntax)."""
    tokens = [t for item in selectors for t in str(item).split()]
    if not tokens:
        return True
    unknown = False
    for token in tokens:
        results = [_part_selects(part, model) for part in token.split(",") if part]
        if not results or any(r is False for r in results):
            continue
        if all(r is True for r in results):
            return True
        unknown = True
    return None if unknown else False


# ── the decision ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Retention:
    """An accepted ``lifecycle.expire``: what ``fluid verify`` holds the live table to."""

    #: ``lifecycle.retention`` as written, and in seconds (years and months at their shortest).
    period: str
    seconds: int


def _is_dbt(build: Mapping[str, Any]) -> bool:
    engine = str(build.get("engine") or "").strip().lower()
    return engine == "dbt" or engine.startswith("dbt-")


def _fix_materialized() -> str:
    return "Declare materialized='table' on the model ({{ config(materialized='table') }})."


def _dbt_model(
    build: Mapping[str, Any], table: str, where: str, validating: bool
) -> Tuple[Optional[DbtModel], Optional[UnsupportedBindingError]]:
    """The dbt model of ``build`` for ``table``; ``(None, None)`` when ``validating`` with
    no contract file to find the project from, as the job emitter does."""
    label = f"{where}: build {build.get('id')!r}"
    repository = str(build.get("repository") or "").strip()
    if "://" in repository or repository.startswith("git@"):
        return None, _refusal(
            "dbt-unresolved",
            f"{label} runs a dbt project from a Git repository ({repository.split('?')[0]}), "
            "which forge-cli cannot read here, so it cannot tell whether the model for "
            f"{table!r} is rebuilt in full.",
            "Point repository at a local dbt project directory (forge-cli reads it, and "
            "uploads it to the workspace), and " + _fix_materialized(),
        )
    source = current_contract_source()
    if source is None:
        if validating:
            return None, None
        return None, _refusal(
            "dbt-unresolved",
            f"{label}: the contract's location is not known here, so its dbt project cannot "
            "be read to see how the model is materialized.",
            "Run it through fluid apply / fluid generate iac with the contract's path.",
        )
    from ...build_runners.dbt.runner import resolve_dbt_project_path

    project = resolve_dbt_project_path(Path(source), dict(build))
    if project is None:
        return None, _refusal(
            "dbt-unresolved",
            f"{label}: repository {repository!r} holds no dbt_project.yml, so the model's "
            "materialization cannot be read.",
            "Point repository at the dbt project directory, relative to the contract.",
        )
    try:
        return resolve_model(project, table), None
    except DbtUnresolved as exc:
        return None, _refusal(
            "dbt-unresolved",
            f"{label}: cannot tell whether the model for {table!r} is rebuilt in full: {exc}.",
            _fix_materialized(),
        )


def _selection_refusal(
    build: Mapping[str, Any], model: DbtModel, table: str, label: str
) -> Optional[UnsupportedBindingError]:
    """A refusal unless the build's dbt selection includes the resolved model: a model the
    build does not run is not rebuilt, and its table keeps whatever it last held."""
    from ...build_runners.dbt.runner import build_selectors

    selectors = build_selectors(dict(build))
    chosen = selects_model(selectors, model)
    if chosen:
        return None
    shown = " ".join(selectors)
    how = (
        "does not select it"
        if chosen is False
        else "selects it only through graph operators, tags, configs or state that forge-cli "
        "cannot evaluate here"
    )
    return _refusal(
        "not-selected",
        f"{label}: the table {table!r} is written by model {model.name!r}, but the build's "
        f"selection ({shown!r}, from properties.model or properties.select) {how}, so a run "
        "need not rebuild the table and rows older than the retention could stay.",
        f"Select the model by name: properties.select: [{model.name!r}] (or "
        f"properties.model: {model.name}).",
    )


def _acquisition_mode_refusal(
    build: Mapping[str, Any], label: str
) -> Optional[UnsupportedBindingError]:
    """A refusal unless the DuckDB acquisition's ``properties.source.mode`` is one the
    Databricks landing runs as a full overwrite of the table."""
    from ...build_runners._databricks_load import LOAD_MODES

    properties = build.get("properties")
    source = properties.get("source") if isinstance(properties, Mapping) else None
    mode = source.get("mode") if isinstance(source, Mapping) else None
    mode = getattr(mode, "value", mode)
    if mode in LOAD_MODES:
        return None
    fix = (
        f"Set properties.source.mode to {LOAD_MODES[0]!r} (the landing overwrites the whole "
        "table), or remove lifecycle.expire."
    )
    if not mode:
        return _refusal(
            "unmapped",
            f"{label} sets no properties.source.mode, so nothing says it rewrites the whole "
            "table, and rows older than the retention could stay.",
            fix,
        )
    return _refusal(
        "incremental",
        f"{label}: the acquisition's source mode is {mode!r}, and only "
        f"{', '.join(repr(m) for m in LOAD_MODES)} rewrites the whole table on Databricks, so "
        "rows older than the retention could stay.",
        fix,
    )


def check_expire(
    where: str,
    exposure: Mapping[str, Any],
    table: str,
    contract: Mapping[str, Any],
    *,
    validating: bool = False,
) -> Tuple[Optional[Retention], List[UnsupportedBindingError]]:
    """The retention ``exposure`` promises and every reason this module refuses it.

    ``(None, [])`` when it sets no ``lifecycle.expire``. ``table`` is the expose's table
    name, the relation dbt must create.
    """
    lifecycle = exposure.get("lifecycle")
    if not isinstance(lifecycle, Mapping) or not lifecycle.get("expire"):
        return None, []
    refusals: List[UnsupportedBindingError] = []
    period = lifecycle.get("retention")
    seconds: Optional[int] = None
    if not period:
        refusals.append(
            _refusal(
                "period",
                f"{where} sets lifecycle.expire but no lifecycle.retention, so there is no "
                "period to keep the data for.",
                "Set lifecycle.retention (an ISO 8601 duration such as P365D), or drop expire.",
            )
        )
    else:
        try:
            seconds = iso_duration_seconds(period)
        except ValueError:
            refusals.append(
                _refusal(
                    "period",
                    f"{where} lifecycle.retention is {period!r}, which is not an ISO 8601 "
                    "duration.",
                    "Write it as an ISO 8601 duration, such as P30D, P12W or P6Y.",
                )
            )
    tblproperties: Mapping[str, Any] = {}
    builds = [b for b in contract.get("builds") or [] if isinstance(b, Mapping)]
    writers = builds_writing_expose(exposure.get("exposeId"), builds)
    if not writers:
        refusals.append(
            _refusal(
                "unmapped",
                f"{where} sets lifecycle.expire, and no build writes it, so nothing says the "
                "table is rebuilt in full: rows it holds could be any age, and nothing on "
                "Databricks would expire them.",
                "Name the build that rewrites the whole table in builds[].outputs, or remove "
                "lifecycle.expire.",
            )
        )
    for build in writers:
        label = f"{where}: build {build.get('id')!r}"
        if is_duckdb_acquisition_build(build):
            # forge-cli's acquisition lands into Databricks as an INSERT OVERWRITE of the
            # whole table, and only in the modes the landing accepts (it refuses any other
            # before the build runs); acceptance rests on both. If the landing ever appends,
            # this must refuse.
            refusal = _acquisition_mode_refusal(build, label)
            if refusal is not None:
                refusals.append(refusal)
            continue
        if not _is_dbt(build):
            refusals.append(
                _refusal(
                    "unmapped",
                    f"{label} (engine {build.get('engine')!r}) writes it, and forge-cli cannot "
                    "tell that it rewrites the whole table, so rows older than the retention "
                    "could stay.",
                    "Use a dbt build with materialized='table', or a DuckDB acquisition, or "
                    "remove lifecycle.expire.",
                )
            )
            continue
        model, refusal = _dbt_model(build, table, where, validating)
        if refusal is not None:
            refusals.append(refusal)
        elif model is not None and (selection := _selection_refusal(build, model, table, label)):
            refusals.append(selection)
        elif model is not None:
            kind = model.materialized
            if kind in ("incremental", "snapshot"):
                refusals.append(
                    _refusal(
                        "incremental",
                        f"{label}: model {model.name!r} is materialized as {kind!r}, which "
                        "keeps rows from earlier runs, so no run leaves the table without a "
                        "row older than the retention. Delta history limits cannot expire "
                        "rows.",
                        "Rebuild it in full (materialized='table'), or drop lifecycle.expire "
                        "and delete old rows in the model.",
                    )
                )
            elif kind != "table":
                refusals.append(
                    _refusal(
                        "not-table",
                        f"{label}: model {model.name!r} is materialized as {kind!r} (dbt's "
                        "default is 'view'), not 'table', so a run does not replace the "
                        "stored table.",
                        _fix_materialized(),
                    )
                )
            else:
                tblproperties = {**tblproperties, **model.tblproperties}
    effective = DEFAULT_LOG_SECONDS
    try:
        effective = history_seconds(tblproperties)
    except ValueError as exc:
        refusals.append(
            _refusal(
                "dbt-unresolved",
                f"{where}: the model's Delta retention properties cannot be read ({exc}).",
                f"Write {LOG_RETENTION} and {DELETED_FILE_RETENTION} as 'interval N days'.",
            )
        )
    if seconds is not None and seconds < effective:

        def _shown(key: str, default: int) -> str:
            value = tblproperties.get(key)
            return f"{value!s} (declared)" if value else f"{days_text(default)} (Delta's default)"

        refusals.append(
            _refusal(
                "too-short",
                f"{where} lifecycle.retention is {period} ({days_text(seconds)}), shorter than "
                f"the table's Delta history of {days_text(effective)} ({LOG_RETENTION} "
                f"{_shown(LOG_RETENTION, DEFAULT_LOG_SECONDS)}, {DELETED_FILE_RETENTION} "
                f"{_shown(DELETED_FILE_RETENTION, DEFAULT_DELETED_FILE_SECONDS)}). VACUUM keeps "
                "old data files that long, so a copy older than the retention would remain. "
                "forge-cli does not shorten the history yet.",
                f"Set lifecycle.retention to at least P{effective // DAY}D, or remove "
                "lifecycle.expire.",
            )
        )
    if refusals:
        return None, refusals
    return Retention(period=str(period), seconds=int(seconds or 0)), []


__all__ = [
    "DEFAULT_DELETED_FILE_SECONDS",
    "DEFAULT_LOG_SECONDS",
    "DELETED_FILE_RETENTION",
    "LOG_RETENTION",
    "DbtModel",
    "DbtUnresolved",
    "Retention",
    "check_expire",
    "days_text",
    "history_seconds",
    "interval_seconds",
    "iso_duration_seconds",
    "selects_model",
    "resolve_model",
    "sql_config",
]
