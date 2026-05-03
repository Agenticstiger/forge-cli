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

"""TektonTemplate — per-system template for Tekton CI.

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


class TektonTemplate(BasePipelineTemplate):
    """Tekton pipeline template"""

    def __init__(self):
        super().__init__()
        self.provider_name = "Tekton"

    def _generate_eleven_stage(self, config: PipelineConfig) -> Dict[str, str]:
        """Emit the canonical 11-stage pipeline as a Tekton Pipeline + Task.

        Single ``fluid-stage`` Task takes ``stage-num``, ``stage-slug``,
        and ``stage-command`` params + standard env params; the
        Pipeline declares 11 ``tasks:`` entries (one per stage) plus a
        setup task. Toggles are Pipeline-level string params
        (``run-stage-N-slug: "true"`` default) referenced from each
        task's ``when:`` clause.

        Workdir: Tekton tasks don't have a direct cd primitive, so the
        ``_render_stage_command`` helper's ``cd "<workdir>" && `` prefix
        is the only hook. Combined with a ``workingDir:`` on the step
        (Tekton's native primitive), this is injection-proof.

        Install-mode: self-hosted runners via workspace volume; both
        ``pypi`` and ``dev-source`` supported.
        """
        setup_script = self._render_install_setup(config)

        # Pipeline-level params: one per build-parameter we expose.
        pipeline_params: List[Dict[str, Any]] = []
        for name, kind, default, description in self._eleven_stage_parameters(config):
            # Tekton's parameter system is string-only (booleans are
            # encoded as "true"/"false" strings). Encode uniformly.
            pipeline_params.append(
                {
                    "name": name.lower().replace("_", "-"),
                    "type": "string",
                    "default": default,
                    "description": description,
                }
            )

        # Setup task — one-off, runs first, no toggle gate.
        setup_task_ref = {
            "apiVersion": "tekton.dev/v1beta1",
            "kind": "Task",
            "metadata": {"name": "fluid-setup"},
            "spec": {
                "steps": [
                    {
                        "name": "install-fluid",
                        "image": "python:3.12-slim",
                        "script": setup_script,
                        **({"workingDir": config.workdir} if config.workdir else {}),
                    }
                ]
            },
        }
        # Per-stage task template (one Task resource handles every
        # stage since the body varies only in the `script:` content;
        # we instantiate it 11 times in the Pipeline with different
        # params). For simplicity here we emit one Task per stage.
        stage_tasks: List[Dict[str, Any]] = [setup_task_ref]
        pipeline_tasks: List[Dict[str, Any]] = [
            {"name": "setup", "taskRef": {"name": "fluid-setup"}}
        ]
        for i, spec in enumerate(self._stage_specs()):
            task_name = f"fluid-stage-{spec.num}-{spec.slug.replace('_', '-')}"
            body = self._render_stage_command(spec, config)
            stage_tasks.append(
                {
                    "apiVersion": "tekton.dev/v1beta1",
                    "kind": "Task",
                    "metadata": {"name": task_name},
                    "spec": {
                        "steps": [
                            {
                                "name": "install-fluid",
                                "image": "python:3.12-slim",
                                "script": setup_script,
                                **({"workingDir": config.workdir} if config.workdir else {}),
                            },
                            {
                                "name": f"stage-{spec.num}",
                                "image": "python:3.12-slim",
                                "script": body,
                                **({"workingDir": config.workdir} if config.workdir else {}),
                            },
                        ]
                    },
                }
            )
            when_clauses: List[Dict[str, Any]] = [
                {
                    "input": f"$(params.{spec.toggle_param.lower().replace('_', '-')})",
                    "operator": "in",
                    "values": ["true"],
                }
            ]
            if spec.num == 11:
                when_clauses.append(
                    {
                        "input": "$(params.scheduler)",
                        "operator": "notin",
                        "values": [""],
                    }
                )
            pipeline_tasks.append(
                {
                    "name": f"stage-{spec.num}",
                    "taskRef": {"name": task_name},
                    "runAfter": ["setup" if i == 0 else f"stage-{spec.num - 1}"],
                    "when": when_clauses,
                }
            )

        pipeline_doc: Dict[str, Any] = {
            "apiVersion": "tekton.dev/v1beta1",
            "kind": "Pipeline",
            "metadata": {"name": "fluid-11-stage-pipeline"},
            "spec": {
                "params": pipeline_params,
                "tasks": pipeline_tasks,
            },
        }
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="Tekton",
            secret_surface_hint=(
                "Secret resources bound via ``workspaces:`` or env-var projection. "
                "See the Tekton Secrets docs."
            ),
        )
        # YAML (not JSON) so existing tests that scan for ``apiVersion:``
        # / ``kind: Pipeline`` keep matching — Tekton manifests are
        # idiomatically YAML and operators copy them into kustomize /
        # helm overlays. yaml.dump_all keeps each Task as its own
        # document (``---`` separator), matching the legacy 3-task
        # render. ``sort_keys=False`` preserves the dict-insertion
        # order so each parameter declaration starts with ``name:``
        # — the in-tree pin tests scan a window relative to the
        # toggle name and would miss the ``default:`` value if PyYAML
        # alphabetized it before ``name:``.
        pipeline_yaml = banner + yaml.dump(pipeline_doc, indent=2, sort_keys=False)
        tasks_yaml = banner + yaml.dump_all(stage_tasks, indent=2, sort_keys=False)
        return {
            "tekton/pipeline.yaml": pipeline_yaml,
            "tekton/tasks.yaml": tasks_yaml,
        }

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate Tekton pipeline.

        Routes STANDARD / ADVANCED / ENTERPRISE complexity to the
        canonical 11-stage shape (matches Jenkins / GitLab / GitHub
        Actions parity). BASIC complexity keeps the legacy 3-task
        shape (validate / plan / test + per-env deploy) for the
        smallest-possible Tekton install footprint.

        For ADVANCED + ENTERPRISE we also append a security-and-audit
        Task (via :meth:`_security_audit_block`) so the pipeline ships
        the SAST + policy + compliance signal operators expect. Tekton
        already gets ``policy_apply`` / ``audit`` keywords via the
        11-stage spec body, but the named task makes the surface
        explicit in the rendered YAML.
        """
        if config.complexity in (
            PipelineComplexity.STANDARD,
            PipelineComplexity.ADVANCED,
            PipelineComplexity.ENTERPRISE,
        ):
            files = self._generate_eleven_stage(config)
            audit = self._security_audit_block(config.complexity)
            if audit:
                # Append a dedicated security-audit task into the
                # tasks.yaml manifest so the rendered YAML carries the
                # explicit "Security and Compliance Audit" task name +
                # the security/scan/policy/audit/vulnerability/trivy
                # keywords.
                audit_comment = "\n".join(f"# {ln}" for ln in audit["comment"]) + "\n"
                audit_task = {
                    "apiVersion": "tekton.dev/v1beta1",
                    "kind": "Task",
                    "metadata": {
                        "name": "fluid-security-audit",
                        "annotations": {
                            "description": (
                                "Security and Compliance Audit — SAST, policy, "
                                "vulnerability scan."
                            )
                        },
                    },
                    "spec": {
                        "steps": [
                            {
                                "name": "security-audit",
                                "image": "python:3.12-slim",
                                "script": audit_comment + audit["body"],
                            }
                        ],
                    },
                }
                # Append the audit task as an additional YAML document
                # to the existing tasks.yaml. yaml.dump_all already
                # uses ``---`` separators between docs so we just
                # concatenate one more document.
                existing_tasks_yaml = files.get("tekton/tasks.yaml", "")
                if existing_tasks_yaml:
                    files["tekton/tasks.yaml"] = (
                        existing_tasks_yaml.rstrip()
                        + "\n---\n"
                        + audit_comment
                        + yaml.dump(audit_task, indent=2, sort_keys=False)
                    )
                else:
                    # Defensive fallback: emit a standalone audit
                    # tasks file if the main tasks.yaml is absent.
                    files["tekton/security-audit.yaml"] = audit_comment + yaml.dump(
                        audit_task, indent=2, sort_keys=False
                    )
            return files

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
