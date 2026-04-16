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
Template System for FLUID Forge

The template system provides reusable project templates for common data product
patterns. Each template includes:
- Project structure definition
- FLUID contract generation
- Provider-specific configurations
- Best practices and conventions

Built-in templates:
- starter: Simple MVP template for quick setup
- analytics: Business intelligence and reporting
- ml_pipeline: Machine learning workflows
- etl_pipeline: Extract, transform, load processes
- streaming: Real-time data processing

Teams can extend this system by creating custom templates that follow
the ProjectTemplate interface.
"""

from typing import Any, Dict, List, Optional

from ..core.interfaces import ComplexityLevel, GenerationContext, ProjectTemplate, TemplateMetadata
from ..core.registry import TemplateRegistry
from .analytics import AnalyticsTemplate
from .etl_pipeline import ETLPipelineTemplate
from .ml_pipeline import MLPipelineTemplate

# Import built-in templates
from .starter import StarterTemplate
from .streaming import StreamingTemplate


def register_templates(registry: TemplateRegistry) -> None:
    """Register all built-in templates with the registry"""

    # Register built-in templates
    templates = [
        ("starter", StarterTemplate),
        ("analytics", AnalyticsTemplate),
        ("ml_pipeline", MLPipelineTemplate),
        ("etl_pipeline", ETLPipelineTemplate),
        ("streaming", StreamingTemplate),
    ]

    for name, template_class in templates:
        registry.register(name, template_class, source="builtin")


__all__ = [
    "StarterTemplate",
    "AnalyticsTemplate",
    "MLPipelineTemplate",
    "ETLPipelineTemplate",
    "StreamingTemplate",
    "register_templates",
]
