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

"""Per-CI-system pipeline templates package.

This package is the modular replacement for the legacy
``pipeline_templates.py`` monolith (3700 LOC). Each CI system\'s
template lives in its own sibling module so adding / updating one
doesn\'t require reading the others.

The legacy import path
``fluid_build.forge.core.pipeline_templates`` still works — that
module is now a thin shim that re-exports everything from this
package (back-compat for tests + downstream tools).
"""

from typing import List, Optional

from ._base import (
    PINNED_ACTIONS,
    BasePipelineTemplate,
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
    StageSpec,
    _pin_action,
)
from .azure_devops import AzureDevOpsTemplate
from .bitbucket import BitbucketTemplate
from .circle_ci import CircleCITemplate
from .github_actions import GitHubActionsTemplate
from .gitlab_ci import GitLabCITemplate
from .jenkins import JenkinsTemplate
from .tekton import TektonTemplate


def generate_pipeline_template(
    provider: str,
    complexity: str = "standard",
    environments: List[str] = None,
    enable_marketplace: bool = False,
    oidc_provider: Optional[str] = None,
):
    """Generate pipeline template for the given provider.

    Convenience wrapper around :class:`PipelineTemplateGenerator` —
    constructs a :class:`PipelineConfig` from string args and returns
    the generated file map.

    Args:
        provider: CI/CD provider (github_actions, gitlab_ci, etc.)
        complexity: Pipeline complexity (basic, standard, advanced, enterprise)
        environments: List of deployment environments
        enable_marketplace: Enable marketplace publishing
        oidc_provider: OIDC auth provider for deploy jobs ("gcp", "aws", "azure", or None)

    Returns:
        Dictionary of filename -> content for pipeline files
    """
    try:
        provider_enum = PipelineProvider(provider)
        complexity_enum = PipelineComplexity(complexity)
    except ValueError as e:
        raise ValueError(f"Invalid parameter: {e}")

    config = PipelineConfig(
        provider=provider_enum,
        complexity=complexity_enum,
        environments=environments,
        enable_marketplace_publishing=enable_marketplace,
        oidc_provider=oidc_provider,
    )

    generator = PipelineTemplateGenerator()
    return generator.generate_pipeline(config)


__all__ = [
    "BasePipelineTemplate",
    "PINNED_ACTIONS",
    "PipelineComplexity",
    "PipelineConfig",
    "PipelineProvider",
    "PipelineTemplateGenerator",
    "StageSpec",
    "_pin_action",
    "GitHubActionsTemplate",
    "GitLabCITemplate",
    "AzureDevOpsTemplate",
    "JenkinsTemplate",
    "BitbucketTemplate",
    "CircleCITemplate",
    "TektonTemplate",
    "generate_pipeline_template",
]
