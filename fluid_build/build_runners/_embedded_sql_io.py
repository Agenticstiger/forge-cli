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

"""What an embedded-SQL build on the DuckDB engine reads, and where it lands.

**Reads.** A ``consumes[]`` entry is a LOGICAL address, ``{productId,
exposeId}``. Until this module no engine turned it into a physical one: the
local provider logged ``local_consumes_not_bound`` and ran the SQL anyway, so a
downstream product had to hard-code where its upstream happened to land on
each target, and an overlay had to patch its SQL as well as its binding. Now
each entry is resolved the way the upstream itself would be deployed:

1. **Find the upstream contract.** The workspace root is the nearest ancestor
   of the building contract that holds ``fluid.workspace.yaml``
   (``util.workspace_root.find_workspace_root``), plus any root in
   ``FLUID_UPSTREAM_CONTRACTS``; contracts are found by the walk the dbt
   sources generator uses for the same question
   (``util.upstream_discovery``), skipping VCS, venv and output directories.
   The match is on the ``id`` the file declares. An id declared twice is an
   error naming both files: picking one would build from whichever the walk
   met last.
2. **Load it with this run's environment.** The same ``--env`` overlay the
   building contract was loaded with, through the same loader, so an aws run
   reads the upstream's aws binding and a local run its local one. Overlays
   keep patching only ``exposes[].binding``; the SQL never changes.
3. **Pick the expose** by ``exposeId`` and compute its READ uri: a local path
   anchored at the UPSTREAM contract's directory
   (``util.binding_paths.resolve_binding_path``), with the file name and format
   the local provider writes it under when an embedded-SQL build of the
   upstream lands it (``util.binding_paths.local_provider_landing``, the
   writer's own rule); an AWS object-store binding
   through the duckdb acquisition runner's own key rule
   (``_object_store_uri``), read as the glob of that prefix's files of the
   binding format, which is also what the Glue table ``fluid apply`` declares
   for the binding serves; a GCP ``bigquery_table`` binding as that table,
   named by the IaC's own rule (``_bigquery_load.bigquery_load_target``, the
   table ``fluid apply`` created and the upstream's build loads), read
   through the BigQuery API into a staged Parquet file when the build runs
   (``_bigquery_read``). Anything else this engine cannot read (another
   warehouse's table, a stream, a GCS or Azure prefix) is an
   :class:`UnreadableBindingError` naming the platform.

Each entry the SQL reads becomes one DuckDB view named by its ``exposeId``, a
quoted identifier that must pass ``validate_ident``. What the SQL reads is
decided by DuckDB's own parser (:func:`relations_read`): an entry whose
``exposeId`` names no relation in the query is LINEAGE ONLY, as every entry was
before this module, and is not resolved (a contract that binds its inputs under
other names, or reads its upstream by path in the SQL, keeps building). When
the parser cannot tell (a statement it does not serialize, a syntax error),
every entry is treated as read. An explicit
``builds[].properties.parameters.inputs`` entry of the same name WINS and the
entry is not resolved at all: that is how a federated upstream, or one this
engine cannot read, is still bound. An entry the SQL reads that is neither
resolved nor covered fails the build before any SQL runs, and so does one
naming the building contract's own id, which would read the file this build is
about to overwrite.

**Lands.** When the first expose's binding is an AWS object-store binding, the
result is written to exactly the object the acquisition runner writes for that
binding (``_object_store_uri`` + ``_file_within_prefix``), inside the prefix
the Glue table points at, so ``fluid verify --env aws`` counts it. When it is
a GCP ``bigquery_table`` binding, the result is staged as Parquet under the
build's ``.fluid/staging`` and one load job moves it into that table
(``_bigquery_load.load_file``, the acquisition runner's own load), whatever
``location.path`` the binding also carries: a ``gs://`` path there is not a
file this path writes. A local binding is unchanged. Any other landing (a
``gs://`` or other non-S3 URI, a GCS bucket, another warehouse) is refused
with :class:`EmbeddedSqlLandingError` rather than written to a local file of
that name. An expose declaring ``policy.privacy.masking`` is refused: this
path does not apply masking, and cleartext must not land silently.

Borrowed, not built:

* dbt's ``ref()`` (``dbt/context/providers.py::RuntimeRefResolver``): a
  logical reference resolves through the manifest to the target node's own
  relation for the CURRENT target, and a missing target raises
  ``TargetNotFoundError`` naming it rather than compiling a guess. Two-argument
  ``ref`` is dbt's answer to a name declared twice; a contract id has no
  package to qualify it with, so a duplicate is refused.
* dbt-duckdb's ``external`` materialization: the result is written to a file
  and downstream reads it through a view over ``read_<format>('<location>')``,
  with ``external_read_location`` turning a multi-file prefix into
  ``<prefix>/*.<format>``.
* SQLMesh resolves a model reference to the relation of the SAME environment
  the plan runs in; the overlay env plays that role here.
* ODPS input ports carry only a ``contractId``; the physical location lives
  with the provider (ODCS ``servers``, keyed by ``environment``). A consume is
  that input port, and the upstream's binding is its server.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from fluid_build._errors import FluidUserError, doc_url
from fluid_build.util.binding_paths import (
    ENV_PLACEHOLDER_RE,
    is_remote_uri,
    local_provider_landing,
    resolve_binding_path,
)

LOG = logging.getLogger("fluid.build_runners.embedded_sql")

#: Binding formats this engine reads from an upstream and lands its own in:
#: the file formats the duckdb acquisition runner writes.
FILE_FORMATS = ("parquet", "csv", "json")
_FORMAT_ALIASES = {"pq": "parquet", "ndjson": "json", "jsonl": "json"}
_SUFFIX_FORMATS = {
    ".parquet": "parquet",
    ".pq": "parquet",
    ".csv": "csv",
    ".tsv": "csv",
    ".json": "json",
    ".ndjson": "json",
    ".jsonl": "json",
}
#: ``binding.platform`` values that name a file on this machine.
_LOCAL_PLATFORMS = frozenset({"", "local"})


# ── Typed errors ─────────────────────────────────────────────────────────


@dataclass
class ConsumesResolutionError(FluidUserError):
    """A ``consumes[]`` entry the build cannot bind to a relation."""

    code: str = "ConsumesResolutionError"


@dataclass
class UnreadableBindingError(ConsumesResolutionError):
    """The upstream expose resolved, but to a binding this engine cannot read."""

    code: str = "UnreadableBindingError"


@dataclass
class EmbeddedSqlLandingError(FluidUserError):
    """The build's own expose names a landing this path will not perform."""

    code: str = "EmbeddedSqlLandingError"


@dataclass
class MaskingNotAppliedError(EmbeddedSqlLandingError):
    """The expose declares masking, which this landing path does not apply."""

    code: str = "MaskingNotAppliedError"


@dataclass
class EmbeddedSqlSovereigntyError(EmbeddedSqlLandingError):
    """A BigQuery read or landing the contract's ``sovereignty`` block does not allow."""

    code: str = "EmbeddedSqlSovereigntyError"


_EXPLICIT_INPUT_FIX = (
    "Or bind it by hand: a builds[].properties.parameters.inputs entry named "
    "'{name}' (with the path to read) wins over the consumes entry and is used as is."
)


def _entry(product_id: str, expose_id: str) -> str:
    return f"consumes {product_id}/{expose_id}"


# ── What was resolved ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedInput:
    """One ``consumes[]`` entry, bound to the relation it reads."""

    product_id: str
    expose_id: str
    uri: str
    format: str
    platform: str
    contract_path: Path
    region: Optional[str] = None
    #: A BigQuery upstream: its ``project.dataset.table`` (the project empty
    #: when the client's own is used), read into ``read_path`` before the SQL.
    table: Optional[str] = None
    #: The local Parquet file a BigQuery upstream was staged into.
    read_path: Optional[str] = None
    #: Whether the upstream's binding names its region. A BigQuery table whose
    #: binding names none is in the IaC's default location (``US``), which a
    #: ``sovereignty`` block must not be satisfied by (:func:`refuse_sovereignty_breach`).
    region_declared: bool = True

    @property
    def view(self) -> str:
        """The DuckDB view the SQL reads it through."""
        return self.expose_id

    def as_input_spec(self) -> Dict[str, Any]:
        """The local provider's input spec (``_register_inputs``) for this entry."""
        if self.table is not None and self.read_path is None:
            raise RuntimeError(
                f"{_entry(self.product_id, self.expose_id)}: BigQuery table {self.table} "
                "was not staged before the SQL ran"
            )
        spec: Dict[str, Any] = {
            "table": self.view,
            "path": self.read_path or self.uri,
            "format": "parquet" if self.read_path else self.format,
            "quoted": True,
            "productId": self.product_id,
            "exposeId": self.expose_id,
        }
        if self.region and self.table is None:
            spec["region"] = self.region
        return spec

    def record(self) -> Dict[str, str]:
        rec = {"productId": self.product_id, "exposeId": self.expose_id, "uri": self.uri}
        if self.read_path:
            rec["staged"] = self.read_path
        return rec


