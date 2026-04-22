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

"""``fluid generate ci`` subcommand.

Generates CI/CD pipeline configurations for every supported CI system.
Backed by :class:`fluid_build.forge.core.pipeline_templates.PipelineTemplateGenerator`
— the same rich generator ``fluid forge`` auto-scaffolds from — so
users of ``fluid generate ci`` get the full deploy/airflow/publish
stage set, not the legacy stub templates.

Usage:
    fluid generate ci                                   # default: gitlab
    fluid generate ci --system github                   # GitHub Actions
    fluid generate ci --system jenkins                  # Jenkins (Jenkinsfile)
    fluid generate ci --system azure                    # Azure DevOps
    fluid generate ci --system bitbucket                # Bitbucket Pipelines
    fluid generate ci --system circleci                 # CircleCI
    fluid generate ci --system tekton                   # Tekton (2 files)
    fluid generate ci contract.fluid.yaml               # specify contract
    fluid generate ci --out .github/workflows/ci.yml    # custom output (single-file only)
    fluid generate ci --complexity advanced             # multi-environment + approvals
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from typing import Dict, Optional

from ._common import CLIError
from ._io import atomic_write
from ._logging import info

# Lazy-imported inside ``run`` so ``register_subcommand`` doesn't drag
# the full pipeline_templates module in for ``--help`` invocations.


# Public → internal enum mapping. The CLI surface uses short names; the
# PipelineProvider enum uses underscore_case for historical reasons.
_SYSTEM_ALIASES: Dict[str, str] = {
    # Canonical short names (what we recommend in docs).
    "github": "github_actions",
    "gitlab": "gitlab_ci",
    "jenkins": "jenkins",
    "azure": "azure_devops",
    "bitbucket": "bitbucket",
    "circleci": "circle_ci",
    "tekton": "tekton",
    # Long-form aliases so `--system github-actions` also works.
    "github-actions": "github_actions",
    "github_actions": "github_actions",
    "gitlab-ci": "gitlab_ci",
    "gitlab_ci": "gitlab_ci",
    "azure-devops": "azure_devops",
    "azure_devops": "azure_devops",
    "circle-ci": "circle_ci",
    "circle_ci": "circle_ci",
}

# Primary output path per system. Used only when ``--out`` is NOT
# supplied *and* the chosen system emits a single file. Multi-file
# systems (Tekton, GitHub Actions enterprise) ignore ``--out`` with a
# warning and keep their canonical paths so downstream tooling can find
# them by convention.
_PRIMARY_OUTPUT: Dict[str, str] = {
    "github_actions": ".github/workflows/fluid-standard.yml",
    "gitlab_ci": ".gitlab-ci.yml",
    "jenkins": "Jenkinsfile",
    "azure_devops": "azure-pipelines.yml",
    "bitbucket": "bitbucket-pipelines.yml",
    "circle_ci": ".circleci/config.yml",
    "tekton": "tekton/pipeline.yaml",
}


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "ci",
        help="Generate CI/CD pipeline (GitHub, GitLab, Jenkins, Azure, Bitbucket, CircleCI, Tekton)",
        description=(
            "Generate CI/CD pipeline configuration for your FLUID project.\n"
            "Covers validate / plan / apply / airflow-sync / publish stages."
        ),
        epilog="""Examples:
  fluid generate ci                          # GitLab CI (default)
  fluid generate ci --system github          # GitHub Actions
  fluid generate ci --system jenkins         # Jenkins (Jenkinsfile)
  fluid generate ci --system azure           # Azure DevOps Pipelines
  fluid generate ci --system bitbucket       # Bitbucket Pipelines
  fluid generate ci --system circleci        # CircleCI
  fluid generate ci --system tekton          # Tekton (writes tekton/*.yaml)
  fluid generate ci --complexity advanced    # multi-env with approvals
  fluid generate ci --out .github/workflows/ci.yml   # single-file systems only
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", nargs="?", default="contract.fluid.yaml", help="contract.fluid.yaml")
    p.add_argument(
        "--system",
        choices=sorted(_SYSTEM_ALIASES.keys()),
        default="gitlab",
        help="CI system (default: gitlab)",
    )
    p.add_argument(
        "--complexity",
        choices=["basic", "standard", "advanced", "enterprise"],
        default="standard",
        help=(
            "Pipeline complexity (default: standard). "
            "basic = validate+apply; standard = full workflow with testing; "
            "advanced = multi-env + approvals; enterprise = + governance/compliance."
        ),
    )
    p.add_argument(
        "--out",
        help=(
            "Output path override (single-file systems only). "
            "Multi-file systems (tekton, enterprise GitHub) keep canonical paths."
        ),
    )
    p.add_argument(
        "--no-generate-artifacts",
        action="store_true",
        help=(
            "Skip the `fluid generate transformation` and `fluid generate schedule` "
            "stages. Use for reference-only contracts (hybrid-reference dbt, "
            "external Airflow) where artifacts are owned outside fluid. "
            "Auto-detected for contracts whose builds[].pattern is hybrid-reference."
        ),
    )
    p.set_defaults(generate_sub="ci", func=_run_from_generate)


