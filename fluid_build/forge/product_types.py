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

"""Type-aware framework for FLUID data products.

Single source of truth for the **Data Mesh productType ↔ medallion layer**
equivalence axiom and for the per-type behaviour every authoring path
shares (forge --ai, forge --blank, init --template, forge --refine,
from_data_products).

Adding a new product type is one row in ``PRODUCT_TYPES``. Adding a new
acquisition engine is one row in ``ACQUISITION_ENGINES``. The
``shape_contract`` builder is pure so every authoring path that lands
on the same ``ProductTypeAnswer`` produces a byte-identical contract
(invariant **I2**).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductType:
    """One row in the canonical Data Mesh / medallion registry.

    A ``ProductType`` carries every facet the authoring stack needs for
    one type:

    * naming (``code`` / ``layer`` / ``aliases``) — accept whatever the
      user types, normalise to the canonical pair;
    * engine selection (``engine_class`` / ``default_engine``) — pick
      the right runner family without hand-rolled if/elif;
    * AI behaviour (``interview_question_keys`` / ``prompt_hint``) —
      shape what the copilot asks and how it reasons;
    * composition (``allowed_upstream_types``) — enforce the data-mesh
      composition rules at validation time.
    """

    code: str  # SDP / ADP / CDP
    layer: str  # Bronze / Silver / Gold
    engine_class: str  # acquisition | transformation
    default_engine: str
    description: str
    aliases: FrozenSet[str] = frozenset()
    interview_question_keys: Tuple[str, ...] = ()
    prompt_hint: str = ""
    allowed_upstream_types: FrozenSet[str] = frozenset()

    @property
    def canonical_names(self) -> Tuple[str, str]:
        """Both canonical aliases (``code``, ``layer``)."""
        return (self.code, self.layer)


PRODUCT_TYPES: Tuple[ProductType, ...] = (
    ProductType(
        code="SDP",
        layer="Bronze",
        engine_class="acquisition",
        default_engine="duckdb",
        description="Source-aligned data product (raw acquisition from an upstream system)",
        aliases=frozenset({"source-aligned", "source_aligned", "raw", "ingest"}),
        interview_question_keys=("source_kind", "source_uri", "schedule"),
        prompt_hint=(
            "This is a Source-Aligned Data Product (SDP / Bronze). "
            "Goal: faithful, auditable acquisition of raw data from one upstream "
            "system. Prefer schema preservation over transformation. The build "
            "section uses pattern='acquisition' and an acquisition engine; do "
            "NOT add joins, derived columns, or business logic."
        ),
        allowed_upstream_types=frozenset(),
    ),
    ProductType(
        code="ADP",
        layer="Silver",
        engine_class="transformation",
        default_engine="dbt",
        description="Aggregated data product (joined / cleaned / conformed across SDPs)",
        aliases=frozenset({"aggregated", "conformed", "cleaned", "integrated"}),
        interview_question_keys=("upstream_products", "join_keys", "grain"),
        prompt_hint=(
            "This is an Aggregated Data Product (ADP / Silver). "
            "Goal: clean, conformed, joined views over upstream Source-Aligned "
            "Products (and optionally other ADPs). Emphasise grain, join keys, "
            "and data quality. Use pattern='declarative' or 'embedded-logic' "
            "with a transformation engine."
        ),
        allowed_upstream_types=frozenset({"SDP", "ADP"}),
    ),
    ProductType(
        code="CDP",
        layer="Gold",
        engine_class="transformation",
        default_engine="dbt",
        description="Consumption-aligned data product (analytics / serving / metrics)",
        aliases=frozenset({"consumption-aligned", "consumption", "serving", "marts", "metrics"}),
        interview_question_keys=("consumers", "metrics", "sla"),
        prompt_hint=(
            "This is a Consumption-Aligned Data Product (CDP / Gold). "
            "Goal: fit-for-purpose marts shaped for known consumers (BI, ML "
            "training, application APIs). Emphasise consumption shape, metric "
            "definitions, semantics, and SLA. Compose from ADPs (and SDPs only "
            "when an ADP would be ceremony). CDP-from-CDP is also allowed "
            "— executive dashboards routinely build on top of mart CDPs."
        ),
        # CDP accepts SDP, ADP, AND CDP. The earlier ``frozenset({"SDP",
        # "ADP"})`` rejected CDP-from-CDP, but in practice gold-on-gold
        # composition is common: a customer-360 CDP feeds an
        # executive-dashboard CDP, a metrics-mart CDP feeds an
        # ML-feature CDP, etc. Only SDP rejects upstreams (it's the
        # raw-acquisition tier and has no upstream by definition).
        allowed_upstream_types=frozenset({"SDP", "ADP", "CDP"}),
    ),
)


# Bronze↔SDP, Silver↔ADP, Gold↔CDP — derived from PRODUCT_TYPES so a new
# row in the registry extends every consumer automatically. ``Platinum``
# and ``Logical`` are valid layer labels with no Data Mesh productType
# analogue — Platinum carries the legacy "platinum/curated" semantic;
# Logical is the modeling-only contract emitted by forge data-model.
LAYER_TO_PRODUCT_TYPE: Dict[str, str] = {pt.layer: pt.code for pt in PRODUCT_TYPES}
PRODUCT_TYPE_TO_LAYER: Dict[str, str] = {pt.code: pt.layer for pt in PRODUCT_TYPES}
VALID_LAYERS: FrozenSet[str] = frozenset(
    {pt.layer for pt in PRODUCT_TYPES} | {"Platinum", "Logical"}
)
VALID_PRODUCT_TYPES: FrozenSet[str] = frozenset({pt.code for pt in PRODUCT_TYPES})


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def get_product_type(name: str) -> Optional[ProductType]:
    """Look up a :class:`ProductType` by code, layer, or alias.

    Case-insensitive. Returns ``None`` if no match. Layer ``Platinum``
    has no Data Mesh code so it returns ``None`` here even though it is
    a valid layer for ``metadata.layer``.
    """
    if not isinstance(name, str):
        return None
    needle = name.strip().lower()
    if not needle:
        return None
    for pt in PRODUCT_TYPES:
        if needle == pt.code.lower() or needle == pt.layer.lower():
            return pt
        if any(needle == alias.lower() for alias in pt.aliases):
            return pt
    return None


# ---------------------------------------------------------------------------
# Normalisation — the equivalence axiom in one place
# ---------------------------------------------------------------------------


class ProductTypeError(ValueError):
    """Raised when ``metadata.layer`` and ``metadata.productType`` disagree.

    Carries the offending pair so callers can render specific guidance.
    """

    def __init__(
        self, message: str, *, layer: Optional[str] = None, product_type: Optional[str] = None
    ):
        super().__init__(message)
        self.layer = layer
        self.product_type = product_type


def normalize_metadata_in_place(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Fill the missing twin (layer ↔ productType) from the registry.

    After this call, both fields are populated and consistent — or the
    function raised :class:`ProductTypeError` because the user-supplied
    pair disagrees with the canonical mapping.

    Pass-through for ``Platinum`` (no productType analogue) and for
    metadata that has neither field set.
    """
    if not isinstance(metadata, dict):
        return metadata

    layer = metadata.get("layer")
    product_type = metadata.get("productType")

    if layer is not None and not isinstance(layer, str):
        raise ProductTypeError(
            f"metadata.layer must be a string, got {type(layer).__name__}",
            layer=str(layer),
            product_type=product_type,
        )
    if product_type is not None and not isinstance(product_type, str):
        raise ProductTypeError(
            f"metadata.productType must be a string, got {type(product_type).__name__}",
            layer=layer,
            product_type=str(product_type),
        )

    if layer and layer not in VALID_LAYERS:
        raise ProductTypeError(
            f"metadata.layer={layer!r} is not one of {sorted(VALID_LAYERS)}",
            layer=layer,
            product_type=product_type,
        )
    if product_type and product_type not in VALID_PRODUCT_TYPES:
        raise ProductTypeError(
            f"metadata.productType={product_type!r} is not one of "
            f"{sorted(VALID_PRODUCT_TYPES)} (SDP=Source-Aligned, "
            "ADP=Aggregated, CDP=Consumption-Aligned)",
            layer=layer,
            product_type=product_type,
        )

    if layer and product_type:
        expected = LAYER_TO_PRODUCT_TYPE.get(layer)
        if expected is None:
            raise ProductTypeError(
                f"metadata.layer={layer!r} has no Data Mesh productType "
                "analogue; either omit metadata.productType or use "
                "Bronze/Silver/Gold.",
                layer=layer,
                product_type=product_type,
            )
        if expected != product_type:
            raise ProductTypeError(
                f"metadata.layer={layer!r} and metadata.productType={product_type!r} "
                "are inconsistent. Canonical mapping: Bronze↔SDP, Silver↔ADP, Gold↔CDP.",
                layer=layer,
                product_type=product_type,
            )
        return metadata

    if layer and not product_type:
        twin = LAYER_TO_PRODUCT_TYPE.get(layer)
        if twin is not None:  # Platinum has no twin → no-op
            metadata["productType"] = twin
        return metadata

    if product_type and not layer:
        metadata["layer"] = PRODUCT_TYPE_TO_LAYER[product_type]
        return metadata

    return metadata


