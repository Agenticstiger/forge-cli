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
checks through :func:`iceberg_sink_preflight` for every build that has a plan
(one that declares ``sink.format: iceberg``, or an embedded Debezium build that
derives its sink), derived or hand-written, so such a contract ``fluid validate``
rejects fails its run before any Connect REST call or ``application.properties``
write. Which builds those are is ONE answer, :func:`iceberg_sink_plan`, read off the build the runner
executes (:func:`executing_build`): the validator, the preflight and both
runners consume it, so none of them re-implements another's gate.

The catalog checks are TABLE-DRIVEN: each one reads the kind's row from
``providers/_iceberg_catalog.py`` (the classification the sink deriver, dbt
``catalogs.yml`` and the IaC emitters share) instead of matching literals.
This validator used to check only ``catalog == "rest"``, so ``catalog:
lakekeeper`` passed with no ``uri``, streamed over REST, and dbt wrote a
Snowflake-managed table for the same expose. Per sink build:

* the kind must be in the table: an unknown value gets each emitter's historic
  fallback, and those fallbacks disagree with one another;
* on GCP the catalog must be named: absent, the sink writes whatever catalog
  reaches the worker (REST by default, Glue by ``catalog-impl``...) while
  dbt-bigquery and the GCP IaC create a BigLake table;
* ``sink.catalog`` must agree with the expose's catalog, because dbt and the
  IaC read only the expose, and so must the catalog an override or a
  hand-written config selects (its ``type`` or ``catalog-impl``);
* every ``binding.location`` key in the row's ``sink_requires`` must be set,
  a warehouse counting when the deriver resolves one (DynamoDB and JDBC derive
  it from an explicit bucket); Glue keeps its advisory region warning: the
  warehouse falls back;
* a bucket ``{{ env.* }}`` template the DynamoDB, JDBC or BigQuery warehouse
  derives from, naming a variable that is unset or empty, is a warning here
  and an error in the runner's preflight;
* the runtime must ship the catalog's client (the stock Apache Iceberg Kafka
  Connect runtime has no Nessie client, and the published sink predates the
  ``bigquery`` catalog type);
* an operator override must not move the warehouse away from the binding, nor
  leave the connector carrying both ``type`` and ``catalog-impl``.

Debezium builds in ``bring-your-own`` / ``managed`` mode create only the SOURCE
connector (no sink is derived), so none of this applies to them; an embedded
Debezium Server build is checked whenever it writes an Iceberg sink, derived
or from its hand-written ``server.sink.config``.

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

import contextlib
import contextvars
import logging
from dataclasses import dataclass
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from ...providers._iceberg_catalog import (
    BIGQUERY_PROJECT_ID,
    BUCKET_WAREHOUSE_KINDS,
    DYNAMODB_CATALOG_IMPL,
    FAMILY_GLUE,
    FAMILY_REST,
    FAMILY_UNKNOWN,
    GLUE_CATALOG_IMPL,
    CatalogKind,
    binding_catalog_kind,
    canonical_catalog_kind,
    catalog_kind_info,
    iceberg_catalog_kind,
    iceberg_sink_exposes,
    known_catalog_kinds,
    resolve_iceberg_catalog,
)

LOG = logging.getLogger("fluid.acquire.iceberg_sink")

_Issues = Tuple[List[str], List[str]]

#: The published Apache Iceberg Kafka Connect sink (Confluent Hub). Its
#: ``CatalogUtil`` knows no ``bigquery`` type; Iceberg 1.10 adds it.
PUBLISHED_KAFKA_CONNECT_SINK_VERSION = "1.9.2"

