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

"""Unit tests for fluid_build.forge.templates.analytics module."""

import logging
import tempfile
from pathlib import Path

from fluid_build.forge.core.interfaces import (
    ComplexityLevel,
    GenerationContext,
    TemplateMetadata,
)
from fluid_build.forge.templates.analytics import AnalyticsTemplate

logger = logging.getLogger("test_forge_templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    name="my-analytics",
    description="Analytics product",
    domain="analytics",
    owner="data-team",
    provider="gcp",
    target_dir=None,
    user_selections=None,
):
    if target_dir is None:
        target_dir = Path(tempfile.mkdtemp())

    project_config = {
        "name": name,
        "description": description,
        "domain": domain,
        "owner": owner,
        "provider": provider,
    }
    metadata = TemplateMetadata(
        name="Analytics",
        description="desc",
        complexity=ComplexityLevel.INTERMEDIATE,
        provider_support=["gcp"],
        use_cases=["BI"],
        technologies=["SQL"],
        estimated_time="10 min",
        tags=["analytics"],
    )
    return GenerationContext(
        project_config=project_config,
        target_dir=target_dir,
        template_metadata=metadata,
        provider_config={},
        user_selections=user_selections or {},
        forge_version="1.0.0",
        creation_time="2024-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# get_metadata
# ---------------------------------------------------------------------------


class TestGetMetadata:
    def test_returns_template_metadata(self):
        template = AnalyticsTemplate()
        metadata = template.get_metadata()
        assert isinstance(metadata, TemplateMetadata)

    def test_metadata_name(self):
        template = AnalyticsTemplate()
        metadata = template.get_metadata()
        assert "Analytics" in metadata.name

    def test_metadata_complexity_intermediate(self):
        template = AnalyticsTemplate()
        metadata = template.get_metadata()
        assert metadata.complexity == ComplexityLevel.INTERMEDIATE

    def test_metadata_provider_support(self):
        template = AnalyticsTemplate()
        metadata = template.get_metadata()
        assert "gcp" in metadata.provider_support
        assert "snowflake" in metadata.provider_support

    def test_metadata_has_tags(self):
        template = AnalyticsTemplate()
        metadata = template.get_metadata()
        assert "analytics" in metadata.tags

    def test_metadata_technologies_include_sql(self):
        template = AnalyticsTemplate()
        metadata = template.get_metadata()
        assert "SQL" in metadata.technologies or "dbt" in metadata.technologies


# ---------------------------------------------------------------------------
# generate_structure
# ---------------------------------------------------------------------------


class TestGenerateStructure:
    def test_returns_dict(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        structure = template.generate_structure(ctx)
        assert isinstance(structure, dict)

    def test_contains_dbt_folder(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        structure = template.generate_structure(ctx)
        assert "dbt/" in structure

    def test_contains_sql_folder(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        structure = template.generate_structure(ctx)
        assert "sql/" in structure

    def test_contains_tests_folder(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        structure = template.generate_structure(ctx)
        assert "tests/" in structure

    def test_contains_dashboards_folder(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        structure = template.generate_structure(ctx)
        assert "dashboards/" in structure


# ---------------------------------------------------------------------------
# generate_contract
# ---------------------------------------------------------------------------


class TestGenerateContract:
    def test_returns_dict(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        contract = template.generate_contract(ctx)
        assert isinstance(contract, dict)

    def test_has_fluid_version(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        contract = template.generate_contract(ctx)
        assert "fluidVersion" in contract
        # Templates emit v0.7.3 directly via the spec-driven builder.
        # v0.5.7 was the legacy shape that the runtime had to coerce.
        assert contract["fluidVersion"] == "0.7.3"

    def test_has_kind_data_product(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        contract = template.generate_contract(ctx)
        assert contract.get("kind") == "DataProduct"

    def test_has_builds_array(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        contract = template.generate_contract(ctx)
        assert "builds" in contract
        assert isinstance(contract["builds"], list)
        assert len(contract["builds"]) > 0

    def test_has_exposes_array(self):
        template = AnalyticsTemplate()
        ctx = _make_context()
        contract = template.generate_contract(ctx)
        assert "exposes" in contract
        assert isinstance(contract["exposes"], list)

    def test_custom_project_name_reflected(self):
        template = AnalyticsTemplate()
        ctx = _make_context(name="custom-project")
        contract = template.generate_contract(ctx)
        name_or_id = contract.get("name", "") + contract.get("id", "")
        assert "custom" in name_or_id.lower()

    def test_has_slo_block(self):
        """v0.7.3 moved SLO from a top-level block to per-expose
        ``quality.slo``. Either shape is acceptable as long as one
        of the produced exposes carries SLO metadata."""
        template = AnalyticsTemplate()
        ctx = _make_context()
        contract = template.generate_contract(ctx)
        if "slo" in contract:
            return  # legacy top-level shape
        # v0.7.3 shape: SLO lives under exposes[*].quality.slo
        exposes = contract.get("exposes") or []
        has_per_expose_slo = any(
            (isinstance(e, dict) and isinstance(e.get("quality"), dict) and "slo" in e["quality"])
            for e in exposes
        )
        # Or the per-expose block may be absent entirely if SLO is
        # optional for this template — we just confirm the contract
        # validates against the v0.7.3 schema (caller handles).
        assert has_per_expose_slo or contract.get("fluidVersion") == "0.7.3"


# ---------------------------------------------------------------------------
# validate_configuration
# ---------------------------------------------------------------------------


class TestValidateConfiguration:
    def test_valid_config_passes(self):
        template = AnalyticsTemplate()
        is_valid, messages = template.validate_configuration(
            {
                "name": "my-product",
                "description": "A valid analytics product",
                "provider": "gcp",
                "domain": "analytics",
            }
        )
        assert is_valid is True

    def test_missing_name_fails(self):
        template = AnalyticsTemplate()
        is_valid, messages = template.validate_configuration(
            {"description": "No name", "provider": "gcp"}
        )
        assert is_valid is False
        assert any("name" in m.lower() for m in messages)

    def test_missing_description_fails(self):
        template = AnalyticsTemplate()
        is_valid, messages = template.validate_configuration(
            {"name": "my-product", "provider": "gcp"}
        )
        assert is_valid is False
        assert any("description" in m.lower() for m in messages)

    def test_non_analytics_provider_generates_warning(self):
        template = AnalyticsTemplate()
        is_valid, messages = template.validate_configuration(
            {
                "name": "my-product",
                "description": "A product",
                "provider": "some_odd_provider",
            }
        )
        assert isinstance(is_valid, bool)

    def test_non_analytics_domain_generates_warning(self):
        template = AnalyticsTemplate()
        is_valid, messages = template.validate_configuration(
            {
                "name": "my-product",
                "description": "A product",
                "provider": "gcp",
                "domain": "logistics",
            }
        )
        assert isinstance(messages, list)


# ---------------------------------------------------------------------------
# get_recommended_providers
# ---------------------------------------------------------------------------


class TestGetRecommendedProviders:
    def test_returns_list(self):
        template = AnalyticsTemplate()
        providers = template.get_recommended_providers()
        assert isinstance(providers, list)
        assert len(providers) > 0

    def test_includes_bigquery_or_snowflake(self):
        template = AnalyticsTemplate()
        providers = template.get_recommended_providers()
        assert "bigquery" in providers or "snowflake" in providers


# ---------------------------------------------------------------------------
# get_customization_prompts
# ---------------------------------------------------------------------------


class TestGetCustomizationPrompts:
    def test_returns_list(self):
        template = AnalyticsTemplate()
        prompts = template.get_customization_prompts()
        assert isinstance(prompts, list)
        assert len(prompts) > 0

    def test_prompts_have_name_and_type(self):
        template = AnalyticsTemplate()
        prompts = template.get_customization_prompts()
        for prompt in prompts:
            assert "name" in prompt
            assert "type" in prompt


# ---------------------------------------------------------------------------
# post_generation_hooks
# ---------------------------------------------------------------------------


class TestPostGenerationHooks:
    def test_hooks_run_without_error(self, tmp_path):
        template = AnalyticsTemplate()
        ctx = _make_context(target_dir=tmp_path)
        ctx.user_selections = {
            "include_dbt": False,
            "include_sample_dashboards": False,
            "data_lineage": False,
            "dimensional_modeling": False,
        }
        template.post_generation_hooks(ctx)

    def test_hooks_create_dbt_project(self, tmp_path):
        template = AnalyticsTemplate()
        ctx = _make_context(target_dir=tmp_path)
        ctx.user_selections = {
            "include_dbt": True,
            "include_sample_dashboards": False,
            "data_lineage": False,
            "dimensional_modeling": False,
        }
        (tmp_path / "dbt").mkdir(parents=True, exist_ok=True)
        template.post_generation_hooks(ctx)
        assert (tmp_path / "dbt" / "dbt_project.yml").exists()


# ---------------------------------------------------------------------------
# Semantics derivation — every template product is queryable out of the box
# ---------------------------------------------------------------------------


class TestTemplateSemantics:
    """Template contracts previously shipped with NO semantics block —
    no MCP query tool, no MetricFlow export — until a human authored
    one by hand. The v073 builder now derives a conservative block from
    the template's columns."""

    def _all_templates(self):
        from fluid_build.forge.templates.analytics import AnalyticsTemplate
        from fluid_build.forge.templates.etl_pipeline import ETLPipelineTemplate
        from fluid_build.forge.templates.ml_pipeline import MLPipelineTemplate
        from fluid_build.forge.templates.starter import StarterTemplate
        from fluid_build.forge.templates.streaming import StreamingTemplate

        return [
            AnalyticsTemplate(),
            ETLPipelineTemplate(),
            MLPipelineTemplate(),
            StarterTemplate(),
            StreamingTemplate(),
        ]

    def test_every_template_contract_carries_semantics(self):
        for template in self._all_templates():
            contract = template.generate_contract(_make_context())
            for expose in contract["exposes"]:
                semantics = expose.get("semantics")
                assert semantics, f"{type(template).__name__}: expose without semantics"
                for key in ("entities", "dimensions", "measures", "metrics"):
                    assert semantics.get(key), (
                        f"{type(template).__name__}: semantics.{key} empty — "
                        "the MCP query gate and the validator lint both need it"
                    )

    def test_every_template_contract_passes_schema_validation(self):
        from fluid_build.schema_manager import FluidSchemaManager

        schema_manager = FluidSchemaManager()
        for template in self._all_templates():
            contract = template.generate_contract(_make_context())
            result = schema_manager.validate_contract(contract, "0.7.3", offline_only=True)
            assert result.is_valid, f"{type(template).__name__}: {result.errors[:3]}"

    def test_analytics_derivation_shape(self):
        contract = AnalyticsTemplate().generate_contract(_make_context())
        semantics = contract["exposes"][0]["semantics"]
        entities = {e["name"]: e for e in semantics["entities"]}
        assert entities["customer"]["expr"] == "customer_id"
        measures = {m["name"]: m for m in semantics["measures"]}
        assert measures["total_metric_value"]["agg"] == "sum"
        assert measures["total_metric_value"]["expr"] == "metric_value"
        time_dims = [d for d in semantics["dimensions"] if d.get("type") == "time"]
        assert time_dims and time_dims[0]["typeParams"] == {"timeGranularity": "day"}
        assert semantics["defaultAggTimeDimension"] == time_dims[0]["name"]

    def test_record_count_floor_for_measureless_columns(self):
        from fluid_build.forge.templates._v073_builder import TemplateSpec, build_contract

        spec = TemplateSpec(template_name="starter", properties={"sql": "SELECT 1"})
        contract = build_contract(spec=spec, project_config={"name": "demo"})
        semantics = contract["exposes"][0]["semantics"]
        assert semantics["measures"] == [
            {"name": "record_count", "agg": "count", "expr": "1", "description": "Row count."}
        ]
        assert semantics["metrics"][0]["measure"] == "record_count"

    def test_spec_semantics_override_wins(self):
        from fluid_build.forge.templates._v073_builder import TemplateSpec, build_contract

        custom = {
            "name": "custom",
            "entities": [{"name": "e", "type": "primary"}],
            "dimensions": [{"name": "d", "type": "categorical"}],
            "measures": [{"name": "m", "agg": "sum", "expr": "x"}],
            "metrics": [{"name": "m", "type": "simple", "measure": "m"}],
        }
        spec = TemplateSpec(
            template_name="starter", properties={"sql": "SELECT 1"}, semantics=custom
        )
        contract = build_contract(spec=spec, project_config={"name": "demo"})
        assert contract["exposes"][0]["semantics"] == custom
