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

"""Phase 3.8 — ``fluid.copilot.staged.invocation`` parent span.

Before this phase, each ``StageCoordinator.from_*`` opened its own
``fluid.copilot.coordinator.from_*`` span at the top of its loop.
Per-stage spans (logical / contract_forge / builder / readme /
transformation / validator) became siblings under whatever the CLI
layer had opened, with no single attribute to group them at query
time.

The new outermost ``fluid.copilot.staged.invocation`` span wraps the
existing coordinator span. It carries ``fluid.copilot.run_id`` so log
queries can group every span / cost record / event from one staged
forge invocation under one id.

Pin:

1. **`_new_run_id`** returns a non-empty string that's unique across
   calls (so two staged runs in one process don't collide).
2. **The coordinator opens the parent span** for each entry point
   (`from_intent` / `from_tables` / `from_catalog`).
3. **The parent span carries `run_id`** + entry-kind attributes the
   audit asked for.
"""

from __future__ import annotations

from unittest import mock

from fluid_build.copilot.agents.coordinator import (
    StageCoordinator,
    _new_run_id,
)

# ---------------------------------------------------------------------------
# Behaviour 1 — _new_run_id
# ---------------------------------------------------------------------------


def test_new_run_id_returns_non_empty_string():
    rid = _new_run_id()
    assert isinstance(rid, str)
    assert len(rid) > 0


def test_new_run_id_is_unique_across_calls():
    """Two back-to-back calls must produce different ids — otherwise
    runs collide in the trace store."""
    seen = {_new_run_id() for _ in range(20)}
    assert len(seen) == 20  # all unique


def test_new_run_id_is_short_enough_for_a_log_line():
    """uuid.hex is 32 chars; the helper truncates to ~12 so it fits
    inline in CLI log output without dominating."""
    rid = _new_run_id()
    assert len(rid) <= 16


# ---------------------------------------------------------------------------
# Behaviour 2 — coordinator wraps each entry point in a parent span
# ---------------------------------------------------------------------------


def _capture_traced_span_calls():
    """Helper: spy on ``traced_span`` to record every span name +
    attributes opened during a coordinator method call.

    Returns ``(patch_context, captured_calls)`` — enter the patch
    context to install the spy, then read ``captured_calls`` for
    the recorded list.
    """
    import contextlib

    captured: list = []

    @contextlib.contextmanager
    def _spy_span(name: str, attributes: dict | None = None):
        captured.append((name, dict(attributes or {})))
        yield None

    return (
        mock.patch(
            "fluid_build.copilot.agents.coordinator.traced_span",
            side_effect=_spy_span,
        ),
        captured,
    )


def test_from_intent_opens_staged_invocation_span():
    """The first ``traced_span`` call in ``from_intent`` is the
    new outermost ``fluid.copilot.staged.invocation`` parent."""
    coordinator = StageCoordinator.__new__(StageCoordinator)
    # Avoid running the inner pipeline; we only care about the span
    # that opens at method entry. Set up the bare minimum to hit
    # the first traced_span line then short-circuit.
    coordinator.logical_agent = mock.MagicMock()
    coordinator.contract_forge_agent = mock.MagicMock()
    coordinator.validator_agent = mock.MagicMock()
    coordinator.readme_agent = mock.MagicMock()
    coordinator.builder = mock.MagicMock()
    coordinator.transformation_agent = mock.MagicMock()
    coordinator.conformance_agent = mock.MagicMock()
    coordinator.critic_agent = mock.MagicMock()

    session = mock.MagicMock()
    session.run_id = ""  # let the coordinator generate a fresh id

    patch_ctx, captured = _capture_traced_span_calls()
    with (
        patch_ctx,
        mock.patch.object(
            coordinator,
            "_stage_budget",
            # Raise after the parent + child spans have been opened so we
            # can read the captured list without running the whole pipeline.
            side_effect=RuntimeError("STOP"),
        ),
    ):
        try:
            coordinator.from_intent(
                session=session,
                intent=mock.MagicMock(),
                technique="dimensional",
            )
        except RuntimeError:
            pass

    # First two spans: staged.invocation (parent), then coordinator.from_intent.
    assert len(captured) >= 2
    parent_name, parent_attrs = captured[0]
    child_name, child_attrs = captured[1]
    assert parent_name == "fluid.copilot.staged.invocation"
    assert child_name == "fluid.copilot.coordinator.from_intent"
    assert parent_attrs.get("fluid.copilot.entry") == "intent"
    assert parent_attrs.get("fluid.copilot.run_id"), "parent span missing run_id attribute"


def test_from_tables_opens_staged_invocation_span():
    coordinator = StageCoordinator.__new__(StageCoordinator)
    coordinator.logical_agent = mock.MagicMock()
    coordinator.contract_forge_agent = mock.MagicMock()
    coordinator.validator_agent = mock.MagicMock()
    coordinator.readme_agent = mock.MagicMock()
    coordinator.builder = mock.MagicMock()
    coordinator.transformation_agent = mock.MagicMock()
    coordinator.conformance_agent = mock.MagicMock()
    coordinator.critic_agent = mock.MagicMock()

    session = mock.MagicMock()
    session.run_id = ""

    patch_ctx, captured = _capture_traced_span_calls()
    with (
        patch_ctx,
        mock.patch.object(coordinator, "_stage_budget", side_effect=RuntimeError("STOP")),
    ):
        try:
            coordinator.from_tables(
                session=session,
                name="x",
                tables=[],
                technique="dimensional",
            )
        except RuntimeError:
            pass

    parent_name, parent_attrs = captured[0]
    assert parent_name == "fluid.copilot.staged.invocation"
    assert parent_attrs.get("fluid.copilot.entry") == "tables"
    assert parent_attrs.get("fluid.copilot.run_id")


def test_session_run_id_is_reused_when_present():
    """If the session already carries a ``run_id`` (set by the CLI
    layer to tie the staged invocation to a parent forge.invocation
    span), the coordinator re-uses it rather than minting a fresh one."""
    coordinator = StageCoordinator.__new__(StageCoordinator)
    coordinator.logical_agent = mock.MagicMock()
    coordinator.contract_forge_agent = mock.MagicMock()
    coordinator.validator_agent = mock.MagicMock()
    coordinator.readme_agent = mock.MagicMock()
    coordinator.builder = mock.MagicMock()
    coordinator.transformation_agent = mock.MagicMock()
    coordinator.conformance_agent = mock.MagicMock()
    coordinator.critic_agent = mock.MagicMock()

    session = mock.MagicMock()
    session.run_id = "preset-run-id-12345"

    patch_ctx, captured = _capture_traced_span_calls()
    with (
        patch_ctx,
        mock.patch.object(coordinator, "_stage_budget", side_effect=RuntimeError("STOP")),
    ):
        try:
            coordinator.from_intent(
                session=session,
                intent=mock.MagicMock(),
                technique="dimensional",
            )
        except RuntimeError:
            pass

    parent_attrs = captured[0][1]
    assert parent_attrs["fluid.copilot.run_id"] == "preset-run-id-12345"
