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
Simplified component registration for FLUID Forge

This module handles explicit registration of all forge components
in a straightforward manner without complex auto-discovery.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def register_all_components(templates, providers, extensions, generators):
    """Register all built-in components with registries"""

    # Register templates
    try:
        from .templates.analytics import AnalyticsTemplate

        templates.register("analytics", AnalyticsTemplate)
    except ImportError as e:
        logger.debug(f"Could not import analytics template: {e}")

    try:
        from .templates.starter import StarterTemplate

        templates.register("starter", StarterTemplate)
    except ImportError as e:
        logger.debug(f"Could not import starter template: {e}")

    try:
        from .templates.ml_pipeline import MLPipelineTemplate

        templates.register("ml_pipeline", MLPipelineTemplate)
    except ImportError as e:
        logger.debug(f"Could not import ml_pipeline template: {e}")

    try:
        from .templates.etl_pipeline import ETLPipelineTemplate

        templates.register("etl_pipeline", ETLPipelineTemplate)
    except ImportError as e:
        logger.debug(f"Could not import etl_pipeline template: {e}")

    try:
        from .templates.streaming import StreamingTemplate

        templates.register("streaming", StreamingTemplate)
    except ImportError as e:
        logger.debug(f"Could not import streaming template: {e}")

    # Register providers
    try:
        from .providers.local import LocalProvider

        providers.register("local", LocalProvider)
    except ImportError as e:
        logger.debug(f"Could not import local provider: {e}")

    try:
        from .providers.gcp import GCPProvider

        providers.register("gcp", GCPProvider)
    except ImportError as e:
        logger.debug(f"Could not import gcp provider: {e}")

    try:
        from .providers.aws import AWSProvider

        providers.register("aws", AWSProvider)
    except ImportError as e:
        logger.debug(f"Could not import aws provider: {e}")

    try:
        from .providers.snowflake import SnowflakeProvider

        providers.register("snowflake", SnowflakeProvider)
    except ImportError as e:
        logger.debug(f"Could not import snowflake provider: {e}")

    # Register extensions
    try:
        from .extensions.ai_assistant import AIAssistantExtension

        extensions.register("ai_assistant", AIAssistantExtension)
    except ImportError as e:
        logger.debug(f"Could not import ai_assistant extension: {e}")

    try:
        from .extensions.environment_validator import EnvironmentValidatorExtension

        extensions.register("environment_validator", EnvironmentValidatorExtension)
    except ImportError as e:
        logger.debug(f"Could not import environment_validator extension: {e}")

    try:
        from .extensions.project_history import ProjectHistoryExtension

        extensions.register("project_history", ProjectHistoryExtension)
    except ImportError as e:
        logger.debug(f"Could not import project_history extension: {e}")

    # Register generators
    try:
        from .generators.contract_generator import ContractGenerator

        generators.register("contract", ContractGenerator)
    except ImportError as e:
        logger.debug(f"Could not import contract generator: {e}")

    try:
        from .generators.readme_generator import ReadmeGenerator

        generators.register("readme", ReadmeGenerator)
    except ImportError as e:
        logger.debug(f"Could not import readme generator: {e}")

    try:
        from .generators.config_generator import ConfigGenerator

        generators.register("config", ConfigGenerator)
    except ImportError as e:
        logger.debug(f"Could not import config generator: {e}")

    # Log registration summary
    template_count = len(templates.list_available())
    provider_count = len(providers.list_available())
    extension_count = len(extensions.list_available())
    generator_count = len(generators.list_available())

    logger.info(
        f"Registered {template_count} templates, {provider_count} providers, {extension_count} extensions, {generator_count} generators"
    )


def get_registration_summary():
    """Get a summary of what should be registered"""
    return {
        "templates": ["analytics", "starter", "ml_pipeline", "etl_pipeline", "streaming"],
        "providers": ["local", "gcp", "aws", "snowflake"],
        "extensions": ["ai_assistant", "environment_validator", "project_history"],
        "generators": ["contract", "readme", "config"],
    }
