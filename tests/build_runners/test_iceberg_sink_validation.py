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

"""PR4 — plan/validate-time checks for the Iceberg streaming sink."""

from __future__ import annotations

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    PUBLISHED_KAFKA_CONNECT_SINK_VERSION,
    executing_build,
    iceberg_sink_plan,
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.providers._iceberg_catalog import (
    DYNAMODB_CATALOG_IMPL,
    FAMILY_REST,
    GLUE_CATALOG_IMPL,
    canonical_catalog_kind,
    catalog_kind_info,
    known_catalog_kinds,
)

pytestmark = [pytest.mark.unit]


def _contract(*, binding=None, kc=None, outputs=None, sink_format="iceberg", with_expose=True):
    binding = binding or {
        "platform": "aws",
        "format": "iceberg",
        "location": {"database": "s", "table": "o", "bucket": "lake", "region": "us-east-1"},
    }
    build = {
        "id": "ingest",
        "pattern": "acquisition",
        "engine": "kafka-connect",
        "properties": {
            "source": {"kind": "postgres", "mode": "incremental_append"},
            "sink": {"format": sink_format},
            "kafka-connect": kc or {},
        },
    }
    if outputs is not None:
        build["outputs"] = outputs
    c = {"id": "b.x", "builds": [build], "exposes": []}
    if with_expose:
        c["exposes"] = [{"exposeId": "events", "kind": "table", "binding": binding}]
    return c


def _errs(contract):
    return validate_iceberg_sink(contract)[0]


def _warns(contract):
    return validate_iceberg_sink(contract)[1]


# ── happy path ──────────────────────────────────────────────────────────────


def test_valid_glue_iceberg_sink_is_clean():
    errors, warnings = validate_iceberg_sink(_contract())
    assert errors == []
    assert warnings == []


def test_non_iceberg_sink_is_ignored():
    assert validate_iceberg_sink(_contract(sink_format="parquet", with_expose=False)) == ([], [])


# ── build -> expose join ────────────────────────────────────────────────────


def test_iceberg_sink_without_iceberg_expose_errors():
    errs = _errs(_contract(with_expose=False))
    assert any("no expose with binding.format=iceberg" in e for e in errs)


def test_outputs_not_referencing_iceberg_expose_errors_for_a_derived_sink():
    # A derived sink writes the expose its outputs name, so outputs that name
    # none leave it no table; this was a warning, and the sink wrote exposes[0].
    contract = _contract(outputs=["something_else"])
    assert any(
        "name none of the Iceberg sink exposes ['events (s.o)']" in e for e in _errs(contract)
    )
    assert _warns(contract) == []


def test_outputs_not_referencing_iceberg_expose_warns_for_a_hand_written_sink():
    hand_written = {"sink_connector_config": {"iceberg.tables": "s.o"}}
    contract = _contract(outputs=["something_else"], kc=hand_written)
    assert _errs(contract) == []
    assert any("don't reference the Iceberg expose" in w for w in _warns(contract))


def test_iceberg_table_alias_binding_counts_as_iceberg_expose():
    # the expose uses the iceberg_table alias -> still recognized (no error)
    binding = {
        "platform": "aws",
        "format": "iceberg_table",
        "location": {"database": "s", "table": "o", "bucket": "lake", "region": "us-east-1"},
    }
    assert _errs(_contract(binding=binding)) == []


# ── upsert / routing gates ──────────────────────────────────────────────────


def test_upsert_mode_rejected_in_v1():
    errs = _errs(_contract(kc={"streamingSink": {"upsertMode": True}}))
    assert any("upsertMode is not supported in v1" in e for e in errs)


def test_dynamic_routing_requires_route_field():
    errs = _errs(_contract(kc={"streamingSink": {"dynamicEnabled": True}}))
    assert any("requires streamingSink.routeField" in e for e in errs)


def test_dynamic_routing_with_route_field_ok():
    errs = _errs(_contract(kc={"streamingSink": {"dynamicEnabled": True, "routeField": "tbl"}}))
    assert errs == []


# ── catalog tagged-union completeness ───────────────────────────────────────


def test_rest_catalog_requires_uri_and_warehouse():
    binding = {
        "platform": "local",
        "format": "iceberg",
        "location": {"database": "default", "table": "events", "catalog": "rest"},
    }
    errs = _errs(_contract(binding=binding))
    assert any("rest catalog requires binding.location.uri" in e for e in errs)
    assert any("rest catalog requires binding.location.warehouse" in e for e in errs)


def test_rest_catalog_complete_ok():
    binding = {
        "platform": "local",
        "format": "iceberg",
        "location": {
            "database": "default",
            "table": "events",
            "catalog": "rest",
            "uri": "http://iceberg:8181",
            "warehouse": "s3://bucket/warehouse/",
        },
    }
    assert _errs(_contract(binding=binding)) == []


def test_glue_without_region_warns():
    binding = {
        "platform": "aws",
        "format": "iceberg",
        "location": {"database": "s", "table": "o", "bucket": "lake"},  # no region
    }
    warns = _warns(_contract(binding=binding))
    assert any("without binding.location.region" in w for w in warns)


