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
FLUID Forge - Extensible Project Bootstrap System

The FLUID Forge system provides a modular, extensible framework for creating
FLUID data products with customizable templates, providers, and extensions.

Architecture Overview:
├── core/           # Core engine and interfaces
├── templates/      # Project templates (analytics, ML, ETL, etc.)
├── providers/      # Infrastructure providers (GCP, AWS, local, etc.)
├── generators/     # Code and config generators
├── extensions/     # User-defined extensions and customizations
└── plugins/        # Third-party plugins and integrations

Key Components:
- ForgeEngine: Main orchestration engine
- TemplateRegistry: Template discovery and management
- ProviderRegistry: Provider plugin system
- ExtensionRegistry: Custom extension loader
- GeneratorFramework: File and config generation system

Extension Points:
- Custom templates via template registry
- Custom providers via provider plugins
- Custom generators via generator framework
- Custom UI components via extension system
- Custom validation via validation plugins
- Custom post-processing via lifecycle hooks

Usage:
    from fluid_build.forge import ForgeEngine

    # Create and configure forge engine
    forge = ForgeEngine()

    # Register custom components
    forge.register_template('my-template', MyTemplate)
    forge.register_provider('my-provider', MyProvider)

    # Run interactive forge process
    result = forge.run()

For extension development, see:
- docs/extending-forge.md
- examples/custom-templates/
- examples/custom-providers/
"""

from .core.engine import ForgeEngine
from .core.interfaces import (
    Extension,
    Generator,
    InfrastructureProvider,
    ProjectTemplate,
    ValidationPlugin,
)
from .core.registry import ExtensionRegistry, ProviderRegistry, TemplateRegistry

# Version info
__version__ = "2.0.0"
__author__ = "FLUID Build Team"

# Main exports
__all__ = [
    "ForgeEngine",
    "TemplateRegistry",
    "ProviderRegistry",
    "ExtensionRegistry",
    "ProjectTemplate",
    "InfrastructureProvider",
    "Extension",
    "Generator",
    "ValidationPlugin",
]

# Initialize default registries
_template_registry = TemplateRegistry()
_provider_registry = ProviderRegistry()
_extension_registry = ExtensionRegistry()


# Convenience functions for global registration
def register_template(name: str, template_class, **kwargs):
    """Register a template globally"""
    return _template_registry.register(name, template_class, **kwargs)


def register_provider(name: str, provider_class, **kwargs):
    """Register a provider globally"""
    return _provider_registry.register(name, provider_class, **kwargs)


def register_extension(name: str, extension_class, **kwargs):
    """Register an extension globally"""
    return _extension_registry.register(name, extension_class, **kwargs)


# Auto-discovery of built-in components
def _discover_builtin_components():
    """Auto-discover and register built-in templates, providers, and extensions"""
    import importlib
    import pkgutil
    from pathlib import Path

    # Discover templates
    try:
        templates_pkg = importlib.import_module("fluid_build.forge.templates")
        for importer, modname, ispkg in pkgutil.iter_modules(templates_pkg.__path__):
            if not modname.startswith("_"):
                try:
                    module = importlib.import_module(f"fluid_build.forge.templates.{modname}")
                    if hasattr(module, "register_templates"):
                        module.register_templates(_template_registry)
                except ImportError:
                    pass
    except ImportError:
        pass

    # Discover providers
    try:
        providers_pkg = importlib.import_module("fluid_build.forge.providers")
        for importer, modname, ispkg in pkgutil.iter_modules(providers_pkg.__path__):
            if not modname.startswith("_"):
                try:
                    module = importlib.import_module(f"fluid_build.forge.providers.{modname}")
                    if hasattr(module, "register_providers"):
                        module.register_providers(_provider_registry)
                except ImportError:
                    pass
    except ImportError:
        pass

    # Discover extensions
    try:
        extensions_pkg = importlib.import_module("fluid_build.forge.extensions")
        for importer, modname, ispkg in pkgutil.iter_modules(extensions_pkg.__path__):
            if not modname.startswith("_"):
                try:
                    module = importlib.import_module(f"fluid_build.forge.extensions.{modname}")
                    if hasattr(module, "register_extensions"):
                        module.register_extensions(_extension_registry)
                except ImportError:
                    pass
    except ImportError:
        pass


# Perform auto-discovery on import
# _discover_builtin_components()

# Use simplified registration instead
from .registration import register_builtin_components
