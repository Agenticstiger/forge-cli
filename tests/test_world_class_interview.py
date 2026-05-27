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

"""Pin every one of the 10 world-class interview fixes.

User's complaint, ten line items:

  1. Ignores welcome scan findings
  2. Generic prompts with no examples
  3. Industry / use-case asked as long lists with Other default
  4. Sequential, not adaptive
  5. Got sample / DDL asked even when detected
  6. No "you decide" escape
  7. productType (SDP/ADP/CDP) not surfaced early
  8. No progress + cost
  9. Domain not inferred from cwd / workspace
 10. Clarifier LLM rounds duplicate bootstrap state
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest import mock

import pytest

from fluid_build.cli._world_class_interview import (
    _QUESTIONS,
    InterviewSignals,
    assess_coverage,
    collect_signals,
    needs_asking,
    run_world_class_bootstrap,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """Sandbox ~/.fluid so usage.json poisoning can't skip the welcome scan."""
    fake_home = tmp_path / "_home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr("fluid_build.cli._welcome_scan.Path.home", lambda: fake_home)
    yield fake_home


class _State:
    """Minimal stand-in for CopilotInterviewState."""

    def __init__(self):
        self.normalized_context: Dict[str, Any] = {}
        self.assumptions: List[str] = []
        self.turns: List[Dict[str, Any]] = []
        self.ready = False

    def apply_patch(self, patch, *, source="x"):
        self.normalized_context.update(patch)

    def add_assumptions(self, items):
        self.assumptions.extend(items)

    def record_turn(self, **kwargs):
        self.turns.append(kwargs)


# ---------------------------------------------------------------------------
# 1. Welcome scan findings populate signals
# ---------------------------------------------------------------------------


def test_signals_pull_workspace_lock(tmp_path):
    from fluid_build.cli.workspace_config import save_workspace_config

    save_workspace_config(tmp_path, name="ws", data_product_type_lock="ADP")
    sig = collect_signals(target_dir=tmp_path)
    assert sig.workspace_lock == "ADP"


def test_signals_pull_sample_data(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "orders.csv").write_text("id,name\n1,a")
    sig = collect_signals(target_dir=tmp_path)
    assert any("orders.csv" in p for p in sig.sample_files)


def test_signals_pull_domain_from_workspace_config(tmp_path):
    """Workspace yaml's domain is honored (gap #9 — domain inferable)."""
    import yaml as _yaml

    (tmp_path / "fluid.workspace.yaml").write_text(
        _yaml.safe_dump({"workspace": {"name": "ws", "domain": "commerce"}})
    )
    sig = collect_signals(target_dir=tmp_path)
    assert sig.domain == "commerce"


# ---------------------------------------------------------------------------
# 2. Every prompt has a concrete example
# ---------------------------------------------------------------------------


def test_every_question_has_example():
    for q in _QUESTIONS:
        assert q.example, f"{q.key} prompt must carry a concrete example"
        assert len(q.example) > 5, f"{q.key} example too short to be useful"


# ---------------------------------------------------------------------------
# 3. Industry / use-case redesign — no long Other-defaulting list
# ---------------------------------------------------------------------------


def test_no_industry_or_use_case_question_in_world_class_set():
    """Gap #3: those long numbered lists with Other default are GONE."""
    keys = {q.key for q in _QUESTIONS}
    assert "industry" not in keys
    assert "use_case" not in keys


# ---------------------------------------------------------------------------
# 4. Adaptive — needs_asking respects context
# ---------------------------------------------------------------------------


def test_needs_asking_returns_false_when_context_filled():
    q = next(q for q in _QUESTIONS if q.key == "domain")
    sig = InterviewSignals()
    assert needs_asking(q, {"domain": "commerce"}, sig) is False


def test_needs_asking_returns_false_when_inferred():
    q = next(q for q in _QUESTIONS if q.key == "domain")
    sig = InterviewSignals(domain="commerce")  # workspace says commerce
    ctx: Dict[str, Any] = {}
    assert needs_asking(q, ctx, sig) is False
    assert ctx["domain"] == "commerce"  # mutated in place


