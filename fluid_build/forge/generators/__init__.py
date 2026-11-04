# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
Generator System for FLUID Forge

The generator system provides reusable code and configuration generators
that can be composed across different templates and providers.

Built-in generators:
- contract_generator: FLUID contract file generation
- readme_generator: Project documentation generation
- config_generator: Configuration file generation
- test_generator: Test framework setup
- ci_generator: CI/CD pipeline generation

Teams can create custom generators by implementing the Generator interface.
"""

from typing import Any, Dict, List, Optional

from ..core.interfaces import Generator
from ..core.registry import GeneratorRegistry
from .config_generator import ConfigGenerator

# Import built-in generators
from .contract_generator import ContractGenerator
from .readme_generator import ReadmeGenerator


def register_generators(registry: GeneratorRegistry) -> None:
    """Register all built-in generators with the registry"""

    generators = [
        ("contract", ContractGenerator),
        ("readme", ReadmeGenerator),
        ("config", ConfigGenerator),
    ]

    for name, generator_class in generators:
        registry.register(name, generator_class, source="builtin")


__all__ = ["ContractGenerator", "ReadmeGenerator", "ConfigGenerator", "register_generators"]
