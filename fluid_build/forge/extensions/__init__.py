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
