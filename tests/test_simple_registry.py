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

"""Tests for fluid_build.forge.core.simple_registry — SimpleRegistry, convenience functions."""

from pathlib import Path

from fluid_build.forge.core.interfaces import (
    ComplexityLevel,
    GenerationContext,
    ProjectTemplate,
    TemplateMetadata,
)
from fluid_build.forge.core.simple_registry import (
    Component,
    SimpleRegistry,
)

# ── Minimal concrete implementations for testing ──


class _DummyTemplate(ProjectTemplate):
    def get_metadata(self):
        return TemplateMetadata(
            name="dummy",
            description="d",
            complexity=ComplexityLevel.BEGINNER,
            provider_support=["local"],
            use_cases=["test"],
            technologies=[],
            estimated_time="1m",
            tags=[],
        )

    def generate_structure(self, ctx):
        return {}

    def generate_contract(self, ctx):
        return {}

    def post_generation_hooks(self, ctx):
        pass


# ── Component ──


class TestComponent:
    def test_defaults(self):
        c = Component(name="x", component_class=_DummyTemplate)
        assert c.name == "x"
        assert c.instance is None
        assert c.enabled is True


# ── SimpleRegistry ──


class TestSimpleRegistry:
    def test_register_and_list(self):
        reg = SimpleRegistry(ProjectTemplate)
        reg.register("t1", _DummyTemplate)
        assert "t1" in reg.list_available()

    def test_register_wrong_type(self):
        reg = SimpleRegistry(ProjectTemplate)
        import pytest

        with pytest.raises(TypeError):
            reg.register("bad", str)  # str isn't a ProjectTemplate

    def test_get_creates_instance(self):
        reg = SimpleRegistry(ProjectTemplate)
        reg.register("t1", _DummyTemplate)
        inst = reg.get("t1")
        assert inst is not None
        assert isinstance(inst, _DummyTemplate)

    def test_get_caches_instance(self):
        reg = SimpleRegistry(ProjectTemplate)
        reg.register("t1", _DummyTemplate)
        a = reg.get("t1")
        b = reg.get("t1")
        assert a is b  # Same instance

    def test_get_missing(self):
        reg = SimpleRegistry(ProjectTemplate)
        assert reg.get("nope") is None

    def test_disable(self):
        reg = SimpleRegistry(ProjectTemplate)
        reg.register("t1", _DummyTemplate)
        assert reg.disable("t1") is True
        assert "t1" not in reg.list_available()
        assert reg.get("t1") is None

    def test_disable_missing(self):
        reg = SimpleRegistry(ProjectTemplate)
        assert reg.disable("nope") is False

    def test_enable(self):
        reg = SimpleRegistry(ProjectTemplate)
        reg.register("t1", _DummyTemplate)
        reg.disable("t1")
        assert reg.enable("t1") is True
        assert "t1" in reg.list_available()

    def test_enable_missing(self):
        reg = SimpleRegistry(ProjectTemplate)
        assert reg.enable("nope") is False


# ── Interfaces — dataclasses / enums ──


class TestComplexityLevel:
    def test_values(self):
        assert ComplexityLevel.BEGINNER.value == "beginner"
        assert ComplexityLevel.INTERMEDIATE.value == "intermediate"
        assert ComplexityLevel.ADVANCED.value == "advanced"


class TestTemplateMetadata:
    def test_defaults(self):
        m = TemplateMetadata(
            name="t",
            description="d",
            complexity=ComplexityLevel.BEGINNER,
            provider_support=["local"],
            use_cases=["test"],
            technologies=["python"],
            estimated_time="5m",
            tags=["quick"],
        )
        assert m.version == "1.0.0"
        assert m.author is None
        assert m.category is None


class TestGenerationContext:
    def test_fields(self):
        meta = TemplateMetadata(
            name="t",
            description="d",
            complexity=ComplexityLevel.BEGINNER,
            provider_support=[],
            use_cases=[],
            technologies=[],
            estimated_time="1m",
            tags=[],
        )
        ctx = GenerationContext(
            project_config={"name": "p"},
            target_dir=Path("/tmp/p"),
            template_metadata=meta,
            provider_config={},
            user_selections={},
            forge_version="2.0.0",
            creation_time="2025-01-01T00:00:00",
        )
        assert ctx.forge_version == "2.0.0"
        assert ctx.target_dir == Path("/tmp/p")


# ── Config constants ──


class TestConfigConstants:
    def test_config_values(self):
        from fluid_build.config import (
            DEFAULT_PROVIDER,
            DEFAULT_REGION,
            RUN_STATE_DIR,
            SUPPORTED_PROVIDERS,
        )

        assert RUN_STATE_DIR == "runtime/.state"
        assert DEFAULT_REGION == "europe-west3"
        assert DEFAULT_PROVIDER == "gcp"
        assert "gcp" in SUPPORTED_PROVIDERS
        assert "local" in SUPPORTED_PROVIDERS
        assert "aws" in SUPPORTED_PROVIDERS
