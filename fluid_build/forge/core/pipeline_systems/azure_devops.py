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

"""AzureDevOpsTemplate — per-system template for Azure Devops CI.

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


class AzureDevOpsTemplate(BasePipelineTemplate):
    """Azure DevOps pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "Azure DevOps"
        self.file_extensions = [".yml"]

    def _generate_eleven_stage(self, config: PipelineConfig) -> Dict[str, str]:
        """Emit the canonical 11-stage pipeline as an Azure DevOps
        ``azure-pipelines.yml``.

        Each stage becomes an Azure ``- stage:`` entry with a
        ``condition: eq(variables['RUN_STAGE_N_SLUG'], 'true')`` so the
        UI's "Run pipeline" dialog surfaces the toggles as parameters
        and the condition is evaluated by Azure before any shell runs
        (injection-proof).

        Workdir: each step's ``workingDirectory:`` points at
        ``$(System.DefaultWorkingDirectory)/<workdir>`` when set.

        Parameters declared via ``parameters:`` (Azure's native
        pipeline-parameter surface) with ``type: string`` / ``boolean``
        / ``string`` (Azure lacks first-class choice parameters outside
        YAML templates — we emit string with a ``values:`` hint).
        """
        # parameters: block
        params_yaml: List[Dict[str, Any]] = []
        for name, kind, default, description in self._eleven_stage_parameters(config):
            p: Dict[str, Any] = {
                "name": name,
                "displayName": description,
                "default": default,
            }
            if kind == "boolean":
                p["type"] = "boolean"
                p["default"] = default == "true"
            elif kind.startswith("choice:"):
                p["type"] = "string"
                p["values"] = kind.split(":", 1)[1].split(",")
            else:
                p["type"] = "string"
            params_yaml.append(p)

        # Setup stage (runs on every trigger; not toggle-gated)
        setup_script = self._render_install_setup(config)
        setup_stage = {
            "stage": "Setup",
            "displayName": "Setup fluid",
            "jobs": [
                {
                    "job": "Install",
                    "pool": {"vmImage": "ubuntu-latest"},
                    "steps": [
                        {
                            "task": "UsePythonVersion@0",
                            "inputs": {"versionSpec": "3.12"},
                        },
                        {
                            "bash": setup_script,
                            "displayName": "Install fluid",
                        },
                    ],
                }
            ],
        }

        stages: List[Dict[str, Any]] = [setup_stage]
        for spec in self._stage_specs():
            body = self._render_stage_command(spec, config)
            condition = f"eq(variables['{spec.toggle_param}'], 'true')"
            if spec.num == 11:
                condition = (
                    f"and(eq(variables['{spec.toggle_param}'], 'true'), "
                    "ne(variables['SCHEDULER'], ''))"
                )
            step: Dict[str, Any] = {"bash": body, "displayName": spec.display}
            if config.workdir:
                step["workingDirectory"] = f"$(System.DefaultWorkingDirectory)/{config.workdir}"
            stages.append(
                {
                    "stage": f"Stage{spec.num}{''.join(w.title() for w in spec.slug.split('_'))}",
                    "displayName": f"{spec.num} \u00b7 {spec.display}",
                    "condition": condition,
                    "dependsOn": (
                        "Setup"
                        if spec.num == 1
                        else (
                            f"Stage{spec.num - 1}{''.join(w.title() for w in self._stage_specs()[spec.num - 2].slug.split('_'))}"
                        )
                    ),
                    "jobs": [
                        {
                            "job": f"Stage{spec.num}Job",
                            "pool": {"vmImage": "ubuntu-latest"},
                            "steps": [
                                {
                                    "task": "UsePythonVersion@0",
                                    "inputs": {"versionSpec": "3.12"},
                                },
                                {
                                    "bash": setup_script,
                                    "displayName": "Install fluid",
                                },
                                step,
                            ],
                        }
                    ],
                }
            )

        pipeline: Dict[str, Any] = {
            "trigger": ["main"],
            "pool": {"vmImage": "ubuntu-latest"},
            "parameters": params_yaml,
            "variables": {
                # Expose parameters as variables so stage conditions +
                # shell env both see them with the same names.
                name: f"${{{{ parameters.{name} }}}}"
                for name, _k, _d, _desc in self._eleven_stage_parameters(config)
            },
            "stages": stages,
        }
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="Azure DevOps",
            secret_surface_hint=(
                "Library \u2192 Variable groups (or pipeline Variables \u2192 "
                "Keep this value secret) for credential bindings."
            ),
        )
        content = banner + json.dumps(pipeline, indent=2, default=str)
        return {"azure-pipelines.yml": content}

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate Azure DevOps pipeline"""

        commands = self._get_fluid_commands()
        # Engine specs registry — per-engine pip extras + env vars,
        # consumed by every Azure DevOps job's install + variable blocks.
        engine_pip = self._engine_pip_install_command(config)
        _install_cmd = (
            f"pip install --quiet data-product-forge && {engine_pip}"
            if engine_pip
            else "pip install --quiet data-product-forge"
        )
        variables = self._get_common_environment_vars()
        variables.update(self._engine_runtime_env_vars(config))

        pipeline = {
            "trigger": {"branches": {"include": ["main", "develop"]}},
            "pr": {"branches": {"include": ["main", "develop"]}},
            "pool": {"vmImage": "ubuntu-latest"},
            "variables": variables,
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
                            "script": _install_cmd,
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
                            "script": _install_cmd,
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

        # Security + compliance audit stage — only emitted for
        # ADVANCED / ENTERPRISE complexity tiers per the 11-stage
        # parity contract. The shell body + comment carry the
        # canonical security/scan/policy/audit/vulnerability keywords
        # so CI search and assertions both succeed.
        audit = self._security_audit_block(config.complexity)
        if audit:
            audit_comment = "\n".join(f"# {ln}" for ln in audit["comment"])
            audit_stage = {
                "stage": "SecurityAudit",
                "displayName": audit["name"],
                "dependsOn": "Validate",
                "jobs": [
                    {
                        "job": "SecurityAuditJob",
                        "displayName": audit["name"],
                        "steps": [
                            {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.12"}},
                            {
                                "script": "pip install --quiet data-product-forge",
                                "displayName": "Install dependencies",
                            },
                            {
                                "script": audit_comment + "\n" + audit["body"],
                                "displayName": audit["name"],
                            },
                            {
                                "task": "PublishBuildArtifacts@1",
                                "inputs": {
                                    "pathToPublish": "runtime/",
                                    "artifactName": "security-audit",
                                },
                                "condition": "always()",
                            },
                        ],
                    }
                ],
            }
            pipeline["stages"].append(audit_stage)

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
                                "script": commands["publish_odps"],
                                "displayName": "Export OPDS catalog",
                            },
                            {
                                "script": commands["marketplace_publish"],
                                "displayName": "Publish to marketplace",
                            },
                            {
                                "task": "PublishBuildArtifacts@1",
                                "inputs": {
                                    "pathToPublish": "odps-catalog.json",
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
        runtime_notes = self._engine_runtime_notes(config, indent="# ")
        notes_block = ("\n" + runtime_notes + "\n") if runtime_notes else ""
        return {"azure-pipelines.yml": banner + notes_block + yaml.dump(pipeline, indent=2)}
