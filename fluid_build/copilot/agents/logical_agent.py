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

"""Logical-stage facade delegating to the composed ModelerAgent.

The coordinator (``copilot/agents/coordinator.py``) calls
``LogicalAgent.from_intent`` / ``from_tables`` / ``from_catalog`` as the
public Logical-stage entry points.

V1.5 added :meth:`from_catalog`: pull a ``CatalogTable[]`` list from a
configured metadata-source catalog (Snowflake / Unity / …), translate
each catalog table into the existing ``TableDefinition`` shape the
modeler already understands, then route to :meth:`from_tables`. This
keeps the modeler-agent prompt, repair loop, and Pydantic outputs
identical regardless of how the user supplied input — a intent, raw
DDL, or a metadata-source catalog all converge on the same staged
pipeline.

Catalog-aware ground truth: when the source catalog supplies rich
metadata (column descriptions, primary/foreign keys, classifications,
glossary terms), :meth:`from_catalog` converts that metadata into
the modeler's input so the LLM transcribes more and synthesizes less.
The catalog adapter is consulted *during* the Logical stage, NOT at
construction time — which keeps the per-MCP-call credential lifecycle
intact and means the catalog's data isn't re-fetched on every
``from_tables`` retry.

V1.5 design principles enforced here:

* **World-class.** ``from_catalog`` returns the same ``LogicalDraft``
  shape as the other entry points — no special-case code paths
  downstream.
* **Lightweight CLI.** The catalog adapter import is lazy (inside
  the method) so callers that don't use ``from_catalog`` pay no
  catalog-SDK import cost.
* **Best UX.** Failures from the adapter (network, auth) propagate
  as the typed catalog exceptions defined in
  ``copilot/catalog/base.py`` — not swallowed and re-raised as a
  generic agent error.
* **Open-community adoption.** Adding a new catalog adapter requires
  only a ``CatalogAdapter`` implementation; ``from_catalog`` is
  catalog-agnostic and dispatches on the adapter type.
"""

from __future__ import annotations

from typing import Any

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.modeler_agent import ModelerAgent
from fluid_build.copilot.schemas.intent import BusinessIntent
from fluid_build.forge_datamodel.from_ddl.parser import (
    ColumnDefinition,
    TableDefinition,
)


