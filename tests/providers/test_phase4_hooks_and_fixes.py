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

# tests/providers/test_phase4_hooks_and_fixes.py
"""
Phase 4: Lifecycle Hooks + Capabilities v2  |  Bugfix validation
=================================================================

Tests cover:
  - SDK hook spec (ProviderHookSpec, CostEstimate, invoke_hook, has_hook)
  - CLI hook integration (run_pre_plan, run_post_plan, etc.)
  - ProviderCapabilities v2 (new fields: dry_run, rollback, cost_estimation, ...)
  - Bugfix validation:
      H1  proxy dicts for PROVIDERS / DISCOVERY_ERRORS
      H2  targeted TypeError catch in build_provider
      H3  ProviderCapabilities __iter__, __len__, extra
      M1  ConsumeSpec operator precedence
      M2  LocalProvider.apply return type
      M3  LocalProvider.render signature aligned with ABC
      M4  resolve_provider_from_contract top-level binding
      M5  _now_iso uses datetime.now(timezone.utc)
      M6  _determine_source_table deterministic fallback
      M7  protocol version checking
"""

from __future__ import annotations

import logging
from typing import List

import pytest

# ---------------------------------------------------------------------------
# SDK imports
# ---------------------------------------------------------------------------
try:
    from fluid_provider_sdk import (
        ApplyResult,
        BaseProvider,
        ConsumeSpec,
        CostEstimate,
        ProviderCapabilities,
        ProviderHookSpec,
        has_hook,
        invoke_hook,
    )

    HAS_SDK = True
except ImportError:
    HAS_SDK = False

pytestmark = pytest.mark.skipif(not HAS_SDK, reason="fluid-provider-sdk not installed")

# ===================================================================
# SECTION 1: CostEstimate
# ===================================================================


class TestCostEstimate:
    def test_defaults(self):
        c = CostEstimate()
        assert c.currency == "USD"
        assert c.monthly == 0.0
        assert c.one_time == 0.0
        assert c.total() == 0.0

    def test_total(self):
        c = CostEstimate(monthly=10.5, one_time=5.0)
        assert c.total() == 15.5

    def test_to_dict(self):
        c = CostEstimate(currency="EUR", monthly=42.0, one_time=0.0, notes="test")
        d = c.to_dict()
        assert d["currency"] == "EUR"
        assert d["total"] == 42.0
        assert d["notes"] == "test"


# ===================================================================
# SECTION 2: ProviderHookSpec (no-op defaults)
# ===================================================================


class TestProviderHookSpec:
    def test_pre_plan_passthrough(self):
        spec = ProviderHookSpec()
        contract = {"id": "test"}
        assert spec.pre_plan(contract) is contract

    def test_post_plan_passthrough(self):
        spec = ProviderHookSpec()
        actions = [{"op": "a"}]
        assert spec.post_plan(actions) is actions

    def test_pre_apply_passthrough(self):
        spec = ProviderHookSpec()
        actions = [{"op": "b"}]
        assert spec.pre_apply(actions) is actions

    def test_post_apply_noop(self):
        spec = ProviderHookSpec()
        assert spec.post_apply({}) is None

    def test_on_error_noop(self):
        spec = ProviderHookSpec()
        assert spec.on_error(RuntimeError("x"), {}) is None

    def test_estimate_cost_returns_none(self):
        spec = ProviderHookSpec()
        assert spec.estimate_cost([]) is None

    def test_validate_sovereignty_returns_empty(self):
        spec = ProviderHookSpec()
        assert spec.validate_sovereignty({}) == []


# ===================================================================
# SECTION 3: invoke_hook / has_hook
# ===================================================================


class TestInvokeHook:
    def test_invoke_existing_hook(self):
        class P:
            def pre_plan(self, contract):
                return {**contract, "injected": True}

        p = P()
        result = invoke_hook(p, "pre_plan", {"id": "c1"})
        assert result["injected"] is True

    def test_invoke_missing_hook_passthrough(self):
        class P:
            pass

        p = P()
        result = invoke_hook(p, "pre_plan", {"id": "c1"})
        assert result == {"id": "c1"}

    def test_invoke_hook_exception_passthrough(self):
        class P:
            def pre_plan(self, contract):
                raise ValueError("boom")

        p = P()
        result = invoke_hook(p, "pre_plan", {"id": "c1"})
        # Should return first arg on failure
        assert result == {"id": "c1"}

    def test_invoke_no_args_returns_none(self):
        class P:
            pass

        p = P()
        assert invoke_hook(p, "missing_hook") is None

    def test_has_hook_true(self):
        class P:
            def estimate_cost(self, actions):
                return CostEstimate(monthly=1.0)

        assert has_hook(P(), "estimate_cost") is True

    def test_has_hook_false(self):
        class P:
            pass

        assert has_hook(P(), "estimate_cost") is False

    def test_has_hook_detects_noop_default(self):
        """If a provider subclasses ProviderHookSpec but doesn't override,
        has_hook should return False."""

        class P(ProviderHookSpec):
            pass

        assert has_hook(P(), "pre_plan") is False

    def test_has_hook_detects_real_override(self):
        class P(ProviderHookSpec):
            def pre_plan(self, contract):
                return {**contract, "modified": True}

        assert has_hook(P(), "pre_plan") is True