#: The catalog each ``catalog-impl`` class is: Apache Iceberg's
#: ``CatalogUtil.ICEBERG_CATALOG_*`` classes (what each ``type`` loads) plus the
#: impl-only DynamoDB catalog. A ``catalog-impl`` that reaches the worker is
#: therefore compared as the catalog it is, not skipped as "no type". A class
#: not listed is a custom catalog: it matches no kind, BigLake included.
_IMPL_CATALOGS: Mapping[str, str] = {
    GLUE_CATALOG_IMPL: "glue",
    DYNAMODB_CATALOG_IMPL: "dynamodb",
    "org.apache.iceberg.rest.RESTCatalog": "rest",
    "org.apache.iceberg.nessie.NessieCatalog": "nessie",
    "org.apache.iceberg.hive.HiveCatalog": "hive",
    "org.apache.iceberg.jdbc.JdbcCatalog": "jdbc",
    "org.apache.iceberg.hadoop.HadoopCatalog": "hadoop",
    "org.apache.iceberg.gcp.bigquery.BigQueryMetastoreCatalog": "bigquery",
}


def executing_build(contract: Mapping[str, Any], build_id: Any) -> Optional[Mapping[str, Any]]:
    """The build a runner whose run context carries ``build_id`` executes.

    ``build_acquisition_run_context`` ids a build ``build.get("id", "unknown")``
    (an explicit ``id: null`` runs as ``None``), so the same spelling selects it
    here. Both streaming runners read their properties from this build and the
    preflight checks this build, so the two can never look at different ones;
    they used to read ``builds[0]`` while the preflight checked ``build_id``.
    ``None`` when no build carries the id.
    """
    for build in contract.get("builds") or []:
        if isinstance(build, Mapping) and build.get("id", "unknown") == build_id:
            return build
    return None


@dataclass(frozen=True)
class IcebergSinkPlan:
    """How one build's runner assembles the Iceberg sink config it pushes.

    The two runtimes spell the catalog selector differently (Kafka Connect
    prefixes ``iceberg.catalog.``; Debezium Server takes bare keys) and merge
    different operator maps over the derived block, so the override checks read
    these instead of assuming the Kafka-Connect shape.
    """

    engine: str
    #: Does the runner derive a catalog block from the expose for this build?
    #: ``False`` means it pushes only the hand-written config, if there is one.
    derives: bool
    type_key: str
    impl_key: str
    #: ``(label, map)`` merged over the derived config, in merge order.
    overrides: Tuple[Tuple[str, Mapping[str, Any]], ...]
    #: ``(label, value)`` of the warehouse override the zero-drift check reads.
    warehouse_override: Tuple[str, Any]


def iceberg_sink_plan(build: Mapping[str, Any]) -> Optional[IcebergSinkPlan]:
    """How ``build``'s runner writes its Iceberg sink, or ``None`` if it has none.

    A plan exists for every build that declares an Iceberg sink or derives
    one, whether the config is derived or hand-written. It is the ONE gate:
    ``fluid validate`` checks a build exactly when this returns a plan, both
    runners run :func:`iceberg_sink_preflight` exactly then, and they derive
    exactly when ``plan.derives``. The Debezium runner derives from
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
        return IcebergSinkPlan(
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
    return IcebergSinkPlan(
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

    A runner calls this whenever :func:`iceberg_sink_plan` says it will push an
    Iceberg sink config, derived or hand-written, so a contract the validator
    rejects fails the run before any Connect REST call or file write instead of
    shipping a connector that never starts (both selector keys) or writes into
    a different catalog than dbt reads. ``build_id`` is the run context's id;
    the build is :func:`executing_build`, the one the runner reads its
    properties from, not one matched by message text. Warnings are logged and
    never fatal, the same split as ``fluid validate`` without ``--strict``, with
    one difference: a ``location.bucket`` template that ``fluid validate``
    only warns about (it may resolve where the sink runs) is an error here,
    because the runner derives the sink config in this process next. The
    template is read from the contract as written when the runner runs inside
    :func:`contract_as_written`, as the acquisition dispatcher runs it.
    """
    build = executing_build(contract, build_id)
    if build is None:
        return None
    run_only: List[str] = []
    token = _AT_RUN.set(run_only)
    try:
        errors, warnings = _validate(contract, builds=(build,))
    finally:
        _AT_RUN.reset(token)
    for msg in warnings:
        (log or LOG).warning("iceberg_sink.preflight.warning build=%s %s", build_id, msg)
    if not errors:
        return None
    # ``fluid validate`` reports every error but the run-only ones.
    see = " (see `fluid validate`)" if any(e not in run_only for e in errors) else ""
    return f"iceberg sink preflight failed{see}: " + "; ".join(errors)


