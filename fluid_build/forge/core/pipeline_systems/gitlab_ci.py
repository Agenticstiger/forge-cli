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

"""GitLabCITemplate — per-system template for Gitlab Ci CI.

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


class GitLabCITemplate(BasePipelineTemplate):
    """GitLab CI pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "GitLab CI"
        self.file_extensions = [".yml"]

    def _generate_eleven_stage(self, config: PipelineConfig) -> Dict[str, str]:
        """Emit the canonical 11-stage pipeline as a ``.gitlab-ci.yml``.

        Each stage becomes a separate job in its own pipeline stage so
        they run sequentially (GitLab parallelizes jobs within the same
        stage). ``rules:`` with ``$RUN_STAGE_N_<SLUG> == "true"``
        implements the per-stage toggle — GitLab evaluates ``rules:``
        before any shell runs, so the gate is injection-proof.

        Workdir: ``default.before_script`` prepends ``cd "$CI_WORKDIR"``
        when set, so every job step runs in the contract folder.
        Stage-body commands ALSO carry a ``cd`` prefix as defence-in-
        depth (same pattern as GitHub Actions).

        Parameters are declared in ``variables:`` so the GitLab
        "Run pipeline" UI surfaces them as editable fields.
        """
        # variables: block — GitLab's native parameter surface.
        variables: Dict[str, str] = {}
        for name, kind, default, _desc in self._eleven_stage_parameters(config):
            # GitLab strings all go through as strings; booleans are
            # encoded as "true"/"false" and evaluated in ``rules:``.
            variables[name] = default

        # Stages list — 1 setup stage + 11 pipeline stages.
        stages = ["setup"] + [f"stage-{spec.num}" for spec in self._stage_specs()]

        jobs: Dict[str, Any] = {}

        # Setup job
        setup_script = self._render_install_setup(config)
        if config.workdir:
            setup_script = f'cd "{config.workdir}" && {setup_script.replace(chr(10), "; ")}'
        else:
            setup_script = setup_script.replace("\n", "; ")
        jobs["setup"] = {
            "stage": "setup",
            "image": "python:3.12-slim",
            "script": [setup_script],
        }

        # 11 stage jobs
        for spec in self._stage_specs():
            body = self._render_stage_command(spec, config)
            rule_expr = (
                f'$({spec.toggle_param}) == "true"'
                if False
                # GitLab uses $VAR (not $(VAR)) in rules:if
                else (f'${spec.toggle_param} == "true"')
            )
            job: Dict[str, Any] = {
                "stage": f"stage-{spec.num}",
                "image": "python:3.12-slim",
                "rules": [{"if": rule_expr}],
                "script": [body],
            }
            # Stage 11 also gates on non-blank SCHEDULER.
            if spec.num == 11:
                job["rules"] = [{"if": (f'${spec.toggle_param} == "true" && $SCHEDULER != ""')}]
            jobs[f"stage-{spec.num}-{spec.slug.replace('_', '-')}"] = job

        pipeline: Dict[str, Any] = {
            "stages": stages,
            "variables": variables,
            **jobs,
        }
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="GitLab CI",
            secret_surface_hint=(
                "Settings \u2192 CI/CD \u2192 Variables. "
                "Protect + mask any credential-bearing values."
            ),
        )
        content = banner + json.dumps(pipeline, indent=2, default=str)
        return {".gitlab-ci.yml": content}

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate GitLab CI pipeline"""

        commands = self._get_fluid_commands()
        env_vars = self._get_common_environment_vars()
        # Engine specs registry — merge per-engine env vars (e.g.
        # AIRBYTE_PROJECT_DIR for engine='airbyte') so they reach every
        # job via GitLab CI's top-level ``variables:`` block.
        env_vars.update(self._engine_runtime_env_vars(config))

        if config.complexity == PipelineComplexity.BASIC:
            pipeline = self._generate_basic_gitlab_pipeline(config, commands, env_vars)
        elif config.complexity == PipelineComplexity.STANDARD:
            pipeline = self._generate_standard_gitlab_pipeline(config, commands, env_vars)
        else:
            pipeline = self._generate_advanced_gitlab_pipeline(config, commands, env_vars)

        # Splice the per-engine pip-install command into ``before_script``
        # so every job picks up the engine extras (dlt[snowflake], airbyte,
        # meltanolabs-tap-postgres, etc.). Pulled from the same registry
        # as Jenkins; consistent across CI systems.
        engine_pip = self._engine_pip_install_command(config)
        if engine_pip and "before_script" in pipeline:
            pipeline["before_script"] = list(pipeline["before_script"]) + [engine_pip]

        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="GitLab CI",
            secret_surface_hint=(
                "Project Settings → CI/CD → Variables. GitLab auto-injects them as "
                "env for every job — mark as Masked + Protected to scope to "
                "protected branches."
            ),
        )
        runtime_notes = self._engine_runtime_notes(config, indent="# ")
        notes_block = ("\n" + runtime_notes + "\n") if runtime_notes else ""
        return {".gitlab-ci.yml": banner + notes_block + yaml.dump(pipeline, indent=2)}

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