# ── zero-drift cross-check (consumes same_warehouse) ────────────────────────


def test_override_warehouse_divergence_warns():
    warns = _warns(
        _contract(kc={"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://other/"}})
    )
    assert any("diverges from the binding warehouse" in w for w in warns)


def test_override_warehouse_matching_is_clean():
    # override equal to the derived warehouse (s3://lake/s/o/) -> no warning
    warns = _warns(
        _contract(kc={"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://lake/s/o/"}})
    )
    assert not any("diverges" in w for w in warns)


# ── managed Confluent Tableflow exposes are not self-managed KC sink targets ──


def test_confluent_expose_not_treated_as_kc_sink_target():
    # a confluent expose is managed Tableflow (the Confluent IaC plugin owns it),
    # so this validator must NOT claim it and demand REST/Glue catalog fields.
    binding = {
        "platform": "confluent",
        "format": "iceberg",
        "location": {
            "environment_id": "env-1",
            "kafka_cluster_id": "lkc-1",
            "bucket": "b",
            "confluent_role_arn": "arn:aws:iam::1:role/x",
            "database": "d",
            "table": "t",
        },
    }
    errs = _errs(_contract(binding=binding))
    # the only expose is confluent -> excluded -> a real iceberg KC sink build
    # correctly reports "no iceberg expose" rather than a bogus rest-catalog error
    assert any("no expose with binding.format=iceberg" in e for e in errs)
    assert not any("rest catalog requires" in e for e in errs)


# ── table-driven catalog checks (one classification for every emitter) ──────
#
# Before the shared kind table this validator checked only the literal "rest",
# so `catalog: lakekeeper` skipped every check, streamed over REST, and dbt
# wrote a Snowflake-managed table for the same expose. Every case below derives
# its expectation from the table row, so a new kind is covered by construction.

CANONICAL_KINDS = sorted({canonical_catalog_kind(k) for k in known_catalog_kinds()})
REQUIRING_KINDS = [k for k in CANONICAL_KINDS if catalog_kind_info(k).sink_requires]
COMPLETE = {"uri": "http://lakekeeper:8181/catalog", "warehouse": "analytics"}


def _loc_binding(catalog=None, *, platform="local", **location):
    loc = {"database": "default", "table": "events", **location}
    if catalog is not None:
        loc["catalog"] = catalog
    return {"platform": platform, "format": "iceberg", "location": loc}


def _sink_contract(binding, *, sink=None, kc=None, engine="kafka-connect", debezium=None):
    """A one-build contract whose sink block, engine and runtime props vary."""
    props = {
        "source": {"kind": "postgres", "mode": "incremental_append"},
        "sink": {"format": "iceberg", **(sink or {})},
    }
    if engine == "debezium":
        props["debezium"] = debezium if debezium is not None else {}
    else:
        props["kafka-connect"] = kc or {}
    return {
        "id": "b.x",
        "builds": [
            {"id": "ingest", "pattern": "acquisition", "engine": engine, "properties": props}
        ],
        "exposes": [{"exposeId": "events", "kind": "table", "binding": binding}],
    }


def _embedded(sink_block=None):
    return {"deployment": {"mode": "embedded"}, "server": {"sink": sink_block or {}}}


@pytest.mark.parametrize("kind", REQUIRING_KINDS)
@pytest.mark.parametrize("missing", ["uri", "warehouse"])
def test_every_requiring_kind_demands_its_location_keys(kind, missing):
    row = catalog_kind_info(kind)
    location = {k: v for k, v in COMPLETE.items() if k != missing}
    errs = _errs(_sink_contract(_loc_binding(kind, **location)))
    demanded = [e for e in errs if f"{kind} catalog requires binding.location.{missing}" in e]
    assert bool(demanded) == (missing in row.sink_requires), errs


@pytest.mark.parametrize("kind", [k for k in CANONICAL_KINDS if k not in REQUIRING_KINDS])
def test_kinds_requiring_nothing_demand_nothing(kind):
    errs = _errs(_sink_contract(_loc_binding(kind, platform="aws", bucket="lake")))
    assert not any("catalog requires" in e for e in errs), errs


@pytest.mark.parametrize("kind", ["rest", "lakekeeper", "polaris", "unity"])
def test_rest_family_warehouse_message_names_the_catalog_name(kind):
    assert catalog_kind_info(kind).family == FAMILY_REST
    errs = _errs(_sink_contract(_loc_binding(kind, uri="http://c:8181/catalog")))
    assert any(
        f"{kind} catalog requires binding.location.warehouse (the catalog name)" in e for e in errs
    ), errs


def test_complete_lakekeeper_is_clean():
    assert validate_iceberg_sink(_sink_contract(_loc_binding("lakekeeper", **COMPLETE))) == (
        [],
        [],
    )


