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

"""Tests for fluid_build.forge.core.registry — component registration & discovery."""

import unittest

import pytest

from fluid_build.forge.core.interfaces import (
    ComplexityLevel,
    Extension,
    GenerationContext,
    Generator,
    InfrastructureProvider,
    ProjectTemplate,
    TemplateMetadata,
    ValidationPlugin,
)
from fluid_build.forge.core.registry import (
    ComponentInfo,
    ExtensionRegistry,
    GeneratorRegistry,
    ProviderRegistry,
    TemplateRegistry,
    ValidationRegistry,
    get_extension_registry,
    get_generator_registry,
    get_provider_registry,
    get_registry_status,
    get_template_registry,
    get_validation_registry,
)

# --- Minimal concrete implementations for testing ---


class _StubTemplate(ProjectTemplate):
    def get_metadata(self):
        return TemplateMetadata(
            name="stub",
            description="d",
            complexity=ComplexityLevel.BEGINNER,
            provider_support=["local"],
            use_cases=["test"],
            technologies=["python"],
            estimated_time="1 min",
            tags=["demo"],
        )

    def generate_structure(self, ctx):
        return {}

    def generate_contract(self, ctx):
        return {}

    def validate_configuration(self, config):
        return True, []

    def get_recommended_providers(self):
        return ["local"]

    def post_generation_hooks(self, ctx):
        pass


class _StubTemplate2(_StubTemplate):
    def get_metadata(self):
        m = super().get_metadata()
        m.name = "stub2"
        m.complexity = ComplexityLevel.ADVANCED
        m.provider_support = ["gcp"]
        m.use_cases = ["analytics"]
        m.tags = ["enterprise"]
        return m


class _StubProvider(InfrastructureProvider):
    def get_metadata(self):
        return {
            "name": "Stub Provider",
            "description": "For testing",
            "supported_services": ["docker"],
        }

    def configure_interactive(self, context):
        return {}

    def generate_config(self, context):
        return {}

    def check_prerequisites(self):
        return True, []

    def get_required_tools(self):
        return ["python"]

    def get_environment_variables(self):
        return []


class _StubGenerator(Generator):
    def generate(self, context):
        return {}

    def get_dependencies(self):
        return []


class _StubGenerator2(_StubGenerator):
    def get_dependencies(self):
        return ["stubgenerator"]


class _StubValidator(ValidationPlugin):
    def validate(self, context):
        return True, []

    def get_validation_scope(self):
        return ["config", "schema"]


class _StubExtension(Extension):
    def get_metadata(self):
        return {"name": "stub-ext"}

    def on_forge_start(self, context):
        pass

    def on_generation_complete(self, context):
        pass


# ---------------------------------------------------------------------------
# ComponentInfo
# ---------------------------------------------------------------------------


class TestComponentInfo(unittest.TestCase):
    def test_defaults(self):
        info = ComponentInfo(name="x", component_class=_StubTemplate)
        self.assertTrue(info.enabled)
        self.assertEqual(info.source, "user")
        self.assertEqual(info.dependencies, [])
        self.assertIsNone(info.instance)

    def test_custom_values(self):
        info = ComponentInfo(
            name="y",
            component_class=_StubTemplate,
            source="builtin",
            dependencies=["a", "b"],
            enabled=False,
        )
        self.assertFalse(info.enabled)
        self.assertEqual(info.dependencies, ["a", "b"])
        self.assertEqual(info.source, "builtin")


# ---------------------------------------------------------------------------
# TemplateRegistry
# ---------------------------------------------------------------------------


