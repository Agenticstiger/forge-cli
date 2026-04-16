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
Core interfaces and base classes for FLUID Forge extensibility

This module defines the fundamental contracts that all forge components must follow.
These interfaces enable a plugin-based architecture where teams can easily extend
the forge system with custom templates, providers, generators, and extensions.

Key Design Principles:
1. Interface Segregation: Small, focused interfaces for specific responsibilities
2. Open/Closed Principle: Open for extension, closed for modification
3. Dependency Inversion: Depend on abstractions, not concretions
4. Plugin Architecture: Easy registration and discovery of new components
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

# Type aliases for clarity
ProjectConfig = Dict[str, Any]
TemplateData = Dict[str, Any]
ValidationResult = Tuple[bool, List[str]]  # (is_valid, errors)


class ComplexityLevel(Enum):
    """Template complexity levels"""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class ProjectLayer(Enum):
    """Data product layers in the data mesh"""

    BRONZE = "Bronze"  # Raw data ingestion
    SILVER = "Silver"  # Cleaned and transformed data
    GOLD = "Gold"  # Business-ready data marts


@dataclass
class TemplateMetadata:
    """Metadata about a project template"""

    name: str
    description: str
    complexity: ComplexityLevel
    provider_support: List[str]
    use_cases: List[str]
    technologies: List[str]
    estimated_time: str
    tags: List[str]
    category: Optional[str] = None
    version: str = "1.0.0"
    author: Optional[str] = None
    license: Optional[str] = None


@dataclass
class GenerationContext:
    """Context passed to generators during project creation"""

    project_config: ProjectConfig
    target_dir: Path
    template_metadata: TemplateMetadata
    provider_config: Dict[str, Any]
    user_selections: Dict[str, Any]
    forge_version: str
    creation_time: str


class ProjectTemplate(ABC):
    """
    Base class for all project templates

    Templates define the structure, configuration, and generation logic
    for specific types of data products (analytics, ML, ETL, etc.)

    Example implementation:
        class AnalyticsTemplate(ProjectTemplate):
            def get_metadata(self) -> TemplateMetadata:
                return TemplateMetadata(
                    name="Analytics Data Product",
                    description="Business intelligence and reporting",
                    complexity=ComplexityLevel.INTERMEDIATE,
                    provider_support=["gcp", "snowflake", "local"],
                    # ... other metadata
                )

            def generate_structure(self, context: GenerationContext) -> Dict[str, Any]:
                return {
                    "sql/": {"queries/": [], "transforms/": []},
                    "docs/": [],
                    "tests/": []
                }
    """

    @abstractmethod
    def get_metadata(self) -> TemplateMetadata:
        """Return template metadata"""
        pass

    @abstractmethod
    def generate_structure(self, context: GenerationContext) -> Dict[str, Any]:
        """Generate the project folder structure"""
        pass

    @abstractmethod
    def generate_contract(self, context: GenerationContext) -> Dict[str, Any]:
        """Generate the FLUID contract for this template"""
        pass

    def validate_configuration(self, config: ProjectConfig) -> ValidationResult:
        """Validate the project configuration for this template"""
        return True, []

    def get_recommended_providers(self) -> List[str]:
        """Get recommended providers for this template"""
        return self.get_metadata().provider_support

    def post_generation_hooks(self, context: GenerationContext) -> None:
        """Execute post-generation hooks (optional)"""
        pass

    def get_customization_prompts(self) -> List[Dict[str, Any]]:
        """Return additional prompts for template customization"""
        return []


class InfrastructureProvider(ABC):
    """
    Base class for infrastructure providers

    Providers handle platform-specific configuration, deployment settings,
    and environment setup for different cloud platforms and runtimes.

    Example implementation:
        class GCPProvider(InfrastructureProvider):
            def get_metadata(self) -> Dict[str, Any]:
                return {
                    "name": "Google Cloud Platform",
                    "description": "Deploy to GCP with BigQuery, Dataflow, etc.",
                    "supported_services": ["bigquery", "dataflow", "composer"]
                }

            def configure_interactive(self, context: GenerationContext) -> Dict[str, Any]:
                # Interactive configuration prompts
                return {"project_id": "my-gcp-project", "region": "us-central1"}
    """

    @abstractmethod
    def get_metadata(self) -> Dict[str, Any]:
        """Return provider metadata"""
        pass

    @abstractmethod
    def configure_interactive(self, context: GenerationContext) -> Dict[str, Any]:
        """Interactive configuration for this provider"""
        pass

    @abstractmethod
    def generate_config(self, context: GenerationContext) -> Dict[str, Any]:
        """Generate provider-specific configuration"""
        pass

    def validate_configuration(self, config: Dict[str, Any]) -> ValidationResult:
        """Validate provider configuration"""
        return True, []

    def get_required_tools(self) -> List[str]:
        """Return list of required CLI tools/SDKs"""
        return []

    def get_environment_variables(self) -> List[str]:
        """Return required environment variables"""
        return []

    def check_prerequisites(self) -> ValidationResult:
        """Check if provider prerequisites are met"""
        return True, []


