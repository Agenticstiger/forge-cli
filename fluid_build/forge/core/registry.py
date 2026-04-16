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
Registry system for FLUID Forge components

The registry system provides centralized management and discovery of templates,
providers, extensions, and other forge components. It supports both built-in
and user-defined components with automatic discovery and validation.

Key Features:
- Type-safe component registration and retrieval
- Automatic component discovery and loading
- Dependency resolution and validation
- Plugin lifecycle management
- Extension point management
- Configuration validation

Usage:
    # Register components
    template_registry.register('analytics', AnalyticsTemplate)
    provider_registry.register('gcp', GCPProvider)

    # Retrieve and use components
    template = template_registry.get('analytics')
    provider = provider_registry.get('gcp')

    # List available components
    available_templates = template_registry.list_available()
"""

import importlib
import logging
import pkgutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

from .interfaces import (
    Extension,
    Generator,
    InfrastructureProvider,
    ProjectTemplate,
    Registrable,
    ValidationPlugin,
)

T = TypeVar("T", bound=Registrable)

logger = logging.getLogger(__name__)


@dataclass
class ComponentInfo:
    """Information about a registered component"""

    name: str
    component_class: Type
    instance: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    enabled: bool = True
    source: str = "user"  # 'builtin', 'user', 'plugin'


class ComponentRegistry(Generic[T], ABC):
    """
    Base registry for forge components

    Provides common functionality for registering, discovering, and managing
    forge components with type safety and validation.
    """

    def __init__(self, component_type: Type[T]):
        self.component_type = component_type
        self._components: Dict[str, ComponentInfo] = {}
        self._initialized = False

    def register(
        self,
        name: str,
        component_class: Type[T],
        metadata: Optional[Dict[str, Any]] = None,
        dependencies: Optional[List[str]] = None,
        source: str = "user",
    ) -> None:
        """Register a component with the registry"""

        # Validate component type
        if not issubclass(component_class, self.component_type):
            raise TypeError(f"Component {name} must inherit from {self.component_type.__name__}")

        # Create component info
        component_info = ComponentInfo(
            name=name,
            component_class=component_class,
            metadata=metadata or {},
            dependencies=dependencies or [],
            source=source,
        )

        # Validate metadata if component is instantiable
        try:
            instance = component_class()
            if hasattr(instance, "get_metadata"):
                component_metadata = instance.get_metadata()
                # Convert dataclass to dict for storage
                if hasattr(component_metadata, "__dict__"):
                    component_info.metadata.update(component_metadata.__dict__)
                elif isinstance(component_metadata, dict):
                    component_info.metadata.update(component_metadata)
            component_info.instance = instance
        except Exception as e:
            logger.warning(f"Could not instantiate component {name}: {e}")

        self._components[name] = component_info
        logger.info(f"Registered {self.component_type.__name__.lower()} '{name}' from {source}")

    def unregister(self, name: str) -> bool:
        """Unregister a component"""
        if name in self._components:
            del self._components[name]
            logger.info(f"Unregistered {self.component_type.__name__.lower()} '{name}'")
            return True
        return False

    def get(self, name: str) -> Optional[T]:
        """Get a component instance by name"""
        component_info = self._components.get(name)
        if not component_info or not component_info.enabled:
            return None

        if component_info.instance is None:
            try:
                component_info.instance = component_info.component_class()
            except Exception as e:
                logger.error(f"Failed to instantiate {name}: {e}")
                return None

        return component_info.instance

    def get_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """Get component metadata by name"""
        component_info = self._components.get(name)
        return component_info.metadata if component_info else None

    def list_available(self, enabled_only: bool = True) -> List[str]:
        """List available component names"""
        if enabled_only:
            return [name for name, info in self._components.items() if info.enabled]
        return list(self._components.keys())

    def list_by_category(self, category: str) -> List[str]:
        """List components by category"""
        return [
            name
            for name, info in self._components.items()
            if info.metadata.get("category") == category and info.enabled
        ]

    def enable(self, name: str) -> bool:
        """Enable a component"""
        if name in self._components:
            self._components[name].enabled = True
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable a component"""
        if name in self._components:
            self._components[name].enabled = False
            return True
        return False

    def validate_dependencies(self) -> Dict[str, List[str]]:
        """Validate component dependencies and return missing ones"""
        missing_deps = {}

        for name, info in self._components.items():
            missing = []
            for dep in info.dependencies:
                if dep not in self._components:
                    missing.append(dep)

            if missing:
                missing_deps[name] = missing

        return missing_deps

    def get_load_order(self) -> List[str]:
        """Get components in dependency-resolved load order"""
        # Simple topological sort
        resolved = []
        remaining = set(self._components.keys())

        while remaining:
            # Find components with no unresolved dependencies
            ready = []
            for name in remaining:
                deps = self._components[name].dependencies
                if all(dep in resolved or dep not in self._components for dep in deps):
                    ready.append(name)

            if not ready:
                # Circular dependency or missing dependency
                logger.warning(f"Circular dependencies detected in: {remaining}")
                ready = list(remaining)  # Force inclusion to avoid infinite loop

            resolved.extend(ready)
            remaining -= set(ready)

        return resolved

    @abstractmethod
    def discover_builtin_components(self) -> None:
        """Discover and register built-in components"""
        pass

    def auto_discover(self, package_path: str) -> int:
        """Auto-discover components in a package"""
        discovered_count = 0

        try:
            package = importlib.import_module(package_path)
            for importer, modname, ispkg in pkgutil.iter_modules(package.__path__):
                if modname.startswith("_"):
                    continue

                try:
                    module = importlib.import_module(f"{package_path}.{modname}")

                    # Look for registration function
                    register_func_name = f"register_{self.component_type.__name__.lower()}s"
                    if hasattr(module, register_func_name):
                        register_func = getattr(module, register_func_name)
                        register_func(self)
                        discovered_count += 1

                    # Look for component classes
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (
                            isinstance(attr, type)
                            and issubclass(attr, self.component_type)
                            and attr != self.component_type
                        ):

                            component_name = attr_name.lower().replace(
                                self.component_type.__name__.lower(), ""
                            )
                            if not component_name:
                                component_name = modname

                            self.register(component_name, attr, source="builtin")
                            discovered_count += 1

                except ImportError as e:
                    logger.debug(f"Could not import {package_path}.{modname}: {e}")
                except Exception as e:
                    logger.warning(f"Error processing {package_path}.{modname}: {e}")

        except ImportError:
            logger.debug(f"Package {package_path} not found for auto-discovery")

        return discovered_count