# ---------------------------------------------------------------------------
# 5. "Got sample data?" skipped when sample data detected
# ---------------------------------------------------------------------------


def test_data_sources_skipped_when_sample_data_detected():
    q = next(q for q in _QUESTIONS if q.key == "data_sources")
    sig = InterviewSignals(sample_files=["data/orders.csv"])
    assert needs_asking(q, {}, sig) is False


def test_data_sources_skipped_when_existing_products_in_workspace():
    """When the user has existing products, "what data sources?" is wrong —
    the products ARE the sources for compose."""
    q = next(q for q in _QUESTIONS if q.key == "data_sources")
    sig = InterviewSignals(existing_products=3)
    assert needs_asking(q, {}, sig) is False


# ---------------------------------------------------------------------------
# 6. :auto escape applies defaults to remaining questions
# ---------------------------------------------------------------------------


def test_auto_mode_skips_all_prompts():
    state = _State()
    state.normalized_context["project_goal"] = "x"  # already known
    sig = InterviewSignals(workspace_lock="ADP", domain="commerce", owner_team="t")
    with (
        mock.patch("fluid_build.cli._world_class_interview.collect_signals", return_value=sig),
        mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text") as ask,
    ):
        run_world_class_bootstrap(state=state, console=None, auto_mode=True)
    assert not ask.called, ":auto must not prompt"
    assert state.normalized_context.get("data_product_type") == "ADP"


def test_auto_typed_midflow_fills_remaining_with_defaults():
    state = _State()
    sig = InterviewSignals(domain="commerce", owner_team="t")
    answers = iter(
        [
            "daily Stripe pricing",  # goal
            ":auto",  # second prompt — :auto kicks in
        ]
    )

    def _ask(_c, prompt, **_kw):
        return next(answers)

    with (
        mock.patch("fluid_build.cli._world_class_interview.collect_signals", return_value=sig),
        mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text", _ask),
    ):
        run_world_class_bootstrap(state=state, console=None)

    # Goal captured; rest auto-filled
    assert state.normalized_context["project_goal"] == "daily Stripe pricing"
    # data_product_type filled by :auto via default
    assert state.normalized_context.get("data_product_type")


# ---------------------------------------------------------------------------
# 7. productType is in the question set (surfaced early, not buried)
# ---------------------------------------------------------------------------


def test_data_product_type_question_present():
    keys = [q.key for q in _QUESTIONS]
    # productType question must appear in the first 2 (right after goal)
    assert "data_product_type" in keys
    assert keys.index("data_product_type") <= 1


def test_data_product_type_inferred_from_workspace_lock():
    q = next(q for q in _QUESTIONS if q.key == "data_product_type")
    sig = InterviewSignals(workspace_lock="CDP")
    ctx: Dict[str, Any] = {}
    assert needs_asking(q, ctx, sig) is False
    assert ctx["data_product_type"] == "CDP"


# ---------------------------------------------------------------------------
# 8. Progress indicator + cost cap
# ---------------------------------------------------------------------------


def test_run_renders_inferences_panel(tmp_path):
    """Detect-first promise: the user sees what we know BEFORE the questions."""
    from fluid_build.cli.workspace_config import save_workspace_config

    save_workspace_config(tmp_path, name="ws")
    state = _State()

    captured_panels: List[Any] = []

    class _Cons:
        def print(self, *args, **_kw):
            captured_panels.append(args)

    def _ask(_c, prompt, **_kw):
        return ":auto"

    with mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text", _ask):
        run_world_class_bootstrap(
            state=state,
            console=_Cons(),
            target_dir=tmp_path,
        )
    # At least one panel rendered (the "What I see" inferences panel)
    assert captured_panels, "World-class bootstrap must render the inferences panel"


def test_progress_format_contains_question_position():
    from fluid_build.cli._world_class_interview import (
        InterviewProgress,
        _format_progress,
    )

    # ``asked`` is the 1-indexed position of the question we're about
    # to ask. The caller pre-increments before calling ``_format_progress``.
    p = InterviewProgress(asked=1, total=4, cumulative_usd=0.0, cap_usd=1.0)
    assert "Q1/4" in _format_progress(p)
    assert "$" in _format_progress(p)  # budget shown


