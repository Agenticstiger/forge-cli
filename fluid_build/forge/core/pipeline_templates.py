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
        """Get standard FLUID commands for different stages"""
        return {
            "validate": "fluid validate --strict",
            "plan": "fluid plan --output plan.json",
            "apply": "fluid apply --plan plan.json",
            "test": "fluid test --coverage",
            "contract_test": "fluid contract-test --all",
            "generate_transformation": "fluid generate transformation",
            "generate_schedule": "fluid generate schedule",
            "check_transformations": (
                "if [ -f dbt_project.yml ] || [ -d models/ ]; then "
                "fluid generate transformation --check; "
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
            "doctor": "fluid doctor --extended",
        }

    def _get_common_environment_vars(self) -> Dict[str, str]:
        """Get common environment variables"""
        return {
            "FLUID_LOG_LEVEL": "INFO",
            "FLUID_CONFIG_PATH": "./fluid_config",
            "PYTHONPATH": ".",
            "PIP_CACHE_DIR": ".pip-cache",
        }

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
                        {"name": "Install FLUID", "run": "pip install -r requirements.txt"},
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

        files = {".github/workflows/fluid-pipeline.yml": yaml.dump(workflow, indent=2)}
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
                        {"name": "Install Dependencies", "run": "pip install -r requirements.txt"},
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
                        {"name": "Install Dependencies", "run": "pip install -r requirements.txt"},
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
                        {"name": "Install Dependencies", "run": "pip install -r requirements.txt"},
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
                        {"name": "Install Dependencies", "run": "pip install -r requirements.txt"},
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
                {"name": "Install Dependencies", "run": "pip install -r requirements.txt"},
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
                {
                    "name": "Run Contract Tests",
                    "run": f"FLUID_ENV={env} {commands['contract_test']}",
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

        files = {".github/workflows/fluid-standard.yml": yaml.dump(workflow, indent=2)}
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

        return {".gitlab-ci.yml": yaml.dump(pipeline, indent=2)}

    def _generate_basic_gitlab_pipeline(self, config, commands, env_vars):
        """Generate basic GitLab CI pipeline"""

        return {
            "stages": ["validate", "generate", "plan", "apply", "test", "publish"],
            "variables": env_vars,
            "image": "python:3.9",
            "before_script": ["pip install -r requirements.txt"],
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
            "publish": {
                "stage": "publish",
                "script": [commands["visualize"], commands["publish_opds"]],
                "artifacts": {
                    "paths": ["pipeline-viz.html", "dependency-graph.png", "opds-catalog.json"],
                    "expire_in": "30 days",
                },
                "only": ["main"],
            },
        }

    def _generate_standard_gitlab_pipeline(self, config, commands, env_vars):
        """Generate standard GitLab CI pipeline with environments"""

        pipeline = {
            "stages": ["validate", "generate", "test", "plan", "deploy", "publish"],
            "variables": env_vars,
            "image": "python:3.9",
            "before_script": ["pip install -r requirements.txt"],
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

        # Add publishing job
        pipeline["publish"] = {
            "stage": "publish",
            "script": [commands["visualize"], commands["publish_opds"]],
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
                        {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.9"}},
                        {
                            "script": "pip install -r requirements.txt",
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
                        {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.9"}},
                        {
                            "script": "pip install -r requirements.txt",
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
                                            "inputs": {"versionSpec": "3.9"},
                                        },
                                        {
                                            "script": "pip install -r requirements.txt",
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

        # Publishing stage
        if config.enable_marketplace_publishing:
            publish_stage = {
                "stage": "Publish",
                "displayName": "Publish Artifacts",
                "dependsOn": f"Deploy{config.environments[-1].title()}",
                "condition": "and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/main'))",
                "jobs": [
                    {
                        "job": "PublishJob",
                        "displayName": "Publish to Marketplace",
                        "steps": [
                            {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.9"}},
                            {
                                "script": "pip install -r requirements.txt",
                                "displayName": "Install dependencies",
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

        return {"azure-pipelines.yml": yaml.dump(pipeline, indent=2)}


class JenkinsTemplate(BasePipelineTemplate):
    """Jenkins pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "Jenkins"
        self.file_extensions = [".groovy"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate Jenkins pipeline"""

        commands = self._get_fluid_commands()

        jenkins_pipeline = f"""
pipeline {{
    agent {{ label 'fluid' }}

    environment {{
        FLUID_LOG_LEVEL = 'INFO'
        FLUID_CONFIG_PATH = './fluid_config'
        PYTHONPATH = '.'
        // Bind credentials from Jenkins credential store.
        // Configure 'fluid-provider-credentials' in Jenkins > Manage Credentials.
        PROVIDER_CREDS = credentials('fluid-provider-credentials')
    }}
    
    triggers {{
        pollSCM('H/5 * * * *')  // Poll every 5 minutes
    }}
    
    stages {{
        stage('Setup') {{
            steps {{
                sh 'pip install -r requirements.txt'
            }}
        }}
        
        stage('Validate') {{
            parallel {{
                stage('FLUID Doctor') {{
                    steps {{
                        sh '{commands["doctor"]}'
                    }}
                }}
                stage('Configuration Validation') {{
                    steps {{
                        sh '{commands["validate"]}'
                    }}
                }}
            }}
        }}
        
        stage('Generate Artifacts') {{
            parallel {{
                stage('Transformations') {{
                    steps {{
                        sh '{commands["generate_transformation"]}'
                    }}
                }}
                stage('Schedules') {{
                    steps {{
                        sh '{commands["generate_schedule"]}'
                    }}
                }}
            }}
        }}

        stage('Plan') {{
            steps {{
                sh '{commands["plan"]}'
                archiveArtifacts artifacts: 'plan.json', fingerprint: true
            }}
        }}
        
        stage('Test') {{
            parallel {{
                stage('Unit Tests') {{
                    steps {{
                        sh 'fluid test --type unit --output test-results-unit.xml'
                    }}
                    post {{
                        always {{
                            junit 'test-results-unit.xml'
                        }}
                    }}
                }}
                stage('Integration Tests') {{
                    steps {{
                        sh 'fluid test --type integration --output test-results-integration.xml'
                    }}
                    post {{
                        always {{
                            junit 'test-results-integration.xml'
                        }}
                    }}
                }}
            }}
        }}
"""

        # Add deployment stages
        for env in config.environments:
            approval = ""
            when_condition = ""

            if env == "prod":
                approval = """
                input {
                    message "Deploy to production?"
                    ok "Deploy"
                    parameters {
                        choice(name: 'DEPLOY_ACTION', choices: ['Deploy', 'Skip'], description: 'Choose deployment action')
                    }
                }"""
                when_condition = "when { branch 'main' }"
            elif env == "staging":
                when_condition = "when { anyOf { branch 'main'; branch 'develop' } }"

            jenkins_pipeline += f"""
        stage('Deploy to {env.upper()}') {{
            {when_condition}
            steps {{{approval}
                sh 'FLUID_ENV={env} {commands["apply"]}'
                sh 'FLUID_ENV={env} {commands["contract_test"]}'
            }}
            post {{
                success {{
                    echo 'Deployment to {env} successful'
                }}
                failure {{
                    echo 'Deployment to {env} failed'
                }}
            }}
        }}
"""

        # Add publishing stage
        jenkins_pipeline += f"""
        stage('Publish') {{
            when {{ branch 'main' }}
            steps {{
                sh '{commands["visualize"]}'
                sh '{commands["publish_opds"]}'
                archiveArtifacts artifacts: 'pipeline-viz.html,dependency-graph.png,opds-catalog.json', fingerprint: true
"""

        if config.enable_marketplace_publishing:
            jenkins_pipeline += f"""
                sh '{commands["marketplace_publish"]}'
"""

        jenkins_pipeline += """
            }
        }
    }
    
    post {
        always {
            cleanWs()
        }
        success {
            echo 'Pipeline completed successfully!'
        }
        failure {
            echo 'Pipeline failed!'
        }
    }
}
"""

        return {"Jenkinsfile": jenkins_pipeline}


class BitbucketTemplate(BasePipelineTemplate):
    """Bitbucket Pipelines template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "Bitbucket Pipelines"

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate Bitbucket pipeline"""

        commands = self._get_fluid_commands()

        pipeline = {
            "image": "python:3.9",
            "definitions": {
                "steps": [
                    {
                        "step": {
                            "name": "Validate",
                            "script": [
                                "pip install -r requirements.txt",
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

        # Add publishing step
        publish_step = {
            "step": {
                "name": "Publish",
                "script": [commands["visualize"], commands["publish_opds"]],
                "artifacts": ["pipeline-viz.html", "dependency-graph.png", "opds-catalog.json"],
            }
        }

        if config.enable_marketplace_publishing:
            publish_step["step"]["script"].append(commands["marketplace_publish"])
            publish_step["step"]["trigger"] = "manual"

        pipeline["pipelines"]["branches"]["main"].append(publish_step)

        return {"bitbucket-pipelines.yml": yaml.dump(pipeline, indent=2)}


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
                    "docker": [{"image": "python:3.9"}],
                    "working_directory": "~/project",
                }
            },
            "jobs": {
                "validate": {
                    "executor": "python-executor",
                    "steps": [
                        "checkout",
                        {"run": "pip install -r requirements.txt"},
                        {"run": {"name": "FLUID Doctor", "command": commands["doctor"]}},
                        {"run": {"name": "Validate", "command": commands["validate"]}},
                    ],
                },
                "plan": {
                    "executor": "python-executor",
                    "steps": [
                        "checkout",
                        {"run": "pip install -r requirements.txt"},
                        {"run": {"name": "Generate Plan", "command": commands["plan"]}},
                        {"persist_to_workspace": {"root": ".", "paths": ["plan.json"]}},
                    ],
                },
                "test": {
                    "executor": "python-executor",
                    "steps": [
                        "checkout",
                        {"run": "pip install -r requirements.txt"},
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
                    {"run": "pip install -r requirements.txt"},
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

        return {".circleci/config.yml": yaml.dump(pipeline, indent=2)}


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
                        "image": "python:3.9",
                        "workingDir": "$(workspaces.source.path)",
                        "script": f"""#!/bin/bash
pip install -r requirements.txt
{commands["doctor"]}
{commands["validate"]}
""",
                    }
                ],
            },
        }

        task_definitions.append(validate_task)

        files = {
            "tekton/pipeline.yaml": yaml.dump(pipeline, indent=2),
            "tekton/tasks.yaml": yaml.dump_all(task_definitions, indent=2),
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
