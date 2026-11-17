# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""TektonTemplate: the thin ``fluid ship`` Task and Pipelines for Tekton.

``tekton/tasks.yaml`` holds one Task, ``fluid-ship``: it installs fluid and
runs ``fluid ship --stage <spec>``. ``tekton/pipeline.yaml`` holds two
Pipelines that run it on a shared ``source`` workspace: ``fluid-plan`` (stages
1-6) and ``fluid-apply`` (stages 7-11, with ``--yes``). Tekton has no native
approval step, so the gate is that apply is a separate Pipeline that a person
starts on purpose, on the same workspace, with the plan run's name as its run
id. Nothing starts it by itself.

Every value is a Task or Pipeline parameter copied into the step environment
under its own name. Ship reads it there. The stage spec and the ``--yes``
switch reach the script the same way (``FLUID_SHIP_STAGE``, ``FLUID_SHIP_YES``):
a ``$(params.x)`` written inside a script is substituted as text, so it would
be code.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import _keyless, _thin
from ._base import BasePipelineTemplate, PipelineConfig, check_pipeline_workdir

PIPELINE_PATH = "tekton/pipeline.yaml"
TASKS_PATH = "tekton/tasks.yaml"
API_VERSION = "tekton.dev/v1"
TASK_NAME = "fluid-ship"
WORKSPACE = "source"
IMAGE = "python:3.12-slim"
#: The stage knobs ship reads, each with its own default when blank.
STAGE_KNOBS: Tuple[str, ...] = (
    "VALIDATE_STRICT",
    "VERIFY_STRICT",
    "GENERATE_EMIT",
    "DIFF_EXIT_ON_DRIFT",
    "DIFF_LAST_APPLIED",
    "PLAN_HTML",
    "POLICY_APPLY_MODE",
)
#: Task parameters that carry the stage spec and the --yes switch.
STAGE_PARAM = "ship-stage"
YES_PARAM = "ship-yes"
RUN_ID = "FLUID_RUN_ID"