# ===================================================================
# SECTION 4: CLI hook wrappers
# ===================================================================


class TestCLIHookWrappers:
    def _logger(self):
        return logging.getLogger("test_hooks")

    def test_run_pre_plan_no_hook(self):
        from fluid_build.cli.hooks import run_pre_plan

        class P:
            name = "test"

        contract = {"id": "c1"}
        assert run_pre_plan(P(), contract, self._logger()) is contract

    def test_run_pre_plan_with_hook(self):
        from fluid_build.cli.hooks import run_pre_plan

        class P(ProviderHookSpec):
            name = "test"

            def pre_plan(self, contract):
                return {**contract, "enriched": True}

        result = run_pre_plan(P(), {"id": "c1"}, self._logger())
        assert result["enriched"] is True

    def test_run_post_plan_no_hook(self):
        from fluid_build.cli.hooks import run_post_plan

        class P:
            name = "test"

        actions = [{"op": "a"}]
        assert run_post_plan(P(), actions, self._logger()) is actions

    def test_run_pre_apply_passthrough(self):
        from fluid_build.cli.hooks import run_pre_apply

        class P:
            name = "test"

        actions = [{"op": "a"}]
        assert run_pre_apply(P(), actions, self._logger()) is actions

    def test_run_post_apply_noop(self):
        from fluid_build.cli.hooks import run_post_apply

        class P:
            name = "test"

        # Should not raise
        run_post_apply(P(), {"applied": 1}, self._logger())

    def test_run_on_error_noop(self):
        from fluid_build.cli.hooks import run_on_error

        class P:
            name = "test"

        run_on_error(P(), RuntimeError("x"), "plan", self._logger())

    def test_run_estimate_cost_none(self):
        from fluid_build.cli.hooks import run_estimate_cost

        class P:
            name = "test"

        assert run_estimate_cost(P(), [], self._logger()) is None

    def test_run_estimate_cost_with_hook(self):
        from fluid_build.cli.hooks import run_estimate_cost

        class P(ProviderHookSpec):
            name = "test"

            def estimate_cost(self, actions):
                return CostEstimate(monthly=5.0)

        result = run_estimate_cost(P(), [{"op": "a"}], self._logger())
        assert result is not None
        assert result.monthly == 5.0

    def test_run_validate_sovereignty_empty(self):
        from fluid_build.cli.hooks import run_validate_sovereignty

        class P:
            name = "test"

        assert run_validate_sovereignty(P(), {}, self._logger()) == []

    def test_run_validate_sovereignty_with_hook(self):
        from fluid_build.cli.hooks import run_validate_sovereignty

        class P(ProviderHookSpec):
            name = "test"

            def validate_sovereignty(self, contract):
                return ["Data resides outside EU"]

        result = run_validate_sovereignty(P(), {}, self._logger())
        assert result == ["Data resides outside EU"]


# ===================================================================
# SECTION 5: ProviderCapabilities v2 (new fields)
# ===================================================================


class TestProviderCapabilitiesV2:
    def test_new_fields_default_false(self):
        cap = ProviderCapabilities()
        assert cap.dry_run is False
        assert cap.rollback is False
        assert cap.cost_estimation is False
        assert cap.schema_validation is False
        assert cap.lineage is False
        assert cap.streaming is False

    def test_new_fields_in_dict(self):
        cap = ProviderCapabilities(cost_estimation=True, lineage=True)
        d = dict(cap.items())
        assert d["cost_estimation"] is True
        assert d["lineage"] is True
        assert d["rollback"] is False

    def test_len_includes_new_fields(self):
        cap = ProviderCapabilities()
        # 5 original + 6 new = 11 core + any extras
        assert len(cap) == 11

    def test_iter_includes_new_fields(self):
        cap = ProviderCapabilities()
        keys = list(cap)
        for expected in (
            "dry_run",
            "rollback",
            "cost_estimation",
            "schema_validation",
            "lineage",
            "streaming",
        ):
            assert expected in keys

    def test_extra_still_works(self):
        cap = ProviderCapabilities(extra={"custom_cap": True})
        assert cap["custom_cap"] is True
        assert len(cap) == 12  # 11 + 1 extra


