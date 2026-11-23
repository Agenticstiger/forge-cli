# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""BitbucketTemplate: the thin ``fluid ship`` pipeline for Bitbucket Pipelines.

``pull-requests: '**'`` runs the plan step, ``fluid ship --stage 1-6``. The
default branch runs the plan step, then an apply step, ``fluid ship --stage
7-11 --yes``, that is a ``deployment`` with ``trigger: manual``: the pipeline
waits at it until someone presses Run, and the deployment environment's
permissions and variables apply. The ``custom:`` pipeline ``fluid`` is the
manual run (Run pipeline) with the same two steps. The plan step's
``runtime/`` and ``dist/artifacts/`` are step artifacts, which the apply step
receives and a reviewer can download.

Bitbucket has no step-level environment block, so the shell environment is
built from the repository and deployment variables, with each generated
default applied only where a variable is unset (``export NAME="${NAME:-x}"``,
a checked literal at generation time). Ship reads the result from the
environment; no variable is expanded inside a script body.
"""

from __future__ import annotations

import shlex
from typing import Any, Dict, List, Tuple

from . import _keyless, _thin
from ._base import (
    BasePipelineTemplate,
    PipelineConfig,
    _pin_image,
    check_pipeline_literal,
    check_pipeline_workdir,
)

PIPELINE_PATH = "bitbucket-pipelines.yml"
IMAGE = _pin_image("python:3.12-slim")
CUSTOM_PIPELINE = "fluid"
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


class BitbucketTemplate(BasePipelineTemplate):
    """Bitbucket Pipelines template"""

    def __init__(self) -> None:
        super().__init__()
        self.provider_name = "Bitbucket Pipelines"
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
        add_ons = _thin.check_add_ons(config.add_ons, config.import_drift)
        custom: Dict[str, Any] = {
            CUSTOM_PIPELINE: [
                {"variables": self._prompt(config)},
                self._plan_step(config),
            ]
        }
        # Security beside every plan, the SBOM beside the plan on the default
        # branch (a parallel group: neither waits for the other); compliance
        # (fluid verify, which reaches the cloud, with the same variables apply
        # has) in its own manual pipeline. Bitbucket has no advisory step, so a
        # failed security scan also stops the apply step after it.
        # The import drift gate sits beside the plan on both, as security does.
        beside_pr = [
            self._add_on_step(config, a) for a in add_ons if a in ("security", "import-drift")
        ]
        beside_branch = [self._add_on_step(config, a) for a in add_ons if a != "compliance"]
        if "compliance" in add_ons:
            custom[f"{CUSTOM_PIPELINE}-compliance"] = [self._add_on_step(config, "compliance")]
        pull_requests = self._beside(self._plan_step(config), beside_pr)
        on_branch = [
            *self._beside(self._plan_step(config), beside_branch),
            self._apply_step(config),
        ]
        return {
            "image": IMAGE,
            "pipelines": {
                "pull-requests": {"**": pull_requests},
                "branches": {branch: on_branch},
                "custom": custom,
            },
        }

    @staticmethod
    def _beside(plan: Dict[str, Any], others: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{"parallel": [plan, *others]}] if others else [plan]

    def _prompt(self, config: PipelineConfig) -> List[Dict[str, Any]]:
        """The Run pipeline form: every parameter but the contract path."""
        prompt: List[Dict[str, Any]] = []
        for name, kind, default, description in self._eleven_stage_parameters(config):
            if name == "CONTRACT" or name in _thin.INSTALL_VARIABLES:
                continue  # the contract is written into the commands; install knobs never run-time
            entry: Dict[str, Any] = {"name": name}
            if default:
                entry["default"] = default
            entry["description"] = description
            if kind == "boolean":
                entry["allowed-values"] = ["true", "false"]
            elif kind.startswith("choice:"):
                options = kind.split(":", 1)[1].split(",")
                if "" not in options:  # blank is a value (SCHEDULER: none): free text
                    entry["allowed-values"] = options
            prompt.append(entry)
        for knob in STAGE_KNOBS:
            prompt.append(
                {
                    "name": knob,
                    "description": f"Passed to fluid ship ({knob}); blank keeps ship's default.",
                }
            )
        return prompt

    def _environment_script(self, config: PipelineConfig) -> str:
        """Each generated default, applied where the variable is unset, then the
        run id both steps share. Defaults are checked literals, written inside
        double quotes, so none can expand or end the quoting."""
        lines = ["set -eu"]
        for name, _kind, default, _description in self._eleven_stage_parameters(config):
            if name == "CONTRACT" or not default:
                continue
            safe = check_pipeline_literal(f"{name} default", default)
            lines.append(f'export {name}="${{{name}:-{safe}}}"')
        # One run id for both steps, so their ship stages share one report.
        lines.append('export FLUID_RUN_ID="${FLUID_RUN_ID:-$BITBUCKET_BUILD_NUMBER}"')
        for key, value in self._engine_runtime_env_vars(config).items():
            lines.append(f"export {key}={shlex.quote(str(value))}")
        return "\n".join(lines) + "\n"

    def _step(self, config: PipelineConfig, name: str, spec: str, *, yes: bool) -> Dict[str, Any]:
        command = _thin.ship_command(
            spec, contract=config.contract_path or _thin.DEFAULT_CONTRACT, yes=yes
        )
        cd = f'cd "{check_pipeline_workdir(config.workdir)}"\n' if config.workdir else ""
        return {
            "name": name,
            "image": IMAGE,
            "script": [
                _keyless.LiteralStr(self._environment_script(config)),
                # A subshell: its `set -eu` and `set --` stay out of later lines.
                _keyless.LiteralStr("(\n" + self._render_install_setup(config) + "\n)\n"),
                _keyless.LiteralStr(cd + command + "\n"),
            ],
        }

    def _plan_step(self, config: PipelineConfig) -> Dict[str, Any]:
        step = self._step(config, "fluid plan (stages 1-6)", "1-6", yes=False)
        prefix = f"{check_pipeline_workdir(config.workdir).rstrip('/')}/" if config.workdir else ""
        # What the apply step receives and what a reviewer downloads.
        step["artifacts"] = [f"{prefix}{path}**" for path in _thin.CARRIED_PATHS]
        return {"step": step}

    def _add_on_step(self, config: PipelineConfig, add_on: str) -> Dict[str, Any]:
        cd = f'cd "{check_pipeline_workdir(config.workdir)}"\n' if config.workdir else ""
        script = "\n".join(
            _thin.add_on_script(
                add_on, config.contract_path or _thin.DEFAULT_CONTRACT, config.import_drift
            )
        )
        prefix = f"{check_pipeline_workdir(config.workdir).rstrip('/')}/" if config.workdir else ""
        step: Dict[str, Any] = {
            "name": _thin.ADD_ON_TITLES[add_on],
            "image": IMAGE,
            "script": [
                _keyless.LiteralStr(self._environment_script(config)),
                _keyless.LiteralStr("(\n" + self._render_install_setup(config) + "\n)\n"),
                _keyless.LiteralStr(cd + script + "\n"),
            ],
        }
        if add_on != "import-drift":  # the gate writes nothing
            step["artifacts"] = [f"{prefix}runtime/**"]
        return {"step": step}

    def _deployment(self, config: PipelineConfig) -> str:
        """Bitbucket has test, staging and production until someone adds more."""
        env = self._pipeline_defaults(config)["FLUID_ENV"].lower()
        if env in ("dev", "test"):
            return "test"
        if env in ("staging", "stage"):
            return "staging"
        return "production"

    def _apply_step(self, config: PipelineConfig) -> Dict[str, Any]:
        step = self._step(config, "fluid apply (stages 7-11)", "7-11", yes=True)
        # A person presses Run; until then the pipeline waits. The deployment
        # environment carries its own variables and permissions.
        step["deployment"] = self._deployment(config)
        step["trigger"] = "manual"
        return {"step": step}

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _header(self, config: PipelineConfig) -> str:
        branch = self._branch(config)
        env_default = self._pipeline_defaults(config)["FLUID_ENV"]
        lines = [
            "fluid pipeline, generated by `fluid generate ci --system bitbucket`.",
            "Each step runs `fluid ship --stage ...` (see `fluid ship --help`).",
            "",
            "A pull request runs the plan step (stages 1-6) and keeps runtime/ as a step",
            f"artifact. A push to {branch} runs it too, then waits at the apply step",
            "(stages 7-11), a deployment with trigger: manual that someone starts; you can",
            f"also run the {branch} pipeline by hand on that branch. It never runs on a pull",
            "request. Run pipeline > Custom: fluid is a manual plan only (stages 1-6): it",
            "asks for the values below and cannot apply from any branch.",
            "",
            f"The apply step deploys to the Bitbucket environment `{self._deployment(config)}`, from",
            f"FLUID_ENV's default ({env_default}): dev or test gives test, staging or stage gives",
            "staging, anything else gives production. Create or rename the environment under",
            "Repository settings > Deployments, and restrict it to the default branch there.",
            "",
            "FLUID_PACKAGE_SPEC, FLUID_PIP_INDEX_URL, FLUID_PIP_EXTRA_INDEX_URL and",
            "FLUID_ALLOW_PRERELEASE are not prompts: set them as repository or deployment",
            "variables in the project settings, never at run time.",
            "",
            "Every other value is a repository or deployment variable of the same name;",
            "where it is unset the default written in the first script line of each step",
            "applies. RUN_STAGE_<N>_<NAME>=false switches a stage off (10 and 11 are off",
            "until set to true).",
        ]
        header = "".join(f"# {line}".rstrip() + "\n" for line in lines) + "\n"
        banner = self._credential_banner(
            comment_prefix="# ",
            ci_system_name="Bitbucket Pipelines",
            secret_surface_hint=(
                "Repository settings > Pipelines > Repository variables (or Deployment "
                "variables for env-scoped secrets). Mark Secured for credential values."
            ),
            cloud_auth=config.cloud_auth,
        )
        notes = self._engine_runtime_notes(config)
        return header + banner + (notes + "\n" if notes else "")