# ---------------------------------------------------------------------------
# Acquisition engine selection (capability-catalog query, not if/elif)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcquisitionEngine:
    """One row in the acquisition-engine capability catalog.

    Used by :func:`select_acquisition_engine` to answer questions like
    *"engines that handle CDC + streaming"*. Adding a new engine is one
    row here, no edits to consumers.
    """

    name: str
    capabilities: FrozenSet[str]
    schemes: FrozenSet[str]
    kinds: FrozenSet[str]
    description: str = ""


# Order matters only as a tiebreaker — first match wins when multiple
# engines satisfy the same capability set.
ACQUISITION_ENGINES: Tuple[AcquisitionEngine, ...] = (
    AcquisitionEngine(
        name="duckdb",
        capabilities=frozenset({"batch", "file", "embedded"}),
        schemes=frozenset({"file", "s3", "gs", "az"}),
        kinds=frozenset({"file", "csv", "parquet", "json", "duckdb"}),
        description="Local / embedded batch acquisition (default for SDP).",
    ),
    AcquisitionEngine(
        name="dlt",
        capabilities=frozenset({"batch", "rest", "api", "incremental"}),
        schemes=frozenset({"http", "https"}),
        kinds=frozenset({"rest", "api", "graphql", "custom"}),
        description="LLM-native Python source generator (great for custom REST/GraphQL).",
    ),
    AcquisitionEngine(
        name="airbyte",
        capabilities=frozenset({"batch", "managed_connectors", "saas"}),
        schemes=frozenset({"airbyte"}),
        kinds=frozenset({"airbyte", "saas", "salesforce", "stripe", "hubspot"}),
        description="Managed connector catalog for SaaS sources.",
    ),
    AcquisitionEngine(
        name="meltano",
        capabilities=frozenset({"batch", "singer", "open_source_connectors"}),
        schemes=frozenset({"meltano", "singer"}),
        kinds=frozenset({"singer", "tap", "meltano"}),
        description="Singer/tap-based open-source acquisition.",
    ),
    AcquisitionEngine(
        name="kafka_connect",
        capabilities=frozenset({"streaming", "kafka"}),
        schemes=frozenset({"kafka"}),
        kinds=frozenset({"kafka", "kafka_connect"}),
        description="Streaming acquisition via Kafka Connect.",
    ),
    AcquisitionEngine(
        name="debezium",
        capabilities=frozenset({"streaming", "cdc", "kafka"}),
        schemes=frozenset({"debezium"}),
        kinds=frozenset({"debezium", "cdc", "postgres-cdc", "mysql-cdc"}),
        description="Change-data-capture from transactional databases.",
    ),
)


