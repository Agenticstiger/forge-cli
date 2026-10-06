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

"""Plan/validate-time checks for the Iceberg streaming sink (RFC §6.8).

These catch the connector's silent-fail-at-first-record traps BEFORE any apply:
a sink with no matching Iceberg expose (the deriver would no-op or mis-target),
the v1-deferred upsert mode, dynamic routing without a route field, and the
catalog traps below. It is a pure function returning (errors, warnings) — the
validate stage routes errors to its collector and surfaces warnings, mirroring
product_types.py. The Kafka-Connect and Debezium-Server runners run the SAME
checks through :func:`iceberg_sink_preflight` right before they derive a sink,
so a contract ``fluid validate`` rejects fails its run before any Connect REST
call or ``application.properties`` write, rather than only when someone
remembered to validate first.

The catalog checks are TABLE-DRIVEN: each one reads the kind's row from
``providers/_iceberg_catalog.py`` (the classification the sink deriver, dbt
``catalogs.yml`` and the IaC emitters share) instead of matching literals.
This validator used to check only ``catalog == "rest"``, so ``catalog:
lakekeeper`` passed with no ``uri``, streamed over REST, and dbt wrote a
Snowflake-managed table for the same expose. Per sink build:

* the kind must be in the table: an unknown value gets each emitter's historic
  fallback, and those fallbacks disagree with one another;
* ``sink.catalog`` must agree with the expose's catalog, because dbt and the
  IaC read only the expose;
* every ``binding.location`` key in the row's ``sink_requires`` must be set
  (Glue keeps its advisory region warning: the warehouse falls back);
* a catalog that addresses its warehouse by NAME (Lakekeeper, Polaris, Unity)
  must not be given an object-store URI, and one that mounts its REST API
  under a path (Lakekeeper's ``/catalog``) should have it in ``uri``;
* the runtime must ship the catalog's client (the stock Apache Iceberg Kafka
  Connect runtime has no Nessie client);
* an operator override must not move the warehouse away from the binding, nor
  leave the connector carrying both ``type`` and ``catalog-impl``.

Debezium builds in ``bring-your-own`` / ``managed`` mode create only the SOURCE
connector (no sink is derived), so none of this applies to them; an embedded
Debezium Server build is checked whenever it would derive an Iceberg sink.

Why imperative Python and not JSON-Schema if/then: these are CROSS-OBJECT checks
(a build's sink ↔ a different expose's binding; a computed warehouse vs an
override). JSON Schema's conditionals (if/then/else, dependentRequired) only
express dependencies WITHIN one object and can't reference across array elements
or compute derived values — so they cannot express the build→expose join or the
warehouse cross-check. This is the same conclusion Apache Kafka Connect reached:
``ConfigDef.Validator`` can't see other fields, so cross-field validation must be
done imperatively by overriding the connector's ``validate()``. We keep ALL the
sink's cross-field checks in this one validator (single source of truth, richer
messages) rather than splitting the same-object ones into the schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...providers._iceberg_catalog import (
    FAMILY_GLUE,
    FAMILY_REST,
    FAMILY_UNKNOWN,
    CatalogKind,
    binding_catalog_kind,
    canonical_catalog_kind,
    catalog_kind_info,
    iceberg_catalog_kind,
    iceberg_sink_exposes,
    is_object_store_uri,
    known_catalog_kinds,
    resolve_iceberg_catalog,
)

LOG = logging.getLogger("fluid.acquire.iceberg_sink")

_Issues = Tuple[List[str], List[str]]

#: ``only_build`` default: check every build. A sentinel, not ``None``, because
#: a build with an explicit ``id: null`` runs with ``ctx.build_id is None``.
_ALL_BUILDS: Any = object()


@dataclass(frozen=True)
class _SinkRuntime:
    """How one build's runner assembles its Iceberg sink config.

    The two runtimes spell the catalog selector differently (Kafka Connect
    prefixes ``iceberg.catalog.``; Debezium Server takes bare keys) and merge
    different operator maps over the derived block, so the override checks read
    these instead of assuming the Kafka-Connect shape.
    """

    engine: str
    #: Does the runner derive a catalog block from the expose for this build?
    derives: bool
    type_key: str
    impl_key: str
    #: ``(label, map)`` merged over the derived config, in merge order.
    overrides: Tuple[Tuple[str, Mapping[str, Any]], ...]
    #: ``(label, value)`` of the warehouse override the zero-drift check reads.
    warehouse_override: Tuple[str, Any]


def _sink_runtime(build: Mapping[str, Any]) -> Optional[_SinkRuntime]:
    """The runtime that writes an Iceberg sink for ``build``, or ``None``.

    Mirrors each runner's own gate. The Debezium runner derives from
    ``server.sink.type`` (default ``iceberg``) in embedded mode and never reads
    ``sink.format``, so selecting its builds by ``sink.format`` alone would let
    a deriving build skip every check while one that never derives (a
    bring-your-own build creates only the source connector) drew errors about
    a catalog nothing writes to.
    """
    props = build.get("properties") or {}
    sink = props.get("sink") or {}
    declares_iceberg = str(sink.get("format") or "").lower() == "iceberg"
    engine = str(build.get("engine") or "").strip().lower()

    if engine == "debezium":
        dbz = props.get("debezium") or {}
        if (dbz.get("deployment") or {}).get("mode", "bring-your-own") != "embedded":
            return None
        server_sink = (dbz.get("server") or {}).get("sink") or {}
        if server_sink.get("type", "iceberg") != "iceberg":
            return None
        derives = bool(server_sink.get("iceberg_sink_enabled", "config" not in server_sink))
        if not (declares_iceberg or derives):
            return None
        config = server_sink.get("config") or {}
        label = "debezium.server.sink.config"
        return _SinkRuntime(
            engine=engine,
            derives=derives,
            type_key="type",
            impl_key="catalog-impl",
            overrides=((label, config),),
            warehouse_override=(label, config.get("warehouse")),
        )

    if not declares_iceberg:
        return None
    kc = props.get("kafka-connect") or {}
    handwritten = kc.get("sink_connector_config")
    derives = bool(kc.get("iceberg_sink_enabled", handwritten is None))
    catalog_overrides = kc.get("iceberg_catalog_overrides") or {}
    # ``iceberg_catalog_overrides`` is applied inside the deriver, so it only
    # reaches the connector when the runner derives; a hand-written
    # ``sink_connector_config`` is merged over the result either way.
    overrides: Tuple[Tuple[str, Mapping[str, Any]], ...] = ()
    if derives:
        overrides += (("iceberg_catalog_overrides", catalog_overrides),)
    if isinstance(handwritten, Mapping):
        overrides += (("sink_connector_config", handwritten),)
    return _SinkRuntime(
        engine=engine,
        derives=derives,
        type_key="iceberg.catalog.type",
        impl_key="iceberg.catalog.catalog-impl",
        overrides=overrides,
        warehouse_override=(
            "iceberg_catalog_overrides",
            catalog_overrides.get("iceberg.catalog.warehouse"),
        ),
    )


def validate_iceberg_sink(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings) for every Iceberg streaming-sink build."""
    return _validate(contract)