@dataclass(frozen=True)
class CoveredInput:
    """A ``consumes[]`` entry bound by an explicit input of the same name."""

    product_id: str
    expose_id: str
    input_name: str


@dataclass(frozen=True)
class LineageOnlyInput:
    """A ``consumes[]`` entry the SQL does not read: recorded, not resolved."""

    product_id: str
    expose_id: str

    def as_consume(self) -> Dict[str, str]:
        return {"productId": self.product_id, "exposeId": self.expose_id}


@dataclass(frozen=True)
class Landing:
    """The object-store object an embedded-SQL build writes its result to."""

    uri: str
    format: str
    region: Optional[str] = None

    def as_output_spec(self) -> Dict[str, Any]:
        spec: Dict[str, Any] = {"path": self.uri, "format": self.format}
        if self.region:
            spec["region"] = self.region
        return spec


@dataclass(frozen=True)
class BigQueryLanding:
    """The BigQuery table an embedded-SQL build loads its result into.

    ``project`` is ``None`` when neither the binding nor the environment names
    one, and the client's own is used, as the acquisition runner's load does.
    ``staged`` is the local Parquet file the SQL writes and the load reads,
    set when the build runs.
    """

    project: Optional[str]
    dataset: str
    table: str
    location: str
    staged: Optional[str] = None
    #: Whether the binding names the region; ``location`` is otherwise the
    #: IaC's default (``US``), as :attr:`ResolvedInput.region_declared`.
    region_declared: bool = True
    #: The expose the result lands in, for the run record.
    expose_id: str = "result"

    @property
    def table_id(self) -> str:
        return f"{self.project or '<default project>'}.{self.dataset}.{self.table}"

    def load_target(self) -> Dict[str, Any]:
        """The target ``_bigquery_load.load_file`` takes."""
        return {
            "project": self.project,
            "dataset": self.dataset,
            "table": self.table,
            "location": self.location,
        }


@dataclass
class EmbeddedSqlIO:
    """Everything :func:`plan_embedded_sql_io` decided, before any SQL runs."""

    inputs: List[ResolvedInput] = field(default_factory=list)
    covered: List[CoveredInput] = field(default_factory=list)
    lineage_only: List[LineageOnlyInput] = field(default_factory=list)
    landing: Optional[Landing] = None
    workspace_root: Optional[Path] = None
    bigquery_landing: Optional[BigQueryLanding] = None
    #: What the plan let through but the operator must hear about: a further
    #: output this path does not write, an advisory sovereignty finding.
    warnings: List[str] = field(default_factory=list)

    @property
    def bigquery_inputs(self) -> List[ResolvedInput]:
        return [r for r in self.inputs if r.table is not None]


# ── Helpers ─────────────────────────────────────────────────────────────


def _normalize_format(raw: Any) -> str:
    fmt = str(raw or "").strip().lower()
    return _FORMAT_ALIASES.get(fmt, fmt)


def _resolve_env(value: Any, *, field_name: str, where: str) -> Optional[str]:
    """``{{ env.NAME }}`` resolved, refusing a variable that is unset or empty.

    The runtime resolver (``base._resolve_env_placeholders``, and
    ``util.binding_paths.resolve_env_placeholders_in_path`` after it) turns an
    unset variable into ``""``, which for a path like
    ``{{ env.FLUID_DATA_DIR }}/orders.parquet`` would read ``/orders.parquet``
    at the filesystem root. A location is not a place to guess.

    A credential-shaped variable (``is_sensitive_key_name``: password, secret,
    token, key ...) is refused too. The resolved location is printed in the
    build output and kept in the apply log, and an upstream contract is a file
    this build discovered, not one its author named, so it must not be able to
    have a secret echoed by spelling it into a path.
    """
    if value is None or value == "":
        return None
    from fluid_build.observability.secret_redactor import is_sensitive_key_name

    text = str(value)
    sensitive = sorted(
        {m.group(1) for m in ENV_PLACEHOLDER_RE.finditer(text) if is_sensitive_key_name(m.group(1))}
    )
    if sensitive:
        names = ", ".join(sensitive)
        raise ConsumesResolutionError(
            what=f"{where}: {field_name} names {names}, which looks like a credential",
            why=(
                "A location is printed in the build output and recorded in the apply log, "
                "so a credential-shaped variable in it is not resolved."
            ),
            fix="Name the location with a variable outside the password/secret/token/key family.",
            doc=doc_url(),
            extras={"variables": sensitive},
        )
    missing = sorted(
        {m.group(1) for m in ENV_PLACEHOLDER_RE.finditer(text) if not os.environ.get(m.group(1))}
    )
    if missing:
        names = ", ".join(missing)
        raise ConsumesResolutionError(
            what=f"{where}: {field_name} needs {names}, which is not set",
            why=(
                f"{field_name} is {text!r}; resolving it with {names} unset would name a "
                "different location than the one the upstream writes."
            ),
            fix=f"Set {names} in the environment this build runs in, as the upstream's own build does.",
            doc=doc_url(),
            extras={"variables": missing},
        )
    return ENV_PLACEHOLDER_RE.sub(lambda m: os.environ[m.group(1)], text)


def explicit_input_names(build: Mapping[str, Any]) -> Dict[str, str]:
    """``{casefolded name: name}`` of the build's explicit ``parameters.inputs``.

    Named as the local provider names them (``_derive_actions_from_contract``):
    ``name``, else the file stem of ``path``. Casefolded because DuckDB
    identifiers are case-insensitive, so ``Orders`` and ``orders`` are one view.
    """
    from fluid_build.providers.local.local import _guess_table_name_from_path

    props = build.get("properties") or {}
    params = props.get("parameters") if isinstance(props, Mapping) else None
    inputs = params.get("inputs") if isinstance(params, Mapping) else None
    names: Dict[str, str] = {}
    for item in inputs or []:
        if not isinstance(item, Mapping) or not item.get("path"):
            continue
        name = str(item.get("name") or _guess_table_name_from_path(Path(str(item["path"]))))
        names[name.casefold()] = name
    return names


def _base_tables(node: Any, ctes: FrozenSet[str], found: Set[str]) -> None:
    """Collect the ``BASE_TABLE`` names under ``node`` that no CTE in scope shadows.

    ``node`` is DuckDB's serialized parse tree (``json_serialize_sql``). A
    query node's ``cte_map`` puts its CTE names in scope for everything below
    it; an unqualified reference to one of them is the CTE, not a relation this
    build has to provide. The exception is a CTE's own body: DuckDB binds
    ``WITH s AS (SELECT * FROM s)`` to the relation ``s``, so a CTE's own name
    is out of scope there unless the body is recursive (a
    ``RECURSIVE_CTE_NODE``, which names itself in ``cte_name``).
    """
    if isinstance(node, list):
        for item in node:
            _base_tables(item, ctes, found)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "RECURSIVE_CTE_NODE":
        own = str(node.get("cte_name") or "").casefold()
        if own:
            ctes = ctes | frozenset({own})
    cte_map = node.get("cte_map")
    if isinstance(cte_map, dict):
        entries = [e for e in cte_map.get("map") or [] if isinstance(e, dict)]
        names = frozenset(str(e.get("key") or "").casefold() for e in entries)
        for entry in entries:
            own = str(entry.get("key") or "").casefold()
            _base_tables(entry.get("value"), ctes | (names - {own}), found)
        ctes = ctes | names
    if node.get("type") == "BASE_TABLE":
        name = str(node.get("table_name") or "").casefold()
        qualified = bool(node.get("schema_name") or node.get("catalog_name"))
        if name and (qualified or name not in ctes):
            found.add(name)
    for key, value in node.items():
        if key != "cte_map":
            _base_tables(value, ctes, found)


def relations_read(sql: str) -> Optional[FrozenSet[str]]:
    """The casefolded names of the relations ``sql`` reads, or ``None`` if unknown.

    Parsed, never bound, by DuckDB's own parser (``json_serialize_sql``, the
    grammar the query will run under), on a connection with external access
    off. ``duckdb.get_table_names`` is not usable here: on DuckDB 1.5 it BINDS
    the query, so it reads the files a ``read_parquet(...)`` names (and fetches
    a URL one names), raises for a ``JOIN ... USING`` against a view that is
    not registered yet, and raises for a glob that matches nothing, which is
    exactly the SQL of a contract that reads its upstream by path.

    ``None`` when the parser cannot say: a statement it does not serialize
    (anything but SELECT, e.g. ``PIVOT``), a syntax error, or no SQL. The
    caller then treats every consumes entry as read, as it did before.
    """
    if not str(sql or "").strip():
        return None
    try:
        import duckdb

        con = duckdb.connect(
            ":memory:",
            config={
                "enable_external_access": False,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
            },
        )
        try:
            row = con.execute("SELECT json_serialize_sql(?)", [str(sql)]).fetchone()
        finally:
            con.close()
        doc = json.loads(row[0]) if row and row[0] else {}
    except Exception as exc:  # noqa: BLE001 - "cannot tell" is the answer
        LOG.debug("embedded_sql_relations_unparsed: %s", type(exc).__name__)
        return None
    if not isinstance(doc, dict) or doc.get("error") or "statements" not in doc:
        return None
    found: Set[str] = set()
    _base_tables(doc["statements"], frozenset(), found)
    return frozenset(found)


