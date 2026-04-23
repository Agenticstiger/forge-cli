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

"""Tests for the pluggable transformation engine framework."""

import pytest

from fluid_build.engines.base import (
    GenerationResult,
    Severity,
    TransformationEngine,
    TransformationIntent,
    ValidationIssue,
)
from fluid_build.engines.registry import (
    _reset_registry,
    get_engine,
    has_engine,
    list_engines,
    register_engine,
)


class TestValidationIssue:
    def test_str_with_field(self):
        issue = ValidationIssue(
            message="bad value", severity=Severity.ERROR, field="builds[0].engine"
        )
        assert str(issue) == "[builds[0].engine] error: bad value"

    def test_str_without_field(self):
        issue = ValidationIssue(message="something wrong")
        assert str(issue) == "error: something wrong"

    def test_warning(self):
        issue = ValidationIssue(message="heads up", severity=Severity.WARNING)
        assert "warning" in str(issue)


class TestTransformationIntent:
    def test_defaults(self):
        intent = TransformationIntent()
        assert intent.source_schemas == {}
        assert intent.stages == []
        assert intent.canonical_model is None

    def test_with_stages(self):
        intent = TransformationIntent(
            stages=[{"name": "stg_orders", "sql": "SELECT * FROM raw.orders", "layer": "staging"}],
            source_schemas={"orders": {"order_id": "integer", "amount": "number"}},
        )
        assert len(intent.stages) == 1
        assert intent.stages[0]["name"] == "stg_orders"


class TestRegistry:
    def setup_method(self):
        # Save current state and reset for isolated tests
        from fluid_build.engines.registry import _ENGINES

        self._saved = dict(_ENGINES)
        _reset_registry()

    def teardown_method(self):
        # Restore previous registrations
        from fluid_build.engines.registry import _ENGINES

        _reset_registry()
        _ENGINES.update(self._saved)

    def test_register_and_get(self):
        @register_engine
        class FakeEngine(TransformationEngine):
            name = "fake"
            supported_patterns = ("embedded-logic",)

            def generate(self, contract, build, **kw) -> GenerationResult:
                return {}

            def validate(self, contract, build):
                return []

        assert has_engine("fake")
        engine = get_engine("fake")
        assert engine is not None
        assert engine.name == "fake"

    def test_list_engines(self):
        @register_engine
        class A(TransformationEngine):
            name = "aaa"
            supported_patterns = ()

            def generate(self, contract, build, **kw):
                return {}

            def validate(self, contract, build):
                return []

        @register_engine
        class B(TransformationEngine):
            name = "bbb"
            supported_patterns = ()

            def generate(self, contract, build, **kw):
                return {}

            def validate(self, contract, build):
                return []

        assert list_engines() == ["aaa", "bbb"]

    def test_get_unknown_engine(self):
        assert get_engine("nonexistent") is None
        assert not has_engine("nonexistent")

    def test_register_no_name_raises(self):
        with pytest.raises(ValueError, match="non-empty 'name'"):

            @register_engine
            class NoName(TransformationEngine):
                name = ""
                supported_patterns = ()

                def generate(self, contract, build, **kw):
                    return {}

                def validate(self, contract, build):
                    return []


class TestBuiltInEngines:
    """Test that built-in engines are registered on import."""

    def test_dbt_registered(self):
        from fluid_build.engines import has_engine

        assert has_engine("dbt")

    def test_sql_registered(self):
        from fluid_build.engines import has_engine

        assert has_engine("sql")

    def test_dbt_engine_name(self):
        from fluid_build.engines import get_engine

        engine = get_engine("dbt")
        assert engine.name == "dbt"
        assert "hybrid-reference" in engine.supported_patterns

    def test_sql_engine_name(self):
        from fluid_build.engines import get_engine

        engine = get_engine("sql")
        assert engine.name == "sql"
        assert "embedded-logic" in engine.supported_patterns
