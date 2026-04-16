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

"""Tests for fluid_build/providers/base.py — BaseProvider, PlanAction, ApplyResult, hooks."""

import json

import pytest

from fluid_build.providers.base import (
    ApplyResult,
    BaseProvider,
    CostEstimate,
    PlanAction,
    ProviderCapabilities,
    ProviderError,
    ProviderHookSpec,
    ProviderMetadata,
    has_hook,
    invoke_hook,
)

# ── PlanAction ──────────────────────────────────────────────────────────


class TestPlanAction:
    def test_basic(self):
        a = PlanAction(action_type="create", op="create", resource_id="table.users")
        assert a.action_type == "create"
        assert a.op == "create"
        assert a.resource_id == "table.users"
        assert a.params == {}

    def test_with_params(self):
        a = PlanAction("update", "update", "bucket/path", params={"force": True})
        assert a.params["force"] is True


# ── ApplyResult ─────────────────────────────────────────────────────────


class TestApplyResult:
    def _result(self):
        return ApplyResult(
            provider="gcp",
            applied=3,
            failed=1,
            duration_sec=2.5,
            timestamp="2025-01-01T00:00:00Z",
            results=[{"id": "r1", "status": "ok"}],
        )

    def test_attrs(self):
        r = self._result()
        assert r.provider == "gcp"
        assert r.applied == 3
        assert r.failed == 1

    def test_to_json(self):
        r = self._result()
        data = json.loads(r.to_json())
        assert data["provider"] == "gcp"
        assert data["applied"] == 3

    def test_get(self):
        r = self._result()
        assert r.get("provider") == "gcp"
        assert r.get("nonexistent", "default") == "default"

    def test_getitem(self):
        r = self._result()
        assert r["provider"] == "gcp"
        with pytest.raises(KeyError):
            _ = r["nonexistent"]

    def test_contains(self):
        r = self._result()
        assert "provider" in r
        assert "nope" not in r


# ── ProviderError ───────────────────────────────────────────────────────


class TestProviderError:
    def test_is_runtime_error(self):
        err = ProviderError("bad thing")
        assert isinstance(err, RuntimeError)
        assert str(err) == "bad thing"


# ── ProviderMetadata ────────────────────────────────────────────────────


class TestProviderMetadata:
    def test_defaults(self):
        m = ProviderMetadata(name="test_provider")
        assert m.name == "test_provider"
        assert m.display_name == "Test Provider"
        assert m.version == "0.0.0"

    def test_custom_display_name(self):
        m = ProviderMetadata(name="x", display_name="Custom Name")
        assert m.display_name == "Custom Name"

    def test_to_dict(self):
        m = ProviderMetadata(name="test", version="1.0.0", author="Me")
        d = m.to_dict()
        assert d["name"] == "test"
        assert d["version"] == "1.0.0"
        assert d["author"] == "Me"
        assert "supported_platforms" in d


# ── ProviderCapabilities ────────────────────────────────────────────────


class TestProviderCapabilities:
    def test_defaults(self):
        c = ProviderCapabilities()
        assert c["planning"] is True
        assert c["apply"] is True
        assert c["render"] is False
        assert c.get("auth") is False

    def test_custom(self):
        c = ProviderCapabilities(render=True, graph=True)
        assert c["render"] is True
        assert c["graph"] is True

    def test_contains(self):
        c = ProviderCapabilities()
        assert "planning" in c
        assert "nonexistent" not in c

    def test_iteration(self):
        c = ProviderCapabilities()
        keys = list(c.keys())
        assert "planning" in keys
        assert len(c) >= 4


# ── BaseProvider ────────────────────────────────────────────────────────


class ConcreteProvider(BaseProvider):
    name = "test"

    def plan(self, contract):
        return [{"action_type": "create", "resource_id": "test_resource"}]

    def apply(self, actions):
        return ApplyResult(
            provider="test",
            applied=1,
            failed=0,
            duration_sec=0.1,
            timestamp="2025-01-01T00:00:00Z",
        )


