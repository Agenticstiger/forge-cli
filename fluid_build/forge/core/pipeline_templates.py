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
Dynamic DataOps Pipeline Templates for FLUID

This module provides comprehensive pipeline configuration templates for different
CI/CD providers with full FLUID workflow integration. Teams can choose their
preferred provider and get a complete DataOps pipeline that includes:

1. Validation (fluid validate)
2. Planning (fluid plan)
3. Application (fluid apply)
4. Testing (fluid test)
5. Visualization (fluid viz)
6. Publishing (fluid publish --format opds)
7. Marketplace publishing (fluid marketplace publish)

The templates support:
- Multi-environment deployments (dev, staging, prod)
- Approval gates and manual triggers
- Artifact management and versioning
- Notification integrations
- Security scanning and compliance
- Performance monitoring
- Rollback capabilities
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint

# SHA-pinned GitHub Actions for supply chain security.
# Each entry maps action@tag to action@sha with a version comment.
# SHAs verified via GitHub API (git/ref/tags/<version>).
# Update these when upgrading action versions.
PINNED_ACTIONS = {
    "actions/checkout@v4": "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5",  # v4.3.1
    "actions/setup-python@v5": "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",  # v5.6.0
    "actions/upload-artifact@v4": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",  # v4.6.2
    "actions/download-artifact@v4": "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",  # v4.3.0
    "aquasecurity/trivy-action@v0": "aquasecurity/trivy-action@57a97c7e7821a5776cebc9bb87c984fa69cba8f1",  # v0.35.0
    "github/codeql-action/upload-sarif@v3": "github/codeql-action/upload-sarif@7fc1baf373eb073c686865bd453d412d506a05a2",  # v3.35.1
    "google-github-actions/auth@v2": "google-github-actions/auth@c200f3691d83b41bf9bbd8638997a462592937ed",  # v2.1.13
    "aws-actions/configure-aws-credentials@v4": "aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a",  # v4.3.1
    "azure/login@v2": "azure/login@eec3c95657c1536435858eda1f3ff5437fee8474",  # v2.3.0
    "anchore/sbom-action@v0": "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610",  # v0.24.0
    "actions/attest-build-provenance@v2": "actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be",  # v2.4.0
}


def _pin_action(action_ref: str) -> str:
    """Pin a GitHub Action reference to its SHA for supply chain security."""
    return PINNED_ACTIONS.get(action_ref, action_ref)


try:
    import yaml
except ImportError:
    # Fallback YAML implementation
    class _YamlFallback:
        def dump(self, data, **kwargs):
            return json.dumps(data, indent=kwargs.get("indent", 2))

        def dump_all(self, documents, **kwargs):
            results = []
            for doc in documents:
                results.append(self.dump(doc, **kwargs))
            return "\n---\n".join(results)

    yaml = _YamlFallback()


class PipelineProvider(Enum):
    """Supported CI/CD providers"""

    GITHUB_ACTIONS = "github_actions"
    GITLAB_CI = "gitlab_ci"
    AZURE_DEVOPS = "azure_devops"
    JENKINS = "jenkins"
    BITBUCKET = "bitbucket"
    CIRCLE_CI = "circle_ci"
    TEKTON = "tekton"


class PipelineComplexity(Enum):
    """Pipeline complexity levels"""

    BASIC = "basic"  # Simple validate -> apply workflow
    STANDARD = "standard"  # Full workflow with testing
    ADVANCED = "advanced"  # Multi-environment with approvals
    ENTERPRISE = "enterprise"  # Full governance and compliance


@dataclass
class PipelineConfig:
    """Configuration for pipeline generation"""

    provider: PipelineProvider
    complexity: PipelineComplexity
    environments: List[str] = None
    enable_approvals: bool = False
    enable_security_scan: bool = True
    enable_performance_monitoring: bool = True
    enable_marketplace_publishing: bool = False
    notification_channels: List[str] = None
    custom_steps: List[Dict[str, Any]] = None
    oidc_provider: Optional[str] = None  # "gcp", "aws", "azure", or None
    # Pipeline working directory, relative to the SCM checkout root. Set when
    # `fluid generate ci` is invoked from a subfolder of a git repo so the
    # generated pipeline can cd into that folder before running fluid commands.
    # None or "" => no cd wrapper (steps run at checkout root).
    workdir: Optional[str] = None
    # Whether the pipeline should run `fluid generate transformation/schedule`
    # in a dedicated "Generate Artifacts" stage. False for reference-only
    # contracts (e.g. hybrid-reference dbt) where the transformation and
    # schedule artifacts already exist externally and fluid shouldn't be
    # asked to generate new ones.
    generates_artifacts: bool = True
    # Install mode for the ``fluid`` CLI inside the Jenkins container.
    # Two values, picked explicitly at generation time — the generated
    # Jenkinsfile carries ONLY the install logic for the selected mode
    # (no runtime branching, no dead fallback code).
    #
    #   "pypi"        — PRODUCTION DEFAULT. Single ``pip install
    #                   data-product-forge`` from stable PyPI. Clean,
    #                   reproducible, works anywhere. Override the
    #                   package spec via FLUID_PACKAGE_SPEC env var
    #                   at Jenkins build time (to pin a version or
    #                   point at a private index).
    #   "dev-source"  — LAB / CONTRIBUTOR ONLY. Installs from a
    #                   bind-mounted forge-cli checkout at
    #                   /forge-cli-src. Fails loudly with an explicit
    #                   "add this to docker-compose" message if the
    #                   mount is missing — no silent fallback to PyPI.
    #
    # ``testpypi`` (pre-release track) and ``auto`` (runtime-decision
    # multi-fallback) were dropped from the design — teams that need
    # pre-release packages override FLUID_PACKAGE_SPEC on top of pypi
    # mode; teams that need multi-env Jenkinsfiles use two generated
    # files instead of one multi-branch file.
    install_mode: str = "pypi"

    def __post_init__(self):
        if self.environments is None:
            if self.complexity == PipelineComplexity.BASIC:
                self.environments = ["dev"]
            elif self.complexity == PipelineComplexity.STANDARD:
                self.environments = ["dev", "staging"]
            else:
                self.environments = ["dev", "staging", "prod"]

        if self.notification_channels is None:
            self.notification_channels = []

        if self.custom_steps is None:
            self.custom_steps = []


