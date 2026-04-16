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
FLUID Forge Core Module

The core module provides the fundamental building blocks for the FLUID Forge system:
- Base interfaces and protocols for extensibility
- Component registry system for plugin management
- Main orchestration engine for workflow coordination
- Type definitions and utility classes

This module establishes the architectural foundation that enables teams to easily
extend the forge system with custom templates, providers, and generators.
"""

from .engine import ForgeEngine
from .interfaces import (
    ComplexityLevel,
    ComponentFactory,
    Extension,
    GenerationContext,
    Generator,
    InfrastructureProvider,
    ProjectLayer,
    ProjectTemplate,
    TemplateMetadata,
    ValidationPlugin,
    ValidationResult,
)
from .registry import (
    ExtensionRegistry,
    GeneratorRegistry,
    ProviderRegistry,
    TemplateRegistry,
    ValidationRegistry,
    extension_registry,
    generator_registry,
    get_registry_status,
    initialize_all_registries,
    provider_registry,
    template_registry,
    validation_registry,
)

__all__ = [
    # Core interfaces
    "ProjectTemplate",
    "InfrastructureProvider",
    "Extension",
    "Generator",
    "ValidationPlugin",
    # Data classes
    "GenerationContext",
    "TemplateMetadata",
    # Enums
    "ComplexityLevel",
    "ProjectLayer",
    # Type aliases
    "ValidationResult",
    # Registry classes
    "TemplateRegistry",
    "ProviderRegistry",
    "ExtensionRegistry",
    "GeneratorRegistry",
    "ValidationRegistry",
    # Global registry instances
    "template_registry",
    "provider_registry",
    "extension_registry",
    "generator_registry",
    "validation_registry",
    # Registry utilities
    "initialize_all_registries",
    "get_registry_status",
    # Main engine
    "ForgeEngine",
    # Utilities
    "ComponentFactory",
]
