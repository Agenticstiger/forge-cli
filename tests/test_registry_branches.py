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

"""Branch-coverage tests for fluid_build.forge.core.registry"""

import pytest

from fluid_build.forge.core.interfaces import InfrastructureProvider, ProjectTemplate
from fluid_build.forge.core.registry import (
    ComponentInfo,
    ComponentRegistry,
    ProviderRegistry,
    TemplateRegistry,
)

# ── ComponentInfo tests ─────────────────────────────────────────────


class TestComponentInfo:
    def test_create_with_defaults(self):
        ci = ComponentInfo(name="mycomp", component_class=object)
        assert ci.name == "mycomp"
        assert ci.instance is None
        assert ci.metadata == {}
        assert ci.dependencies == []
        assert ci.enabled is True
        assert ci.source == "user"

    def test_create_with_all_fields(self):
        ci = ComponentInfo(
            name="x",
            component_class=int,
            instance=42,
            metadata={"k": "v"},
            dependencies=["dep1"],
            enabled=False,
            source="builtin",
        )
        assert ci.instance == 42
        assert ci.metadata == {"k": "v"}
        assert ci.dependencies == ["dep1"]
        assert ci.enabled is False
        assert ci.source == "builtin"


# ── Concrete test subclass (simpler than using TemplateRegistry) ──


class _DummyBase:
    """Minimal class to use as component_type."""

    def get_metadata(self):
        return {"category": "test"}


class _DummyComponent(_DummyBase):
    pass


class _DummyBadInit(_DummyBase):
    def __init__(self):
        raise RuntimeError("cannot init")


class _DummyDictMeta(_DummyBase):
    def get_metadata(self):
        return {"category": "special", "version": "1.0"}


class _ConcreteRegistry(ComponentRegistry):
    """Non-abstract subclass for testing."""

    def __init__(self):
        super().__init__(_DummyBase)

    def discover_builtin_components(self):
        pass


@pytest.fixture
def registry():
    return _ConcreteRegistry()


# ── ComponentRegistry tests ─────────────────────────────────────────


class TestRegister:
    def test_register_valid_component(self, registry):
        registry.register("comp1", _DummyComponent)
        assert "comp1" in registry._components
        info = registry._components["comp1"]
        assert info.source == "user"
        assert info.instance is not None

    def test_register_with_metadata(self, registry):
        registry.register("comp1", _DummyComponent, metadata={"extra": "yes"})
        info = registry._components["comp1"]
        # Metadata should combine provided + get_metadata()
        assert "extra" in info.metadata
        assert "category" in info.metadata

    def test_register_with_dependencies(self, registry):
        registry.register("comp1", _DummyComponent, dependencies=["dep1", "dep2"])
        info = registry._components["comp1"]
        assert info.dependencies == ["dep1", "dep2"]

    def test_register_with_source(self, registry):
        registry.register("comp1", _DummyComponent, source="plugin")
        assert registry._components["comp1"].source == "plugin"

    def test_register_wrong_type_raises(self, registry):
        with pytest.raises(TypeError, match="must inherit from"):
            registry.register("bad", int)

    def test_register_bad_init_still_registers(self, registry):
        """Component that fails to instantiate is still registered (with warning)."""
        registry.register("bad", _DummyBadInit)
        assert "bad" in registry._components
        assert registry._components["bad"].instance is None

    def test_register_dict_metadata(self, registry):
        """get_metadata returning a dict should update metadata."""
        registry.register("dm", _DummyDictMeta)
        info = registry._components["dm"]
        assert info.metadata.get("category") == "special"


class TestUnregister:
    def test_unregister_existing(self, registry):
        registry.register("x", _DummyComponent)
        assert registry.unregister("x") is True
        assert "x" not in registry._components

    def test_unregister_nonexistent(self, registry):
        assert registry.unregister("nope") is False


class TestGet:
    def test_get_existing(self, registry):
        registry.register("x", _DummyComponent)
        inst = registry.get("x")
        assert isinstance(inst, _DummyComponent)

    def test_get_nonexistent(self, registry):
        assert registry.get("missing") is None

    def test_get_disabled(self, registry):
        registry.register("x", _DummyComponent)
        registry.disable("x")
        assert registry.get("x") is None

    def test_get_lazy_instantiation(self, registry):
        registry.register("x", _DummyComponent)
        # Clear instance to test lazy creation
        registry._components["x"].instance = None
        inst = registry.get("x")
        assert isinstance(inst, _DummyComponent)

    def test_get_lazy_instantiation_failure(self, registry):
        registry.register("bad", _DummyBadInit)
        # Instance should be None (init failed during register)
        assert registry._components["bad"].instance is None
        # get() tries again and fails
        result = registry.get("bad")
        assert result is None