def test_lakekeeper_used_to_skip_every_check():
    # The bug: no uri and no warehouse, and the old literal-"rest" check passed it.
    errs = _errs(_sink_contract(_loc_binding("lakekeeper")))
    assert any("lakekeeper catalog requires binding.location.uri" in e for e in errs)
    assert any("lakekeeper catalog requires binding.location.warehouse" in e for e in errs)


# ── sink.catalog must agree with the expose (dbt + IaC read only the expose) ──


@pytest.mark.parametrize(
    "sink_catalog, binding",
    [
        # AWS expose with no catalog is Glue (the platform default).
        ("lakekeeper", _loc_binding(None, platform="aws", bucket="lake", region="us-east-1")),
        ("rest", _loc_binding("lakekeeper", **COMPLETE)),
        ("nessie", _loc_binding("rest", uri="http://c:8181", warehouse="s3://b/w")),
    ],
)
def test_sink_catalog_disagreeing_with_expose_errors(sink_catalog, binding):
    errs = _errs(_sink_contract(binding, sink={"catalog": sink_catalog}))
    hit = [e for e in errs if "disagrees with the expose's catalog" in e]
    assert hit, errs
    assert f"binding.location.catalog: {canonical_catalog_kind(sink_catalog)}" in hit[0]


def test_disagreement_names_the_platform_default_when_expose_has_no_catalog():
    binding = _loc_binding(None, platform="aws", bucket="lake", region="us-east-1")
    errs = _errs(_sink_contract(binding, sink={"catalog": "lakekeeper"}))
    assert any("'glue'" in e and "platform default" in e for e in errs), errs


@pytest.mark.parametrize(
    "sink_catalog, location_catalog",
    [
        ("iceberg-rest", "rest"),
        ("ICEBERG_REST", "rest"),
        ("rest", "iceberg_rest"),
        ("snowflake", "snowflake_managed"),
        ("Lakekeeper", "lakekeeper"),
    ],
)
def test_aliases_of_the_same_catalog_agree(sink_catalog, location_catalog):
    binding = _loc_binding(location_catalog, **COMPLETE)
    errs = _errs(_sink_contract(binding, sink={"catalog": sink_catalog}))
    assert not any("disagrees" in e for e in errs), errs


# ── unknown kinds are refused, never guessed ────────────────────────────────


@pytest.mark.parametrize("where", ["location", "sink"])
def test_unknown_kind_errors_naming_value_and_listing_kinds(where):
    if where == "location":
        contract = _sink_contract(_loc_binding("lakekeper", **COMPLETE))
    else:
        contract = _sink_contract(
            _loc_binding("lakekeeper", **COMPLETE), sink={"catalog": "gravitino"}
        )
    errs = _errs(contract)
    unknown = [e for e in errs if "unknown Iceberg catalog" in e]
    assert len(unknown) == 1, errs
    value, field = (
        ("'lakekeper'", "binding.location.catalog")
        if where == "location"
        else (
            "'gravitino'",
            "sink.catalog",
        )
    )
    assert value in unknown[0] and field in unknown[0]
    for kind in known_catalog_kinds():
        assert kind in unknown[0]
    # No per-kind guesses about a row that does not exist, and a typo is never
    # offered back as the remedy.
    assert not any("catalog requires" in e for e in errs)
    assert not any("disagrees" in e for e in errs)


def test_generic_rest_accepts_an_object_store_warehouse():
    # Plain REST catalogs (the apache/iceberg-rest-fixture) take an s3:// warehouse.
    binding = _loc_binding("rest", uri="http://iceberg:8181", warehouse="s3://bucket/warehouse/")
    assert validate_iceberg_sink(_sink_contract(binding)) == ([], [])


# ── runtime support: the stock KC runtime has no Nessie client ──────────────


def test_nessie_on_kafka_connect_warns_about_the_missing_runtime_jar():
    binding = _loc_binding("nessie", uri="http://nessie:19120/api/v2", warehouse="s3://b/w")
    errors, warnings = validate_iceberg_sink(_sink_contract(binding))
    assert errors == []
    assert any("does not bundle iceberg-nessie" in w for w in warnings), warnings


def test_nessie_on_debezium_server_has_no_kafka_connect_warning():
    binding = _loc_binding("nessie", uri="http://nessie:19120/api/v2", warehouse="s3://b/w")
    contract = _sink_contract(binding, engine="debezium", debezium=_embedded())
    assert not any("iceberg-nessie" in w for w in _warns(contract))


# ── check 5: the warehouse override compares against the RESOLVED warehouse ─


def test_rest_override_equal_to_binding_warehouse_is_clean():
    # Regression: the override was compared against the Glue writer's
    # s3://<bucket>/<db>/<table>/ even for REST, so a MATCHING override "diverged".
    binding = _loc_binding("lakekeeper", **COMPLETE)
    kc = {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "analytics"}}
    assert validate_iceberg_sink(_sink_contract(binding, kc=kc)) == ([], [])


