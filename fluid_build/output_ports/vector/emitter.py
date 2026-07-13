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

"""pgvector vector / embeddings output-port emitter.

A **pure function** that compiles a FLUID contract into a pgvector RAG
target: the embeddings-table + ANN-index DDL for a data product's
``ai-embeddable`` columns, plus a provenance manifest a retrieval
pipeline needs. No credentials, no network — the mirror of the confluent
IaC plugin's contract-in / artefact-out shape.

Design borrows (see the PR's borrow-before-build receipts):

* **pgvector README** — the exact DDL grammar (``CREATE EXTENSION
  vector``; ``embedding vector(<dim>)``; ``USING hnsw (embedding
  vector_cosine_ops)``) and the distance→operator-class mapping
  (cosine→``vector_cosine_ops`` / l2→``vector_l2_ops`` /
  inner_product→``vector_ip_ops`` / l1→``vector_l1_ops``).
* **Airbyte's pgvector destination** — the config surface: an embedding
  model + dimensions, the text fields to embed (here, the
  ``ai-embeddable`` columns), and metadata/provenance for filtering.
* **In-repo prior art** — the ai_ready agent
  (:mod:`fluid_build.copilot.agents.ai_ready_agent`) that labels
  embedding-friendly text columns ``ai-embeddable: "true"`` (this port
  *consumes* that label), the confluent IaC plugin's
  ``validate_*_binding`` / ``_*_exposures`` structure, and the central
  SQL-safety helpers (:mod:`fluid_build.providers._sql_safety`) that
  every emitted identifier routes through.

The embeddings table is a standard one-row-per-chunk RAG shape::

    CREATE TABLE <expose>_embeddings (
        id            bigserial PRIMARY KEY,
        source_key    text,             -- ties a chunk back to its source row
        source_column text NOT NULL,    -- which ai-embeddable column it embeds
        content       text NOT NULL,    -- the embedded text
        embedding     vector(<dim>),
        embedding_model text,
        chunk_index   integer NOT NULL DEFAULT 0,
        created_at    timestamptz NOT NULL DEFAULT now()
    );
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from fluid_build.iac.naming import safe_ident
from fluid_build.providers._sql_safety import validate_ident

__all__ = [
    "VECTOR_PLATFORM",
    "AI_EMBEDDABLE_LABEL",
    "VectorTarget",
    "VectorPortArtifacts",
    "iter_vector_exposes",
    "embeddable_columns",
    "resolve_table_name",
    "compile_pgvector_ddl",
    "build_rag_manifest",
    "compile_vector_port",
    "validate_vector_binding",
]

#: ``binding.platform`` token that selects the vector output port.
VECTOR_PLATFORM = "pgvector"

#: Column label the ai_ready agent stamps on embedding-friendly text columns.
AI_EMBEDDABLE_LABEL = "ai-embeddable"

# Distance metric -> pgvector operator-class *suffix*. Prefixed with the
# vector type (``vector`` / ``halfvec``) at emit time so a halfvec column
# gets ``halfvec_cosine_ops``. Fixed table — never contract-derived — so the
# operator class can never carry injected SQL.
_METRIC_OPS_SUFFIX: Dict[str, str] = {
    "cosine": "cosine_ops",
    "l2": "l2_ops",
    "inner_product": "ip_ops",
    "l1": "l1_ops",
}

_VECTOR_TYPES = frozenset({"vector", "halfvec"})
_INDEX_TYPES = frozenset({"hnsw", "ivfflat", "none"})

# pgvector defaults (README). Applied when the contract omits tuning.
_HNSW_DEFAULT_M = 16
_HNSW_DEFAULT_EF_CONSTRUCTION = 64
_IVFFLAT_DEFAULT_LISTS = 100


@dataclass
class VectorTarget:
    """One resolved pgvector target — the emit unit for a single expose."""

    expose_id: str
    product: str
    table: str
    dimensions: int
    vector_type: str
    index_type: str
    distance_metric: str
    embedding_model: str
    source_key_column: str
    source_table: str
    embeddable_columns: List[Dict[str, Any]] = field(default_factory=list)
    hnsw_opts: Dict[str, Any] = field(default_factory=dict)
    ivfflat_opts: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorPortArtifacts:
    """Bundled emit output: the DDL string, the RAG manifest, the targets."""

    ddl: str
    manifest: Dict[str, Any]
    targets: List[VectorTarget]


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _comment_safe(value: Any) -> str:
    """Flatten a value for safe interpolation into a SQL ``--`` line comment.

    The DDL body only ever interpolates validated identifiers + ints, but the
    generated ``-- ...`` header lines echo contract-derived free text (column
    names — which carry no schema pattern — the embedding-model string, the
    product id). A newline in any of those would terminate the ``--`` comment
    and let the rest land as executable SQL, so every control character
    (newline / carriage-return / tab / etc.) collapses to a single space here.
    Closes the only comment-breakout injection path in the emitter.
    """
    text = value if isinstance(value, str) else str(value)
    return "".join(ch if (ch >= " " and ch != "\x7f") else " " for ch in text)


def iter_vector_exposes(
    contract: Mapping[str, Any],
) -> Iterable[Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]]:
    """Yield ``(expose, binding, vectorConfig)`` for every pgvector-bound expose."""
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, dict):
            continue
        binding = expose.get("binding") or {}
        if not isinstance(binding, dict):
            continue
        if _as_str(binding.get("platform")).lower() != VECTOR_PLATFORM:
            continue
        vcfg = binding.get("vectorConfig")
        yield expose, binding, (vcfg if isinstance(vcfg, dict) else {})


def embeddable_columns(expose: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return the expose's ``ai-embeddable``-labelled columns, order preserved.

    Consumes the label the ai_ready agent (PR #405) stamps. A column whose
    ``labels["ai-embeddable"]`` is a truthy string ("true"/"1"/"yes") is an
    embedding target; every other column is skipped.
    """
    contract = expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, dict) else None
    out: List[Dict[str, Any]] = []
    for col in schema if isinstance(schema, list) else []:
        if not isinstance(col, dict):
            continue
        labels = col.get("labels")
        if not isinstance(labels, dict):
            continue
        if _as_str(labels.get(AI_EMBEDDABLE_LABEL)).lower() in {"true", "1", "yes"}:
            out.append(col)
    return out


