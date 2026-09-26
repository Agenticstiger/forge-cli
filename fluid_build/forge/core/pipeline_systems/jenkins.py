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

"""JenkinsTemplate — per-system template for Jenkins CI.

Extracted from the monolithic ``pipeline_templates.py`` so each CI system's
quirks stay contained. Inherits the 11-stage rendering scaffold from
:class:`fluid_build.forge.core.pipeline_systems._base.BasePipelineTemplate`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Tuple

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
    APPLY_MODES,
    BUNDLE_PATH,
    PINNED_ACTIONS,
    SCHEDULERS,
    BasePipelineTemplate,
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    StageSpec,
    _pin_action,
    apply_build_id_sh,
    check_pipeline_workdir,
    sh_param,
)
from ._engine_specs import (
    render_bootstrap_shell_section,
    render_runner_env_vars,
    render_runtime_notes,
)

#: Jenkins lists APPLY_MODE's choices in this order, the default moved first.
_JENKINS_APPLY_MODE_ORDER: Tuple[str, ...] = (
    "dry-run",
    "amend",
    "create-only",
    "amend-and-build",
    "replace",
    "replace-and-build",
)
assert sorted(_JENKINS_APPLY_MODE_ORDER) == sorted(APPLY_MODES)

#: Where stage 7 records the plan it applied (one file per env), and where
#: stage 0 puts the one the last successful build recorded. Workspace-root
#: relative; stage 0 removes the directory first, so a file committed there
#: can never pose as a baseline.
_CI_STATE_DIR = ".fluid-ci"

_STEP_INDENT = " " * 16


@dataclass(frozen=True)
class _Param:
    """One Jenkins build parameter: its declaration and its shell fallback."""

    name: str
    kind: str  # "boolean" | "string" | "choice"
    default: str
    description: str
    choices: Tuple[str, ...] = ()
    keep_blank: bool = False

    def sh(self) -> str:
        return sh_param(self.name, self.default, keep_blank=self.keep_blank)

    def declaration(self) -> str:
        desc = _groovy_sq(self.description)
        if self.kind == "boolean":
            return (
                f"booleanParam(name: '{self.name}', defaultValue: {self.default},\n"
                f"                     description: {desc})"
            )
        if self.kind == "choice":
            # Jenkins' default for a choice parameter is its first choice.
            ordered = [self.default] + [c for c in self.choices if c != self.default]
            listed = ", ".join(_groovy_sq(c) for c in ordered)
            return (
                f"choice(name: '{self.name}',\n"
                f"               choices: [{listed}],\n"
                f"               description: {desc})"
            )
        return (
            f"string(name: '{self.name}', defaultValue: {_groovy_sq(self.default)},\n"
            f"               description: {desc})"
        )


def _groovy_sq(text: str) -> str:
    """A Groovy single-quoted string literal."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _when_on(param: _Param) -> str:
    """Declarative ``when`` expression for a boolean parameter, with its default.

    Declarative fills ``params`` with the declared defaults even when a build
    carries no parameters, but a job-level parameter of the same name (job-dsl)
    may be a string, so the value is compared as text and a missing one is the
    default.
    """
    return (
        f"(params.{param.name} == null ? {param.default} "
        f": params.{param.name}.toString() == 'true')"
    )


def _sh_step(lines: List[str], cd: str, indent: str = _STEP_INDENT) -> str:
    """One ``sh '''...'''`` step running ``lines`` under ``set -eu``.

    The body is a Groovy ``'''`` string: ``$`` stays literal for the shell, but
    a backslash would be a Groovy escape, so none is allowed.
    """
    for line in lines:
        if "\\" in line or "'''" in line:
            raise ValueError(f"shell line cannot sit in a Groovy ''' string: {line!r}")
    body = "\n".join(f"{indent}    {line}" for line in lines)
    return f"{indent}sh '''{cd}set -eu\n{body}'''"


def _needs_bundle(stage: int) -> str:
    return (
        f'if [ ! -f {BUNDLE_PATH} ]; then echo "stage {stage} reads {BUNDLE_PATH}, '
        'which stage 1 writes: run stage 1 in the same build" >&2; exit 1; fi'
    )