def test_rest_override_divergence_names_kind_and_not_glue():
    binding = _loc_binding("lakekeeper", **COMPLETE)
    kc = {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "other"}}
    warns = _warns(_sink_contract(binding, kc=kc))
    hit = [w for w in warns if "diverges from the binding warehouse" in w]
    assert len(hit) == 1, warns
    assert "lakekeeper catalog" in hit[0]
    assert "Glue" not in hit[0]


def test_glue_override_divergence_message_is_unchanged():
    # Byte-identical for Glue on AWS: the text operators and tooling already see.
    warns = _warns(
        _contract(kc={"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://other/"}})
    )
    assert warns == [
        "iceberg sink (build 'ingest'): iceberg_catalog_overrides warehouse 's3://other/' "
        "diverges from the binding warehouse 's3://lake/s/o/'; the connector will use the "
        "override but the static Glue table may differ"
    ]


def test_glue_region_warning_is_unchanged():
    binding = {
        "platform": "aws",
        "format": "iceberg",
        "location": {"database": "s", "table": "o", "bucket": "lake"},
    }
    assert _warns(_contract(binding=binding)) == [
        "iceberg sink (build 'ingest'): glue catalog without binding.location.region; "
        "the connector needs iceberg.catalog.client.region"
    ]


def test_missing_required_warehouse_is_not_also_reported_as_divergence():
    binding = _loc_binding("lakekeeper", uri="http://c:8181/catalog")
    kc = {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "analytics"}}
    errors, warnings = validate_iceberg_sink(_sink_contract(binding, kc=kc))
    assert any("requires binding.location.warehouse" in e for e in errors)
    assert not any("diverges" in w for w in warnings)


# ── check 6: type XOR catalog-impl (CatalogUtil refuses both) ───────────────

_GLUE_AWS = _loc_binding(None, platform="aws", bucket="lake", region="us-east-1")
_REST = _loc_binding("rest", uri="http://iceberg:8181", warehouse="s3://b/w")


@pytest.mark.parametrize(
    "binding, kc, conflict",
    [
        # derived Glue catalog-impl + an override type
        (_GLUE_AWS, {"iceberg_catalog_overrides": {"iceberg.catalog.type": "rest"}}, True),
        # derived REST type + an override catalog-impl
        (_REST, {"iceberg_catalog_overrides": {"iceberg.catalog.catalog-impl": "x.Y"}}, True),
        # presence is what CatalogUtil checks: an empty value still trips it
        (_GLUE_AWS, {"iceberg_catalog_overrides": {"iceberg.catalog.type": ""}}, True),
        # overriding the SAME key the deriver emits is fine
        (
            _GLUE_AWS,
            {"iceberg_catalog_overrides": {"iceberg.catalog.catalog-impl": GLUE_CATALOG_IMPL}},
            False,
        ),
        (_REST, {"iceberg_catalog_overrides": {"iceberg.catalog.type": "rest"}}, False),
        # a hand-written sink config merged over a derived one (opt-in)
        (
            _GLUE_AWS,
            {
                "iceberg_sink_enabled": True,
                "sink_connector_config": {"iceberg.catalog.type": "glue"},
            },
            True,
        ),
        # derivation OFF: catalog overrides are never applied, only the hand-written map
        (
            _GLUE_AWS,
            {
                "sink_connector_config": {"topics": "t"},
                "iceberg_catalog_overrides": {"iceberg.catalog.type": "rest"},
            },
            False,
        ),
        (
            _GLUE_AWS,
            {
                "sink_connector_config": {
                    "iceberg.catalog.type": "glue",
                    "iceberg.catalog.catalog-impl": "x.Y",
                }
            },
            True,
        ),
    ],
)
def test_overrides_must_not_set_both_catalog_selectors(binding, kc, conflict):
    errs = _errs(_sink_contract(binding, kc=kc))
    hit = [e for e in errs if "would carry both" in e]
    assert bool(hit) == conflict, errs
    if conflict:
        assert "iceberg.catalog.type" in hit[0] and "iceberg.catalog.catalog-impl" in hit[0]
        assert "CatalogUtil" in hit[0]


def test_debezium_server_selector_conflict_uses_bare_keys():
    contract = _sink_contract(
        _GLUE_AWS,
        engine="debezium",
        debezium=_embedded({"iceberg_sink_enabled": True, "config": {"type": "rest"}}),
    )
    hit = [e for e in _errs(contract) if "would carry both" in e]
    assert len(hit) == 1
    assert "type (from debezium.server.sink.config)" in hit[0]
    assert "catalog-impl (from the derived glue catalog config)" in hit[0]


# ── Debezium: only a build that derives a sink is checked ───────────────────

_BROKEN = _loc_binding("lakekeeper")  # no uri, no warehouse


@pytest.mark.parametrize("mode", ["bring-your-own", "managed", None])
def test_debezium_source_only_modes_draw_no_sink_findings(mode):
    debezium = {"deployment": {"mode": mode}} if mode else {}
    contract = _sink_contract(_BROKEN, engine="debezium", debezium=debezium)
    assert validate_iceberg_sink(contract) == ([], [])
    contract["exposes"] = []  # not even the missing-expose join applies
    assert validate_iceberg_sink(contract) == ([], [])


