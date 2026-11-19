# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""GitLabCITemplate: the thin ``fluid ship`` pipeline for GitLab CI.

Two jobs. ``fluid-plan`` runs stages 1-6 with one ``fluid ship --stage 1-6``
(ship prints a collapsible section per stage) on a merge request, on the
default branch and on a manual run, and keeps ``runtime/`` and
``dist/artifacts/`` as artifacts, with ship's JUnit report as the pipeline's
test report. On the default branch it also publishes the release packet
(``fluid ship --release-dir release``): the bundle, the artifacts, the plan and
the reports, with ``release.json`` written last, only when every stage passed.
``fluid-apply`` runs ``fluid ship --stage 7-11 --yes`` on the default branch only
(``$CI_DEFAULT_BRANCH``, the project's own setting; a merge request and any
other ref plan only): a manual job in the environment named after
``FLUID_ENV``, one at a time per environment (``resource_group``), that
downloads that job's artifacts and refuses to start without its packet. The
shape follows GitLab's OpenTofu CI/CD component: plan on merge requests, apply
by hand on the default branch.

A merge request also gets ``fluid-impact``: ``fluid diff --baseline --impact``
of the contract against the merge request's base, with the plan, as Markdown
and JSON beside the plan (``expose_as``). A plan job that fails keeps its
reports as evidence and drops what could be deployed (the bundle, the plan,
the artifacts), so nothing a failed run wrote reads as a release.

The Run pipeline form is ``spec:inputs`` (GitLab 17.11 or later): the stage
switches and the most-used knobs, at most :data:`MAX_INPUTS`. Each input goes
into a job variable, never into a script, so a value stays data. Every other
value ship reads is a variable default here, which a project CI/CD variable
overrides (and overrides an input of the same name too).
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Tuple

from . import _keyless, _thin
from ._base import BasePipelineTemplate, PipelineConfig, check_pipeline_workdir