def iceberg_sink_preflight(
    contract: Mapping[str, Any],
    build_id: Any,
    *,
    log: Optional[logging.Logger] = None,
) -> Optional[str]:
    """Run-time twin of ``fluid validate``: ONE build's sink errors, or ``None``.

    A runner calls this right before it derives a sink, so a contract the
    validator rejects fails the run before any Connect REST call or file write
    instead of shipping a connector that never starts (both selector keys) or
    writes into a different catalog than dbt reads. ``build_id`` is the run
    context's id (``build.get("id", "unknown")``); the build is selected by it,
    not by matching message text. Warnings are logged and never fatal, the same
    split as ``fluid validate`` without ``--strict``.
    """
    errors, warnings = _validate(contract, only_build=build_id)
    for msg in warnings:
        (log or LOG).warning("iceberg_sink.preflight.warning build=%s %s", build_id, msg)
    if not errors:
        return None
    return "iceberg sink preflight failed (see `fluid validate`): " + "; ".join(errors)


def _validate(contract: Mapping[str, Any], *, only_build: Any = _ALL_BUILDS) -> _Issues:
    errors: List[str] = []
    warnings: List[str] = []

    iceberg_exposes = iceberg_sink_exposes(contract)
    expose_ids = {e.get("exposeId") or e.get("id") for e in iceberg_exposes}

    for build in contract.get("builds") or []:
        if not isinstance(build, Mapping):
            continue
        # ``build_acquisition_run_context`` ids a build ``build.get("id", "unknown")``.
        if only_build is not _ALL_BUILDS and build.get("id", "unknown") != only_build:
            continue
        runtime = _sink_runtime(build)
        if runtime is None:
            continue  # not a build that writes an Iceberg sink
        _check_build(build, runtime, iceberg_exposes, expose_ids, errors, warnings)

    return errors, warnings