def list_acquisition_engines(
    *, capabilities: Optional[FrozenSet[str]] = None
) -> List[AcquisitionEngine]:
    """Query the acquisition catalog by capability set.

    Returns engines whose ``capabilities`` superset *capabilities*.
    With no filter, returns every engine in registration order.
    """
    if not capabilities:
        return list(ACQUISITION_ENGINES)
    return [e for e in ACQUISITION_ENGINES if capabilities <= e.capabilities]


def select_acquisition_engine(
    *,
    source_kind: Optional[str] = None,
    source_uri: Optional[str] = None,
    capabilities: Optional[FrozenSet[str]] = None,
) -> AcquisitionEngine:
    """Pick the canonical acquisition engine for an SDP source.

    Resolution order (each step short-circuits if it produces a unique match):

    1. **Capabilities filter** — if the caller asked for ``cdc`` + ``streaming``,
       only engines exposing both stay in the running.
    2. **Kind exact match** — e.g. ``salesforce`` → airbyte.
    3. **URI scheme match** — e.g. ``kafka://...`` → kafka_connect.
    4. **Fallback** — first engine in the filtered list, defaulting to ``duckdb``.

    The return is always one engine; never raises so the AI path can
    proceed and the user can override later.
    """
    candidates = list_acquisition_engines(capabilities=capabilities)
    if not candidates:
        candidates = list(ACQUISITION_ENGINES)

    if source_kind:
        kind_l = source_kind.strip().lower()
        for engine in candidates:
            if kind_l in engine.kinds:
                return engine

    if source_uri:
        try:
            scheme = (urlparse(source_uri).scheme or "").lower()
        except Exception:
            scheme = ""
        if scheme:
            for engine in candidates:
                if scheme in engine.schemes:
                    return engine

    for engine in candidates:
        if engine.name == "duckdb":
            return engine
    return candidates[0]


