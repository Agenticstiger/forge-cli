# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``fluid apply`` must emit OpenLineage, not just the acquisition runners.

#467 wired emission into ``build_runners/_acquisition_common.py`` only, so a
real Snowflake / AWS / GCP apply — the headline state-changing command —
produced zero events for a receiver configured with the standard
``OPENLINEAGE_URL``. The OpenTofu engine now brackets ``tofu apply`` with
START and COMPLETE / FAIL.
"""

from __future__ import annotations

import pytest

from fluid_build.api.conformance.runner import assert_openlineage_shape
from fluid_build.api.lineage import RunEventType
from fluid_build.build_runners._lineage import (
    FLUID_ENGINE_FACET_KEY,
    BufferedLineageEmitter,
    build_apply_event,
    emit_apply_event,
    encode_event,
)

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_CONTRACT = {
    "id": "silver.demo.orders_v1",
    "exposes": [
        {
            "exposeId": "orders",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {"database": "DB", "schema": "SC", "table": "ORDERS"},
            },
        },
        {
            "exposeId": "orders_file",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "./out/orders.parquet"},
            },
        },
    ],
}


class TestApplyEventShape:
    def _event(self, event_type=RunEventType.COMPLETE, **kwargs):
        base = {
            "event_type": event_type,
            "event_time": "2026-05-15T00:00:05Z",
            "run_id": "01APPLYRUN",
            "run_started_at": "2026-05-15T00:00:00Z",
            "provider": "snowflake",
        }
        base.update(kwargs)
        return build_apply_event(_CONTRACT, **base)

    @pytest.mark.parametrize(
        "event_type", [RunEventType.START, RunEventType.COMPLETE, RunEventType.FAIL]
    )
    def test_payload_is_spec_conformant(self, event_type):
        assert_openlineage_shape(encode_event(self._event(event_type)))

    def test_job_name_matches_the_acquisition_shape(self):
        payload = encode_event(self._event())
        assert payload["job"]["namespace"] == "fluid"
        assert payload["job"]["name"] == "silver.demo.orders_v1.apply"

    def test_outputs_are_the_physical_bindings(self):
        """Not a synthetic ``fluid``/product-id node — the real table."""
        payload = encode_event(self._event())
        outputs = {(d["namespace"], d["name"]) for d in payload["outputs"]}
        assert ("snowflake", "DB.SC.ORDERS") in outputs
        assert ("local", "./out/orders.parquet") in outputs

    def test_engine_facet_is_a_conformant_object(self):
        payload = encode_event(self._event(facets={"applied_changes": {"add": 1}}))
        facet = payload["run"]["facets"][FLUID_ENGINE_FACET_KEY]
        assert facet["_producer"] and facet["_schemaURL"]
        assert facet["engine"] == "opentofu"
        assert facet["provider"] == "snowflake"
        assert facet["applied_changes"] == {"add": 1}

    def test_run_id_is_stable_across_the_pair(self):
        start = encode_event(
            self._event(RunEventType.START, event_time="2026-05-15T00:00:00Z")
        )
        done = encode_event(self._event(RunEventType.COMPLETE))
        assert start["run"]["runId"] == done["run"]["runId"]

    def test_a_contract_without_exposes_still_emits(self):
        event = build_apply_event(
            {"id": "x"},
            event_type=RunEventType.START,
            event_time="2026-05-15T00:00:00Z",
            run_id="01X",
            run_started_at="2026-05-15T00:00:00Z",
            provider="aws",
        )
        assert_openlineage_shape(encode_event(event))
        assert event.outputs == []


class TestEmitIsNonFatal:
    def test_a_broken_emitter_does_not_raise(self):
        class _Boom(BufferedLineageEmitter):
            def emit(self, event):  # noqa: D401
                raise RuntimeError("receiver exploded")

        emit_apply_event(
            _Boom(),
            _CONTRACT,
            event_type=RunEventType.START,
            event_time="2026-05-15T00:00:00Z",
            run_id="01X",
            run_started_at="2026-05-15T00:00:00Z",
            provider="snowflake",
        )  # must not raise

    def test_null_emitter_is_a_noop(self):
        from fluid_build.build_runners._lineage import NullLineageEmitter

        emit_apply_event(
            NullLineageEmitter(),
            _CONTRACT,
            event_type=RunEventType.START,
            event_time="2026-05-15T00:00:00Z",
            run_id="01X",
            run_started_at="2026-05-15T00:00:00Z",
            provider="snowflake",
        )


class TestApplyEngineBracket:
    """The engine's own context manager: START before, terminal after."""

    def _run(self, *, fail: bool):
        from fluid_build.cli import _apply_opentofu_engine as eng

        buffered = BufferedLineageEmitter()
        import fluid_build.build_runners._lineage as lin

        original = lin.resolve_lineage_emitter
        lin.resolve_lineage_emitter = lambda: buffered
        try:
            with eng._apply_lineage(_CONTRACT, provider="snowflake", planned={"add": 1}) as record:
                if fail:
                    raise RuntimeError("tofu apply failed")
                record({"add": 1, "change": 0, "remove": 0})
        finally:
            lin.resolve_lineage_emitter = original
        return buffered.events

    def test_success_emits_start_then_complete(self):
        events = self._run(fail=False)
        assert [e.event_type for e in events] == [RunEventType.START, RunEventType.COMPLETE]
        applied = events[1].run_facets[FLUID_ENGINE_FACET_KEY]["applied_changes"]
        assert applied == {"add": 1, "change": 0, "remove": 0}

    def test_failure_emits_start_then_fail_and_reraises(self):
        """A failing ``tofu apply`` must still report the run, not vanish."""
        import fluid_build.build_runners._lineage as lin
        from fluid_build.cli import _apply_opentofu_engine as eng

        buffered = BufferedLineageEmitter()
        original = lin.resolve_lineage_emitter
        lin.resolve_lineage_emitter = lambda: buffered
        try:
            with pytest.raises(RuntimeError):
                with eng._apply_lineage(_CONTRACT, provider="snowflake", planned={}):
                    raise RuntimeError("tofu apply failed")
        finally:
            lin.resolve_lineage_emitter = original

        assert [e.event_type for e in buffered.events] == [
            RunEventType.START,
            RunEventType.FAIL,
        ]
        assert len({e.run_id for e in buffered.events}) == 1
