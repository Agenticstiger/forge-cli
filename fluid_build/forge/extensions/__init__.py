# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
Extension System for FLUID Forge

The extension system provides hooks and customization points for teams to
extend the forge workflow with custom functionality.

Built-in extensions:
- project_history: Track and reuse previous project configurations
- environment_validator: Validate development environment setup
- ai_assistant: AI-powered recommendations and code generation
- git_integration: Enhanced Git repository setup and hooks

Teams can create custom extensions by implementing the Extension interface.
"""

from typing import Any, Dict, List, Optional

from ..core.interfaces import Extension
from ..core.registry import ExtensionRegistry
from .ai_assistant import AIAssistantExtension
from .environment_validator import EnvironmentValidatorExtension

# Import built-in extensions
from .project_history import ProjectHistoryExtension


def register_extensions(registry: ExtensionRegistry) -> None:
    """Register all built-in extensions with the registry"""

    extensions = [
        ("project_history", ProjectHistoryExtension),
        ("environment_validator", EnvironmentValidatorExtension),
        ("ai_assistant", AIAssistantExtension),
    ]

    for name, extension_class in extensions:
        registry.register(name, extension_class, source="builtin")


__all__ = [
    "ProjectHistoryExtension",
    "EnvironmentValidatorExtension",
    "AIAssistantExtension",
    "register_extensions",
]