# ---------------------------------------------------------------------------
# Composition rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositionViolation:
    """One illegal upstream reference."""

    upstream_id: str
    upstream_type: Optional[str]
    target_type: str
    reason: str


def validate_composition_for_contract(
    contract: Dict,
    *,
    workspace_root: Optional[Path] = None,
    contract_path: Optional[Path] = None,
) -> List[CompositionViolation]:
    """Validate ``consumes[]`` composition for a hand-authored contract.

    Resolves this contract's ``metadata.productType`` (or the
    equivalent layer mapping) and looks up each upstream's productType
    by walking ``workspace_root`` for matching ``*.fluid.yaml`` files.

    Returns the list of :class:`CompositionViolation`s — empty when
    every consume is consistent OR when the upstream's productType
    couldn't be resolved (best-effort: we don't fail closed on missing
    catalog data because that would over-block legitimate
    cross-workspace consumes).

    Used by ``cli/validate.py::_validate_contract_for_version`` so
    ``fluid validate`` surfaces composition violations the same way it
    surfaces sovereignty / agent-policy violations. The previously
    standalone :func:`validate_composition` keeps its callers (the
    ``forge_datamodel.from_data_products`` pipeline) but now shares
    the rule definition with ``fluid validate``.
    """
    metadata = contract.get("metadata") or {}
    target_raw = metadata.get("productType") or metadata.get("layer")
    if not target_raw:
        return []
    target_pt = get_product_type(str(target_raw))
    if target_pt is None:
        return []

    consumes = contract.get("consumes") or []
    if not consumes:
        return []

    # Resolve the workspace search root: explicit > contract dir +
    # ancestors > cwd.
    roots: List[Path] = []
    if workspace_root is not None:
        roots.append(Path(workspace_root))
    elif contract_path is not None:
        base = Path(contract_path).parent
        roots.append(base)
        cur = base
        for _ in range(3):  # bounded ancestor walk
            cur = cur.parent
            if cur != cur.parent:
                roots.append(cur)
    else:
        roots.append(Path.cwd())

    upstream_types: Dict[str, Optional[str]] = {}
    for c in consumes:
        ref = c.get("productId") or c.get("ref") or c.get("provider")
        if not ref:
            continue
        upstream_types[str(ref)] = _scan_workspace_for_product_type(str(ref), roots, contract_path)

    return validate_composition(target_type=target_pt.code, upstream_types=upstream_types)


