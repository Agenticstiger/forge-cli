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

"""Per-product cost ceiling — ``FLUID_COST_LIMIT_USD_PER_PRODUCT``.

The per-RUN ceiling (``FLUID_COST_LIMIT_USD``) caps the aggregate of
a single CLI invocation. The per-PRODUCT ceiling adds a second cap
that fires as soon as ANY single product's running spend exceeds the
limit, even if the run aggregate hasn't crossed the per-run cap. This
matters for ``--from-product-list`` invocations that compose many
products in one run — without per-product gating, a single runaway
product could quietly burn the whole per-run budget.

Pin:

1. **Push/pop product** correctly tracks the current scope.
2. **Per-product attribution** credits ``record_call`` to the top of
   the product stack.
3. **Per-product ceiling fires** when one product crosses the cap,
   even if the per-run total is well under its (much larger) cap.
4. **Both ceilings stack** — per-product fires first when applicable,
   per-run fires after.
5. **Reset clears the stack** — tests are hermetic.
"""

from __future__ import annotations

import pytest

from fluid_build.copilot.cost import (
    CostLimitExceeded,
    check_cost_ceiling,
    get_run_tracker,
    reset_run_tracker,
)


@pytest.fixture(autouse=True)
def _reset_tracker():
    """Each test starts with an empty tracker + clean env."""
    reset_run_tracker()
    yield
    reset_run_tracker()


class TestProductStack:
    def test_push_pop_peek(self):
        tr = get_run_tracker()
        assert tr.current_product() is None
        tr.push_product("alpha")
        assert tr.current_product() == "alpha"
        tr.push_product("beta")
        assert tr.current_product() == "beta"
        assert tr.pop_product() == "beta"
        assert tr.current_product() == "alpha"
        assert tr.pop_product() == "alpha"
        assert tr.current_product() is None
        # Defensive: pop on empty returns None, doesn't raise.
        assert tr.pop_product() is None

    def test_push_empty_string_is_noop(self):
        tr = get_run_tracker()
        tr.push_product("")
        assert tr.current_product() is None


class TestPerProductAttribution:
    def test_record_call_attributes_to_top_of_stack(self):
        tr = get_run_tracker()
        tr.push_product("orders_v1")
        tr.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1000,
            output_tokens=500,
            usd_override=0.0030,
        )
        assert tr.per_product_usd("orders_v1") == 0.003

    def test_explicit_product_id_overrides_stack(self):
        tr = get_run_tracker()
        tr.push_product("on_stack")
        tr.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=100,
            output_tokens=50,
            usd_override=0.001,
            product_id="explicit",
        )
        assert tr.per_product_usd("explicit") == 0.001
        # Stack-pushed product saw zero — explicit wins.
        assert tr.per_product_usd("on_stack") == 0.0

    def test_no_product_routes_to_unattributed_bucket(self):
        tr = get_run_tracker()
        tr.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=100,
            output_tokens=50,
            usd_override=0.001,
        )
        # Unattributed bucket lives under empty-string key.
        assert tr.per_product_usd("") == 0.001


class TestCeilingFires:
    def test_per_product_ceiling_fires_when_product_exceeds(self, monkeypatch):
        monkeypatch.setenv("FLUID_COST_LIMIT_USD_PER_PRODUCT", "0.005")
        tr = get_run_tracker()
        tr.push_product("expensive_product")
        tr.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=10_000,
            output_tokens=5_000,
            usd_override=0.010,
        )
        with pytest.raises(CostLimitExceeded) as exc_info:
            check_cost_ceiling()
        assert exc_info.value.running_usd == 0.01
        assert exc_info.value.limit_usd == 0.005

    def test_per_product_ceiling_no_op_when_no_product_pushed(self, monkeypatch):
        """When nothing's on the stack, the per-product ceiling
        doesn't fire (the unattributed bucket isn't gated)."""
        monkeypatch.setenv("FLUID_COST_LIMIT_USD_PER_PRODUCT", "0.001")
        tr = get_run_tracker()
        tr.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=10_000,
            output_tokens=5_000,
            usd_override=0.010,
        )
        # No exception — the unattributed bucket isn't gated by
        # per-product limits (per-run still applies if set).
        check_cost_ceiling()

    def test_per_product_fires_before_per_run(self, monkeypatch):
        """When BOTH limits are set and the per-product is tighter,
        the per-product ceiling fires first."""
        monkeypatch.setenv("FLUID_COST_LIMIT_USD_PER_PRODUCT", "0.001")
        monkeypatch.setenv("FLUID_COST_LIMIT_USD", "1.000")
        tr = get_run_tracker()
        tr.push_product("p1")
        tr.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=10_000,
            output_tokens=5_000,
            usd_override=0.005,
        )
        with pytest.raises(CostLimitExceeded) as exc_info:
            check_cost_ceiling()
        # The per-product cap (0.001) fires; per-run (1.000) wouldn't.
        assert exc_info.value.limit_usd == 0.001

    def test_no_per_product_limit_falls_through_to_per_run(self, monkeypatch):
        monkeypatch.delenv("FLUID_COST_LIMIT_USD_PER_PRODUCT", raising=False)
        monkeypatch.setenv("FLUID_COST_LIMIT_USD", "0.001")
        tr = get_run_tracker()
        tr.push_product("p1")
        tr.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=10_000,
            output_tokens=5_000,
            usd_override=0.005,
        )
        with pytest.raises(CostLimitExceeded) as exc_info:
            check_cost_ceiling()
        assert exc_info.value.limit_usd == 0.001


class TestResetClears:
    def test_reset_clears_stack_and_per_product(self):
        tr = get_run_tracker()
        tr.push_product("p1")
        tr.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=100,
            output_tokens=50,
            usd_override=0.001,
        )
        assert tr.current_product() == "p1"
        assert tr.per_product_usd("p1") == 0.001

        reset_run_tracker()

        tr = get_run_tracker()
        assert tr.current_product() is None
        # After reset, the bucket for p1 is empty (returns 0.0 since
        # the entry doesn't exist anymore).
        assert tr.per_product_usd("p1") == 0.0