def test_debezium_embedded_iceberg_sink_is_checked():
    contract = _sink_contract(_BROKEN, engine="debezium", debezium=_embedded())
    assert any("lakekeeper catalog requires binding.location.uri" in e for e in _errs(contract))


def test_debezium_embedded_deriving_build_is_checked_without_sink_format():
    # The Debezium runner derives from server.sink.type and never reads
    # sink.format, so selecting by sink.format alone let this build skip.
    contract = _sink_contract(_BROKEN, engine="debezium", debezium=_embedded())
    contract["builds"][0]["properties"].pop("sink")
    assert any("lakekeeper catalog requires binding.location.uri" in e for e in _errs(contract))


def test_debezium_embedded_non_iceberg_server_sink_is_not_checked():
    contract = _sink_contract(
        _BROKEN, engine="debezium", debezium=_embedded({"type": "s3", "config": {"a": "b"}})
    )
    assert validate_iceberg_sink(contract) == ([], [])


@pytest.mark.parametrize("enabled", [None, True])
def test_debezium_embedded_warehouse_override_is_cross_checked(enabled):
    sink_block = {"config": {"warehouse": "s3://elsewhere/wh"}}
    if enabled is not None:
        sink_block["iceberg_sink_enabled"] = enabled
    contract = _sink_contract(_GLUE_AWS, engine="debezium", debezium=_embedded(sink_block))
    warns = [w for w in _warns(contract) if "diverges from the binding warehouse" in w]
    assert len(warns) == 1
    assert warns[0].startswith(
        "iceberg sink (build 'ingest'): debezium.server.sink.config warehouse 's3://elsewhere/wh'"
    )
    assert "static Glue table" in warns[0]


def test_debezium_embedded_matching_warehouse_override_is_clean():
    sink_block = {
        "iceberg_sink_enabled": True,
        "config": {"warehouse": "s3://lake/default/events/"},
    }
    contract = _sink_contract(_GLUE_AWS, engine="debezium", debezium=_embedded(sink_block))
    assert not any("diverges" in w for w in _warns(contract))


# ── runner preflight: ONE build's errors, warnings logged ───────────────────


def test_preflight_selects_only_the_named_build():
    contract = _sink_contract(_loc_binding("lakekeeper", **COMPLETE))
    bad = contract["builds"][0]
    bad["properties"]["kafka-connect"] = {"streamingSink": {"upsertMode": True}}
    good = {**bad, "id": "good", "properties": {**bad["properties"], "kafka-connect": {}}}
    contract["builds"].append(good)
    # `fluid validate` sees the one defect ...
    assert len(_errs(contract)) == 1
    # ... and each runner sees only its own build's.
    msg = iceberg_sink_preflight(contract, "ingest")
    assert msg is not None and "build 'ingest'" in msg and "upsertMode" in msg
    assert iceberg_sink_preflight(contract, "good") is None


def test_preflight_clean_build_returns_none_and_logs_warnings(caplog):
    binding = _loc_binding("nessie", uri="http://nessie:19120/api/v2", warehouse="s3://b/w")
    with caplog.at_level("WARNING", logger="fluid.acquire.iceberg_sink"):
        assert iceberg_sink_preflight(_sink_contract(binding), "ingest") is None
    assert any("iceberg-nessie" in r.getMessage() for r in caplog.records)


def test_preflight_ignores_unknown_build_ids():
    assert iceberg_sink_preflight(_sink_contract(_BROKEN), "not-this-one") is None


def test_preflight_matches_the_run_context_id_of_an_id_less_build():
    contract = _sink_contract(_BROKEN)
    del contract["builds"][0]["id"]
    # build_acquisition_run_context ids it "unknown"; the message keeps "?".
    msg = iceberg_sink_preflight(contract, "unknown")
    assert msg is not None and "build '?'" in msg


# ── ONE gate: the plan validate, the preflight and both runners read ─────────


def test_executing_build_selects_by_the_run_context_id():
    contract = _sink_contract(_GLUE_AWS)
    first = contract["builds"][0]
    second = {**first, "id": "second"}
    contract["builds"].append(second)
    assert executing_build(contract, "ingest") is first
    assert executing_build(contract, "second") is second
    assert executing_build(contract, "absent") is None
    del first["id"]
    assert executing_build(contract, "unknown") is first  # run-context spelling
    first["id"] = None
    assert executing_build(contract, None) is first


@pytest.mark.parametrize(
    "kc, derives",
    [
        ({}, True),
        ({"sink_connector_config": {"topics": "t"}}, False),
        ({"sink_connector_config": {"topics": "t"}, "iceberg_sink_enabled": True}, True),
        ({"iceberg_sink_enabled": False}, False),
    ],
)
def test_kafka_connect_plan_covers_derived_and_handwritten_sinks(kc, derives):
    build = _sink_contract(_GLUE_AWS, kc=kc)["builds"][0]
    plan = iceberg_sink_plan(build)
    assert plan is not None and plan.derives is derives