def resolve_table_name(vcfg: Mapping[str, Any], expose_id: str) -> str:
    """Return the injection-safe embeddings-table name for an expose.

    An explicit ``vectorConfig.table`` is validated strictly via
    :func:`validate_ident` (raising ``ValueError`` on anything that is not a
    bare SQL identifier — the injection boundary). Otherwise the default
    ``<expose>_embeddings`` is normalised through :func:`safe_ident` (dots /
    hyphens in an exposeId become ``_``) so it is always a valid identifier.
    """
    explicit = _as_str(vcfg.get("table"))
    if explicit:
        return validate_ident(explicit)
    return validate_ident(f"{safe_ident(expose_id)}_embeddings")


def _resolve_target(
    expose: Mapping[str, Any],
    binding: Mapping[str, Any],
    vcfg: Mapping[str, Any],
    product: str,
) -> VectorTarget:
    expose_id = _as_str(expose.get("exposeId")) or _as_str(expose.get("id")) or "expose"
    vector_type = _as_str(vcfg.get("vectorType")).lower() or "vector"
    if vector_type not in _VECTOR_TYPES:
        vector_type = "vector"
    index_type = _as_str(vcfg.get("indexType")).lower() or "hnsw"
    if index_type not in _INDEX_TYPES:
        index_type = "hnsw"
    metric = _as_str(vcfg.get("distanceMetric")).lower() or "cosine"
    if metric not in _METRIC_OPS_SUFFIX:
        metric = "cosine"
    location = binding.get("location") or {}
    source_table = _as_str(location.get("table")) if isinstance(location, dict) else ""
    return VectorTarget(
        expose_id=expose_id,
        product=product,
        table=resolve_table_name(vcfg, expose_id),
        dimensions=int(vcfg.get("dimensions") or 0),
        vector_type=vector_type,
        index_type=index_type,
        distance_metric=metric,
        embedding_model=_as_str(vcfg.get("embeddingModel")),
        source_key_column=_as_str(vcfg.get("sourceKeyColumn")),
        source_table=source_table,
        embeddable_columns=[
            {
                "name": _as_str(col.get("name")),
                "type": _as_str(col.get("type")),
                "description": _as_str(col.get("description")),
            }
            for col in embeddable_columns(expose)
        ],
        hnsw_opts=vcfg.get("hnsw") if isinstance(vcfg.get("hnsw"), dict) else {},
        ivfflat_opts=vcfg.get("ivfflat") if isinstance(vcfg.get("ivfflat"), dict) else {},
    )


