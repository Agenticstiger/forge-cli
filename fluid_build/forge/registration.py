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
Fixed and simplified plugin registration system

This addresses the critical issue where template discovery was broken.
Instead of complex auto-discovery, we use explicit registration.
"""

import logging
import os

from fluid_build.cli.console import success

from .core.registry import (
    get_extension_registry,
    get_generator_registry,
    get_provider_registry,
    get_template_registry,
)

LOG = logging.getLogger("fluid.forge.registration")


def register_builtin_components():
    """
    Register all built-in components explicitly

    This replaces the broken auto-discovery system with a simple,
    reliable registration approach.
    """

    # Check if we should show registration messages (only in debug mode or when explicitly requested)
    show_registration = (
        LOG.isEnabledFor(logging.DEBUG)
        or os.getenv("FLUID_SHOW_REGISTRATION", "").lower() in ("1", "true", "yes")
        or os.getenv("FLUID_LOG_LEVEL", "").upper() == "DEBUG"
    )

    # Register templates
    template_registry = get_template_registry()
    try:
        from .templates.starter import StarterTemplate

        template_registry.register("starter", StarterTemplate, source="builtin")

        from .templates.analytics import AnalyticsTemplate

        template_registry.register("analytics", AnalyticsTemplate, source="builtin")

        from .templates.ml_pipeline import MLPipelineTemplate

        template_registry.register("ml_pipeline", MLPipelineTemplate, source="builtin")

        from .templates.etl_pipeline import ETLPipelineTemplate

        template_registry.register("etl_pipeline", ETLPipelineTemplate, source="builtin")

        from .templates.streaming import StreamingTemplate

        template_registry.register("streaming", StreamingTemplate, source="builtin")

        if show_registration:
            success(f"Registered {len(template_registry.list_available())} templates")
        else:
            LOG.debug(f"Registered {len(template_registry.list_available())} templates")

    except Exception as e:
        LOG.error(f"Failed to register templates: {e}")

    # Register providers
    provider_registry = get_provider_registry()
    provider_specs = (
        ("local", ".providers.local", "LocalProvider"),
        ("gcp", ".providers.gcp", "GCPProvider"),
        ("aws", ".providers.aws", "AWSProvider"),
        ("snowflake", ".providers.snowflake", "SnowflakeProvider"),
    )
    for provider_name, module_name, class_name in provider_specs:
        try:
            module = __import__(f"{__package__}{module_name}", fromlist=[class_name])
            provider_class = getattr(module, class_name)
            provider_registry.register(provider_name, provider_class, source="builtin")
        except Exception as e:
            LOG.warning("Skipping built-in provider '%s' during registration: %s", provider_name, e)

    try:
        if show_registration:
            success(f"Registered {len(provider_registry.list_available())} providers")
        else:
            LOG.debug(f"Registered {len(provider_registry.list_available())} providers")
    except Exception as e:
        LOG.warning("Unable to summarize registered providers: %s", e)

    # Register extensions
    extension_registry = get_extension_registry()
    try:
        from .extensions.project_history import ProjectHistoryExtension

        extension_registry.register("project_history", ProjectHistoryExtension, source="builtin")

        from .extensions.environment_validator import EnvironmentValidatorExtension

        extension_registry.register(
            "environment_validator", EnvironmentValidatorExtension, source="builtin"
        )

        from .extensions.ai_assistant import AIAssistantExtension

        extension_registry.register("ai_assistant", AIAssistantExtension, source="builtin")

        if show_registration:
            success(f"Registered {len(extension_registry.list_available())} extensions")
        else:
            LOG.debug(f"Registered {len(extension_registry.list_available())} extensions")

    except Exception as e:
        LOG.error(f"Failed to register extensions: {e}")

    # Register generators
    generator_registry = get_generator_registry()
    try:
        from .generators.contract_generator import ContractGenerator

        generator_registry.register("contract", ContractGenerator, source="builtin")

        from .generators.readme_generator import ReadmeGenerator

        generator_registry.register("readme", ReadmeGenerator, source="builtin")

        from .generators.config_generator import ConfigGenerator

        generator_registry.register("config", ConfigGenerator, source="builtin")

        if show_registration:
            success(f"Registered {len(generator_registry.list_available())} generators")
        else:
            LOG.debug(f"Registered {len(generator_registry.list_available())} generators")

    except Exception as e:
        LOG.error(f"Failed to register generators: {e}")


# Call registration immediately
register_builtin_components()