class JenkinsTemplate(BasePipelineTemplate):
    """Jenkins pipeline template — 11-stage parameterized Jenkinsfile.

    Produces a fully-parameterized declarative pipeline mirroring the
    perfect-pipeline 11-stage design. Every stage has its own
    ``RUN_STAGE_N_NAME`` boolean toggle + per-stage configuration (apply
    mode, publish targets, diff drift behavior, etc.) exposed as Jenkins
    build parameters so operators can run any subset of the pipeline
    from the "Build With Parameters" UI without editing Groovy.

    Core operating modes the parameters support out of the box:

    * **Structural dry-run** (bundle → validate → generate → validate
      artifacts → diff → plan → apply ``--mode dry-run``) — zero
      warehouse writes (stage 8 only checks the bindings, stage 9 has
      nothing to verify). Safe for every PR. The default.
    * **Schema deploy** (above + apply ``--mode amend`` + policy-apply
      + verify). Stage 10 publish and stage 11 schedule-sync off.
    * **Full productionization** (all 11 stages on, apply
      ``--mode amend-and-build`` with the contract's build id, publish to
      a list of catalogs, schedule-sync DAGs to the scheduler).
    * **Destructive replace** (apply ``--mode replace`` +
      ``ALLOW_DATA_LOSS=true``). Auto-snapshot before drop.

    The chain: stage 1 bundles the contract with ``--env``; stages 2, 3,
    5, 6 and 9 read that bundle; stage 6 plans for the APPLY_MODE stage 7
    applies, and stage 7 applies stage 6's plan with ``--bundle``. Stage
    10 publishes the contract with ``--env``.

    Parameters come from one table (``_parameters``): its values are the
    ``parameters {}`` defaults AND every shell's fallback, because Jenkins
    runs a job's first build, and its first after it lost its parameter
    definitions, with no parameters in the environment.

    Back-compat: the legacy ``generates_artifacts: False`` (reference-only
    contracts) and ``workdir: "..."`` (subfolder checkout) config flags
    still work — stage 3 is skipped when the contract declares itself
    reference-only, and every sh block is wrapped with ``cd "<workdir>"``
    when workdir is set.
    """

    system_apply_mode_default = "dry-run"

    def __init__(self):
        super().__init__()
        self.provider_name = "Jenkins"
        self.file_extensions = [".groovy"]

    # ── Parameter table ─────────────────────────────────────────────────

    def _parameters(self, config: PipelineConfig) -> Dict[str, _Param]:
        """Every build parameter, in declaration order, with its default."""
        d = self._pipeline_defaults(config)
        keep = self.keep_blank_parameters
        install_mode = config.install_mode or "pypi"
        rows: List[_Param] = [
            _Param(
                "CONTRACT",
                "string",
                d["CONTRACT"],
                "Contract path relative to the workspace (or workdir when set).",
            ),
            _Param(
                "FLUID_ENV",
                "string",
                d["FLUID_ENV"],
                "Environment overlay (dev | staging | prod | ...). Stage 1 bundles the "
                "contract with it; every later stage checks the bundle is for it.",
            ),
        ]
        if install_mode == "pypi":
            rows += [
                _Param(
                    "FLUID_PACKAGE_SPEC",
                    "string",
                    d["FLUID_PACKAGE_SPEC"],
                    "Package spec stage 0 installs into the workspace venv. Defaults to the "
                    "forge-cli that generated this file, with the extras its contract needs.",
                ),
                _Param(
                    "FLUID_PIP_INDEX_URL",
                    "string",
                    d["FLUID_PIP_INDEX_URL"],
                    "Primary pip index. Leave blank for stable PyPI; set "
                    "'https://test.pypi.org/simple/' for TestPyPI pilot builds, or your "
                    "private mirror URL.",
                    keep_blank=True,
                ),
                _Param(
                    "FLUID_PIP_EXTRA_INDEX_URL",
                    "string",
                    d["FLUID_PIP_EXTRA_INDEX_URL"],
                    "Fallback pip index. Usually 'https://pypi.org/simple/' when PRIMARY "
                    "points at TestPyPI so transitive deps still resolve.",
                    keep_blank=True,
                ),
                _Param(
                    "FLUID_ALLOW_PRERELEASE",
                    "boolean",
                    d["FLUID_ALLOW_PRERELEASE"],
                    "Pass pip --pre (pulls alpha/rc releases). Leave false in prod.",
                ),
            ]
        toggles = {spec.num: spec for spec in self._stage_specs(config)}

        def toggle(num: int, description: str) -> _Param:
            spec = toggles[num]
            on = self._stage_default_run(spec, config)
            return _Param(spec.toggle_param, "boolean", "true" if on else "false", description)

        rows += [
            toggle(
                1, "Stage 1: deterministic tgz bundle + MANIFEST.json (SHA-256), for FLUID_ENV."
            ),
            toggle(
                2,
                "Stage 2: validators on the bundle (schema + contract rules + sqlglot + openapi).",
            ),
            _Param(
                "VALIDATE_STRICT",
                "boolean",
                "true",
                "Stage 2: --strict (any validator error fails the pipeline).",
            ),
            toggle(
                3,
                "Stage 3: ODCS + ODPS-Bitol + schedule + policy fanout from the bundle. "
                "Off for reference-only contracts.",
            ),
            _Param(
                "GENERATE_EMIT",
                "string",
                "odcs,odps-bitol,schedule,policies",
                "Stage 3 --emit list (comma-separated). dbt excluded by design (execution artifact).",
            ),
            toggle(4, "Stage 4: re-verify MANIFEST SHA-256 + per-format schema validators."),
            toggle(5, "Stage 5: compare the bundled contract with the live target (drift gate)."),
            _Param(
                "DIFF_EXIT_ON_DRIFT",
                "boolean",
                "true",
                "Stage 5: --exit-on-drift (hard-fail if drift detected).",
            ),
            toggle(6, "Stage 6: plan the bundle for APPLY_MODE; emits bundleDigest + planDigest."),
            _Param(
                "PLAN_HTML",
                "boolean",
                "true",
                "Stage 6: also write runtime/plan.html, a visualization of the plan.",
            ),
            toggle(
                7,
                "Stage 7: apply stage 6's plan with the bundle (mode matrix; "
                "plan-binding cryptographically verified).",
            ),
            _Param(
                "APPLY_MODE",
                "choice",
                d["APPLY_MODE"],
                "Stages 6 and 7: the mode planned and applied. dry-run = render only (safe); "
                "amend = additive; *-and-build also runs the contract's builds; replace = "
                "DROP+CREATE (requires ALLOW_DATA_LOSS in non-dev).",
                choices=_JENKINS_APPLY_MODE_ORDER,
            ),
            _Param(
                "APPLY_BUILD_ID",
                "string",
                d["APPLY_BUILD_ID"],
                "Stage 7: the build amend-and-build / replace-and-build runs (blank runs "
                "every build). Not passed with any other mode.",
                keep_blank="APPLY_BUILD_ID" in keep,
            ),
            _Param(
                "ALLOW_DATA_LOSS",
                "boolean",
                d["ALLOW_DATA_LOSS"],
                "Stage 7: gate waiver for --mode replace* in non-dev or when target has rows.",
            ),
            _Param(
                "NO_VERIFY_DIGEST",
                "boolean",
                d["NO_VERIFY_DIGEST"],
                "Stage 7: DR emergency escape — waives BOTH the plan-binding and federation "
                "upstream-digest gates (--no-verify-plan-binding --no-verify-federation). "
                "Use only when the original bundle / upstreams are unreachable.",
            ),
            toggle(
                8,
                "Stage 8: enforce IAM/GRANT bindings (self-gated on bindings.json presence; "
                "checked, not enforced, after a dry-run apply).",
            ),
            _Param(
                "POLICY_APPLY_MODE",
                "choice",
                "enforce",
                "Stage 8: enforce = apply GRANTs; check = dry-run / PR report only.",
                choices=("enforce", "check"),
            ),
            toggle(
                9,
                "Stage 9: post-apply reconciliation of the bundle vs the live target "
                "(skipped after a dry-run apply).",
            ),
            _Param(
                "VERIFY_STRICT",
                "boolean",
                "true" if config.verify_strict_default else "false",
                "Stage 9: --strict (fail on any schema mismatch, including silent type coercions).",
            ),
            toggle(
                10,
                "Stage 10: push catalog artifacts to one or more targets. Opt-in — typically "
                "gated to main branch.",
            ),
            _Param(
                "PUBLISH_TARGETS",
                "string",
                d["PUBLISH_TARGETS"],
                "Stage 10: space-separated publish targets (fluid-command-center datahub "
                "datamesh-manager collibra ...).",
            ),
            toggle(
                11,
                "Stage 11: push generated DAGs to scheduler (airflow / mwaa / composer / "
                "astronomer / prefect / dagster).",
            ),
            _Param(
                "SCHEDULER",
                "choice",
                d["SCHEDULER"],
                "Stage 11 scheduler target. Blank = no-op.",
                choices=("",) + SCHEDULERS,
                keep_blank=True,
            ),
            _Param(
                "SCHEDULER_DESTINATION",
                "string",
                d["SCHEDULER_DESTINATION"],
                "Stage 11: airflow/mwaa DAG root. Supports s3://, gs://, az://, ssh://, scp://, "
                "file:// or a bare path; this product's DAGs go to <root>/<contract id>/ and "
                "nothing outside that directory is deleted. Required for airflow + mwaa; "
                "ignored for composer / astronomer / prefect / dagster.",
                keep_blank=True,
            ),
            _Param(
                "SCHEDULER_ENVIRONMENT_NAME",
                "string",
                d["SCHEDULER_ENVIRONMENT_NAME"],
                "Stage 11: composer environment name or astronomer deployment name.",
                keep_blank=True,
            ),
            _Param(
                "SCHEDULER_LOCATION",
                "string",
                d["SCHEDULER_LOCATION"],
                "Stage 11: GCP region for composer (e.g. europe-west1, us-central1).",
                keep_blank=True,
            ),
            _Param(
                "SCHEDULER_WORKSPACE",
                "string",
                d["SCHEDULER_WORKSPACE"],
                "Stage 11: prefect workspace or dagster-cloud deployment name.",
                keep_blank=True,
            ),
            _Param(
                "SCHEDULE_SYNC_DRY_RUN",
                "boolean",
                d["SCHEDULE_SYNC_DRY_RUN"],
                "Stage 11: --dry-run (log the planned subprocess argv without executing).",
            ),
        ]
        return {row.name: row for row in rows}

    # ── Rendering ───────────────────────────────────────────────────────

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate the 11-stage parameterized Jenkinsfile.

        Returns a ``{"Jenkinsfile": <content>}`` dict matching the
        ``BasePipelineTemplate`` contract.
        """
        install_mode = config.install_mode or "pypi"
        if install_mode not in ("pypi", "dev-source"):
            # Defensive: unknown install_mode. Caller passed something
            # we don't support — raise NOW (at generate time) rather
            # than emit a broken Jenkinsfile that confuses CI later.
            raise ValueError(
                f"Unknown install_mode {install_mode!r} — expected 'pypi' or 'dev-source'"
            )
        P = self._parameters(config)

        def v(name: str) -> str:
            """The shell read of parameter ``name``, with its declared default."""
            return P[name].sh()

        # ``cd "<workdir>" && `` prefix for every sh block when the
        # contract lives in a subfolder of the SCM checkout. Jenkins
        # checks out at repo root; fluid needs to run from the contract
        # folder. Every sh block uses the triple-single ``sh '''...'''``
        # form so double-quoted paths inside don't collide with outer
        # string delimiters, and Jenkins params reach the shell as the
        # environment variables Jenkins exports for them, each read with
        # its declared default, never by Groovy interpolation.
        workdir = (config.workdir or "").strip("/")
        if workdir:
            check_pipeline_workdir(workdir)
        CD = f'cd "{workdir}" && ' if workdir else ""
        # Archive patterns are rooted at the SCM root (the Jenkins workspace),
        # so every glob gets the workdir prefix. ``allowEmptyArchive: true``
        # on every archiveArtifacts handles reference-only contracts that
        # legitimately produce no plan.json / artifacts/ / reports.
        W = f"{workdir}/" if workdir else ""
        env_flag = f'--env "{v("FLUID_ENV")}"'
        # The DAG stage 3 renders applies this path relative to the checkout
        # (FLUID_PROJECT_DIR on the Airflow worker); a bundle does not record it.
        project_contract = f"{W}{v('CONTRACT')}"
        last_applied = bool(config.diff_last_applied)
        applied_dir = f"$WORKSPACE/{_CI_STATE_DIR}/applied"
        baseline_dir = f"$WORKSPACE/{_CI_STATE_DIR}/last-applied"
        dry_run_applied = (
            f'[ "{v("RUN_STAGE_7_APPLY")}" = "true" ] && [ "{v("APPLY_MODE")}" = "dry-run" ]'
        )

        def skip_after_dry_run(stage: int, what: str) -> List[str]:
            # A dry-run build writes nothing: not the target, not the bindings,
            # not the catalog, not the scheduler (whose DAG would apply for real).
            return [
                f"if {dry_run_applied}; then",
                (
                    f'  echo "stage {stage}: stage 7 ran as a dry run and applied nothing, so '
                    f'{what} — skipped (APPLY_MODE amend or amend-and-build applies)"'
                ),
                "  exit 0",
                "fi",
            ]

        def when(num: int, extra: str = "") -> str:
            toggle_name = {spec.num: spec.toggle_param for spec in self._stage_specs(config)}[num]
            expression = _when_on(P[toggle_name])
            if extra:
                expression = f"{expression} && {extra}"
            return f"            when {{ expression {{ return {expression} }} }}"

        # ── Stage 0: install ────────────────────────────────────────────
        if install_mode == "pypi":
            setup_install = (
                "                // Install the fluid CLI into a virtual environment in the\n"
                "                // workspace (PEP 668 agents refuse a bare `pip install`), and\n"
                "                // run every later stage from it: FLUID_VENV and PATH in the\n"
                "                // pipeline's environment {} block. Four parameters override\n"
                "                // the install from the Build-With-Parameters dialog:\n"
                "                //   FLUID_PACKAGE_SPEC         package spec (defaults to the\n"
                "                //                              forge-cli that generated this file)\n"
                "                //   FLUID_PIP_INDEX_URL        primary index (blank = PyPI)\n"
                "                //   FLUID_PIP_EXTRA_INDEX_URL  fallback index\n"
                "                //   FLUID_ALLOW_PRERELEASE     'true' → add --pre\n"
                "                // Each value is ONE pip argument (set --, `--opt=value`, and\n"
                "                // `--` before the spec): a value cannot add pip options.\n"
                + _sh_step(
                    [
                        'rm -rf "$FLUID_VENV"',
                        'python3 -m venv "$FLUID_VENV"',
                        "set -- --quiet --disable-pip-version-check",
                        f'INDEX_URL="{v("FLUID_PIP_INDEX_URL")}"',
                        f'EXTRA_INDEX_URL="{v("FLUID_PIP_EXTRA_INDEX_URL")}"',
                        'if [ -n "$INDEX_URL" ]; then set -- "$@" "--index-url=$INDEX_URL"; fi',
                        (
                            'if [ -n "$EXTRA_INDEX_URL" ]; then '
                            'set -- "$@" "--extra-index-url=$EXTRA_INDEX_URL"; fi'
                        ),
                        (
                            f'if [ "{v("FLUID_ALLOW_PRERELEASE")}" = "true" ]; then '
                            'set -- "$@" --pre; fi'
                        ),
                        f'"$FLUID_VENV/bin/python" -m pip install "$@" -- "{v("FLUID_PACKAGE_SPEC")}"',
                    ],
                    "",
                )
            )
            pip_executable = '"$FLUID_VENV/bin/python" -m pip'
        else:
            # install-mode=dev-source uses PYTHONPATH=/forge-cli-src to
            # point Python at the bind mount LIVE — no pip install. That
            # sidesteps a pile of wheel-cache / stale-file bugs that made
            # ``pip install /forge-cli-src`` unreliable in practice.
            # The PYTHONPATH export happens in the pipeline-level
            # ``environment {}`` block (added below in dev-source mode),
            # so every downstream sh step inherits it automatically.
            setup_install = """                sh '''set -e
                      if [ ! -d /forge-cli-src ] || [ ! -f /forge-cli-src/pyproject.toml ]; then
                        cat >&2 <<EOM

ERROR: This Jenkinsfile has install-mode=dev-source but /forge-cli-src
       is not mounted in the Jenkins container.

       To fix, add this to deploy/docker/docker-compose.yml under the
       jenkins service's volumes block:

         - \\\\${FORGE_CLI_REPO:-../../../forge-cli}:/forge-cli-src:ro

       Then: docker compose restart jenkins

       OR regenerate this Jenkinsfile for production use:

         fluid generate ci --system jenkins --out Jenkinsfile
         # (defaults to --install-mode pypi)

EOM
                        exit 2
                      fi
                      # Do NOT `pip uninstall data-product-forge` here: the
                      # package's ``fluid`` entry-point IS the command on PATH,
                      # and PYTHONPATH=/forge-cli-src (prepended, exported in the
                      # environment {} block) already wins over site-packages for
                      # every ``import fluid_build`` — so the bind-mounted checkout
                      # shadows the installed modules while the console script
                      # keeps ``fluid`` callable. A prior version uninstalled it
                      # "to avoid shadowing", which removed the ``fluid`` command
                      # itself and broke stage 0 with ``fluid: not found``. Mirror
                      # the non-Jenkins runners' dev-source setup (_render_install_setup)
                      # which keeps the console script and only overrides via PYTHONPATH.
                      python -c "import fluid_build" || (echo 'FATAL: fluid_build import failed; check /forge-cli-src bind mount' >&2 && exit 3)
                      echo "install-mode=dev-source — fluid command via the installed console script; imports resolve from /forge-cli-src via PYTHONPATH"'''"""
            pip_executable = "pip"

        # ── Engine-aware bootstrap (uses the shared registry) ─────────────
        # Pulls per-engine pip extras from
        # ``fluid_build.forge.core.pipeline_systems._engine_specs`` so this
        # template stays small and the same engine-resolution logic is
        # reused across every CI emitter (github_actions, gitlab_ci,
        # tekton, …). Empty string when the contract has no engine
        # declared (engine-agnostic Jenkinsfile, no extras installed).
        # In pypi mode it installs into the workspace venv.
        engine_bootstrap_sh = render_bootstrap_shell_section(
            engine=getattr(config, "engine", None),
            source_kind=getattr(config, "source_kind", None),
            sink_platform=getattr(config, "sink_platform", None),
            pip_executable=pip_executable,
            indent="                      ",  # matches sh ''' ''' nesting
        )
        # Wrap in a single ``sh '''...'''`` step so it runs as ONE shell
        # invocation (preserves env across pip lines, fails loud on first
        # error). When the registry returns nothing we emit no extra
        # shell step — the Setup stage stays clean.
        if engine_bootstrap_sh:
            engine_bootstrap_step = (
                f"                sh '''set -e\n{engine_bootstrap_sh}\n                '''"
            )
        else:
            engine_bootstrap_step = ""

        version_lines = []
        if install_mode == "pypi":
            # The fluid every later stage runs must be the one just installed,
            # not one the agent image already has on PATH.
            version_lines = [
                'if [ "$(command -v fluid || true)" != "$FLUID_VENV/bin/fluid" ]; then',
                (
                    '  echo "stage 0: the CLI first on PATH is $(command -v fluid || echo nothing), '
                    'not the one in $FLUID_VENV: the PATH entry of environment {} did not apply" >&2'
                ),
                "  exit 1",
                "fi",
            ]
        version_step = _sh_step(
            [
                # FLUID_ENV names an overlay file, and a baseline file below;
                # refuse anything but a plain name before any stage uses it.
                f'case "{v("FLUID_ENV")}" in',
                (
                    '  .*|-*|*[!A-Za-z0-9_.-]*) echo "FLUID_ENV must be a plain environment name '
                    '([A-Za-z0-9_.-], not starting with . or -)" >&2; exit 2 ;;'
                ),
                "esac",
                *version_lines,
                "fluid --version",
            ],
            CD,
        )
        baseline_fetch = ""
        if last_applied:
            baseline_fetch = (
                "\n                // Stage 5's baseline: the plan the last successful build\n"
                "                // applied for each env, from that build's own artifacts\n"
                "                // (copyartifact plugin; optional, so a first run has none).\n"
                "                // The directory is emptied first: a file committed to the\n"
                "                // repository at that path must never pose as a baseline.\n"
                f"                sh 'rm -rf \"$WORKSPACE/{_CI_STATE_DIR}\"'\n"
                "                script {\n"
                "                    copyArtifacts(projectName: env.JOB_NAME,\n"
                "                                  selector: lastSuccessful(),\n"
                f"                                  filter: '{_CI_STATE_DIR}/applied/*.json',\n"
                f"                                  target: '{_CI_STATE_DIR}/last-applied',\n"
                "                                  flatten: true,\n"
                "                                  optional: true,\n"
                "                                  fingerprintArtifacts: true)\n"
                "                }"
            )

        # ── Stages 1-11 ─────────────────────────────────────────────────
        stage1 = _sh_step(
            [
                "mkdir -p runtime",
                f'fluid bundle "{v("CONTRACT")}" {env_flag} --format tgz --out {BUNDLE_PATH}',
            ],
            CD,
        )
        stage2 = _sh_step(
            [
                "mkdir -p runtime",
                _needs_bundle(2),
                f"set -- {BUNDLE_PATH} {env_flag} --report runtime/validate-report.json",
                f'if [ "{v("VALIDATE_STRICT")}" = "true" ]; then set -- "$@" --strict; fi',
                'fluid validate "$@"',
            ],
            CD,
        )
        stage3 = _sh_step(
            [
                _needs_bundle(3),
                (
                    f"fluid generate artifacts {BUNDLE_PATH} {env_flag} "
                    f'--contract-path "{project_contract}" '
                    f'--out dist/artifacts/ --emit "{v("GENERATE_EMIT")}"'
                ),
            ],
            CD,
        )
        stage4 = _sh_step(
            [
                "mkdir -p runtime",
                (
                    "fluid validate-artifacts dist/artifacts/ "
                    "--manifest dist/artifacts/MANIFEST.json "
                    "--report runtime/validate-artifacts-report.json"
                ),
            ],
            CD,
        )
        stage5_lines = [
            "mkdir -p runtime",
            _needs_bundle(5),
            f"set -- {BUNDLE_PATH} {env_flag} --out runtime/diff-report.json",
            f'if [ "{v("DIFF_EXIT_ON_DRIFT")}" = "true" ]; then set -- "$@" --exit-on-drift; fi',
        ]
        if last_applied:
            stage5_lines += [
                f'BASELINE="{baseline_dir}/{v("FLUID_ENV")}.json"',
                (
                    'if [ -f "$BASELINE" ]; then set -- "$@" --last-applied "$BASELINE"; '
                    'else echo "stage 5: the last successful build applied no plan for this env '
                    '(or there is none yet): comparing the target with the contract alone"; fi'
                ),
            ]
        stage5_lines.append('fluid diff "$@"')
        stage5 = _sh_step(stage5_lines, CD)
        stage6 = _sh_step(
            [
                "mkdir -p runtime",
                _needs_bundle(6),
                (
                    f"set -- {BUNDLE_PATH} {env_flag} "
                    f'--mode "{v("APPLY_MODE")}" --out runtime/plan.json'
                ),
                (
                    f'if [ "{v("PLAN_HTML")}" = "true" ]; then '
                    'set -- "$@" --html runtime/plan.html; fi'
                ),
                'fluid plan "$@"',
            ],
            CD,
        )
        stage7_lines = [
            "mkdir -p runtime",
            _needs_bundle(7),
            f'MODE="{v("APPLY_MODE")}"',
            (
                f'set -- runtime/plan.json --bundle {BUNDLE_PATH} --mode "$MODE" {env_flag} '
                "--yes --ensure-opentofu --report runtime/apply-report.html"
            ),
            apply_build_id_sh(v("APPLY_BUILD_ID")).strip(),
            f'if [ "{v("ALLOW_DATA_LOSS")}" = "true" ]; then set -- "$@" --allow-data-loss; fi',
            (
                f'if [ "{v("NO_VERIFY_DIGEST")}" = "true" ]; then '
                'set -- "$@" --no-verify-plan-binding --no-verify-federation; fi'
            ),
            'fluid apply "$@"',
        ]
        if last_applied:
            stage7_lines += [
                # Recorded only once apply succeeded (set -e), and only when
                # it changed the target: a dry run applied nothing.
                'if [ "$MODE" != "dry-run" ]; then',
                f'  mkdir -p "{applied_dir}"',
                f'  cp runtime/plan.json "{applied_dir}/{v("FLUID_ENV")}.json"',
                "fi",
            ]
        stage7 = _sh_step(stage7_lines, CD)
        stage8 = _sh_step(
            [
                "mkdir -p runtime",
                f'POLICY_MODE="{v("POLICY_APPLY_MODE")}"',
                f'if {dry_run_applied} && [ "$POLICY_MODE" = "enforce" ]; then',
                (
                    '  echo "stage 8: stage 7 ran as a dry run and wrote nothing, so the bindings '
                    'are checked (--mode check), not enforced"'
                ),
                "  POLICY_MODE=check",
                "fi",
                "if [ -f dist/artifacts/policy/bindings.json ]; then",
                '  set -- dist/artifacts/policy/bindings.json --mode "$POLICY_MODE"',
                '  if fluid policy-apply "$@" > runtime/policy-apply-report.json 2>&1; then',
                "    cat runtime/policy-apply-report.json",
                "  else",
                "    cat runtime/policy-apply-report.json; exit 1",
                "  fi",
                "else",
                '  echo "no dist/artifacts/policy/bindings.json — skipping stage 8"',
                "fi",
            ],
            CD,
        )
        stage9 = _sh_step(
            [
                *skip_after_dry_run(9, "there is nothing to verify"),
                "mkdir -p runtime",
                _needs_bundle(9),
                f"set -- {BUNDLE_PATH} {env_flag} --out runtime/verify-report.json",
                f'if [ "{v("VERIFY_STRICT")}" = "true" ]; then set -- "$@" --strict; fi',
                'fluid verify "$@"',
            ],
            CD,
        )
        publish_env = f" {env_flag}" if config.publish_include_env else ""
        stage10 = _sh_step(
            [
                *skip_after_dry_run(10, "there is no applied product to publish"),
                "mkdir -p runtime",
                f'set -- "{v("CONTRACT")}"{publish_env} --format json',
                # PUBLISH_TARGETS is a space-separated list: each word is ONE
                # `--target=<word>` argument, and `set -f` keeps a word from
                # being a glob. A word may not carry an endpoint
                # (`name:https://...`): the agent's catalog credential
                # (FLUID_API_KEY...) would go to whatever the parameter names.
                "set -f",
                f"for t in {v('PUBLISH_TARGETS')}; do",
                (
                    '  case "$t" in *:*) echo "PUBLISH_TARGETS names catalogs, not endpoints: '
                    'set the endpoint on the agent (FLUID_CC_ENDPOINT...)" >&2; exit 2 ;; esac'
                ),
                '  set -- "$@" "--target=$t"',
                "done",
                "set +f",
                "rc=0",
                'fluid publish "$@" > runtime/publish-report.json || rc=$?',
                "cat runtime/publish-report.json",
                'exit "$rc"',
            ],
            CD,
        )
        stage11 = _sh_step(
            [
                *skip_after_dry_run(11, "there is no applied product to schedule"),
                f'SCHEDULER_V="{v("SCHEDULER")}"',
                'if [ -z "$SCHEDULER_V" ]; then',
                '  echo "SCHEDULER is blank: no scheduler to sync to — skipping stage 11"',
                "  exit 0",
                "fi",
                (
                    "if [ ! -d dist/artifacts/schedule ] || "
                    '[ -z "$(ls -A dist/artifacts/schedule 2>/dev/null)" ]; then'
                ),
                (
                    '  echo "no dist/artifacts/schedule/ DAGs to sync — skipping stage 11 '
                    "(reference-only contract, stage 3 not run, or no scheduled build or "
                    'orchestration.engine)"'
                ),
                "  exit 0",
                "fi",
                "mkdir -p runtime",
                # --delete-scope product: this product's DAGs go to
                # <destination>/<contract id>/ and nothing outside it is deleted,
                # so every product's job can share one DAG root.
                (
                    'set -- --scheduler "$SCHEDULER_V" --dags-dir dist/artifacts/schedule/ '
                    f"{env_flag} --delete-scope product --report runtime/schedule-sync-report.json"
                ),
                f'DEST="{v("SCHEDULER_DESTINATION")}"',
                'if [ -n "$DEST" ]; then set -- "$@" --destination "$DEST"; fi',
                f'ENV_NAME="{v("SCHEDULER_ENVIRONMENT_NAME")}"',
                'if [ -n "$ENV_NAME" ]; then set -- "$@" --environment-name "$ENV_NAME"; fi',
                f'LOCATION="{v("SCHEDULER_LOCATION")}"',
                'if [ -n "$LOCATION" ]; then set -- "$@" --location "$LOCATION"; fi',
                f'WORKSPACE_NAME="{v("SCHEDULER_WORKSPACE")}"',
                'if [ -n "$WORKSPACE_NAME" ]; then set -- "$@" --workspace "$WORKSPACE_NAME"; fi',
                f'if [ "{v("SCHEDULE_SYNC_DRY_RUN")}" = "true" ]; then set -- "$@" --dry-run; fi',
                'fluid schedule-sync "$@"',
            ],
            CD,
        )

        # ── Pipeline-level blocks ───────────────────────────────────────
        parameters_block = (
            "\n    parameters {\n"
            + "\n".join(f"        {p.declaration()}" for p in P.values())
            + "\n    }"
        )

        options_lines = [
            "        disableConcurrentBuilds()",
            "        buildDiscarder(logRotator(numToKeepStr: '20'))",
        ]
        if last_applied:
            options_lines += [
                "        // copyartifact runs in Production mode: a job may copy its own",
                "        // artifacts (stage 0's baseline) only when it says so.",
                '        copyArtifactPermission("/${env.JOB_NAME}")',
            ]

        env_lines = [
            "        FLUID_LOG_LEVEL = 'INFO'",
            "        FLUID_CONFIG_PATH = './fluid_config'",
        ]
        if install_mode == "pypi":
            env_lines += [
                "        // Stage 0 installs fluid here; every stage runs it from here.",
                '        FLUID_VENV = "${env.WORKSPACE}/.fluid-venv"',
                '        PATH = "${env.WORKSPACE}/.fluid-venv/bin:${env.PATH}"',
            ]
        else:
            # dev-source: imports resolve LIVE from the bind mount, so every
            # sh step in every stage inherits PYTHONPATH (Jenkins expands
            # ``environment {}`` as env vars for every sh invocation).
            env_lines.append("        PYTHONPATH = '/forge-cli-src'")
        # Container-runtime env vars (e.g. FLUID_RUNNER_HOST_OVERRIDE).
        # Same pattern: shared registry → CI-emitter-agnostic dict →
        # Jenkins ``environment {}`` block. Empty dict → no extra lines.
        for key, value in render_runner_env_vars(
            runner_host_override=getattr(config, "runner_host_override", "") or "",
            engine=getattr(config, "engine", None),
        ).items():
            # Jenkins env-block syntax: ``KEY = 'value'`` (single-quoted
            # so values with literal $ aren't expanded).
            env_lines.append(f"        {key} = {_groovy_sq(str(value))}")
        env_block = "\n".join(env_lines)

        # Per-engine runtime notes (docker socket, external services, ...).
        # Sourced from the same registry as engine_bootstrap_step + runner
        # env vars; rendered as Jenkinsfile ``//`` comments above the
        # ``environment {}`` block so operators see the requirements
        # before they hit a runtime error. Empty when the engine has no
        # special runtime needs (dlt / meltano / dbt / duckdb).
        runtime_notes_text = render_runtime_notes(
            engine=getattr(config, "engine", None),
            indent="    // ",  # 4-sp indent matches `pipeline {` inner blocks
        )
        if runtime_notes_text:
            runtime_notes_block = (
                "\n    // ── Engine runtime requirements (from the shared "
                "engine_specs registry) ──\n"
                f"{runtime_notes_text}\n"
            )
        else:
            runtime_notes_block = ""

        post_success = []
        if last_applied:
            post_success = [
                "            // Carry forward every env's applied plan this build did not",
                "            // replace, so the next build's baseline is the last apply per env",
                "            // even when this one applied nothing.",
                _sh_step(
                    [
                        f'mkdir -p "{applied_dir}"',
                        f'for f in "{baseline_dir}"/*.json; do',
                        '  [ -f "$f" ] || continue',
                        f'  [ -f "{applied_dir}/$(basename "$f")" ] || cp "$f" "{applied_dir}/"',
                        "done",
                    ],
                    "",
                    indent=" " * 12,
                ),
                (
                    f"            archiveArtifacts artifacts: '{_CI_STATE_DIR}/applied/*.json', "
                    "fingerprint: true, allowEmptyArchive: true"
                ),
            ]
        post_success.append("            echo '✅ 11-stage pipeline completed successfully'")
        post_success_block = "\n".join(post_success)

        stage3_when = when(3)
        stage4_when = when(4, f"fileExists('{W}dist/artifacts/MANIFEST.json')")

        jenkins_pipeline = f"""
