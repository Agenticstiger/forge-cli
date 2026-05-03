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

"""Phase 3.6 — per-agent cost-budget ceiling.

The post-hoc ``check_cost_ceiling()`` was the only enforcement point;
it ran AFTER each ``record_call`` so a runaway agent could already
have spent the budget by the time the limit was checked. The new
``predict_call_cost`` helper projects the running total + the next
call's estimated cost so the agent base class can abort BEFORE the
spend.

Pin:

1. **No limit configured** → ``predict_call_cost`` always returns
   would_exceed=False.
2. **Under limit** → would_exceed=False; projected reflects the call's
   estimated cost.
3. **Over limit** → would_exceed=True; projected reflects the
   over-budget total.
4. **Unknown model** → estimate=$0 → never exceeds (we don't pre-flight-block on unknown cost).
5. **Ollama** → cost=$0 → never exceeds.
6. **Existing record_call'd cost** is added to the projection.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from fluid_build.copilot.cost import (
    get_run_tracker,
    predict_call_cost,
    reset_run_tracker,
)


@pytest.fixture(autouse=True)
def _force_embedded_price_table(monkeypatch):
    """Disable litellm so the embedded MODEL_PRICES_USD table prices
    each call. Keeps the per-call USD deterministic regardless of
    litellm's upstream pricing changes (same posture as the broader
    ``test_cost_tracking.py`` fixture)."""
    fake_litellm = type(sys)("_test_fake_litellm_per_agent")
    fake_litellm.cost_per_token = lambda **_kw: (_ for _ in ()).throw(
        RuntimeError("litellm disabled for embedded-fallback test")
    )
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)


@pytest.fixture(autouse=True)
def _reset_tracker():
    reset_run_tracker()
    yield
    reset_run_tracker()


# ---------------------------------------------------------------------------
# Behaviour 1 — no limit configured
# ---------------------------------------------------------------------------


def test_no_limit_returns_no_exceed(monkeypatch):
    monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)
    would_exceed, projected, limit = predict_call_cost(
        provider="openai",
        model="gpt-4.1-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert would_exceed is False
    assert limit is None
    # Projection still computed so callers can show a forecast.
    assert projected > 0


# ---------------------------------------------------------------------------
# Behaviour 2 — under limit
# ---------------------------------------------------------------------------


def test_under_limit_returns_no_exceed(monkeypatch):
    monkeypatch.setenv("FLUID_COST_LIMIT_USD", "10.0")
    would_exceed, projected, limit = predict_call_cost(
        provider="openai",
        model="gpt-4.1-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # gpt-4.1-mini at 1M+1M = $0.75, well under $10.
    assert would_exceed is False
    assert projected == pytest.approx(0.75)
    assert limit == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Behaviour 3 — over limit
# ---------------------------------------------------------------------------


def test_over_limit_returns_would_exceed_true(monkeypatch):
    monkeypatch.setenv("FLUID_COST_LIMIT_USD", "0.01")
    would_exceed, projected, limit = predict_call_cost(
        provider="openai",
        model="gpt-4.1-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # $0.75 projected > $0.01 limit.
    assert would_exceed is True
    assert projected > limit


# ---------------------------------------------------------------------------
# Behaviour 4 — unknown model = $0 estimate, never exceeds
# ---------------------------------------------------------------------------


def test_unknown_model_estimate_zero_never_exceeds(monkeypatch):
    """When we can't price the planned call, don't pre-flight block —
    the post-hoc check_cost_ceiling() catches it after the spend."""
    monkeypatch.setenv("FLUID_COST_LIMIT_USD", "0.01")
    would_exceed, projected, _limit = predict_call_cost(
        provider="openai",
        model="totally-fictional-model-9000",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # Estimate falls back to $0 → projection = running ($0) + 0 = 0.
    assert would_exceed is False
    assert projected == 0.0


# ---------------------------------------------------------------------------
# Behaviour 5 — Ollama = $0
# ---------------------------------------------------------------------------


def test_ollama_never_exceeds(monkeypatch):
    monkeypatch.setenv("FLUID_COST_LIMIT_USD", "0.0001")
    would_exceed, projected, _limit = predict_call_cost(
        provider="ollama",
        model="llama3.1",
        input_tokens=10_000_000,
        output_tokens=5_000_000,
    )
    assert would_exceed is False
    assert projected == 0.0


# ---------------------------------------------------------------------------
# Behaviour 6 — existing recorded cost is summed into projection
# ---------------------------------------------------------------------------


def test_existing_recorded_cost_is_added_to_projection(monkeypatch):
    """Already-spent USD + the next call's estimate must combine to
    push past a budget, not just the next call alone."""
    monkeypatch.setenv("FLUID_COST_LIMIT_USD", "1.0")

    # Record a prior call worth $0.75.
    get_run_tracker().record_call(
        provider="openai",
        model="gpt-4.1-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )

    # Next call alone is $0.75; running ($0.75) + next ($0.75) = $1.50 > $1.0.
    would_exceed, projected, _limit = predict_call_cost(
        provider="openai",
        model="gpt-4.1-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert would_exceed is True
    assert projected == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Behaviour 7 — projection without limit is still useful
# ---------------------------------------------------------------------------


def test_projection_without_limit_returns_running_plus_estimate(monkeypatch):
    """``would_exceed=False, limit=None`` but the projected number is
    still computed so callers (UIs, dashboards) can display the
    forecast even when no ceiling is configured."""
    monkeypatch.delenv("FLUID_COST_LIMIT_USD", raising=False)

    get_run_tracker().record_call(
        provider="openai",
        model="gpt-4.1-mini",
        input_tokens=2_000_000,
        output_tokens=1_000_000,
    )

    _exceed, projected, _limit = predict_call_cost(
        provider="openai",
        model="gpt-4.1-mini",
        input_tokens=1_000_000,
        output_tokens=500_000,
    )
    # Running: 2M*$0.15 + 1M*$0.60 = $0.30 + $0.60 = $0.90
    # Estimate: 1M*$0.15 + 0.5M*$0.60 = $0.15 + $0.30 = $0.45
    # Projected = $1.35
    assert projected == pytest.approx(0.9 + 0.45)
