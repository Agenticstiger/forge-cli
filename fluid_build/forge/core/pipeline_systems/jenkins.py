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
      warehouse writes. Safe for every PR.
    * **Schema deploy** (above + apply ``--mode amend`` + policy-apply
      + verify). Stage 10 publish and stage 11 schedule-sync off.
    * **Full productionization** (all 11 stages on, apply
      ``--mode amend-and-build`` with a specific BUILD_ID, publish to a
      list of catalogs, schedule-sync DAGs to the scheduler).
    * **Destructive replace** (apply ``--mode replace`` +
      ``ALLOW_DATA_LOSS=true``). Auto-snapshot before drop.

    Back-compat: the legacy ``generates_artifacts: False`` (reference-only
    contracts) and ``workdir: "..."`` (subfolder checkout) config flags
    still work — stage 3 is skipped when the contract declares itself
    reference-only, and every sh block is wrapped with ``cd "<workdir>"``
    when workdir is set.
    """

    def __init__(self):
        super().__init__()
        self.provider_name = "Jenkins"
        self.file_extensions = [".groovy"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate the 11-stage parameterized Jenkinsfile.

        Returns a ``{"Jenkinsfile": <content>}`` dict matching the
        ``BasePipelineTemplate`` contract.
        """

        # ``cd "<workdir>" && `` prefix for every sh block when the
        # contract lives in a subfolder of the SCM checkout. Jenkins
        # checks out at repo root; fluid needs to run from the contract
        # folder. Every sh block uses the triple-single ``sh '''...'''``
        # form so double-quoted paths inside don't collide with outer
        # string delimiters, and Jenkins params reach the shell via
        # env-var injection (``${APPLY_MODE}`` etc.) rather than Groovy
        # interpolation.
        CD = f'cd "{config.workdir}" && ' if config.workdir else ""

        # Archive patterns are rooted at the SCM root (the Jenkins workspace),
        # so every glob gets the workdir prefix. ``allowEmptyArchive: true``
        # on every archiveArtifacts handles reference-only contracts that
        # legitimately produce no plan.json / artifacts/ / reports.
        P = f"{config.workdir}/" if config.workdir else ""

        # Reference-only contracts (pattern: hybrid-reference) delegate
        # generation to upstream — omit stage 3 entirely in that case.
        stage_3_enabled_default = "true" if config.generates_artifacts else "false"

        # Stage 10 ``PUBLISH_TARGETS`` rendering.
        #
        # Default (``config.default_publish_target is None``): emit the
        # bare ``${PUBLISH_TARGETS}`` form — same as before this flag
        # existed.
        #
        # Opt-in (``--default-publish-target X``): emit the
        # ``${PUBLISH_TARGETS:-X}`` shell-level fallback so the very
        # first Pipeline-from-SCM build Jenkins auto-triggers
        # (before the parameters block is exported as env vars)
        # still publishes to the intended catalog.
        _pub_target = (config.default_publish_target or "").strip()
        PUBLISH_TARGETS_EXPANSION = (
            f"PUBLISH_TARGETS:-{_pub_target}" if _pub_target else "PUBLISH_TARGETS"
        )
        verify_strict_default = "true" if config.verify_strict_default else "false"
        publish_stage_default = "true" if config.publish_stage_default else "false"
        publish_command = (
            '''fluid publish "${CONTRACT:-contract.fluid.yaml}" ${TARGET_FLAGS} \\
                         --env "${FLUID_ENV:-dev}"'''
            if config.publish_include_env
            else """fluid publish "${CONTRACT:-contract.fluid.yaml}" ${TARGET_FLAGS}"""
        )

        # --- Install-mode dispatch --------------------------------------
        # Pick the Setup stage's pip-install shell body based on
        # ``config.install_mode``. The generated Jenkinsfile carries only
        # the logic for the selected mode — no runtime branching, no dead
        # fallback code. This keeps production Jenkinsfiles short + clean.
        install_mode = config.install_mode or "pypi"
        if install_mode == "pypi":
            setup_install_sh = """                // Install the fluid CLI from stable PyPI. Four Jenkins
                // parameters let operators override from the Build-With-
                // Parameters dialog without editing Groovy:
                //   FLUID_PACKAGE_SPEC         package spec (name + optional version
                //                              pin, e.g. 'data-product-forge==X.Y.Z')
                //   FLUID_PIP_INDEX_URL        primary index (leave blank for stable
                //                              PyPI; set 'https://test.pypi.org/simple/'
                //                              for TestPyPI pilot builds)
                //   FLUID_PIP_EXTRA_INDEX_URL  fallback index (usually pypi.org/simple
                //                              when PRIMARY points at TestPyPI, so
                //                              transitive deps still resolve)
                //   FLUID_ALLOW_PRERELEASE     'true' → add --pre (alpha/rc releases);
                //                              leave 'false' for stable-only in prod
                sh '''set -e
                      INDEX_FLAGS=""
                      if [ -n "${FLUID_PIP_INDEX_URL:-}" ]; then
                        INDEX_FLAGS="--index-url ${FLUID_PIP_INDEX_URL}"
                      fi
                      if [ -n "${FLUID_PIP_EXTRA_INDEX_URL:-}" ]; then
                        INDEX_FLAGS="${INDEX_FLAGS} --extra-index-url ${FLUID_PIP_EXTRA_INDEX_URL}"
                      fi
                      PRE_FLAG=""
                      if [ "${FLUID_ALLOW_PRERELEASE:-false}" = "true" ]; then
                        PRE_FLAG="--pre"
                      fi
                      pip install --quiet --upgrade ${PRE_FLAG} ${INDEX_FLAGS} \\
                        "${FLUID_PACKAGE_SPEC:-data-product-forge}"'''"""
        elif install_mode == "dev-source":
            # install-mode=dev-source uses PYTHONPATH=/forge-cli-src to
            # point Python at the bind mount LIVE — no pip install. That
            # sidesteps a pile of wheel-cache / stale-file bugs that made
            # ``pip install /forge-cli-src`` unreliable in practice.
            # The PYTHONPATH export happens in the pipeline-level
            # ``environment {}`` block (added below in dev-source mode),
            # so every downstream sh step inherits it automatically.
            setup_install_sh = """                sh '''set -e
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
                      # Wipe any stale data-product-forge install from
                      # site-packages so its modules don't shadow the
                      # bind mount. PYTHONPATH-prepending normally wins
                      # over site-packages, but a leftover egg-info or
                      # namespace package fragment can confuse imports.
                      pip uninstall -y data-product-forge 2>/dev/null || true
                      echo "install-mode=dev-source — fluid imports will resolve from /forge-cli-src via PYTHONPATH"'''"""
        else:
            # Defensive: unknown install_mode. Caller passed something
            # we don't support — raise NOW (at generate time) rather
            # than emit a broken Jenkinsfile that confuses CI later.
            raise ValueError(
                f"Unknown install_mode {install_mode!r} — expected 'pypi' or 'dev-source'"
            )

        # PYTHONPATH differs per install mode:
        # - pypi: ``.`` (current workspace). fluid installed via pip,
        #   which places everything under site-packages — no need to
        #   add the bind mount.
        # - dev-source: ``/forge-cli-src`` (the bind mount). This lets
        #   ``import fluid_build`` resolve LIVE against the host source,
        #   bypassing pip's wheel cache + stale-file pitfalls. Every sh
        #   step in every stage inherits this (Jenkins expands
        #   ``environment {}`` as env vars for every sh invocation).
        if install_mode == "dev-source":
            pythonpath_value = "/forge-cli-src"
        else:
            pythonpath_value = "."

        # Install-mode-specific Jenkins parameters. pypi mode exposes
        # pip-install overrides (package spec, index URLs, prerelease
        # toggle) so operators can swap TestPyPI in without editing
        # Groovy. dev-source mode has no such overrides — it always
        # installs from the bind mount and fails loud if it's missing.
        if install_mode == "pypi":
            install_mode_parameters = """
        // ── Install overrides (pypi mode only) ──────────────────────
        // Default = stable PyPI, no prerelease. Override for pilot /
        // private-index / pinned-version builds.
        string(name: 'FLUID_PACKAGE_SPEC',
               defaultValue: 'data-product-forge',
               description: 'Package spec for pip. Pin a version via \\'data-product-forge==X.Y.Z\\'.')
        string(name: 'FLUID_PIP_INDEX_URL',
               defaultValue: '',
               description: 'Primary pip index. Leave blank for stable PyPI; set \\'https://test.pypi.org/simple/\\' for TestPyPI pilot builds, or your private mirror URL.')
        string(name: 'FLUID_PIP_EXTRA_INDEX_URL',
               defaultValue: '',
               description: 'Fallback pip index. Usually \\'https://pypi.org/simple/\\' when PRIMARY points at TestPyPI so transitive deps still resolve.')
        booleanParam(name: 'FLUID_ALLOW_PRERELEASE', defaultValue: false,
                     description: 'Pass pip --pre (pulls alpha/rc releases). Leave false in prod.')"""
        else:
            install_mode_parameters = ""

        # Parameter block — every stage gets a boolean toggle + per-stage
        # config. Operators trigger "Build with Parameters" in the Jenkins
        # UI to pick a subset of the 11-stage pipeline without editing Groovy.
        # Choice order + defaults match the HTML design doc (perfect-pipeline).
        parameters_block = f"""
    parameters {{
        // ── Global ──────────────────────────────────────────────────
        string(name: 'CONTRACT',  defaultValue: 'contract.fluid.yaml',
               description: 'Contract path relative to the workspace (or workdir when set).')
        string(name: 'FLUID_ENV', defaultValue: 'dev',
               description: 'Environment overlay (dev | staging | prod | ...).'){install_mode_parameters}

        // ── Stage 1 — bundle ────────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_1_BUNDLE',  defaultValue: true,
                     description: 'Stage 1: deterministic tgz bundle + MANIFEST.json (SHA-256).')
        // BUNDLE_FORMAT is intentionally not a parameter: Stages 4 (validate
        // artifacts), 6 (plan → bundleDigest), and 7 (apply → plan-binding
        // verification) all require the tgz MANIFEST.json. yaml/json bundles
        // are valid for `fluid bundle` but would break every downstream stage
        // in this pipeline. Operators who need a single-file YAML resolve
        // should run `fluid bundle --format yaml` out-of-band.

        // ── Stage 2 — validate ─────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_2_VALIDATE', defaultValue: true,
                     description: 'Stage 2: extension-routed validators (schema + sqlglot + openapi).')
        booleanParam(name: 'VALIDATE_STRICT',      defaultValue: true,
                     description: 'Stage 2: --strict (any validator error fails the pipeline).')

        // ── Stage 3 — generate artifacts ───────────────────────────
        booleanParam(name: 'RUN_STAGE_3_GENERATE_ARTIFACTS', defaultValue: {stage_3_enabled_default},
                     description: 'Stage 3: ODCS + ODPS-Bitol + schedule + policy fanout. Off for reference-only contracts.')
        string(name: 'GENERATE_EMIT',
               defaultValue: 'odcs,odps-bitol,schedule,policies',
               description: 'Stage 3 --emit list (comma-separated). dbt excluded by design (execution artifact).')

        // ── Stage 4 — validate artifacts ───────────────────────────
        booleanParam(name: 'RUN_STAGE_4_VALIDATE_ARTIFACTS', defaultValue: true,
                     description: 'Stage 4: re-verify MANIFEST SHA-256 + per-format schema validators.')

        // ── Stage 5 — diff (drift gate) ────────────────────────────
        booleanParam(name: 'RUN_STAGE_5_DIFF',  defaultValue: true,
                     description: 'Stage 5: compare contract vs live warehouse schema.')
        booleanParam(name: 'DIFF_EXIT_ON_DRIFT', defaultValue: true,
                     description: 'Stage 5: --exit-on-drift (hard-fail if drift detected).')

        // ── Stage 6 — plan ─────────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_6_PLAN', defaultValue: true,
                     description: 'Stage 6: compute DDL operations; emits bundleDigest + planDigest.')
        booleanParam(name: 'PLAN_HTML',        defaultValue: true,
                     description: 'Stage 6: emit HTML visualization of the plan.')

        // ── Stage 7 — apply ────────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_7_APPLY', defaultValue: true,
                     description: 'Stage 7: execute DDL (mode matrix; plan-binding cryptographically verified).')
        choice(name: 'APPLY_MODE',
               choices: ['dry-run', 'amend', 'create-only', 'amend-and-build', 'replace', 'replace-and-build'],
               description: 'Stage 7 mode. dry-run = render only (safe); amend = default additive; replace = DROP+CREATE (requires ALLOW_DATA_LOSS in non-dev).')
        string(name: 'APPLY_BUILD_ID', defaultValue: '',
               description: 'Stage 7: required for amend-and-build / replace-and-build (dbt build ID from contract builds[]).')
        booleanParam(name: 'ALLOW_DATA_LOSS', defaultValue: false,
                     description: 'Stage 7: gate waiver for --mode replace* in non-dev or when target has rows.')
        booleanParam(name: 'NO_VERIFY_DIGEST', defaultValue: false,
                     description: 'Stage 7: DR emergency escape — skip plan-binding verification. Use only when the original bundle is unreachable.')

        // ── Stage 8 — policy apply ─────────────────────────────────
        booleanParam(name: 'RUN_STAGE_8_POLICY_APPLY', defaultValue: true,
                     description: 'Stage 8: enforce IAM/GRANT bindings (self-gated on bindings.json presence).')
        choice(name: 'POLICY_APPLY_MODE',
               choices: ['enforce', 'check'],
               description: 'Stage 8: enforce = apply GRANTs; check = dry-run / PR report only.')

        // ── Stage 9 — verify ───────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_9_VERIFY', defaultValue: true,
                     description: 'Stage 9: post-apply reconciliation vs live warehouse.')
        booleanParam(name: 'VERIFY_STRICT',      defaultValue: {verify_strict_default},
                     description: 'Stage 9: --strict (fail on any schema mismatch, including silent type coercions).')

        // ── Stage 10 — publish ─────────────────────────────────────
        booleanParam(name: 'RUN_STAGE_10_PUBLISH', defaultValue: {publish_stage_default},
                     description: 'Stage 10: push catalog artifacts to one or more targets. Opt-in — typically gated to main branch.')
        string(name: 'PUBLISH_TARGETS',
               defaultValue: 'datamesh-manager',
               description: 'Stage 10: space-separated publish targets (command-center datahub datamesh-manager collibra ...).')

        // ── Stage 11 — schedule sync (Path A) ──────────────────────
        booleanParam(name: 'RUN_STAGE_11_SCHEDULE_SYNC', defaultValue: false,
                     description: 'Stage 11: push generated DAGs to scheduler (airflow / mwaa / composer / astronomer / prefect / dagster).')
        choice(name: 'SCHEDULER',
               choices: ['', 'airflow', 'mwaa', 'composer', 'astronomer', 'prefect', 'dagster'],
               description: 'Stage 11 scheduler target. Blank = no-op.')
        string(name: 'SCHEDULER_DESTINATION',
               defaultValue: '',
               description: 'Stage 11: airflow/mwaa destination URL. Supports s3://, gs://, az://, ssh://, scp://, file:// or a bare path. Required for airflow + mwaa; ignored for composer / astronomer / prefect / dagster.')
        string(name: 'SCHEDULER_ENVIRONMENT_NAME',
               defaultValue: '',
               description: 'Stage 11: composer environment name or astronomer deployment name.')
        string(name: 'SCHEDULER_LOCATION',
               defaultValue: '',
               description: 'Stage 11: GCP region for composer (e.g. europe-west1, us-central1).')
        string(name: 'SCHEDULER_WORKSPACE',
               defaultValue: '',
               description: 'Stage 11: prefect workspace or dagster-cloud deployment name.')
        booleanParam(name: 'SCHEDULE_SYNC_DRY_RUN',
                     defaultValue: false,
                     description: 'Stage 11: --dry-run (log the planned subprocess argv without executing).')
    }}"""

        jenkins_pipeline = f"""
pipeline {{
    // Default to any available agent. Change to `label 'your-label'`
    // if you have a dedicated FLUID-equipped agent pool.
    agent any

    options {{
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }}
{parameters_block}

    environment {{
        FLUID_LOG_LEVEL = 'INFO'
        FLUID_CONFIG_PATH = './fluid_config'
        PYTHONPATH = '{pythonpath_value}'

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
        //   DMM_API_URL = credentials('dmm-api-url')
        //   DMM_API_KEY = credentials('dmm-api-key')
    }}

    stages {{
        stage('Setup [install-mode: {install_mode}]') {{
            steps {{
{setup_install_sh}
                sh '''{CD}fluid --version'''
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 1 — bundle (structural)
        // Deterministic .tgz + MANIFEST.json (SHA-256 merkle root).
        // Root of trust for every downstream stage.
        // ═════════════════════════════════════════════════════════════
        stage('1 · bundle') {{
            when {{ expression {{ return params.RUN_STAGE_1_BUNDLE }} }}
            steps {{
                sh '''{CD}mkdir -p runtime
                       fluid bundle "${{CONTRACT:-contract.fluid.yaml}}" --format tgz --out runtime/bundle.tgz'''
                archiveArtifacts artifacts: '{P}runtime/bundle.tgz', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 2 — validate (structural)
        // Extension-routed: schema + sqlglot (SQL) + openapi-spec-validator.
        // Fail early, fail loud.
        // ═════════════════════════════════════════════════════════════
        stage('2 · validate') {{
            when {{ expression {{ return params.RUN_STAGE_2_VALIDATE }} }}
            environment {{
                VALIDATE_STRICT_FLAG = "${{params.VALIDATE_STRICT ? '--strict' : ''}}"
            }}
            steps {{
                sh '''{CD}fluid validate "${{CONTRACT:-contract.fluid.yaml}}" ${{VALIDATE_STRICT_FLAG}} \\
                           --report runtime/validate-report.json'''
                archiveArtifacts artifacts: '{P}runtime/validate-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 3 — generate artifacts (structural)
        // ODCS + ODPS-Bitol + schedule + policy fanout. dbt excluded.
        // Auto-skipped for hybrid-reference contracts.
        // ═════════════════════════════════════════════════════════════
        stage('3 · generate artifacts') {{
            when {{ expression {{ return params.RUN_STAGE_3_GENERATE_ARTIFACTS }} }}
            steps {{
                sh '''{CD}fluid generate artifacts "${{CONTRACT:-contract.fluid.yaml}}" \\
                         --out dist/artifacts/ \\
                         --emit "${{GENERATE_EMIT}}"'''
                archiveArtifacts artifacts: '{P}dist/artifacts/**/*', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 4 — validate artifacts (structural)
        // Re-verifies MANIFEST SHA-256 + per-format schema validators.
        // Defence-in-depth against in-flight CI tampering.
        // ═════════════════════════════════════════════════════════════
        stage('4 · validate artifacts') {{
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
            when {{
                expression {{
                    return params.RUN_STAGE_4_VALIDATE_ARTIFACTS \
                        && fileExists('{P}dist/artifacts/MANIFEST.json')
                }}
            }}
            steps {{
                sh '''{CD}fluid validate-artifacts dist/artifacts/ \\
                         --manifest dist/artifacts/MANIFEST.json \\
                         --report runtime/validate-artifacts-report.json'''
                archiveArtifacts artifacts: '{P}runtime/validate-artifacts-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 5 — diff (drift gate)
        // Live warehouse vs contract. --exit-on-drift forces a human
        // decision before plan proceeds against a drifted baseline.
        // ═════════════════════════════════════════════════════════════
        stage('5 · diff (drift gate)') {{
            when {{ expression {{ return params.RUN_STAGE_5_DIFF }} }}
            // SECURITY: argument-smuggling defence (match stages 7, 9, 11).
            environment {{
                DIFF_EXIT_ON_DRIFT_VAL = "${{params.DIFF_EXIT_ON_DRIFT}}"
            }}
            steps {{
                // ``fluid diff`` takes ``--out``, NOT ``--report``.
                // Pre-fix the template emitted ``--report`` which made
                // every stage-5 invocation fail with
                // ``unrecognized arguments: --report`` before any
                // drift comparison could run.
                sh '''{CD}set -eu
                    set -- "${{CONTRACT:-contract.fluid.yaml}}" --env "${{FLUID_ENV:-dev}}" --out runtime/diff-report.json
                    if [ "${{DIFF_EXIT_ON_DRIFT_VAL:-false}}" = "true" ]; then set -- "$@" --exit-on-drift; fi
                    fluid diff "$@"'''
                archiveArtifacts artifacts: '{P}runtime/diff-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 6 — plan (structural)
        // DDL operations + plan.json with bundleDigest + planDigest.
        // Terraform-style "apply consumes exact plan" binding.
        // ═════════════════════════════════════════════════════════════
        stage('6 · plan') {{
            when {{ expression {{ return params.RUN_STAGE_6_PLAN }} }}
            // Pass APPLY_MODE through to plan so plan.json's recorded
            // ``mode`` matches what Stage 7 will request. The
            // apply-side mode-mismatch gate (apply.py:apply_plan_mode_mismatch)
            // requires plan.mode == apply.mode (only None ↔ amend is
            // normalized as compatible). Without this, every non-amend
            // APPLY_MODE — including the ``dry-run`` default — would
            // generate a mode-less plan and trip the gate at apply time.
            environment {{
                PLAN_HTML_FLAG = "${{params.PLAN_HTML ? '--html' : ''}}"
                APPLY_MODE     = "${{params.APPLY_MODE}}"
            }}
            steps {{
                sh '''{CD}fluid plan "${{CONTRACT:-contract.fluid.yaml}}" \\
                           --out runtime/plan.json ${{PLAN_HTML_FLAG}} \\
                           --mode "$APPLY_MODE" \\
                           --env "${{FLUID_ENV:-dev}}"'''
                archiveArtifacts artifacts: '{P}runtime/plan.json,{P}runtime/plan.html', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 7 — apply (structural)
        // Six-mode DDL matrix. Destructive modes (replace*) require
        // ALLOW_DATA_LOSS when FLUID_ENV != dev or target has rows.
        // ═════════════════════════════════════════════════════════════
        stage('7 · apply') {{
            when {{ expression {{ return params.RUN_STAGE_7_APPLY }} }}
            // SECURITY: user-supplied params routed through plain
            // environment-block assignments as raw env vars — NOT
            // Groovy-ternary-concatenated into a single string. The
            // previous pattern set
            //   APPLY_BUILD_FLAG = "--build " + params.APPLY_BUILD_ID
            // then expanded `${{APPLY_BUILD_FLAG}}` UNQUOTED in the sh
            // body, which IFS-word-splits on whitespace. A Jenkins user
            // with Build-With-Parameters permission could set
            //   APPLY_BUILD_ID="x --allow-data-loss --no-verify-digest"
            // → the value split into 4 argv tokens → `fluid apply` saw
            // --allow-data-loss and --no-verify-digest even when the
            // Jenkins booleans ALLOW_DATA_LOSS and NO_VERIFY_DIGEST were
            // false. Auth-gate bypass.
            //
            // Fix: env vars carry raw values; POSIX `set --` + if/then/fi
            // composes argv so each "$VAR" expansion is one argv token.
            // This matches the stage-11 pattern hardened in commit 8673544.
            environment {{
                APPLY_BUILD_ID_VAL = "${{params.APPLY_BUILD_ID}}"
                APPLY_MODE = "${{params.APPLY_MODE}}"
                ALLOW_DATA_LOSS = "${{params.ALLOW_DATA_LOSS}}"
                NO_VERIFY_DIGEST = "${{params.NO_VERIFY_DIGEST}}"
            }}
            steps {{
                sh '''{CD}set -eu
                    set -- runtime/plan.json --mode "$APPLY_MODE" --env "${{FLUID_ENV:-dev}}" --yes --report runtime/apply-report.html
                    if [ -n "${{APPLY_BUILD_ID_VAL:-}}" ]; then set -- "$@" --build "$APPLY_BUILD_ID_VAL"; fi
                    if [ "${{ALLOW_DATA_LOSS:-false}}" = "true" ]; then set -- "$@" --allow-data-loss; fi
                    if [ "${{NO_VERIFY_DIGEST:-false}}" = "true" ]; then set -- "$@" --no-verify-digest; fi
                    fluid apply "$@"'''
                archiveArtifacts artifacts: '{P}runtime/apply-report.html', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 8 — policy apply (structural)
        // Enforces IAM/GRANT bindings. Runs AFTER apply (GRANTs need
        // target objects) and BEFORE verify (transform on under-authed
        // objects surfaces as policy failure, not masked build error).
        // Self-gated on dist/artifacts/policy/bindings.json existence.
        // ═════════════════════════════════════════════════════════════
        stage('8 · policy apply') {{
            when {{ expression {{ return params.RUN_STAGE_8_POLICY_APPLY }} }}
            steps {{
                // ``fluid policy-apply`` does NOT accept a --report
                // flag — pre-fix the template emitted one anyway, so
                // when bindings.json DID exist, the command failed
                // loud with ``unrecognized arguments: --report``.
                // Policy-apply's report output goes to stdout; if a
                // JSON report is needed, capture stdout to the file
                // via shell redirection.
                sh '''{CD}if [ -f dist/artifacts/policy/bindings.json ]; then \\
                         fluid policy-apply dist/artifacts/policy/bindings.json \\
                           --mode "${{POLICY_APPLY_MODE}}" --env "${{FLUID_ENV:-dev}}" \\
                           > runtime/policy-apply-report.json 2>&1 || \\
                         {{ cat runtime/policy-apply-report.json; exit 1; }}; \\
                       else echo "no dist/artifacts/policy/bindings.json — skipping stage 8"; fi'''
                archiveArtifacts artifacts: '{P}runtime/policy-apply-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 9 — verify (structural)
        // Post-apply reconciliation. Catches silent DDL coercions
        // (TIMESTAMP_NTZ → LTZ, Redshift length truncations, etc.).
        // ═════════════════════════════════════════════════════════════
        stage('9 · verify') {{
            when {{ expression {{ return params.RUN_STAGE_9_VERIFY }} }}
            // SECURITY: same argument-smuggling defence as stage 7 —
            // route VERIFY_STRICT through a plain env var and compose
            // argv via POSIX set -- + if/then/fi rather than Groovy-
            // ternary-concatenating + unquoted env expansion. A
            // malicious VERIFY_STRICT=true Jenkins boolean is safe
            // anyway (it's just a toggle), but the pattern keeps the
            // defence consistent across all stages that take
            // parameters.
            environment {{
                VERIFY_STRICT_VAL = "${{params.VERIFY_STRICT}}"
            }}
            steps {{
                // ``fluid verify`` takes ``--out``, NOT ``--report``
                // (fixed in this batch — previously emitted --report
                // which failed with ``unrecognized arguments`` on
                // every invocation).
                sh '''{CD}set -eu
                    set -- "${{CONTRACT:-contract.fluid.yaml}}" --env "${{FLUID_ENV:-dev}}" --out runtime/verify-report.json
                    if [ "${{VERIFY_STRICT_VAL:-false}}" = "true" ]; then set -- "$@" --strict; fi
                    fluid verify "$@"'''
                archiveArtifacts artifacts: '{P}runtime/verify-report.json', fingerprint: true, allowEmptyArchive: true
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 10 — publish (publication)
        // Multi-target catalog publisher. Push to CC / DMM / DataHub /
        // Collibra / Alation / marketplace / blob storage.
        // ═════════════════════════════════════════════════════════════
        stage('10 · publish') {{
            when {{ expression {{ return params.RUN_STAGE_10_PUBLISH }} }}
            steps {{
                // PUBLISH_TARGETS is a space-separated string; shell
                // iterates it word-split into a list of --target flags.
                // The ``${{PUBLISH_TARGETS:-X}}`` shell fallback (opt-in
                // via ``fluid generate ci --default-publish-target X``)
                // protects the first Pipeline-from-SCM build Jenkins
                // auto-triggers: the ``parameters {{ }}`` block isn't
                // exported as env vars on that first run, so without a
                // shell fallback the CLI's built-in target
                // (``fluid-command-center``) is used — which may not be
                // reachable. When no fallback is configured the bare
                // ``${{PUBLISH_TARGETS}}`` form is emitted.
                sh '''{CD}TARGET_FLAGS=""; \\
                       for t in ${{{PUBLISH_TARGETS_EXPANSION}}}; do \\
                         TARGET_FLAGS="${{TARGET_FLAGS}} --target $t"; \\
                       done; \\
                       {publish_command}'''
            }}
        }}

        // ═════════════════════════════════════════════════════════════
        // Stage 11 — schedule sync (publication, Path A only)
        // Pushes generated DAGs to the scheduler's control plane.
        // Path B (EventBridge / MWAA / Snowflake Tasks) is applied in
        // Stage 7 via SchedulePlanner.
        // ═════════════════════════════════════════════════════════════
        stage('11 · schedule sync') {{
            when {{
                expression {{ return params.RUN_STAGE_11_SCHEDULE_SYNC && params.SCHEDULER?.trim() }}
            }}
            // Thread user-supplied params through the environment rather
            // than Groovy-interpolating them into the sh string. Jenkins
            // quotes env values safely; passing via the environment +
            // bash array construction below is injection-proof — a
            // malicious param value reaches our CLI as a single argv
            // token and is rejected there by _validate_destination /
            // _validate_safe_ident.
            environment {{
                SCHEDULER = "${{params.SCHEDULER}}"
                SCHEDULER_DESTINATION = "${{params.SCHEDULER_DESTINATION}}"
                SCHEDULER_ENVIRONMENT_NAME = "${{params.SCHEDULER_ENVIRONMENT_NAME}}"
                SCHEDULER_LOCATION = "${{params.SCHEDULER_LOCATION}}"
                SCHEDULER_WORKSPACE = "${{params.SCHEDULER_WORKSPACE}}"
                SCHEDULE_SYNC_DRY_RUN = "${{params.SCHEDULE_SYNC_DRY_RUN}}"
            }}
            steps {{
                // Use POSIX `set --` rather than bash arrays so this runs
                // under Jenkins's default `/bin/sh` invocation. Each $VAR
                // is quoted — one argv token per expansion — so a
                // malicious value stays a single token that our CLI then
                // rejects in _validate_destination / _validate_safe_ident.
                // Use if/then/fi rather than `[ ] && …` because the
                // `set -e` interaction with `&&` short-circuits is shell-
                // dependent and can trip on the first false test.
                // Self-gate on the presence of generated DAG files —
                // mirrors stage 8's bindings.json gate. Three failure
                // shapes are collapsed into a single clean skip with
                // guidance:
                //
                //  * ``dist/artifacts/schedule/`` missing entirely —
                //    contract is reference-only (builds[].pattern =
                //    hybrid-reference / reference / external-reference)
                //    so ``fluid generate artifacts`` auto-skipped the
                //    ``schedule`` emitter. Nothing to sync; this is
                //    the most common case for A1 / A2 variants.
                //
                //  * ``dist/artifacts/schedule/`` exists but is empty —
                //    stage 3 ran but the contract has no
                //    ``orchestration.engine`` configured so the schedule
                //    emitter produced no DAGs. Still a valid "nothing
                //    to sync" state for Path-B contracts.
                //
                //  * stage 3 never ran (``RUN_STAGE_3_GENERATE_ARTIFACTS=
                //    false``) so there's no ``dist/artifacts/`` tree
                //    at all. Safe to skip.
                //
                // Without this gate, fluid schedule-sync hard-fails with
                // ``schedule_sync_dags_dir_missing`` / ``_empty`` (CLI
                // exit 2, config error) and the whole pipeline is
                // FAILURE — even though the pre-stage-11 work (bundle
                // → apply → verify) succeeded. That's wrong for
                // reference-only pipelines which are the default
                // shape on A1 / A2. Direct CLI users of
                // ``fluid schedule-sync`` still get the strict
                // hard-fail so typos in ``--dags-dir`` surface loud.
                sh '''{CD}set -eu
                    if [ ! -d dist/artifacts/schedule ] || [ -z "$(ls -A dist/artifacts/schedule 2>/dev/null)" ]; then
                        echo "no dist/artifacts/schedule/ DAGs to sync — skipping stage 11 (reference-only contract, stage 3 not run, or no orchestration.engine configured)"
                        exit 0
                    fi
                    set -- --scheduler "$SCHEDULER" --dags-dir dist/artifacts/schedule/ --env "${{FLUID_ENV:-dev}}"
                    if [ -n "${{SCHEDULER_DESTINATION:-}}" ];      then set -- "$@" --destination "$SCHEDULER_DESTINATION"; fi
                    if [ -n "${{SCHEDULER_ENVIRONMENT_NAME:-}}" ]; then set -- "$@" --environment-name "$SCHEDULER_ENVIRONMENT_NAME"; fi
                    if [ -n "${{SCHEDULER_LOCATION:-}}" ];         then set -- "$@" --location "$SCHEDULER_LOCATION"; fi
                    if [ -n "${{SCHEDULER_WORKSPACE:-}}" ];        then set -- "$@" --workspace "$SCHEDULER_WORKSPACE"; fi
                    if [ "${{SCHEDULE_SYNC_DRY_RUN:-false}}" = "true" ]; then set -- "$@" --dry-run; fi
                    fluid schedule-sync "$@"'''
            }}
        }}
    }}

    post {{
        always {{
            cleanWs()
        }}
        success {{
            echo '✅ 11-stage pipeline completed successfully'
        }}
        failure {{
            echo '❌ 11-stage pipeline failed — check stage view for gate that fired'
        }}
        unstable {{
            echo '⚠ 11-stage pipeline unstable — some stages warned but did not hard-fail'
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
        return {"Jenkinsfile": banner + jenkins_pipeline}
