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

"""GitHubActionsTemplate — per-system template for Github Actions CI.

Extracted from the monolithic ``pipeline_templates.py`` so each CI system's
quirks stay contained. Inherits the 11-stage rendering scaffold from
:class:`fluid_build.forge.core.pipeline_systems._base.BasePipelineTemplate`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

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

    yaml = _YamlFallback()  # type: ignore[assignment]

from ._base import (
    PINNED_ACTIONS,
    BasePipelineTemplate,
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    StageSpec,
    _pin_action,
)


class GitHubActionsTemplate(BasePipelineTemplate):
    """GitHub Actions pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "GitHub Actions"
        self.file_extensions = [".yml", ".yaml"]

    def _generate_eleven_stage(self, config: PipelineConfig) -> Dict[str, str]:
        """Emit the canonical 11-stage pipeline as a GitHub Actions workflow.

        Single-file output (``.github/workflows/fluid-pipeline.yml``)
        with ``workflow_dispatch.inputs`` for every build parameter and
        one ``steps:`` entry per stage. Stage toggles map to
        ``if: ${{ inputs.run_stage_N_slug }}`` which — because GitHub
        Actions evaluates ``inputs`` at workflow-load time, not at
        shell-eval time — is injection-proof: a malicious input value
        can't break out of the conditional or affect downstream steps.

        Workdir handling: when ``config.workdir`` is set, every step
        inherits ``defaults.run.working-directory`` at the job level
        (GitHub Actions' native primitive), and each stage body is
        additionally prefixed with ``cd "<workdir>" && `` via the
        shared ``_render_stage_command`` helper (defence-in-depth —
        the job-level default covers raw ``run:`` scripts; the body
        prefix covers the case where a step uses the shell directly).

        Parameters threaded via env vars at job level (NOT interpolated
        into ``run:`` bodies) so stage 7's APPLY_BUILD_ID can't smuggle
        argv tokens. Matches the Jenkins stage-7 + stage-11 pattern.
        """
        env_vars = [
            "CONTRACT",
            "FLUID_ENV",
            "APPLY_MODE",
            "APPLY_BUILD_ID",
            "ALLOW_DATA_LOSS",
            "NO_VERIFY_DIGEST",
            "PUBLISH_TARGETS",
            "SCHEDULER",
            "SCHEDULER_DESTINATION",
            "SCHEDULER_ENVIRONMENT_NAME",
            "SCHEDULER_LOCATION",
            "SCHEDULER_WORKSPACE",
            "SCHEDULE_SYNC_DRY_RUN",
            "FLUID_PACKAGE_SPEC",
            "FLUID_PIP_INDEX_URL",
            "FLUID_PIP_EXTRA_INDEX_URL",
            "FLUID_ALLOW_PRERELEASE",
        ]
        # Build workflow_dispatch.inputs block
        inputs: Dict[str, Dict[str, Any]] = {}
        for name, kind, default, description in self._eleven_stage_parameters(config):
            # GitHub Actions naming: lower-snake for inputs
            key = name.lower()
            if kind.startswith("choice:"):
                options = kind.split(":", 1)[1].split(",")
                inputs[key] = {
                    "type": "choice",
                    "options": options,
                    "default": default,
                    "description": description,
                }
            elif kind == "boolean":
                inputs[key] = {
                    "type": "boolean",
                    "default": default == "true",
                    "description": description,
                }
            else:
                inputs[key] = {
                    "type": "string",
                    "default": default,
                    "description": description,
                }
        # Build job env: from inputs (one-to-one mapping so stage
        # sh bodies read $VAR instead of ${{ inputs.var }}).
        job_env: Dict[str, str] = {}
        for var in env_vars:
            job_env[var] = "${{ inputs." + var.lower() + " }}"
        # Steps: checkout → setup → 11 stages
        steps: List[Dict[str, Any]] = [
            {"name": "Checkout", "uses": _pin_action("actions/checkout@v4")},
            {
                "name": "Setup Python",
                "uses": _pin_action("actions/setup-python@v5"),
                "with": {"python-version": "3.12"},
            },
            {
                "name": "Setup fluid (install + verify)",
                "run": self._render_install_setup(config),
                "shell": "bash",
            },
        ]
        for spec in self._stage_specs():
            stage_body = self._render_stage_command(spec, config)
            step: Dict[str, Any] = {
                "name": f"{spec.num} \u00b7 {spec.display}",
                # ``inputs.run_stage_N_slug`` is a workflow_dispatch
                # input set from the UI; GitHub evaluates it before
                # running the step, so a malicious value can't
                # short-circuit into shell context.
                "if": "${{ inputs." + spec.toggle_param.lower() + " }}",
                "run": stage_body,
                "shell": "bash",
            }
            # Stage 11 requires a non-blank SCHEDULER; gate accordingly.
            if spec.num == 11:
                step["if"] = (
                    "${{ inputs." + spec.toggle_param.lower() + " && inputs.scheduler != '' }}"
                )
            steps.append(step)
        workflow: Dict[str, Any] = {
            "name": "fluid 11-stage pipeline",
            "on": {"workflow_dispatch": {"inputs": inputs}},
            "jobs": {
                "pipeline": {
                    "runs-on": "ubuntu-latest",
                    "env": job_env,
                    "steps": steps,
                }
            },
        }
        # Apply workdir via defaults.run.working-directory.
        if config.workdir:
            workflow["jobs"]["pipeline"]["defaults"] = {
                "run": {"working-directory": config.workdir}
            }
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="GitHub Actions",
            secret_surface_hint=(
                "Settings \u2192 Secrets and variables \u2192 Actions. "
                "Map each secret to a job-level env: block."
            ),
        )
        content = banner + json.dumps(workflow, indent=2, default=str)
        # json.dumps produces valid YAML (JSON is a subset). Keep it
        # this way — no yaml.dump dependency, no anchor/alias surprises.
        return {".github/workflows/fluid-pipeline.yml": content}

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
        # Engine specs registry — per-engine pip extras + runtime env
        # vars sourced from ``_engine_specs.py`` so adding a new engine
        # doesn't require editing this file.
        env_vars.update(self._engine_runtime_env_vars(config))
        engine_pip = self._engine_pip_install_command(config)
        engine_install_step = (
            [{"name": "Install engine extras", "run": engine_pip}] if engine_pip else []
        )

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
                            "with": {"python-version": "3.10"},
                        },
                        {"name": "Install FLUID", "run": "pip install --quiet data-product-forge"},
                        *engine_install_step,
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
        runtime_notes = self._engine_runtime_notes(config, indent="# ")
        notes_block = ("\n" + runtime_notes + "\n") if runtime_notes else ""
        files = {
            ".github/workflows/fluid-pipeline.yml": banner
            + notes_block
            + yaml.dump(workflow, indent=2)
        }
        files[".env.ci.example"] = self._generate_env_ci_example(config.oidc_provider)
        return files

    def _generate_standard_workflow(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate standard GitHub Actions workflow with multiple environments"""

        commands = self._get_fluid_commands()
        env_vars = self._get_common_environment_vars()
        env_vars.update(self._engine_runtime_env_vars(config))
        _install_cmd = self._install_command(config)

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
                            "with": {"python-version": "3.10"},
                        },
                        {
                            "name": "Install Dependencies",
                            "run": _install_cmd,
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
                            "with": {"python-version": "3.10"},
                        },
                        {
                            "name": "Install Dependencies",
                            "run": _install_cmd,
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
                            "with": {"python-version": "3.10"},
                        },
                        {
                            "name": "Install Dependencies",
                            "run": _install_cmd,
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
                            "with": {"python-version": "3.10"},
                        },
                        {
                            "name": "Install Dependencies",
                            "run": _install_cmd,
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
                    "with": {"python-version": "3.10"},
                },
                {"name": "Install Dependencies", "run": _install_cmd},
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
        runtime_notes = self._engine_runtime_notes(config, indent="# ")
        notes_block = ("\n" + runtime_notes + "\n") if runtime_notes else ""
        files = {
            ".github/workflows/fluid-standard.yml": banner
            + notes_block
            + yaml.dump(workflow, indent=2)
        }
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