def _scan_workspace_for_product_type(
    product_id: str,
    roots: List[Path],
    contract_path: Optional[Path],
) -> Optional[str]:
    """Walk ``roots`` for a ``*.fluid.yaml`` whose ``id`` equals
    ``product_id``; return its productType code or ``None``."""
    try:
        import yaml
    except Exception:  # pragma: no cover
        return None

    self_path = Path(contract_path).resolve() if contract_path else None

    for root in roots:
        if not root.exists():
            continue
        try:
            for candidate in root.rglob("*.fluid.yaml"):
                if self_path is not None and candidate.resolve() == self_path:
                    continue
                try:
                    data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                if data.get("id") == product_id:
                    md = data.get("metadata") or {}
                    raw = md.get("productType") or md.get("layer")
                    if not raw:
                        return None
                    pt = get_product_type(str(raw))
                    return pt.code if pt else None
        except Exception:  # pragma: no cover — defensive
            continue
    return None


def validate_composition(
    *, target_type: str, upstream_types: Mapping[str, Optional[str]]
) -> List[CompositionViolation]:
    """Validate composition rules against ``allowed_upstream_types``.

    *target_type*: the product type being authored (SDP / ADP / CDP).

    *upstream_types*: mapping of upstream contract id → its product type
    (or ``None`` if unknown — treated as a violation only when the
    target type accepts a non-empty set; SDP rejects all upstreams).
    """
    target = get_product_type(target_type)
    if target is None:
        return []

    violations: List[CompositionViolation] = []
    allowed = target.allowed_upstream_types

    if not allowed:
        for uid, _utype in upstream_types.items():
            violations.append(
                CompositionViolation(
                    upstream_id=uid,
                    upstream_type=_utype,
                    target_type=target.code,
                    reason=(
                        f"{target.code} ({target.description}) does not accept "
                        "upstream products."
                    ),
                )
            )
        return violations

    for uid, utype in upstream_types.items():
        if utype is None:
            violations.append(
                CompositionViolation(
                    upstream_id=uid,
                    upstream_type=None,
                    target_type=target.code,
                    reason="Upstream productType is unknown — cannot verify composition.",
                )
            )
            continue
        utype_norm_pt = get_product_type(utype)
        utype_code = utype_norm_pt.code if utype_norm_pt else utype
        if utype_code not in allowed:
            violations.append(
                CompositionViolation(
                    upstream_id=uid,
                    upstream_type=utype_code,
                    target_type=target.code,
                    reason=(
                        f"{target.code} accepts upstreams of type "
                        f"{sorted(allowed)} but {uid!r} is {utype_code}."
                    ),
                )
            )
    return violations


# ---------------------------------------------------------------------------
# The pure builder — every authoring path goes through here
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductTypeAnswer:
    """Frozen, hashable record of every decision an authoring path made.

    Two ProductTypeAnswers that compare equal MUST produce byte-equivalent
    contracts via :func:`shape_contract`. This is the I2 invariant.
    """

    product_type: str  # SDP / ADP / CDP — must resolve via get_product_type
    name: str
    domain: str = "analytics"
    owner_team: str = "data-team"
    owner_email: str = "data-team@example.com"
    description: Optional[str] = None
    fluid_version: str = "0.7.3"

    # SDP-specific
    source_kind: Optional[str] = None
    source_uri: Optional[str] = None
    transform_engine: Optional[str] = None  # CLI override; None ⇒ default_engine
    used_dlt_generation: bool = False
    dlt_source_module: Optional[str] = None

    # ADP/CDP-specific — composition
    upstream_products: Tuple[Tuple[str, str], ...] = ()  # ((id, ref), …)

    # Optional governance / sovereignty pass-through
    jurisdiction: Optional[str] = None
    regulatory_framework: Tuple[str, ...] = ()
    data_sensitivity: Optional[str] = None

    # Schema seed for the single exposes[] entry
    columns: Tuple[Tuple[str, str, bool], ...] = (("id", "integer", True),)