class LogicalAgent:
    def __init__(self) -> None:
        self._modeler = ModelerAgent()

    def from_tables(
        self,
        session: StageSession,
        *,
        name: str,
        tables: list[TableDefinition],
        technique: str,
        source_type: str | None = None,
    ):
        # ModelerAgent's ``_retrieve_prior_similar_models`` runs
        # RAG retrieval against ``memory/semantic`` directly and —
        # post-Sprint #1 — also writes results to the session
        # scratchpad so other agents (CriticAgent, BuilderAgent)
        # can read them from a single place.
        return self._modeler.from_tables(
            session,
            name=name,
            tables=tables,
            technique=technique,
            source_type=source_type,
        )

    def from_intent(self, session: StageSession, *, intent: BusinessIntent, technique: str):
        return self._modeler.from_intent(session, intent=intent, technique=technique)

    def from_catalog(
        self,
        session: StageSession,
        *,
        name: str,
        adapter: Any,
        scope: Any,
        technique: str,
    ):
        """Forge a logical model from a metadata-source catalog scope.

        Three-stage catalog metadata flow (Sprint D / Gap 3):

        1. **Logical** — catalog ``CatalogTable[]`` is translated
           into :class:`TableDefinition` for the modeler. Column
           descriptions / classifications / sensitivity tags /
           glossary terms / mask expressions land verbatim on
           ``ColumnDefinition.qualifiers``.
        2. **Contract / Builder** — aggregate catalog signal lands
           on :attr:`LogicalDraft.source_summary` so
           :func:`build_contract_from_logical` can promote owner /
           domain / sensitivity-tag union / lineage upstream chain
           into the Fluid contract's ``metadata.*`` and
           ``agentPolicy.*`` blocks.
        3. **Transformation** — per-column partition/clustering keys
           survive on ``ColumnDefinition.qualifiers`` so the dbt
           emitter (``cli/generate_speed_transformation.py``) can
           pick them up via ``logical.dimensional.facts[*].columns``
           / DV2 equivalents in Sprint D.

        ``adapter`` must implement :class:`CatalogAdapter`; ``scope``
        is a :class:`CatalogScope`. Adapter exceptions
        (:class:`CatalogConnectionError` /
        :class:`CatalogPermissionError`) propagate verbatim so the
        operator sees actionable suggestions.
        """
        # Gap 9 — wrap the catalog round-trip in wall-clock timing
        # so the cost-summary footer can show "Catalog fetch:
        # snowflake took 4.2s (read-only metadata)." Helps operators
        # see whether their forge runtime is dominated by the LLM
        # stage or by catalog latency.
        import time as _time

        from fluid_build.copilot.cost import get_run_tracker

        catalog_start = _time.perf_counter()

        catalog_tables = adapter.list_tables(scope)
        if not catalog_tables:
            # Empty scope — record the (small) catalog latency
            # before bailing so even empty-scope runs surface in
            # the summary footer.
            elapsed_ms = int((_time.perf_counter() - catalog_start) * 1000)
            try:
                get_run_tracker().record_catalog_fetch(adapter.name, elapsed_ms)
            except Exception:  # pragma: no cover — defensive
                pass
            # Empty scope — let the caller decide whether to forge
            # against an empty input or treat as an error. Same
            # behaviour as ``from_tables(tables=[])``.
            return self._modeler.from_tables(
                session,
                name=name,
                tables=[],
                technique=technique,
                source_type=adapter.name,
            )

        # Pull full per-table detail (columns, descriptions, FKs)
        # for every listed table. The list-then-detail two-pass
        # pattern is what every catalog ABC supports.
        tables: list[TableDefinition] = []
        full_catalog_tables = []
        for catalog_table in catalog_tables:
            full = adapter.get_table(catalog_table.fqn)
            full_catalog_tables.append(full)
            tables.append(_translate_catalog_table(full))

        # Done with catalog round-trip — record total ms.
        elapsed_ms = int((_time.perf_counter() - catalog_start) * 1000)
        try:
            get_run_tracker().record_catalog_fetch(adapter.name, elapsed_ms)
        except Exception:  # pragma: no cover — defensive
            pass

        logical = self._modeler.from_tables(
            session,
            name=name,
            tables=tables,
            technique=technique,
            source_type=adapter.name,
        )

        # Step 2 of the three-stage flow: enrich source_summary
        # with aggregate catalog signal so the Builder and
        # Transformation stages can promote it into the contract /
        # dbt project. Mutating in place is fine — source_summary
        # is a Dict[str, Any] field on LogicalDraft.
        catalog_summary = _aggregate_catalog_summary(
            adapter_name=adapter.name,
            catalog_tables=full_catalog_tables,
        )
        # Merge — the modeler already populated ``source_kind`` /
        # ``table_count`` so we extend rather than replace.
        logical.source_summary.update(catalog_summary)

        # Note: dialect back-fill USED to fire here (Gap 10's
        # initial wiring) AND again at coordinator pre-emit. That
        # was double work for catalog forges. The pre-emit hook is
        # the canonical firing point because it covers intent / DDL
        # / catalog forges uniformly. This early firing was
        # removed in the post-V1.5 simplification pass.

        # Gap 6 — lineage-driven DV2 link inference.
        #
        # Catalogs surface upstream → downstream lineage edges
        # (Snowflake OBJECT_DEPENDENCIES, Unity system.access.lineage,
        # DataHub upstream, Dataplex lineage entries). The modeler
        # infers DV2 links primarily from FK constraints; lineage
        # edges add a *deterministic* second signal. We append any
        # link the lineage implies that the modeler missed — never
        # remove or modify what the modeler decided.
        lineage_map = catalog_summary.get("lineage_by_table") or {}
        if technique == "data_vault_2" and lineage_map and logical.dv2:
            inferred = infer_dv2_links_from_lineage(logical.dv2, lineage_map)
            if inferred:
                logical.dv2.links.extend(inferred)
                logical.source_summary["lineage_inferred_link_count"] = len(inferred)

        return logical