class PipelineTemplateGenerator:
    """Generates CI/CD pipeline templates for different providers"""

    def __init__(self):
        self.templates = {}
        self._initialize_templates()

    def _initialize_templates(self):
        """Initialize all pipeline templates"""
        self.templates = {
            PipelineProvider.GITHUB_ACTIONS: GitHubActionsTemplate(),
            PipelineProvider.GITLAB_CI: GitLabCITemplate(),
            PipelineProvider.AZURE_DEVOPS: AzureDevOpsTemplate(),
            PipelineProvider.JENKINS: JenkinsTemplate(),
            PipelineProvider.BITBUCKET: BitbucketTemplate(),
            PipelineProvider.CIRCLE_CI: CircleCITemplate(),
            PipelineProvider.TEKTON: TektonTemplate(),
        }

    def generate_pipeline(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate pipeline configuration files"""
        template = self.templates.get(config.provider)
        if not template:
            raise ValueError(f"Unsupported provider: {config.provider}")

        return template.generate(config)

    def list_available_providers(self) -> List[str]:
        """List available pipeline providers"""
        return [provider.value for provider in PipelineProvider]

    def get_provider_features(self, provider: PipelineProvider) -> Dict[str, Any]:
        """Get features supported by a provider"""
        template = self.templates.get(provider)
        if not template:
            return {}

        return template.get_features()


class BasePipelineTemplate:
    """Base class for pipeline templates"""

    def __init__(self):
        self.provider_name = "unknown"
        self.file_extensions = [".yml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate pipeline configuration"""
        raise NotImplementedError

    def get_features(self) -> Dict[str, Any]:
        """Get supported features"""
        return {
            "multi_environment": True,
            "approvals": True,
            "security_scanning": True,
            "artifact_management": True,
            "notifications": True,
            "parallel_execution": True,
            "matrix_builds": True,
        }

    def _get_fluid_commands(self) -> Dict[str, str]:
        """Get standard FLUID commands for different stages.

        Contract-path commands use the POSIX parameter-expansion default
        ``${CONTRACT:-contract.fluid.yaml}`` so Build Now works out of the
        box against the canonical filename. Operators who keep the
        contract under a different name export ``CONTRACT`` in the CI
        job / agent env to override. ``$BUILD_ID`` remains CI-injected —
        when unset, ``apply`` falls through to the plan-file branch for
        non-dbt contracts.
        """
        return {
            "validate": "fluid validate ${CONTRACT:-contract.fluid.yaml}",
            # 11-stage pipeline stage 5 — drift gate. Runs BEFORE plan so the
            # plan is never computed against a drifted baseline. --exit-on-drift
            # makes any drift a hard fail; local inspection runs omit the flag.
            "diff": (
                "fluid diff ${CONTRACT:-contract.fluid.yaml} --exit-on-drift "
                "--env ${FLUID_ENV:-dev}"
            ),
            "plan": "fluid plan ${CONTRACT:-contract.fluid.yaml} --out runtime/plan.json",
            # --build is required for dbt hybrid-reference builds; the
            # inline conditional keeps the template useful for both shapes.
            "apply": (
                'if [ -n "$BUILD_ID" ]; then '
                "fluid apply ${CONTRACT:-contract.fluid.yaml} --build $BUILD_ID --yes; "
                "else "
                "fluid apply runtime/plan.json --yes; "
                "fi"
            ),
            # 11-stage pipeline stage 8 — enforce policy bindings against the
            # freshly-applied schema. GRANT statements require the target
            # objects to exist, so policy-apply runs AFTER apply but BEFORE
            # verify — a transform running on an under-authorized object
            # would otherwise mask the policy gap as a build failure.
            # Skipped silently when bindings.json is absent (e.g. a
            # reference-only contract that delegates policy to upstream).
            "policy_apply": (
                "if [ -f dist/artifacts/policy/bindings.json ]; then "
                "fluid policy-apply dist/artifacts/policy/bindings.json "
                "--mode enforce --env ${FLUID_ENV:-dev}; "
                "elif [ -f runtime/policy/bindings.json ]; then "
                "fluid policy-apply runtime/policy/bindings.json "
                "--mode enforce --env ${FLUID_ENV:-dev}; "
                "fi"
            ),
            # 11-stage pipeline stage 9 — post-apply reconciliation. --strict
            # fails on any schema mismatch (not just missing objects), which
            # catches silent type coercions (TIMESTAMP_NTZ→LTZ, etc.). Writes
            # a JSON report for CI artifact uploads.
            "verify": (
                "fluid verify ${CONTRACT:-contract.fluid.yaml} --strict "
                "--env ${FLUID_ENV:-dev} --report runtime/verify-report.json"
            ),
            "test": "fluid test --coverage",
            "contract_test": "fluid contract-tests ${CONTRACT:-contract.fluid.yaml}",
            "generate_transformation": "fluid generate speed-transformation",
            "generate_schedule": "fluid generate schedule",
            "check_transformations": (
                "if [ -f dbt_project.yml ] || [ -d models/ ]; then "
                "fluid generate speed-transformation --check; "
                "fi"
            ),
            "check_schedules": (
                "if [ -d dags/ ] || [ -d pipelines/ ] || [ -d flows/ ]; then "
                "fluid generate schedule --check; "
                "fi"
            ),
            "visualize": "fluid viz-plan --output pipeline-viz.html && fluid viz-graph --output dependency-graph.png",
            "publish_opds": "fluid export-opds --output opds-catalog.json",
            "marketplace_publish": "fluid marketplace publish --catalog opds-catalog.json",
            # Plain `fluid doctor` — `--extended` requires scripts/diagnose.sh
            # in the workspace, which fresh forge-generated variants don't
            # ship. Users who set up extended diagnostics can edit the
            # generated file to opt in.
            "doctor": "fluid doctor",
            # Airflow DAG deployment: rsync the generated ``dags/`` directory
            # to an operator-supplied destination. Skipped when
            # $AIRFLOW_DAGS_DEST is unset or dags/ doesn't exist.
            "airflow_sync": (
                'if [ -d dags/ ] && [ -n "$AIRFLOW_DAGS_DEST" ]; then '
                'rsync -av --delete dags/ "$AIRFLOW_DAGS_DEST"/; '
                "fi"
            ),
            # Stage 10 — Catalog publish. Push the contract (+ ODPS/ODCS
            # exports) to one or more catalogs. Uses ``--target`` (repeatable)
            # per the 11-stage pipeline design: ``PUBLISH_TARGETS`` is a
            # space-separated list (e.g. ``command-center datahub``) that the
            # shell expands into ``--target X --target Y``. Falls back to the
            # legacy ``${CATALOG:-datamesh-manager}`` single-target form so
            # existing environments keep working.
            "publish_catalog": (
                'if [ -n "$DMM_API_URL" ] || [ -n "$PUBLISH_TARGETS" ]; then '
                'if [ -n "$PUBLISH_TARGETS" ]; then '
                'TARGETS=""; for t in $PUBLISH_TARGETS; do TARGETS="$TARGETS --target $t"; done; '
                "fluid publish ${CONTRACT:-contract.fluid.yaml} $TARGETS; "
                "else "
                "fluid publish ${CONTRACT:-contract.fluid.yaml} "
                "--target ${CATALOG:-datamesh-manager}; "
                "fi; "
                "fi"
            ),
        }

    def _get_common_environment_vars(self) -> Dict[str, str]:
        """Get common environment variables"""
        return {
            "FLUID_LOG_LEVEL": "INFO",
            "FLUID_CONFIG_PATH": "./fluid_config",
            "PYTHONPATH": ".",
            "PIP_CACHE_DIR": ".pip-cache",
        }

    def _credential_banner(
        self,
        comment_prefix: str,
        ci_system_name: str,
        secret_surface_hint: str,
    ) -> str:
        """Render a provider-agnostic credential-model banner.

        Every FLUID-aware CI pipeline needs the same thing: make the
        provider's env vars (SNOWFLAKE_* / GOOGLE_APPLICATION_CREDENTIALS
        / AWS_* / AZURE_* / DMM_*) available to the shell steps that
        call ``fluid``. *How* those env vars arrive differs per CI
        system — GitHub Actions uses ``secrets`` mapped via ``env:``,
        GitLab injects CI/CD variables automatically, Jenkins offers
        either agent env passthrough or the ``credentials()`` DSL, etc.

        This helper emits the common "what env vars fluid expects"
        paragraph plus a system-specific pointer so the generated
        file is self-documenting — no hard-coded Snowflake-only list.

        Arguments:
          comment_prefix: ``# `` for YAML / shell; ``// `` for Groovy.
          ci_system_name: human-readable name shown in the banner.
          secret_surface_hint: one-line hint on how THIS system
            surfaces secrets (e.g. "Settings → Secrets and variables
            → Actions"). Kept short — the rest is pattern-agnostic.
        """
        p = comment_prefix
        lines = [
            f"{p}FLUID CI/CD Pipeline — {ci_system_name}",
            f"{p}",
            f"{p}Credential model (provider-agnostic):",
            f"{p}  fluid's credential resolver reads provider auth from the runner",
            f"{p}  environment. Each provider expects its own env vars:",
            f"{p}    Snowflake → SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER / SNOWFLAKE_PASSWORD /",
            f"{p}                SNOWFLAKE_ROLE / SNOWFLAKE_WAREHOUSE / SNOWFLAKE_DATABASE",
            f"{p}    GCP       → GOOGLE_APPLICATION_CREDENTIALS (or Workload Identity via OIDC)",
            f"{p}    AWS       → AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (or OIDC role)",
            f"{p}    Azure     → AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_SUBSCRIPTION_ID",
            f"{p}    Catalog   → DMM_API_URL / DMM_API_KEY (only if using `fluid publish`)",
            f"{p}  See ``fluid_build.credentials.resolver`` for the full resolver chain.",
            f"{p}",
            f"{p}How to surface them in {ci_system_name}:",
            f"{p}  {secret_surface_hint}",
            f"{p}",
        ]
        return "\n".join(lines) + "\n"

    def _get_oidc_steps(self, oidc_provider: Optional[str]) -> List[Dict[str, Any]]:
        """Get OIDC authentication steps for GitHub Actions deploy jobs."""
        if oidc_provider == "gcp":
            return [
                {
                    "name": "Authenticate to Google Cloud (OIDC)",
                    "id": "auth",
                    "uses": _pin_action("google-github-actions/auth@v2"),
                    "with": {
                        "workload_identity_provider": "projects/${{ vars.GCP_PROJECT_NUMBER }}/locations/global/workloadIdentityPools/${{ vars.WIF_POOL }}/providers/${{ vars.WIF_PROVIDER }}",
                        "service_account": "${{ vars.GCP_SA_EMAIL }}",
                    },
                }
            ]
        elif oidc_provider == "aws":
            return [
                {
                    "name": "Configure AWS Credentials (OIDC)",
                    "uses": _pin_action("aws-actions/configure-aws-credentials@v4"),
                    "with": {
                        "role-to-assume": "${{ vars.AWS_ROLE_ARN }}",
                        "aws-region": "${{ vars.AWS_REGION }}",
                    },
                }
            ]
        elif oidc_provider == "azure":
            return [
                {
                    "name": "Azure Login (OIDC)",
                    "uses": _pin_action("azure/login@v2"),
                    "with": {
                        "client-id": "${{ vars.AZURE_CLIENT_ID }}",
                        "tenant-id": "${{ vars.AZURE_TENANT_ID }}",
                        "subscription-id": "${{ vars.AZURE_SUBSCRIPTION_ID }}",
                    },
                }
            ]
        return []

    def _get_deploy_job_permissions(self, oidc_provider: Optional[str]) -> Dict[str, str]:
        """Get permissions for a deployment job."""
        perms = {"contents": "read"}
        if oidc_provider:
            perms["id-token"] = "write"
        return perms

    def _generate_env_ci_example(self, oidc_provider: Optional[str] = None) -> str:
        """Generate .env.ci.example content with required secrets per provider."""
        lines = [
            "# Required CI/CD Secrets for FLUID Pipeline",
            "# Copy these to your CI provider's secret store",
            "#",
            "# FLUID Configuration",
            "FLUID_LOG_LEVEL=INFO",
            "# CONTRACT=contract.fluid.yaml",
            "",
        ]
        if oidc_provider == "gcp":
            lines += [
                "# GCP Workload Identity Federation (OIDC — no stored secrets needed!)",
                "# Configure these as GitHub Actions variables (vars), not secrets:",
                "# GCP_PROJECT_NUMBER=123456789",
                "# WIF_POOL=fluid-pool",
                "# WIF_PROVIDER=github-provider",
                "# GCP_SA_EMAIL=fluid@project.iam.gserviceaccount.com",
            ]
        elif oidc_provider == "aws":
            lines += [
                "# AWS OIDC (no stored secrets needed!)",
                "# Configure these as GitHub Actions variables (vars), not secrets:",
                "# AWS_ROLE_ARN=arn:aws:iam::123456789:role/fluid-deploy",
                "# AWS_REGION=us-east-1",
            ]
        elif oidc_provider == "azure":
            lines += [
                "# Azure Federated Identity (OIDC — no stored secrets needed!)",
                "# Configure these as GitHub Actions variables (vars), not secrets:",
                "# AZURE_CLIENT_ID=00000000-0000-0000-0000-000000000000",
                "# AZURE_TENANT_ID=00000000-0000-0000-0000-000000000000",
                "# AZURE_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000",
            ]
        else:
            lines += [
                "# Provider Authentication",
                "# Prefer OIDC/Workload Identity Federation over stored secrets.",
                "# See: fluid forge --ci github_actions --oidc-provider gcp",
                "#",
                "# If OIDC is not available, configure these as CI secrets:",
                "# SNOWFLAKE_ACCOUNT=your_account",
                "# SNOWFLAKE_USER=your_user",
                "# SNOWFLAKE_PASSWORD=your_password  # Use key-pair auth instead!",
                "# GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json",
                "# AWS_ACCESS_KEY_ID=AKIA...  # Use OIDC instead!",
                "# AWS_SECRET_ACCESS_KEY=...",
            ]
        lines.append("")
        return "\n".join(lines)


class GitHubActionsTemplate(BasePipelineTemplate):
    """GitHub Actions pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "GitHub Actions"
        self.file_extensions = [".yml", ".yaml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate GitHub Actions workflow"""

        if config.complexity == PipelineComplexity.BASIC:
            return self._generate_basic_workflow(config)
        elif config.complexity == PipelineComplexity.STANDARD:
            return self._generate_standard_workflow(config)
        elif config.complexity == PipelineComplexity.ADVANCED:
            return self._generate_advanced_workflow(config)
        else:  # ENTERPRISE
            return self._generate_enterprise_workflow(config)

    def _generate_basic_workflow(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate basic GitHub Actions workflow"""

        commands = self._get_fluid_commands()
        env_vars = self._get_common_environment_vars()

        workflow = {
            "name": "FLUID DataOps Pipeline",
            "on": {
                "push": {"branches": ["main", "develop"]},
                "pull_request": {"branches": ["main"]},
            },
            "permissions": {},  # Least privilege — grant per-job only
            "env": env_vars,
            "jobs": {
                "fluid-pipeline": {
                    "runs-on": "ubuntu-latest",
                    "permissions": self._get_deploy_job_permissions(config.oidc_provider),
                    "steps": [
                        {"name": "Checkout code", "uses": _pin_action("actions/checkout@v4")},
                        {
                            "name": "Set up Python",
                            "uses": _pin_action("actions/setup-python@v5"),
                            "with": {"python-version": "3.9"},
                        },
                        {"name": "Install FLUID", "run": "pip install --quiet data-product-forge"},
                        *self._get_oidc_steps(config.oidc_provider),
                        {"name": "FLUID Doctor Check", "run": commands["doctor"]},
                        {"name": "Validate Configuration", "run": commands["validate"]},
                        {
                            "name": "Generate Transformations",
                            "run": commands["generate_transformation"],
                        },
                        {"name": "Generate Schedules", "run": commands["generate_schedule"]},
                        {"name": "Generate Plan", "run": commands["plan"]},
                        {
                            "name": "Apply Changes",
                            "run": commands["apply"],
                            "if": "github.ref == 'refs/heads/main'",
                        },
                        {"name": "Run Tests", "run": commands["test"]},
                        {
                            "name": "Generate Artifacts",
                            "run": f"{commands['visualize']} && {commands['publish_opds']}",
                        },
                        {
                            "name": "Upload Artifacts",
                            "uses": _pin_action("actions/upload-artifact@v4"),
                            "with": {
                                "name": "fluid-artifacts",
                                "path": "plan.json\npipeline-viz.html\ndependency-graph.png\nopds-catalog.json\ntest-results/",
                            },
                        },
                    ],
                }
            },
        }

        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="GitHub Actions",
            secret_surface_hint=(
                "Settings → Secrets and variables → Actions. Reference per-job via "
                "`env: FOO: ${{ secrets.FOO }}`."
            ),
        )
        files = {".github/workflows/fluid-pipeline.yml": banner + yaml.dump(workflow, indent=2)}
        files[".env.ci.example"] = self._generate_env_ci_example(config.oidc_provider)
        return files

    def _generate_standard_workflow(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate standard GitHub Actions workflow with multiple environments"""

        commands = self._get_fluid_commands()
        env_vars = self._get_common_environment_vars()

        workflow = {
            "name": "FLUID DataOps Pipeline - Standard",
            "on": {
                "push": {"branches": ["main", "develop", "feature/*"]},
                "pull_request": {"branches": ["main", "develop"]},
            },
            "permissions": {},  # Least privilege — grant per-job only
            "env": env_vars,
            "jobs": {
                "validate": {
                    "runs-on": "ubuntu-latest",
                    "permissions": {"contents": "read"},
                    "outputs": {"changes-detected": "${{ steps.changes.outputs.changes }}"},
                    "steps": [
                        {"name": "Checkout", "uses": _pin_action("actions/checkout@v4")},
                        {
                            "name": "Setup Python",
                            "uses": _pin_action("actions/setup-python@v5"),
                            "with": {"python-version": "3.9"},
                        },
                        {
                            "name": "Install Dependencies",
                            "run": "pip install --quiet data-product-forge",
                        },
                        {"name": "FLUID Doctor", "run": commands["doctor"]},
                        {"name": "Validate", "run": commands["validate"]},
                        {
                            "name": "Detect Changes",
                            "id": "changes",
                            "run": 'if git diff --name-only HEAD~1 | grep -E \'\\.(sql|py|yaml|json)$\'; then\n  echo "changes=true" >> $GITHUB_OUTPUT\nelse\n  echo "changes=false" >> $GITHUB_OUTPUT\nfi',
                        },
                    ],
                },
                "generate": {
                    "needs": "validate",
                    "runs-on": "ubuntu-latest",
                    "permissions": {"contents": "read"},
                    "if": "needs.validate.outputs.changes-detected == 'true'",
                    "steps": [
                        {"name": "Checkout", "uses": _pin_action("actions/checkout@v4")},
                        {
                            "name": "Setup Python",
                            "uses": _pin_action("actions/setup-python@v5"),
                            "with": {"python-version": "3.9"},
                        },
                        {
                            "name": "Install Dependencies",
                            "run": "pip install --quiet data-product-forge",
                        },
                        {
                            "name": "Generate Transformations",
                            "run": commands["generate_transformation"],
                        },
                        {
                            "name": "Generate Schedules",
                            "run": commands["generate_schedule"],
                        },
                        {
                            "name": "Check Transformation Drift",
                            "run": commands["check_transformations"],
                        },
                        {
                            "name": "Check Schedule Drift",
                            "run": commands["check_schedules"],
                        },
                    ],
                },
                "plan": {
                    "needs": "generate",
                    "runs-on": "ubuntu-latest",
                    "permissions": {"contents": "read"},
                    "if": "needs.validate.outputs.changes-detected == 'true'",
                    "steps": [
                        {"name": "Checkout", "uses": _pin_action("actions/checkout@v4")},
                        {
                            "name": "Setup Python",
                            "uses": _pin_action("actions/setup-python@v5"),
                            "with": {"python-version": "3.9"},
                        },
                        {
                            "name": "Install Dependencies",
                            "run": "pip install --quiet data-product-forge",
                        },
                        {"name": "Generate Plan", "run": commands["plan"]},
                        {
                            "name": "Upload Plan",
                            "uses": _pin_action("actions/upload-artifact@v4"),
                            "with": {"name": "plan", "path": "plan.json"},
                        },
                    ],
                },
                "test": {
                    "needs": "validate",
                    "runs-on": "ubuntu-latest",
                    "permissions": {"contents": "read"},
                    "strategy": {"matrix": {"test-type": ["unit", "integration", "contract"]}},
                    "steps": [
                        {"name": "Checkout", "uses": _pin_action("actions/checkout@v4")},
                        {
                            "name": "Setup Python",
                            "uses": _pin_action("actions/setup-python@v5"),
                            "with": {"python-version": "3.9"},
                        },
                        {
                            "name": "Install Dependencies",
                            "run": "pip install --quiet data-product-forge",
                        },
                        {
                            "name": "Run Tests",
                            "run": "fluid test --type ${{ matrix.test-type }} --output test-results-${{ matrix.test-type }}.xml",
                        },
                        {
                            "name": "Upload Test Results",
                            "uses": _pin_action("actions/upload-artifact@v4"),
                            "with": {
                                "name": "test-results-${{ matrix.test-type }}",
                                "path": "test-results-${{ matrix.test-type }}.xml",
                            },
                        },
                    ],
                },
            },
        }

        # Add deployment jobs for each environment
        for env in config.environments:
            job_name = f"deploy-{env}"

            depends_on = ["plan", "test"]
            if env == "prod":
                depends_on.append("deploy-staging")

            deploy_steps = [
                {"name": "Checkout", "uses": _pin_action("actions/checkout@v4")},
                {
                    "name": "Setup Python",
                    "uses": _pin_action("actions/setup-python@v5"),
                    "with": {"python-version": "3.9"},
                },
                {"name": "Install Dependencies", "run": "pip install --quiet data-product-forge"},
                *self._get_oidc_steps(config.oidc_provider),
                {
                    "name": "Generate Transformations",
                    "run": f"FLUID_ENV={env} {commands['generate_transformation']}",
                },
                {
                    "name": "Generate Schedules",
                    "run": f"FLUID_ENV={env} {commands['generate_schedule']}",
                },
                {
                    "name": "Download Plan",
                    "uses": _pin_action("actions/download-artifact@v4"),
                    "with": {"name": "plan"},
                },
                {
                    "name": f"Deploy to {env.upper()}",
                    "run": f"FLUID_ENV={env} {commands['apply']}",
                },
                # 11-stage pipeline stage 8 — policy enforcement. Runs
                # AFTER apply (GRANTs need the target objects to exist)
                # and BEFORE verify/tests (so unauthorized access surfaces
                # as a clear policy failure, not a masked build error).
                # Self-gates on bindings.json existence — no-op when the
                # contract doesn't emit policies.
                {
                    "name": f"Enforce Policies ({env.upper()})",
                    "run": f"FLUID_ENV={env} {commands['policy_apply']}",
                },
                # 11-stage pipeline stage 9 — post-apply reconciliation.
                # Runs after policy enforcement so the verify pass sees the
                # fully-authorized state. Writes a JSON report uploaded
                # as a CI artifact below.
                {
                    "name": f"Verify Deployed State ({env.upper()})",
                    "run": f"FLUID_ENV={env} {commands['verify']}",
                },
                {
                    "name": "Run Contract Tests",
                    "run": f"FLUID_ENV={env} {commands['contract_test']}",
                },
                # Airflow DAG sync + catalog publish fire only in prod.
                # Both commands self-gate on env vars, so safe no-ops
                # when AIRFLOW_DAGS_DEST / DMM_API_URL are unset.
                {
                    "name": "Sync Airflow DAGs",
                    "run": commands["airflow_sync"],
                    "if": f"'{env}' == 'prod'",
                },
                {
                    "name": "Publish Contract to Catalog",
                    "run": commands["publish_catalog"],
                    "if": f"'{env}' == 'prod'",
                },
                {
                    "name": "Generate Visualization",
                    "run": commands["visualize"],
                    "if": f"'{env}' == 'prod'",
                },
                {
                    "name": "Publish to Marketplace",
                    "run": f"{commands['publish_opds']} && {commands['marketplace_publish']}",
                    "if": f"'{env}' == 'prod' && {str(config.enable_marketplace_publishing).lower()}",
                },
            ]

            workflow["jobs"][job_name] = {
                "needs": depends_on,
                "runs-on": "ubuntu-latest",
                "permissions": self._get_deploy_job_permissions(config.oidc_provider),
                "environment": env,
                "if": f"github.ref == 'refs/heads/main' || (github.ref == 'refs/heads/develop' && '{env}' != 'prod')",
                "steps": deploy_steps,
            }

        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="GitHub Actions",
            secret_surface_hint=(
                "Settings → Secrets and variables → Actions. Then reference them "
                "per-job via `env: FOO: ${{ secrets.FOO }}` or, for OIDC providers "
                "(GCP/AWS/Azure), use the Workload Identity Federation steps "
                "already wired when --oidc-provider is set."
            ),
        )
        files = {".github/workflows/fluid-standard.yml": banner + yaml.dump(workflow, indent=2)}
        files[".env.ci.example"] = self._generate_env_ci_example(config.oidc_provider)
        return files

    def _generate_advanced_workflow(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate advanced workflow with approvals and security"""

        files = self._generate_standard_workflow(config)

        # Add security workflow
        security_workflow = {
            "name": "Security Scan",
            "on": {
                "push": {"branches": ["main", "develop"]},
                "schedule": [{"cron": "0 2 * * *"}],  # Daily at 2 AM
            },
            "permissions": {},  # Least privilege — grant per-job only
            "jobs": {
                "security-scan": {
                    "runs-on": "ubuntu-latest",
                    "permissions": {
                        "contents": "read",
                        "security-events": "write",  # Required for SARIF upload
                    },
                    "steps": [
                        {"name": "Checkout", "uses": _pin_action("actions/checkout@v4")},
                        {
                            "name": "Run Trivy vulnerability scanner",
                            "uses": _pin_action("aquasecurity/trivy-action@v0"),
                            "with": {
                                "scan-type": "fs",
                                "format": "sarif",
                                "output": "trivy-results.sarif",
                            },
                        },
                        {"name": "FLUID Security Check", "run": "fluid validate --security-only"},
                        {
                            "name": "Upload SARIF",
                            "uses": _pin_action("github/codeql-action/upload-sarif@v3"),
                            "with": {"sarif_file": "trivy-results.sarif"},
                            "if": "always()",
                        },
                    ],
                }
            },
        }

        files[".github/workflows/security.yml"] = yaml.dump(security_workflow, indent=2)

        return files

    def _generate_enterprise_workflow(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate enterprise workflow with full governance"""

        files = self._generate_advanced_workflow(config)

        # Add compliance and audit workflow
        compliance_workflow = {
            "name": "Compliance and Audit",
            "on": {"schedule": [{"cron": "0 0 * * 0"}], "workflow_dispatch": None},  # Weekly
            "permissions": {},  # Least privilege — grant per-job only
            "jobs": {
                "compliance-audit": {
                    "runs-on": "ubuntu-latest",
                    "permissions": {"contents": "read"},
                    "steps": [
                        {"name": "Checkout", "uses": _pin_action("actions/checkout@v4")},
                        {
                            "name": "Generate Compliance Report",
                            "run": "fluid audit --compliance --output compliance-report.json",
                        },
                        {"name": "Check Data Lineage", "run": "fluid lineage --validate"},
                        {"name": "Performance Benchmarks", "run": "fluid benchmark --baseline"},
                        {
                            "name": "Upload Compliance Artifacts",
                            "uses": _pin_action("actions/upload-artifact@v4"),
                            "with": {
                                "name": "compliance-artifacts",
                                "path": "compliance-report.json",
                            },
                        },
                    ],
                },
                "supply-chain": {
                    "runs-on": "ubuntu-latest",
                    "permissions": {
                        "contents": "read",
                        "id-token": "write",
                        "attestations": "write",
                    },
                    "steps": [
                        {"name": "Checkout", "uses": _pin_action("actions/checkout@v4")},
                        {
                            "name": "Generate SBOM",
                            "uses": _pin_action("anchore/sbom-action@v0"),
                            "with": {"output-file": "sbom.spdx.json"},
                        },
                        {
                            "name": "Attest Build Provenance",
                            "uses": _pin_action("actions/attest-build-provenance@v2"),
                            "with": {"subject-path": "sbom.spdx.json"},
                        },
                        {
                            "name": "Upload SBOM",
                            "uses": _pin_action("actions/upload-artifact@v4"),
                            "with": {"name": "sbom", "path": "sbom.spdx.json"},
                        },
                    ],
                },
            },
        }

        files[".github/workflows/compliance.yml"] = yaml.dump(compliance_workflow, indent=2)

        return files


class GitLabCITemplate(BasePipelineTemplate):
    """GitLab CI pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "GitLab CI"
        self.file_extensions = [".yml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate GitLab CI pipeline"""

        commands = self._get_fluid_commands()
        env_vars = self._get_common_environment_vars()

        if config.complexity == PipelineComplexity.BASIC:
            pipeline = self._generate_basic_gitlab_pipeline(config, commands, env_vars)
        elif config.complexity == PipelineComplexity.STANDARD:
            pipeline = self._generate_standard_gitlab_pipeline(config, commands, env_vars)
        else:
            pipeline = self._generate_advanced_gitlab_pipeline(config, commands, env_vars)

        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="GitLab CI",
            secret_surface_hint=(
                "Project Settings → CI/CD → Variables. GitLab auto-injects them as "
                "env for every job — mark as Masked + Protected to scope to "
                "protected branches."
            ),
        )
        return {".gitlab-ci.yml": banner + yaml.dump(pipeline, indent=2)}

    def _generate_basic_gitlab_pipeline(self, config, commands, env_vars):
        """Generate basic GitLab CI pipeline"""

        return {
            "stages": ["validate", "generate", "plan", "apply", "test", "publish"],
            "variables": env_vars,
            "image": "python:3.12-slim",
            "before_script": ["pip install --quiet data-product-forge"],
            "validate": {
                "stage": "validate",
                "script": [commands["doctor"], commands["validate"]],
                "rules": [{"if": "$CI_PIPELINE_SOURCE == 'push'"}],
            },
            "generate": {
                "stage": "generate",
                "script": [
                    commands["generate_transformation"],
                    commands["generate_schedule"],
                ],
            },
            "plan": {
                "stage": "plan",
                "script": [commands["plan"]],
                "artifacts": {"paths": ["plan.json"], "expire_in": "1 day"},
            },
            "apply": {
                "stage": "apply",
                "script": [commands["apply"]],
                "dependencies": ["plan"],
                "only": ["main"],
                "when": "manual",
            },
            "test": {
                "stage": "test",
                "script": [commands["test"], commands["contract_test"]],
                "artifacts": {
                    "reports": {"junit": "test-results/*.xml"},
                    "paths": ["test-results/"],
                },
            },
            "airflow_sync": {
                "stage": "deploy",
                "script": [commands["airflow_sync"]],
                "only": ["main"],
            },
            "publish": {
                "stage": "publish",
                "script": [
                    commands["publish_catalog"],
                    commands["visualize"],
                    commands["publish_opds"],
                ],
                "artifacts": {
                    "paths": ["pipeline-viz.html", "dependency-graph.png", "opds-catalog.json"],
                    "expire_in": "30 days",
                    "when": "always",
                },
                "only": ["main"],
            },
        }

    def _generate_standard_gitlab_pipeline(self, config, commands, env_vars):
        """Generate standard GitLab CI pipeline with environments"""

        pipeline = {
            "stages": ["validate", "generate", "test", "plan", "deploy", "publish"],
            "variables": env_vars,
            "image": "python:3.12-slim",
            "before_script": ["pip install --quiet data-product-forge"],
        }

        # Add validation, generation, and testing jobs
        pipeline.update(
            {
                "validate": {
                    "stage": "validate",
                    "script": [commands["doctor"], commands["validate"]],
                },
                "generate-artifacts": {
                    "stage": "generate",
                    "script": [
                        commands["generate_transformation"],
                        commands["generate_schedule"],
                        commands["check_transformations"],
                        commands["check_schedules"],
                    ],
                },
                "unit-tests": {
                    "stage": "test",
                    "script": ["fluid test --type unit"],
                    "artifacts": {"reports": {"junit": "test-results/unit.xml"}},
                },
                "integration-tests": {
                    "stage": "test",
                    "script": ["fluid test --type integration"],
                    "artifacts": {"reports": {"junit": "test-results/integration.xml"}},
                },
                "plan": {
                    "stage": "plan",
                    "script": [commands["plan"]],
                    "artifacts": {"paths": ["plan.json"]},
                },
            }
        )

        # Add deployment jobs for each environment
        for env in config.environments:
            deploy_script = []
            # Add OIDC authentication for GitLab CI if configured.
            # SECURITY_REVIEW S-005: never write federated creds to a
            # predictable `/tmp/*.json` path. Use ``mktemp`` + ``chmod 600``
            # + a ``trap`` cleanup so the credential file exists only for
            # the lifetime of the single shell invocation.
            if config.oidc_provider == "gcp":
                deploy_script.append(
                    'CRED_FILE="$(mktemp)" '
                    '&& chmod 600 "$CRED_FILE" '
                    "&& trap 'rm -f \"$CRED_FILE\"' EXIT "
                    '&& printf "%s" "${FLUID_OIDC_TOKEN}" > "$CRED_FILE" '
                    '&& gcloud auth login --cred-file="$CRED_FILE" --quiet'
                )
            elif config.oidc_provider == "aws":
                deploy_script.append(
                    'CRED_FILE="$(mktemp)" '
                    '&& chmod 600 "$CRED_FILE" '
                    "&& trap 'rm -f \"$CRED_FILE\"' EXIT "
                    "&& aws sts assume-role-with-web-identity"
                    ' --role-arn "${AWS_ROLE_ARN}"'
                    ' --web-identity-token "${FLUID_OIDC_TOKEN}"'
                    ' --role-session-name "fluid-ci-${CI_PIPELINE_ID}"'
                    ' > "$CRED_FILE"'
                )
            deploy_script.append(f"FLUID_ENV={env} {commands['generate_transformation']}")
            deploy_script.append(f"FLUID_ENV={env} {commands['generate_schedule']}")
            deploy_script.append(f"FLUID_ENV={env} {commands['apply']}")

            deploy_job = {
                "stage": "deploy",
                "script": deploy_script,
                "environment": {"name": env},
                "dependencies": ["plan"],
            }

            # GitLab native OIDC token injection
            if config.oidc_provider:
                deploy_job["id_tokens"] = {
                    "FLUID_OIDC_TOKEN": {"aud": "https://YOUR_IDENTITY_POOL_AUDIENCE"}
                }

            if env == "prod":
                deploy_job["when"] = "manual"
                deploy_job["only"] = ["main"]
            elif env == "staging":
                deploy_job["only"] = ["main", "develop"]

            pipeline[f"deploy-{env}"] = deploy_job

        # Airflow DAG sync — no-op when dags/ is empty or AIRFLOW_DAGS_DEST is unset.
        pipeline["airflow-sync"] = {
            "stage": "deploy",
            "script": [commands["airflow_sync"]],
            "only": ["main"],
            "dependencies": [f"deploy-{config.environments[-1]}"],
        }

        # Add publishing job
        pipeline["publish"] = {
            "stage": "publish",
            "script": [
                commands["publish_catalog"],
                commands["visualize"],
                commands["publish_opds"],
            ],
            "artifacts": {
                "paths": ["pipeline-viz.html", "dependency-graph.png", "opds-catalog.json"]
            },
            "only": ["main"],
            "dependencies": [f"deploy-{config.environments[-1]}"],  # Depends on final environment
        }

        if config.enable_marketplace_publishing:
            pipeline["marketplace-publish"] = {
                "stage": "publish",
                "script": [commands["marketplace_publish"]],
                "only": ["main"],
                "when": "manual",
                "dependencies": ["publish"],
            }

        return pipeline

    def _generate_advanced_gitlab_pipeline(self, config, commands, env_vars):
        """Generate advanced GitLab CI pipeline with security and compliance"""

        pipeline = self._generate_standard_gitlab_pipeline(config, commands, env_vars)

        # Add security stage
        pipeline["stages"].insert(-1, "security")

        # Add security jobs
        pipeline.update(
            {
                "security-scan": {
                    "stage": "security",
                    "script": ["fluid validate --security-only", "trivy fs ."],
                    "artifacts": {"reports": {"sast": "security-report.json"}},
                },
                "compliance-check": {
                    "stage": "security",
                    "script": ["fluid audit --compliance"],
                    "artifacts": {"paths": ["compliance-report.json"]},
                    "only": ["main"],
                },
            }
        )

        return pipeline


class AzureDevOpsTemplate(BasePipelineTemplate):
    """Azure DevOps pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "Azure DevOps"
        self.file_extensions = [".yml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate Azure DevOps pipeline"""

        commands = self._get_fluid_commands()

        pipeline = {
            "trigger": {"branches": {"include": ["main", "develop"]}},
            "pr": {"branches": {"include": ["main", "develop"]}},
            "pool": {"vmImage": "ubuntu-latest"},
            "variables": self._get_common_environment_vars(),
            "stages": [],
        }

        # Validation stage
        validate_stage = {
            "stage": "Validate",
            "displayName": "Validate and Test",
            "jobs": [
                {
                    "job": "ValidateJob",
                    "displayName": "FLUID Validation",
                    "steps": [
                        {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.12"}},
                        {
                            "script": "pip install --quiet data-product-forge",
                            "displayName": "Install dependencies",
                        },
                        {"script": commands["doctor"], "displayName": "FLUID Doctor Check"},
                        {"script": commands["validate"], "displayName": "Validate configuration"},
                        {
                            "script": commands["generate_transformation"],
                            "displayName": "Generate transformations",
                        },
                        {
                            "script": commands["generate_schedule"],
                            "displayName": "Generate schedules",
                        },
                        {"script": commands["plan"], "displayName": "Generate plan"},
                        {
                            "task": "PublishBuildArtifacts@1",
                            "inputs": {"pathToPublish": "plan.json", "artifactName": "plan"},
                        },
                    ],
                },
                {
                    "job": "TestJob",
                    "displayName": "Run Tests",
                    "steps": [
                        {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.12"}},
                        {
                            "script": "pip install --quiet data-product-forge",
                            "displayName": "Install dependencies",
                        },
                        {"script": commands["test"], "displayName": "Run tests"},
                        {
                            "task": "PublishTestResults@2",
                            "inputs": {
                                "testResultsFiles": "test-results/*.xml",
                                "testRunTitle": "FLUID Tests",
                            },
                        },
                    ],
                },
            ],
        }

        pipeline["stages"].append(validate_stage)

        # Deployment stages for each environment
        for env in config.environments:
            deploy_stage = {
                "stage": f"Deploy{env.title()}",
                "displayName": f"Deploy to {env.upper()}",
                "dependsOn": "Validate",
                "jobs": [
                    {
                        "deployment": f"Deploy{env.title()}Job",
                        "displayName": f"Deploy to {env}",
                        "environment": env,
                        "strategy": {
                            "runOnce": {
                                "deploy": {
                                    "steps": [
                                        {
                                            "task": "UsePythonVersion@0",
                                            "inputs": {"versionSpec": "3.12"},
                                        },
                                        {
                                            "script": "pip install --quiet data-product-forge",
                                            "displayName": "Install dependencies",
                                        },
                                        {
                                            "task": "DownloadBuildArtifacts@0",
                                            "inputs": {"artifactName": "plan"},
                                        },
                                        {
                                            "script": f"FLUID_ENV={env} {commands['generate_transformation']}",
                                            "displayName": "Generate transformations",
                                        },
                                        {
                                            "script": f"FLUID_ENV={env} {commands['generate_schedule']}",
                                            "displayName": "Generate schedules",
                                        },
                                        {
                                            "script": f"FLUID_ENV={env} {commands['apply']}",
                                            "displayName": f"Apply to {env}",
                                        },
                                        {
                                            "script": f"FLUID_ENV={env} {commands['contract_test']}",
                                            "displayName": "Run contract tests",
                                        },
                                    ]
                                }
                            }
                        },
                    }
                ],
            }

            if env == "prod":
                deploy_stage["condition"] = (
                    "and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'))"
                )

            pipeline["stages"].append(deploy_stage)

        # Airflow DAG sync + catalog publish: always added on main.
        # Both shell commands are self-gating via env vars
        # (AIRFLOW_DAGS_DEST / DMM_API_URL) so they no-op cleanly when
        # the operator hasn't configured them.
        airflow_publish_stage = {
            "stage": "DeployExtras",
            "displayName": "Deploy Extras (Airflow + Catalog)",
            "dependsOn": f"Deploy{config.environments[-1].title()}",
            "condition": "and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'))",
            "jobs": [
                {
                    "job": "AirflowAndPublish",
                    "displayName": "Sync DAGs + Publish contract",
                    "steps": [
                        {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.12"}},
                        {
                            "script": "pip install data-product-forge",
                            "displayName": "Install data-product-forge",
                        },
                        {
                            "script": commands["airflow_sync"],
                            "displayName": "Sync Airflow DAGs",
                        },
                        {
                            "script": commands["publish_catalog"],
                            "displayName": "Publish contract to catalog",
                        },
                    ],
                }
            ],
        }
        pipeline["stages"].append(airflow_publish_stage)

        # Publishing stage (OPDS + marketplace — opt-in)
        if config.enable_marketplace_publishing:
            publish_stage = {
                "stage": "Publish",
                "displayName": "Publish Artifacts",
                "dependsOn": "DeployExtras",
                "condition": "and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'))",
                "jobs": [
                    {
                        "job": "PublishJob",
                        "displayName": "Publish to Marketplace",
                        "steps": [
                            {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.12"}},
                            {
                                "script": "pip install data-product-forge",
                                "displayName": "Install data-product-forge",
                            },
                            {
                                "script": commands["visualize"],
                                "displayName": "Generate visualizations",
                            },
                            {
                                "script": commands["publish_opds"],
                                "displayName": "Export OPDS catalog",
                            },
                            {
                                "script": commands["marketplace_publish"],
                                "displayName": "Publish to marketplace",
                            },
                            {
                                "task": "PublishBuildArtifacts@1",
                                "inputs": {
                                    "pathToPublish": "opds-catalog.json",
                                    "artifactName": "marketplace-artifacts",
                                },
                            },
                        ],
                    }
                ],
            }

            pipeline["stages"].append(publish_stage)

        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="Azure DevOps Pipelines",
            secret_surface_hint=(
                "Pipelines → Library → Variable Groups. Link the group to the "
                "pipeline and map secret vars into step env via `env: FOO: $(FOO)`."
            ),
        )
        return {"azure-pipelines.yml": banner + yaml.dump(pipeline, indent=2)}


class JenkinsTemplate(BasePipelineTemplate):
    """Jenkins pipeline template — 11-stage parameterized Jenkinsfile.

    Produces a fully-parameterized declarative pipeline mirroring the
    perfect-pipeline 11-stage design. Every stage has its own
    ``RUN_STAGE_N_NAME`` boolean toggle + per-stage configuration (apply
    mode, publish targets, diff drift behavior, etc.) exposed as Jenkins
    build parameters so operators can run any subset of the pipeline
    from the "Build With Parameters" UI without editing Groovy.

    Core operating modes the parameters support out of the box:

    * **Structural dry-run** (bundle → validate → generate → validate
      artifacts → diff → plan → apply ``--mode dry-run``) — zero
      warehouse writes. Safe for every PR.
    * **Schema deploy** (above + apply ``--mode amend`` + policy-apply
      + verify). Stage 10 publish and stage 11 schedule-sync off.
    * **Full productionization** (all 11 stages on, apply
      ``--mode amend-and-build`` with a specific BUILD_ID, publish to a
      list of catalogs, schedule-sync DAGs to the scheduler).
    * **Destructive replace** (apply ``--mode replace`` +
      ``ALLOW_DATA_LOSS=true``). Auto-snapshot before drop.

    Back-compat: the legacy ``generates_artifacts: False`` (reference-only
    contracts) and ``workdir: "..."`` (subfolder checkout) config flags
    still work — stage 3 is skipped when the contract declares itself
    reference-only, and every sh block is wrapped with ``cd "<workdir>"``
    when workdir is set.
    """

    def __init__(self):
        super().__init__()
        self.provider_name = "Jenkins"
        self.file_extensions = [".groovy"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate the 11-stage parameterized Jenkinsfile.

        Returns a ``{"Jenkinsfile": <content>}`` dict matching the
        ``BasePipelineTemplate`` contract.
        """

        # ``cd "<workdir>" && `` prefix for every sh block when the
        # contract lives in a subfolder of the SCM checkout. Jenkins
        # checks out at repo root; fluid needs to run from the contract
        # folder. Every sh block uses the triple-single ``sh '''...'''``
        # form so double-quoted paths inside don't collide with outer
        # string delimiters, and Jenkins params reach the shell via
        # env-var injection (``${APPLY_MODE}`` etc.) rather than Groovy
        # interpolation.
        CD = f'cd "{config.workdir}" && ' if config.workdir else ""

        # Archive patterns are rooted at the SCM root (the Jenkins workspace),
        # so every glob gets the workdir prefix. ``allowEmptyArchive: true``
        # on every archiveArtifacts handles reference-only contracts that
        # legitimately produce no plan.json / artifacts/ / reports.
        P = f"{config.workdir}/" if config.workdir else ""

        # Reference-only contracts (pattern: hybrid-reference) delegate
        # generation to upstream — omit stage 3 entirely in that case.
        stage_3_enabled_default = "true" if config.generates_artifacts else "false"

        # --- Install-mode dispatch --------------------------------------
        # Pick the Setup stage's pip-install shell body based on
        # ``config.install_mode``. The generated Jenkinsfile carries only
        # the logic for the selected mode — no runtime branching, no dead
        # fallback code. This keeps production Jenkinsfiles short + clean.
        install_mode = config.install_mode or "pypi"
        if install_mode == "pypi":
            setup_install_sh = """                // Install the fluid CLI from stable PyPI. Four Jenkins
                // parameters let operators override from the Build-With-
                // Parameters dialog without editing Groovy:
                //   FLUID_PACKAGE_SPEC         package spec (name + optional version
                //                              pin, e.g. 'data-product-forge==X.Y.Z')
                //   FLUID_PIP_INDEX_URL        primary index (leave blank for stable
                //                              PyPI; set 'https://test.pypi.org/simple/'
                //                              for TestPyPI pilot builds)
                //   FLUID_PIP_EXTRA_INDEX_URL  fallback index (usually pypi.org/simple
                //                              when PRIMARY points at TestPyPI, so
                //                              transitive deps still resolve)
                //   FLUID_ALLOW_PRERELEASE     'true' → add --pre (alpha/rc releases);
                //                              leave 'false' for stable-only in prod
                sh '''set -e
                      INDEX_FLAGS=""
                      if [ -n "${FLUID_PIP_INDEX_URL:-}" ]; then
                        INDEX_FLAGS="--index-url ${FLUID_PIP_INDEX_URL}"
                      fi
                      if [ -n "${FLUID_PIP_EXTRA_INDEX_URL:-}" ]; then
                        INDEX_FLAGS="${INDEX_FLAGS} --extra-index-url ${FLUID_PIP_EXTRA_INDEX_URL}"
                      fi
                      PRE_FLAG=""
                      if [ "${FLUID_ALLOW_PRERELEASE:-false}" = "true" ]; then
                        PRE_FLAG="--pre"
                      fi
                      pip install --quiet --upgrade ${PRE_FLAG} ${INDEX_FLAGS} \\
                        "${FLUID_PACKAGE_SPEC:-data-product-forge}"'''"""
        elif install_mode == "dev-source":
            # install-mode=dev-source uses PYTHONPATH=/forge-cli-src to
            # point Python at the bind mount LIVE — no pip install. That
            # sidesteps a pile of wheel-cache / stale-file bugs that made
            # ``pip install /forge-cli-src`` unreliable in practice.
            # The PYTHONPATH export happens in the pipeline-level
            # ``environment {}`` block (added below in dev-source mode),
            # so every downstream sh step inherits it automatically.
            setup_install_sh = """                sh '''set -e
                      if [ ! -d /forge-cli-src ] || [ ! -f /forge-cli-src/pyproject.toml ]; then
                        cat >&2 <<EOM

ERROR: This Jenkinsfile has install-mode=dev-source but /forge-cli-src
       is not mounted in the Jenkins container.

       To fix, add this to deploy/docker/docker-compose.yml under the
       jenkins service's volumes block:

         - \\\\${FORGE_CLI_REPO:-../../../forge-cli}:/forge-cli-src:ro

       Then: docker compose restart jenkins

       OR regenerate this Jenkinsfile for production use:

         fluid generate ci --system jenkins --out Jenkinsfile
         # (defaults to --install-mode pypi)

EOM
                        exit 2
                      fi
                      # Wipe any stale data-product-forge install from
                      # site-packages so its modules don't shadow the
                      # bind mount. PYTHONPATH-prepending normally wins
                      # over site-packages, but a leftover egg-info or
                      # namespace package fragment can confuse imports.
                      pip uninstall -y data-product-forge 2>/dev/null || true
                      echo "install-mode=dev-source — fluid imports will resolve from /forge-cli-src via PYTHONPATH"'''"""
        else:
            # Defensive: unknown install_mode. Caller passed something
            # we don't support — raise NOW (at generate time) rather
            # than emit a broken Jenkinsfile that confuses CI later.
            raise ValueError(
                f"Unknown install_mode {install_mode!r} — expected 'pypi' or 'dev-source'"
            )

        # PYTHONPATH differs per install mode:
        # - pypi: ``.`` (current workspace). fluid installed via pip,
        #   which places everything under site-packages — no need to
        #   add the bind mount.
        # - dev-source: ``/forge-cli-src`` (the bind mount). This lets
        #   ``import fluid_build`` resolve LIVE against the host source,
        #   bypassing pip's wheel cache + stale-file pitfalls. Every sh
        #   step in every stage inherits this (Jenkins expands
        #   ``environment {}`` as env vars for every sh invocation).
        if install_mode == "dev-source":
            pythonpath_value = "/forge-cli-src"
        else:
            pythonpath_value = "."

        # Install-mode-specific Jenkins parameters. pypi mode exposes
        # pip-install overrides (package spec, index URLs, prerelease
        # toggle) so operators can swap TestPyPI in without editing
        # Groovy. dev-source mode has no such overrides — it always
        # installs from the bind mount and fails loud if it's missing.
        if install_mode == "pypi":
            install_mode_parameters = """
        // ── Install overrides (pypi mode only) ──────────────────────
        // Default = stable PyPI, no prerelease. Override for pilot /
        // private-index / pinned-version builds.
        string(name: 'FLUID_PACKAGE_SPEC',
               defaultValue: 'data-product-forge',
               description: 'Package spec for pip. Pin a version via \\'data-product-forge==X.Y.Z\\'.')
        string(name: 'FLUID_PIP_INDEX_URL',
               defaultValue: '',
               description: 'Primary pip index. Leave blank for stable PyPI; set \\'https://test.pypi.org/simple/\\' for TestPyPI pilot builds, or your private mirror URL.')
        string(name: 'FLUID_PIP_EXTRA_INDEX_URL',
               defaultValue: '',
               description: 'Fallback pip index. Usually \\'https://pypi.org/simple/\\' when PRIMARY points at TestPyPI so transitive deps still resolve.')
        booleanParam(name: 'FLUID_ALLOW_PRERELEASE', defaultValue: false,
                     description: 'Pass pip --pre (pulls alpha/rc releases). Leave false in prod.')"""
        else:
            install_mode_parameters = ""

        # Parameter block — every stage gets a boolean toggle + per-stage
        # config. Operators trigger "Build with Parameters" in the Jenkins
        # UI to pick a subset of the 11-stage pipeline without editing Groovy.
        # Choice order + defaults match the HTML design doc (perfect-pipeline).
        parameters_block = f"""
    parameters {{
        // ── Global ──────────────────────────────────────────────────
        string(name: 'CONTRACT',  defaultValue: 'contract.fluid.yaml',
               description: 'Contract path relative to the workspace (or workdir when set).')
        string(name: 'FLUID_ENV', defaultValue: 'dev',
               description: 'Environment overlay (dev | staging | prod | ...).'){install_mode_parameters}

        // ── Stage 1 — bundle ────────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_1_BUNDLE',  defaultValue: true,
                     description: 'Stage 1: deterministic tgz bundle + MANIFEST.json (SHA-256).')
        choice(name: 'BUNDLE_FORMAT',
               choices: ['tgz', 'yaml-single-file'],
               description: 'Stage 1 output format.')

        // ── Stage 2 — validate ─────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_2_VALIDATE', defaultValue: true,
                     description: 'Stage 2: extension-routed validators (schema + sqlglot + openapi).')
        booleanParam(name: 'VALIDATE_STRICT',      defaultValue: true,
                     description: 'Stage 2: --strict (any validator error fails the pipeline).')

        // ── Stage 3 — generate artifacts ───────────────────────────
        booleanParam(name: 'RUN_STAGE_3_GENERATE_ARTIFACTS', defaultValue: {stage_3_enabled_default},
                     description: 'Stage 3: ODCS + ODPS-Bitol + schedule + policy fanout. Off for reference-only contracts.')
        string(name: 'GENERATE_EMIT',
               defaultValue: 'odcs,odps-bitol,schedule,policies',
               description: 'Stage 3 --emit list (comma-separated). dbt excluded by design (execution artifact).')

        // ── Stage 4 — validate artifacts ───────────────────────────
        booleanParam(name: 'RUN_STAGE_4_VALIDATE_ARTIFACTS', defaultValue: true,
                     description: 'Stage 4: re-verify MANIFEST SHA-256 + per-format schema validators.')

        // ── Stage 5 — diff (drift gate) ────────────────────────────
        booleanParam(name: 'RUN_STAGE_5_DIFF',  defaultValue: true,
                     description: 'Stage 5: compare contract vs live warehouse schema.')
        booleanParam(name: 'DIFF_EXIT_ON_DRIFT', defaultValue: true,
                     description: 'Stage 5: --exit-on-drift (hard-fail if drift detected).')

        // ── Stage 6 — plan ─────────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_6_PLAN', defaultValue: true,
                     description: 'Stage 6: compute DDL operations; emits bundleDigest + planDigest.')
        booleanParam(name: 'PLAN_HTML',        defaultValue: true,
                     description: 'Stage 6: emit HTML visualization of the plan.')

        // ── Stage 7 — apply ────────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_7_APPLY', defaultValue: true,
                     description: 'Stage 7: execute DDL (mode matrix; plan-binding cryptographically verified).')
        choice(name: 'APPLY_MODE',
               choices: ['dry-run', 'amend', 'create-only', 'amend-and-build', 'replace', 'replace-and-build'],
               description: 'Stage 7 mode. dry-run = render only (safe); amend = default additive; replace = DROP+CREATE (requires ALLOW_DATA_LOSS in non-dev).')
        string(name: 'APPLY_BUILD_ID', defaultValue: '',
               description: 'Stage 7: required for amend-and-build / replace-and-build (dbt build ID from contract builds[]).')
        booleanParam(name: 'ALLOW_DATA_LOSS', defaultValue: false,
                     description: 'Stage 7: gate waiver for --mode replace* in non-dev or when target has rows.')
        booleanParam(name: 'NO_VERIFY_DIGEST', defaultValue: false,
                     description: 'Stage 7: DR emergency escape — skip plan-binding verification. Use only when the original bundle is unreachable.')

        // ── Stage 8 — policy apply ─────────────────────────────────
        booleanParam(name: 'RUN_STAGE_8_POLICY_APPLY', defaultValue: true,
                     description: 'Stage 8: enforce IAM/GRANT bindings (self-gated on bindings.json presence).')
        choice(name: 'POLICY_APPLY_MODE',
               choices: ['enforce', 'check'],
               description: 'Stage 8: enforce = apply GRANTs; check = dry-run / PR report only.')

        // ── Stage 9 — verify ───────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_9_VERIFY', defaultValue: true,
                     description: 'Stage 9: post-apply reconciliation vs live warehouse.')
        booleanParam(name: 'VERIFY_STRICT',      defaultValue: true,
                     description: 'Stage 9: --strict (fail on any schema mismatch, including silent type coercions).')

        // ── Stage 10 — publish ─────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_10_PUBLISH', defaultValue: false,
                     description: 'Stage 10: push catalog artifacts to one or more targets. Opt-in — typically gated to main branch.')
        string(name: 'PUBLISH_TARGETS',
               defaultValue: 'datamesh-manager',
               description: 'Stage 10: space-separated publish targets (command-center datahub datamesh-manager collibra ...).')

        // ── Stage 11 — schedule sync (Path A) ──────────────────────
        booleanParam(name: 'RUN_STAGE_11_SCHEDULE_SYNC', defaultValue: false,
                     description: 'Stage 11: push generated DAGs to scheduler (airflow / mwaa / composer / astronomer / prefect / dagster).')
        choice(name: 'SCHEDULER',
               choices: ['', 'airflow', 'mwaa', 'composer', 'astronomer', 'prefect', 'dagster'],
               description: 'Stage 11 scheduler target. Blank = no-op.')
        string(name: 'SCHEDULER_DESTINATION',
               defaultValue: '',
               description: 'Stage 11: airflow/mwaa destination URL. Supports s3://, gs://, az://, ssh://, scp://, file:// or a bare path. Required for airflow + mwaa; ignored for composer / astronomer / prefect / dagster.')
        string(name: 'SCHEDULER_ENVIRONMENT_NAME',
               defaultValue: '',
               description: 'Stage 11: composer environment name or astronomer deployment name.')
        string(name: 'SCHEDULER_LOCATION',
               defaultValue: '',
               description: 'Stage 11: GCP region for composer (e.g. europe-west1, us-central1).')
        string(name: 'SCHEDULER_WORKSPACE',
               defaultValue: '',
               description: 'Stage 11: prefect workspace or dagster-cloud deployment name.')
        booleanParam(name: 'SCHEDULE_SYNC_DRY_RUN',
                     defaultValue: false,
                     description: 'Stage 11: --dry-run (log the planned subprocess argv without executing).')
    }}"""

        jenkins_pipeline = f"""
pipeline {{
    // Default to any available agent. Change to `label 'your-label'`
    // if you have a dedicated FLUID-equipped agent pool.
    agent any

    options {{
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }}
{parameters_block}

    environment {{
        FLUID_LOG_LEVEL = 'INFO'
        FLUID_CONFIG_PATH = './fluid_config'
        PYTHONPATH = '{pythonpath_value}'

        // ── Provider credential bindings (pick ONE pattern) ──────
        // See the top-of-file banner for the full env-var list per
        // provider.
        //
        // Path 1 — agent env passthrough. Set the env vars on the
        // Jenkins agent/container (docker-compose `environment:`,
        // Kubernetes agent template, or Jenkins Global Node
        // Properties). `sh` steps inherit them automatically; no
        // changes needed here.
        //
        // Path 2 — Jenkins credential store. After creating
        // `string` credentials in Jenkins, uncomment + adapt:
        //
        //   <PROVIDER_ENV_VAR> = credentials('<your-credential-id>')
        //
        // e.g. Snowflake:  SNOWFLAKE_ACCOUNT = credentials('snowflake-account')
        //      GCP:        GOOGLE_APPLICATION_CREDENTIALS = credentials('gcp-sa-key')
        //      AWS:        AWS_ACCESS_KEY_ID = credentials('aws-access-key')
        //                  AWS_SECRET_ACCESS_KEY = credentials('aws-secret-key')
        //
        // Catalog publish (only if using `fluid publish`):
        //   DMM_API_URL = credentials('dmm-api-url')
        //   DMM_API_KEY = credentials('dmm-api-key')
    }}

    stages {{
        stage('Setup [install-mode: {install_mode}]') {{
            steps {{
{setup_install_sh}
                sh '''{CD}fluid --version'''
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 1 — bundle (structural)
        // Deterministic .tgz + MANIFEST.json (SHA-256 merkle root).
        // Root of trust for every downstream stage.
        // ═════════════════════════════════════════════════════════════
        stage('1 · bundle') {{
            when {{ expression {{ return params.RUN_STAGE_1_BUNDLE }} }}
            steps {{
                sh '''{CD}mkdir -p runtime
                       fluid bundle "${{CONTRACT:-contract.fluid.yaml}}" --format "${{BUNDLE_FORMAT}}" --out runtime/bundle.tgz'''
                archiveArtifacts artifacts: '{P}runtime/bundle.tgz', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 2 — validate (structural)
        // Extension-routed: schema + sqlglot (SQL) + openapi-spec-validator.
        // Fail early, fail loud.
        // ═════════════════════════════════════════════════════════════
        stage('2 · validate') {{
            when {{ expression {{ return params.RUN_STAGE_2_VALIDATE }} }}
            environment {{
                VALIDATE_STRICT_FLAG = "${{params.VALIDATE_STRICT ? '--strict' : ''}}"
            }}
            steps {{
                sh '''{CD}fluid validate "${{CONTRACT:-contract.fluid.yaml}}" ${{VALIDATE_STRICT_FLAG}} \\
                           --report runtime/validate-report.json'''
                archiveArtifacts artifacts: '{P}runtime/validate-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 3 — generate artifacts (structural)
        // ODCS + ODPS-Bitol + schedule + policy fanout. dbt excluded.
        // Auto-skipped for hybrid-reference contracts.
        // ═════════════════════════════════════════════════════════════
        stage('3 · generate artifacts') {{
            when {{ expression {{ return params.RUN_STAGE_3_GENERATE_ARTIFACTS }} }}
            steps {{
                sh '''{CD}fluid generate artifacts "${{CONTRACT:-contract.fluid.yaml}}" \\
                         --out dist/artifacts/ \\
                         --emit "${{GENERATE_EMIT}}"'''
                archiveArtifacts artifacts: '{P}dist/artifacts/**/*', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 4 — validate artifacts (structural)
        // Re-verifies MANIFEST SHA-256 + per-format schema validators.
        // Defence-in-depth against in-flight CI tampering.
        // ═════════════════════════════════════════════════════════════
        stage('4 · validate artifacts') {{
            when {{ expression {{ return params.RUN_STAGE_4_VALIDATE_ARTIFACTS }} }}
            steps {{
                sh '''{CD}fluid validate-artifacts dist/artifacts/ \\
                         --manifest dist/artifacts/MANIFEST.json \\
                         --report runtime/validate-artifacts-report.json'''
                archiveArtifacts artifacts: '{P}runtime/validate-artifacts-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 5 — diff (drift gate)
        // Live warehouse vs contract. --exit-on-drift forces a human
        // decision before plan proceeds against a drifted baseline.
        // ═════════════════════════════════════════════════════════════
        stage('5 · diff (drift gate)') {{
            when {{ expression {{ return params.RUN_STAGE_5_DIFF }} }}
            environment {{
                DIFF_DRIFT_FLAG = "${{params.DIFF_EXIT_ON_DRIFT ? '--exit-on-drift' : ''}}"
            }}
            steps {{
                sh '''{CD}fluid diff "${{CONTRACT:-contract.fluid.yaml}}" ${{DIFF_DRIFT_FLAG}} \\
                           --env "${{FLUID_ENV:-dev}}" \\
                           --report runtime/diff-report.json'''
                archiveArtifacts artifacts: '{P}runtime/diff-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 6 — plan (structural)
        // DDL operations + plan.json with bundleDigest + planDigest.
        // Terraform-style "apply consumes exact plan" binding.
        // ═════════════════════════════════════════════════════════════
        stage('6 · plan') {{
            when {{ expression {{ return params.RUN_STAGE_6_PLAN }} }}
            environment {{
                PLAN_HTML_FLAG = "${{params.PLAN_HTML ? '--html' : ''}}"
            }}
            steps {{
                sh '''{CD}fluid plan "${{CONTRACT:-contract.fluid.yaml}}" \\
                           --out runtime/plan.json ${{PLAN_HTML_FLAG}} \\
                           --env "${{FLUID_ENV:-dev}}"'''
                archiveArtifacts artifacts: '{P}runtime/plan.json,{P}runtime/plan.html', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 7 — apply (structural)
        // Six-mode DDL matrix. Destructive modes (replace*) require
        // ALLOW_DATA_LOSS when FLUID_ENV != dev or target has rows.
        // ═════════════════════════════════════════════════════════════
        stage('7 · apply') {{
            when {{ expression {{ return params.RUN_STAGE_7_APPLY }} }}
            environment {{
                APPLY_BUILD_FLAG = "${{params.APPLY_BUILD_ID ? '--build ' + params.APPLY_BUILD_ID : ''}}"
                APPLY_LOSS_FLAG  = "${{params.ALLOW_DATA_LOSS ? '--allow-data-loss' : ''}}"
                APPLY_DIG_FLAG   = "${{params.NO_VERIFY_DIGEST ? '--no-verify-digest' : ''}}"
            }}
            steps {{
                sh '''{CD}fluid apply runtime/plan.json \\
                           --mode "${{APPLY_MODE}}" \\
                           ${{APPLY_BUILD_FLAG}} ${{APPLY_LOSS_FLAG}} ${{APPLY_DIG_FLAG}} \\
                           --env "${{FLUID_ENV:-dev}}" --yes \\
                           --report runtime/apply-report.html'''
                archiveArtifacts artifacts: '{P}runtime/apply-report.html', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 8 — policy apply (structural)
        // Enforces IAM/GRANT bindings. Runs AFTER apply (GRANTs need
        // target objects) and BEFORE verify (transform on under-authed
        // objects surfaces as policy failure, not masked build error).
        // Self-gated on dist/artifacts/policy/bindings.json existence.
        // ═════════════════════════════════════════════════════════════
        stage('8 · policy apply') {{
            when {{ expression {{ return params.RUN_STAGE_8_POLICY_APPLY }} }}
            steps {{
                sh '''{CD}if [ -f dist/artifacts/policy/bindings.json ]; then \\
                         fluid policy-apply dist/artifacts/policy/bindings.json \\
                           --mode "${{POLICY_APPLY_MODE}}" --env "${{FLUID_ENV:-dev}}" \\
                           --report runtime/policy-apply-report.json; \\
                       else echo "no dist/artifacts/policy/bindings.json — skipping stage 8"; fi'''
                archiveArtifacts artifacts: '{P}runtime/policy-apply-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 9 — verify (structural)
        // Post-apply reconciliation. Catches silent DDL coercions
        // (TIMESTAMP_NTZ → LTZ, Redshift length truncations, etc.).
        // ═════════════════════════════════════════════════════════════
        stage('9 · verify') {{
            when {{ expression {{ return params.RUN_STAGE_9_VERIFY }} }}
            environment {{
                VERIFY_STRICT_FLAG = "${{params.VERIFY_STRICT ? '--strict' : ''}}"
            }}
            steps {{
                sh '''{CD}fluid verify "${{CONTRACT:-contract.fluid.yaml}}" ${{VERIFY_STRICT_FLAG}} \\
                           --env "${{FLUID_ENV:-dev}}" \\
                           --report runtime/verify-report.json'''
                archiveArtifacts artifacts: '{P}runtime/verify-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 10 — publish (publication)
        // Multi-target catalog publisher. Push to CC / DMM / DataHub /
        // Collibra / Alation / marketplace / blob storage.
        // ═════════════════════════════════════════════════════════════
        stage('10 · publish') {{
            when {{ expression {{ return params.RUN_STAGE_10_PUBLISH }} }}
            steps {{
                // PUBLISH_TARGETS is a space-separated string; shell
                // iterates it word-split into a list of --target flags.
                sh '''{CD}TARGET_FLAGS=""; \\
                       for t in ${{PUBLISH_TARGETS}}; do \\
                         TARGET_FLAGS="${{TARGET_FLAGS}} --target $t"; \\
                       done; \\
                       fluid publish "${{CONTRACT:-contract.fluid.yaml}}" ${{TARGET_FLAGS}} \\
                         --env "${{FLUID_ENV:-dev}}"'''
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 11 — schedule sync (publication, Path A only)
        // Pushes generated DAGs to the scheduler's control plane.
        // Path B (EventBridge / MWAA / Snowflake Tasks) is applied in
        // Stage 7 via SchedulePlanner.
        // ═════════════════════════════════════════════════════════════
        stage('11 · schedule sync') {{
            when {{
                expression {{ return params.RUN_STAGE_11_SCHEDULE_SYNC && params.SCHEDULER?.trim() }}
            }}
            // Thread user-supplied params through the environment rather
            // than Groovy-interpolating them into the sh string. Jenkins
            // quotes env values safely; passing via the environment +
            // bash array construction below is injection-proof — a
            // malicious param value reaches our CLI as a single argv
            // token and is rejected there by _validate_destination /
            // _validate_safe_ident.
            environment {{
                SCHEDULER = "${{params.SCHEDULER}}"
                SCHEDULER_DESTINATION = "${{params.SCHEDULER_DESTINATION}}"
                SCHEDULER_ENVIRONMENT_NAME = "${{params.SCHEDULER_ENVIRONMENT_NAME}}"
                SCHEDULER_LOCATION = "${{params.SCHEDULER_LOCATION}}"
                SCHEDULER_WORKSPACE = "${{params.SCHEDULER_WORKSPACE}}"
                SCHEDULE_SYNC_DRY_RUN = "${{params.SCHEDULE_SYNC_DRY_RUN}}"
            }}
            steps {{
                // Use POSIX `set --` rather than bash arrays so this runs
                // under Jenkins's default `/bin/sh` invocation. Each $VAR
                // is quoted — one argv token per expansion — so a
                // malicious value stays a single token that our CLI then
                // rejects in _validate_destination / _validate_safe_ident.
                // Use if/then/fi rather than `[ ] && …` because the
                // `set -e` interaction with `&&` short-circuits is shell-
                // dependent and can trip on the first false test.
                sh '''{CD}set -eu
                    set -- --scheduler "$SCHEDULER" --dags-dir dist/artifacts/schedule/ --env "${{FLUID_ENV:-dev}}"
                    if [ -n "${{SCHEDULER_DESTINATION:-}}" ];      then set -- "$@" --destination "$SCHEDULER_DESTINATION"; fi
                    if [ -n "${{SCHEDULER_ENVIRONMENT_NAME:-}}" ]; then set -- "$@" --environment-name "$SCHEDULER_ENVIRONMENT_NAME"; fi
                    if [ -n "${{SCHEDULER_LOCATION:-}}" ];         then set -- "$@" --location "$SCHEDULER_LOCATION"; fi
                    if [ -n "${{SCHEDULER_WORKSPACE:-}}" ];        then set -- "$@" --workspace "$SCHEDULER_WORKSPACE"; fi
                    if [ "${{SCHEDULE_SYNC_DRY_RUN:-false}}" = "true" ]; then set -- "$@" --dry-run; fi
                    fluid schedule-sync "$@"'''
            }}
        }}
    }}

    post {{
        always {{
            cleanWs()
        }}
        success {{
            echo '✅ 11-stage pipeline completed successfully'
        }}
        failure {{
            echo '❌ 11-stage pipeline failed — check stage view for gate that fired'
        }}
        unstable {{
            echo '⚠ 11-stage pipeline unstable — some stages warned but did not hard-fail'
        }}
    }}
}}
"""

        banner = self._credential_banner(
            comment_prefix="// ",
            ci_system_name="Jenkinsfile",
            secret_surface_hint=(
                "Either (a) expose them as env vars on the Jenkins agent "
                "(docker-compose `environment:`, Kubernetes agent template, "
                "Jenkins Global Node Properties — sh steps inherit), or "
                "(b) create string credentials in Jenkins → Manage Credentials "
                "and bind them via the `credentials()` DSL inside the "
                "`environment {}` block."
            ),
        )
        return {"Jenkinsfile": banner + jenkins_pipeline}


class BitbucketTemplate(BasePipelineTemplate):
    """Bitbucket Pipelines template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "Bitbucket Pipelines"

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate Bitbucket pipeline"""

        commands = self._get_fluid_commands()

        pipeline = {
            "image": "python:3.12-slim",
            "definitions": {
                "steps": [
                    {
                        "step": {
                            "name": "Validate",
                            "script": [
                                "pip install --quiet data-product-forge",
                                commands["doctor"],
                                commands["validate"],
                            ],
                        }
                    },
                    {
                        "step": {
                            "name": "Plan",
                            "script": [commands["plan"]],
                            "artifacts": ["plan.json"],
                        }
                    },
                    {
                        "step": {
                            "name": "Test",
                            "script": [commands["test"], commands["contract_test"]],
                        }
                    },
                ]
            },
            "pipelines": {
                "branches": {"main": [{"step": "Validate"}, {"step": "Plan"}, {"step": "Test"}]}
            },
        }

        # Add deployment steps for each environment
        for env in config.environments:
            deploy_step = {
                "step": {
                    "name": f"Deploy to {env.upper()}",
                    "deployment": env,
                    "script": [
                        f"FLUID_ENV={env} {commands['apply']}",
                        f"FLUID_ENV={env} {commands['contract_test']}",
                    ],
                }
            }

            if env == "prod":
                deploy_step["step"]["trigger"] = "manual"

            pipeline["pipelines"]["branches"]["main"].append(deploy_step)

        # Airflow DAG sync — self-gates on AIRFLOW_DAGS_DEST env var.
        pipeline["pipelines"]["branches"]["main"].append(
            {"step": {"name": "Airflow DAG Sync", "script": [commands["airflow_sync"]]}}
        )

        # Add publishing step (catalog push + visualize + OPDS export)
        publish_step = {
            "step": {
                "name": "Publish",
                "script": [
                    commands["publish_catalog"],
                    commands["visualize"],
                    commands["publish_opds"],
                ],
                "artifacts": ["pipeline-viz.html", "dependency-graph.png", "opds-catalog.json"],
            }
        }

        if config.enable_marketplace_publishing:
            publish_step["step"]["script"].append(commands["marketplace_publish"])
            publish_step["step"]["trigger"] = "manual"

        pipeline["pipelines"]["branches"]["main"].append(publish_step)

        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="Bitbucket Pipelines",
            secret_surface_hint=(
                "Repository Settings → Repository Variables (or Deployment "
                "Variables for env-scoped secrets). Secured variables are "
                "auto-injected as env vars for every step."
            ),
        )
        return {"bitbucket-pipelines.yml": banner + yaml.dump(pipeline, indent=2)}


class CircleCITemplate(BasePipelineTemplate):
    """CircleCI pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "CircleCI"

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate CircleCI pipeline"""

        commands = self._get_fluid_commands()

        pipeline = {
            "version": 2.1,
            "executors": {
                "python-executor": {
                    "docker": [{"image": "python:3.12-slim"}],
                    "working_directory": "~/project",
                }
            },
            "jobs": {
                "validate": {
                    "executor": "python-executor",
                    "steps": [
                        "checkout",
                        {"run": "pip install --quiet data-product-forge"},
                        {"run": {"name": "FLUID Doctor", "command": commands["doctor"]}},
                        {"run": {"name": "Validate", "command": commands["validate"]}},
                    ],
                },
                "plan": {
                    "executor": "python-executor",
                    "steps": [
                        "checkout",
                        {"run": "pip install --quiet data-product-forge"},
                        {"run": {"name": "Generate Plan", "command": commands["plan"]}},
                        {"persist_to_workspace": {"root": ".", "paths": ["plan.json"]}},
                    ],
                },
                "test": {
                    "executor": "python-executor",
                    "steps": [
                        "checkout",
                        {"run": "pip install --quiet data-product-forge"},
                        {"run": {"name": "Run Tests", "command": commands["test"]}},
                        {"store_test_results": {"path": "test-results"}},
                    ],
                },
            },
            "workflows": {
                "fluid-pipeline": {
                    "jobs": ["validate", "plan", {"test": {"requires": ["validate"]}}]
                }
            },
        }

        # Add deployment jobs
        for env in config.environments:
            job_name = f"deploy-{env}"

            deploy_job = {
                "executor": "python-executor",
                "steps": [
                    "checkout",
                    {"attach_workspace": {"at": "."}},
                    {"run": "pip install --quiet data-product-forge"},
                    {
                        "run": {
                            "name": f"Deploy to {env}",
                            "command": f"FLUID_ENV={env} {commands['apply']}",
                        }
                    },
                    {
                        "run": {
                            "name": "Contract Tests",
                            "command": f"FLUID_ENV={env} {commands['contract_test']}",
                        }
                    },
                ],
            }

            pipeline["jobs"][job_name] = deploy_job

            # Add to workflow with dependencies
            workflow_job = {job_name: {"requires": ["plan", "test"]}}

            if env == "prod":
                workflow_job[job_name]["filters"] = {"branches": {"only": "main"}}
                workflow_job[job_name]["type"] = "approval"

            pipeline["workflows"]["fluid-pipeline"]["jobs"].append(workflow_job)

        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="CircleCI",
            secret_surface_hint=(
                "Project Settings → Environment Variables (per-project) or "
                "Organization Settings → Contexts (reusable across projects). "
                "Both auto-inject as env for every step."
            ),
        )
        return {".circleci/config.yml": banner + yaml.dump(pipeline, indent=2)}


class TektonTemplate(BasePipelineTemplate):
    """Tekton pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "Tekton"

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate Tekton pipeline"""

        commands = self._get_fluid_commands()

        # Tekton pipeline definition
        pipeline = {
            "apiVersion": "tekton.dev/v1beta1",
            "kind": "Pipeline",
            "metadata": {"name": "fluid-dataops-pipeline"},
            "spec": {
                "workspaces": [{"name": "shared-data"}],
                "tasks": [
                    {
                        "name": "validate",
                        "taskRef": {"name": "fluid-validate"},
                        "workspaces": [{"name": "source", "workspace": "shared-data"}],
                    },
                    {
                        "name": "plan",
                        "taskRef": {"name": "fluid-plan"},
                        "workspaces": [{"name": "source", "workspace": "shared-data"}],
                        "runAfter": ["validate"],
                    },
                    {
                        "name": "test",
                        "taskRef": {"name": "fluid-test"},
                        "workspaces": [{"name": "source", "workspace": "shared-data"}],
                        "runAfter": ["validate"],
                    },
                ],
            },
        }

        # Add deployment tasks
        for env in config.environments:
            deploy_task = {
                "name": f"deploy-{env}",
                "taskRef": {"name": "fluid-deploy"},
                "params": [{"name": "environment", "value": env}],
                "workspaces": [{"name": "source", "workspace": "shared-data"}],
                "runAfter": ["plan", "test"],
            }

            pipeline["spec"]["tasks"].append(deploy_task)

        # Task definitions
        task_definitions = []

        # Validate task
        validate_task = {
            "apiVersion": "tekton.dev/v1beta1",
            "kind": "Task",
            "metadata": {"name": "fluid-validate"},
            "spec": {
                "workspaces": [{"name": "source"}],
                "steps": [
                    {
                        "name": "validate",
                        "image": "python:3.12-slim",
                        "workingDir": "$(workspaces.source.path)",
                        "script": f"""#!/bin/bash
pip install --quiet data-product-forge
{commands["doctor"]}
{commands["validate"]}
""",
                    }
                ],
            },
        }

        task_definitions.append(validate_task)

        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="Tekton",
            secret_surface_hint=(
                "Kubernetes Secrets referenced via `envFrom: - secretRef:` on "
                "each Task (or `volumeMounts` for key-files like "
                "GOOGLE_APPLICATION_CREDENTIALS). Create the Secrets in the "
                "same namespace as the PipelineRun."
            ),
        )
        files = {
            "tekton/pipeline.yaml": banner + yaml.dump(pipeline, indent=2),
            "tekton/tasks.yaml": banner + yaml.dump_all(task_definitions, indent=2),
        }

        return files


# Main pipeline generator function
def generate_pipeline_template(
    provider: str,
    complexity: str = "standard",
    environments: List[str] = None,
    enable_marketplace: bool = False,
    oidc_provider: Optional[str] = None,
) -> Dict[str, str]:
    """
    Generate pipeline template for specified provider

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


if __name__ == "__main__":
    # Demo the pipeline generator
    cprint("FLUID Dynamic DataOps Pipeline Templates")
    cprint("=" * 50)

    generator = PipelineTemplateGenerator()

    cprint(f"Available providers: {generator.list_available_providers()}")

    # Generate a sample GitHub Actions pipeline
    config = PipelineConfig(
        provider=PipelineProvider.GITHUB_ACTIONS,
        complexity=PipelineComplexity.STANDARD,
        environments=["dev", "staging", "prod"],
        enable_marketplace_publishing=True,
    )

    files = generator.generate_pipeline(config)

    cprint(f"\nGenerated {len(files)} pipeline files:")
    for filename, content in files.items():
        cprint(f"  - {filename} ({len(content)} characters)")

    cprint("\nSample file content preview:")
    first_file = list(files.items())[0]
    cprint(f"\n{first_file[0]}:")
    cprint("-" * 40)
    cprint(first_file[1][:500] + "..." if len(first_file[1]) > 500 else first_file[1])
