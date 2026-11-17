# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""AzureDevOpsTemplate: the thin ``fluid ship`` pipeline for Azure DevOps.

Two stages. ``plan`` runs ``fluid ship --stage 1-6`` on a pull request, on a
push to the default branch and on a manual run, and publishes ``runtime/`` and
``dist/artifacts/`` as the ``fluid-carried`` pipeline artifact. ``apply`` is a
``deployment`` job in a second stage, on the default branch only, bound to the
Azure DevOps environment named after the ``FLUID_ENV`` parameter: approvals
and checks live on that environment, so the pipeline pauses until they pass.
It fetches the plan stage's files and runs ``fluid ship --stage 7-11 --yes``.

Every value is a pipeline parameter (the Run pipeline form) copied into a
pipeline variable of the same name, which Azure DevOps exposes to each step as
an environment variable. Ship reads them there. None is expanded inside a
script body.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import _keyless, _thin
from ._base import (
    BasePipelineTemplate,
    PipelineConfig,
    check_pipeline_workdir,
)

PIPELINE_PATH = "azure-pipelines.yml"
CARRIED_ARTIFACT = "fluid-carried"
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


class AzureDevOpsTemplate(BasePipelineTemplate):
    """Azure DevOps pipeline template"""

    def __init__(self) -> None:
        super().__init__()
        self.provider_name = "Azure DevOps"
        self.file_extensions = [".yml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        return {PIPELINE_PATH: self._header(config) + _thin.render_yaml(self._pipeline(config))}

    # ------------------------------------------------------------------
    # The pipeline
    # ------------------------------------------------------------------

    @staticmethod
    def _branch(config: PipelineConfig) -> str:
        return config.default_branch  # checked by PipelineConfig

    def _pipeline(self, config: PipelineConfig) -> Dict[str, Any]:
        branch = self._branch(config)
        return {
            "trigger": {"branches": {"include": [branch]}},
            # Honoured for GitHub and Bitbucket Cloud repositories. For Azure
            # Repos a branch policy with this pipeline as build validation
            # starts the plan on a pull request.
            "pr": {"branches": {"include": [branch]}},
            "parameters": self._parameters(config),
            "variables": self._variables(config),
            "stages": [
                self._plan_stage(config),
                self._apply_stage(config, branch),
                *[
                    self._add_on_stage(config, branch, add_on)
                    for add_on in _thin.check_add_ons(config.add_ons)
                ],
            ],
        }

    def _parameters(self, config: PipelineConfig) -> List[Dict[str, Any]]:
        """The Run pipeline form. Every parameter is a string, so a boolean
        reaches ship as ``true`` or ``false`` (a boolean parameter expands as
        ``True`` or ``False``)."""
        parameters: List[Dict[str, Any]] = []
        for name, kind, default, description in self._eleven_stage_parameters(config):
            if name == "CONTRACT":
                continue  # written into the ship commands
            entry: Dict[str, Any] = {
                "name": name,
                "displayName": description,
                "type": "string",
                "default": default,
            }
            if kind == "boolean":
                entry["values"] = ["true", "false"]
            elif kind.startswith("choice:"):
                options = kind.split(":", 1)[1].split(",")
                if "" not in options:  # blank is a value (SCHEDULER: none): free text
                    entry["values"] = options
            parameters.append(entry)
        for knob in STAGE_KNOBS:
            parameters.append(
                {
                    "name": knob,
                    "displayName": f"Passed to fluid ship ({knob}); blank keeps ship's default.",
                    "type": "string",
                    "default": "",
                }
            )
        return parameters

    def _variables(self, config: PipelineConfig) -> Dict[str, str]:
        """Each parameter, copied into a variable of the same name: Azure DevOps
        gives every step its variables as environment variables."""
        # One run id for both stages, so their ship stages share one report.
        variables: Dict[str, str] = {"FLUID_RUN_ID": "$(Build.BuildId)"}
        for entry in self._parameters(config):
            variables[entry["name"]] = "${{ parameters." + entry["name"] + " }}"
        variables.update(self._engine_runtime_env_vars(config))
        return variables

    @staticmethod
    def _cd(config: PipelineConfig) -> str:
        return f'cd "{check_pipeline_workdir(config.workdir)}"\n' if config.workdir else ""

    def _setup_steps(self, config: PipelineConfig) -> List[Dict[str, Any]]:
        return [
            {"checkout": "self"},
            {"task": "UsePythonVersion@0", "inputs": {"versionSpec": "3.12"}},
            {
                # A subshell: its `set -eu` and `set --` stay out of later lines.
                "bash": _keyless.LiteralStr("(\n" + self._render_install_setup(config) + "\n)\n"),
                "displayName": "Install fluid",
            },
        ]

    def _ship_step(
        self, config: PipelineConfig, title: str, spec: str, *, yes: bool
    ) -> Dict[str, Any]:
        command = _thin.ship_command(
            spec, contract=config.contract_path or _thin.DEFAULT_CONTRACT, yes=yes
        )
        return {
            "bash": _keyless.LiteralStr(self._cd(config) + command + "\n"),
            "displayName": title,
        }

    def _plan_stage(self, config: PipelineConfig) -> Dict[str, Any]:
        stage_dir = "$(Build.ArtifactStagingDirectory)/fluid"
        stage_files = "".join(
            f'if [ -d "{path}" ]; then mkdir -p "{stage_dir}/{path}"; '
            f'cp -R "{path}." "{stage_dir}/{path}"; fi\n'
            for path in _thin.CARRIED_PATHS
        )
        steps: List[Dict[str, Any]] = [
            *self._setup_steps(config),
            self._ship_step(config, "fluid ship stages 1-6 (plan)", "1-6", yes=False),
            {
                # What apply needs and what a reviewer reads. A marker file keeps
                # the artifact non-empty when a stage that writes a path is off.
                "bash": _keyless.LiteralStr(
                    "set -eu\n"
                    + self._cd(config)
                    + f'mkdir -p "{stage_dir}"\n'
                    + f'touch "{stage_dir}/.fluid-carried"\n'
                    + stage_files
                ),
                "displayName": "Collect the bundle, plan and reports",
                "condition": "always()",
            },
            {
                "task": "PublishPipelineArtifact@1",
                "displayName": "Keep the bundle, plan and reports",
                "condition": "always()",
                "inputs": {"targetPath": stage_dir, "artifact": CARRIED_ARTIFACT},
            },
            {
                "task": "PublishTestResults@2",
                "displayName": "Publish ship's test report",
                "condition": "always()",
                "inputs": {
                    "testResultsFormat": "JUnit",
                    "testResultsFiles": "runtime/ship-junit.xml",
                    "searchFolder": "$(System.DefaultWorkingDirectory)"
                    + (f"/{check_pipeline_workdir(config.workdir)}" if config.workdir else ""),
                    "failTaskOnFailedTests": False,
                    "failTaskOnMissingResultsFile": False,
                },
            },
        ]
        return {
            "stage": "plan",
            "displayName": "Plan (stages 1-6)",
            "jobs": [{"job": "plan", "pool": {"vmImage": "ubuntu-latest"}, "steps": steps}],
        }

    def _apply_stage(self, config: PipelineConfig, branch: str) -> Dict[str, Any]:
        target = "$(Build.SourcesDirectory)" + (
            f"/{check_pipeline_workdir(config.workdir)}" if config.workdir else ""
        )
        steps: List[Dict[str, Any]] = [
            *self._setup_steps(config),
            {"download": "current", "artifact": CARRIED_ARTIFACT},
            {
                "bash": _keyless.LiteralStr(
                    "set -eu\n"
                    f'mkdir -p "{target}"\n'
                    f'cp -R "$(Pipeline.Workspace)/{CARRIED_ARTIFACT}/." "{target}/"\n'
                ),
                "displayName": "Restore the bundle and plan the plan stage kept",
            },
            self._ship_step(config, "fluid ship stages 7-11 (apply)", "7-11", yes=True),
        ]
        return {
            "stage": "apply",
            "displayName": "Apply (stages 7-11)",
            "dependsOn": "plan",
            # Never a pull request: only the default branch, whether pushed to or
            # run by hand there.
            "condition": (
                f"and(succeeded(), eq(variables['Build.SourceBranch'], 'refs/heads/{branch}'))"
            ),
            "jobs": [
                {
                    "deployment": "apply",
                    "pool": {"vmImage": "ubuntu-latest"},
                    # Approvals and checks on this environment gate stages 7-11.
                    "environment": "${{ parameters.FLUID_ENV }}",
                    "strategy": {"runOnce": {"deploy": {"steps": steps}}},
                }
            ],
        }

    def _add_on_stage(self, config: PipelineConfig, branch: str, add_on: str) -> Dict[str, Any]:
        """An add-on runs beside the plan (``dependsOn: []``), so it never
        blocks plan or apply, and keeps ``runtime/``. Compliance runs ``fluid
        verify``, which reaches the cloud: it gets the same variables apply has."""
        when = {
            "security": None,  # every run: pull requests and the default branch
            "sbom": f"eq(variables['Build.SourceBranch'], 'refs/heads/{branch}')",
            "compliance": "eq(variables['Build.Reason'], 'Manual')",
        }[add_on]
        script = self._cd(config) + "\n".join(
            _thin.add_on_script(add_on, config.contract_path or _thin.DEFAULT_CONTRACT)
        )
        run: Dict[str, Any] = {
            "bash": _keyless.LiteralStr(script + "\n"),
            "displayName": _thin.ADD_ON_TITLES[add_on],
        }
        if add_on != "security":
            run["continueOnError"] = "true"  # advisory
        base = "$(System.DefaultWorkingDirectory)" + (
            f"/{check_pipeline_workdir(config.workdir)}" if config.workdir else ""
        )
        steps = [
            *self._setup_steps(config),
            run,
            {
                "task": "PublishPipelineArtifact@1",
                "displayName": f"Keep the {add_on} reports",
                "condition": "always()",
                "inputs": {"targetPath": f"{base}/runtime", "artifact": f"fluid-{add_on}"},
            },
        ]
        stage: Dict[str, Any] = {
            "stage": add_on,
            "displayName": _thin.ADD_ON_TITLES[add_on],
            "dependsOn": [],
        }
        if when:
            stage["condition"] = when
        stage["jobs"] = [{"job": add_on, "pool": {"vmImage": "ubuntu-latest"}, "steps": steps}]
        return stage

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _header(self, config: PipelineConfig) -> str:
        branch = self._branch(config)
        env_default = self._pipeline_defaults(config)["FLUID_ENV"]
        lines = [
            "fluid pipeline, generated by `fluid generate ci --system azure`.",
            "Each stage runs `fluid ship --stage ...` (see `fluid ship --help`).",
            "",
            f"A pull request against {branch} runs stages 1-6 (plan) and keeps runtime/ as the",
            f"{CARRIED_ARTIFACT} pipeline artifact. A push to {branch}, or a manual run on it,",
            "also runs stages 7-11 (apply) as a deployment job in the Azure DevOps",
            f"environment named after FLUID_ENV (default {env_default}). Add approvals and",
            "checks to that environment (Pipelines > Environments) so nothing is applied",
            "unreviewed. A pull request never reaches apply.",
            "",
            "Every value is a parameter on the Run pipeline form, copied into a variable of",
            "the same name that ship reads from the environment. RUN_STAGE_<N>_<NAME> set to",
            "false switches a stage off (10 and 11 are off until set to true).",
        ]
        header = "".join(f"# {line}".rstrip() + "\n" for line in lines) + "\n"
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="Azure DevOps Pipelines",
            secret_surface_hint=(
                "Pipelines > Library > Variable Groups. Link the group to the "
                "pipeline and map secret vars into step env via `env: FOO: $(FOO)`."
            ),
            cloud_auth=config.cloud_auth,
        )
        notes = self._engine_runtime_notes(config)
        return header + banner + (notes + "\n" if notes else "")