def _translate_catalog_table(catalog_table: Any) -> TableDefinition:
    """Translate a :class:`CatalogTable` into a :class:`TableDefinition`.

    The modeler-agent prompt today accepts :class:`TableDefinition`
    (the DDL-parser output). Catalog tables carry strictly MORE
    metadata (descriptions, classifications, glossary terms,
    sensitivity tags) — we surface what the existing
    ``TableDefinition`` shape supports verbatim and stash the rest
    in :attr:`ColumnDefinition.qualifiers` so future modeler-prompt
    enhancements (Sprint D) can pick them up without a fresh
    translation pass.
    """
    columns = []
    for cat_col in catalog_table.columns:
        qualifiers = dict(cat_col.catalog_specific or {})
        if cat_col.classifications:
            qualifiers["catalog_classifications"] = list(cat_col.classifications)
        if cat_col.sensitivity_tags:
            # Use ``.value`` so the qualifier is the canonical
            # string ("pii" / "phi" / …), not the Python repr
            # (``"SensitivityTag.PII"``). Downstream prompt
            # fragments and validators key off the string value
            # — the enum repr would look like a typo to the LLM.
            qualifiers["catalog_sensitivity_tags"] = [t.value for t in cat_col.sensitivity_tags]
        if cat_col.business_glossary_terms:
            qualifiers["catalog_glossary_terms"] = list(cat_col.business_glossary_terms)
        if cat_col.mask_expression:
            qualifiers["catalog_column_mask"] = cat_col.mask_expression
        if cat_col.partition_key:
            qualifiers["catalog_partition_key"] = True
        if cat_col.clustering_key:
            qualifiers["catalog_clustering_key"] = True
        columns.append(
            ColumnDefinition(
                name=cat_col.name,
                logical_type=cat_col.data_type,
                qualifiers=qualifiers,
                nullable=cat_col.nullable,
                primary_key=cat_col.primary_key,
                comment=cat_col.description,
            )
        )
    return TableDefinition(
        name=catalog_table.name,
        columns=columns,
        primary_keys=list(catalog_table.primary_key_columns),
        comment=catalog_table.description,
    )


# Snowflake / Unity / Glue system roles that are NOT meaningful as
# a "team owner" — a Fluid contract showing ``owner.team:
# ACCOUNTADMIN`` is misleading because it just identifies the
# privilege escalation that created the table, not the human team
# responsible. We capture these separately as ``creating_role``
# (audit info) and never promote them to ``metadata.owner.team``.
_SYSTEM_ROLE_NAMES: frozenset[str] = frozenset(
    {
        # Snowflake
        "ACCOUNTADMIN",
        "SYSADMIN",
        "SECURITYADMIN",
        "USERADMIN",
        "ORGADMIN",
        "PUBLIC",
        # Unity / Databricks
        "ADMIN",
        "ADMINS",
        # AWS Glue
        "ROOT",
        # Generic
        "DBO",
        "POSTGRES",
    }
)

# Tag-key candidates for team / owner ownership intent. Matched
# case-insensitively. The first hit wins (sorted by priority).
_OWNER_TAG_KEYS: tuple[str, ...] = (
    "team",
    "owner",
    "owner_team",
    "data_team",
    "owning_team",
    "data_product_owner",
    "steward",
)
# Tag-key candidates for business domain.
_DOMAIN_TAG_KEYS: tuple[str, ...] = (
    "domain",
    "business_domain",
    "data_domain",
    "subject_area",
)


def _find_tag_value(tags: dict, candidates: tuple[str, ...]) -> str | None:
    """Case-insensitive lookup over ``candidates`` against ``tags``.

    Returns the first non-empty match. Used to pull team / domain
    out of catalog tags without forcing a single canonical key —
    different organisations use different conventions ("team" vs
    "owner_team" vs "data_team") and we want to honour all of them
    without making the user normalise upstream.
    """
    if not tags:
        return None
    lower_tags = {str(k).lower(): str(v).strip() for k, v in tags.items() if v}
    for cand in candidates:
        value = lower_tags.get(cand.lower())
        if value:
            return value
    return None