def _search_roots(contract_dir: Path) -> Tuple[Optional[Path], List[Path]]:
    """The workspace root above ``contract_dir`` and every root to walk."""
    from fluid_build.util.upstream_discovery import collect_search_roots
    from fluid_build.util.workspace_root import find_workspace_root

    workspace_root = find_workspace_root(Path(contract_dir))
    return workspace_root, collect_search_roots(workspace_root)


def _looked_in(workspace_root: Optional[Path], roots: Sequence[Path], contract_dir: Path) -> str:
    from fluid_build.util.upstream_discovery import CONTRACT_FILENAMES

    if not roots:
        return (
            f"nowhere: no fluid.workspace.yaml in {contract_dir} or any directory above it, "
            "and FLUID_UPSTREAM_CONTRACTS is not set"
        )
    parts = []
    for root in roots:
        label = (
            " (the workspace root)" if workspace_root and root == workspace_root.resolve() else ""
        )
        parts.append(f"{root}{label}")
    return (
        f"{', '.join(parts)}, four directories deep, in files named "
        f"{' or '.join(CONTRACT_FILENAMES)}"
    )


def _find_expose(contract: Mapping[str, Any], expose_id: str) -> Optional[Mapping[str, Any]]:
    for expose in contract.get("exposes") or []:
        if (
            isinstance(expose, Mapping)
            and (expose.get("exposeId") or expose.get("id")) == expose_id
        ):
            return expose
    return None


def _format_from_suffix(path: str) -> str:
    return _SUFFIX_FORMATS.get(Path(path.rstrip("/")).suffix.lower(), "")


def _unreadable(where: str, platform: str, fmt: str, detail: str) -> UnreadableBindingError:
    shown = platform or "unset"
    return UnreadableBindingError(
        what=f"{where}: the DuckDB engine cannot read a {shown} binding ({detail})",
        why=(
            f"The upstream expose's binding has platform {shown!r} and format {fmt or 'unset'!r}. "
            "This build reads a local file or an S3 prefix of parquet, csv or json files, and "
            "nothing else: a warehouse table, a stream, or a GCS or Azure prefix is not one."
        ),
        fix=_EXPLICIT_INPUT_FIX.format(name=where.rsplit("/", 1)[-1]),
        doc=doc_url(),
        extras={"platform": shown, "format": fmt},
    )


def _require_bigquery_reader(where: str) -> None:
    """Refuse a BigQuery upstream before any SQL runs when it cannot be read here."""
    from ._bigquery_read import missing_dependency

    problem = missing_dependency()
    if problem:
        raise UnreadableBindingError(
            what=f"{where}: the upstream is a BigQuery table, and this install cannot read one",
            why=problem,
            fix="Install the gcp extra where this build runs: pip install 'data-product-forge[gcp]'",
            doc=doc_url(),
            extras={"platform": "gcp", "format": "bigquery_table"},
        )


def _read_uri(path: str, platform: str, fmt: str, where: str) -> Tuple[str, str, str]:
    """A binding whose ``location.path`` is already a URI: only ``s3://`` reads."""
    from .duckdb.runner import _FILE_FORMAT_EXT

    scheme = path.split("://", 1)[0].lower()
    fmt = fmt or _format_from_suffix(path) or "parquet"
    if scheme != "s3" or fmt not in FILE_FORMATS:
        raise _unreadable(where, platform or scheme, fmt, f"{scheme}:// location")
    uri = f"{path}*.{_FILE_FORMAT_EXT[fmt]}" if path.endswith("/") else path
    return uri, fmt, "aws"


def _landed_by_local_provider(upstream: Mapping[str, Any], expose: Mapping[str, Any]) -> bool:
    """Whether the local provider writes ``expose``: an embedded-SQL build lands it.

    That provider writes the FIRST expose of a contract with an embedded-SQL
    build on DuckDB (``base._execute_embedded_sql_build``), under its own file
    name and format rule. Any other local expose (an acquisition build's, or a
    file nothing in the workspace builds) is where and as what it is declared.
    """
    from .base import LOCAL_SQL_PLATFORMS, embedded_sql_platform, is_embedded_sql_build

    exposes = [e for e in upstream.get("exposes") or [] if isinstance(e, Mapping)]
    if not exposes or exposes[0] is not expose:
        return False
    builds = [b for b in upstream.get("builds") or [] if isinstance(b, dict)]
    return any(
        is_embedded_sql_build(b) and embedded_sql_platform(b) in LOCAL_SQL_PLATFORMS for b in builds
    )


def _read_local(
    path: Optional[str], fmt: str, upstream_path: Path, where: str, *, local_provider: bool
) -> Tuple[str, str, str]:
    """A local binding: its path, anchored at the UPSTREAM contract's directory.

    With ``local_provider`` (the local provider writes it), the file name and
    format are that writer's (``local_provider_landing``): a parquet binding
    at ``out/orders`` is ``out/orders.parquet``, and a ``json`` one is CSV.
    """
    from .duckdb.runner import _FILE_FORMAT_EXT

    if not path:
        raise ConsumesResolutionError(
            what=f"{where}: the upstream expose binds no location.path",
            why=f"{upstream_path} declares the expose with a local binding that names no file.",
            fix="Give the upstream expose a binding.location.path, the file its build writes.",
            doc=doc_url(),
        )
    local = str(resolve_binding_path(path, upstream_path.parent))
    if local_provider:
        local, fmt = local_provider_landing(local, fmt)
    else:
        fmt = fmt if fmt in FILE_FORMATS else (_format_from_suffix(local) or "csv")
    if Path(local).is_dir():
        local = str(Path(local) / f"*.{_FILE_FORMAT_EXT[fmt]}")
    return local, fmt, "local"


def _read_s3_prefix(
    binding: Mapping[str, Any],
    loc: Mapping[str, Any],
    path: Optional[str],
    fmt: str,
    where: str,
) -> Tuple[str, str, str]:
    """An AWS bucket binding: the prefix the duckdb runner writes into, as a glob."""
    from .duckdb.runner import _FILE_FORMAT_EXT, _object_store_uri

    # The IaC emitter's default: a binding with no format is emitted as parquet.
    fmt = fmt or "parquet"
    if fmt not in FILE_FORMATS:
        raise _unreadable(where, "aws", fmt, f"format {fmt}")
    bucket = _resolve_env(loc.get("bucket"), field_name="location.bucket", where=where)
    if not bucket:
        raise _unreadable(where, "aws", fmt, "no location.bucket, so not an S3 prefix")
    if not path:
        raise _unreadable(where, "aws", fmt, "no location.path inside the bucket")
    resolved_loc = {**dict(loc), "bucket": bucket, "path": path}
    prefix = _object_store_uri({**dict(binding), "platform": "aws"}, resolved_loc, path)
    if prefix is None:  # pragma: no cover - a resolved bucket always composes
        raise _unreadable(where, "aws", fmt, "location.bucket did not resolve")
    return f"{prefix}*.{_FILE_FORMAT_EXT[fmt]}", fmt, "aws"


#: The ``binding.location`` keys a BigQuery table is named by.
_BIGQUERY_LOCATION_KEYS = ("project", "dataset", "table", "view", "region", "location")


def _bigquery_target(expose: Mapping[str, Any], where: str) -> Optional[Dict[str, Any]]:
    """The BigQuery table ``expose`` is bound to, or ``None`` when it is not one.

    Named by the IaC's own rule (``_bigquery_load.bigquery_load_target``, which
    reads ``iac/providers/gcp.py``), so this is the table ``fluid apply``
    created and the acquisition runner loads. ``{{ env.* }}`` in the naming
    keys is resolved first, refusing an unset or credential-shaped variable
    (:func:`_resolve_env`), and the project falls back to the environment
    (``GOOGLE_PROJECT`` and friends) as the load's does.
    """
    from ._bigquery_load import bigquery_load_target

    raw_binding = expose.get("binding")
    binding: Mapping[str, Any] = raw_binding if isinstance(raw_binding, Mapping) else {}
    raw_loc = binding.get("location")
    loc: Mapping[str, Any] = raw_loc if isinstance(raw_loc, Mapping) else {}
    if bigquery_load_target(binding, expose) is None:
        return None
    resolved_loc: Dict[str, Any] = dict(loc)
    for key in _BIGQUERY_LOCATION_KEYS:
        if loc.get(key):
            resolved_loc[key] = _resolve_env(
                loc.get(key), field_name=f"location.{key}", where=where
            )
    target = bigquery_load_target({**dict(binding), "location": resolved_loc}, expose)
    if target is not None:
        # ``bigquery_load_target`` falls back to ``US``, the IaC's own default;
        # a sovereignty check must know the location was not declared.
        target["location_declared"] = bool(
            resolved_loc.get("region") or resolved_loc.get("location")
        )
    return target


