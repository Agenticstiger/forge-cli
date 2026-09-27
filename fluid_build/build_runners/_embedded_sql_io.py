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
   (``util.binding_paths.resolve_binding_path``); an AWS object-store binding
   through the duckdb acquisition runner's own key rule
   (``_object_store_uri``), read as the glob of that prefix's files of the
   binding format, which is also what the Glue table ``fluid apply`` declares
   for the binding serves. Anything this engine cannot read (a warehouse
   table, a stream, a GCS or Azure prefix) is an :class:`UnreadableBindingError`
   naming the platform.

Each entry becomes one DuckDB view named by its ``exposeId``, a quoted
identifier that must pass ``validate_ident``. An explicit
``builds[].properties.parameters.inputs`` entry of the same name WINS and the
entry is not resolved at all: that is how a federated upstream, or one this
engine cannot read, is still bound. An entry that is neither resolved nor
covered fails the build before any SQL runs.

**Lands.** When the first expose's binding is an AWS object-store binding, the
result is written to exactly the object the acquisition runner writes for that
binding (``_object_store_uri`` + ``_file_within_prefix``), inside the prefix
the Glue table points at, so ``fluid verify --env aws`` counts it. A local
binding is unchanged. An expose declaring ``policy.privacy.masking`` is
refused: this path does not apply masking, and cleartext must not land
silently.

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

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build._errors import FluidUserError, doc_url
from fluid_build.util.binding_paths import (
    ENV_PLACEHOLDER_RE,
    is_remote_uri,
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

    @property
    def view(self) -> str:
        """The DuckDB view the SQL reads it through."""
        return self.expose_id

    def as_input_spec(self) -> Dict[str, Any]:
        """The local provider's input spec (``_register_inputs``) for this entry."""
        spec: Dict[str, Any] = {
            "table": self.view,
            "path": self.uri,
            "format": self.format,
            "quoted": True,
            "productId": self.product_id,
            "exposeId": self.expose_id,
        }
        if self.region:
            spec["region"] = self.region
        return spec

    def record(self) -> Dict[str, str]:
        return {"productId": self.product_id, "exposeId": self.expose_id, "uri": self.uri}


@dataclass(frozen=True)
class CoveredInput:
    """A ``consumes[]`` entry bound by an explicit input of the same name."""

    product_id: str
    expose_id: str
    input_name: str


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


@dataclass
class EmbeddedSqlIO:
    """Everything :func:`plan_embedded_sql_io` decided, before any SQL runs."""

    inputs: List[ResolvedInput] = field(default_factory=list)
    covered: List[CoveredInput] = field(default_factory=list)
    landing: Optional[Landing] = None
    workspace_root: Optional[Path] = None


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


def _read_uri(path: str, platform: str, fmt: str, where: str) -> Tuple[str, str, str]:
    """A binding whose ``location.path`` is already a URI: only ``s3://`` reads."""
    from .duckdb.runner import _FILE_FORMAT_EXT

    scheme = path.split("://", 1)[0].lower()
    fmt = fmt or _format_from_suffix(path) or "parquet"
    if scheme != "s3" or fmt not in FILE_FORMATS:
        raise _unreadable(where, platform or scheme, fmt, f"{scheme}:// location")
    uri = f"{path}*.{_FILE_FORMAT_EXT[fmt]}" if path.endswith("/") else path
    return uri, fmt, "aws"


def _read_local(
    path: Optional[str], fmt: str, upstream_path: Path, where: str
) -> Tuple[str, str, str]:
    """A local binding: its path, anchored at the UPSTREAM contract's directory."""
    from .duckdb.runner import _FILE_FORMAT_EXT

    if not path:
        raise ConsumesResolutionError(
            what=f"{where}: the upstream expose binds no location.path",
            why=f"{upstream_path} declares the expose with a local binding that names no file.",
            fix="Give the upstream expose a binding.location.path, the file its build writes.",
            doc=doc_url(),
        )
    local = str(resolve_binding_path(path, upstream_path.parent))
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


def _read_location(
    expose: Mapping[str, Any], upstream_path: Path, where: str
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
        return (*_read_local(path, fmt, upstream_path, where), None)
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
    entries: Sequence[Mapping[str, Any]], explicit: Mapping[str, str]
) -> Tuple[List[CoveredInput], List[Tuple[str, str]]]:
    """``(covered, pending)``: the entries an explicit input binds, and the rest."""
    covered: List[CoveredInput] = []
    pending: List[Tuple[str, str]] = []
    views: Dict[str, str] = {}
    for entry in entries:
        product_id, expose_id = _entry_ids(entry)
        if expose_id.casefold() in explicit:
            covered.append(CoveredInput(product_id, expose_id, explicit[expose_id.casefold()]))
            continue
        _claim_view(product_id, expose_id, views)
        pending.append((product_id, expose_id))
    return covered, pending


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
) -> Mapping[str, Any]:
    """The upstream's expose ``expose_id``, as this run's ``env`` overlay leaves it."""
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
    return expose