def _run_from_generate(args, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate ci``."""
    return run(args, logger)


def _contract_is_reference_only(contract_path: str) -> bool:
    """Detect whether a contract is reference-only (no artifact generation).

    Returns True when any build uses ``pattern: hybrid-reference`` — these
    contracts point at externally-owned dbt projects / Airflow DAGs, so
    asking fluid to generate transformations or schedules is a no-op that
    only surfaces spurious pipeline failures. Returns False on any read or
    parse error so we err on the side of keeping the generate stages
    (the legacy behavior).
    """
    try:
        import yaml

        with open(contract_path) as fh:
            contract = yaml.safe_load(fh) or {}
    except (FileNotFoundError, OSError, ImportError):
        return False
    except Exception:
        return False
    builds = contract.get("builds") or []
    if not isinstance(builds, list):
        return False
    reference_patterns = {"hybrid-reference", "reference", "external-reference"}
    for build in builds:
        if isinstance(build, dict) and build.get("pattern") in reference_patterns:
            return True
    return False


def _git_prefix() -> Optional[str]:
    """Return the current directory's path relative to the git repo root.

    Used by ``fluid generate ci`` so Jenkins-style pipelines (which run at
    the SCM checkout root, not the contract's folder) know to cd into the
    subfolder before executing fluid commands. Returns None when not inside
    a git repo or when git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    prefix = result.stdout.strip().rstrip("/")
    return prefix or None


def run(args, logger: logging.Logger) -> int:
    try:
        from fluid_build.forge.core.pipeline_templates import (
            PipelineComplexity,
            PipelineConfig,
            PipelineProvider,
            PipelineTemplateGenerator,
        )

        system = getattr(args, "system", "gitlab")
        canonical = _SYSTEM_ALIASES.get(system, system)
        try:
            provider = PipelineProvider(canonical)
        except ValueError:
            raise CLIError(
                1,
                "generate_ci_unknown_system",
                {"system": system, "choices": sorted(_SYSTEM_ALIASES.keys())},
            )

        complexity_value = getattr(args, "complexity", "standard") or "standard"
        try:
            complexity = PipelineComplexity(complexity_value)
        except ValueError:
            raise CLIError(
                1,
                "generate_ci_unknown_complexity",
                {"complexity": complexity_value},
            )

        contract_path = getattr(args, "contract", None) or "contract.fluid.yaml"
        no_generate_flag = bool(getattr(args, "no_generate_artifacts", False))
        generates_artifacts = not (no_generate_flag or _contract_is_reference_only(contract_path))

        config = PipelineConfig(
            provider=provider,
            complexity=complexity,
            workdir=_git_prefix(),
            generates_artifacts=generates_artifacts,
        )
        files = PipelineTemplateGenerator().generate_pipeline(config)
        if not files:
            raise CLIError(
                1,
                "generate_ci_empty",
                {"system": system, "hint": "Template returned no files."},
            )

        # ``--out`` rewrites the *primary* file's path. Non-primary
        # files (Tekton's ``tekton/tasks.yaml``, GitHub's
        # ``.env.ci.example``) keep their canonical paths so downstream
        # tooling finds them where expected. Log a hint when the system
        # emits multiple files so operators know the override only
        # touched the main one.
        out_override = getattr(args, "out", None)
        primary = _PRIMARY_OUTPUT.get(canonical)
        if out_override:
            if primary and primary in files:
                files = {
                    (out_override if rel == primary else rel): content
                    for rel, content in files.items()
                }
                if len(files) > 1:
                    info(
                        logger,
                        "generate_ci_out_secondary_files_retained",
                        system=system,
                        primary=out_override,
                    )
            elif len(files) == 1:
                only_key = next(iter(files))
                files = {out_override: files[only_key]}
            else:
                info(
                    logger,
                    "generate_ci_out_ignored_multifile",
                    system=system,
                    file_count=len(files),
                )
        for rel, content in sorted(files.items()):
            directory = os.path.dirname(rel)
            if directory:
                os.makedirs(directory, exist_ok=True)
            atomic_write(rel, content)
            info(
                logger,
                "generate_ci_ok",
                out=rel,
                system=system,
                primary=(rel == primary or rel == out_override),
            )
        return 0
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "generate_ci_failed", {"error": str(e)})