PIPELINE_PATH = ".gitlab-ci.yml"
IMAGE = "python:3.12-slim"
#: ``fluid-impact`` reads the merge request's base with git, which the slim image lacks.
IMPACT_IMAGE = "python:3.12"
#: GitLab takes at most 20 inputs for a pipeline.
MAX_INPUTS = 20
#: The knobs on the Run pipeline form beside the 11 stage switches; every other
#: value is a variable default a project CI/CD variable sets.
FORM_KNOBS: Tuple[str, ...] = (
    "FLUID_ENV",
    "APPLY_MODE",
    "ALLOW_DATA_LOSS",
    "NO_VERIFY_DIGEST",
    "VALIDATE_STRICT",
    "VERIFY_STRICT",
    "PUBLISH_TARGETS",
    "SCHEDULER",
    "SCHEDULE_SYNC_DRY_RUN",
)
#: The text a free-text input may hold, checked when the pipeline is created.
#: Without it an input could hold ``$NAME``, which GitLab expands into the job
#: variable it is mapped to: another variable's value, in a job log or an
#: environment name. FLUID_ENV follows ship's environment grammar
#: (:func:`._base.check_env_name`); PUBLISH_TARGETS is target names, space separated.
INPUT_PATTERNS: Dict[str, str] = {
    "FLUID_ENV": "^[A-Za-z][A-Za-z0-9_.-]{0,63}$",
    "PUBLISH_TARGETS": "^[A-Za-z0-9_.-]*( [A-Za-z0-9_.-]+)*$",
}
#: Where the plan job publishes the release packet, on the default branch.
RELEASE_DIR = "release"
#: The job variable the plan job's rules set to :data:`RELEASE_DIR`.
RELEASE_VARIABLE = "FLUID_RELEASE_DIR"
IMPACT_DIR = "runtime/impact/"
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
        inputs, variables = self._form(config)
        header = _thin.render_yaml({"spec": {"inputs": inputs}})
        body = _thin.render_yaml(self._pipeline(config, variables))
        return {PIPELINE_PATH: self._header(config) + header + "---\n" + body}

    def _pipeline(self, config: PipelineConfig, variables: Dict[str, Any]) -> Dict[str, Any]:
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
            "variables": variables,
            "stages": ["plan", "apply"],
            "fluid-plan": self._plan_job(config),
            "fluid-impact": self._impact_job(config),
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

    def _form(self, config: PipelineConfig) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """The ``spec:inputs`` of the Run pipeline form, and the global variables.

        An input on the form reaches ship as the job variable it is mapped to
        (``FLUID_ENV: $[[ inputs.fluid_env ]]``); a value is never pasted into
        a script. Every other value is a plain default.
        """
        from fluid_build.cli._ship_table import STAGE_TOGGLES

        on_form = set(STAGE_TOGGLES.values()) | set(FORM_KNOBS)
        inputs: Dict[str, Any] = {}
        variables: Dict[str, Any] = {
            # One run id for every job, so their stages share one ship report.
            "FLUID_RUN_ID": "$CI_PIPELINE_ID",
        }
        parameters = [p for p in self._eleven_stage_parameters(config) if p[0] != "CONTRACT"]
        parameters += [
            (
                knob,
                "choice:,true,false" if knob in ("VALIDATE_STRICT", "VERIFY_STRICT") else "string",
                "",
                f"Passed to fluid ship (see fluid ship --help); blank keeps ship's default "
                f"for {knob}.",
            )
            for knob in STAGE_KNOBS
        ]
        for name, kind, default, description in parameters:
            if name in _thin.INSTALL_VARIABLES or name not in on_form:
                # What pip installs is a project CI/CD variable, never per run.
                variables[name] = default
                continue
            key = name.lower()
            inputs[key] = self._input(kind, default, description)
            if name in INPUT_PATTERNS:
                inputs[key]["regex"] = INPUT_PATTERNS[name]
            if "options" not in inputs[key] and "regex" not in inputs[key]:
                raise ValueError(f"input {key} takes any text: give it options or a regex")
            variables[name] = f"$[[ inputs.{key} ]]"
        if len(inputs) > MAX_INPUTS:
            raise ValueError(f"{len(inputs)} inputs: GitLab takes at most {MAX_INPUTS}")
        variables.update(self._engine_runtime_env_vars(config))
        return inputs, variables

    @staticmethod
    def _input(kind: str, default: str, description: str) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"type": "string", "default": default, "description": description}
        if kind == "boolean":
            entry["options"] = ["true", "false"]
        elif kind.startswith("choice:"):
            options = kind.split(":", 1)[1].split(",")
            if "" in options:  # blank is a value (SCHEDULER: none), which options cannot hold
                named = [o for o in options if o]
                entry["regex"] = "^(" + "|".join(re.escape(o) for o in named) + ")?$"
                entry["description"] = f"{description} One of: {', '.join(named)}, or blank."
            else:
                entry["options"] = options
        return entry

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
        # The release packet, where the rules below name a directory for it.
        command += f' ${{{RELEASE_VARIABLE}:+--release-dir "${RELEASE_VARIABLE}"}}'
        job = self._job(config, "plan", command)
        job["artifacts"]["paths"].append(self._in_workdir(config, f"{RELEASE_DIR}/"))
        job["artifacts"]["expose_as"] = "fluid plan"
        job["after_script"] = self._evidence_only(config)
        job["rules"] = [
            # A scheduled pipeline runs the add-ons only.
            {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "never"},
            # The packet apply downloads: published on the default branch only,
            # where apply runs. A merge request plans without one, so a stage
            # switched off there still plans.
            {
                "if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH",
                "variables": {RELEASE_VARIABLE: RELEASE_DIR},
            },
            {"when": "on_success"},
        ]
        return job

    def _evidence_only(self, config: PipelineConfig) -> List[str]:
        """``after_script``: a job that did not pass keeps its reports and drops
        what could be deployed, before its artifacts are uploaded."""
        # The leaf table, not _ship_stages: generating a file must not import ship.
        from fluid_build.cli._ship_table import ARTIFACTS_DIR, BUNDLE_PATH, PLAN_PATH

        stem = PLAN_PATH.rsplit(".", 1)[0]
        deployable = [
            BUNDLE_PATH,
            PLAN_PATH,
            f"{stem}.tfplan",
            f"{stem}.tfplan.json",
            f"{stem}.gitlab-tofu.json",
            ARTIFACTS_DIR,
            f"{RELEASE_DIR}/",
        ]
        lines: List[str] = []
        if config.workdir:
            # after_script starts in the project directory again.
            lines.append(f'cd "{check_pipeline_workdir(config.workdir)}"')
        lines.append(
            'if [ "$CI_JOB_STATUS" != "success" ]; then '
            f"rm -rf {' '.join(shlex.quote(p) for p in deployable)}; "
            "echo 'fluid-plan did not pass: its reports are kept, its bundle, plan and "
            "artifacts are not'; fi"
        )
        return lines

    def _impact_job(self, config: PipelineConfig) -> Dict[str, Any]:
        """On a merge request, what the change reaches: ``fluid diff --impact``
        against the contract at the merge request's base, with the plan."""
        contract = _thin.quoted_contract(self._contract(config))
        out = IMPACT_DIR
        job = self._job(config, "plan", "")
        job.pop("id_tokens", None)
        job.pop("variables", None)
        job["image"] = IMPACT_IMAGE
        job["script"] = [
            f"mkdir -p {out}",
            'base="$(mktemp -d)"',
            # A shallow clone may not hold the base commit.
            'git fetch --quiet --depth 1 origin "$CI_MERGE_REQUEST_DIFF_BASE_SHA"',
            'git archive --format=tar "$CI_MERGE_REQUEST_DIFF_BASE_SHA:./" | tar -x -C "$base"',
            f'if [ ! -f "$base"/{contract} ]; then echo "The contract is new in this merge '
            f'request: nothing to compare it with." > {out}impact.md; exit 0; fi',
            "plan=''; if [ -f runtime/plan.json ]; then plan='--plan runtime/plan.json'; fi",
            f'fluid diff {contract} --baseline "$base"/{contract} --impact $plan '
            f"--format markdown --out {out}impact.json > {out}impact.md",
        ]
        job["needs"] = [{"job": "fluid-plan", "artifacts": True}]
        job["rules"] = [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'}]
        # The report informs a review; it never blocks the merge request.
        job["allow_failure"] = True
        job["artifacts"] = {
            "when": "always",
            "expire_in": "30 days",
            "paths": [self._in_workdir(config, out)],
            "expose_as": "fluid impact",
        }
        return job

    def _apply_job(self, config: PipelineConfig) -> Dict[str, Any]:
        command = _thin.ship_command(
            f"{_thin.APPLY_STAGES[0]}-{_thin.APPLY_STAGES[-1]}",
            contract=self._contract(config),
            yes=True,
        )
        job = self._job(config, "apply", command)
        # Apply what fluid-plan published, from its own artifacts, or nothing.
        job["script"].insert(
            -1,
            f"test -f {RELEASE_DIR}/release.json || {{ echo 'fluid-plan published no release "
            "packet: nothing to apply'; exit 1; }",
        )
        job.update(
            {
                # Exactly the plan job's artifacts, of this pipeline: no other
                # job's, and no branch's latest.
                "needs": [{"job": "fluid-plan", "artifacts": True}],
                "dependencies": ["fluid-plan"],
                # A person starts apply; until then the pipeline is blocked,
                # not passed. Protect the environment to choose who may.
                "rules": [
                    {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"', "when": "never"},
                    {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "never"},
                    # The default branch only, a manual (web) run on it included.
                    {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH", "when": "manual"},
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
            "A merge request runs stages 1-6 (fluid-plan) and keeps runtime/ as an artifact;",
            "fluid-impact writes what the change reaches (fluid diff --impact against the",
            "merge request's base) beside it. Its resource section reads unknown, with the",
            "reason, while stage 6 saves no OpenTofu plan.",
            "",
            "On the default branch, fluid-plan also publishes the release packet in",
            "release/ (fluid ship --release-dir) once every stage passed, and fluid-apply",
            "runs stages 7-11 from that job's artifacts when someone starts it, in the",
            f"environment named after FLUID_ENV (default {env_default}). Protect that",
            "environment (Settings > CI/CD > Protected environments) to choose who may.",
            "There, stages 1-4 must run and release/ must not be in the repository: ship",
            "refuses a packet otherwise, and the plan fails with its reason (no packet, no",
            "apply). A merge request and any other ref plan only, without a packet. The",
            "default branch is the project's own ($CI_DEFAULT_BRANCH), whatever",
            "--default-branch said. A fluid-plan that fails keeps its reports and drops its",
            "bundle, plan and artifacts.",
            "",
            "The Run pipeline form is the spec:inputs below (GitLab 17.11 or later). An",
            "input sets the job variable of the same name in upper case; a project CI/CD",
            "variable of that name overrides it. Every other value is a variable default a",
            "project CI/CD variable sets. RUN_STAGE_<N>_<NAME>=false switches a stage off.",
            "",
            "Set FLUID_PACKAGE_SPEC and the FLUID_PIP_* install variables as project",
            "CI/CD variables, and set Settings > CI/CD > Variables > Minimum role to use",
            "pipeline variables to No one allowed (the form needs none): whoever may set",
            "pipeline variables can pass FLUID_PACKAGE_SPEC to a run and choose what apply",
            "installs.",
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