def _positive_int(value: Any, default: int) -> int:
    """Coerce a tuning value to a positive int, falling back to ``default``.

    Guards the DDL: an int can never carry injected SQL, and clamping to a
    positive value keeps ``WITH (m = ...)`` well-formed for any partial dict.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _table_ddl(target: VectorTarget) -> str:
    table = validate_ident(target.table)  # defence-in-depth (already validated)
    col_type = f"{target.vector_type}({int(target.dimensions)})"
    cols = [
        "    id bigserial PRIMARY KEY",
        "    source_key text",
        "    source_column text NOT NULL",
        "    content text NOT NULL",
        f"    embedding {col_type}",
        "    embedding_model text",
        "    chunk_index integer NOT NULL DEFAULT 0",
        "    created_at timestamptz NOT NULL DEFAULT now()",
    ]
    return f'CREATE TABLE IF NOT EXISTS "{table}" (\n' + ",\n".join(cols) + "\n);"


def _index_ddl(target: VectorTarget) -> str:
    if target.index_type == "none":
        return ""
    table = validate_ident(target.table)
    ops = f"{target.vector_type}_{_METRIC_OPS_SUFFIX[target.distance_metric]}"
    index_name = validate_ident(f"{table}_embedding_idx")
    if target.index_type == "hnsw":
        m = _positive_int(target.hnsw_opts.get("m"), _HNSW_DEFAULT_M)
        efc = _positive_int(target.hnsw_opts.get("efConstruction"), _HNSW_DEFAULT_EF_CONSTRUCTION)
        opts = f"WITH (m = {m}, ef_construction = {efc})"
        using = f"hnsw (embedding {ops})"
    else:  # ivfflat
        lists = _positive_int(target.ivfflat_opts.get("lists"), _IVFFLAT_DEFAULT_LISTS)
        opts = f"WITH (lists = {lists})"
        using = f"ivfflat (embedding {ops})"
    return f'CREATE INDEX IF NOT EXISTS "{index_name}"\n' f'    ON "{table}" USING {using} {opts};'


def compile_pgvector_ddl(contract: Mapping[str, Any]) -> str:
    """Compile the pgvector DDL for every pgvector-bound expose in *contract*.

    Emits (once) ``CREATE EXTENSION IF NOT EXISTS vector`` then, per expose, a
    ``CREATE TABLE`` embeddings target and its ANN index. Returns ``""`` when
    the contract has no pgvector expose. Raises ``ValueError`` if a table name
    is not a bare SQL identifier (the injection boundary).
    """
    product = _as_str(contract.get("id")) or _as_str(contract.get("name")) or "product"
    blocks: List[str] = []
    for expose, binding, vcfg in iter_vector_exposes(contract):
        target = _resolve_target(expose, binding, vcfg, product)
        cols = ", ".join(_comment_safe(c["name"]) for c in target.embeddable_columns) or "(none)"
        header = (
            f"-- expose: {_comment_safe(target.expose_id)}  "
            f"(embeddable column(s): {cols})\n"
            f"-- model: {_comment_safe(target.embedding_model) or '(unset)'}  "
            f"dimensions: {int(target.dimensions)}  metric: {target.distance_metric}"
        )
        parts = [header, _table_ddl(target)]
        index_ddl = _index_ddl(target)
        if index_ddl:
            parts.append(index_ddl)
        blocks.append("\n".join(parts))
    if not blocks:
        return ""
    preamble = (
        "-- FLUID vector output port (pgvector) — generated, do not edit by hand.\n"
        f"-- product: {_comment_safe(product)}\n"
        "CREATE EXTENSION IF NOT EXISTS vector;"
    )
    return preamble + "\n\n" + "\n\n".join(blocks) + "\n"


def build_rag_manifest(contract: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the RAG provenance manifest for every pgvector-bound expose.

    The JSON a retrieval / ingestion pipeline consumes: which source columns
    to embed, with which model + dimensions + distance metric, into which
    table, and how to link a chunk back to its source row.
    """
    product = _as_str(contract.get("id")) or _as_str(contract.get("name")) or "product"
    targets: List[Dict[str, Any]] = []
    for expose, binding, vcfg in iter_vector_exposes(contract):
        target = _resolve_target(expose, binding, vcfg, product)
        targets.append(
            {
                "product": product,
                "expose": target.expose_id,
                "table": target.table,
                "vectorStore": VECTOR_PLATFORM,
                "vectorType": target.vector_type,
                "dimensions": target.dimensions,
                "embeddingModel": target.embedding_model,
                "distanceMetric": target.distance_metric,
                "indexType": target.index_type,
                "sourceKeyColumn": target.source_key_column,
                "sourceTable": target.source_table,
                "embeddableColumns": target.embeddable_columns,
            }
        )
    return {
        "version": "1",
        "generator": "fluid vector output port",
        "vectorStore": VECTOR_PLATFORM,
        "product": product,
        "targets": targets,
    }


