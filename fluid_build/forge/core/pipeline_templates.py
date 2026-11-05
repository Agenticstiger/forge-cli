# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Back-compat shim — pipeline templates split into per-CI-system modules.

The 3700-LOC monolith that used to live here was split into the
``pipeline_systems`` sibling package — one module per CI system
(GitHub Actions / GitLab / Jenkins / Azure DevOps / Bitbucket /
CircleCI / Tekton) plus a shared ``_base`` module with the enums,
config dataclass, and stage-rendering scaffold.

This module is kept as a thin re-export so any code still calling
``from fluid_build.forge.core.pipeline_templates import …`` continues
to work without churn (tests, downstream tools, generated docs).

New code should import directly from
``fluid_build.forge.core.pipeline_systems`` to make the per-system
locality explicit.
"""

from .pipeline_systems import (  # noqa: F401  (re-export)
    PINNED_ACTIONS,
    AzureDevOpsTemplate,
    BasePipelineTemplate,
    BitbucketTemplate,
    CircleCITemplate,
    GitHubActionsTemplate,
    GitLabCITemplate,
    JenkinsTemplate,
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
    StageSpec,
    TektonTemplate,
    _pin_action,
    generate_pipeline_template,
)

__all__ = [
    "BasePipelineTemplate",
    "PINNED_ACTIONS",
    "PipelineComplexity",
    "PipelineConfig",
    "PipelineProvider",
    "PipelineTemplateGenerator",
    "StageSpec",
    "_pin_action",
    "AzureDevOpsTemplate",
    "BitbucketTemplate",
    "CircleCITemplate",
    "GitHubActionsTemplate",
    "GitLabCITemplate",
    "JenkinsTemplate",
    "TektonTemplate",
    "generate_pipeline_template",
]