class TemplateRegistry(ComponentRegistry[ProjectTemplate]):
    """Registry for project templates"""

    def __init__(self):
        super().__init__(ProjectTemplate)

    def discover_builtin_components(self) -> None:
        """Discover built-in templates"""
        count = self.auto_discover("fluid_build.forge.templates")
        logger.info(f"Discovered {count} built-in templates")

    def get_by_complexity(self, complexity: str) -> List[str]:
        """Get templates by complexity level"""
        return [
            name
            for name, info in self._components.items()
            if info.metadata.get("complexity") == complexity and info.enabled
        ]

    def get_by_provider_support(self, provider: str) -> List[str]:
        """Get templates that support a specific provider"""
        return [
            name
            for name, info in self._components.items()
            if provider in info.metadata.get("provider_support", []) and info.enabled
        ]

    def get_recommended_for_domain(self, domain: str) -> List[str]:
        """Get templates recommended for a domain"""
        recommendations = []
        domain_lower = domain.lower()

        for name, info in self._components.items():
            if not info.enabled:
                continue

            metadata = info.metadata
            use_cases = [uc.lower() for uc in metadata.get("use_cases", [])]
            tags = [tag.lower() for tag in metadata.get("tags", [])]

            # Score based on domain match
            score = 0
            if any(domain_lower in uc for uc in use_cases):
                score += 3
            if domain_lower in tags:
                score += 2
            if any(domain_lower in tag for tag in tags):
                score += 1

            if score > 0:
                recommendations.append((name, score))

        # Sort by score and return names
        recommendations.sort(key=lambda x: x[1], reverse=True)
        return [name for name, score in recommendations]


class ProviderRegistry(ComponentRegistry[InfrastructureProvider]):
    """Registry for infrastructure providers"""

    def __init__(self):
        super().__init__(InfrastructureProvider)

    def discover_builtin_components(self) -> None:
        """Discover built-in providers"""
        count = self.auto_discover("fluid_build.forge.providers")
        logger.info(f"Discovered {count} built-in providers")

    def get_by_service_support(self, service: str) -> List[str]:
        """Get providers that support a specific service"""
        return [
            name
            for name, info in self._components.items()
            if service in info.metadata.get("supported_services", []) and info.enabled
        ]

    def check_prerequisites(self) -> Dict[str, Dict[str, Any]]:
        """Check prerequisites for all providers"""
        results = {}

        for name, info in self._components.items():
            if not info.enabled:
                continue

            provider = self.get(name)
            if provider:
                try:
                    is_valid, errors = provider.check_prerequisites()
                    results[name] = {
                        "available": is_valid,
                        "errors": errors,
                        "required_tools": provider.get_required_tools(),
                        "env_vars": provider.get_environment_variables(),
                    }
                except Exception as e:
                    results[name] = {
                        "available": False,
                        "errors": [str(e)],
                        "required_tools": [],
                        "env_vars": [],
                    }

        return results