def _sanitize_id_segment(s: str) -> str:
    """Lowercase, collapse non-id chars to underscore, strip edges."""
    out: List[str] = []
    for ch in (s or "").strip().lower():
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append("_")
    cleaned = "".join(out).strip("_-.") or "product"
    return cleaned


def _engine_block(answer: ProductTypeAnswer, pt: ProductType) -> Dict[str, Any]:
    """Build the single ``builds[]`` entry for the contract.

    Schema-driven: every property here is exactly what fluid-schema-0.7.3
    expects. ``trigger.type`` is ``schedule`` (NOT ``scheduled``);
    ``acquisition`` source carries ``kind`` + ``mode`` per ``acquisitionSource``;
    transformation properties carry ``sql`` per ``embedded-logic``.
    """
    if pt.engine_class == "acquisition":
        engine = (
            answer.transform_engine
            or select_acquisition_engine(
                source_kind=answer.source_kind, source_uri=answer.source_uri
            ).name
        )
        # ``acquisitionSource`` requires ``kind`` + ``mode``. The schema
        # enumerates mode ∈ {full_refresh, incremental_append,
        # incremental_dedup, incremental_merge, cdc, streaming}. Default
        # to ``full_refresh`` (snapshot semantics) — universally
        # supported by every engine in the catalog.
        source: Dict[str, Any] = {
            "kind": (answer.source_kind or "filesystem").lower(),
            "mode": "full_refresh",
        }
        if answer.source_uri:
            source["connection"] = {"uri": answer.source_uri}
        properties: Dict[str, Any] = {"source": source}
        if answer.used_dlt_generation and answer.dlt_source_module:
            engine = "dlt"
            # dlt-specific marker rides alongside acquisitionSource as a
            # connection extension; the engine reads source_module from
            # connection.uri-style paths.
            source["connection"] = source.get("connection") or {}
            source["connection"]["module"] = answer.dlt_source_module
        return {
            "id": "main_acquisition",
            "pattern": "acquisition",
            "engine": engine,
            "properties": properties,
            "execution": {
                "trigger": {"type": "schedule", "cron": "0 * * * *"},
                "runtime": {"platform": "local", "resources": {"cpu": "1", "memory": "2Gi"}},
            },
        }

    # Transformation: ADP / CDP — embedded-logic SQL is the most
    # broadly compatible default. The LLM later replaces the seed SQL
    # with the real transformation; the seed only needs to validate.
    engine = answer.transform_engine or pt.default_engine
    properties = {"sql": "SELECT 1 AS id"}
    return {
        "id": "main_transform",
        "pattern": "embedded-logic",
        "engine": engine,
        "properties": properties,
        "execution": {
            "trigger": {"type": "manual"},
            "runtime": {"platform": "local", "resources": {"cpu": "1", "memory": "2Gi"}},
        },
    }


def _consumes_block(answer: ProductTypeAnswer) -> List[Dict[str, str]]:
    """Build the ``consumes[]`` array per fluid-schema-0.7.3.

    Schema requires ``productId`` + ``exposeId`` per consumeRef. The
    answer's ``upstream_products`` carries ``(product_id, expose_id)``
    tuples — second element is the *exposeId* of that product, not a
    file ref.
    """
    consumes: List[Dict[str, str]] = []
    for product_id, expose_id in answer.upstream_products:
        consumes.append({"productId": product_id, "exposeId": expose_id})
    return consumes