def resolve_consumes(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    contract_dir: Path,
    *,
    env: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[List[ResolvedInput], List[CoveredInput], Optional[Path]]:
    """Bind every ``consumes[]`` entry, or raise :class:`ConsumesResolutionError`.

    Returns ``(resolved, covered, workspace_root)``. An entry whose ``exposeId``
    names an explicit ``builds[].properties.parameters.inputs`` entry is
    COVERED: the explicit input wins on the name collision and the entry is
    not resolved at all. Every other entry must resolve; the first that does
    not raises, naming its productId and where it looked, so the SQL never
    runs with an input missing.
    """
    from fluid_build.util.upstream_discovery import index_contract_paths

    entries = [c for c in contract.get("consumes") or [] if isinstance(c, Mapping)]
    if not entries:
        return [], [], None
    env = _validated_env(env)
    covered, pending = _split_covered(entries, explicit_input_names(build))
    if not pending:
        return [], covered, None

    root, roots = _search_roots(contract_dir)
    index, skipped = index_contract_paths(roots)
    ws = _Workspace(Path(contract_dir), root, roots, index, skipped)
    resolved: List[ResolvedInput] = []
    for product_id, expose_id in pending:
        upstream_path = _upstream_contract(ws, product_id, expose_id)
        expose = _upstream_expose(upstream_path, product_id, expose_id, env, logger or LOG)
        uri, fmt, platform, region = _read_location(
            expose, upstream_path, _entry(product_id, expose_id)
        )
        resolved.append(
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
    return resolved, covered, root


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
    """
    from .duckdb.runner import _file_within_prefix, _object_store_uri

    exposes = [e for e in contract.get("exposes") or [] if isinstance(e, Mapping)]
    if not exposes:
        return None
    expose = exposes[0]
    binding = expose.get("binding") if isinstance(expose.get("binding"), Mapping) else {}
    raw_loc = binding.get("location")
    loc: Mapping[str, Any] = raw_loc if isinstance(raw_loc, Mapping) else {}
    raw_path = loc.get("path")
    if not raw_path:
        return None
    platform = str(binding.get("platform") or "").strip().lower()
    names_bucket = platform == "aws" and bool(loc.get("bucket"))
    if not names_bucket and not is_remote_uri(str(raw_path)):
        return None  # a local binding: unchanged

    expose_id = str(expose.get("exposeId") or expose.get("id") or "result")
    where = f"expose {expose_id}"
    path = str(_resolve_env(raw_path, field_name="location.path", where=where))
    resolved_loc: Dict[str, Any] = {
        **dict(loc),
        "path": path,
        "table": _resolve_env(loc.get("table"), field_name="location.table", where=where),
    }
    region = _resolve_env(loc.get("region"), field_name="location.region", where=where)
    uri: Optional[str]
    if is_remote_uri(path):
        uri = path if path.lower().startswith("s3://") else None
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
    landing = object_store_landing(contract, build)
    inputs, covered, workspace_root = resolve_consumes(
        contract, build, contract_dir, env=env, logger=logger
    )
    return EmbeddedSqlIO(
        inputs=inputs, covered=covered, landing=landing, workspace_root=workspace_root
    )


__all__ = [
    "ConsumesResolutionError",
    "CoveredInput",
    "EmbeddedSqlIO",
    "EmbeddedSqlLandingError",
    "FILE_FORMATS",
    "Landing",
    "MaskingNotAppliedError",
    "ResolvedInput",
    "UnreadableBindingError",
    "explicit_input_names",
    "object_store_landing",
    "plan_embedded_sql_io",
    "refuse_unapplied_masking",
    "resolve_consumes",
]
