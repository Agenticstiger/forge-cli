# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""GitLabCITemplate: the thin ``fluid ship`` pipeline for GitLab CI.

Two jobs. ``fluid-plan`` runs stages 1-6 with one ``fluid ship --stage 1-6``
(ship prints a collapsible section per stage) on a merge request, on the
default branch and on a manual run, and keeps ``runtime/`` and
``dist/artifacts/`` as artifacts, with ship's JUnit report as the pipeline's
test report. ``fluid-apply`` runs ``fluid ship --stage 7-11 --yes`` on the
default branch or a manual run: a manual job in the environment named after
``FLUID_ENV``, one at a time per environment (``resource_group``). The shape
follows GitLab's OpenTofu CI/CD component: plan on merge requests, apply by
hand on the default branch.

Every value is a CI/CD variable that ship reads from the job environment:
the defaults below, which a project variable or the Run pipeline form
overrides. ``RUN_STAGE_<N>_<NAME>`` switches a stage off (or 10 and 11 on);
ship skips a switched-off stage inside the job.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import _keyless, _thin
from ._base import BasePipelineTemplate, PipelineConfig, check_pipeline_workdir

PIPELINE_PATH = ".gitlab-ci.yml"
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


class GitLabCITemplate(BasePipelineTemplate):
    """GitLab CI pipeline template"""

    def __init__(self) -> None:
        super().__init__()
        self.provider_name = "GitLab CI"
        self.file_extensions = [".yml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        return {PIPELINE_PATH: self._header(config) + _thin.render_yaml(self._pipeline(config))}

    def _pipeline(self, config: PipelineConfig) -> Dict[str, Any]:
        add_ons = _thin.check_add_ons(config.add_ons)
        rules: List[Dict[str, str]] = [
            {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
            # A branch with an open merge request runs its merge request
            # pipeline only, not a second branch pipeline.
            {"if": "$CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS", "when": "never"},
            {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"},
            {"if": '$CI_PIPELINE_SOURCE == "web"'},
        ]
        if add_ons:  # their scheduled runs (Build > Pipeline schedules)
            rules.append({"if": '$CI_PIPELINE_SOURCE == "schedule"'})
        pipeline: Dict[str, Any] = {
            "workflow": {"rules": rules},
            "variables": self._variables(config),
            "stages": ["plan", "apply"],
            "fluid-plan": self._plan_job(config),
            "fluid-apply": self._apply_job(config),
        }
        for add_on in add_ons:
            pipeline[f"fluid-{add_on}"] = self._add_on_job(add_on, config)
        return pipeline

    #: When each add-on runs: security beside every plan, compliance on a
    #: schedule or by hand, the SBOM on the default branch and on a schedule.
    _ADD_ON_RULES = {
        "security": [
            {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
            {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"},
            {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
        ],
        "compliance": [
            {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
            {"if": '$CI_PIPELINE_SOURCE == "web"'},
        ],
        "sbom": [
            {"if": '$CI_PIPELINE_SOURCE == "schedule"'},
            {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"},
        ],
    }

    def _add_on_job(self, add_on: str, config: PipelineConfig) -> Dict[str, Any]:
        """An add-on runs beside the plan, never blocks it, and keeps what it wrote."""
        job = self._job(config, "plan", "")
        script = _thin.add_on_script(add_on, self._contract(config))
        auth = _keyless.gitlab_auth_script(config.cloud_auth) if add_on == "compliance" else []
        job["script"] = [*auth, *script]
        if add_on != "compliance":
            job.pop("id_tokens", None)
            job.pop("variables", None)
        job["rules"] = self._ADD_ON_RULES[add_on]
        job["needs"] = []
        job["allow_failure"] = add_on != "security"
        job["artifacts"] = {
            "when": "always",
            "expire_in": "30 days",
            "paths": [self._in_workdir(config, "runtime/")],
        }
        return job

    def _variables(self, config: PipelineConfig) -> Dict[str, Any]:
        """The defaults, each with what the Run pipeline form shows."""
        variables: Dict[str, Any] = {
            # One run id for both jobs, so their stages share one ship report.
            "FLUID_RUN_ID": "$CI_PIPELINE_ID",
        }
        for name, kind, default, description in self._eleven_stage_parameters(config):
            if name == "CONTRACT":
                continue  # written into the ship commands
            if name in _thin.INSTALL_VARIABLES:
                # A plain default, not on the Run pipeline form: what pip
                # installs is set as a project CI/CD variable, never per run.
                variables[name] = default
                continue
            entry: Dict[str, Any] = {"value": default, "description": description}
            if kind == "boolean":
                entry["options"] = ["true", "false"]
            elif kind.startswith("choice:"):
                options = kind.split(":", 1)[1].split(",")
                if "" in options:  # blank is a value (SCHEDULER: none): free text
                    entry["description"] = (
                        f"{description} One of: {', '.join(o for o in options if o)}, or blank."
                    )
                else:
                    entry["options"] = options
            variables[name] = entry
        for knob in STAGE_KNOBS:
            variables[knob] = {
                "value": "",
                "description": f"Passed to fluid ship (see fluid ship --help); blank keeps "
                f"ship's default for {knob}.",
            }
        variables.update(self._engine_runtime_env_vars(config))
        return variables

    def _job(self, config: PipelineConfig, stage: str, command: str) -> Dict[str, Any]:
        before: List[str] = []
        if config.workdir:
            before.append(f'cd "{check_pipeline_workdir(config.workdir)}"')
        # A subshell: GitLab runs before_script and script in one shell, so the
        # install's `set -eu` and `set --` would otherwise reach every later line.
        before.append(_keyless.LiteralStr("(\n" + self._render_install_setup(config) + "\n)\n"))
        job: Dict[str, Any] = {
            "stage": stage,
            "image": IMAGE,
            "before_script": before,
            "script": [*_keyless.gitlab_auth_script(config.cloud_auth), command],
            "artifacts": {
                "when": "always",
                "expire_in": "30 days",
                "paths": self._carried(config),
                "reports": {"junit": self._in_workdir(config, "runtime/ship-junit.xml")},
            },
        }
        tokens = _keyless.gitlab_id_tokens(config.cloud_auth)
        if tokens:
            job["id_tokens"] = tokens
        auth_vars = _keyless.gitlab_auth_variables(config.cloud_auth)
        if auth_vars:
            job["variables"] = auth_vars
        return job

    def _plan_job(self, config: PipelineConfig) -> Dict[str, Any]:
        command = _thin.ship_command(
            f"{_thin.PLAN_STAGES[0]}-{_thin.PLAN_STAGES[-1]}", contract=self._contract(config)
        )
        job = self._job(config, "plan", command)
        job["artifacts"]["expose_as"] = "fluid plan"
        # A scheduled pipeline runs the add-ons only.
        job["rules"] = [
            {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "never"},
            {"when": "on_success"},
        ]
        return job

    def _apply_job(self, config: PipelineConfig) -> Dict[str, Any]:
        command = _thin.ship_command(
            f"{_thin.APPLY_STAGES[0]}-{_thin.APPLY_STAGES[-1]}",
            contract=self._contract(config),
            yes=True,
        )
        job = self._job(config, "apply", command)
        job.update(
            {
                "needs": [{"job": "fluid-plan", "artifacts": True}],
                # A person starts apply; until then the pipeline is blocked,
                # not passed. Protect the environment to choose who may.
                "rules": [
                    {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"', "when": "never"},
                    {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "never"},
                    {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH", "when": "manual"},
                    {"if": '$CI_PIPELINE_SOURCE == "web"', "when": "manual"},
                ],
                "allow_failure": False,
                "environment": {"name": "$FLUID_ENV"},
                # One apply per environment at a time; a second one waits.
                "resource_group": "fluid-$FLUID_ENV",
            }
        )
        return job

    @staticmethod
    def _contract(config: PipelineConfig) -> str:
        return config.contract_path or _thin.DEFAULT_CONTRACT

    @staticmethod
    def _in_workdir(config: PipelineConfig, path: str) -> str:
        return f"{config.workdir.rstrip('/')}/{path}" if config.workdir else path

    def _carried(self, config: PipelineConfig) -> List[str]:
        return [self._in_workdir(config, path) for path in _thin.CARRIED_PATHS]

    def _header(self, config: PipelineConfig) -> str:
        env_default = self._pipeline_defaults(config)["FLUID_ENV"]
        lines = [
            "fluid pipeline, generated by `fluid generate ci --system gitlab`.",
            "Each job runs `fluid ship --stage ...` (see `fluid ship --help`), one",
            "collapsible section per stage.",
            "",
            "A merge request runs stages 1-6 (fluid-plan) and keeps runtime/ as an artifact.",
            "On the default branch, fluid-apply runs stages 7-11 when someone starts it, in",
            f"the environment named after FLUID_ENV (default {env_default}). Protect that",
            "environment (Settings > CI/CD > Protected environments) to choose who may.",
            "",
            "Every value below is a default: a project CI/CD variable or the Run pipeline",
            "form overrides it. RUN_STAGE_<N>_<NAME>=false switches a stage off.",
        ]
        header = "".join(f"# {line}".rstrip() + "\n" for line in lines) + "\n"
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="GitLab CI",
            secret_surface_hint=(
                "Settings > CI/CD > Variables. Mark credentials Masked and Protected."
            ),
            cloud_auth=config.cloud_auth,
        )
        notes = self._engine_runtime_notes(config)
        return header + banner + (notes + "\n" if notes else "")