def test_non_iceberg_kafka_connect_build_has_no_plan():
    build = _sink_contract(_GLUE_AWS, sink={"format": "parquet"})["builds"][0]
    assert iceberg_sink_plan(build) is None


@pytest.mark.parametrize(
    "dbz, derives",
    [
        (_embedded(), True),
        (_embedded({"config": {"catalog.name": "x"}}), False),
        ({"deployment": {"mode": "bring-your-own"}}, None),
        (_embedded({"type": "s3"}), None),
    ],
    ids=["derived", "handwritten", "source-only", "non-iceberg-server-sink"],
)
def test_debezium_plan_covers_derived_and_handwritten_sinks(dbz, derives):
    build = _sink_contract(_GLUE_AWS, engine="debezium", debezium=dbz)["builds"][0]
    plan = iceberg_sink_plan(build)
    assert (None if plan is None else plan.derives) is derives


@pytest.mark.parametrize("kc", [{}, {"sink_connector_config": {"topics": "t"}}])
def test_preflight_refuses_a_handwritten_sink_validate_refuses(kc):
    contract = _sink_contract(_BROKEN, kc=kc)
    assert _errs(contract)
    msg = iceberg_sink_preflight(contract, "ingest")
    assert msg is not None and "lakekeeper catalog requires binding.location.uri" in msg


# ── GCP: an unnamed catalog is read two ways ────────────────────────────────

_GCP_REST_LOC = {"uri": "https://rest.example/catalog", "warehouse": "gs://acme-lake/wh"}


@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_gcp_expose_with_no_catalog_errors_naming_both_readings(engine):
    contract = _sink_contract(
        _loc_binding(platform="gcp", **_GCP_REST_LOC),
        engine=engine,
        debezium=_embedded() if engine == "debezium" else None,
    )
    hit = [e for e in _errs(contract) if "sets no binding.location.catalog" in e]
    assert len(hit) == 1, _errs(contract)
    msg = hit[0]
    assert "a REST catalog (the 'gcp' platform default)" in msg
    assert "dbt-bigquery and the GCP IaC" in msg and "BigLake metastore table" in msg
    assert "Set binding.location.catalog: bigquery" in msg and "lakekeeper" in msg


def test_gcp_rest_sink_catalog_does_not_name_the_expose_catalog():
    # dbt-bigquery and the IaC never read sink.catalog, so it cannot settle it.
    contract = _sink_contract(
        _loc_binding(platform="gcp", **_GCP_REST_LOC), sink={"catalog": "rest"}
    )
    hit = [e for e in _errs(contract) if "sets no binding.location.catalog" in e]
    assert len(hit) == 1 and "a REST catalog (sink.catalog 'rest')" in hit[0]


def test_gcp_sink_catalog_bigquery_agrees_with_biglake_and_warns_the_runtime():
    # sink.catalog bigquery reaches BigLake, and so do dbt-bigquery and the GCP
    # IaC for an absent location.catalog: one catalog, so no split and no
    # "disagrees with 'rest'" error (dbt and the IaC never read 'rest' here).
    # The published sink still cannot load type=bigquery, which is a warning.
    contract = _sink_contract(
        _loc_binding(platform="gcp", warehouse="gs://acme/wh", project="acme-proj"),
        sink={"catalog": "bigquery"},
    )
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    assert any("iceberg.catalog.type=bigquery" in w for w in warnings), warnings


def test_gcp_handwritten_rest_sink_is_a_split_named_after_the_handwritten_config():
    contract = _sink_contract(
        _loc_binding(platform="gcp", **_GCP_REST_LOC),
        kc={"sink_connector_config": {"iceberg.catalog.type": "rest"}},
    )
    hit = [e for e in _errs(contract) if "sets no binding.location.catalog" in e]
    assert len(hit) == 1 and "a REST catalog (the hand-written sink config)" in hit[0], hit


def test_gcp_handwritten_bigquery_sink_is_not_a_split():
    contract = _sink_contract(
        _loc_binding(platform="gcp", warehouse="gs://acme/wh"),
        kc={"sink_connector_config": {"iceberg.catalog.type": "bigquery"}},
    )
    assert not any("sets no binding.location.catalog" in e for e in _errs(contract))


def test_bigquery_warning_follows_the_type_that_reaches_the_worker():
    # A hand-written config that sets type=rest over a bigquery expose sends
    # REST to the worker, so the "cannot load bigquery" warning would be false.
    contract = _sink_contract(
        _loc_binding("bigquery", platform="gcp", warehouse="gs://acme/wh"),
        kc={"sink_connector_config": {"iceberg.catalog.type": "rest"}},
    )
    errors, warnings = validate_iceberg_sink(contract)
    assert not any("iceberg.catalog.type=bigquery" in w for w in warnings), warnings
    # ... and REST is not the bigquery catalog dbt and the IaC read (JRN-707-2).
    assert any("Declare the catalog the sink writes to" in e for e in errors), errors