class ExtensionRegistry(ComponentRegistry[Extension]):
    """Registry for forge extensions"""

    def __init__(self):
        super().__init__(Extension)
        self._lifecycle_hooks: Dict[str, List[Extension]] = {}

    def discover_builtin_components(self) -> None:
        """Discover built-in extensions"""
        count = self.auto_discover("fluid_build.forge.extensions")
        logger.info(f"Discovered {count} built-in extensions")

    def register(
        self,
        name: str,
        component_class: Type[Extension],
        metadata: Optional[Dict[str, Any]] = None,
        dependencies: Optional[List[str]] = None,
        source: str = "user",
    ) -> None:
        """Register extension and set up lifecycle hooks"""
        super().register(name, component_class, metadata, dependencies, source)

        # Set up lifecycle hooks
        extension = self.get(name)
        if extension:
            for hook_name in [
                "on_forge_start",
                "on_template_selected",
                "on_provider_configured",
                "on_generation_complete",
            ]:
                if hasattr(extension, hook_name):
                    if hook_name not in self._lifecycle_hooks:
                        self._lifecycle_hooks[hook_name] = []
                    self._lifecycle_hooks[hook_name].append(extension)

    def trigger_lifecycle_hook(self, hook_name: str, *args, **kwargs) -> None:
        """Trigger a lifecycle hook for all registered extensions"""
        extensions = self._lifecycle_hooks.get(hook_name, [])
        for extension in extensions:
            try:
                hook_method = getattr(extension, hook_name)
                hook_method(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in extension {extension} hook {hook_name}: {e}")


class GeneratorRegistry(ComponentRegistry[Generator]):
    """Registry for generators"""

    def __init__(self):
        super().__init__(Generator)

    def discover_builtin_components(self) -> None:
        """Discover built-in generators"""
        count = self.auto_discover("fluid_build.forge.generators")
        logger.info(f"Discovered {count} built-in generators")

    def get_dependency_order(self, generator_names: List[str]) -> List[str]:
        """Get generators in dependency-resolved order"""
        resolved = []
        remaining = set(generator_names)

        while remaining:
            ready = []
            for name in remaining:
                generator = self.get(name)
                if generator:
                    deps = generator.get_dependencies()
                    if all(dep in resolved or dep not in generator_names for dep in deps):
                        ready.append(name)

            if not ready:
                # Break circular dependency
                ready = list(remaining)

            resolved.extend(ready)
            remaining -= set(ready)

        return resolved


class ValidationRegistry(ComponentRegistry[ValidationPlugin]):
    """Registry for validation plugins"""

    def __init__(self):
        super().__init__(ValidationPlugin)

    def discover_builtin_components(self) -> None:
        """Discover built-in validation plugins"""
        count = self.auto_discover("fluid_build.forge.plugins.validation")
        logger.info(f"Discovered {count} built-in validation plugins")

    def get_by_scope(self, scope: str) -> List[str]:
        """Get validators for a specific scope"""
        validators = []
        for name, info in self._components.items():
            if not info.enabled:
                continue

            validator = self.get(name)
            if validator and scope in validator.get_validation_scope():
                validators.append(name)

        return validators

    def validate_all(self, context, scope: str) -> Dict[str, Any]:
        """Run all validators for a scope"""
        results = {}
        validators = self.get_by_scope(scope)

        for validator_name in validators:
            validator = self.get(validator_name)
            if validator:
                try:
                    is_valid, errors = validator.validate(context)
                    results[validator_name] = {"valid": is_valid, "errors": errors}
                except Exception as e:
                    results[validator_name] = {
                        "valid": False,
                        "errors": [f"Validation failed: {e}"],
                    }

        return results


# Global registry instances
template_registry = TemplateRegistry()
provider_registry = ProviderRegistry()
extension_registry = ExtensionRegistry()
generator_registry = GeneratorRegistry()
validation_registry = ValidationRegistry()


def initialize_all_registries() -> None:
    """Initialize all registries with built-in components"""
    registries = [
        template_registry,
        provider_registry,
        extension_registry,
        generator_registry,
        validation_registry,
    ]

    for registry in registries:
        try:
            registry.discover_builtin_components()
        except Exception as e:
            logger.error(f"Failed to initialize {registry.__class__.__name__}: {e}")


def get_registry_status() -> Dict[str, Dict[str, Any]]:
    """Get status of all registries"""
    return {
        "templates": {
            "count": len(template_registry.list_available()),
            "names": template_registry.list_available(),
        },
        "providers": {
            "count": len(provider_registry.list_available()),
            "names": provider_registry.list_available(),
        },
        "extensions": {
            "count": len(extension_registry.list_available()),
            "names": extension_registry.list_available(),
        },
        "generators": {
            "count": len(generator_registry.list_available()),
            "names": generator_registry.list_available(),
        },
        "validators": {
            "count": len(validation_registry.list_available()),
            "names": validation_registry.list_available(),
        },
    }


# Convenience functions for accessing registries
def get_template_registry() -> TemplateRegistry:
    """Get the global template registry"""
    return template_registry


def get_provider_registry() -> ProviderRegistry:
    """Get the global provider registry"""
    return provider_registry


def get_extension_registry() -> ExtensionRegistry:
    """Get the global extension registry"""
    return extension_registry


def get_generator_registry() -> GeneratorRegistry:
    """Get the global generator registry"""
    return generator_registry


def get_validation_registry() -> ValidationRegistry:
    """Get the global validation registry"""
    return validation_registry
