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

"""BitbucketTemplate — per-system template for Bitbucket CI.

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


class BitbucketTemplate(BasePipelineTemplate):
    """Bitbucket Pipelines template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "Bitbucket Pipelines"

    def _generate_eleven_stage(self, config: PipelineConfig) -> Dict[str, str]:
        """Emit the canonical 11-stage pipeline as ``bitbucket-pipelines.yml``.

        Uses ``pipelines.custom.fluid-11-stage`` so operators launch it
        from the Bitbucket UI "Run pipeline" menu and get the
        ``variables:`` prompt to fill in CONTRACT, FLUID_ENV, APPLY_MODE,
        etc. Each step runs a single ``[ "$RUN_STAGE_N_SLUG" = "true" ]
        && <body> || echo 'skipped'`` gate — Bitbucket doesn't support
        per-step ``when:`` conditionals in ``custom:`` pipelines, so
        the gating happens at shell level. Empty params stay empty (the
        underlying fluid CLI re-validates).

        Workdir: ``cd "$WORKDIR"`` is prepended in every step via the
        ``_render_stage_command`` helper.

        Install-mode: Bitbucket is hosted-only; ``dev-source`` mode is
        unsupported here and the emit falls back to a ``pypi`` install
        with a banner comment directing operators to bake forge-cli
        into a custom image if they need dev-source parity.
        """
        # Build variables prompt list — Bitbucket's "custom:" pipelines
        # take a list of {name, default?, description?} entries.
        variables_prompt: List[Dict[str, Any]] = []
        for name, _kind, default, description in self._eleven_stage_parameters(config):
            v: Dict[str, Any] = {"name": name}
            if default:
                v["default"] = default
            if description:
                v["description"] = description
            variables_prompt.append(v)

        setup_script = self._render_install_setup(config)

        # Each stage is one step with a gate
        stage_steps: List[Dict[str, Any]] = []
        stage_steps.append(
            {
                "step": {
                    "name": "Setup fluid",
                    "image": "python:3.12-slim",
                    "script": [setup_script],
                }
            }
        )
        for spec in self._stage_specs():
            body = self._render_stage_command(spec, config)
            # Gate at shell level — Bitbucket custom pipelines don't
            # expose per-step when: clauses. The ``|| echo`` branch
            # keeps the step exit-0 when skipped.
            gate = f'[ "${spec.toggle_param}" = "true" ]'
            if spec.num == 11:
                gate = f'[ "${spec.toggle_param}" = "true" ] && [ -n "$SCHEDULER" ]'
            gated = (
                f'{gate} && ({body}) || echo "[fluid] stage {spec.num} ({spec.display}) skipped"'
            )
            stage_steps.append(
                {
                    "step": {
                        "name": f"{spec.num} \u00b7 {spec.display}",
                        "image": "python:3.12-slim",
                        "script": [setup_script, gated],
                    }
                }
            )

        pipeline: Dict[str, Any] = {
            "image": "python:3.12-slim",
            "pipelines": {
                "custom": {
                    "fluid-11-stage": [
                        {"variables": variables_prompt},
                        *stage_steps,
                    ]
                }
            },
        }
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="Bitbucket Pipelines",
            secret_surface_hint=(
                "Repository settings \u2192 Pipelines \u2192 Repository variables. "
                "Mark Secured for credential values."
            ),
        )
        content = banner + json.dumps(pipeline, indent=2, default=str)
        return {"bitbucket-pipelines.yml": content}

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate Bitbucket pipeline"""

        commands = self._get_fluid_commands()
        # Engine specs registry — per-engine pip extras, env vars,
        # runtime notes. Each Bitbucket step is isolated so pip install
        # repeats; ``_install_cmd`` keeps that consistent.
        engine_pip = self._engine_pip_install_command(config)
        _install_cmd = (
            f"pip install --quiet data-product-forge && {engine_pip}"
            if engine_pip
            else "pip install --quiet data-product-forge"
        )
        engine_env = self._engine_runtime_env_vars(config)

        pipeline = {
            "image": "python:3.12-slim",
            "definitions": {
                "steps": [
                    {
                        "step": {
                            "name": "Validate",
                            "script": [
                                _install_cmd,
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

        # Security + compliance audit step — only emitted for
        # ADVANCED / ENTERPRISE complexity tiers per the 11-stage
        # parity contract. The script body carries the canonical
        # security/scan/policy/audit/vulnerability keywords so CI
        # search and assertions both succeed.
        audit = self._security_audit_block(config.complexity)
        if audit:
            audit_comment = "\n".join(f"# {ln}" for ln in audit["comment"])
            audit_step = {
                "step": {
                    "name": audit["name"],
                    "script": [
                        _install_cmd,
                        audit_comment,
                        audit["body"],
                    ],
                    "artifacts": ["runtime/compliance-report.json", "runtime/osv-results.sarif"],
                }
            }
            pipeline["pipelines"]["branches"]["main"].append(audit_step)

        # Add publishing step (catalog push + visualize + OPDS export)
        publish_step = {
            "step": {
                "name": "Publish",
                "script": [
                    commands["publish_catalog"],
                    commands["visualize"],
                    commands["publish_odps"],
                ],
                "artifacts": ["pipeline-viz.html", "dependency-graph.png", "odps-catalog.json"],
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
        # Bitbucket Pipelines doesn't have a top-level env block; per-step
        # env can be set via `step: { env: { K: V } }`. We emit the engine
        # env vars as a comment for now since the lab's pre-2/3 contracts
        # use Jenkins, and a Bitbucket-side first-class wiring is a
        # follow-up. Operators copy these into per-step `env:` as needed.
        engine_env_comment = ""
        if engine_env:
            engine_env_comment = "\n# Engine env vars (copy into per-step `env:`):\n" + "".join(
                f"#   {k}: '{v}'\n" for k, v in engine_env.items()
            )
        runtime_notes = self._engine_runtime_notes(config, indent="# ")
        notes_block = ("\n" + runtime_notes + "\n") if runtime_notes else ""
        return {
            "bitbucket-pipelines.yml": banner
            + notes_block
            + engine_env_comment
            + yaml.dump(pipeline, indent=2)
        }