def _bigquery_table_id(target: Mapping[str, Any]) -> str:
    return f"{target.get('project') or ''}.{target['dataset']}.{target['table']}"


def _read_location(
    expose: Mapping[str, Any], upstream_path: Path, where: str, *, local_provider: bool = False
) -> Tuple[str, str, str, Optional[str]]:
    """``(uri, format, platform, region)`` the upstream expose is READ from."""
    raw_binding = expose.get("binding")
    binding: Mapping[str, Any] = raw_binding if isinstance(raw_binding, Mapping) else {}
    raw_loc = binding.get("location") or expose.get("location") or {}
    loc: Mapping[str, Any] = raw_loc if isinstance(raw_loc, Mapping) else {}
    platform = str(binding.get("platform") or "").strip().lower()
    fmt = _normalize_format(binding.get("format") or expose.get("format"))
    path = _resolve_env(loc.get("path"), field_name="location.path", where=where)
    region = _resolve_env(loc.get("region"), field_name="location.region", where=where)

    if path and is_remote_uri(path):
        return (*_read_uri(path, platform, fmt, where), region)
    if platform in _LOCAL_PLATFORMS:
        return (
            *_read_local(path, fmt, upstream_path, where, local_provider=local_provider),
            None,
        )
    if platform == "aws":
        return (*_read_s3_prefix(binding, loc, path, fmt, where), region)
    raise _unreadable(where, platform, fmt, f"platform {platform}")


def _validated_env(env: Optional[str]) -> Optional[str]:
    """The overlay env, refused when it could name a file outside ``overlays/``."""
    if env is None or env == "":
        return None
    from fluid_build.forge.core.bundle import _ENV_NAME_RE

    if not _ENV_NAME_RE.match(str(env)):
        raise ConsumesResolutionError(
            what=f"--env {env!r} is not an environment name",
            why="The upstream contracts are loaded with this env's overlay, so it selects a file.",
            fix="Pass a plain environment name such as dev, local or aws.",
            doc=doc_url(),
        )
    return str(env)


# ── Resolution ──────────────────────────────────────────────────────────


def _entry_ids(entry: Mapping[str, Any]) -> Tuple[str, str]:
    """``(productId, exposeId)`` of one consumes entry; both are required."""
    product_id = str(entry.get("productId") or "").strip()
    expose_id = str(entry.get("exposeId") or "").strip()
    if not product_id or not expose_id:
        raise ConsumesResolutionError(
            what=f"a consumes entry names no {'productId' if not product_id else 'exposeId'}",
            why=f"The entry is {dict(entry)!r}; both productId and exposeId are required.",
            fix="Name the upstream product and the expose this build reads.",
            doc=doc_url(),
        )
    return product_id, expose_id


def _claim_view(product_id: str, expose_id: str, views: Dict[str, str]) -> None:
    """Refuse an exposeId that cannot name a view, or names one already taken."""
    from fluid_build.providers._sql_safety import validate_ident

    where = _entry(product_id, expose_id)
    try:
        validate_ident(expose_id)
    except ValueError:
        raise ConsumesResolutionError(
            what=f"{where}: exposeId {expose_id!r} cannot name a SQL view",
            why=(
                "Each consumes entry is registered as a view named by its exposeId, which "
                "must be a plain identifier (letters, digits and underscores, not starting "
                "with a digit) for the SQL to read it."
            ),
            fix=_EXPLICIT_INPUT_FIX.format(name="<a view name>"),
            doc=doc_url(),
        ) from None
    other = views.get(expose_id.casefold())
    if other is not None:
        raise ConsumesResolutionError(
            what=f"{where}: view {expose_id!r} is also {other}'s",
            why="Two consumes entries would register the same view name, and one would hide the other.",
            fix=_EXPLICIT_INPUT_FIX.format(name=expose_id),
            doc=doc_url(),
        )
    views[expose_id.casefold()] = product_id


def _split_covered(
    entries: Sequence[Mapping[str, Any]],
    explicit: Mapping[str, str],
    read: Optional[FrozenSet[str]],
) -> Tuple[List[CoveredInput], List[LineageOnlyInput], List[Tuple[str, str]]]:
    """``(covered, lineage_only, pending)`` of the consumes entries.

    COVERED: an explicit input binds the name. LINEAGE ONLY: the SQL reads no
    relation of that name (``read`` is :func:`relations_read`; ``None`` means
    it could not tell, and then nothing is lineage only). PENDING: the rest,
    which must resolve.
    """
    covered: List[CoveredInput] = []
    lineage_only: List[LineageOnlyInput] = []
    pending: List[Tuple[str, str]] = []
    views: Dict[str, str] = {}
    for entry in entries:
        product_id, expose_id = _entry_ids(entry)
        if expose_id.casefold() in explicit:
            covered.append(CoveredInput(product_id, expose_id, explicit[expose_id.casefold()]))
            continue
        if read is not None and expose_id.casefold() not in read:
            lineage_only.append(LineageOnlyInput(product_id, expose_id))
            continue
        _claim_view(product_id, expose_id, views)
        pending.append((product_id, expose_id))
    return covered, lineage_only, pending


def _refuse_self_consume(contract: Mapping[str, Any], pending: Sequence[Tuple[str, str]]) -> None:
    """A build never reads its own product: it would read what it is about to overwrite."""
    own = str(contract.get("id") or "").strip()
    for product_id, expose_id in pending:
        if own and product_id == own:
            raise ConsumesResolutionError(
                what=f"{_entry(product_id, expose_id)}: the contract consumes its own id",
                why=(
                    f"{own!r} is this contract. Resolving the entry would read the file this "
                    "build is about to overwrite, so the result would depend on the previous run."
                ),
                fix=(
                    "Consume the upstream product this data comes from instead. "
                    + _EXPLICIT_INPUT_FIX.format(name=expose_id)
                ),
                doc=doc_url(),
                extras={"productId": product_id},
            )


@dataclass(frozen=True)
class _Workspace:
    """Where the upstream contracts were looked for, and what was found."""

    contract_dir: Path
    root: Optional[Path]
    roots: List[Path]
    index: Dict[str, List[Path]]
    skipped: List[Tuple[Path, str]]


def _upstream_contract(ws: _Workspace, product_id: str, expose_id: str) -> Path:
    """The one contract file declaring ``product_id``, or the typed error."""
    where = _entry(product_id, expose_id)
    paths = ws.index.get(product_id) or []
    if not paths:
        unread = "; ".join(f"{p} ({why})" for p, why in ws.skipped)
        raise ConsumesResolutionError(
            what=f"{where}: no contract in the workspace declares id {product_id!r}",
            why=(
                f"Looked in {_looked_in(ws.root, ws.roots, ws.contract_dir)}."
                + (f" Could not read: {unread}." if unread else "")
            ),
            fix=(
                "Check the productId, keep the upstream contract under the directory "
                "holding fluid.workspace.yaml, or add its repository to "
                "FLUID_UPSTREAM_CONTRACTS. " + _EXPLICIT_INPUT_FIX.format(name=expose_id)
            ),
            doc=doc_url(),
            extras={"productId": product_id, "roots": [str(r) for r in ws.roots]},
        )
    if len(paths) > 1:
        listed = " and ".join(str(p) for p in paths)
        raise ConsumesResolutionError(
            what=f"{where}: id {product_id!r} is declared by {len(paths)} contracts",
            why=f"Both {listed} declare it, and which one this build reads would be a guess.",
            fix="Give each contract its own id, or remove the stale copy.",
            doc=doc_url(),
            extras={"productId": product_id, "paths": [str(p) for p in paths]},
        )
    return paths[0]


def _upstream_expose(
    upstream_path: Path, product_id: str, expose_id: str, env: Optional[str], log: logging.Logger
) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    """``(upstream contract, its expose expose_id)``, as this run's ``env`` overlay leaves them."""
    from fluid_build._contract_loader import load_contract_with_overlay

    where = _entry(product_id, expose_id)
    try:
        upstream = load_contract_with_overlay(str(upstream_path), env, log)
    except Exception as exc:  # noqa: BLE001 - reported as the typed error below
        raise ConsumesResolutionError(
            what=f"{where}: could not load {upstream_path}",
            why=f"{type(exc).__name__}: {exc}",
            fix="Fix the upstream contract (`fluid validate` it), or bind the input by hand.",
            doc=doc_url(),
        ) from exc
    expose = _find_expose(upstream, expose_id)
    if expose is None:
        available = [
            str(e.get("exposeId") or e.get("id"))
            for e in upstream.get("exposes") or []
            if isinstance(e, Mapping)
        ]
        raise ConsumesResolutionError(
            what=f"{where}: {product_id} has no expose {expose_id!r}",
            why=(
                f"{upstream_path}"
                + (f" with the {env!r} overlay" if env else "")
                + f" exposes {', '.join(available) or 'nothing'}."
            ),
            fix="Name one of those exposes in exposeId.",
            doc=doc_url(),
        )
    return upstream, expose