# ===================================================================
# SECTION 6: Bugfix validation
# ===================================================================


class TestBugfixH1ProxyDicts:
    """H1: PROVIDERS and DISCOVERY_ERRORS in base.py should proxy to canonical."""

    def test_providers_proxy_reflects_canonical(self):
        from fluid_build.providers import PROVIDERS as canonical
        from fluid_build.providers.base import PROVIDERS

        # They should reflect the same data
        for name in canonical:
            assert name in PROVIDERS

    def test_discovery_errors_proxy_reflects_canonical(self):
        from fluid_build.providers import DISCOVERY_ERRORS as canonical
        from fluid_build.providers.base import DISCOVERY_ERRORS

        assert len(DISCOVERY_ERRORS) == len(canonical)


class TestBugfixH2TypeErrorCatch:
    """H2: build_provider should not swallow real TypeErrors."""

    def test_real_type_error_propagates(self):
        from fluid_build.providers import PROVIDERS, register_provider

        class BadProvider(BaseProvider):
            name = "bad_type_error_test"

            def plan(self, contract):
                return []

            def apply(self, actions):
                return ApplyResult(
                    provider="bad", applied=0, failed=0, duration_sec=0.0, timestamp="", results=[]
                )

            def __init__(self, **kwargs):
                raise TypeError("real bug in constructor")

        register_provider("bad_type_error_test", BadProvider, source="test")
        try:
            from fluid_build.cli._common import build_provider

            with pytest.raises(TypeError, match="real bug"):
                build_provider("bad_type_error_test", "proj", "region", logging.getLogger("test"))
        finally:
            PROVIDERS.pop("bad_type_error_test", None)


class TestBugfixH3Capabilities:
    """H3: ProviderCapabilities must have __iter__, __len__, extra."""

    def test_iter(self):
        cap = ProviderCapabilities()
        assert list(cap)  # should be iterable

    def test_len(self):
        cap = ProviderCapabilities()
        assert len(cap) > 0

    def test_extra_kwarg(self):
        cap = ProviderCapabilities(extra={"my_flag": True})
        assert cap["my_flag"] is True


class TestBugfixM1ConsumeSpec:
    """M1: ConsumeSpec.from_dict should handle both dict and string locations."""

    def test_string_location(self):
        spec = ConsumeSpec.from_dict({"source": "my-source", "location": "s3://bucket/path"})
        assert spec.path == "s3://bucket/path"

    def test_dict_location_with_path(self):
        spec = ConsumeSpec.from_dict(
            {"source": "my-source", "location": {"path": "/data/file.csv", "project": "proj"}}
        )
        assert spec.path == "/data/file.csv"


class TestBugfixM3RenderSignature:
    """M3: LocalProvider.render() should accept SDK-style src kwarg."""

    def test_render_accepts_src_kwarg(self):
        from fluid_build.providers.local.local import LocalProvider

        p = LocalProvider(project="test", region="local", logger=logging.getLogger("test"))
        # Should accept src as first positional — just verify no TypeError on signature
        import inspect

        sig = inspect.signature(p.render)
        params = list(sig.parameters.keys())
        assert "src" in params
        assert "out" in params
        assert "fmt" in params
        assert "plan" in params  # backward compat kwarg


class TestBugfixM4ResolveProviderTopLevel:
    """M4: resolve_provider_from_contract should check top-level binding.platform."""

    def test_snowflake_style_binding(self):
        from fluid_build.cli._common import resolve_provider_from_contract

        contract = {"binding": {"platform": "snowflake", "account": "xyz123"}}
        provider, location = resolve_provider_from_contract(contract)
        assert provider == "snowflake"


class TestBugfixM6DeterministicSourceTable:
    """M6: _determine_source_table should use sorted() for deterministic fallback."""

    def test_deterministic_fallback(self):
        from fluid_build.providers.local.planner import _determine_source_table

        logger = logging.getLogger("test")
        # With unordered set, sorted() should always pick alphabetically first
        loaded = {"zebra", "alpha", "mango"}
        result = _determine_source_table({}, [], loaded, logger)
        assert result == "alpha"

    def test_explicit_source_preferred(self):
        from fluid_build.providers.local.planner import _determine_source_table

        logger = logging.getLogger("test")
        loaded = {"zebra", "alpha"}
        result = _determine_source_table({"source_table": "explicit"}, [], loaded, logger)
        assert result == "explicit"