pipeline {{
    // Default to any available agent. Change to `label 'your-label'`
    // if you have a dedicated FLUID-equipped agent pool.
    agent any

    options {{
{chr(10).join(options_lines)}
    }}
{parameters_block}{runtime_notes_block}
    environment {{
{env_block}
        // ── Provider credential bindings (pick ONE pattern) ──────
        // See the top-of-file banner for the full env-var list per
        // provider.
        //
        // Path 1 — agent env passthrough. Set the env vars on the
        // Jenkins agent/container (docker-compose `environment:`,
        // Kubernetes agent template, or Jenkins Global Node
        // Properties). `sh` steps inherit them automatically; no
        // changes needed here.
        //
        // Path 2 — Jenkins credential store. After creating
        // `string` credentials in Jenkins, uncomment + adapt:
        //
        //   <PROVIDER_ENV_VAR> = credentials('<your-credential-id>')
        //
        // e.g. Snowflake:  SNOWFLAKE_ACCOUNT = credentials('snowflake-account')
        //      GCP:        GOOGLE_APPLICATION_CREDENTIALS = credentials('gcp-sa-key')
        //      AWS:        AWS_ACCESS_KEY_ID = credentials('aws-access-key')
        //                  AWS_SECRET_ACCESS_KEY = credentials('aws-secret-key')
        //
        // Catalog publish (only if using `fluid publish`):
        //   FLUID_API_KEY = credentials('command-center-api-key')
        //   DMM_API_URL = credentials('dmm-api-url')
        //   DMM_API_KEY = credentials('dmm-api-key')
    }}

    stages {{
        stage('0 — Bootstrap FLUID [{install_mode}]') {{
            steps {{
{setup_install}
{engine_bootstrap_step}
{version_step}{baseline_fetch}
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 1 — bundle (structural)
        // Deterministic .tgz + MANIFEST.json (SHA-256 merkle root) of the
        // contract with its FLUID_ENV overlay. Root of trust for every
        // downstream stage: 2, 3, 5, 6 and 9 read it, 7 applies with it.
        // ═════════════════════════════════════════════════════════════
        stage('1 - bundle') {{
{when(1)}
            steps {{
{stage1}
                archiveArtifacts artifacts: '{W}{BUNDLE_PATH}', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 2 — validate (structural)
        // Extension-routed: schema + contract rules + sqlglot (SQL) +
        // openapi-spec-validator, on the bundle. Fail early, fail loud.
        // ═════════════════════════════════════════════════════════════
        stage('2 - validate') {{
{when(2)}
            steps {{
{stage2}
                archiveArtifacts artifacts: '{W}runtime/validate-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 3 — generate artifacts (structural)
        // ODCS + ODPS-Bitol + schedule + policy fanout. dbt excluded.
        // Auto-skipped for hybrid-reference contracts.
        // ═════════════════════════════════════════════════════════════
        stage('3 - generate artifacts') {{
{stage3_when}
            steps {{
{stage3}
                archiveArtifacts artifacts: '{W}dist/artifacts/**/*', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 4 — validate artifacts (structural)
        // Re-verifies MANIFEST SHA-256 + per-format schema validators.
        // Defence-in-depth against in-flight CI tampering.
        // ═════════════════════════════════════════════════════════════
        stage('4 - validate artifacts') {{
            // Self-gate: stage 4 re-verifies the output of stage 3
            // (generate artifacts). When stage 3 was skipped — either
            // because the contract is reference-only (RUN_STAGE_3_*
            // default False) or because the operator unchecked it —
            // ``dist/artifacts/`` won't exist and this stage would
            // hard-fail with ``validate_artifacts_input_missing``,
            // cascading into skipping every downstream stage.
            //
            // Fix: skip stage 4 when either (a) the run-toggle is
            // off, OR (b) the artifacts directory doesn't exist.
            // The ``fileExists`` check runs at Groovy-pipeline-
            // evaluation time; if the path is missing we no-op the
            // stage so stages 5-11 can still run.
{stage4_when}
            steps {{
{stage4}
                archiveArtifacts artifacts: '{W}runtime/validate-artifacts-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 5 — diff (drift gate)
        // Live target vs the bundled contract. --exit-on-drift forces a
        // human decision before plan proceeds against a drifted baseline.
        // ═════════════════════════════════════════════════════════════
        stage('5 - diff (drift gate)') {{
{when(5)}
            steps {{
                // ``fluid diff`` takes ``--out``, NOT ``--report``.
{stage5}
                archiveArtifacts artifacts: '{W}runtime/diff-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 6 — plan (structural)
        // DDL operations + plan.json with bundleDigest + planDigest.
        // Terraform-style "apply consumes exact plan" binding.
        // ═════════════════════════════════════════════════════════════
        stage('6 - plan') {{
{when(6)}
            steps {{
                // Plan for the SAME mode stage 7 applies: plan.json records
                // it, and ``fluid apply`` refuses a plan made for another
                // (apply_plan_mode_mismatch), before any build.
{stage6}
                archiveArtifacts artifacts: '{W}runtime/plan.json,{W}runtime/plan.html', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 7 — apply (structural)
        // Six-mode DDL matrix. Destructive modes (replace*) require
        // ALLOW_DATA_LOSS when FLUID_ENV != dev or target has rows.
        // ═════════════════════════════════════════════════════════════
        stage('7 - apply') {{
{when(7)}
            // SECURITY: parameters reach the shell as the environment
            // variables Jenkins exports for them, never Groovy-
            // concatenated into the script: a pattern that expanded
            //   "--mode amend-and-build --build-id " + params.APPLY_BUILD_ID
            // unquoted let APPLY_BUILD_ID="x --allow-data-loss" add argv
            // tokens (auth-gate bypass). POSIX `set --` + if/then/fi
            // composes argv so each "$VAR" expansion is one token.
            steps {{
{stage7}
                // `fluid apply --report` writes runtime/apply-report.html for the
                // modes that apply DDL; a *-and-build mode hands over to the
                // build runner, whose record of each run is the JSON under the
                // source contract's .fluid/runs/.
                archiveArtifacts artifacts: '{W}runtime/apply-report.html,{W}**/.fluid/runs/**/*.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 8 — policy apply (structural)
        // Enforces IAM/GRANT bindings. Runs AFTER apply (GRANTs need
        // target objects) and BEFORE verify (transform on under-authed
        // objects surfaces as policy failure, not masked build error).
        // Self-gated on dist/artifacts/policy/bindings.json existence.
        // ═════════════════════════════════════════════════════════════
        stage('8 - policy apply') {{
{when(8)}
            steps {{
                // ``fluid policy-apply`` does NOT accept a --report flag;
                // its report goes to stdout, captured to the file.
{stage8}
                archiveArtifacts artifacts: '{W}runtime/policy-apply-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 9 — verify (structural)
        // Post-apply reconciliation. Catches silent DDL coercions
        // (TIMESTAMP_NTZ → LTZ, Redshift length truncations, etc.).
        // ═════════════════════════════════════════════════════════════
        stage('9 - verify') {{
{when(9)}
            steps {{
                // ``fluid verify`` takes ``--out``, NOT ``--report``.
{stage9}
                archiveArtifacts artifacts: '{W}runtime/verify-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 10 — publish (publication)
        // Multi-target catalog publisher. Push to CC / DMM / DataHub /
        // Collibra / Alation / marketplace / blob storage. The result
        // document (`--format json`) is archived.
        // ═════════════════════════════════════════════════════════════
        stage('10 - publish') {{
{when(10)}
            steps {{
{stage10}
                archiveArtifacts artifacts: '{W}runtime/publish-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 11 — schedule sync (publication, Path A only)
        // Pushes generated DAGs to the scheduler's control plane.
        // Path B (EventBridge / MWAA / Snowflake Tasks) is applied in
        // Stage 7 via SchedulePlanner.
        // ═════════════════════════════════════════════════════════════
        stage('11 - schedule sync') {{
{when(11)}
            steps {{
                // Use POSIX `set --` rather than bash arrays so this runs
                // under Jenkins's default `/bin/sh` invocation. Each $VAR
                // is quoted — one argv token per expansion — so a
                // malicious value stays a single token that our CLI then
                // rejects in _validate_destination / _validate_safe_ident.
                // Self-gated on a scheduler and on generated DAG files
                // (reference-only contracts, stage 3 not run, or no
                // scheduled build): nothing to sync is not a failure.
{stage11}
                archiveArtifacts artifacts: '{W}runtime/schedule-sync-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}
    }}

    post {{
        success {{
{post_success_block}
        }}
        failure {{
            echo '❌ 11-stage pipeline failed — check stage view for gate that fired'
        }}
        unstable {{
            echo '⚠ 11-stage pipeline unstable — some stages warned but did not hard-fail'
        }}
        cleanup {{
            // deleteDir() is a core Pipeline step (no plugin), and
            // `cleanup` runs after every other post condition.
            deleteDir()
        }}
    }}
}}
"""

        banner = self._credential_banner(
            comment_prefix="// ",
            ci_system_name="Jenkinsfile",
            secret_surface_hint=(
                "Either (a) expose them as env vars on the Jenkins agent "
                "(docker-compose `environment:`, Kubernetes agent template, "
                "Jenkins Global Node Properties — sh steps inherit), or "
                "(b) create string credentials in Jenkins → Manage Credentials "
                "and bind them via the `credentials()` DSL inside the "
                "`environment {}` block."
            ),
        )
        return {"Jenkinsfile": banner + self._jenkins_banner(config) + jenkins_pipeline}

    def _jenkins_banner(self, config: PipelineConfig) -> str:
        """What this Jenkinsfile needs from Jenkins and its agent."""
        from fluid_build import __version__

        lines = [
            f"Generated by forge-cli {__version__} (`fluid generate ci --system jenkins`).",
            "",
            "Jenkins plugins it needs:",
            "  " + ", ".join(self.required_plugins(config)),
            "  (the workspace is removed with the core deleteDir() step, so no",
            "  ws-cleanup)",
        ]
        if config.diff_last_applied:
            lines += [
                "  copyartifact: stage 0 copies the plan the last successful build",
                "  applied for each env, stage 5's `fluid diff --last-applied`",
                "  baseline, from that build's own artifacts.",
            ]
        lines += [""]
        if (config.install_mode or "pypi") == "pypi":
            lines += [
                "Agent: python3 with the venv module (stage 0 installs FLUID_PACKAGE_SPEC",
                "into $WORKSPACE/.fluid-venv and every stage runs fluid from there),",
                "and rsync for a file:// or ssh:// stage-11 destination.",
                "",
            ]
        lines += [
            "Every parameter below has a default, and every stage reads it with the",
            "same default as its fallback: Jenkins runs a job's first build, and its",
            "first after the job lost its parameter definitions (a restart that",
            "re-seeds jobs from job-dsl or JCasC), with no parameters exported.",
            "",
        ]
        return "".join(f"// {line}".rstrip() + "\n" for line in lines)

    @staticmethod
    def required_plugins(config: PipelineConfig) -> List[str]:
        """The Jenkins plugins (update-center ids) the generated file needs."""
        plugins = ["workflow-aggregator", "git"]
        if config.diff_last_applied:
            plugins.append("copyartifact")
        return plugins