@pytest.mark.parametrize(
    "catalog, location",
    [
        ("bigquery", {"warehouse": "gs://acme/wh", "project": "acme-proj"}),
        ("rest", _GCP_REST_LOC),
        ("lakekeeper", COMPLETE),
    ],
)
def test_gcp_expose_naming_its_catalog_is_not_refused(catalog, location):
    errs = _errs(_sink_contract(_loc_binding(catalog, platform="gcp", **location)))
    assert errs == []


@pytest.mark.parametrize(
    "sink, kc, reaching, source",
    [
        # glue is in the acquisitionSink.catalog schema enum, reached by catalog-impl
        ({"catalog": "glue"}, None, "a glue", "sink.catalog 'glue'"),
        (
            None,
            {"sink_connector_config": {"iceberg.catalog.catalog-impl": GLUE_CATALOG_IMPL}},
            "a glue",
            "the hand-written sink config",
        ),
        (
            None,
            {"sink_connector_config": {"iceberg.catalog.catalog-impl": DYNAMODB_CATALOG_IMPL}},
            "a dynamodb",
            "the hand-written sink config",
        ),
        (
            None,
            {"sink_connector_config": {"iceberg.catalog.catalog-impl": "com.acme.LakeCatalog"}},
            "a com.acme.LakeCatalog",
            "the hand-written sink config",
        ),
        (
            {"catalog": "bigquery"},
            {"iceberg_catalog_overrides": {"iceberg.catalog.type": "rest"}},
            "a REST",
            "iceberg_catalog_overrides",
        ),
    ],
    ids=[
        "sink-catalog-glue",
        "handwritten-glue-impl",
        "handwritten-dynamodb-impl",
        "handwritten-custom-impl",
        "override-type-over-sink-catalog",
    ],
)
def test_gcp_split_covers_a_catalog_reached_by_catalog_impl(sink, kc, reaching, source):
    """CON-2 / LOGIC-2: the gate used to key on the wire ``type``, which is
    absent for a catalog reached by ``catalog-impl``, so the sink committed to
    Glue (or DynamoDB, or a custom class) while dbt-bigquery and the GCP IaC
    created a BigLake table. Every catalog but BigLake is refused."""
    contract = _sink_contract(
        _loc_binding(platform="gcp", warehouse="gs://acme/wh"), sink=sink, kc=kc
    )
    errs = _errs(contract)
    hit = [e for e in errs if "sets no binding.location.catalog" in e]
    assert len(hit) == 1, errs
    assert f"{reaching} catalog ({source})" in hit[0]
    assert not any("disagrees" in e or "Declare the catalog" in e for e in errs), errs
    assert iceberg_sink_preflight(contract, "ingest") is not None


@pytest.mark.parametrize(
    "kc",
    [
        {"sink_connector_config": {"iceberg.catalog.type": "bigquery"}},
        {
            "sink_connector_config": {
                "iceberg.catalog.catalog-impl": (
                    "org.apache.iceberg.gcp.bigquery.BigQueryMetastoreCatalog"
                )
            }
        },
    ],
    ids=["type", "catalog-impl"],
)
def test_gcp_handwritten_biglake_is_not_a_split_by_type_or_impl(kc):
    contract = _sink_contract(_loc_binding(platform="gcp", warehouse="gs://acme/wh"), kc=kc)
    assert not any("sets no binding.location.catalog" in e for e in _errs(contract))


# ── a hand-written selector must agree with a NAMED expose catalog ───────────


def _disagreements(contract):
    return [e for e in _errs(contract) if "Declare the catalog the sink writes to" in e]


