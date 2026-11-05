# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