def _check_build(
    build: Mapping[str, Any],
    runtime: _SinkRuntime,
    iceberg_exposes: Sequence[Mapping[str, Any]],
    expose_ids: AbstractSet[Any],
    errors: List[str],
    warnings: List[str],
) -> None:
    props = build.get("properties") or {}
    sink = props.get("sink") or {}
    bid = build.get("id", "?")
    kc = props.get("kafka-connect") or {}
    streaming = kc.get("streamingSink") or kc.get("streaming_sink") or {}

    # 1. build -> expose join: an Iceberg sink needs a matching Iceberg
    #    expose (the deriver resolves the catalog identity from it). HARD.
    if not iceberg_exposes:
        errors.append(
            f"iceberg sink (build {bid!r}) has no expose with binding.format=iceberg; "
            "the connector has no table identity to write to"
        )
        return
    outputs = build.get("outputs") or []
    if outputs and not (set(outputs) & expose_ids):
        warnings.append(
            f"iceberg sink (build {bid!r}) outputs {list(outputs)} don't reference the "
            f"Iceberg expose(s) {sorted(x for x in expose_ids if x)}; the join is implicit"
        )
    binding = iceberg_exposes[0].get("binding") or {}

    # 2. upsert is deferred to v2 (locked v1 decision) — gate, don't silently
    #    append-only. HARD.
    if streaming.get("upsertMode") is True:
        errors.append(
            f"iceberg sink (build {bid!r}): streamingSink.upsertMode is not supported in "
            "v1 (CDC/upsert deferred); remove it or use append mode"
        )

    # 3. dynamic routing needs a route field, else records with no target are
    #    silently dropped. HARD.
    if streaming.get("dynamicEnabled") is True and not streaming.get("routeField"):
        errors.append(
            f"iceberg sink (build {bid!r}): streamingSink.dynamicEnabled requires "
            "streamingSink.routeField"
        )

    _check_catalog(bid, binding, sink, runtime, errors, warnings)


def _check_catalog(
    bid: Any,
    binding: Mapping[str, Any],
    sink: Mapping[str, Any],
    runtime: _SinkRuntime,
    errors: List[str],
    warnings: List[str],
) -> None:
    """The catalog checks (4-6), every one read off the kind's table row."""
    loc = binding.get("location") or {}
    kind = iceberg_catalog_kind(binding, sink)
    info = catalog_kind_info(kind)
    sink_kind = canonical_catalog_kind(sink.get("catalog"))

    # An unknown kind gets each emitter's historic fallback (REST for the sink,
    # Snowflake-managed for dbt), so the per-kind checks below would be guesses
    # about a row that does not exist. Refuse the value instead. HARD.
    if info.family == FAMILY_UNKNOWN:
        from_sink = bool(sink_kind)
        raw = sink.get("catalog") if from_sink else loc.get("catalog")
        where = "sink.catalog" if from_sink else "binding.location.catalog"
        errors.append(
            f"iceberg sink (build {bid!r}): unknown Iceberg catalog {raw!r} in {where}; "
            f"use one of: {', '.join(known_catalog_kinds())}"
        )
        return

    # The sink must write through the catalog dbt and the IaC read. Both read
    # only the expose, so a ``sink.catalog`` that differs streams into one
    # catalog while the static table and the models live in another. Compared
    # canonically: ``iceberg-rest`` and ``rest`` are the same catalog. HARD.
    expose_kind = binding_catalog_kind(binding)
    if sink_kind and sink_kind != expose_kind:
        expose_source = (
            "binding.location.catalog"
            if canonical_catalog_kind(loc.get("catalog"))
            else f"the {binding.get('platform')!r} platform default"
        )
        errors.append(
            f"iceberg sink (build {bid!r}): sink.catalog {sink.get('catalog')!r} disagrees "
            f"with the expose's catalog {expose_kind!r} ({expose_source}); the sink would "
            f"write through {sink_kind} while dbt and the IaC read {expose_kind}. Set "
            f"binding.location.catalog: {sink_kind} and drop sink.catalog"
        )

    # 4. catalog tagged-union completeness, from the row's ``sink_requires``.
    #    HARD: forge can't derive a REST uri or a catalog name. Glue requires
    #    nothing (the warehouse falls back), so its region stays advisory.
    for key in info.sink_requires:
        if not loc.get(key):
            suffix = (
                " (the catalog name)"
                if key == "warehouse" and (info.warehouse_is_name or info.family == FAMILY_REST)
                else ""
            )
            errors.append(
                f"iceberg sink (build {bid!r}): {kind} catalog requires "
                f"binding.location.{key}{suffix}"
            )
    if info.family == FAMILY_GLUE and not loc.get("region"):
        warnings.append(
            f"iceberg sink (build {bid!r}): glue catalog without binding.location.region; "
            "the connector needs iceberg.catalog.client.region"
        )

    # A name-addressed catalog owns the storage location: handed an s3:// URI
    # as ``warehouse`` it looks up a warehouse literally called that and the
    # REST client's /v1/config fails before the first commit. HARD.
    warehouse = loc.get("warehouse")
    if info.warehouse_is_name and is_object_store_uri(warehouse):
        errors.append(
            f"iceberg sink (build {bid!r}): {kind} addresses a warehouse by NAME "
            '(e.g. "analytics" or "<project-id>/<name>"), not an object-store URI; '
            f"binding.location.warehouse is {warehouse!r}"
        )

    # Lakekeeper mounts the Iceberg REST API under ``/catalog``; a bare host
    # sends the client's GET /v1/config to a path that does not serve it.
    # Advisory: a reverse proxy may legitimately rewrite the path.
    uri = str(loc.get("uri") or "")
    if info.uri_suffix and uri and not uri.rstrip("/").endswith(info.uri_suffix):
        warnings.append(
            f"iceberg sink (build {bid!r}): binding.location.uri {uri!r} does not end in "
            f"{info.uri_suffix!r}; {kind} serves the Iceberg REST API under "
            f"<host>{info.uri_suffix}"
        )

    # The stock Apache Iceberg Kafka Connect runtime bundles the AWS, GCP and
    # Azure modules (Hive only in its -hive- distribution) but no iceberg-nessie
    # (apache/iceberg kafka-connect/build.gradle), so ``type=nessie`` fails to
    # load NessieCatalog on a stock worker. Advisory: a custom image may add it.
    if info.name == "nessie" and runtime.engine == "kafka-connect":
        warnings.append(
            f"iceberg sink (build {bid!r}): the stock Apache Iceberg Kafka Connect runtime "
            "does not bundle iceberg-nessie (apache/iceberg kafka-connect/build.gradle); "
            "add the iceberg-nessie jar to the worker's connector plugin directory, or "
            "the sink cannot load NessieCatalog"
        )

    _check_warehouse_override(bid, binding, sink, kind, info, runtime, warnings)
    _check_selector_overrides(bid, kind, info, runtime, errors)