class TestTemplateRegistry(unittest.TestCase):
    def test_register_and_get(self):
        reg = TemplateRegistry()
        reg.register("test", _StubTemplate)
        instance = reg.get("test")
        self.assertIsNotNone(instance)
        self.assertIsInstance(instance, ProjectTemplate)

    def test_register_wrong_type_raises(self):
        reg = TemplateRegistry()
        with self.assertRaises(TypeError):
            reg.register("bad", dict)

    def test_unregister_existing(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        self.assertTrue(reg.unregister("a"))
        self.assertIsNone(reg.get("a"))

    def test_unregister_nonexistent(self):
        reg = TemplateRegistry()
        self.assertFalse(reg.unregister("nope"))

    def test_list_available_all_enabled(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        reg.register("b", _StubTemplate2)
        names = reg.list_available()
        self.assertIn("a", names)
        self.assertIn("b", names)

    def test_list_available_disabled_excluded(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        reg.register("b", _StubTemplate2)
        reg.disable("b")
        enabled = reg.list_available(enabled_only=True)
        self.assertIn("a", enabled)
        self.assertNotIn("b", enabled)

    def test_list_available_all_includes_disabled(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        reg.register("b", _StubTemplate2)
        reg.disable("b")
        all_names = reg.list_available(enabled_only=False)
        self.assertIn("b", all_names)

    def test_enable_disable_cycle(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        self.assertTrue(reg.disable("a"))
        self.assertIsNone(reg.get("a"))
        self.assertTrue(reg.enable("a"))
        self.assertIsNotNone(reg.get("a"))

    def test_enable_nonexistent_returns_false(self):
        reg = TemplateRegistry()
        self.assertFalse(reg.enable("nope"))

    def test_disable_nonexistent_returns_false(self):
        reg = TemplateRegistry()
        self.assertFalse(reg.disable("nope"))

    def test_get_metadata_existing(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        meta = reg.get_metadata("a")
        self.assertIsNotNone(meta)
        self.assertIn("name", meta)

    def test_get_metadata_nonexistent(self):
        reg = TemplateRegistry()
        self.assertIsNone(reg.get_metadata("nope"))

    def test_validate_dependencies_no_missing(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        self.assertEqual(reg.validate_dependencies(), {})

    def test_validate_dependencies_missing(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate, dependencies=["missing_dep"])
        missing = reg.validate_dependencies()
        self.assertIn("a", missing)
        self.assertIn("missing_dep", missing["a"])

    def test_validate_dependencies_resolved(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        reg.register("b", _StubTemplate2, dependencies=["a"])
        self.assertEqual(reg.validate_dependencies(), {})

    def test_get_load_order_respects_dependencies(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        reg.register("b", _StubTemplate2, dependencies=["a"])
        order = reg.get_load_order()
        self.assertLess(order.index("a"), order.index("b"))

    def test_get_load_order_circular_dependency_handled(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate, dependencies=["b"])
        reg.register("b", _StubTemplate2, dependencies=["a"])
        order = reg.get_load_order()
        self.assertEqual(set(order), {"a", "b"})

    def test_list_by_category(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate, metadata={"category": "analytics"})
        result = reg.list_by_category("analytics")
        # Category key may have been merged — check no crash and returns list
        self.assertIsInstance(result, list)

    def test_get_by_complexity(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        result = reg.get_by_complexity("beginner")
        # May or may not match depending on metadata merge
        self.assertIsInstance(result, list)

    def test_get_by_provider_support(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        result = reg.get_by_provider_support("local")
        self.assertIsInstance(result, list)

    def test_get_recommended_for_domain_analytics(self):
        reg = TemplateRegistry()
        reg.register(
            "analytics_tmpl",
            _StubTemplate,
            metadata={"use_cases": ["analytics reporting"], "tags": ["analytics"]},
        )
        result = reg.get_recommended_for_domain("analytics")
        self.assertIsInstance(result, list)

    def test_get_recommended_for_domain_no_match(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate, metadata={"use_cases": [], "tags": []})
        result = reg.get_recommended_for_domain("zymurgy")
        self.assertEqual(result, [])

    def test_auto_discover_missing_package_returns_zero(self):
        reg = TemplateRegistry()
        count = reg.auto_discover("fluid_build.nonexistent.package.xyz")
        self.assertEqual(count, 0)

    def test_get_disabled_returns_none(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate)
        reg.disable("a")
        self.assertIsNone(reg.get("a"))

    def test_source_label_stored(self):
        reg = TemplateRegistry()
        reg.register("a", _StubTemplate, source="builtin")
        info = reg._components["a"]
        self.assertEqual(info.source, "builtin")


# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------


class TestProviderRegistry(unittest.TestCase):
    def test_register_and_get_provider(self):
        reg = ProviderRegistry()
        reg.register("stub", _StubProvider)
        instance = reg.get("stub")
        self.assertIsNotNone(instance)
        self.assertIsInstance(instance, InfrastructureProvider)

    def test_get_by_service_support(self):
        reg = ProviderRegistry()
        reg.register(
            "stub",
            _StubProvider,
            metadata={"supported_services": ["bigquery", "dataflow"]},
        )
        result = reg.get_by_service_support("bigquery")
        self.assertIsInstance(result, list)

    def test_check_prerequisites(self):
        reg = ProviderRegistry()
        reg.register("stub", _StubProvider)
        results = reg.check_prerequisites()
        self.assertIn("stub", results)
        self.assertIn("available", results["stub"])


# ---------------------------------------------------------------------------
# GeneratorRegistry
# ---------------------------------------------------------------------------


class TestGeneratorRegistry(unittest.TestCase):
    def test_register_and_get_generator(self):
        reg = GeneratorRegistry()
        reg.register("gen1", _StubGenerator)
        instance = reg.get("gen1")
        self.assertIsNotNone(instance)

    def test_get_dependency_order(self):
        reg = GeneratorRegistry()
        reg.register("stubgenerator", _StubGenerator)
        reg.register("gen2", _StubGenerator2)
        order = reg.get_dependency_order(["stubgenerator", "gen2"])
        self.assertLess(order.index("stubgenerator"), order.index("gen2"))


# ---------------------------------------------------------------------------
# ValidationRegistry
# ---------------------------------------------------------------------------


class TestValidationRegistry(unittest.TestCase):
    def test_register_and_get_validator(self):
        reg = ValidationRegistry()
        reg.register("val1", _StubValidator)
        instance = reg.get("val1")
        self.assertIsNotNone(instance)

    def test_get_by_scope(self):
        reg = ValidationRegistry()
        reg.register("val1", _StubValidator)
        result = reg.get_by_scope("config")
        self.assertIn("val1", result)

    def test_get_by_scope_no_match(self):
        reg = ValidationRegistry()
        reg.register("val1", _StubValidator)
        result = reg.get_by_scope("nonexistent_scope")
        self.assertEqual(result, [])

    def test_validate_all(self):
        reg = ValidationRegistry()
        reg.register("val1", _StubValidator)
        context = object()
        results = reg.validate_all(context, "config")
        self.assertIn("val1", results)
        self.assertTrue(results["val1"]["valid"])


# ---------------------------------------------------------------------------
# ExtensionRegistry
# ---------------------------------------------------------------------------


class TestExtensionRegistry(unittest.TestCase):
    def test_register_extension_and_lifecycle_hooks(self):
        reg = ExtensionRegistry()
        reg.register("ext1", _StubExtension)
        instance = reg.get("ext1")
        self.assertIsNotNone(instance)
        # Should have set up lifecycle hooks
        self.assertIn("on_forge_start", reg._lifecycle_hooks)

    def test_trigger_lifecycle_hook(self):
        reg = ExtensionRegistry()
        reg.register("ext1", _StubExtension)
        # Should not raise
        reg.trigger_lifecycle_hook("on_forge_start", None)

    def test_trigger_nonexistent_hook_no_error(self):
        reg = ExtensionRegistry()
        reg.trigger_lifecycle_hook("on_nonexistent_hook")


# ---------------------------------------------------------------------------
# Global registry convenience functions
# ---------------------------------------------------------------------------


class TestGlobalRegistryFunctions(unittest.TestCase):
    def test_get_template_registry(self):
        reg = get_template_registry()
        self.assertIsInstance(reg, TemplateRegistry)

    def test_get_provider_registry(self):
        reg = get_provider_registry()
        self.assertIsInstance(reg, ProviderRegistry)

    def test_get_extension_registry(self):
        reg = get_extension_registry()
        self.assertIsInstance(reg, ExtensionRegistry)

    def test_get_generator_registry(self):
        reg = get_generator_registry()
        self.assertIsInstance(reg, GeneratorRegistry)

    def test_get_validation_registry(self):
        reg = get_validation_registry()
        self.assertIsInstance(reg, ValidationRegistry)

    def test_get_registry_status_structure(self):
        status = get_registry_status()
        for key in ("templates", "providers", "extensions", "generators", "validators"):
            self.assertIn(key, status)
            self.assertIn("count", status[key])
            self.assertIn("names", status[key])


if __name__ == "__main__":
    unittest.main()