@pytest.mark.parametrize(
    "binding, kc, engine, label, setting, expose",
    [
        # following the GCP split error's remedy, while the config still writes REST
        (
            _loc_binding("bigquery", platform="gcp", warehouse="gs://acme/wh"),
            {"sink_connector_config": {"iceberg.catalog.type": "rest"}},
            "kafka-connect",
            "sink_connector_config",
            "iceberg.catalog.type='rest'",
            "'bigquery' (binding.location.catalog)",
        ),
        # the AWS Glue default, written over REST (a REST endpoint fronting Glue)
        (
            _GLUE_AWS,
            {"sink_connector_config": {"iceberg.catalog.type": "rest"}},
            "kafka-connect",
            "sink_connector_config",
            "iceberg.catalog.type='rest'",
            "'glue' (the 'aws' platform default)",
        ),
        # an override over a derived REST config
        (
            _loc_binding("lakekeeper", **COMPLETE),
            {"iceberg_catalog_overrides": {"iceberg.catalog.type": "nessie"}},
            "kafka-connect",
            "iceberg_catalog_overrides",
            "iceberg.catalog.type='nessie'",
            "'lakekeeper' (binding.location.catalog)",
        ),
        # the Snowflake-managed default, written through Glue
        (
            _loc_binding(platform="snowflake", **COMPLETE),
            {"sink_connector_config": {"iceberg.catalog.catalog-impl": GLUE_CATALOG_IMPL}},
            "kafka-connect",
            "sink_connector_config",
            f"iceberg.catalog.catalog-impl='{GLUE_CATALOG_IMPL}'",
            "'snowflake-managed' (the 'snowflake' platform default)",
        ),
        (
            _loc_binding("bigquery", platform="gcp", warehouse="gs://acme/wh"),
            None,
            "debezium",
            "debezium.server.sink.config",
            "type='rest'",
            "'bigquery' (binding.location.catalog)",
        ),
    ],
    ids=["gcp-remedy-followed", "aws-glue-default", "override", "snowflake-default", "debezium"],
)
def test_a_handwritten_catalog_that_differs_from_the_named_one_is_refused(
    binding, kc, engine, label, setting, expose
):
    """JRN-707-2: the sink would write one catalog while dbt and the IaC read
    another, and validate and the preflight both passed."""
    debezium = _embedded({"config": {"type": "rest", "uri": "http://lk:8181/catalog"}})
    contract = _sink_contract(
        binding, kc=kc, engine=engine, debezium=debezium if engine == "debezium" else None
    )
    hit = _disagreements(contract)
    assert len(hit) == 1, _errs(contract)
    assert hit[0].startswith(f"iceberg sink (build 'ingest'): {label} sets {setting}")
    assert f"the expose's catalog {expose}" in hit[0]
    assert "is catalog: rest" in hit[0]
    msg = iceberg_sink_preflight(contract, "ingest")
    assert msg is not None and "Declare the catalog the sink writes to" in msg


@pytest.mark.parametrize(
    "binding, kc",
    [
        # every REST-family kind is type=rest on the wire
        (
            _loc_binding("lakekeeper", **COMPLETE),
            {"sink_connector_config": {"iceberg.catalog.type": "rest"}},
        ),
        (
            _loc_binding("rest", **COMPLETE),
            {"sink_connector_config": {"iceberg.catalog.type": "REST"}},
        ),
        (
            _loc_binding(platform="snowflake", **COMPLETE),
            {"sink_connector_config": {"iceberg.catalog.type": "rest"}},
        ),
        # Glue by type or by its class
        (_GLUE_AWS, {"sink_connector_config": {"iceberg.catalog.type": "glue"}}),
        (_GLUE_AWS, {"sink_connector_config": {"iceberg.catalog.catalog-impl": GLUE_CATALOG_IMPL}}),
        # a REST endpoint that fronts Glue, declared as what the sink writes to
        (
            _loc_binding("rest", platform="aws", **COMPLETE),
            {"sink_connector_config": {"iceberg.catalog.type": "rest"}},
        ),
        # a hand-written config that selects no catalog cannot disagree
        (_GLUE_AWS, {"sink_connector_config": {"topics": "t"}}),
    ],
)
def test_a_handwritten_catalog_that_matches_the_named_one_passes(binding, kc):
    assert _disagreements(_sink_contract(binding, kc=kc)) == []


def test_sink_catalog_disagreement_is_reported_once():
    # sink.catalog and the hand-written type both disagree: one cause, one error
    contract = _sink_contract(
        _loc_binding("bigquery", platform="gcp", warehouse="gs://acme/wh"),
        sink={"catalog": "rest"},
        kc={
            "iceberg_sink_enabled": True,
            "sink_connector_config": {"iceberg.catalog.type": "rest"},
        },
    )
    errs = _errs(contract)
    assert len([e for e in errs if "disagrees" in e]) == 1, errs
    assert _disagreements(contract) == [], errs


def test_both_selectors_draw_only_the_selector_error():
    # The sink never starts, so no catalog reaches it to disagree with.
    contract = _sink_contract(
        _GLUE_AWS, kc={"iceberg_catalog_overrides": {"iceberg.catalog.type": "rest"}}
    )
    errs = _errs(contract)
    assert any("would carry both" in e for e in errs), errs
    assert _disagreements(contract) == [], errs


def test_unnamed_catalog_off_gcp_is_not_refused():
    assert _errs(_sink_contract(_loc_binding(**COMPLETE))) == []


# ── bigquery: the published Kafka Connect sink predates the type ────────────


def test_bigquery_on_kafka_connect_warns_about_the_published_sink():
    contract = _sink_contract(
        _loc_binding("bigquery", platform="gcp", warehouse="gs://acme/wh", project="acme-proj")
    )
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    hit = [w for w in warnings if "iceberg.catalog.type=bigquery" in w]
    assert len(hit) == 1
    assert PUBLISHED_KAFKA_CONNECT_SINK_VERSION in hit[0] and "1.10" in hit[0]
    assert "Confluent Hub" in hit[0]


def test_bigquery_on_debezium_server_has_no_kafka_connect_warning():
    contract = _sink_contract(
        _loc_binding("bigquery", platform="gcp", warehouse="gs://acme/wh"),
        engine="debezium",
        debezium=_embedded(),
    )
    assert not any("iceberg.catalog.type=bigquery" in w for w in _warns(contract))