def _validate(contract: Mapping[str, Any], *, builds: Optional[Iterable[Any]] = None) -> _Issues:
    errors: List[str] = []
    warnings: List[str] = []

    iceberg_exposes = iceberg_sink_exposes(contract)
    expose_ids = {e.get("exposeId") or e.get("id") for e in iceberg_exposes}

    candidates = (contract.get("builds") or []) if builds is None else builds
    for build in candidates:
        if not isinstance(build, Mapping):
            continue
        runtime = iceberg_sink_plan(build)
        if runtime is None:
            continue  # not a build that writes an Iceberg sink
        _check_build(build, runtime, iceberg_exposes, expose_ids, errors, warnings)

    return errors, warnings


def _check_build(
    build: Mapping[str, Any],
    runtime: IcebergSinkPlan,
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

    _check_catalog(
        bid,
        binding,
        sink,
        runtime,
        errors,
        warnings,
        auto_creates=_auto_creates(streaming, runtime),
    )


def _check_catalog(
    bid: Any,
    binding: Mapping[str, Any],
    sink: Mapping[str, Any],
    runtime: IcebergSinkPlan,
    errors: List[str],
    warnings: List[str],
    *,
    auto_creates: bool = False,
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
    # only the expose, so a ``sink.catalog``, an override or a hand-written
    # config that writes another catalog streams into one catalog while the
    # static table and the models live in another. Compared as the catalog the
    # worker builds: ``iceberg-rest``, ``lakekeeper`` and ``type=rest`` are all
    # the REST catalog. ONE error per cause, the most specific first. HARD.
    expose_kind = binding_catalog_kind(binding)
    reaching, origin = _reaching_catalog(info, runtime)
    if _gcp_catalog_unnamed(binding):
        if reaching is not None and reaching != "bigquery":
            _gcp_split(bid, sink, sink_kind, reaching, origin, runtime, errors)
    elif sink_kind and sink_kind != expose_kind:
        errors.append(
            f"iceberg sink (build {bid!r}): sink.catalog {sink.get('catalog')!r} disagrees "
            f"with the expose's catalog {expose_kind!r} ({_expose_source(binding)}); the sink "
            f"would write through {sink_kind} while dbt and the IaC read {expose_kind}. Set "
            f"binding.location.catalog: {sink_kind} and drop sink.catalog"
        )
    elif (
        origin is not None
        and reaching is not None
        and reaching != _wire_catalog(catalog_kind_info(expose_kind))
        and not _carries_both_selectors(kind, info, runtime)
    ):
        label, key, value = origin
        errors.append(
            f"iceberg sink (build {bid!r}): {label} sets {key}={value!r}, so the sink would "
            f"write through {_catalog_noun(reaching)} catalog while dbt and the IaC read the "
            f"expose's catalog {expose_kind!r} ({_expose_source(binding)}). Declare the "
            "catalog the sink writes to in binding.location.catalog; a REST endpoint that "
            "fronts Glue (Glue's Iceberg REST endpoint) is catalog: rest"
        )

    # 4. catalog tagged-union completeness, from the row's ``sink_requires``.
    #    HARD: forge can't derive a REST uri or a catalog name. A warehouse is
    #    present when the deriver resolves one (DynamoDB and JDBC derive it
    #    from an explicit bucket). A key the catalog needs only to start (the
    #    DynamoDB / JDBC warehouse, the BigQuery project) is also present when
    #    an override map sets its catalog property: the runner merges that map
    #    into the config it pushes. A bucket whose ``{{ env.* }}`` template
    #    does not resolve here is explicit, so ``fluid validate`` only warns:
    #    the runner's preflight, in the environment the sink config is
    #    derived in, refuses it when the sink would use that warehouse: a
    #    DynamoDB / JDBC catalog needs one to start, Debezium Server needs one
    #    to boot, and a Kafka Connect BigQuery sink reads it only to
    #    auto-create tables (see the BigQuery warning below). Otherwise the run
    #    warns too. Glue requires nothing (the warehouse falls back), so its
    #    region stays advisory.
    from ...providers._iceberg_catalog import unset_bucket_env_vars

    derived_warehouse = resolve_iceberg_catalog(binding, sink=sink, account_ref="").warehouse
    # Only a deriving build reads the bucket; one that does not pushes the
    # operator's map as written.
    as_written = _binding_as_written(binding)
    waits_on: Tuple[str, ...] = ()
    if runtime.derives and not _overrides_warehouse(runtime):
        waits_on = unset_bucket_env_vars(as_written, kind)
    if waits_on:
        required = "warehouse" in info.sink_requires
        refused = required or runtime.engine == "debezium" or auto_creates
        issue = _unresolved_bucket_issue(
            bid, kind, as_written, waits_on, required=required, refused=refused
        )
        run_only = _AT_RUN.get()
        if run_only is None or not refused:
            warnings.append(issue)
        else:
            errors.append(issue)
            run_only.append(issue)
    for key in info.sink_requires:
        if key == "warehouse" and waits_on:
            continue  # reported above, naming the variables
        present = derived_warehouse if key == "warehouse" else str(loc.get(key) or "").strip()
        start_prop = _start_only_property(key, kind)
        if not present and start_prop is not None:
            present = _overrides_catalog_property(runtime, start_prop)
        if not present:
            errors.append(
                f"iceberg sink (build {bid!r}): {kind} catalog requires "
                f"binding.location.{key}{_requires_hint(key, kind, info)}"
            )
    if info.family == FAMILY_GLUE and not loc.get("region"):
        warnings.append(
            f"iceberg sink (build {bid!r}): glue catalog without binding.location.region; "
            "the connector needs iceberg.catalog.client.region"
        )

    # The stock Apache Iceberg Kafka Connect runtime bundles the AWS, GCP and
    # Azure modules (Hive only in its -hive- distribution) but no iceberg-nessie
    # (apache/iceberg kafka-connect/build.gradle), so ``type=nessie`` fails to
    # load NessieCatalog on a stock worker. Advisory: a custom image may add it.
    if reaching == "nessie" and runtime.engine == "kafka-connect":
        warnings.append(
            f"iceberg sink (build {bid!r}): the stock Apache Iceberg Kafka Connect runtime "
            "does not bundle iceberg-nessie (apache/iceberg kafka-connect/build.gradle); "
            "add the iceberg-nessie jar to the worker's connector plugin directory, or "
            "the sink cannot load NessieCatalog"
        )
    # ``type=bigquery`` (BigQueryMetastoreCatalog) is in Iceberg's CatalogUtil
    # only from 1.10 (apache-iceberg-1.10.0 CatalogUtil.java:78, :321-322; absent
    # at apache-iceberg-1.9.2); the sink published on Confluent Hub is older,
    # so a stock worker fails to load the catalog at connector start.
    # Advisory: a runtime built from Iceberg >= 1.10 has it.
    if reaching == "bigquery" and runtime.engine == "kafka-connect":
        warnings.append(
            f"iceberg sink (build {bid!r}): the sink config sets "
            "iceberg.catalog.type=bigquery, which the published Apache Iceberg Kafka Connect "
            f"sink ({PUBLISHED_KAFKA_CONNECT_SINK_VERSION} on Confluent Hub) cannot load: "
            "Iceberg's CatalogUtil gains the bigquery type in 1.10, so on that sink the "
            "connector fails at start. Run a sink built from Iceberg >= 1.10"
        )
    # BigQueryMetastoreCatalog reads the warehouse to place a dataset it
    # creates, and a table it creates in a dataset with no default storage
    # location URI; without one, both throw IllegalArgumentException
    # (apache-iceberg-1.10.0 BigQueryMetastoreCatalog.java:146-161, :197-208,
    # :298-303). The Kafka Connect sink's auto-create calls createNamespace
    # before every createTable and catches only AlreadyExists / Forbidden
    # (IcebergWriterFactory.java:89, :122-137), so every table auto-create
    # throws, even in a dataset that exists. Debezium Server needs one to boot:
    # IcebergConfig declares ``debezium.sink.iceberg.warehouse`` as a String
    # with no default (memiiso/debezium-server-iceberg 1.2.0.Final
    # IcebergConfig.java:44-45), in the config mapping GlobalConfig nests
    # (GlobalConfig.java:14-15) and IcebergChangeConsumer injects
    # (IcebergChangeConsumer.java:79), and SmallRye Config refuses to start
    # with a required mapping property missing. Advisory; a bucket that waits
    # on a variable is reported above.
    if (
        reaching == "bigquery"
        and kind == "bigquery"
        and runtime.derives
        and not derived_warehouse
        and not waits_on
        and not _overrides_warehouse(runtime)
    ):
        warnings.append(
            f"iceberg sink (build {bid!r}): no gs:// warehouse can be derived from "
            "binding.location (a gs:// warehouse, or a bucket), so the derived bigquery "
            "catalog config sets none. "
            + (
                "Debezium Server declares debezium.sink.iceberg.warehouse with no default "
                "(debezium-server-iceberg 1.2.0.Final IcebergConfig), so the server refuses "
                "to boot without it, whether or not the tables exist. Set "
                "binding.location.warehouse or binding.location.bucket, or warehouse in "
                "debezium.server.sink.config"
                if runtime.engine == "debezium"
                else "With iceberg.tables.auto-create-enabled the sink calls createNamespace "
                "for every table it creates, and BigQueryMetastoreCatalog.createNamespace "
                "refuses without a warehouse, so table auto-creation fails even in a dataset "
                "that exists; without auto-create, tables that exist need none"
            )
        )

    _check_warehouse_override(bid, binding, sink, kind, info, runtime, warnings)
    _check_selector_overrides(bid, kind, info, runtime, errors)


#: The run-only errors of the :func:`iceberg_sink_preflight` that is running,
#: ``None`` under ``fluid validate``. The runner derives the sink config in
#: the same process right after the preflight, so a ``location.bucket``
#: template this environment cannot resolve leaves the sink with no
#: warehouse, or one in a bucket the contract does not name.
_AT_RUN: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar(
    "iceberg_sink_preflight", default=None
)

#: The contract as written, before the acquisition dispatcher resolved its
#: ``{{ env.* }}`` templates (see :func:`contract_as_written`).
_AS_WRITTEN: contextvars.ContextVar[Optional[Mapping[str, Any]]] = contextvars.ContextVar(
    "iceberg_sink_contract_as_written", default=None
)


@contextlib.contextmanager
def contract_as_written(contract: Mapping[str, Any]) -> Iterator[None]:
    """Let :func:`iceberg_sink_preflight` read ``contract`` as written.

    ``base._execute_acquisition_build`` resolves every ``{{ env.X }}`` before
    the runner sees the contract, and a missing or empty ``X`` becomes ``""``:
    ``acme-{{ env.LAKE_ENV }}-lake`` reaches the runner as ``acme--lake``.
    Inside this block the preflight reads the bucket template from
    ``contract``, so it names the variable and refuses the build instead of
    passing a warehouse in a bucket the contract does not name.
    """
    token = _AS_WRITTEN.set(contract)
    try:
        yield
    finally:
        _AS_WRITTEN.reset(token)


def _binding_as_written(binding: Mapping[str, Any]) -> Mapping[str, Any]:
    """``binding`` as the contract wrote it, while a runner runs; else ``binding``.

    The expose binding in the contract :func:`contract_as_written` holds whose
    resolution by the dispatcher's own resolver is ``binding``. Compared with
    strings stripped: a plan-embedded contract is first resolved by
    ``_contract_loader.resolve_contract_env_templates``, which strips every
    string it renders.
    """
    contract = _AS_WRITTEN.get()
    if contract is None or _AT_RUN.get() is None:
        return binding
    return _match_as_written(contract, binding)


def unresolved_bucket_vars_at_run(
    binding: Mapping[str, Any], kind: str, runtime: IcebergSinkPlan
) -> Tuple[str, ...]:
    """The unset or empty variables the derived warehouse waits on, as check 4 reads them.

    For a runner deriving inside :func:`contract_as_written`, where ``binding``
    is already resolved: the variables in its bucket as the contract wrote it.
    ``()`` outside that scope, for a build that does not derive, and when an
    override map sets the warehouse (the override is what reaches the sink).
    """
    contract = _AS_WRITTEN.get()
    if contract is None or not runtime.derives or _overrides_warehouse(runtime):
        return ()
    from ...providers._iceberg_catalog import unset_bucket_env_vars

    return unset_bucket_env_vars(_match_as_written(contract, binding), kind)


def _match_as_written(contract: Mapping[str, Any], binding: Mapping[str, Any]) -> Mapping[str, Any]:
    """The expose binding in ``contract`` that resolves to ``binding``, else ``binding``."""
    from ..base import _resolve_env_placeholders

    target = _stripped(binding)
    for expose in contract.get("exposes") or []:
        raw = expose.get("binding") if isinstance(expose, Mapping) else None
        if isinstance(raw, Mapping) and _stripped(_resolve_env_placeholders(dict(raw))) == target:
            return raw
    return binding


def _stripped(node: Any) -> Any:
    """``node`` with every string leaf stripped of surrounding whitespace."""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, Mapping):
        return {k: _stripped(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_stripped(v) for v in node]
    return node


def _unresolved_bucket_issue(
    bid: Any,
    kind: str,
    binding: Mapping[str, Any],
    names: Tuple[str, ...],
    *,
    required: bool,
    refused: bool,
) -> str:
    """Check 4's message for a warehouse that waits on unset or empty bucket variables."""
    raw = (binding.get("location") or {}).get("bucket")
    one = len(names) == 1
    unset = f"{', '.join(names)} {'is' if one else 'are'} unset or empty"
    refuses = f"the {kind} catalog refuses to start without a warehouse"
    if _AT_RUN.get() is not None:
        return (
            f"iceberg sink (build {bid!r}): the {kind} catalog's warehouse derives from "
            f"binding.location.bucket {raw!r}, and {unset} in the runner's environment, so "
            "the bucket does not resolve: the sink would get a warehouse in a bucket the "
            f"contract does not name, or none{f', and {refuses}' if required else ''}. "
            f"Set {'it' if one else 'them'} here, or set binding.location.warehouse"
        )
    return (
        f"iceberg sink (build {bid!r}): the {kind} catalog's warehouse derives from "
        f"binding.location.bucket {raw!r}, and {unset} here, so it cannot be derived at "
        f"validate time. Set {'it' if one else 'them'} where the sink runs"
        + (
            ": the runner refuses the build when the bucket does not resolve there"
            + (f", because {refuses}" if required else "")
            if refused
            else ""
        )
    )


def _auto_creates(streaming: Mapping[str, Any], runtime: IcebergSinkPlan) -> bool:
    """Does this Kafka Connect sink auto-create tables? Its default is off.

    ``streamingSink.autoCreate`` sets ``iceberg.tables.auto-create-enabled``,
    and an override map merged after it wins (apache-iceberg-1.10.0
    IcebergSinkConfig.java:154-159 defaults the key to false).
    """
    value: Any = streaming.get("autoCreate")
    for _label, cfg in runtime.overrides:
        if "iceberg.tables.auto-create-enabled" in cfg:
            value = cfg["iceberg.tables.auto-create-enabled"]
    return str(value).strip().lower() == "true"


def _requires_hint(key: str, kind: str, info: CatalogKind) -> str:
    """What a missing ``sink_requires`` key is to the sink, for check 4's error."""
    if key == "warehouse" and info.family == FAMILY_REST:
        return " (the catalog name)"
    if key == "warehouse" and kind in BUCKET_WAREHOUSE_KINDS:
        return (
            " (an object-store location), or a binding.location.bucket on platform aws or "
            "gcp to derive it from, or the sink's warehouse property in an override; the "
            f"{kind} catalog refuses to start without a warehouse"
        )
    if key == "project" and kind == "bigquery":
        return (
            f" (the sink's {BIGQUERY_PROJECT_ID}), or that property in an override; the "
            "bigquery catalog refuses to start without it"
        )
    return ""


def _start_only_property(key: str, kind: str) -> Optional[str]:
    """The catalog property behind a ``sink_requires`` key only the sink needs.

    DynamoDbCatalog and JdbcCatalog refuse to start without ``warehouse``, and
    BigQueryMetastoreCatalog without ``gcp.bigquery.project-id`` (the rows in
    ``_iceberg_catalog`` cite the upstream lines), so an override map that sets
    the property supplies what the sink needs. ``None`` for any other key.
    """
    if key == "warehouse" and kind in BUCKET_WAREHOUSE_KINDS:
        return "warehouse"
    if key == "project" and kind == "bigquery":
        return BIGQUERY_PROJECT_ID
    return None


def _overrides_catalog_property(runtime: IcebergSinkPlan, prop: str) -> bool:
    """Does an override map merged over the derived config set catalog property ``prop``?"""
    # A catalog property carries the same runtime prefix as the type key:
    # ``iceberg.catalog.warehouse`` on Kafka Connect, bare ``warehouse`` on
    # Debezium Server.
    key = runtime.type_key[: -len("type")] + prop
    return any(mapping.get(key) for _label, mapping in runtime.overrides)


def _overrides_warehouse(runtime: IcebergSinkPlan) -> bool:
    """Does an override map merged over the derived config set the warehouse?"""
    return _overrides_catalog_property(runtime, "warehouse")


def _reaching_catalog_type(info: Any, runtime: IcebergSinkPlan) -> Optional[str]:
    """The catalog ``type`` the worker actually receives, or ``None``.

    The derived value when the runner derives (``None`` for a catalog reached by
    ``catalog-impl``), then every override map in merge order, the last one
    winning, as the runner merges them. A hand-written config therefore decides
    for itself: its ``type`` is what reaches the worker, not the expose's kind.
    """
    reaching: Optional[str] = None
    if runtime.derives and not info.catalog_impl:
        reaching = info.runtime_type
    for _label, mapping in runtime.overrides:
        if runtime.impl_key in mapping:
            reaching = None
        if runtime.type_key in mapping:
            reaching = str(mapping[runtime.type_key] or "").strip().lower() or None
    return reaching


def _wire_catalog(info: CatalogKind) -> str:
    """The catalog the worker builds from ``info``'s derived selector.

    ``rest`` for every REST-family kind (lakekeeper, polaris, unity,
    snowflake-managed), the kind itself for a native ``type``, and the kind a
    ``catalog-impl`` class is (glue, dynamodb).
    """
    if info.catalog_impl:
        return _IMPL_CATALOGS.get(info.catalog_impl, info.catalog_impl)
    return info.runtime_type or ""


def _reaching_catalog(
    info: CatalogKind, runtime: IcebergSinkPlan
) -> Tuple[Optional[str], Optional[Tuple[str, str, str]]]:
    """The catalog the worker builds, and the override that chose it.

    Starts from the derived kind (``info`` is :func:`iceberg_catalog_kind`'s
    row, so ``sink.catalog`` counts) when the runner derives, then reads every
    override map in merge order, the last one winning: a ``type`` names its
    catalog, and a ``catalog-impl`` is the catalog its class is (a class
    :data:`_IMPL_CATALOGS` does not know stays its own name, which matches no
    kind). ``origin`` is ``(map label, key, value)`` of the override that
    decided, ``None`` when the derived config did; the catalog is ``None``
    when nothing selects one.
    """
    reaching: Optional[str] = _wire_catalog(info) if runtime.derives else None
    origin: Optional[Tuple[str, str, str]] = None
    for label, mapping in runtime.overrides:
        if runtime.impl_key in mapping:
            impl = str(mapping[runtime.impl_key] or "").strip()
            reaching = _IMPL_CATALOGS.get(impl, impl) or None
            origin = (label, runtime.impl_key, impl)
        if runtime.type_key in mapping:
            wire = str(mapping[runtime.type_key] or "").strip().lower()
            reaching = wire or None
            origin = (label, runtime.type_key, wire)
    return reaching, origin


def _catalog_noun(catalog: str) -> str:
    """``a REST`` / ``a glue`` / ``a com.acme.Custom``, for the messages."""
    return f"a {'REST' if catalog == 'rest' else catalog}"


def _expose_source(binding: Mapping[str, Any]) -> str:
    """Where the expose's catalog comes from, for the disagreement messages."""
    loc = binding.get("location") or {}
    if canonical_catalog_kind(loc.get("catalog")):
        return "binding.location.catalog"
    return f"the {binding.get('platform')!r} platform default"


def _gcp_split(
    bid: Any,
    sink: Mapping[str, Any],
    sink_kind: str,
    reaching: str,
    origin: Optional[Tuple[str, str, str]],
    runtime: IcebergSinkPlan,
    errors: List[str],
) -> None:
    """GCP is the one platform where an absent catalog means two things.

    dbt-bigquery and the GCP IaC read only an EXPLICIT ``location.catalog`` and
    create a BigLake metastore table, while the sink writes through whatever
    catalog type reaches the worker (the ``rest`` platform default, a
    ``sink.catalog``, or a hand-written config). Unless that is BigLake too,
    one table is written to two catalogs. Naming the expose's catalog is the only
    way they agree. A catalog reached by ``catalog-impl`` (``sink.catalog:
    glue``, a hand-written ``GlueCatalog``) counts like any other. HARD.
    """
    if not runtime.derives:
        source = "the hand-written sink config"
    elif origin is not None:
        source = origin[0]
    elif sink_kind:
        source = f"sink.catalog {sink.get('catalog')!r}"
    else:
        source = "the 'gcp' platform default"
    errors.append(
        f"iceberg sink (build {bid!r}): the GCP Iceberg expose sets no "
        "binding.location.catalog, so it is read two ways: the sink would write through a "
        f"{'REST' if reaching == 'rest' else reaching} catalog ({source}) while dbt-bigquery and "
        "the GCP IaC, which read only "
        "binding.location.catalog, create a BigLake metastore table. Set "
        "binding.location.catalog: bigquery, or the REST kind your catalog is "
        "(e.g. rest, lakekeeper)"
    )


def _gcp_catalog_unnamed(binding: Mapping[str, Any]) -> bool:
    """Is ``binding`` a GCP Iceberg expose with no ``location.catalog``?"""
    from ...iac.provider_match import canonical_cloud

    loc = binding.get("location") or {}
    return canonical_cloud(binding.get("platform")) == "gcp" and not canonical_catalog_kind(
        loc.get("catalog")
    )


def _check_warehouse_override(
    bid: Any,
    binding: Mapping[str, Any],
    sink: Mapping[str, Any],
    kind: str,
    info: CatalogKind,
    runtime: IcebergSinkPlan,
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
        # The binding has no warehouse to diverge from: check 4 refused it, or
        # (DynamoDB / JDBC) accepted the override as the sink's warehouse.
        return
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
    runtime: IcebergSinkPlan,
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
    setters = _selector_setters(kind, info, runtime)
    if runtime.type_key in setters and runtime.impl_key in setters:
        errors.append(
            f"iceberg sink (build {bid!r}): the sink config would carry both "
            f"{runtime.type_key} (from {setters[runtime.type_key]}) and {runtime.impl_key} "
            f"(from {setters[runtime.impl_key]}); Iceberg's CatalogUtil refuses a catalog "
            "with both type and catalog-impl set, so the sink never starts. Keep one, and "
            "change catalogs with binding.location.catalog rather than an override"
        )


def _carries_both_selectors(kind: str, info: CatalogKind, runtime: IcebergSinkPlan) -> bool:
    """Check 6's condition: the sink never starts, so no catalog reaches it."""
    setters = _selector_setters(kind, info, runtime)
    return runtime.type_key in setters and runtime.impl_key in setters


def _selector_setters(kind: str, info: CatalogKind, runtime: IcebergSinkPlan) -> Dict[str, str]:
    """``{selector key: who set it}`` in the merged config, the last setter winning."""
    setters: Dict[str, str] = {}
    if runtime.derives:
        derived = runtime.impl_key if info.catalog_impl else runtime.type_key
        setters[derived] = f"the derived {kind} catalog config"
    for label, mapping in runtime.overrides:
        for key in (runtime.type_key, runtime.impl_key):
            if key in mapping:
                setters[key] = label
    return setters