class TestGetMetadata:
    def test_existing(self, registry):
        registry.register("x", _DummyComponent, metadata={"foo": "bar"})
        meta = registry.get_metadata("x")
        assert "foo" in meta

    def test_nonexistent(self, registry):
        assert registry.get_metadata("nope") is None


class TestListAvailable:
    def test_empty(self, registry):
        assert registry.list_available() == []

    def test_enabled_only(self, registry):
        registry.register("a", _DummyComponent)
        registry.register("b", _DummyComponent)
        registry.disable("b")
        result = registry.list_available(enabled_only=True)
        assert "a" in result
        assert "b" not in result

    def test_all(self, registry):
        registry.register("a", _DummyComponent)
        registry.register("b", _DummyComponent)
        registry.disable("b")
        result = registry.list_available(enabled_only=False)
        assert "a" in result
        assert "b" in result


class TestListByCategory:
    def test_matching_category(self, registry):
        registry.register("x", _DummyComponent)
        # get_metadata returns {"category": "test"}
        result = registry.list_by_category("test")
        assert "x" in result

    def test_non_matching(self, registry):
        registry.register("x", _DummyComponent)
        assert registry.list_by_category("other") == []

    def test_disabled_excluded(self, registry):
        registry.register("x", _DummyComponent)
        registry.disable("x")
        assert registry.list_by_category("test") == []


class TestEnableDisable:
    def test_enable_existing(self, registry):
        registry.register("x", _DummyComponent)
        registry.disable("x")
        assert registry.enable("x") is True
        assert registry._components["x"].enabled is True

    def test_enable_nonexistent(self, registry):
        assert registry.enable("nope") is False

    def test_disable_existing(self, registry):
        registry.register("x", _DummyComponent)
        assert registry.disable("x") is True
        assert registry._components["x"].enabled is False

    def test_disable_nonexistent(self, registry):
        assert registry.disable("nope") is False


class TestValidateDependencies:
    def test_no_deps(self, registry):
        registry.register("x", _DummyComponent)
        assert registry.validate_dependencies() == {}

    def test_satisfied_deps(self, registry):
        registry.register("dep", _DummyComponent)
        registry.register("x", _DummyComponent, dependencies=["dep"])
        assert registry.validate_dependencies() == {}

    def test_missing_deps(self, registry):
        registry.register("x", _DummyComponent, dependencies=["missing"])
        result = registry.validate_dependencies()
        assert "x" in result
        assert "missing" in result["x"]

    def test_multiple_missing(self, registry):
        registry.register("x", _DummyComponent, dependencies=["a", "b"])
        result = registry.validate_dependencies()
        assert len(result["x"]) == 2


class TestGetLoadOrder:
    def test_no_deps_all_returned(self, registry):
        registry.register("a", _DummyComponent)
        registry.register("b", _DummyComponent)
        order = registry.get_load_order()
        assert set(order) == {"a", "b"}

    def test_respects_dependency_order(self, registry):
        registry.register("base", _DummyComponent)
        registry.register("derived", _DummyComponent, dependencies=["base"])
        order = registry.get_load_order()
        assert order.index("base") < order.index("derived")

    def test_circular_dependency_still_resolves(self, registry):
        """Circular deps are force-resolved with a warning."""
        registry.register("a", _DummyComponent, dependencies=["b"])
        registry.register("b", _DummyComponent, dependencies=["a"])
        order = registry.get_load_order()
        assert set(order) == {"a", "b"}

    def test_missing_dep_treated_as_resolved(self, registry):
        """Dependencies not in registry are treated as satisfied."""
        registry.register("x", _DummyComponent, dependencies=["external"])
        order = registry.get_load_order()
        assert "x" in order


# ── TemplateRegistry tests ──────────────────────────────────────────


class TestTemplateRegistry:
    def test_init(self):
        tr = TemplateRegistry()
        assert tr.component_type == ProjectTemplate

    def test_get_by_complexity(self):
        tr = TemplateRegistry()
        # Empty registry
        assert tr.get_by_complexity("simple") == []

    def test_get_by_provider_support(self):
        tr = TemplateRegistry()
        assert tr.get_by_provider_support("gcp") == []

    def test_get_recommended_for_domain_empty(self):
        tr = TemplateRegistry()
        assert tr.get_recommended_for_domain("analytics") == []


# ── ProviderRegistry tests ──────────────────────────────────────────


class TestProviderRegistry:
    def test_init(self):
        pr = ProviderRegistry()
        assert pr.component_type == InfrastructureProvider

    def test_get_by_service_support(self):
        pr = ProviderRegistry()
        assert pr.get_by_service_support("bigquery") == []