def _aggregate_catalog_summary(
    *,
    adapter_name: str,
    catalog_tables: list,
) -> dict:
    """Aggregate :class:`CatalogTable` list into a flat summary dict.

    Owner / domain priority (V1.5):

    1. **Catalog tags first.** Tags like ``team`` / ``owner`` /
       ``data_team`` are explicit metadata the data team set
       intentionally — these are the authoritative source for
       ``metadata.owner.team``.
    2. **Non-system table owner.** If no tag matches, fall back to
       :attr:`CatalogTable.owner` BUT skip system roles
       (``ACCOUNTADMIN`` / ``SYSADMIN`` / etc.) — those are
       privilege artefacts, not team identities.
    3. **None** → contract emitter falls back to its hardcoded
       default (``"data-team"``), which is an obvious signal that
       no real owner was found.

    Other aggregations (sensitivity tags, lineage, classifications,
    quality score, freshness SLAs) are union / min / set
    operations across the table list — non-destructive, O(N) once.
    """
    from collections import Counter

    sensitivity_set: set[str] = set()
    classifications_set: set[str] = set()
    glossary_set: set[str] = set()
    lineage_upstream: set[str] = set()
    # Per-table lineage map — drives Gap 6 DV2 link inference.
    # Key is the downstream table's FQN; value is the list of
    # upstream FQNs the catalog reported. Preserving this as a map
    # (instead of just the flat ``lineage_upstream`` set) lets the
    # post-processor know which DOWNSTREAM hub each upstream is
    # connected to.
    lineage_by_table: dict[str, list[str]] = {}
    quality_scores: list[float] = []
    freshness_set: set[str] = set()
    creating_roles: Counter = Counter()
    tag_owners: Counter = Counter()
    tag_domains: Counter = Counter()
    raw_owners: Counter = Counter()
    table_domains: Counter = Counter()

    for t in catalog_tables:
        # Tag-based owner / domain (priority 1).
        tag_owner = _find_tag_value(t.tags or {}, _OWNER_TAG_KEYS)
        if tag_owner:
            tag_owners[tag_owner] += 1
        tag_domain = _find_tag_value(t.tags or {}, _DOMAIN_TAG_KEYS)
        if tag_domain:
            tag_domains[tag_domain] += 1

        # Catalog-table.owner — useful as audit info when it's a
        # role (record under creating_roles) and as a fallback team
        # name when it's clearly NOT a system role.
        if t.owner:
            owner_str = t.owner.strip()
            if owner_str.upper() in _SYSTEM_ROLE_NAMES:
                creating_roles[owner_str] += 1
            elif owner_str:
                raw_owners[owner_str] += 1
        if t.domain:
            table_domains[t.domain] += 1

        # Sensitivity / classification / glossary unions.
        sensitivity_set.update(tag.value for tag in t.sensitivity_tags or [])
        classifications_set.update(t.classifications or [])
        glossary_set.update(t.glossary_terms or [])
        if t.data_quality_score is not None:
            quality_scores.append(float(t.data_quality_score))
        if t.freshness_sla:
            freshness_set.add(t.freshness_sla)
        for col in t.columns or []:
            sensitivity_set.update(tag.value for tag in col.sensitivity_tags or [])
            classifications_set.update(col.classifications or [])
            glossary_set.update(col.business_glossary_terms or [])
        if t.lineage and t.lineage.upstream:
            upstream_fqns = [ref.fqn for ref in t.lineage.upstream]
            lineage_upstream.update(upstream_fqns)
            # Use the catalog's FQN if present; fall back to the bare
            # name. Either is fine — the link inferer keys both
            # source_table sets the same way.
            downstream_key = t.fqn or t.name
            lineage_by_table[downstream_key] = upstream_fqns

    summary: dict = {
        "source_kind": "catalog",
        "source_catalog_name": adapter_name,
    }

    # Owner: tag-based wins; fall back to non-system raw owner;
    # else leave unset so the contract emitter uses its default.
    if tag_owners:
        summary["dominant_owner"] = tag_owners.most_common(1)[0][0]
        summary["dominant_owner_source"] = "tag"
    elif raw_owners:
        summary["dominant_owner"] = raw_owners.most_common(1)[0][0]
        summary["dominant_owner_source"] = "table_owner"

    # Domain: tag-based wins; fall back to typed CatalogTable.domain.
    if tag_domains:
        summary["dominant_domain"] = tag_domains.most_common(1)[0][0]
    elif table_domains:
        summary["dominant_domain"] = table_domains.most_common(1)[0][0]

    # Creating role(s) — pure audit info; surfaces in the contract's
    # provenance block but never as team owner.
    if creating_roles:
        summary["creating_roles"] = sorted(creating_roles)

    if sensitivity_set:
        summary["sensitivity_tags"] = sorted(sensitivity_set)
    if classifications_set:
        summary["classifications"] = sorted(classifications_set)
    if glossary_set:
        summary["glossary_terms"] = sorted(glossary_set)
    if lineage_upstream:
        summary["lineage_upstream"] = sorted(lineage_upstream)
    if lineage_by_table:
        # Sort each value list for stable ordering — matters for
        # deterministic forge runs and for the test pin in
        # ``test_lineage_to_dv2_links.py``.
        summary["lineage_by_table"] = {k: sorted(v) for k, v in sorted(lineage_by_table.items())}
    if quality_scores:
        summary["data_quality_score_min"] = min(quality_scores)
    if freshness_set:
        summary["freshness_sla_set"] = sorted(freshness_set)
    return summary


# ---------------------------------------------------------------------
# Gap 6 — lineage → DV2 link inference (deterministic post-processor)
# ---------------------------------------------------------------------