def test_progress_format_no_overflow_on_last_question():
    """H23 regression: ``[Q3/2]`` on the second-of-two prompt is wrong;
    the counter must never exceed ``total``.
    """
    from fluid_build.cli._world_class_interview import (
        InterviewProgress,
        _format_progress,
    )

    p = InterviewProgress(asked=2, total=2)
    assert "Q2/2" in _format_progress(p)
    # And the production overflow path: if a caller ever pre-increments
    # past ``total`` (e.g. a re-asked question), the renderer clamps to
    # ``total`` rather than rendering ``Q3/2``.
    p_overflow = InterviewProgress(asked=3, total=2)
    assert "Q2/2" in _format_progress(p_overflow)
    assert "Q3/2" not in _format_progress(p_overflow)


def test_progress_runs_q1_through_qn_without_overflow():
    """H23: the world-class bootstrap loop pre-increments ``asked``
    before rendering each prompt, so the user sees Q1/N, Q2/N, ...,
    QN/N — never Q(N+1)/N or skipping Q1.
    """
    from typing import List
    from unittest import mock

    state = _State()
    sig = InterviewSignals()
    captured_prompts: List[str] = []

    def _capture(_console, prompt, **_kw):
        captured_prompts.append(prompt)
        return "answer"

    with (
        mock.patch("fluid_build.cli._world_class_interview.collect_signals", return_value=sig),
        mock.patch("fluid_build.cli.forge_dialogs.ask_friendly_text", _capture),
    ):
        run_world_class_bootstrap(state=state, console=None)

    n = len(captured_prompts)
    assert n >= 1
    # Expect Q1/n through Qn/n in order; never Q0 or Q(n+1).
    for i, prompt in enumerate(captured_prompts, start=1):
        assert f"Q{i}/{n}" in prompt, f"prompt {i} should carry 'Q{i}/{n}', got: {prompt!r}"
        # Overflow guard: no Q(n+1)/n in any prompt.
        assert f"Q{n + 1}/{n}" not in prompt


# ---------------------------------------------------------------------------
# 9. Domain inferred from cwd name
# ---------------------------------------------------------------------------


def test_domain_inferred_from_cwd_name(tmp_path):
    """A cwd named 'stripe-pricing' suggests a 'stripe' domain."""
    target = tmp_path / "stripe-pricing-product"
    target.mkdir()
    sig = collect_signals(target_dir=target)
    # cwd_name should be set even if no workspace config exists
    assert sig.cwd_name == "stripe-pricing-product"


def test_domain_question_inferred_when_signal_present():
    q = next(q for q in _QUESTIONS if q.key == "domain")
    sig = InterviewSignals(cwd_name="commerce-orders-pipeline")
    ctx: Dict[str, Any] = {}
    needs_asking(q, ctx, sig)  # mutates ctx via inference
    assert ctx.get("domain") == "commerce"


# ---------------------------------------------------------------------------
# 10. Schema-coverage gate
# ---------------------------------------------------------------------------


def test_assess_coverage_marks_missing_fields():
    rep = assess_coverage({})
    assert not rep.is_complete
    missing = set(rep.missing)
    # Must flag every required schema-relevant facet
    for field in ("name (or project_goal)", "domain", "owner_team", "layer/productType"):
        assert field in missing


def test_assess_coverage_complete_when_every_facet_filled():
    rep = assess_coverage(
        {
            "project_goal": "x",
            "domain": "commerce",
            "owner_team": "data",
            "data_product_type": "SDP",
            "data_sources": "data/orders.csv",
        }
    )
    assert rep.is_complete


def test_assess_coverage_consumes_satisfies_data_source_requirement():
    """Compose mode populates ``consumes`` instead of ``data_sources`` —
    the schema-coverage check honors that."""
    rep = assess_coverage(
        {
            "project_goal": "x",
            "domain": "commerce",
            "owner_team": "data",
            "data_product_type": "ADP",
            "consumes": [{"productId": "x.y.z", "exposeId": "main"}],
        }
    )
    assert rep.is_complete


