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

"""
ML Pipeline Template for FLUID Forge
Machine learning and data science workflows with feature engineering
"""

from typing import Any, Dict

from ..core.interfaces import (
    ComplexityLevel,
    GenerationContext,
    ProjectTemplate,
    TemplateMetadata,
    ValidationResult,
)


class MLPipelineTemplate(ProjectTemplate):
    """ML Pipeline template for machine learning data products"""

    def get_metadata(self) -> TemplateMetadata:
        return TemplateMetadata(
            name="ML Pipeline Data Product",
            description="Machine learning and data science workflows with feature engineering",
            complexity=ComplexityLevel.ADVANCED,
            provider_support=["local", "gcp", "aws", "vertex_ai", "sagemaker"],
            use_cases=[
                "Predictive modeling and forecasting",
                "Customer churn prediction",
                "Recommendation systems",
                "Anomaly detection and monitoring",
                "Computer vision applications",
                "Natural language processing",
            ],
            technologies=["Python", "scikit-learn", "TensorFlow", "PyTorch", "MLflow", "Kubeflow"],
            estimated_time="20-30 minutes",
            tags=["ml", "pipeline", "data-science", "prediction", "ai"],
            category="ml",
            version="1.0.0",
        )

    def generate_structure(self, context: GenerationContext) -> Dict[str, Any]:
        return {
            "notebooks/": {
                "exploration/": {},
                "training/": {},
                "evaluation/": {},
                "experiments/": {},
            },
            "src/": {
                "features/": {"engineering/": {}, "selection/": {}, "validation/": {}},
                "models/": {"training/": {}, "evaluation/": {}, "serving/": {}},
                "pipelines/": {"training/": {}, "inference/": {}, "batch/": {}},
                "utils/": {"data/": {}, "model/": {}, "evaluation/": {}},
            },
            "data/": {"raw/": {}, "processed/": {}, "features/": {}, "models/": {}},
            "config/": {"training/": {}, "serving/": {}, "monitoring/": {}},
            "tests/": {"unit/": {}, "integration/": {}, "model/": {}},
            "docs/": {"model_cards/": {}, "experiments/": {}, "api/": {}},
            "scripts/": {"training/": {}, "inference/": {}, "deployment/": {}},
        }

    def generate_contract(self, context: GenerationContext) -> Dict[str, Any]:
        """Generate a v0.7.3-canonical contract via the shared builder.

        Per-template specifics (product type, build pattern, engine,
        default columns) live in :class:`TemplateSpec` below;
        :func:`build_contract` populates the canonical fluidVersion,
        layer↔productType pair, owner, exposes[].binding, builds[],
        and consumes[] structure so every template stays in lockstep
        with schema changes.
        """
        from ._v073_builder import TemplateSpec, build_contract

        spec = TemplateSpec(
            template_name="ml_pipeline",
            product_type="ADP",
            pattern="embedded-logic",
            engine="python",
            properties={"model": "ml_pipeline.train"},
            expose_id="model_predictions",
            binding_format="parquet",
            description_suffix="ML training + inference pipeline",
        )
        return build_contract(spec=spec, project_config=context.project_config)

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        errors = []
        if not config.get("name"):
            errors.append("Project name is required")
        return len(errors) == 0, errors