def _check_warehouse_override(
    bid: Any,
    binding: Mapping[str, Any],
    sink: Mapping[str, Any],
    kind: str,
    info: CatalogKind,
    runtime: _SinkRuntime,
    warnings: List[str],
) -> None:
    """5. zero-drift cross-check (consumes PR1's same_warehouse).

    If the operator overrides the warehouse it must still match the binding,
    else the streaming write and what dbt / the IaC address diverge. Operator
    wins (warn, not fail), per the locked decision. The comparison is against
    the warehouse the deriver itself resolves: for Glue that is PR1's canonical
    ``s3://`` writer (unchanged), for every other catalog it is
    ``location.warehouse``. Comparing a REST catalog's override against the
    Glue writer used to report a matching override as diverging.
    """
    label, override_wh = runtime.warehouse_override
    if not override_wh:
        return
    from fluid_build.providers.aws.util.warehouse import same_warehouse

    derived_wh = resolve_iceberg_catalog(binding, sink=sink, account_ref="").warehouse
    if not derived_wh and "warehouse" in info.sink_requires:
        return  # check 4 already refused the missing warehouse
    if same_warehouse(override_wh, derived_wh):
        return
    if info.family == FAMILY_GLUE:
        warnings.append(
            f"iceberg sink (build {bid!r}): {label} warehouse "
            f"{override_wh!r} diverges from the binding warehouse {derived_wh!r}; "
            "the connector will use the override but the static Glue table may differ"
        )
    else:
        warnings.append(
            f"iceberg sink (build {bid!r}): {label} warehouse "
            f"{override_wh!r} diverges from the binding warehouse {derived_wh!r} of the "
            f"{kind} catalog; the sink will write there while dbt and the IaC address "
            "the binding's warehouse"
        )


def _check_selector_overrides(
    bid: Any,
    kind: str,
    info: CatalogKind,
    runtime: _SinkRuntime,
    errors: List[str],
) -> None:
    """6. the merged config must carry ``type`` XOR ``catalog-impl``.

    Iceberg's ``CatalogUtil.buildIcebergCatalog`` throws "both type and
    catalog-impl are set" when it gets both, so the sink never starts. The
    deriver emits exactly one; an override of the OTHER key (``type`` over a
    derived Glue ``catalog-impl``, say) silently re-creates the crash, because
    override maps are merged last. Presence is what counts: CatalogUtil checks
    the key for null, so even an empty value trips it. HARD.
    """
    setters: Dict[str, str] = {}
    if runtime.derives:
        derived = runtime.impl_key if info.catalog_impl else runtime.type_key
        setters[derived] = f"the derived {kind} catalog config"
    for label, mapping in runtime.overrides:
        for key in (runtime.type_key, runtime.impl_key):
            if key in mapping:
                setters[key] = label
    if runtime.type_key in setters and runtime.impl_key in setters:
        errors.append(
            f"iceberg sink (build {bid!r}): the sink config would carry both "
            f"{runtime.type_key} (from {setters[runtime.type_key]}) and {runtime.impl_key} "
            f"(from {setters[runtime.impl_key]}); Iceberg's CatalogUtil refuses a catalog "
            "with both type and catalog-impl set, so the sink never starts. Keep one, and "
            "change catalogs with binding.location.catalog rather than an override"
        )
