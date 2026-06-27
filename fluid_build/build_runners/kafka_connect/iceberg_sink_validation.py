# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Plan/validate-time checks for the Iceberg streaming sink (RFC §6.8).

These catch the connector's silent-fail-at-first-record traps BEFORE any apply:
a sink with no matching Iceberg expose (the deriver would no-op or mis-target),
an incomplete catalog tagged-union, the v1-deferred upsert mode, dynamic routing
without a route field, and an operator warehouse override that diverges from the
binding (which would split the streaming write from the static Glue table). It is
a pure function returning (errors, warnings) — the validate stage routes errors
to its collector and surfaces warnings, mirroring product_types.py.

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

from typing import Any, List, Mapping, Tuple

_OBJECT_STORE_PREFIXES = ("s3://", "s3a://", "gs://", "abfss://")
# binding.format aliases that normalize to canonical "iceberg" (mirror _common).
_ICEBERG_FORMATS = {"iceberg", "iceberg_table", "iceberg-table"}


def _is_iceberg_binding(exposure: Any) -> bool:
    if not isinstance(exposure, Mapping):
        return False
    binding = exposure.get("binding") or {}
    # A confluent expose is a MANAGED Tableflow output (the Confluent IaC plugin
    # + validate_confluent_binding own it), not a self-managed Kafka-Connect sink
    # target. Excluding it keeps this validator from demanding REST/Glue catalog
    # fields the managed path doesn't use (RFC §15).
    if str(binding.get("platform") or "").lower() == "confluent":
        return False
    return str(binding.get("format") or "").lower() in _ICEBERG_FORMATS


def validate_iceberg_sink(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings) for every Iceberg streaming-sink build."""
    errors: List[str] = []
    warnings: List[str] = []

    exposes = [e for e in (contract.get("exposes") or []) if isinstance(e, Mapping)]
    iceberg_exposes = [e for e in exposes if _is_iceberg_binding(e)]
    expose_ids = {e.get("exposeId") or e.get("id") for e in iceberg_exposes}

    for build in contract.get("builds") or []:
        if not isinstance(build, Mapping):
            continue
        props = build.get("properties") or {}
        sink = props.get("sink") or {}
        if str(sink.get("format") or "").lower() != "iceberg":
            continue  # not an Iceberg sink build

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
            continue
        outputs = build.get("outputs") or []
        if outputs and not (set(outputs) & expose_ids):
            warnings.append(
                f"iceberg sink (build {bid!r}) outputs {list(outputs)} don't reference the "
                f"Iceberg expose(s) {sorted(x for x in expose_ids if x)}; the join is implicit"
            )
        binding = iceberg_exposes[0].get("binding") or {}
        loc = binding.get("location") or {}

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

        # 4. catalog tagged-union completeness. HARD for REST (forge can't derive
        #    a uri/warehouse); advisory for Glue (warehouse falls back).
        catalog_kind = str(
            sink.get("catalog")
            or loc.get("catalog")
            or ("glue" if str(binding.get("platform") or "").lower() == "aws" else "rest")
        ).lower()
        if catalog_kind == "rest":
            if not loc.get("uri"):
                errors.append(
                    f"iceberg sink (build {bid!r}): rest catalog requires binding.location.uri"
                )
            if not loc.get("warehouse"):
                errors.append(
                    f"iceberg sink (build {bid!r}): rest catalog requires "
                    "binding.location.warehouse (the catalog name)"
                )
        elif catalog_kind == "glue":
            if not loc.get("region"):
                warnings.append(
                    f"iceberg sink (build {bid!r}): glue catalog without binding.location.region; "
                    "the connector needs iceberg.catalog.client.region"
                )

        # 5. zero-drift cross-check (consumes PR1's same_warehouse): if the
        #    operator overrides the warehouse, it must still match the binding —
        #    else the streaming write and the static Glue table diverge. Operator
        #    wins (warn, not fail), per the locked decision.
        overrides = kc.get("iceberg_catalog_overrides") or {}
        override_wh = overrides.get("iceberg.catalog.warehouse")
        if override_wh:
            from fluid_build.providers.aws.util.warehouse import (
                get_iceberg_warehouse,
                same_warehouse,
            )

            derived_wh = get_iceberg_warehouse(loc, account_ref="")
            if not same_warehouse(override_wh, derived_wh):
                warnings.append(
                    f"iceberg sink (build {bid!r}): iceberg_catalog_overrides warehouse "
                    f"{override_wh!r} diverges from the binding warehouse {derived_wh!r}; "
                    "the connector will use the override but the static Glue table may differ"
                )

    return errors, warnings
