# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""CircleCITemplate: the thin ``fluid ship`` config for CircleCI.

One workflow, three jobs. ``plan`` runs ``fluid ship --stage 1-6`` on every
push, so on a pull request's branch and on the default branch, and when a
pipeline is triggered by hand through the API. On the default branch only, a
``type: approval`` job (``approve-apply``) waits for someone to press Approve,
and then ``apply`` runs ``fluid ship --stage 7-11 --yes``. ``runtime/`` and
``dist/artifacts/`` travel from plan to apply in the workflow workspace and
are kept as artifacts for reviewers.

Every value is a pipeline parameter (lower-case name) copied into the job
environment under its upper-case name. Ship reads it there. None is expanded
inside a script body.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import _keyless, _thin
from ._base import (
    BasePipelineTemplate,
    PipelineConfig,
    check_pipeline_workdir,
)

RUN_COMPLIANCE = "run_compliance"
SCHEDULED = "<< pipeline.trigger_source >>"
CONFIG_PATH = ".circleci/config.yml"
IMAGE = "cimg/python:3.12"
PROJECT_DIR = "~/project"
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


class CircleCITemplate(BasePipelineTemplate):
    """CircleCI pipeline template"""

    def __init__(self) -> None:
        super().__init__()
        self.provider_name = "CircleCI"
        self.file_extensions = [".yml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        return {CONFIG_PATH: self._header(config) + _thin.render_yaml(self._config(config))}

    # ------------------------------------------------------------------
    # The config
    # ------------------------------------------------------------------

    @staticmethod
    def _branch(config: PipelineConfig) -> str:
        return config.default_branch  # checked by PipelineConfig

    def _config(self, config: PipelineConfig) -> Dict[str, Any]:
        branch = self._branch(config)
        only_default = {"branches": {"only": branch}}
        add_ons = _thin.check_add_ons(config.add_ons)
        jobs: Dict[str, Any] = {
            "plan": self._plan_job(config),
            "apply": self._apply_job(config),
        }
        for add_on in add_ons:
            jobs[add_on] = self._add_on_job(config, add_on)
        # Beside the plan, never required by apply: security on every push, the
        # SBOM on the default branch.
        beside: List[Any] = []
        if "security" in add_ons:
            beside.append("security")
        if "sbom" in add_ons:
            beside.append({"sbom": {"filters": only_default}})
        # A scheduled run starts no plan and parks no approval; a run that asks for
        # the compliance workflow does not start the plan either.
        plan_when: Dict[str, Any] = {"equal": ["scheduled_pipeline", SCHEDULED]}
        unless = [plan_when]
        if "compliance" in add_ons:
            unless.append("<< pipeline.parameters.run_compliance >>")
        workflows: Dict[str, Any] = {
            "fluid": {
                "when": {"not": unless[0]} if len(unless) == 1 else {"not": {"or": unless}},
                "jobs": [
                    "plan",
                    *beside,
                    {
                        # Nothing past this point runs until someone approves.
                        "approve-apply": {
                            "type": "approval",
                            "requires": ["plan"],
                            "filters": only_default,
                        }
                    },
                    {
                        "apply": {
                            "requires": ["approve-apply"],
                            "filters": only_default,
                        }
                    },
                ],
            }
        }
        if "compliance" in add_ons:
            # fluid verify reaches the cloud, so it runs only on a schedule you own,
            # or on a manual or API run that sets run_compliance: never on a push.
            workflows["fluid-compliance"] = {
                "when": {
                    "or": [
                        {"equal": ["scheduled_pipeline", SCHEDULED]},
                        "<< pipeline.parameters.run_compliance >>",
                    ]
                },
                "jobs": [{"compliance": {"filters": only_default}}],
            }
        return {
            "version": 2.1,
            "parameters": self._parameters(config),
            "jobs": jobs,
            "workflows": workflows,
        }

    def _parameters(self, config: PipelineConfig) -> Dict[str, Any]:
        """Pipeline parameters, one per value. Every one is a string, so a
        boolean reaches ship as ``true`` or ``false``."""
        parameters: Dict[str, Any] = {}
        for name, kind, default, description in self._eleven_stage_parameters(config):
            if name == "CONTRACT" or name in _thin.INSTALL_VARIABLES:
                continue  # the contract is written into the commands; install knobs never run-time
            entry: Dict[str, Any] = {
                "type": "string",
                "default": default,
                "description": description,
            }
            options = kind.split(":", 1)[1].split(",") if kind.startswith("choice:") else []
            if kind == "boolean":
                entry = {
                    "type": "enum",
                    "enum": ["true", "false"],
                    "default": default,
                    "description": description,
                }
            elif options and "" not in options:
                entry = {
                    "type": "enum",
                    "enum": options,
                    "default": default,
                    "description": description,
                }
            parameters[name.lower()] = entry
        for knob in STAGE_KNOBS:
            parameters[knob.lower()] = {
                "type": "string",
                "default": "",
                "description": f"Passed to fluid ship ({knob}); blank keeps ship's default.",
            }
        # Not a value ship reads: it starts the compliance workflow on a manual run.
        if "compliance" in _thin.check_add_ons(config.add_ons):
            parameters[RUN_COMPLIANCE] = {
                "type": "boolean",
                "default": False,
                "description": "Run the compliance workflow (fluid verify) instead of the plan.",
            }
        return parameters

    def _environment(self, config: PipelineConfig) -> Dict[str, str]:
        # One run id for both jobs, so their stages share one ship report.
        env: Dict[str, str] = {"FLUID_RUN_ID": "<< pipeline.number >>"}
        for key in self._parameters(config):
            if key == RUN_COMPLIANCE:
                continue
            env[key.upper()] = f"<< pipeline.parameters.{key} >>"
        env.update(self._engine_runtime_env_vars(config))
        return env

    def _carried_paths(self, config: PipelineConfig) -> List[str]:
        prefix = f"{check_pipeline_workdir(config.workdir).rstrip('/')}/" if config.workdir else ""
        return [f"{prefix}{path.rstrip('/')}" for path in _thin.CARRIED_PATHS]

    def _cd(self, config: PipelineConfig) -> str:
        return f'cd "{check_pipeline_workdir(config.workdir)}"\n' if config.workdir else ""

    def _job(self, config: PipelineConfig) -> Dict[str, Any]:
        return {
            "docker": [{"image": IMAGE}],
            "environment": self._environment(config),
            "steps": [
                "checkout",
                {
                    "run": {
                        "name": "Install fluid",
                        # A subshell: its `set -eu` and `set --` stay out of later lines.
                        "command": _keyless.LiteralStr(
                            "(\n" + self._render_install_setup(config) + "\n)\n"
                        ),
                    }
                },
            ],
        }

    def _ship_step(self, config: PipelineConfig, spec: str, *, yes: bool) -> Dict[str, Any]:
        command = _thin.ship_command(
            spec, contract=config.contract_path or _thin.DEFAULT_CONTRACT, yes=yes
        )
        return {
            "run": {
                "name": f"fluid ship --stage {spec}",
                "command": _keyless.LiteralStr(self._cd(config) + command + "\n"),
            }
        }

    def _keep_steps(self, config: PipelineConfig) -> List[Dict[str, Any]]:
        runtime, artifacts = self._carried_paths(config)
        return [
            # when: always, so the reports of a failed plan are kept too.
            {"store_artifacts": {"path": runtime, "destination": "runtime", "when": "always"}},
            {
                "store_artifacts": {
                    "path": artifacts,
                    "destination": "dist-artifacts",
                    "when": "always",
                }
            },
            {"store_test_results": {"path": runtime, "when": "always"}},
        ]

    def _plan_job(self, config: PipelineConfig) -> Dict[str, Any]:
        job = self._job(config)
        job["steps"] += [
            self._ship_step(config, "1-6", yes=False),
            {
                # persist_to_workspace needs every path to exist; a stage that
                # writes one may be switched off.
                "run": {
                    "name": "Make sure the carried paths exist",
                    "command": _keyless.LiteralStr(
                        self._cd(config) + "mkdir -p runtime dist/artifacts\n"
                    ),
                }
            },
            {"persist_to_workspace": {"root": PROJECT_DIR, "paths": self._carried_paths(config)}},
            *self._keep_steps(config),
        ]
        return job

    def _add_on_job(self, config: PipelineConfig, add_on: str) -> Dict[str, Any]:
        """Install, run the add-on's lines, keep runtime/. The job environment is
        the one apply has, so compliance (fluid verify) reaches the cloud."""
        job = self._job(config)
        lines = _thin.add_on_script(add_on, config.contract_path or _thin.DEFAULT_CONTRACT)
        job["steps"] += [
            {
                "run": {
                    "name": _thin.ADD_ON_TITLES[add_on],
                    "command": _keyless.LiteralStr(self._cd(config) + "\n".join(lines) + "\n"),
                }
            },
            {
                "store_artifacts": {
                    "path": self._carried_paths(config)[0],
                    "destination": add_on,
                    "when": "always",
                }
            },
        ]
        return job

    def _apply_job(self, config: PipelineConfig) -> Dict[str, Any]:
        job = self._job(config)
        # Checkout first, then the plan job's files over it.
        job["steps"].insert(1, {"attach_workspace": {"at": PROJECT_DIR}})
        job["steps"] += [self._ship_step(config, "7-11", yes=True), *self._keep_steps(config)]
        return job

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _header(self, config: PipelineConfig) -> str:
        branch = self._branch(config)
        lines = [
            "fluid pipeline, generated by `fluid generate ci --system circleci`.",
            "Each job runs `fluid ship --stage ...` (see `fluid ship --help`).",
            "",
            "The plan job (stages 1-6) runs on every push, so on a pull request's branch",
            f"and on {branch}, and keeps runtime/ and dist/artifacts/ as artifacts. On",
            f"{branch} only, the workflow then waits at approve-apply (a hold job: press",
            "Approve in the CircleCI UI) before the apply job (stages 7-11). To run it by",
            "hand, trigger a pipeline on the branch from the API with the parameters below.",
            "A scheduled pipeline starts no plan. With the compliance add-on, a scheduled",
            "run, or an API run with run_compliance=true, starts only fluid-compliance.",
            "",
            "FLUID_PACKAGE_SPEC, FLUID_PIP_INDEX_URL, FLUID_PIP_EXTRA_INDEX_URL and",
            "FLUID_ALLOW_PRERELEASE are not pipeline parameters: set them as project",
            "environment variables in the project settings, never at run time.",
            "",
            "Every value is a pipeline parameter (lower-case name, e.g. fluid_env) copied",
            "into the job environment under its upper-case name. A stage switched off with",
            "run_stage_<n>_<name>=false is skipped inside ship (10 and 11 are off until set",
            "to true).",
        ]
        header = "".join(f"# {line}".rstrip() + "\n" for line in lines) + "\n"
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="CircleCI",
            secret_surface_hint=(
                "Project Settings > Environment Variables (per-project) or "
                "Organization Settings > Contexts (reusable across projects; add "
                "`context:` to the jobs to use one)."
            ),
            cloud_auth=config.cloud_auth,
        )
        notes = self._engine_runtime_notes(config)
        return header + banner + (notes + "\n" if notes else "")