class TestBaseProvider:
    def test_instantiation(self):
        p = ConcreteProvider(project="proj1", region="us-central1")
        assert p.project == "proj1"
        assert p.region == "us-central1"
        assert p.logger is not None

    def test_plan(self):
        p = ConcreteProvider()
        actions = p.plan({"id": "test"})
        assert len(actions) == 1

    def test_apply(self):
        p = ConcreteProvider()
        result = p.apply([])
        assert result.applied == 1

    def test_capabilities(self):
        p = ConcreteProvider()
        caps = p.capabilities()
        assert caps["planning"] is True

    def test_render_not_supported(self):
        p = ConcreteProvider()
        with pytest.raises(ProviderError, match="not supported"):
            p.render("src")

    def test_require_pass(self):
        p = ConcreteProvider()
        p.require(True, "should not raise")

    def test_require_fail(self):
        p = ConcreteProvider()
        with pytest.raises(ProviderError):
            p.require(False, "precondition failed")

    def test_debug_kv(self):
        p = ConcreteProvider()
        p.debug_kv(key="value")  # should not raise

    def test_info_kv(self):
        p = ConcreteProvider()
        p.info_kv(key="value")

    def test_warn_kv(self):
        p = ConcreteProvider()
        p.warn_kv(key="value")

    def test_err_kv(self):
        p = ConcreteProvider()
        p.err_kv(key="value")

    def test_get_provider_info(self):
        info = ConcreteProvider.get_provider_info()
        assert info.name == "test"

    def test_extra_kwargs(self):
        p = ConcreteProvider(custom_flag=True)
        assert p.extra["custom_flag"] is True


# ── CostEstimate ────────────────────────────────────────────────────────


class TestCostEstimate:
    def test_defaults(self):
        c = CostEstimate()
        assert c.currency == "USD"
        assert c.total() == 0.0

    def test_with_values(self):
        c = CostEstimate(monthly=100.0, one_time=50.0)
        assert c.total() == 150.0

    def test_to_dict(self):
        c = CostEstimate(monthly=10.0, notes="estimate")
        d = c.to_dict()
        assert d["monthly"] == 10.0
        assert d["notes"] == "estimate"
        assert d["total"] == 10.0


# ── Hook utilities ──────────────────────────────────────────────────────


class TestHookSpec:
    def test_default_hooks(self):
        spec = ProviderHookSpec()
        assert spec.pre_plan({"id": "x"}) == {"id": "x"}
        assert spec.post_plan([1, 2]) == [1, 2]
        assert spec.pre_apply([]) == []
        assert spec.post_apply(None) is None
        assert spec.on_error(None, None) is None
        assert spec.estimate_cost([]) is None
        assert spec.validate_sovereignty({}) == []


class TestInvokeHook:
    def test_invoke_existing(self):
        class MyHook:
            def pre_plan(self, contract):
                return {"modified": True}

        result = invoke_hook(MyHook(), "pre_plan", {"id": "x"})
        assert result == {"modified": True}

    def test_invoke_missing(self):
        result = invoke_hook(object(), "nonexistent", "fallback_val")
        assert result == "fallback_val"

    def test_invoke_missing_no_args(self):
        result = invoke_hook(object(), "nonexistent")
        assert result is None

    def test_invoke_with_error(self):
        class BadHook:
            def pre_plan(self, contract):
                raise ValueError("oops")

        result = invoke_hook(BadHook(), "pre_plan", {"id": "x"})
        assert result == {"id": "x"}  # falls back to first arg


class TestHasHook:
    def test_has_custom_hook(self):
        class MyProvider:
            def pre_plan(self, contract):
                pass

        assert has_hook(MyProvider(), "pre_plan") is True

    def test_missing_hook(self):
        assert has_hook(object(), "pre_plan") is False

    def test_default_hook_spec(self):
        spec = ProviderHookSpec()
        # Default implementation should return False
        assert has_hook(spec, "pre_plan") is False
