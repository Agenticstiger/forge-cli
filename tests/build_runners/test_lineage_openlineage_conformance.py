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

"""OpenLineage wire-format conformance for the acquisition lineage emitter.

Regression pins for three defects that made every emitted payload
unconsumable by Marquez, DataHub or any other OpenLineage receiver:

1. ``producer`` and ``schemaURL`` were missing (both required by ``BaseEvent``).
2. ``run`` and ``job`` were flattened to top-level snake_case keys.
3. ``runId`` carried FLUID's base32 run id rather than the ``format: uuid``
   the spec requires.

Also pins the credential-safety property of dataset namespaces and the
env-var resolution that makes an existing ``OPENLINEAGE_URL`` deployment
work with no FLUID-specific configuration.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from fluid_build.api.conformance.runner import assert_openlineage_shape
from fluid_build.api.lineage import DatasetFacet, RunEvent, RunEventType
from fluid_build.build_runners._lineage import (
    FLUID_ENGINE_FACET_KEY,
    FLUID_RUN_FACET_KEY,
    BufferedLineageEmitter,
    HttpLineageEmitter,
    NullLineageEmitter,
    _dataset_namespace,
    build_run_event,
    emit_run_event,
    encode_event,
    resolve_lineage_emitter,
    run_id_to_uuid,
)

pytestmark = pytest.mark.unit

_FLUID_RUN_ID = "01HXXABCDE123456"
_EVENT_TIME = "2026-05-15T00:00:00Z"


def _event(**overrides) -> RunEvent:
    base = {
        "event_type": RunEventType.COMPLETE,
        "event_time": _EVENT_TIME,
        "run_id": _FLUID_RUN_ID,
        "job_namespace": "fluid",
        "job_name": "orders.acquire",
        "inputs": [DatasetFacet(namespace="postgres", name="public.orders")],
        "outputs": [DatasetFacet(namespace="fluid", name="orders")],
    }
    base.update(overrides)
    return RunEvent(**base)


class TestWireFormat:
    def test_payload_is_openlineage_shaped(self):
        assert_openlineage_shape(encode_event(_event()))

    def test_base_event_required_fields_present(self):
        """Defect 1: producer and schemaURL were absent entirely."""
        payload = encode_event(_event())
        assert payload["producer"].startswith("https://github.com/Agenticstiger/forge-cli")
        assert payload["schemaURL"].startswith("https://openlineage.io/spec/")
        assert payload["eventTime"] == _EVENT_TIME

    def test_run_and_job_are_nested_not_flattened(self):
        """Defect 2: run/job were emitted as flat snake_case top-level keys."""
        payload = encode_event(_event())
        assert payload["run"]["runId"]
        assert payload["job"] == {"namespace": "fluid", "name": "orders.acquire", "facets": {}}
        for legacy in ("run_id", "job_namespace", "job_name", "run_facets", "event_type"):
            assert legacy not in payload

    def test_run_id_is_a_uuid(self):
        """Defect 3: the spec declares runId as `format: uuid`."""
        payload = encode_event(_event())
        uuid.UUID(payload["run"]["runId"])

    def test_run_id_mapping_is_deterministic(self):
        """Re-emitting the same run must produce the same OpenLineage id."""
        assert run_id_to_uuid(_FLUID_RUN_ID, _EVENT_TIME) == run_id_to_uuid(
            _FLUID_RUN_ID, _EVENT_TIME
        )

    def test_run_id_mapping_is_monotonic(self):
        """UUIDv7 keeps FLUID's sortability property across timestamps."""
        earlier = run_id_to_uuid("01AAA", "2026-05-15T00:00:00Z")
        later = run_id_to_uuid("01BBB", "2026-05-15T01:00:00Z")
        assert uuid.UUID(earlier) < uuid.UUID(later)

    def test_native_run_id_preserved_for_correlation(self):
        payload = encode_event(_event())
        facet = payload["run"]["facets"][FLUID_RUN_FACET_KEY]
        assert facet["fluidRunId"] == _FLUID_RUN_ID
        assert facet["_producer"] and facet["_schemaURL"]

    def test_caller_run_facets_survive(self):
        payload = encode_event(
            _event(run_facets={"engine": {"_producer": "p", "_schemaURL": "s", "name": "duckdb"}})
        )
        assert payload["run"]["facets"]["engine"]["name"] == "duckdb"
        assert FLUID_RUN_FACET_KEY in payload["run"]["facets"]

    @pytest.mark.parametrize("event_type", list(RunEventType))
    def test_every_fluid_event_type_maps(self, event_type):
        payload = encode_event(_event(event_type=event_type))
        assert payload["eventType"] == event_type.value

    def test_unparseable_event_time_does_not_raise(self):
        """Observability must never break a run over a bad timestamp."""
        assert_openlineage_shape(encode_event(_event(event_time="not-a-timestamp")))