@dataclass
class _Bound:
    """What :func:`_bind_consumes` decided for each consumes entry."""

    resolved: List[ResolvedInput] = field(default_factory=list)
    covered: List[CoveredInput] = field(default_factory=list)
    lineage_only: List[LineageOnlyInput] = field(default_factory=list)
    workspace_root: Optional[Path] = None


def _bind_consumes(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    contract_dir: Path,
    *,
    env: Optional[str],
    logger: Optional[logging.Logger],
) -> _Bound:
    from fluid_build.util.upstream_discovery import index_contract_paths

    entries = [c for c in contract.get("consumes") or [] if isinstance(c, Mapping)]
    if not entries:
        return _Bound()
    env = _validated_env(env)
    props = build.get("properties") if isinstance(build.get("properties"), Mapping) else {}
    read = relations_read(str((props or {}).get("sql") or ""))
    covered, lineage_only, pending = _split_covered(entries, explicit_input_names(build), read)
    bound = _Bound(covered=covered, lineage_only=lineage_only)
    if not pending:
        return bound
    _refuse_self_consume(contract, pending)

    root, roots = _search_roots(contract_dir)
    index, skipped = index_contract_paths(roots)
    ws = _Workspace(Path(contract_dir), root, roots, index, skipped)
    for product_id, expose_id in pending:
        upstream_path = _upstream_contract(ws, product_id, expose_id)
        upstream, expose = _upstream_expose(
            upstream_path, product_id, expose_id, env, logger or LOG
        )
        where = _entry(product_id, expose_id)
        bigquery = _bigquery_target(expose, where)
        if bigquery is not None:
            # Before the location.path check: a bigquery_table binding may also
            # carry a gs:// staging path, which is not where the table's rows are.
            _require_bigquery_reader(where)
            table_id = _bigquery_table_id(bigquery)
            bound.resolved.append(
                ResolvedInput(
                    product_id=product_id,
                    expose_id=expose_id,
                    uri=f"bigquery://{table_id.lstrip('.')}",
                    format="bigquery_table",
                    platform="gcp",
                    contract_path=upstream_path,
                    region=str(bigquery["location"]),
                    table=table_id,
                    region_declared=bool(bigquery.get("location_declared")),
                )
            )
            continue
        uri, fmt, platform, region = _read_location(
            expose,
            upstream_path,
            _entry(product_id, expose_id),
            local_provider=_landed_by_local_provider(upstream, expose),
        )
        bound.resolved.append(
            ResolvedInput(
                product_id=product_id,
                expose_id=expose_id,
                uri=uri,
                format=fmt,
                platform=platform,
                contract_path=upstream_path,
                region=region,
            )
        )
    bound.workspace_root = root
    return bound