def shape_contract(answer: ProductTypeAnswer) -> Dict[str, Any]:
    """Pure: ProductTypeAnswer → FLUID contract dict.

    Same answer produces a byte-identical contract every time. This is
    the function every authoring path (forge --ai, forge --blank,
    init --template, forge --refine, from_data_products) MUST funnel
    through.
    """
    pt = get_product_type(answer.product_type)
    if pt is None:
        raise ProductTypeError(
            f"Unknown product type {answer.product_type!r}; expected one of "
            f"{sorted(VALID_PRODUCT_TYPES)} or a layer name.",
            product_type=answer.product_type,
        )

    name = _sanitize_id_segment(answer.name)
    expose_name = f"{name}_output"
    columns = [
        {"name": col_name, "type": col_type, "required": required}
        for (col_name, col_type, required) in answer.columns
    ]

    metadata: Dict[str, Any] = {
        "layer": pt.layer,
        "productType": pt.code,
        "owner": {"team": answer.owner_team, "email": answer.owner_email},
    }
    normalize_metadata_in_place(metadata)

    contract: Dict[str, Any] = {
        "fluidVersion": answer.fluid_version,
        "kind": "DataProduct",
        "id": f"{pt.layer.lower()}.{answer.domain}.{name}_v1",
        "name": answer.name,
        "description": answer.description or f"FLUID {pt.code} ({pt.layer}) — {answer.name}",
        "domain": answer.domain,
        "metadata": metadata,
        "consumes": _consumes_block(answer),
        "builds": [_engine_block(answer, pt)],
        "exposes": [
            {
                "exposeId": expose_name,
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": f"runtime/out/{expose_name}.csv"},
                },
                "contract": {"schema": columns},
            }
        ],
    }

    if answer.jurisdiction or answer.regulatory_framework:
        sov: Dict[str, Any] = {"enforcementMode": "strict"}
        if answer.jurisdiction:
            sov["jurisdiction"] = answer.jurisdiction
        if answer.regulatory_framework:
            sov["regulatoryFramework"] = list(answer.regulatory_framework)
        contract["sovereignty"] = sov

    if answer.data_sensitivity in ("confidential", "restricted"):
        contract["exposes"][0]["policy"] = {
            "agentPolicy": {
                "deniedUseCases": ["training", "fine_tuning"],
                "canStore": False,
                "canReason": False,
                "auditRequired": True,
            }
        }

    return contract


# ---------------------------------------------------------------------------
# Scaffold (filesystem layout for the contract)
# ---------------------------------------------------------------------------


def scaffold_files(contract: Mapping[str, Any], target_dir: str) -> Dict[str, str]:
    """Pure mapping of relative-path → file contents for the contract.

    Returned map is suitable for round-tripping through any writer. The
    SDP-with-dlt path adds a ``sources/<name>.py`` entry (caller fills
    in the LLM-generated body); other paths get the contract YAML only.
    """
    import yaml  # local import — keeps top-level light for tests

    name = _sanitize_id_segment(contract.get("name") or "product")
    files: Dict[str, str] = {
        "contract.fluid.yaml": yaml.safe_dump(dict(contract), sort_keys=False),
    }

    pt_code = (contract.get("metadata") or {}).get("productType")
    pt = get_product_type(pt_code) if pt_code else None
    if pt and pt.engine_class == "acquisition":
        builds = contract.get("builds") or []
        if builds and (builds[0].get("engine") == "dlt"):
            module_path = (
                builds[0].get("properties", {}).get("dlt", {}).get("source_module")
                or f"./sources/{name}.py"
            )
            relpath = module_path.lstrip("./")
            files[relpath] = (
                "# Placeholder dlt source — overwritten by generate_dlt_source.\n"
                "import dlt\n\n\n"
                "@dlt.source\n"
                f"def {name}_source():\n"
                "    return ()\n"
            )

    _ = target_dir  # reserved for future absolute-path emission
    return files


__all__ = [
    "ACQUISITION_ENGINES",
    "AcquisitionEngine",
    "CompositionViolation",
    "LAYER_TO_PRODUCT_TYPE",
    "PRODUCT_TYPES",
    "PRODUCT_TYPE_TO_LAYER",
    "ProductType",
    "ProductTypeAnswer",
    "ProductTypeError",
    "VALID_LAYERS",
    "VALID_PRODUCT_TYPES",
    "get_product_type",
    "list_acquisition_engines",
    "normalize_metadata_in_place",
    "scaffold_files",
    "select_acquisition_engine",
    "shape_contract",
    "validate_composition",
    "validate_composition_for_contract",
]
