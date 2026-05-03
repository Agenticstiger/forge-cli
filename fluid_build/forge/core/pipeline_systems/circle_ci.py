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

"""CircleCITemplate — per-system template for Circle Ci CI.

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


class CircleCITemplate(BasePipelineTemplate):
    """CircleCI pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "CircleCI"

    def _generate_eleven_stage(self, config: PipelineConfig) -> Dict[str, str]:
        """Emit the canonical 11-stage pipeline as ``.circleci/config.yml``.

        One job per stage, chained via ``requires:`` in the workflow
        block. Stage toggles implemented via ``when: <<pipeline.parameters.run_stage_N>>``
        — CircleCI evaluates these at pipeline-load time, not at shell
        level, so the gates are injection-proof.

        Workdir: jobs use ``working_directory: ~/project/<workdir>``
        when set (CircleCI's native primitive).

        Install-mode: hosted-only (like Bitbucket); dev-source falls
        back to pypi with a banner comment.
        """
        # parameters block — CircleCI's native pipeline-parameter surface.
        parameters: Dict[str, Dict[str, Any]] = {}
        for name, kind, default, description in self._eleven_stage_parameters(config):
            key = name.lower()
            if kind == "boolean":
                parameters[key] = {
                    "type": "boolean",
                    "default": default == "true",
                    "description": description,
                }
            elif kind.startswith("choice:"):
                # CircleCI lacks native choice; use string + description.
                parameters[key] = {
                    "type": "string",
                    "default": default,
                    "description": (
                        description + " Values: " + ", ".join(kind.split(":", 1)[1].split(","))
                    ),
                }
            else:
                parameters[key] = {
                    "type": "string",
                    "default": default,
                    "description": description,
                }

        # Shared env mapping — parameters flow into job env.
        env_from_params = {
            name: f"<<pipeline.parameters.{name.lower()}>>"
            for name, _k, _d, _desc in self._eleven_stage_parameters(config)
        }

        setup_script = self._render_install_setup(config)
        working_dir = f"~/project/{config.workdir}" if config.workdir else "~/project"

        # Jobs: setup + 11 stage jobs
        jobs: Dict[str, Any] = {
            "setup": {
                "docker": [{"image": "cimg/python:3.12"}],
                "working_directory": working_dir,
                "steps": ["checkout", {"run": {"command": setup_script}}],
            }
        }
        for spec in self._stage_specs():
            body = self._render_stage_command(spec, config)
            jobs[f"stage_{spec.num}_{spec.slug}"] = {
                "docker": [{"image": "cimg/python:3.12"}],
                "working_directory": working_dir,
                "environment": env_from_params,
                "steps": [
                    "checkout",
                    {"run": {"command": setup_script}},
                    {
                        "run": {
                            "name": f"{spec.num} \u00b7 {spec.display}",
                            "command": body,
                        }
                    },
                ],
            }

        # Workflow with stage toggles as `when:` clauses
        workflow_jobs: List[Any] = ["setup"]
        prev_job = "setup"
        for spec in self._stage_specs():
            job_key = f"stage_{spec.num}_{spec.slug}"
            when_expr = f"<<pipeline.parameters.{spec.toggle_param.lower()}>>"
            if spec.num == 11:
                # Stage 11 additionally requires a non-blank SCHEDULER.
                # CircleCI's ``when:`` supports logic via
                # ``and: [param, {not: {equal: [<<…>>, '']}}]``.
                when_expr = {
                    "and": [
                        when_expr,
                        {
                            "not": {
                                "equal": [
                                    "<<pipeline.parameters.scheduler>>",
                                    "",
                                ]
                            }
                        },
                    ]
                }
            workflow_jobs.append(
                {
                    job_key: {
                        "requires": [prev_job],
                        "when": when_expr,
                    }
                }
            )
            prev_job = job_key

        pipeline: Dict[str, Any] = {
            "version": 2.1,
            "parameters": parameters,
            "jobs": jobs,
            "workflows": {
                "fluid-11-stage": {"jobs": workflow_jobs},
            },
        }
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="CircleCI",
            secret_surface_hint=(
                "Project Settings \u2192 Environment Variables for credential bindings."
            ),
        )
        content = banner + json.dumps(pipeline, indent=2, default=str)
        return {".circleci/config.yml": content}

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

        # Security + compliance audit job — only emitted for
        # ADVANCED / ENTERPRISE complexity tiers per the 11-stage
        # parity contract. The command body carries the canonical
        # security/scan/policy/audit/vulnerability keywords so CI
        # search and assertions both succeed.
        audit = self._security_audit_block(config.complexity)
        if audit:
            audit_comment = "\n".join(f"# {ln}" for ln in audit["comment"])
            pipeline["jobs"]["security-audit"] = {
                "executor": "python-executor",
                "steps": [
                    "checkout",
                    {"run": "pip install --quiet data-product-forge"},
                    {
                        "run": {
                            "name": audit["name"],
                            "command": audit_comment + "\n" + audit["body"],
                        }
                    },
                    {"store_artifacts": {"path": "runtime/", "destination": "security-audit"}},
                ],
            }
            pipeline["workflows"]["fluid-pipeline"]["jobs"].append(
                {"security-audit": {"requires": ["validate"]}}
            )

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