def resolve_consumes(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    contract_dir: Path,
    *,
    env: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[List[ResolvedInput], List[CoveredInput], Optional[Path]]:
    """Bind every ``consumes[]`` entry the SQL reads, or raise :class:`ConsumesResolutionError`.

    Returns ``(resolved, covered, workspace_root)``. An entry whose ``exposeId``
    names an explicit ``builds[].properties.parameters.inputs`` entry is
    COVERED: the explicit input wins on the name collision and the entry is
    not resolved at all. An entry whose ``exposeId`` names no relation the
    build's SQL reads (:func:`relations_read`) is lineage only and is not
    resolved either (:func:`plan_embedded_sql_io` reports those). Every other
    entry must resolve; the first that does not raises, naming its productId
    and where it looked, so the SQL never runs with an input missing.
    """
    bound = _bind_consumes(contract, build, contract_dir, env=env, logger=logger)
    return bound.resolved, bound.covered, bound.workspace_root


# ── Landing ─────────────────────────────────────────────────────────────


def _landed_exposes(
    contract: Mapping[str, Any], build: Mapping[str, Any]
) -> List[Mapping[str, Any]]:
    """The first expose (what the local provider writes) plus any the build names."""
    exposes = [e for e in contract.get("exposes") or [] if isinstance(e, Mapping)]
    if not exposes:
        return []
    named = {str(o) for o in build.get("outputs") or [] if isinstance(o, str)}
    extra = [e for e in exposes[1:] if (e.get("exposeId") or e.get("id")) in named]
    return [exposes[0], *extra]


def refuse_unapplied_masking(contract: Mapping[str, Any], build: Mapping[str, Any]) -> None:
    """Raise :class:`MaskingNotAppliedError` when a landed expose declares masking."""
    for expose in _landed_exposes(contract, build):
        policy = expose.get("policy") if isinstance(expose.get("policy"), Mapping) else {}
        privacy = policy.get("privacy") if isinstance(policy.get("privacy"), Mapping) else {}
        masking = privacy.get("masking") if isinstance(privacy, Mapping) else None
        if not masking:
            continue
        expose_id = str(expose.get("exposeId") or expose.get("id") or "?")
        columns = sorted(
            {str(r.get("column")) for r in masking if isinstance(r, Mapping) and r.get("column")}
        )
        raise MaskingNotAppliedError(
            what=(
                f"expose {expose_id!r} declares policy.privacy.masking, which the embedded-SQL "
                "landing path does not apply yet"
            ),
            why=(
                "This build writes the query result as the SQL returns it, so "
                f"{', '.join(columns) or 'the masked columns'} would land in cleartext."
            ),
            fix=(
                "Land this expose through an engine that enforces its masking, or write the "
                "masking into properties.sql and declare the columns as they then land."
            ),
            doc=doc_url(),
            extras={"exposeId": expose_id, "columns": columns},
        )


#: ``binding.platform`` values whose storage this path cannot write, and whose
#: expose the local provider would otherwise write as a local file: another
#: cloud's object store or a warehouse. A gcp ``bigquery_table`` is checked
#: before this, and lands through a load job; a GCS bucket or any other gcp
#: resource does not. Platforms another stage delivers from the landed file
#: (an output port such as pgvector) keep the local write.
_UNLANDABLE_PLATFORMS = frozenset({"gcp", "azure", "snowflake", "databricks"})


def _refuse_unlandable(binding: Mapping[str, Any], path: Any, platform: str, where: str) -> None:
    """Refuse a landing this path would otherwise write to a local file.

    The local provider writes whatever ``location.path`` it is given to the
    local filesystem, so a ``gs://`` path became a file named ``gs:/...`` and a
    GCS or other-cloud binding a local file, and the build reported success
    with nothing where the contract said it would be.
    """
    fmt = _normalize_format(binding.get("format")) or "unset"
    text = str(path or "")
    scheme = text.split("://", 1)[0].lower() if is_remote_uri(text) else ""
    if scheme and scheme != "s3":
        raise EmbeddedSqlLandingError(
            what=f"{where}: the embedded-SQL path cannot land in a {scheme}:// location",
            why=(
                f"The binding (platform {platform or 'unset'!r}, format {fmt!r}) names {text}. "
                "This path writes a local file, an S3 object, or loads a BigQuery table, and "
                "the local writer would have created a file named after that URI instead."
            ),
            fix=(
                "Bind the expose as a gcp bigquery_table (it is loaded through a load job), "
                "an aws bucket prefix, or a local path, or land it with another engine."
            ),
            doc=doc_url(),
            extras={"platform": platform or "unset", "format": fmt, "scheme": scheme},
        )
    if platform in _UNLANDABLE_PLATFORMS:
        raise EmbeddedSqlLandingError(
            what=f"{where}: the embedded-SQL path cannot land a {platform} binding ({fmt})",
            why=(
                "This path writes a local file, an S3 object for an aws binding naming a "
                "bucket, or loads a gcp bigquery_table; a "
                f"{platform} binding of format {fmt!r} is none of them, and writing it as a "
                "local file would report success with nothing where the contract says."
            ),
            fix=("Bind the expose as one of those, or land it with an engine for that platform."),
            doc=doc_url(),
            extras={"platform": platform, "format": fmt},
        )


def _require_bigquery_writer(where: str) -> None:
    from ._bigquery_read import missing_dependency

    problem = missing_dependency()
    if problem:
        raise EmbeddedSqlLandingError(
            what=f"{where}: the expose is a BigQuery table, and this install cannot load one",
            why=problem,
            fix="Install the gcp extra where this build runs: pip install 'data-product-forge[gcp]'",
            doc=doc_url(),
        )


def bigquery_landing(
    contract: Mapping[str, Any], build: Mapping[str, Any]
) -> Optional[BigQueryLanding]:
    """The BigQuery table the result is loaded into, or ``None`` when it lands elsewhere.

    The first expose, as for every landing here (the local provider writes
    exposes[0]). A further expose the build names in ``outputs`` is not
    landed by this path at all (:func:`further_outputs`).
    """
    exposes = _landed_exposes(contract, build)
    if not exposes:
        return None
    expose = exposes[0]
    expose_id = str(expose.get("exposeId") or expose.get("id") or "result")
    where = f"expose {expose_id}"
    target = _bigquery_target(expose, where)
    if target is None:
        return None
    _require_bigquery_writer(where)
    return BigQueryLanding(
        project=target.get("project") or None,
        dataset=str(target["dataset"]),
        table=str(target["table"]),
        location=str(target["location"]),
        region_declared=bool(target.get("location_declared")),
        expose_id=expose_id,
    )


#: ``binding.platform`` values whose expose a further output would have to be
#: written to off this machine: an object store or a warehouse this path
#: writes only for the first expose, if at all.
_REMOTE_OUTPUT_PLATFORMS = frozenset({"aws"}) | _UNLANDABLE_PLATFORMS


def further_outputs(contract: Mapping[str, Any], build: Mapping[str, Any]) -> List[str]:
    """Refuse a further output this path would not land remotely; warn about a local one.

    The local provider writes one result, the first expose's
    (``LocalProvider._derive_actions_from_contract``), and this path lands
    that one only: in S3, in a BigQuery table, or as a local file. Every
    other expose the build names in ``outputs`` is written nowhere. One bound
    to a cloud store or a warehouse (a BigQuery table, a ``gs://`` or S3
    prefix, another platform's binding) is refused: the build would report
    success with nothing where that contract says. A local one, or an output
    port another stage delivers (pgvector, kafka, ...), is returned as a
    warning to print, which is what the build did before, said out loud.
    """
    exposes = _landed_exposes(contract, build)
    first_id = str(exposes[0].get("exposeId") or exposes[0].get("id") or "?") if exposes else "?"
    warnings: List[str] = []
    for extra in exposes[1:]:
        extra_id = str(extra.get("exposeId") or extra.get("id") or "?")
        raw_binding = extra.get("binding")
        binding: Mapping[str, Any] = raw_binding if isinstance(raw_binding, Mapping) else {}
        raw_loc = binding.get("location")
        loc: Mapping[str, Any] = raw_loc if isinstance(raw_loc, Mapping) else {}
        platform = str(binding.get("platform") or "").strip().lower()
        path = str(loc.get("path") or "")
        if platform in _REMOTE_OUTPUT_PLATFORMS or is_remote_uri(path):
            raise EmbeddedSqlLandingError(
                what=(
                    f"expose {extra_id}: the embedded-SQL path lands one result, the first "
                    f"expose's ({first_id})"
                ),
                why=(
                    f"The build names {extra_id!r} in outputs and binds it to "
                    f"{platform or 'a remote location'} ({path or 'no path'}), which nothing "
                    "would write: the build would report success without it."
                ),
                fix="Land one expose per embedded-SQL build, or give this one its own build.",
                doc=doc_url(),
                extras={"exposeId": extra_id, "platform": platform or "unset"},
            )
        warnings.append(
            f"expose {extra_id} is named in the build's outputs, but this path writes only "
            f"the first expose ({first_id}); {extra_id} is not written by this build"
        )
    return warnings


def bigquery_staging_dir(contract_dir: Path, build_id: Any) -> Path:
    """``.fluid/staging/<build>`` beside the contract, where the acquisition
    runner stages a BigQuery-bound file too (``_bigquery_staging_path``)."""
    from .duckdb.runner import _path_part

    return Path(contract_dir) / ".fluid" / "staging" / _path_part(build_id or "build")


def stage_bigquery_io(
    io: EmbeddedSqlIO, contract_dir: Path, build_id: Any, *, logger: logging.Logger
) -> Tuple[EmbeddedSqlIO, List[Path], List[Dict[str, Any]]]:
    """Read each BigQuery upstream into a local Parquet file; name the result's staged file.

    Returns ``(io with every BigQuery input staged and the landing's staged
    path set, the staged input files, one fact per table read)``. The caller
    removes the staged inputs after the build: they are copies of another
    product's table.
    """
    import dataclasses

    from ._bigquery_read import stage_table
    from .duckdb.runner import _path_part

    root = bigquery_staging_dir(contract_dir, build_id)
    staged: List[Path] = []
    reads: List[Dict[str, Any]] = []
    inputs: List[ResolvedInput] = []
    try:
        for r in io.inputs:
            if r.table is None:
                inputs.append(r)
                continue
            dest = root / "inputs" / f"{_path_part(r.expose_id)}.parquet"
            staged.append(dest)
            reads.append({**stage_table(r.table, dest, logger=logger), "view": r.view})
            inputs.append(dataclasses.replace(r, read_path=str(dest)))
    except Exception:
        remove_staged(staged)
        raise
    landing = io.bigquery_landing
    if landing is not None:
        landing = dataclasses.replace(
            landing, staged=str(root / f"{_path_part(landing.table)}.parquet")
        )
    return dataclasses.replace(io, inputs=inputs, bigquery_landing=landing), staged, reads


def remove_staged(paths: Sequence[Path]) -> None:
    """Delete staged upstream copies; a file already gone is not an error."""
    for path in paths:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - reported, never raised
            LOG.warning("embedded_sql_staged_input_not_removed path=%s error=%s", path, exc)


def load_bigquery_landing(landing: BigQueryLanding, *, logger: logging.Logger) -> Dict[str, Any]:
    """Load the staged result into its table: the acquisition runner's own load.

    ``WRITE_TRUNCATE``: the embedded-SQL result replaces the table, as it
    replaces the local file or the S3 object on the other targets. The count
    the load is held to is read back from the staged file.
    """
    from ._bigquery_load import load_file
    from .duckdb.runner import _count_file_rows

    if landing.staged is None:
        raise EmbeddedSqlLandingError(
            what=f"BigQuery table {landing.table_id}: the result was not staged",
            why="The load reads the staged Parquet file the SQL writes, and none was named.",
            fix="Report this: the build must stage the result before it loads it.",
            doc=doc_url(),
        )
    return load_file(
        landing.staged,
        landing.load_target(),
        mode="full_refresh",
        sink_format="parquet",
        expected_rows=_count_file_rows(landing.staged, "parquet"),
        logger=logger,
    )


def write_bigquery_run_record(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    contract_dir: Path,
    landing: BigQueryLanding,
    *,
    started_at: str,
    facts: Optional[Mapping[str, Any]] = None,
    error: Optional[str] = None,
    logger: logging.Logger = LOG,
) -> Optional[str]:
    """Record a BigQuery-landing run where ``fluid verify`` reads the acquisition runs.

    The acquisition runner records each run under
    ``<contract dir>/.fluid/runs/<product>/<build>/runs/`` (``FileStateStore``),
    with ``facets.bigquery_load`` naming the table and the rows the load
    landed, and the BigQuery verifier holds the table's ``COUNT(*)`` to the
    newest such run (``_verify_bigquery``). This writes the same record for an
    embedded-SQL build, so silver and gold are held to the rows their load
    landed, as bronze is: ``records_total`` is the load's count, which
    :func:`load_bigquery_landing` has already held to the staged file's rows
    (``rows_from: write``), and the mode is ``full_refresh`` (the load is
    ``WRITE_TRUNCATE``). It is dbt's pattern too: ``run_results.json`` keeps
    each node's ``relation_name`` and ``adapter_response.rows_affected``.

    A failed run is recorded without ``bigquery_load``: whether it changed
    the table is unknown, so verify reports the count without a comparison
    until the next run succeeds. Returns the run id, or ``None`` when ids the
    state store refuses (``validate_identifier``) leave nothing to record.
    """
    from ._acquisition_common import generate_run_id, utc_now_iso
    from ._ids import IdentifierViolation, validate_identifier
    from ._state import FileStateStore

    try:
        product_id = validate_identifier(str(contract.get("id") or ""), kind="contract.id")
        build_id = validate_identifier(str(build.get("id") or ""), kind="build.id")
    except IdentifierViolation as exc:
        logger.warning("embedded_sql_run_record_skipped reason=%s", type(exc).__name__)
        return None
    run_id = generate_run_id()
    succeeded = facts is not None and error is None
    facets: Dict[str, Any] = {"engine": "duckdb", "pattern": "embedded-logic"}
    if succeeded and facts is not None:
        facets["bigquery_load"] = dict(facts)
        facets["landed"] = {
            "mode": "full_refresh",
            "rows_from": "write",
            "destinations": {landing.expose_id: f"bigquery://{facts.get('table')}"},
        }
    record: Dict[str, Any] = {
        "run_id": run_id,
        "state": "succeeded" if succeeded else "failed",
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "records_total": int(facts["rows"]) if succeeded and facts is not None else 0,
        "streams": [],
        "facets": facets,
    }
    if error is not None:
        record["error"] = error
    FileStateStore(Path(contract_dir) / ".fluid").write_run_record(product_id, build_id, record)
    return run_id


def refuse_unlandable_first_expose(contract: Mapping[str, Any]) -> None:
    """For a DuckDB SQL build with no inline SQL: refuse what it would land locally.

    Such a build keeps the provider's old handling (no consumes resolution and
    no remote landing), so a BigQuery table, a GCS path or another platform's
    binding would be written as a local file of that name.
    """
    exposes = [e for e in contract.get("exposes") or [] if isinstance(e, Mapping)]
    if not exposes:
        return
    expose = exposes[0]
    expose_id = str(expose.get("exposeId") or expose.get("id") or "result")
    where = f"expose {expose_id}"
    binding = expose.get("binding") if isinstance(expose.get("binding"), Mapping) else {}
    if _bigquery_target(expose, where) is not None:
        raise EmbeddedSqlLandingError(
            what=f"{where}: only a build with inline properties.sql loads a BigQuery table",
            why=(
                "This build has no inline SQL, so it runs the provider's multi-stage path, "
                "which writes local files and would land nothing in the table."
            ),
            fix="Give the build its SQL in properties.sql.",
            doc=doc_url(),
        )
    raw_loc = binding.get("location")
    loc: Mapping[str, Any] = raw_loc if isinstance(raw_loc, Mapping) else {}
    _refuse_unlandable(
        binding, loc.get("path"), str(binding.get("platform") or "").strip().lower(), where
    )


def object_store_landing(
    contract: Mapping[str, Any], build: Mapping[str, Any]
) -> Optional[Landing]:
    """The S3 object the result lands in, or ``None`` for a local landing.

    ``contract`` is the one ``{{ env.* }}`` templates are still in, so an unset
    bucket variable is refused rather than resolved to ``""`` (which would
    quietly turn an S3 binding into a local file).

    The duckdb runner's rule, applied to the first expose: a binding naming a
    ``location.bucket`` and a ``location.path`` composes
    ``s3://<bucket>/<path>/`` (``_object_store_uri``), and the file inside it is
    ``<location.table>.<ext>``, else ``<exposeId>.<ext>`` where the runner uses
    the stream (``_file_within_prefix``). A ``path`` that is already an
    ``s3://`` URI is used as the runner uses it. No bucket, or no path, keeps
    the local write, as it does in the runner.

    ``None`` for a BigQuery table too, which :func:`bigquery_landing` lands.
    A landing this path cannot perform (another URI scheme, a platform that is
    neither local, aws nor a BigQuery table) is refused
    (:func:`_refuse_unlandable`) instead of becoming a local file.
    """
    from .duckdb.runner import _file_within_prefix, _object_store_uri

    exposes = [e for e in contract.get("exposes") or [] if isinstance(e, Mapping)]
    if not exposes:
        return None
    expose = exposes[0]
    binding = expose.get("binding") if isinstance(expose.get("binding"), Mapping) else {}
    raw_loc = binding.get("location")
    loc: Mapping[str, Any] = raw_loc if isinstance(raw_loc, Mapping) else {}
    expose_id = str(expose.get("exposeId") or expose.get("id") or "result")
    where = f"expose {expose_id}"
    if _bigquery_target(expose, where) is not None:
        return None  # a BigQuery table: :func:`bigquery_landing` loads it
    raw_path = loc.get("path")
    platform = str(binding.get("platform") or "").strip().lower()
    _refuse_unlandable(binding, raw_path, platform, where)
    if not raw_path:
        return None
    names_bucket = platform == "aws" and bool(loc.get("bucket"))
    if not names_bucket and not is_remote_uri(str(raw_path)):
        return None  # a local binding: unchanged

    path = str(_resolve_env(raw_path, field_name="location.path", where=where))
    resolved_loc: Dict[str, Any] = {
        **dict(loc),
        "path": path,
        "table": _resolve_env(loc.get("table"), field_name="location.table", where=where),
    }
    region = _resolve_env(loc.get("region"), field_name="location.region", where=where)
    uri: Optional[str]
    if is_remote_uri(path):
        # A resolved path that is still a non-S3 URI is refused like one written
        # out: the local provider would write a file named "gs:/..." instead.
        _refuse_unlandable(binding, path, platform, where)
        uri = path
    else:
        resolved_loc["bucket"] = _resolve_env(
            loc.get("bucket"), field_name="location.bucket", where=where
        )
        uri = _object_store_uri(dict(binding), resolved_loc, path)
    if uri is None:
        return None  # not S3: the provider's own handling, as before

    fmt = _normalize_format(binding.get("format")) or "parquet"
    if fmt not in FILE_FORMATS:
        raise EmbeddedSqlLandingError(
            what=f"{where}: the embedded-SQL path cannot land format {fmt!r} in S3",
            why=(
                f"The binding names {uri}; this path writes parquet, csv or json files there, "
                f"as the duckdb acquisition runner does, and {fmt!r} is none of them."
            ),
            fix="Declare binding.format parquet, csv or json, or land the expose with another engine.",
            doc=doc_url(),
        )
    return Landing(
        uri=_file_within_prefix(uri, resolved_loc, expose_id, fmt), format=fmt, region=region
    )


def _same_bigquery_table(input_table: str, landing: BigQueryLanding) -> bool:
    """Whether a BigQuery input and the landing may be one table.

    Names compared case-insensitively. A project left to the client (empty on
    either side) matches any project: both resolve to the client's own at run
    time, so they may well be the same, and the doubt is refused, not assumed.
    """
    project, dataset, table = input_table.rsplit(".", 2)
    if (dataset.lower(), table.lower()) != (landing.dataset.lower(), landing.table.lower()):
        return False
    return not project or not landing.project or project.lower() == landing.project.lower()


def _s3_read_prefix(uri: str) -> str:
    """The prefix an S3 input reads: the glob's directory, or the one object itself."""
    last = uri.rsplit("/", 1)[-1]
    if any(ch in last for ch in "*?["):
        return uri[: len(uri) - len(last)]
    return uri


def refuse_landing_into_input(io: EmbeddedSqlIO) -> None:
    """Refuse a landing that would write into one of the build's own inputs.

    The same rule Dagster applies to an asset whose dependency is its own key
    (``_validate_self_deps``, "Asset ... depends on itself"), on what the two
    resolve to rather than on product ids: :func:`_refuse_self_consume` already
    refuses a contract consuming its own id, but an overlay naming an
    upstream's table (a typo, or two products left to the ``default``
    dataset with the same expose id) passed it, and the build's
    ``WRITE_TRUNCATE`` replaced another product's rows with its query result.
    That is a data write, not a planned delete, so ``--allow-data-loss`` is
    never asked. An S3 landing inside the prefix an input reads is refused for
    the same reason: the object would become rows of that product's table.
    """
    if io.bigquery_landing is not None:
        _refuse_loading_an_input(io.bigquery_landing, io.inputs)
    if io.landing is not None:
        _refuse_landing_in_an_input_prefix(io.landing, io.inputs)


def _refuse_loading_an_input(bq: BigQueryLanding, inputs: Sequence[ResolvedInput]) -> None:
    for r in inputs:
        if r.table is None or not _same_bigquery_table(r.table, bq):
            continue
        raise EmbeddedSqlLandingError(
            what=(
                f"expose {bq.expose_id}: the result would be loaded into BigQuery table "
                f"{bq.table_id}, which {_entry(r.product_id, r.expose_id)} reads"
            ),
            why=(
                f"The load replaces the table (WRITE_TRUNCATE), so {r.product_id}'s rows "
                "would be overwritten with this build's query result."
            ),
            fix=(
                "Bind this expose to its own dataset and table in the overlay, and name "
                "the project on both bindings so they cannot resolve to the same table."
            ),
            doc=doc_url(),
            extras={"table": bq.table_id, "productId": r.product_id},
        )


def _refuse_landing_in_an_input_prefix(landing: Landing, inputs: Sequence[ResolvedInput]) -> None:
    for r in inputs:
        if r.table is not None or not r.uri.lower().startswith("s3://"):
            continue
        prefix = _s3_read_prefix(r.uri)
        inside = prefix.endswith("/") and landing.uri.startswith(prefix)
        if landing.uri != prefix and not inside:
            continue
        raise EmbeddedSqlLandingError(
            what=(
                f"the result would land at {landing.uri}, inside {prefix}, which "
                f"{_entry(r.product_id, r.expose_id)} reads"
            ),
            why=(
                f"Every object under that prefix is a row source of {r.product_id}'s "
                "table, so this build would add its result to another product's data."
            ),
            fix="Bind this expose to a prefix of its own in the overlay.",
            doc=doc_url(),
            extras={"uri": landing.uri, "productId": r.product_id},
        )


#: BigQuery's two multi-regions, by the jurisdiction Google's own location
#: value groups put them in: ``in:eu-locations`` lists ``EU`` and
#: ``in:us-locations`` lists ``US`` (Resource Manager, "Restricting resource
#: locations"). The region table the sovereignty validator reads
#: (``policy.sovereignty.region_jurisdiction_map``) carries single regions only.
_BIGQUERY_MULTI_REGIONS = {"eu": "EU", "us": "US"}


def _jurisdiction(location: str) -> str:
    from fluid_build.policy.sovereignty import region_jurisdiction_map

    table = region_jurisdiction_map()
    text = str(location or "").strip()
    return (
        table.get(text)
        or table.get(text.lower())
        or _BIGQUERY_MULTI_REGIONS.get(text.lower())
        or "Unknown"
    )


#: A sovereignty finding: ``(severity, message)``, the severity ``severity_for``'s.
_Finding = Tuple[str, str]


def refuse_sovereignty_breach(contract: Mapping[str, Any], io: EmbeddedSqlIO) -> List[str]:
    """Hold the BigQuery tables this build reads and loads to the contract's ``sovereignty``.

    ``fluid validate`` checks a binding's declared ``region`` and skips one
    that declares none, and a BigQuery binding with no region is created,
    loaded and read in ``US`` (the IaC's default). Reading an EU upstream
    through this path and loading the result is a copy the build itself
    makes, so it is checked here, before anything is read, by the validator's
    own rules (``policy.sovereignty.SovereigntyValidator``) on the locations
    the reads and the load actually use:

    * a BigQuery input or landing whose binding names no region;
    * the landing's location in ``deniedRegions`` (an error in every mode),
      outside ``allowedRegions``, or outside ``jurisdiction``;
    * with ``dataResidency`` and no ``crossBorderTransfer`` (the schema's
      defaults), an input in another jurisdiction than the landing.

    A finding blocks at the severity ``enforcementMode`` gives it
    (``severity_for``: strict refuses, advisory warns, audit logs). Returns
    the warnings to print; an unknown jurisdiction is a warning, never an
    agreement. Nothing is checked for a build with no BigQuery read or load.
    """
    raw = contract.get("sovereignty")
    sovereignty: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    if not sovereignty or (io.bigquery_landing is None and not io.bigquery_inputs):
        return []
    from fluid_build.policy.sovereignty import (
        DEFAULT_ENFORCEMENT_MODE,
        EnforcementMode,
        severity_for,
    )

    try:
        mode = EnforcementMode(sovereignty.get("enforcementMode", DEFAULT_ENFORCEMENT_MODE))
    except ValueError:
        mode = EnforcementMode.STRICT  # a mode the schema does not know enforces, not less
    blocking = severity_for(mode)
    findings = [
        *_undeclared_region_findings(io, blocking),
        *_landing_region_findings(io.bigquery_landing, sovereignty, blocking),
        *_cross_border_findings(io, sovereignty, blocking),
    ]
    errors = [m for sev, m in findings if sev == "error"]
    if errors:
        more = f" (and {len(errors) - 1} more below)" if len(errors) > 1 else ""
        raise EmbeddedSqlSovereigntyError(
            what=f"the contract's sovereignty block refuses this build: {errors[0]}{more}",
            why="; ".join(errors),
            fix=(
                "Name a region on every BigQuery binding (the overlay's location.region), "
                "inside sovereignty.allowedRegions and the declared jurisdiction."
            ),
            doc=doc_url(),
            extras={"enforcementMode": mode.value, "findings": errors},
        )
    for message in (m for sev, m in findings if sev == "info"):
        LOG.info("embedded_sql_sovereignty_audit %s", message)
    return [f"sovereignty: {m}" for sev, m in findings if sev == "warning"]


def _undeclared_region_findings(io: EmbeddedSqlIO, blocking: str) -> List[_Finding]:
    """A BigQuery input or landing whose location is the IaC's default, not declared."""
    findings: List[_Finding] = [
        (
            blocking,
            f"{_entry(r.product_id, r.expose_id)}: BigQuery table {r.table} names no region, "
            f"so it is read from the default location {r.region}",
        )
        for r in io.bigquery_inputs
        if not r.region_declared
    ]
    bq = io.bigquery_landing
    if bq is not None and not bq.region_declared:
        findings.append(
            (
                blocking,
                f"expose {bq.expose_id}: BigQuery table {bq.table_id} names no region, so it "
                f"would be loaded in {bq.location}",
            )
        )
    return findings


def _landing_region_findings(
    bq: Optional[BigQueryLanding], sovereignty: Mapping[str, Any], blocking: str
) -> List[_Finding]:
    """The validator's checks 1 to 3 on the location the load goes to."""
    if bq is None:
        return []
    from fluid_build.policy.sovereignty import UNCONSTRAINED_JURISDICTIONS

    where = f"expose {bq.expose_id}: BigQuery table {bq.table_id}"
    allowed = [str(x) for x in sovereignty.get("allowedRegions") or []]
    findings: List[_Finding] = []
    # Denied is an error in every mode, as the validator's check 1 is.
    if bq.location in [str(x) for x in sovereignty.get("deniedRegions") or []]:
        findings.append(("error", f"{where}: region {bq.location} is explicitly denied"))
    if allowed and bq.location not in allowed:
        findings.append(
            (
                blocking,
                f"{where}: region {bq.location} is not in allowedRegions ({', '.join(allowed)})",
            )
        )
    jurisdiction = sovereignty.get("jurisdiction")
    if not jurisdiction or jurisdiction in UNCONSTRAINED_JURISDICTIONS:
        return findings
    found = _jurisdiction(bq.location)
    if found == "Unknown":
        findings.append(("warning", f"{where}: region {bq.location} has no known jurisdiction"))
    elif found not in (jurisdiction, "Global"):
        findings.append(
            (
                blocking,
                f"{where}: region {bq.location} is in {found}, not the required "
                f"jurisdiction {jurisdiction}",
            )
        )
    return findings


def _cross_border_findings(
    io: EmbeddedSqlIO, sovereignty: Mapping[str, Any], blocking: str
) -> List[_Finding]:
    """The validator's check 4 across the inputs and the landing: one jurisdiction."""
    from fluid_build.policy.sovereignty import (
        DEFAULT_CROSS_BORDER_TRANSFER,
        DEFAULT_DATA_RESIDENCY,
    )

    residency = sovereignty.get("dataResidency", DEFAULT_DATA_RESIDENCY)
    if not residency or sovereignty.get("crossBorderTransfer", DEFAULT_CROSS_BORDER_TRANSFER):
        return []
    bq = io.bigquery_landing
    land_at = bq.location if bq is not None else (io.landing.region if io.landing else None)
    if land_at is None:
        if io.landing is None:
            return []  # a local file: no cloud location to compare
        return [
            (
                blocking,
                f"the result lands at {io.landing.uri}, whose binding names no region, so a "
                "BigQuery input's transfer to it cannot be checked",
            )
        ]
    land_j = _jurisdiction(land_at)
    findings: List[_Finding] = []
    for r in (r for r in io.inputs if r.region):
        read_j = _jurisdiction(str(r.region))
        entry = _entry(r.product_id, r.expose_id)
        if "Unknown" in (land_j, read_j):
            findings.append(
                (
                    "warning",
                    f"{entry} is read from {r.region} and the result lands in {land_at}; one "
                    "has no known jurisdiction, so the transfer cannot be verified",
                )
            )
        elif read_j != land_j:
            findings.append(
                (
                    blocking,
                    f"{entry} is read from {r.region} ({read_j}) and the result lands in "
                    f"{land_at} ({land_j}), and crossBorderTransfer is false",
                )
            )
    return findings


def plan_embedded_sql_io(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    contract_dir: Path,
    *,
    env: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> EmbeddedSqlIO:
    """Decide what the build reads and where it lands, refusing what it must not do.

    ``contract`` is the build's contract BEFORE ``{{ env.* }}`` resolution (see
    :func:`object_store_landing`); ``build`` may be either.
    """
    refuse_unapplied_masking(contract, build)
    warnings = further_outputs(contract, build)
    bq_landing = bigquery_landing(contract, build)
    landing = object_store_landing(contract, build)
    bound = _bind_consumes(contract, build, contract_dir, env=env, logger=logger)
    io = EmbeddedSqlIO(
        inputs=bound.resolved,
        covered=bound.covered,
        lineage_only=bound.lineage_only,
        landing=landing,
        workspace_root=bound.workspace_root,
        bigquery_landing=bq_landing,
        warnings=warnings,
    )
    # Before anything is read or staged: both are about what the build would
    # write where, which is known from the plan alone.
    refuse_landing_into_input(io)
    io.warnings.extend(refuse_sovereignty_breach(contract, io))
    return io


__all__ = [
    "BigQueryLanding",
    "ConsumesResolutionError",
    "CoveredInput",
    "EmbeddedSqlIO",
    "EmbeddedSqlLandingError",
    "EmbeddedSqlSovereigntyError",
    "FILE_FORMATS",
    "Landing",
    "LineageOnlyInput",
    "MaskingNotAppliedError",
    "ResolvedInput",
    "UnreadableBindingError",
    "bigquery_landing",
    "bigquery_staging_dir",
    "explicit_input_names",
    "further_outputs",
    "load_bigquery_landing",
    "object_store_landing",
    "plan_embedded_sql_io",
    "refuse_landing_into_input",
    "refuse_sovereignty_breach",
    "refuse_unapplied_masking",
    "refuse_unlandable_first_expose",
    "relations_read",
    "remove_staged",
    "resolve_consumes",
    "stage_bigquery_io",
    "write_bigquery_run_record",
]
