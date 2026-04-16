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
Simplified FLUID Forge main module

This is a streamlined version of the forge system that focuses on
essential functionality while maintaining extensibility.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error

from .core.interfaces import GenerationContext
from .core.simple_registry import (
    get_provider,
    get_registry_status,
    get_template,
    initialize_registries,
    list_providers,
    list_templates,
)

logger = logging.getLogger(__name__)


class SimplifiedForge:
    """
    Simplified forge system

    Provides basic project generation functionality with much
    less complexity than the original system.
    """

    def __init__(self):
        self.context: Optional[GenerationContext] = None
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize the forge system"""
        try:
            initialize_registries()
            self._initialized = True
            logger.info("Forge system initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize forge system: {e}")
            return False

    def get_available_templates(self) -> List[str]:
        """Get list of available templates"""
        if not self._initialized:
            self.initialize()
        return list_templates()

    def get_available_providers(self) -> List[str]:
        """Get list of available providers"""
        if not self._initialized:
            self.initialize()
        return list_providers()

    def get_template_info(self, template_name: str) -> Optional[Dict[str, Any]]:
        """Get information about a template"""
        template = get_template(template_name)
        if not template:
            return None

        try:
            metadata = template.get_metadata()
            return {
                "name": template_name,
                "display_name": getattr(metadata, "display_name", template_name),
                "description": getattr(metadata, "description", "No description"),
                "complexity": getattr(metadata, "complexity", "unknown"),
                "use_cases": getattr(metadata, "use_cases", []),
                "provider_support": getattr(metadata, "provider_support", []),
            }
        except Exception as e:
            logger.warning(f"Could not get metadata for template {template_name}: {e}")
            return {"name": template_name, "error": str(e)}

    def get_provider_info(self, provider_name: str) -> Optional[Dict[str, Any]]:
        """Get information about a provider"""
        provider = get_provider(provider_name)
        if not provider:
            return None

        try:
            is_available, errors = provider.check_prerequisites()
            return {
                "name": provider_name,
                "available": is_available,
                "errors": errors,
                "required_tools": provider.get_required_tools(),
                "env_vars": provider.get_environment_variables(),
            }
        except Exception as e:
            logger.warning(f"Could not check provider {provider_name}: {e}")
            return {"name": provider_name, "available": False, "error": str(e)}

    def create_project(
        self,
        template_name: str,
        provider_name: str,
        project_name: str,
        output_path: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Create a new project"""

        if not self._initialized:
            if not self.initialize():
                return False

        try:
            # Get template and provider
            template = get_template(template_name)
            provider = get_provider(provider_name)

            if not template:
                logger.error(f"Template '{template_name}' not found")
                return False

            if not provider:
                logger.error(f"Provider '{provider_name}' not found")
                return False

            # Check provider prerequisites
            is_available, errors = provider.check_prerequisites()
            if not is_available:
                logger.error(f"Provider '{provider_name}' not available: {errors}")
                return False

            # Create generation context
            from datetime import datetime

            template_metadata = template.get_metadata()

            self.context = GenerationContext(
                project_config={"name": project_name, **(config or {})},
                target_dir=Path(output_path),
                template_metadata=template_metadata,
                provider_config={},
                user_selections={},
                forge_version="2.0.0-simplified",
                creation_time=datetime.now().isoformat(),
            )

            # Generate project
            logger.info(
                f"Creating project '{project_name}' using template '{template_name}' and provider '{provider_name}'"
            )

            success = template.generate_project(self.context)

            if success:
                logger.info(f"Project '{project_name}' created successfully at {output_path}")
            else:
                logger.error(f"Failed to create project '{project_name}'")

            return success

        except Exception as e:
            logger.error(f"Error creating project: {e}")
            return False

    def list_all_components(self) -> Dict[str, List[str]]:
        """List all available components"""
        if not self._initialized:
            self.initialize()

        return get_registry_status()

    def get_system_status(self) -> Dict[str, Any]:
        """Get overall system status"""
        if not self._initialized:
            self.initialize()

        status = get_registry_status()

        # Check provider availability
        provider_status = {}
        for provider_name in status.get("providers", []):
            provider_info = self.get_provider_info(provider_name)
            provider_status[provider_name] = (
                provider_info.get("available", False) if provider_info else False
            )

        return {
            "initialized": self._initialized,
            "components": status,
            "provider_availability": provider_status,
        }


# Global forge instance
_global_forge: Optional[SimplifiedForge] = None


def get_forge() -> SimplifiedForge:
    """Get global forge instance"""
    global _global_forge

    if _global_forge is None:
        _global_forge = SimplifiedForge()

    return _global_forge


# Convenience functions
def initialize_forge() -> bool:
    """Initialize the global forge instance"""
    return get_forge().initialize()


def create_project(
    template_name: str,
    provider_name: str,
    project_name: str,
    output_path: str,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Create a project using the global forge instance"""
    return get_forge().create_project(
        template_name, provider_name, project_name, output_path, config
    )


def list_templates() -> List[str]:
    """List available templates"""
    return get_forge().get_available_templates()


def list_providers() -> List[str]:
    """List available providers"""
    return get_forge().get_available_providers()


def get_template_info(name: str) -> Optional[Dict[str, Any]]:
    """Get template information"""
    return get_forge().get_template_info(name)


def get_provider_info(name: str) -> Optional[Dict[str, Any]]:
    """Get provider information"""
    return get_forge().get_provider_info(name)


def get_system_status() -> Dict[str, Any]:
    """Get system status"""
    return get_forge().get_system_status()


if __name__ == "__main__":
    # Demo the simplified forge system
    forge = SimplifiedForge()

    if forge.initialize():
        success("Forge initialized successfully")

        status = forge.get_system_status()
        cprint("\nSystem Status:")
        cprint(f"Templates: {len(status['components']['templates'])}")
        cprint(f"Providers: {len(status['components']['providers'])}")
        cprint(f"Extensions: {len(status['components']['extensions'])}")
        cprint(f"Generators: {len(status['components']['generators'])}")

        cprint(f"\nAvailable Templates: {status['components']['templates']}")
        cprint(f"Available Providers: {status['components']['providers']}")

        cprint("\nProvider Availability:")
        for provider, available in status["provider_availability"].items():
            cprint(f"  {provider}: {'✅' if available else '❌'}")

    else:
        console_error("Failed to initialize forge")
