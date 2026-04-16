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
Simplified registry system for FLUID Forge components

This is a streamlined version of the component registry system that focuses
on essential functionality while maintaining type safety and extensibility.

Key simplifications:
- Reduced complexity from 474 to ~150 lines
- Removed complex auto-discovery in favor of explicit registration
- Simplified metadata handling
- Consolidated registry types into a single base class
- Removed advanced dependency resolution
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

from .interfaces import Extension, Generator, InfrastructureProvider, ProjectTemplate, Registrable

T = TypeVar("T", bound=Registrable)
logger = logging.getLogger(__name__)


@dataclass
class Component:
    """Simple component wrapper"""

    name: str
    component_class: Type
    instance: Optional[Any] = None
    enabled: bool = True


class SimpleRegistry(Generic[T]):
    """
    Simplified component registry

    Provides basic registration and retrieval functionality without
    complex auto-discovery or dependency resolution.
    """

    def __init__(self, component_type: Type[T]):
        self.component_type = component_type
        self._components: Dict[str, Component] = {}

    def register(self, name: str, component_class: Type[T]) -> None:
        """Register a component"""
        if not issubclass(component_class, self.component_type):
            raise TypeError(f"Component must inherit from {self.component_type.__name__}")

        self._components[name] = Component(name, component_class)
        logger.info(f"Registered {self.component_type.__name__.lower()}: {name}")

    def get(self, name: str) -> Optional[T]:
        """Get component instance"""
        component = self._components.get(name)
        if not component or not component.enabled:
            return None

        if component.instance is None:
            try:
                component.instance = component.component_class()
            except Exception as e:
                logger.error(f"Failed to create {name}: {e}")
                return None

        return component.instance

    def list_available(self) -> List[str]:
        """List available components"""
        return [name for name, comp in self._components.items() if comp.enabled]

    def disable(self, name: str) -> bool:
        """Disable a component"""
        if name in self._components:
            self._components[name].enabled = False
            return True
        return False

    def enable(self, name: str) -> bool:
        """Enable a component"""
        if name in self._components:
            self._components[name].enabled = True
            return True
        return False


class TemplateRegistry(SimpleRegistry[ProjectTemplate]):
    """Registry for project templates"""

    def get_by_use_case(self, use_case: str) -> List[str]:
        """Get templates by use case"""
        results = []
        for name in self.list_available():
            template = self.get(name)
            if template and hasattr(template, "get_metadata"):
                metadata = template.get_metadata()
                if hasattr(metadata, "use_cases") and use_case in metadata.use_cases:
                    results.append(name)
        return results


class ProviderRegistry(SimpleRegistry[InfrastructureProvider]):
    """Registry for infrastructure providers"""

    def check_available_providers(self) -> Dict[str, bool]:
        """Check which providers are available"""
        results = {}
        for name in self.list_available():
            provider = self.get(name)
            if provider:
                try:
                    is_valid, _ = provider.check_prerequisites()
                    results[name] = is_valid
                except Exception:
                    results[name] = False
            else:
                results[name] = False
        return results


class ExtensionRegistry(SimpleRegistry[Extension]):
    """Registry for forge extensions"""

    pass


class GeneratorRegistry(SimpleRegistry[Generator]):
    """Registry for generators"""

    pass


# Global registry instances
templates = TemplateRegistry(ProjectTemplate)
providers = ProviderRegistry(InfrastructureProvider)
extensions = ExtensionRegistry(Extension)
generators = GeneratorRegistry(Generator)


def initialize_registries():
    """Initialize registries with built-in components"""
    # Import and register built-in components
    try:
        from ..simple_registration import register_all_components

        register_all_components(templates, providers, extensions, generators)
        logger.info("Initialized all registries")
    except ImportError as e:
        logger.warning(f"Could not load registration module: {e}")
        # Try the existing registration system
        try:
            from ..registration import register_builtin_components

            register_builtin_components()
            logger.info("Initialized using existing registration system")
        except Exception as e2:
            logger.error(f"Failed to initialize registries: {e2}")


def get_registry_status() -> Dict[str, List[str]]:
    """Get simple status of all registries"""
    return {
        "templates": templates.list_available(),
        "providers": providers.list_available(),
        "extensions": extensions.list_available(),
        "generators": generators.list_available(),
    }


# Convenience access functions
def get_template(name: str) -> Optional[ProjectTemplate]:
    """Get a template by name"""
    return templates.get(name)


def get_provider(name: str) -> Optional[InfrastructureProvider]:
    """Get a provider by name"""
    return providers.get(name)


def get_extension(name: str) -> Optional[Extension]:
    """Get an extension by name"""
    return extensions.get(name)


def get_generator(name: str) -> Optional[Generator]:
    """Get a generator by name"""
    return generators.get(name)


def list_templates() -> List[str]:
    """List available templates"""
    return templates.list_available()


def list_providers() -> List[str]:
    """List available providers"""
    return providers.list_available()


def list_extensions() -> List[str]:
    """List available extensions"""
    return extensions.list_available()


def list_generators() -> List[str]:
    """List available generators"""
    return generators.list_available()