def compile_vector_port(contract: Mapping[str, Any]) -> VectorPortArtifacts:
    """Bundle the DDL, the RAG manifest, and the resolved targets."""
    product = _as_str(contract.get("id")) or _as_str(contract.get("name")) or "product"
    targets = [
        _resolve_target(expose, binding, vcfg, product)
        for expose, binding, vcfg in iter_vector_exposes(contract)
    ]
    return VectorPortArtifacts(
        ddl=compile_pgvector_ddl(contract),
        manifest=build_rag_manifest(contract),
        targets=targets,
    )


def validate_vector_binding(
    contract: Mapping[str, Any],
) -> Tuple[List[str], List[str]]:
    """Plan-time gate for pgvector-bound exposes (anti-no-op, mirrors confluent).

    Surfaces a clean error at validate time instead of emitting an incomplete
    or colliding target at apply time. Returns ``(errors, warnings)``.

    Errors (hard): missing ``dimensions``; a table-name collision between two
    exposes. Warnings (soft): an expose with no ``ai-embeddable`` column
    (nothing to embed); a missing ``embeddingModel`` / ``sourceKeyColumn``
    (incomplete RAG provenance — retrieval still works, lineage is degraded).
    """
    errors: List[str] = []
    warnings: List[str] = []
    tables: Dict[str, str] = {}
    for expose, binding, vcfg in iter_vector_exposes(contract):
        eid = _as_str(expose.get("exposeId")) or "?"

        dims = vcfg.get("dimensions")
        if not isinstance(dims, int) or isinstance(dims, bool) or dims < 1:
            errors.append(
                f"expose '{eid}': platform=pgvector requires "
                f"binding.vectorConfig.dimensions (a positive integer), got {dims!r}"
            )

        if not embeddable_columns(expose):
            warnings.append(
                f"expose '{eid}': no ai-embeddable column in the schema — the vector "
                f"output port has nothing to embed. Label text columns with "
                f"labels.ai-embeddable='true' (the ai_ready agent does this)."
            )

        if not _as_str(vcfg.get("embeddingModel")):
            warnings.append(
                f"expose '{eid}': no binding.vectorConfig.embeddingModel — the RAG "
                f"manifest can't record which model produced the vectors (model-parity check lost)."
            )
        if not _as_str(vcfg.get("sourceKeyColumn")):
            warnings.append(
                f"expose '{eid}': no binding.vectorConfig.sourceKeyColumn — an embedding "
                f"chunk can't be tied back to its source row (update/delete propagation lost)."
            )

        try:
            table = resolve_table_name(vcfg, eid)
        except ValueError as exc:
            errors.append(f"expose '{eid}': invalid binding.vectorConfig.table — {exc}")
            continue
        if table in tables and tables[table] != eid:
            errors.append(
                f"expose '{eid}': resolves to the same embeddings table {table!r} as "
                f"expose '{tables[table]}' — set a distinct binding.vectorConfig.table"
            )
        else:
            tables.setdefault(table, eid)

    return errors, warnings