def test_assess_coverage_seed_override_satisfies_data_source_requirement():
    """Refine mode populates seed_contract_override — also fine."""
    rep = assess_coverage(
        {
            "project_goal": "x",
            "domain": "commerce",
            "owner_team": "data",
            "data_product_type": "SDP",
            "seed_contract_override": {"id": "x.y.z"},
        }
    )
    assert rep.is_complete


# ---------------------------------------------------------------------------
# Compose / refine still skip the whole thing
# ---------------------------------------------------------------------------


def test_streaming_contract_preview_renders_after_each_answer(tmp_path, silent_console=None):
    """Phase 3 #4: contract grows visibly after every answer."""
    from fluid_build.cli._streaming_contract_preview import (
        shape_contract_from_context,
    )

    # Empty context — produces a placeholder schema-shaped contract
    contract_empty = shape_contract_from_context({})
    assert contract_empty["fluidVersion"] == "0.7.3"
    assert contract_empty["kind"] == "DataProduct"

    # Mid-flow: only goal answered
    contract_partial = shape_contract_from_context({"project_goal": "stripe pricing"})
    assert contract_partial["name"] == "stripe pricing"
    assert contract_partial["domain"] == "tbd"  # not answered yet

    # Fully answered: contract is complete
    contract_full = shape_contract_from_context(
        {
            "project_goal": "stripe pricing",
            "data_product_type": "SDP",
            "domain": "commerce",
            "owner_team": "data-platform",
        }
    )
    assert contract_full["domain"] == "commerce"
    assert contract_full["metadata"]["productType"] == "SDP"
    assert contract_full["metadata"]["layer"] == "Bronze"  # canonical pair


def test_streaming_preview_skipped_via_env_var(tmp_path, monkeypatch):
    """FLUID_FORGE_NO_STREAMING_PREVIEW=1 disables the live preview."""
    from fluid_build.cli._world_class_interview import run_world_class_bootstrap

    monkeypatch.setenv("FLUID_FORGE_NO_STREAMING_PREVIEW", "1")
    state = type(
        "S",
        (),
        {
            "normalized_context": {"project_goal": "x"},
            "apply_patch": lambda self, p, source="": self.normalized_context.update(p),
            "add_assumptions": lambda self, items: None,
            "record_turn": lambda self, **k: None,
            "ready": False,
        },
    )()
    streaming_render_calls = {"count": 0}

    def _spy_render(*_a, **_kw):
        streaming_render_calls["count"] += 1

    with mock.patch(
        "fluid_build.cli._streaming_contract_preview.render_growing_contract",
        _spy_render,
    ):
        run_world_class_bootstrap(state=state, console=None, target_dir=tmp_path)

    assert (
        streaming_render_calls["count"] == 0
    ), "Streaming preview must respect FLUID_FORGE_NO_STREAMING_PREVIEW"


def test_world_class_not_invoked_in_compose_mode():
    """Compose mode runs the dedicated _run_compose_interview, NOT the
    world-class bootstrap (different flow).
    """
    from fluid_build.cli import forge_copilot_interview as iv

    flags = {"world_class": 0, "compose": 0}

    def _spy_world(*_a, **_kw):
        flags["world_class"] += 1

    def _spy_compose(*_a, **_kw):
        flags["compose"] += 1

    with (
        mock.patch(
            "fluid_build.cli._world_class_interview.run_world_class_bootstrap",
            _spy_world,
        ),
        mock.patch.object(iv, "_run_compose_interview", _spy_compose),
    ):
        iv.run_adaptive_copilot_interview(
            initial_context={"composition": {"upstream_products": [{"id": "x.y.z"}]}},
            console=None,
            llm_config=mock.MagicMock(),
            discovery_report=mock.MagicMock(sample_files=[]),
            capability_matrix={},
            project_memory=None,
        )

    assert flags["world_class"] == 0, "compose mode must skip world-class bootstrap"
    assert flags["compose"] == 1