class TestBugfixM7VersionChecking:
    """M7: Protocol version checking at registration."""

    def test_check_sdk_compat_exists(self):
        from fluid_build.providers import _check_sdk_compat

        assert callable(_check_sdk_compat)

    def test_parse_version(self):
        from fluid_build.providers import _parse_version

        assert _parse_version("0.7.1") == (0, 7, 1)
        assert _parse_version("1.0.0") == (1, 0, 0)
        assert _parse_version("invalid") == (0, 0, 0)

    def test_compat_check_no_crash(self):
        """Version check should be advisory — never crash."""
        from fluid_build.providers import _check_sdk_compat

        # Passing a class that has no version info should not crash
        class FakeProvider:
            name = "fake"

        _check_sdk_compat("fake", FakeProvider, logging.getLogger("test"))


# ===================================================================
# SECTION 7: Integration — provider with hooks
# ===================================================================


class TestHookedProviderIntegration:
    """Test a provider that implements hooks end-to-end."""

    def _make_hooked_provider(self):
        class HookedProvider(BaseProvider, ProviderHookSpec):
            name = "hooked_test"
            call_log: List[str]

            def __init__(self, **kw):
                super().__init__(**kw)
                self.call_log = []

            def plan(self, contract):
                self.call_log.append("plan")
                return [{"op": "test_op", "params": {}}]

            def apply(self, actions):
                self.call_log.append("apply")
                return ApplyResult(
                    provider="hooked_test",
                    applied=1,
                    failed=0,
                    duration_sec=0.01,
                    timestamp="T",
                    results=[],
                )

            def pre_plan(self, contract):
                self.call_log.append("pre_plan")
                return {**contract, "hooked": True}

            def post_plan(self, actions):
                self.call_log.append("post_plan")
                return actions + [{"op": "audited", "injected_by": "hook"}]

            def pre_apply(self, actions):
                self.call_log.append("pre_apply")
                return actions

            def post_apply(self, result):
                self.call_log.append("post_apply")

            def estimate_cost(self, actions):
                self.call_log.append("estimate_cost")
                return CostEstimate(monthly=9.99, notes="test estimate")

            def validate_sovereignty(self, contract):
                self.call_log.append("validate_sovereignty")
                return []

        return HookedProvider(project="test", region="local", logger=logging.getLogger("test"))

    def test_has_hook_detects_overrides(self):
        p = self._make_hooked_provider()
        assert has_hook(p, "pre_plan") is True
        assert has_hook(p, "post_plan") is True
        assert has_hook(p, "estimate_cost") is True

    def test_has_hook_detects_inherited_noop(self):
        p = self._make_hooked_provider()
        assert has_hook(p, "on_error") is False  # not overridden

    def test_invoke_pre_plan(self):
        p = self._make_hooked_provider()
        result = invoke_hook(p, "pre_plan", {"id": "c1"})
        assert result["hooked"] is True
        assert "pre_plan" in p.call_log

    def test_invoke_post_plan_appends(self):
        p = self._make_hooked_provider()
        result = invoke_hook(p, "post_plan", [{"op": "a"}])
        assert len(result) == 2
        assert result[1]["op"] == "audited"

    def test_invoke_estimate_cost(self):
        p = self._make_hooked_provider()
        result = invoke_hook(p, "estimate_cost", [{"op": "a"}])
        assert isinstance(result, CostEstimate)
        assert result.monthly == 9.99

    def test_cli_wrappers_with_hooked_provider(self):
        from fluid_build.cli.hooks import (
            run_estimate_cost,
            run_post_apply,
            run_post_plan,
            run_pre_apply,
            run_pre_plan,
            run_validate_sovereignty,
        )

        p = self._make_hooked_provider()
        logger = logging.getLogger("test")

        c = run_pre_plan(p, {"id": "c1"}, logger)
        assert c["hooked"] is True

        actions = run_post_plan(p, [{"op": "a"}], logger)
        assert len(actions) == 2

        actions2 = run_pre_apply(p, actions, logger)
        assert actions2 is actions

        run_post_apply(p, {"applied": 1}, logger)

        est = run_estimate_cost(p, [{"op": "a"}], logger)
        assert est is not None and est.monthly == 9.99

        violations = run_validate_sovereignty(p, {}, logger)
        assert violations == []

        assert p.call_log == [
            "pre_plan",
            "post_plan",
            "pre_apply",
            "post_apply",
            "estimate_cost",
            "validate_sovereignty",
        ]
