# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

from typing import Dict, Optional, Tuple

from ._base import (
    PINNED_ACTIONS,
    PINNED_IMAGES,
    BasePipelineTemplate,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
    _pin_action,
)
from ._thin import ImportDrift
from .azure_devops import AzureDevOpsTemplate
from .bitbucket import BitbucketTemplate
from .circle_ci import CircleCITemplate
from .github_actions import GitHubActionsTemplate
from .gitlab_ci import GitLabCITemplate
from .jenkins import JenkinsTemplate
from .tekton import TektonTemplate


def generate_pipeline_template(
    provider: str,
    cloud_auth: Tuple[str, ...] = (),
    add_ons: Tuple[str, ...] = (),
    import_drift: Optional[ImportDrift] = None,
) -> Dict[str, str]:
    """The pipeline files for ``provider`` (github_actions, gitlab_ci, ...).

    Convenience wrapper around :class:`PipelineTemplateGenerator`:
    ``cloud_auth`` names the platforms the jobs authenticate to without a
    static key (any of "aws", "gcp", "snowflake", "databricks", "openbao"),
    ``add_ons`` the jobs beside the pipeline (security, compliance, sbom,
    import-drift); ``import-drift`` needs ``import_drift`` (the importer, its
    source and the options of the import that wrote the contracts).
    """
    try:
        provider_enum = PipelineProvider(provider)
    except ValueError as e:
        raise ValueError(f"Invalid parameter: {e}")
    config = PipelineConfig(
        provider=provider_enum,
        cloud_auth=cloud_auth,
        add_ons=add_ons,
        import_drift=import_drift,
    )
    return PipelineTemplateGenerator().generate_pipeline(config)


__all__ = [
    "BasePipelineTemplate",
    "PINNED_ACTIONS",
    "PINNED_IMAGES",
    "PipelineConfig",
    "PipelineProvider",
    "PipelineTemplateGenerator",
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