def _table_token(fqn: str) -> str:
    """Return the bare table name from a fully-qualified catalog
    identifier. ``DB.SCHEMA.ORDERS`` → ``orders`` (case-folded).

    Used to match catalog FQNs against
    :attr:`HubDefinition.mapped_source_tables` which the modeler
    populates with bare names. Case-folded so SF's uppercase FQNs
    match the modeler's typical lowercase table tokens.
    """
    base = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
    return base.strip().strip('`"[]').lower()


def _build_table_to_hub_map(dv2: Any) -> dict[str, str]:
    """Index ``hub.entity_name`` keyed by every source-table token
    each hub maps to. Two hubs claiming the same source-table are
    rare; when it happens, the later one wins (deterministic since
    we iterate in declaration order)."""
    out: dict[str, str] = {}
    for hub in dv2.hubs or []:
        for src in hub.mapped_source_tables or []:
            out[_table_token(src)] = hub.entity_name
        # Also index by the entity name itself — some catalogs
        # report lineage with cleaned-up entity names rather than
        # raw table names.
        out[_table_token(hub.entity_name)] = hub.entity_name
    return out


def infer_dv2_links_from_lineage(
    dv2: Any,
    lineage_by_table: dict[str, list[str]],
) -> list:
    """Infer DV2 link definitions from catalog lineage edges.

    Algorithm:

    1. Index every hub by every (mapped_source_table, entity_name)
       token the modeler populated.
    2. For each ``downstream → [upstream...]`` lineage edge:
       a. Resolve the downstream hub via the index. Skip if no
          hub maps to that table (it's a non-modeled raw table).
       b. For each upstream FQN, resolve to a hub. Skip
          self-edges.
       c. If a link with the same ``hubs_involved`` set already
          exists in ``dv2.links``, skip — never duplicate the
          modeler's work.
       d. Append a new ``LinkDefinition`` named via the existing
          ``link_name(...)`` helper, marked
          ``relationships=[]`` and ``join_keys=[]`` so the
          downstream contract emitter knows this link came from
          lineage signal alone (no FK confirmed).

    The function is **deterministic** — same lineage map in, same
    link list out, regardless of dict iteration order. Sorted
    iteration over both downstream tables and upstream lists
    guarantees stable output for cache-keying.
    """
    from fluid_build.copilot.schemas.data_model import LinkDefinition
    from fluid_build.forge_datamodel.dv2 import link_name

    if not dv2 or not lineage_by_table:
        return []

    table_to_hub = _build_table_to_hub_map(dv2)
    if not table_to_hub:
        return []

    # Existing links keyed by frozenset(hubs_involved) so we can
    # de-duplicate against modeler-emitted links.
    existing_pairs: set[frozenset[str]] = set()
    for link in dv2.links or []:
        if link.hubs_involved:
            existing_pairs.add(frozenset(link.hubs_involved))

    inferred: list = []
    seen_new: set[frozenset[str]] = set()
    # Sorted iteration → deterministic output.
    for downstream, upstreams in sorted(lineage_by_table.items()):
        downstream_hub = table_to_hub.get(_table_token(downstream))
        if not downstream_hub:
            continue
        for upstream in sorted(upstreams):
            upstream_hub = table_to_hub.get(_table_token(upstream))
            if not upstream_hub or upstream_hub == downstream_hub:
                continue
            pair = frozenset({upstream_hub, downstream_hub})
            if pair in existing_pairs or pair in seen_new:
                continue
            seen_new.add(pair)
            inferred.append(
                LinkDefinition(
                    link_name=link_name(upstream_hub, downstream_hub),
                    link_table_name=link_name(upstream_hub, downstream_hub),
                    hubs_involved=[upstream_hub, downstream_hub],
                    join_keys=[],
                    relationships=[],
                )
            )
    return inferred


def _compose_retrieval_query(
    *,
    name: str,
    technique: str,
    tables: list[TableDefinition],
) -> str:
    """Build a free-text retrieval query from a from_tables call.

    Includes the model name + technique + first ~10 table names
    (cap to bound the query token count). This is the string the
    semantic search engine sees, so it should be specific to THIS
    forge — not so generic that every prior model matches.
    """
    table_names = [t.name for t in tables[:10] if getattr(t, "name", None)]
    parts = [name or "", f"technique={technique}", ", ".join(table_names)]
    return " ".join(p for p in parts if p).strip()


__all__ = [
    "LogicalAgent",
    "infer_dv2_links_from_lineage",
    "_compose_retrieval_query",
]
