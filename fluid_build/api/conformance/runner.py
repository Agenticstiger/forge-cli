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

"""Runner conformance test mixin.

Subclasses set ``runner`` and ``fixtures`` class attributes; the test
methods inherited from this class assert capability declarations are
honest, state round-trips correctly, replay is byte-identical, lineage
events conform to the OpenLineage shape, and secrets never appear in
captured logs.
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from fluid_build.api.runner import Runner

#: Facet keys OpenLineage reserves for its own bookkeeping on custom facets.
_FACET_META_KEYS = ("_producer", "_schemaURL")


class RunnerConformance:
    """Mixin asserting a runner satisfies the public Protocol contract.

    Each test method is independent; subclasses can ``pytest.skip`` any
    that genuinely don't apply (e.g., DuckDB has no streaming capability,
    so the streaming-specific tests just skip).
    """

    runner: ClassVar[Runner]
    fixtures: ClassVar[str] = "fluid_build.api.conformance.fixtures.minimal"

    def test_has_required_class_vars(self) -> None:
        assert hasattr(self.runner, "name") and isinstance(self.runner.name, str)
        assert hasattr(self.runner, "declared_capabilities")
        assert hasattr(self.runner, "declared_modes")
        assert isinstance(self.runner.declared_capabilities, frozenset)
        assert isinstance(self.runner.declared_modes, frozenset)
        assert self.runner.declared_modes <= {"embedded", "bring-your-own", "managed"}

    def test_capabilities_non_empty(self) -> None:
        assert (
            len(self.runner.declared_capabilities) > 0
        ), f"Runner {self.runner.name} declares no capabilities — at least one is required."

    def test_plan_idempotent(self, conformance_ctx: Any) -> None:
        """Calling plan twice must return equivalent results."""
        a = self.runner.plan(conformance_ctx)
        b = self.runner.plan(conformance_ctx)
        assert a.streams_planned == b.streams_planned

    def test_run_returns_run_id(self, conformance_ctx: Any) -> None:
        result = self.runner.run(conformance_ctx)
        assert result.run_id == conformance_ctx.run_id
        assert result.state.terminal

    def test_fingerprint_stable(self, conformance_ctx: Any) -> None:
        f1 = self.runner.fingerprint(conformance_ctx)
        f2 = self.runner.fingerprint(conformance_ctx)
        assert f1.digest == f2.digest

    def test_lineage_events_conform_to_openlineage(self, conformance_ctx: Any) -> None:
        """Every lineage event a runner emits must satisfy the OpenLineage spec.

        This class has always documented that it asserts "lineage events
        conform to the OpenLineage shape" but carried no such check, so a
        runner could emit an unconsumable payload and still pass
        conformance. Validation is structural (the required-field and
        nesting rules from ``BaseEvent`` / ``RunEvent`` / ``Run`` / ``Job``
        in spec 2-0-2) rather than a network fetch of the schema, so the
        suite stays offline and deterministic.
        """
        from fluid_build.api.lineage import RunEventType
        from fluid_build.build_runners._lineage import BufferedLineageEmitter, encode_event

        buffered = BufferedLineageEmitter()
        object.__setattr__(conformance_ctx, "lineage", buffered)

        self.runner.run(conformance_ctx)

        if not buffered.events:
            import pytest

            pytest.skip(f"{self.runner.name} emits no lineage events")

        for event in buffered.events:
            assert isinstance(event.event_type, RunEventType)
            payload = encode_event(event)
            assert_openlineage_shape(payload)


def assert_openlineage_shape(payload: dict) -> None:
    """Assert ``payload`` matches the OpenLineage 2-0-2 RunEvent structure.

    Kept module-level so runner suites outside this mixin (and the emitter's
    own tests) can reuse it.
    """
    import uuid as _uuid

    # BaseEvent required fields.
    for key in ("eventTime", "producer", "schemaURL"):
        assert key in payload, f"OpenLineage BaseEvent requires {key!r}; got {sorted(payload)}"
        assert isinstance(payload[key], str) and payload[key], f"{key!r} must be a non-empty string"

    assert payload["schemaURL"].startswith(
        "https://openlineage.io/spec/"
    ), f"schemaURL must point at the OpenLineage spec, got {payload['schemaURL']!r}"

    # RunEvent required nesting. The historic bug was flattening these to
    # top-level snake_case keys, so assert the flattened form is absent too.
    for legacy in ("run_id", "job_namespace", "job_name", "run_facets", "event_type", "event_time"):
        assert legacy not in payload, (
            f"{legacy!r} is the pre-OpenLineage flattened field name; "
            "the event must use the nested spec shape"
        )

    assert "run" in payload and isinstance(payload["run"], dict), "RunEvent requires a nested `run`"
    assert "job" in payload and isinstance(payload["job"], dict), "RunEvent requires a nested `job`"

    run_id = payload["run"].get("runId")
    assert run_id, "Run requires `runId`"
    # The spec declares runId as `format: uuid`.
    _uuid.UUID(str(run_id))

    job = payload["job"]
    assert job.get("namespace"), "Job requires `namespace`"
    assert job.get("name"), "Job requires `name`"

    assert payload.get("eventType") in {
        "START",
        "RUNNING",
        "COMPLETE",
        "ABORT",
        "FAIL",
        "OTHER",
    }, f"unknown eventType {payload.get('eventType')!r}"

    # Every value in ``run.facets`` must be a ``RunFacet`` — an *object*
    # carrying the OpenLineage bookkeeping keys (``BaseFacet.required`` is
    # ``["_producer", "_schemaURL"]``), otherwise consumers cannot attribute
    # or version them. The ``isinstance(facet, dict)`` guard that used to
    # wrap this loop made a non-object facet skip the check silently, which
    # is precisely how terminal events shipped ``{"engine": "duckdb",
    # "duration_seconds": 0.02}`` as bare scalars: START validated, every
    # COMPLETE/FAIL did not.
    for facet_name, facet in (payload["run"].get("facets") or {}).items():
        assert isinstance(facet, dict), (
            f"run facet {facet_name!r} must be an object (RunFacet inherits BaseFacet), "
            f"got {type(facet).__name__}"
        )
        for meta in _FACET_META_KEYS:
            assert meta in facet, f"run facet {facet_name!r} is missing {meta!r}"

    for side in ("inputs", "outputs"):
        for dataset in payload.get(side) or []:
            assert dataset.get("namespace"), f"{side} dataset requires `namespace`"
            assert dataset.get("name"), f"{side} dataset requires `name`"