class TektonTemplate(BasePipelineTemplate):
    """Tekton pipeline template"""

    def __init__(self) -> None:
        super().__init__()
        self.provider_name = "Tekton"
        self.file_extensions = [".yaml", ".yml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        header = self._header(config)
        pipelines = [
            self._pipeline(config, "fluid-plan", "1-6", yes=False),
            self._pipeline(config, "fluid-apply", "7-11", yes=True),
        ]
        tasks = [self._task(config)]
        # Each add-on is its own Task and Pipeline, started deliberately like
        # fluid-apply, so it never blocks fluid-plan or fluid-apply.
        for add_on in _thin.check_add_ons(config.add_ons):
            tasks.append(self._add_on_task(config, add_on))
            pipelines.append(self._add_on_pipeline(config, add_on))
        return {
            PIPELINE_PATH: header + self._documents(pipelines),
            TASKS_PATH: header + self._documents(tasks),
        }

    @staticmethod
    def _documents(documents: List[Dict[str, Any]]) -> str:
        return "---\n".join(_thin.render_yaml(doc) for doc in documents)

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def _values(self, config: PipelineConfig) -> List[Tuple[str, str, str]]:
        """Every value ship reads from the environment: (name, default,
        description). The contract path is written into the script."""
        values = [
            (name, default, description)
            for name, _kind, default, description in self._eleven_stage_parameters(config)
            if name != "CONTRACT"
        ]
        values += [
            (knob, "", f"Passed to fluid ship ({knob}); blank keeps ship's default.")
            for knob in STAGE_KNOBS
        ]
        return values

    def _param_decls(self, config: PipelineConfig) -> List[Dict[str, Any]]:
        return [
            {"name": name, "type": "string", "default": default, "description": description}
            for name, default, description in self._values(config)
        ]

    # ------------------------------------------------------------------
    # The Task
    # ------------------------------------------------------------------

    def _task(self, config: PipelineConfig) -> Dict[str, Any]:
        contract = config.contract_path or _thin.DEFAULT_CONTRACT
        ship = _thin.ship_command('"$FLUID_SHIP_STAGE"', contract=contract)
        cd = f'cd "{check_pipeline_workdir(config.workdir)}"\n' if config.workdir else ""
        script = (
            "#!/bin/sh\n"
            "set -eu\n"
            # A subshell: its `set -eu` and `set --` stay out of the lines after it.
            "(\n" + self._render_install_setup(config) + "\n)\n" + cd + "set --\n"
            'if [ "$FLUID_SHIP_YES" = "true" ]; then\n'
            "  set -- --yes\n"
            "fi\n"
            f'{ship} "$@"\n'
        )
        env: List[Dict[str, str]] = [
            {"name": "FLUID_SHIP_STAGE", "value": f"$(params.{STAGE_PARAM})"},
            {"name": "FLUID_SHIP_YES", "value": f"$(params.{YES_PARAM})"},
            {"name": RUN_ID, "value": f"$(params.{RUN_ID})"},
        ]
        env += [
            {"name": name, "value": f"$(params.{name})"} for name, _d, _x in self._values(config)
        ]
        env += [
            {"name": k, "value": str(v)} for k, v in self._engine_runtime_env_vars(config).items()
        ]
        return {
            "apiVersion": API_VERSION,
            "kind": "Task",
            "metadata": {"name": TASK_NAME},
            "spec": {
                "description": "Install fluid and run `fluid ship --stage <spec>` on the workspace.",
                "params": [
                    {
                        "name": STAGE_PARAM,
                        "type": "string",
                        "description": "The stage spec ship runs, e.g. 1-6 or 7-11.",
                    },
                    {
                        "name": YES_PARAM,
                        "type": "string",
                        "default": "false",
                        "description": "true passes --yes (stage 7 does not stop to ask).",
                    },
                    {
                        "name": RUN_ID,
                        "type": "string",
                        "default": "",
                        "description": "Run id both phases share, so ship merges one report.",
                    },
                    *self._param_decls(config),
                ],
                "workspaces": [
                    {
                        "name": WORKSPACE,
                        "description": "The repository checkout. runtime/ and dist/artifacts/ "
                        "stay here between the plan and apply Pipelines.",
                    }
                ],
                "steps": [
                    {
                        "name": "ship",
                        "image": IMAGE,
                        "workingDir": f"$(workspaces.{WORKSPACE}.path)",
                        "env": env,
                        "script": _keyless.LiteralStr(script),
                    }
                ],
            },
        }

    def _add_on_task(self, config: PipelineConfig, add_on: str) -> Dict[str, Any]:
        """Install, run the add-on's lines, in the step environment apply has
        (compliance runs fluid verify, which reaches the cloud)."""
        cd = f'cd "{check_pipeline_workdir(config.workdir)}"\n' if config.workdir else ""
        lines = _thin.add_on_script(add_on, config.contract_path or _thin.DEFAULT_CONTRACT)
        script = (
            "#!/bin/sh\n"
            "set -eu\n"
            "(\n" + self._render_install_setup(config) + "\n)\n" + cd + "\n".join(lines) + "\n"
        )
        env: List[Dict[str, str]] = [{"name": RUN_ID, "value": f"$(params.{RUN_ID})"}]
        env += [{"name": n, "value": f"$(params.{n})"} for n, _d, _x in self._values(config)]
        env += [
            {"name": k, "value": str(v)} for k, v in self._engine_runtime_env_vars(config).items()
        ]
        return {
            "apiVersion": API_VERSION,
            "kind": "Task",
            "metadata": {"name": f"{TASK_NAME}-{add_on}"},
            "spec": {
                "description": f"{_thin.ADD_ON_TITLES[add_on]}: reports are written to runtime/.",
                "params": [
                    {
                        "name": RUN_ID,
                        "type": "string",
                        "default": "",
                        "description": "Run id, so ship's reports of one run share it.",
                    },
                    *self._param_decls(config),
                ],
                "workspaces": [{"name": WORKSPACE}],
                "steps": [
                    {
                        "name": add_on,
                        "image": IMAGE,
                        "workingDir": f"$(workspaces.{WORKSPACE}.path)",
                        "env": env,
                        "script": _keyless.LiteralStr(script),
                    }
                ],
            },
        }

    def _add_on_pipeline(self, config: PipelineConfig, add_on: str) -> Dict[str, Any]:
        task_params: List[Dict[str, str]] = [
            {"name": RUN_ID, "value": "$(context.pipelineRun.name)"}
        ]
        task_params += [
            {"name": n, "value": f"$(params.{n})"} for n, _d, _x in self._values(config)
        ]
        return {
            "apiVersion": API_VERSION,
            "kind": "Pipeline",
            "metadata": {"name": f"fluid-{add_on}"},
            "spec": {
                "description": f"{_thin.ADD_ON_TITLES[add_on]}. Start it on purpose.",
                "params": self._param_decls(config),
                "workspaces": [{"name": WORKSPACE}],
                "tasks": [
                    {
                        "name": add_on,
                        "taskRef": {"name": f"{TASK_NAME}-{add_on}"},
                        "workspaces": [{"name": WORKSPACE, "workspace": WORKSPACE}],
                        "params": task_params,
                    }
                ],
            },
        }

    # ------------------------------------------------------------------
    # The Pipelines
    # ------------------------------------------------------------------

    def _pipeline(
        self, config: PipelineConfig, name: str, spec: str, *, yes: bool
    ) -> Dict[str, Any]:
        params: List[Dict[str, Any]] = []
        if yes:
            # The plan run's name: required, so apply cannot start without it.
            params.append(
                {
                    "name": RUN_ID,
                    "type": "string",
                    "description": "The name of the fluid-plan PipelineRun this apply follows.",
                }
            )
            run_id = f"$(params.{RUN_ID})"
        else:
            run_id = "$(context.pipelineRun.name)"
        params += self._param_decls(config)
        task_params: List[Dict[str, str]] = [
            {"name": STAGE_PARAM, "value": spec},
            {"name": YES_PARAM, "value": "true" if yes else "false"},
            {"name": RUN_ID, "value": run_id},
        ]
        task_params += [
            {"name": value_name, "value": f"$(params.{value_name})"}
            for value_name, _d, _x in self._values(config)
        ]
        description = (
            "Stages 7-11 (apply) on the workspace fluid-plan used. Start it on purpose."
            if yes
            else "Stages 1-6 (plan) of fluid ship; keeps runtime/ on the workspace."
        )
        return {
            "apiVersion": API_VERSION,
            "kind": "Pipeline",
            "metadata": {"name": name},
            "spec": {
                "description": description,
                "params": params,
                "workspaces": [{"name": WORKSPACE}],
                "tasks": [
                    {
                        "name": "ship",
                        "taskRef": {"name": TASK_NAME},
                        "workspaces": [{"name": WORKSPACE, "workspace": WORKSPACE}],
                        "params": task_params,
                    }
                ],
            },
        }

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _header(self, config: PipelineConfig) -> str:
        lines = [
            "fluid pipeline, generated by `fluid generate ci --system tekton`.",
            f"Task {TASK_NAME} (tekton/tasks.yaml) runs `fluid ship --stage ...` (see",
            "`fluid ship --help`); the Pipelines fluid-plan and fluid-apply "
            "(tekton/pipeline.yaml)",
            f"run it on a shared workspace named {WORKSPACE}.",
            "",
            f"Apply both files, then fill the {WORKSPACE} workspace with the repository",
            "checkout (for example a git-clone Task before fluid-plan, on a PVC). Start the",
            "plan, for example from a Tekton Triggers binding on a pull request or a push:",
            f"  tkn pipeline start fluid-plan -w name={WORKSPACE},claimName=<pvc>",
            "",
            "Tekton has no approval step. Apply is a separate Pipeline that nothing starts",
            "by itself: when the plan is reviewed, start it on the SAME workspace with the",
            "plan run's name as the run id, so it finds runtime/ and ship merges one report:",
            f"  tkn pipeline start fluid-apply -w name={WORKSPACE},claimName=<pvc> \\",
            f"    -p {RUN_ID}=<name of the fluid-plan PipelineRun>",
            "Limit who may create PipelineRuns of fluid-apply with RBAC.",
            "",
            "Add-ons (security, compliance, sbom), when generated, are Pipelines named",
            "fluid-<add-on>, started the same way on the same workspace (-p is optional).",
            "They are separate from fluid-plan and fluid-apply, so they never block them.",
            "",
            "Every value is a Pipeline parameter, passed to the Task and copied into the",
            "step environment under its own name. RUN_STAGE_<N>_<NAME>=false switches a stage",
            "off (10 and 11 are off until set to true).",
        ]
        header = "".join(f"# {line}".rstrip() + "\n" for line in lines) + "\n"
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="Tekton",
            secret_surface_hint=(
                "ConfigMap or Secret values referenced via `envFrom:` on the Task step, "
                "in the same namespace as the PipelineRun. Cloud access comes from the "
                "Task's service account identity, not a key."
            ),
            cloud_auth=config.cloud_auth,
        )
        notes = self._engine_runtime_notes(config)
        return header + banner + (notes + "\n" if notes else "")
