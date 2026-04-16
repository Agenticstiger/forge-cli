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
        project_config = context.project_config
        project_name = project_config.get("name", "ml-pipeline")
        description = project_config.get("description", "ML pipeline data product")
        domain = project_config.get("domain", "ml")
        owner = project_config.get("owner", "ml-team")
        provider = project_config.get("provider", "gcp")

        return {
            "fluidVersion": project_config.get("fluid_version", "0.5.7"),
            "kind": "DataProduct",
            "id": f"{project_name.replace('-', '_')}_ml_pipeline",
            "name": f"{project_name} ML Pipeline",
            "description": description,
            "domain": domain,
            "metadata": {
                "layer": "Gold",
                "owner": {"team": owner, "email": f"{owner}@company.com"},
                "status": "Development",
                "tags": ["ml", "pipeline", "data-science", "prediction"],
                "created": context.creation_time,
                "template": "ml_pipeline",
                "forge_version": context.forge_version,
            },
            "consumes": [
                {
                    "id": "training_data",
                    "ref": "urn:fluid:training_data:v1",
                    "description": "Training dataset for ML model",
                }
            ],
            "builds": [  # Changed from 'build' to 'builds' array
                {
                    "transformation": {
                        "pattern": "hybrid-reference",
                        "engine": "python",
                        "properties": {
                            "script": "src/pipelines/training/train_pipeline.py",
                            "requirements": "requirements.txt",
                            "environment": {
                                "python_version": "3.9",
                                "packages": ["scikit-learn", "tensorflow", "mlflow"],
                            },
                        },
                    },
                    "execution": {
                        "trigger": {"type": "schedule", "cron": "0 6 * * 1"},
                        "runtime": {
                            "platform": provider,
                            "resources": {"cpu": "4", "memory": "16GB", "gpu": "1"},
                        },
                    },
                }
            ],  # Close builds array
            "exposes": [
                {
                    "exposeId": "model_predictions",  # Changed from 'id'
                    "kind": "table",  # Changed from 'type'
                    "description": "ML model prediction results",
                    "binding": {  # Changed from 'location'
                        "format": "table",
                        "dataset": "ml_outputs",  # Flattened from properties
                        "table": "predictions",
                    },
                    "schema": [
                        {
                            "name": "record_id",
                            "type": "string",
                            "description": "Unique record identifier",
                            "nullable": False,
                        },
                        {
                            "name": "prediction",
                            "type": "float",
                            "description": "Model prediction value",
                            "nullable": False,
                        },
                        {
                            "name": "confidence_score",
                            "type": "float",
                            "description": "Prediction confidence score",
                            "nullable": True,
                        },
                        {
                            "name": "model_version",
                            "type": "string",
                            "description": "Version of the model used",
                            "nullable": False,
                        },
                        {
                            "name": "predicted_at",
                            "type": "timestamp",
                            "description": "Prediction timestamp",
                            "nullable": False,
                        },
                    ],
                    "quality": [
                        {
                            "name": "prediction_completeness",
                            "rule": "record_id IS NOT NULL AND prediction IS NOT NULL",
                            "onFailure": {"action": "reject_row"},
                        }
                    ],
                }
            ],
            "slo": {"freshnessMinutes": 240, "availabilityPct": 99.0},
            "ml_config": {
                "framework": "scikit-learn",
                "model_type": "classification",
                "training_schedule": "weekly",
                "model_registry": True,
                "feature_store": True,
                "monitoring": True,
            },
        }

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        errors = []
        if not config.get("name"):
            errors.append("Project name is required")
        return len(errors) == 0, errors