class TestCredentialSafety:
    def test_namespace_derives_from_kind_only(self):
        """Namespaces must never fold in the resolved connection.

        ``ConnectionSpec.raw`` holds host/user/password after env
        interpolation, and lineage events travel to a third-party receiver.
        """
        assert _dataset_namespace("postgres") == "postgres"
        assert _dataset_namespace(None) == "fluid"

    def test_result_facets_are_redacted_before_leaving_the_machine(self):
        """Engine traces in ``result.facets`` must be scrubbed.

        ``_state.write_run_record`` already routes this exact field through
        ``redact_value`` before writing to disk, because Kafka Connect task
        status has carried ``sasl.jaas.config`` and connector passwords
        verbatim. A lineage event is POSTed to a third-party receiver, so
        omitting the scrub here would leak further than the on-disk case
        that chokepoint exists to prevent.
        """
        result = SimpleNamespace(
            facets={
                "connector": {
                    "password": "hunter2",
                    "sasl.jaas.config": 'org.apache.kafka.common.security.plain.PlainLoginModule required username="u" password="p";',
                }
            }
        )
        ctx = SimpleNamespace(
            run_id=_FLUID_RUN_ID,
            product_id="orders",
            build_id="acquire",
            lineage=BufferedLineageEmitter(),
            source=SimpleNamespace(kind="kafka", streams=["orders"]),
        )
        payload = encode_event(
            build_run_event(
                ctx, event_type=RunEventType.COMPLETE, event_time=_EVENT_TIME, result=result
            )
        )
        serialised = str(payload)
        assert "hunter2" not in serialised
        assert 'password="p"' not in serialised

    def test_stream_names_are_redacted(self):
        """Stream names are env-interpolated before reaching the emitter.

        ``_resolve_env_placeholders`` recurses into ``streams``, so a
        contract declaring ``streams: ["{{ env.DB_PASSWORD }}"]`` arrives
        here already resolved. That value lands in the on-disk run record
        too, but only the lineage path sends it to a third-party receiver.
        """
        ctx = SimpleNamespace(
            run_id=_FLUID_RUN_ID,
            product_id="orders",
            build_id="acquire",
            lineage=BufferedLineageEmitter(),
            source=SimpleNamespace(
                kind="postgres",
                streams=["AKIAIOSFODNN7EXAMPLE", "public.orders"],
            ),
        )
        payload = encode_event(
            build_run_event(ctx, event_type=RunEventType.START, event_time=_EVENT_TIME)
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in str(payload)
        # Ordinary stream names survive untouched.
        assert any(d["name"] == "public.orders" for d in payload["inputs"])

    def test_built_event_carries_no_connection_material(self):
        ctx = SimpleNamespace(
            run_id=_FLUID_RUN_ID,
            product_id="orders",
            build_id="acquire",
            lineage=BufferedLineageEmitter(),
            source=SimpleNamespace(
                kind="postgres",
                streams=["public.orders"],
                connection=SimpleNamespace(raw={"host": "db.internal", "password": "s3cret"}),
            ),
        )
        payload = encode_event(
            build_run_event(ctx, event_type=RunEventType.START, event_time=_EVENT_TIME)
        )
        assert "s3cret" not in str(payload)
        assert "db.internal" not in str(payload)
        assert payload["inputs"][0]["namespace"] == "postgres"
        assert payload["inputs"][0]["name"] == "public.orders"


class TestEmitterResolution:
    def test_unconfigured_returns_null_emitter(self, monkeypatch):
        for var in (
            "FLUID_OPENLINEAGE_URL",
            "OPENLINEAGE_URL",
            "FLUID_OPENLINEAGE_ENDPOINT",
            "OPENLINEAGE_ENDPOINT",
        ):
            monkeypatch.delenv(var, raising=False)
        assert isinstance(resolve_lineage_emitter(), NullLineageEmitter)

    def test_standard_openlineage_url_is_honoured(self, monkeypatch):
        """An existing Marquez/DataHub deployment must work unmodified.

        This is the adoption-critical behaviour: a team that already exports
        ``OPENLINEAGE_URL`` gets FLUID lineage with zero FLUID-specific setup.
        """
        monkeypatch.delenv("FLUID_OPENLINEAGE_URL", raising=False)
        monkeypatch.delenv("FLUID_OPENLINEAGE_ENDPOINT", raising=False)
        monkeypatch.delenv("OPENLINEAGE_ENDPOINT", raising=False)
        monkeypatch.setenv("OPENLINEAGE_URL", "https://marquez.example")
        emitter = resolve_lineage_emitter()
        assert isinstance(emitter, HttpLineageEmitter)
        assert emitter.endpoint == "https://marquez.example/api/v1/lineage"

    def test_fluid_prefixed_override_wins(self, monkeypatch):
        monkeypatch.setenv("OPENLINEAGE_URL", "https://standard.example")
        monkeypatch.setenv("FLUID_OPENLINEAGE_URL", "https://override.example")
        monkeypatch.delenv("FLUID_OPENLINEAGE_ENDPOINT", raising=False)
        monkeypatch.delenv("OPENLINEAGE_ENDPOINT", raising=False)
        assert resolve_lineage_emitter().endpoint == "https://override.example/api/v1/lineage"

    def test_trailing_slash_does_not_double_up(self, monkeypatch):
        monkeypatch.delenv("FLUID_OPENLINEAGE_URL", raising=False)
        monkeypatch.delenv("FLUID_OPENLINEAGE_ENDPOINT", raising=False)
        monkeypatch.delenv("OPENLINEAGE_ENDPOINT", raising=False)
        monkeypatch.setenv("OPENLINEAGE_URL", "https://marquez.example/")
        assert resolve_lineage_emitter().endpoint == "https://marquez.example/api/v1/lineage"

    def test_api_key_is_picked_up(self, monkeypatch):
        monkeypatch.delenv("FLUID_OPENLINEAGE_URL", raising=False)
        monkeypatch.setenv("OPENLINEAGE_URL", "https://marquez.example")
        monkeypatch.setenv("OPENLINEAGE_API_KEY", "tok")
        assert resolve_lineage_emitter().api_key == "tok"

    def test_bad_timeout_falls_back_without_raising(self, monkeypatch):
        monkeypatch.delenv("FLUID_OPENLINEAGE_URL", raising=False)
        monkeypatch.setenv("OPENLINEAGE_URL", "https://marquez.example")
        monkeypatch.setenv("FLUID_OPENLINEAGE_TIMEOUT_SECONDS", "not-a-number")
        assert resolve_lineage_emitter().timeout_seconds == 5.0


class TestEmitIsNonFatal:
    def test_null_emitter_short_circuits(self):
        ctx = SimpleNamespace(lineage=NullLineageEmitter())
        emit_run_event(ctx, event_type=RunEventType.START, event_time=_EVENT_TIME)

    def test_missing_lineage_attribute_is_tolerated(self):
        emit_run_event(SimpleNamespace(), event_type=RunEventType.START, event_time=_EVENT_TIME)

    def test_broken_context_never_raises(self):
        """Assembly failures must not abort a run."""

        class Exploding:
            @property
            def product_id(self):
                raise RuntimeError("boom")

        ctx = Exploding()
        ctx_ns = SimpleNamespace(lineage=BufferedLineageEmitter())
        object.__setattr__(ctx_ns, "product_id", property(lambda self: 1 / 0))
        emit_run_event(ctx, event_type=RunEventType.START, event_time=_EVENT_TIME)

    def test_buffered_emitter_receives_the_event(self):
        buffered = BufferedLineageEmitter()
        ctx = SimpleNamespace(
            run_id=_FLUID_RUN_ID,
            product_id="orders",
            build_id="acquire",
            lineage=buffered,
            source=SimpleNamespace(kind="postgres", streams=["public.orders"]),
        )
        emit_run_event(ctx, event_type=RunEventType.START, event_time=_EVENT_TIME)
        assert len(buffered.events) == 1
        assert buffered.events[0].job_name == "orders.acquire"


class TestShapeAssertionCatchesRegressions:
    """The conformance helper must actually fail on the historic payload."""

    def test_rejects_the_old_flattened_payload(self):
        legacy = {
            "eventTime": _EVENT_TIME,
            "eventType": "COMPLETE",
            "run_id": _FLUID_RUN_ID,
            "job_namespace": "fluid",
            "job_name": "orders.acquire",
            "inputs": [],
            "outputs": [],
            "run_facets": {},
        }
        with pytest.raises(AssertionError):
            assert_openlineage_shape(legacy)

    def test_rejects_non_uuid_run_id(self):
        payload = encode_event(_event())
        payload["run"]["runId"] = _FLUID_RUN_ID
        with pytest.raises(ValueError):
            assert_openlineage_shape(payload)

    def test_rejects_missing_producer(self):
        payload = encode_event(_event())
        del payload["producer"]
        with pytest.raises(AssertionError):
            assert_openlineage_shape(payload)


class TestTerminalEventFacets:
    """Defect 4: ``run.facets`` carried bare scalars on every terminal event.

    ``RunResult.facets`` is a flat dict of engine telemetry
    (``{"engine": "duckdb", "duration_seconds": 0.02}``). It used to be
    copied straight into ``run.facets``, where the spec requires each value
    to be a ``RunFacet`` object with ``_producer`` + ``_schemaURL``. START
    (which has no ``result``) validated; COMPLETE / FAIL / ABORT did not.
    """

    def _ctx(self):
        return SimpleNamespace(
            run_id=_FLUID_RUN_ID,
            product_id="orders",
            build_id="acquire",
            lineage=BufferedLineageEmitter(),
            source=SimpleNamespace(kind="duckdb", streams=["orders"]),
        )

    def _result(self, **extra):
        base = {"engine": "duckdb", "duration_seconds": 0.0243}
        base.update(extra)
        return SimpleNamespace(facets=base, started_at=_EVENT_TIME)

    @pytest.mark.parametrize(
        "event_type", [RunEventType.COMPLETE, RunEventType.FAIL, RunEventType.ABORT]
    )
    def test_terminal_events_are_shape_conformant(self, event_type):
        payload = encode_event(
            build_run_event(
                self._ctx(),
                event_type=event_type,
                event_time="2026-05-15T00:00:05Z",
                result=self._result(),
            )
        )
        assert_openlineage_shape(payload)

    def test_engine_scalars_are_nested_under_one_conformant_facet(self):
        payload = encode_event(
            build_run_event(
                self._ctx(),
                event_type=RunEventType.COMPLETE,
                event_time="2026-05-15T00:00:05Z",
                result=self._result(pii_findings=["email"]),
            )
        )
        facets = payload["run"]["facets"]
        # No bare scalars promoted to top-level facet names.
        assert "engine" not in facets
        assert "duration_seconds" not in facets
        engine_facet = facets[FLUID_ENGINE_FACET_KEY]
        assert engine_facet["_producer"] and engine_facet["_schemaURL"]
        assert engine_facet["engine"] == "duckdb"
        assert engine_facet["duration_seconds"] == pytest.approx(0.0243)
        # Nothing the engine reported is dropped.
        assert engine_facet["pii_findings"] == ["email"]

    def test_no_engine_facet_when_result_has_none(self):
        payload = encode_event(
            build_run_event(
                self._ctx(), event_type=RunEventType.START, event_time=_EVENT_TIME
            )
        )
        assert FLUID_ENGINE_FACET_KEY not in payload["run"]["facets"]

    def test_shape_assertion_rejects_a_bare_scalar_run_facet(self):
        """The helper itself must catch the regression, not skip it.

        It previously guarded the facet loop with ``isinstance(facet, dict)``,
        so a bare scalar facet passed conformance silently.
        """
        payload = encode_event(_event())
        payload["run"]["facets"]["engine"] = "duckdb"
        with pytest.raises(AssertionError):
            assert_openlineage_shape(payload)


class TestRunIdIsStableAcrossOneRun:
    """Defect 5: START and COMPLETE of one run carried different ``runId``.

    ``generate_static_uuid`` folds the supplied instant into the UUIDv7
    timestamp field. Seeding it with each event's own ``event_time`` made
    the pair uncorrelatable in Marquez / DataHub.
    """

    def _ctx(self):
        return SimpleNamespace(
            run_id=_FLUID_RUN_ID,
            product_id="orders",
            build_id="acquire",
            lineage=BufferedLineageEmitter(),
            source=SimpleNamespace(kind="duckdb", streams=["orders"]),
        )

    def test_start_and_complete_share_one_run_id(self):
        start = encode_event(
            build_run_event(
                self._ctx(), event_type=RunEventType.START, event_time=_EVENT_TIME
            )
        )
        complete = encode_event(
            build_run_event(
                self._ctx(),
                event_type=RunEventType.COMPLETE,
                # Finished later — the event times legitimately differ.
                event_time="2026-05-15T00:00:37Z",
                result=SimpleNamespace(
                    facets={"engine": "duckdb"}, started_at=_EVENT_TIME
                ),
            )
        )
        assert start["run"]["runId"] == complete["run"]["runId"]
        assert start["eventTime"] != complete["eventTime"]

    def test_run_started_at_falls_back_to_event_time(self):
        """A result without ``started_at`` must still produce a valid event."""
        payload = encode_event(
            build_run_event(
                self._ctx(),
                event_type=RunEventType.COMPLETE,
                event_time=_EVENT_TIME,
                result=SimpleNamespace(facets={"engine": "duckdb"}),
            )
        )
        assert payload["run"]["runId"] == run_id_to_uuid(_FLUID_RUN_ID, _EVENT_TIME)


class TestOutputDatasetIsThePhysicalBinding:
    """Defect 6: the output side was a synthetic ``fluid``/<product id> node.

    ``build_run_event`` hardcoded ``DatasetFacet(namespace="fluid",
    name=product_id)`` regardless of where the build actually lands, so a
    FLUID lineage edge could never join to the dataset a warehouse-side
    OpenLineage integration reports for the same table. Resolved from
    ``exposes[].binding`` instead — contract-declared, so unlike the input
    side there is no resolved-connection material to leak.
    """

    def _ctx(self, contract):
        return SimpleNamespace(
            run_id=_FLUID_RUN_ID,
            product_id="bronze.demo.orders",
            build_id="ingest",
            contract=contract,
            lineage=BufferedLineageEmitter(),
            source=SimpleNamespace(kind="sqlite", streams=["orders"]),
        )

    def test_snowflake_expose_resolves_to_the_table(self):
        contract = {
            "id": "bronze.demo.orders",
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {
                        "platform": "snowflake",
                        "location": {"database": "DB", "schema": "SC", "table": "ORDERS"},
                    },
                }
            ],
        }
        payload = encode_event(
            build_run_event(
                self._ctx(contract), event_type=RunEventType.START, event_time=_EVENT_TIME
            )
        )
        assert [(d["namespace"], d["name"]) for d in payload["outputs"]] == [
            ("snowflake", "DB.SC.ORDERS")
        ]

    def test_file_expose_resolves_to_the_path(self):
        contract = {
            "id": "bronze.demo.orders",
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": "./out/orders.parquet"},
                    },
                }
            ],
        }
        payload = encode_event(
            build_run_event(
                self._ctx(contract), event_type=RunEventType.START, event_time=_EVENT_TIME
            )
        )
        assert [(d["namespace"], d["name"]) for d in payload["outputs"]] == [
            ("local", "./out/orders.parquet")
        ]

    def test_falls_back_to_the_product_node_without_exposes(self):
        """An event must always carry an output, even for a bare contract."""
        payload = encode_event(
            build_run_event(
                self._ctx({"id": "bronze.demo.orders"}),
                event_type=RunEventType.START,
                event_time=_EVENT_TIME,
            )
        )
        assert [(d["namespace"], d["name"]) for d in payload["outputs"]] == [
            ("fluid", "bronze.demo.orders")
        ]
        assert_openlineage_shape(payload)

    def test_no_connection_material_reaches_the_output_side(self):
        """``exposes[].binding`` is contract-declared, but assert it anyway."""
        contract = {
            "id": "bronze.demo.orders",
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {
                        "platform": "postgres",
                        "location": {
                            "database": "app",
                            "schema": "public",
                            "table": "orders",
                        },
                    },
                }
            ],
        }
        ctx = self._ctx(contract)
        ctx.source = SimpleNamespace(
            kind="postgres",
            streams=["public.orders"],
            connection=SimpleNamespace(raw={"host": "db.internal", "password": "s3cret"}),
        )
        payload = encode_event(
            build_run_event(ctx, event_type=RunEventType.START, event_time=_EVENT_TIME)
        )
        assert "s3cret" not in str(payload)
        assert "db.internal" not in str(payload)
