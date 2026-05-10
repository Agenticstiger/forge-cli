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

"""Pin the cross-stage run-id correlation contract.

The ``traced_stage`` decorator is the single chokepoint that stamps
``fluid.run_id`` onto every CLI stage's root span. This test:

1. Asserts ``traced_stage`` resolves a run-id via
   :func:`get_or_create_run_id` and includes it in the span attributes
   it would emit.
2. Asserts the four user-flagged 11-stage entry points
   (bundle / plan / verify / publish) all wear the decorator. A
   regression where one is silently un-decorated would break
   correlation across that stage of the pipeline.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


def _ensure_workspace_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_RUN_ID", raising=False)


def test_traced_stage_stamps_run_id(monkeypatch, tmp_path: Path):
    """The decorator should resolve a run-id and pass it through to
    the span attribute set."""
    _ensure_workspace_dir(monkeypatch, tmp_path)

    from fluid_build.observability.tracing import traced_stage as decorator

    # Build a minimal Namespace-shaped object so ``_args_to_attrs``
    # has something to inspect.
    class _NS:
        env = "dev"
        provider = "snowflake"

    captured: dict = {}

    # Stub out the OTEL plumbing — _get_tracer returns None when OTEL
    # is disabled, in which case the decorator falls through to the
    # raw function. Force-enable a fake tracer so the attribute path
    # is exercised.
    class _FakeSpan:
        def __init__(self) -> None:
            self.attrs: dict = {}

        def set_attribute(self, k, v) -> None:  # noqa: D401
            self.attrs[k] = v

        def set_status(self, s) -> None:  # noqa: D401
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _FakeTracer:
        def __init__(self) -> None:
            self.last_span: _FakeSpan | None = None

        def start_as_current_span(self, name):
            self.last_span = _FakeSpan()
            captured["span_name"] = name
            captured["span"] = self.last_span
            return self.last_span

    fake = _FakeTracer()
    monkeypatch.setattr("fluid_build.observability.tracing._get_tracer", lambda: fake)

    @decorator("test_stage")
    def fake_run(args) -> int:
        return 0

    result = fake_run(_NS())
    assert result == 0
    assert captured.get("span_name") == "fluid.test_stage"
    span: _FakeSpan = captured["span"]
    assert span.attrs.get("fluid.stage") == "test_stage"
    assert span.attrs.get("fluid.env") == "dev"
    # Critical: the run-id was stamped by the decorator.
    rid = span.attrs.get("fluid.run_id")
    assert rid is not None and rid != ""


def test_traced_stage_run_id_persists_across_stages(monkeypatch, tmp_path: Path):
    """Two consecutive stages must share the same run-id (the
    persisted .fluid/run-id.txt is the cross-stage carrier)."""
    _ensure_workspace_dir(monkeypatch, tmp_path)

    seen_run_ids: list[str] = []

    class _FakeSpan:
        def __init__(self):
            self.attrs = {}

        def set_attribute(self, k, v):
            self.attrs[k] = v
            if k == "fluid.run_id":
                seen_run_ids.append(v)

        def set_status(self, s):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    class _FakeTracer:
        def start_as_current_span(self, name):
            return _FakeSpan()

    monkeypatch.setattr("fluid_build.observability.tracing._get_tracer", lambda: _FakeTracer())

    from fluid_build.observability.tracing import traced_stage

    class _NS:
        env = "dev"

    @traced_stage("bundle")
    def stage_bundle(args) -> int:
        return 0

    @traced_stage("plan")
    def stage_plan(args) -> int:
        return 0

    stage_bundle(_NS())
    stage_plan(_NS())

    assert len(seen_run_ids) == 2
    assert (
        seen_run_ids[0] == seen_run_ids[1]
    ), "Bundle and plan must share the same run_id via the persisted file"


@pytest.mark.parametrize(
    "module_path",
    [
        # User-flagged "must land" stages from session 1.
        "fluid_build.cli.bundle",
        "fluid_build.cli.plan",
        "fluid_build.cli.apply",
        "fluid_build.cli.verify",
        "fluid_build.cli.publish",
        # Full 11-stage parity (session 2 close-the-gap).
        "fluid_build.cli.validate",
        "fluid_build.cli.diff",
        "fluid_build.cli.generate_artifacts",
        "fluid_build.cli.validate_artifacts",
        "fluid_build.cli.policy_apply",
        "fluid_build.cli.schedule_sync",
    ],
)
def test_stage_run_function_is_decorated(module_path):
    """Each user-flagged stage's ``run`` must wear the
    ``traced_stage`` decorator. We detect the wrapping by inspecting
    the ``__wrapped__`` attribute that ``functools.wraps`` sets on
    decorated functions."""
    mod = importlib.import_module(module_path)
    run_fn = getattr(mod, "run", None)
    assert run_fn is not None, f"{module_path} has no run()"
    assert hasattr(run_fn, "__wrapped__"), (
        f"{module_path}.run must be decorated with @traced_stage so the "
        f"run-id correlation attribute lands on its OTel root span"
    )