class Generator(ABC):
    """
    Base class for code and configuration generators

    Generators create specific types of files (contracts, configs, docs, etc.)
    based on templates and context. They can be composed and reused across
    different project templates.
    """

    @abstractmethod
    def generate(self, context: GenerationContext) -> Dict[str, str]:
        """Generate files. Returns {file_path: content} mapping"""
        pass

    def get_dependencies(self) -> List[str]:
        """Return list of other generators this depends on"""
        return []

    def validate_context(self, context: GenerationContext) -> ValidationResult:
        """Validate that context has required data for generation"""
        return True, []


class ValidationPlugin(ABC):
    """
    Base class for validation plugins

    Validation plugins provide custom validation logic for project
    configurations, generated code, and deployment settings.
    """

    @abstractmethod
    def validate(self, context: GenerationContext) -> ValidationResult:
        """Perform validation and return result"""
        pass

    def get_validation_scope(self) -> List[str]:
        """Return list of validation scopes (config, generated_files, etc.)"""
        return ["config"]


class Extension(ABC):
    """
    Base class for forge extensions

    Extensions can modify the forge workflow, add custom UI components,
    integrate with external tools, or provide additional functionality.
    """

    @abstractmethod
    def get_metadata(self) -> Dict[str, Any]:
        """Return extension metadata"""
        pass

    def on_forge_start(self, context: GenerationContext) -> None:
        """Called when forge process starts"""
        pass

    def on_template_selected(self, template: ProjectTemplate, context: GenerationContext) -> None:
        """Called when template is selected"""
        pass

    def on_provider_configured(
        self, provider: InfrastructureProvider, context: GenerationContext
    ) -> None:
        """Called when provider is configured"""
        pass

    def on_generation_complete(self, context: GenerationContext) -> None:
        """Called when project generation is complete"""
        pass

    def modify_prompts(
        self, prompts: List[Dict[str, Any]], context: GenerationContext
    ) -> List[Dict[str, Any]]:
        """Modify or add to the interactive prompts"""
        return prompts


# Protocol for registry-compatible components
class Registrable(Protocol):
    """Protocol for components that can be registered in the forge system"""

    def get_metadata(self) -> Dict[str, Any]:
        """Return component metadata for registry"""
        ...


# Utility classes for common patterns
class BaseGenerator(Generator):
    """Base implementation with common generator functionality"""

    def __init__(self, template_dir: Optional[Path] = None):
        self.template_dir = template_dir

    def load_template(self, template_name: str) -> str:
        """Load a template file"""
        if not self.template_dir:
            raise ValueError("Template directory not configured")

        template_path = self.template_dir / template_name
        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

        return template_path.read_text()

    def render_template(self, template_content: str, context: Dict[str, Any]) -> str:
        """Render template with context (basic string replacement)"""
        result = template_content
        for key, value in context.items():
            placeholder = f"{{{key}}}"
            result = result.replace(placeholder, str(value))
        return result


class FileGenerator(BaseGenerator):
    """Generator for creating files from templates"""

    def __init__(self, file_templates: Dict[str, str], template_dir: Optional[Path] = None):
        super().__init__(template_dir)
        self.file_templates = file_templates  # {output_path: template_name}

    def generate(self, context: GenerationContext) -> Dict[str, str]:
        """Generate files from templates"""
        result = {}

        template_context = {
            "project_name": context.project_config.get("name", ""),
            "description": context.project_config.get("description", ""),
            "owner": context.project_config.get("owner", ""),
            "domain": context.project_config.get("domain", ""),
            **context.user_selections,
        }

        for output_path, template_name in self.file_templates.items():
            template_content = self.load_template(template_name)
            rendered_content = self.render_template(template_content, template_context)
            result[output_path] = rendered_content

        return result


# Factory for creating standard components
class ComponentFactory:
    """Factory for creating standard forge components"""

    @staticmethod
    def create_file_generator(
        templates: Dict[str, str], template_dir: Optional[Path] = None
    ) -> FileGenerator:
        """Create a file generator with template mappings"""
        return FileGenerator(templates, template_dir)

    @staticmethod
    def create_validation_plugin(validation_func) -> ValidationPlugin:
        """Create a validation plugin from a function"""

        class FunctionValidationPlugin(ValidationPlugin):
            def validate(self, context: GenerationContext) -> ValidationResult:
                return validation_func(context)

        return FunctionValidationPlugin()
