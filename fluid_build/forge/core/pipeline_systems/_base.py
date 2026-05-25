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

"""Shared scaffolding for the per-CI-system pipeline templates.

This module owns:

* ``PINNED_ACTIONS`` + ``_pin_action`` — SHA-pinning for GitHub Actions
  third-party action references (supply-chain hardening).
* ``PipelineProvider`` / ``PipelineComplexity`` enums.
* ``PipelineConfig`` dataclass — single source of truth for the
  knobs operators tune (environments, OIDC provider, install mode,
  publish defaults).
* ``BasePipelineTemplate`` — common stage-rendering logic the
  per-system subclasses inherit.
* ``StageSpec`` — frozen dataclass for one of the 11 pipeline stages.
* ``PipelineTemplateGenerator`` — dispatcher that constructs the right
  per-system template and calls ``.generate(config)`` on it.

Per-system classes live in sibling modules
(``github_actions.py`` / ``gitlab_ci.py`` / ``jenkins.py`` / etc.) so
each CI system's quirks stay contained and the file count is bounded.
"""

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from fluid_build.cli.console import cprint

# SHA-pinned GitHub Actions for supply chain security.
# Each entry maps action@tag to action@sha with a version comment.
# SHAs verified via GitHub API (git/ref/tags/<version>).
# Update these when upgrading action versions.
PINNED_ACTIONS = {
    "actions/checkout@v4": "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5",  # v4.3.1
    "actions/setup-python@v5": "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",  # v5.6.0
    "actions/upload-artifact@v4": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",  # v4.6.2
    "actions/download-artifact@v4": "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",  # v4.3.0
    "google/osv-scanner-action/osv-scanner-action@v2": "google/osv-scanner-action/osv-scanner-action@9a498708959aeaef5ef730655706c5a1df1edbc2",  # v2.3.8
    "github/codeql-action/upload-sarif@v3": "github/codeql-action/upload-sarif@7fc1baf373eb073c686865bd453d412d506a05a2",  # v3.35.1
    "google-github-actions/auth@v2": "google-github-actions/auth@c200f3691d83b41bf9bbd8638997a462592937ed",  # v2.1.13
    "aws-actions/configure-aws-credentials@v4": "aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a",  # v4.3.1
    "azure/login@v2": "azure/login@eec3c95657c1536435858eda1f3ff5437fee8474",  # v2.3.0
    "anchore/sbom-action@v0": "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610",  # v0.24.0
    "actions/attest-build-provenance@v2": "actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be",  # v2.4.0
}


def _pin_action(action_ref: str) -> str:
    """Pin a GitHub Action reference to its SHA for supply chain security."""
    return PINNED_ACTIONS.get(action_ref, action_ref)


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

    yaml = _YamlFallback()


class PipelineProvider(Enum):
    """Supported CI/CD providers"""

    GITHUB_ACTIONS = "github_actions"
    GITLAB_CI = "gitlab_ci"
    AZURE_DEVOPS = "azure_devops"
    JENKINS = "jenkins"
    BITBUCKET = "bitbucket"
    CIRCLE_CI = "circle_ci"
    TEKTON = "tekton"


class PipelineComplexity(Enum):
    """Pipeline complexity levels"""

    BASIC = "basic"  # Simple validate -> apply workflow
    STANDARD = "standard"  # Full workflow with testing
    ADVANCED = "advanced"  # Multi-environment with approvals
    ENTERPRISE = "enterprise"  # Full governance and compliance


@dataclass
class PipelineConfig:
    """Configuration for pipeline generation"""

    provider: PipelineProvider
    complexity: PipelineComplexity
    environments: List[str] = None
    enable_approvals: bool = False
    enable_security_scan: bool = True
    enable_performance_monitoring: bool = True
    enable_marketplace_publishing: bool = False
    notification_channels: List[str] = None
    custom_steps: List[Dict[str, Any]] = None
    oidc_provider: Optional[str] = None  # "gcp", "aws", "azure", or None
    # Pipeline working directory, relative to the SCM checkout root. Set when
    # `fluid generate ci` is invoked from a subfolder of a git repo so the
    # generated pipeline can cd into that folder before running fluid commands.
    # None or "" => no cd wrapper (steps run at checkout root).
    workdir: Optional[str] = None
    # Whether the pipeline should run `fluid generate transformation/schedule`
    # in a dedicated "Generate Artifacts" stage. False for reference-only
    # contracts (e.g. hybrid-reference dbt) where the transformation and
    # schedule artifacts already exist externally and fluid shouldn't be
    # asked to generate new ones.
    generates_artifacts: bool = True
    # Install mode for the ``fluid`` CLI inside the Jenkins container.
    # Two values, picked explicitly at generation time — the generated
    # Jenkinsfile carries ONLY the install logic for the selected mode
    # (no runtime branching, no dead fallback code).
    #
    #   "pypi"        — PRODUCTION DEFAULT. Single ``pip install
    #                   data-product-forge`` from stable PyPI. Clean,
    #                   reproducible, works anywhere. Override the
    #                   package spec via FLUID_PACKAGE_SPEC env var
    #                   at Jenkins build time (to pin a version or
    #                   point at a private index).
    #   "dev-source"  — LAB / CONTRIBUTOR ONLY. Installs from a
    #                   bind-mounted forge-cli checkout at
    #                   /forge-cli-src. Fails loudly with an explicit
    #                   "add this to docker-compose" message if the
    #                   mount is missing — no silent fallback to PyPI.
    #
    # ``testpypi`` (pre-release track) and ``auto`` (runtime-decision
    # multi-fallback) were dropped from the design — teams that need
    # pre-release packages override FLUID_PACKAGE_SPEC on top of pypi
    # mode; teams that need multi-env Jenkinsfiles use two generated
    # files instead of one multi-branch file.
    install_mode: str = "pypi"
    # FLUID acquisition / transformation engine declared in the contract's
    # ``builds[0].engine`` field. Drives the per-engine pip-install plan
    # in the generated pipeline's bootstrap stage. Common values:
    # ``dlt``, ``airbyte``, ``meltano``, ``debezium``, ``kafka_connect``,
    # ``duckdb``, ``dbt``. ``None`` skips engine-side bootstrap (caller
    # is expected to provide the runtime).
    engine: Optional[str] = None
    # Source kind (``builds[0].properties.source.kind``) — picks the
    # right per-source pip extras (e.g. dlt[sql_database] for postgres
    # source, meltanolabs-tap-postgres for meltano + postgres). ``None``
    # means engine has no source-kind dependency (PyAirbyte, dbt).
    source_kind: Optional[str] = None
    # Sink platform (``binding.platform`` of the first expose) — picks
    # the right per-sink pip extras (e.g. dlt[snowflake], dbt-snowflake,
    # meltanolabs-target-snowflake). ``None`` means no sink known yet.
    sink_platform: Optional[str] = None
    # Container-runtime loopback override env var, propagated into the
    # generated pipeline's environment block. When the FLUID process
    # runs inside a container (CI runner, Jenkins agent), ``localhost``
    # in the contract's source.connection.host points at the container,
    # not the operator's machine. Setting this to ``host.docker.internal``
    # (Docker Desktop) / the bridge IP (Linux) / etc. is what the FLUID
    # acquisition runner's ``apply_loopback_host_override`` reads. Empty
    # string = don't emit the env var (operator handles externally).
    runner_host_override: str = ""
    # Opt-in fallback catalog target baked into the Jenkins Stage 10
    # publish shell as ``${PUBLISH_TARGETS:-<value>}``. Left ``None`` by
    # default, which preserves the original ``${PUBLISH_TARGETS}`` (no
    # fallback) form — matches behaviour before this flag was added.
    #
    # Set this (via ``fluid generate ci --default-publish-target X``) when
    # you expect the first Pipeline-from-SCM build Jenkins auto-triggers
    # to publish to a specific catalog. On that first build, the
    # ``parameters { }`` block's defaults are parsed but NOT exported as
    # env vars, so ``${PUBLISH_TARGETS}`` is empty; without this
    # fallback the CLI would use its built-in ``fluid-command-center``
    # default, which may not be reachable. Common values:
    # ``datamesh-manager``, ``horizon``, ``datahub``, ``collibra``.
    # Only the Jenkins template consumes this today.
    default_publish_target: Optional[str] = None
    # Jenkins-only generation defaults for the stage-9 verify strictness,
    # stage-10 publish toggle, and whether stage 10 passes an explicit
    # ``--env`` flag to ``fluid publish``. These exist so scenario-specific
    # launchpads can ask ``fluid generate ci`` to emit the intended default
    # behavior directly instead of patching the generated Jenkinsfile text.
    #
    # ``publish_include_env`` defaults to False because ``fluid publish``
    # does NOT accept ``--env``; including it makes Stage 10 die with
    # ``unrecognized arguments: --env dev``. Operators who add ``--env``
    # support to ``fluid publish`` (or wrap it via a custom CLI alias) can
    # opt in via ``--publish-include-env`` at generate time.
    verify_strict_default: bool = True
    publish_stage_default: bool = False
    publish_include_env: bool = False

    def __post_init__(self):
        if self.environments is None:
            if self.complexity == PipelineComplexity.BASIC:
                self.environments = ["dev"]
            elif self.complexity == PipelineComplexity.STANDARD:
                self.environments = ["dev", "staging"]
            else:
                self.environments = ["dev", "staging", "prod"]

        if self.notification_channels is None:
            self.notification_channels = []

        if self.custom_steps is None:
            self.custom_steps = []


class PipelineTemplateGenerator:
    """Dispatches a :class:`PipelineConfig` to the right per-system
    template implementation.

    Per-system classes live in sibling modules and are imported lazily
    inside :meth:`_initialize_templates` so each one only loads when
    the dispatcher is constructed (not at import time of ``_base``).
    Lazy imports also break the circular reference: each system module
    imports ``BasePipelineTemplate`` / ``StageSpec`` from this module,
    so this module can\'t import them up front.
    """

    def __init__(self):
        self.templates = {}
        self._initialize_templates()

    def _initialize_templates(self):
        """Lazy-import every per-system template and register it.

        Lazy because importing ``GitHubActionsTemplate`` (etc.) at
        module level would create a circular import — those modules
        import ``BasePipelineTemplate`` from THIS module.
        """
        from .azure_devops import AzureDevOpsTemplate
        from .bitbucket import BitbucketTemplate
        from .circle_ci import CircleCITemplate
        from .github_actions import GitHubActionsTemplate
        from .gitlab_ci import GitLabCITemplate
        from .jenkins import JenkinsTemplate
        from .tekton import TektonTemplate

        self.templates = {
            PipelineProvider.GITHUB_ACTIONS: GitHubActionsTemplate(),
            PipelineProvider.GITLAB_CI: GitLabCITemplate(),
            PipelineProvider.AZURE_DEVOPS: AzureDevOpsTemplate(),
            PipelineProvider.JENKINS: JenkinsTemplate(),
            PipelineProvider.BITBUCKET: BitbucketTemplate(),
            PipelineProvider.CIRCLE_CI: CircleCITemplate(),
            PipelineProvider.TEKTON: TektonTemplate(),
        }

    def generate_pipeline(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate pipeline configuration files"""
        template = self.templates.get(config.provider)
        if not template:
            raise ValueError(f"Unsupported provider: {config.provider}")

        return template.generate(config)

    def list_available_providers(self) -> List[str]:
        """List available pipeline providers"""
        return [provider.value for provider in PipelineProvider]

    def get_provider_features(self, provider: PipelineProvider) -> Dict[str, Any]:
        """Get features supported by a provider"""
        template = self.templates.get(provider)
        if not template:
            return {}

        return template.get_features()


class BasePipelineTemplate:
    """Base class for pipeline templates"""

    def __init__(self):
        self.provider_name = "unknown"
        self.file_extensions = [".yml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate pipeline configuration"""
        raise NotImplementedError

    def get_features(self) -> Dict[str, Any]:
        """Get supported features"""
        return {
            "multi_environment": True,
            "approvals": True,
            "security_scanning": True,
            "artifact_management": True,
            "notifications": True,
            "parallel_execution": True,
            "matrix_builds": True,
        }

    def _get_fluid_commands(self) -> Dict[str, str]:
        """Get standard FLUID commands for different stages.

        Contract-path commands use the POSIX parameter-expansion default
        ``${CONTRACT:-contract.fluid.yaml}`` so Build Now works out of the
        box against the canonical filename. Operators who keep the
        contract under a different name export ``CONTRACT`` in the CI
        job / agent env to override. ``$BUILD_ID`` remains CI-injected —
        when unset, ``apply`` falls through to the plan-file branch for
        non-dbt contracts.
        """
        return {
            "validate": "fluid validate ${CONTRACT:-contract.fluid.yaml}",
            # 11-stage pipeline stage 5 — drift gate. Runs BEFORE plan so the
            # plan is never computed against a drifted baseline. --exit-on-drift
            # makes any drift a hard fail; local inspection runs omit the flag.
            "diff": (
                "fluid diff ${CONTRACT:-contract.fluid.yaml} --exit-on-drift "
                "--env ${FLUID_ENV:-dev}"
            ),
            "plan": "fluid plan ${CONTRACT:-contract.fluid.yaml} --out runtime/plan.json",
            # --build is required for dbt hybrid-reference builds; the
            # inline conditional keeps the template useful for both shapes.
            "apply": (
                'if [ -n "$BUILD_ID" ]; then '
                "fluid apply ${CONTRACT:-contract.fluid.yaml} --build $BUILD_ID --yes; "
                "else "
                "fluid apply runtime/plan.json --yes; "
                "fi"
            ),
            # 11-stage pipeline stage 8 — enforce policy bindings against the
            # freshly-applied schema. GRANT statements require the target
            # objects to exist, so policy-apply runs AFTER apply but BEFORE
            # verify — a transform running on an under-authorized object
            # would otherwise mask the policy gap as a build failure.
            # Skipped silently when bindings.json is absent (e.g. a
            # reference-only contract that delegates policy to upstream).
            "policy_apply": (
                "if [ -f dist/artifacts/policy/bindings.json ]; then "
                "fluid policy-apply dist/artifacts/policy/bindings.json "
                "--mode enforce; "
                "elif [ -f runtime/policy/bindings.json ]; then "
                "fluid policy-apply runtime/policy/bindings.json "
                "--mode enforce; "
                "fi"
            ),
            # 11-stage pipeline stage 9 — post-apply reconciliation. --strict
            # fails on any schema mismatch (not just missing objects), which
            # catches silent type coercions (TIMESTAMP_NTZ→LTZ, etc.). Writes
            # a JSON report for CI artifact uploads.
            # ``fluid verify`` takes ``--out``, NOT ``--report`` — the
            # latter is the apply CLI's flag. Mixing the two produces a
            # runtime "unrecognized arguments" failure under stage 9.
            "verify": (
                "fluid verify ${CONTRACT:-contract.fluid.yaml} --strict "
                "--env ${FLUID_ENV:-dev} --out runtime/verify-report.json"
            ),
            "test": "fluid test --coverage",
            "contract_test": "fluid contract-tests ${CONTRACT:-contract.fluid.yaml}",
            "generate_transformation": "fluid generate speed-transformation",
            "generate_schedule": "fluid generate schedule",
            "check_transformations": (
                "if [ -f dbt_project.yml ] || [ -d models/ ]; then "
                "fluid generate speed-transformation --check; "
                "fi"
            ),
            "check_schedules": (
                "if [ -d dags/ ] || [ -d pipelines/ ] || [ -d flows/ ]; then "
                "fluid generate schedule --check; "
                "fi"
            ),
            "visualize": "fluid viz-plan --output pipeline-viz.html && fluid viz-graph --output dependency-graph.png",
            "publish_opds": "fluid export-opds --output opds-catalog.json",
            "marketplace_publish": "fluid marketplace publish --catalog opds-catalog.json",
            # Plain `fluid doctor` — `--extended` requires scripts/diagnose.sh
            # in the workspace, which fresh forge-generated variants don't
            # ship. Users who set up extended diagnostics can edit the
            # generated file to opt in.
            "doctor": "fluid doctor",
            # Airflow DAG deployment: rsync the generated ``dags/`` directory
            # to an operator-supplied destination. Skipped when
            # $AIRFLOW_DAGS_DEST is unset or dags/ doesn't exist.
            "airflow_sync": (
                'if [ -d dags/ ] && [ -n "$AIRFLOW_DAGS_DEST" ]; then '
                'rsync -av --delete dags/ "$AIRFLOW_DAGS_DEST"/; '
                "fi"
            ),
            # Stage 10 — Catalog publish. Push the contract (+ ODPS/ODCS
            # exports) to one or more catalogs. Uses ``--target`` (repeatable)
            # per the 11-stage pipeline design: ``PUBLISH_TARGETS`` is a
            # space-separated list (e.g. ``command-center datahub``) that the
            # shell expands into ``--target X --target Y``. Falls back to the
            # legacy ``${CATALOG:-datamesh-manager}`` single-target form so
            # existing environments keep working.
            "publish_catalog": (
                'if [ -n "$DMM_API_URL" ] || [ -n "$PUBLISH_TARGETS" ]; then '
                'if [ -n "$PUBLISH_TARGETS" ]; then '
                'TARGETS=""; for t in $PUBLISH_TARGETS; do TARGETS="$TARGETS --target $t"; done; '
                "fluid publish ${CONTRACT:-contract.fluid.yaml} $TARGETS; "
                "else "
                "fluid publish ${CONTRACT:-contract.fluid.yaml} "
                "--target ${CATALOG:-datamesh-manager}; "
                "fi; "
                "fi"
            ),
        }

    def _get_common_environment_vars(self) -> Dict[str, str]:
        """Get common environment variables"""
        return {
            "FLUID_LOG_LEVEL": "INFO",
            "FLUID_CONFIG_PATH": "./fluid_config",
            "PYTHONPATH": ".",
            "PIP_CACHE_DIR": ".pip-cache",
        }

    # ── EngineRuntime registry integration (used by ALL CI emitters) ──
    #
    # The three helpers below pull the per-engine bootstrap + runtime
    # facts from ``_engine_specs`` so each subclass (github_actions,
    # gitlab_ci, circle_ci, azure_devops, bitbucket, tekton, jenkins)
    # can consume them in its native dialect without re-implementing
    # the engine→pip / engine→env-var dispatch logic. Adding a new
    # engine means one entry in ``_engine_specs.py`` and EVERY CI
    # emitter picks it up automatically.

    def _engine_pip_install_command(self, config: "PipelineConfig") -> str:
        """One-line ``pip install <engine extras>`` command, or "" if none.

        Returns the empty string when the contract has no engine declared
        OR the registry has no extras for the (engine, source, sink)
        combo. Caller can splice the result into a shell step body and
        skip the step cleanly when empty.
        """
        # Lazy import to avoid a startup-time cycle.
        from ._engine_specs import (
            render_pip_install_command,
            resolve_engine_bootstrap,
        )

        bootstrap = resolve_engine_bootstrap(
            getattr(config, "engine", None),
            source_kind=getattr(config, "source_kind", None),
            sink_platform=getattr(config, "sink_platform", None),
        )
        return render_pip_install_command(bootstrap)

    def _install_command(self, config: "PipelineConfig") -> str:
        """Combined ``pip install`` for forge-cli + per-engine extras.

        Returns ``"pip install --quiet data-product-forge"`` when the
        contract has no engine declared OR the registry has no extras;
        otherwise returns ``"pip install --quiet data-product-forge && <engine pip install>"``.

        Used by every CI emitter as the install step body so adding a
        new engine to ``_engine_specs.py`` reaches every CI system
        automatically (the alternative — an ``Install engine extras``
        step BEFORE the FLUID install step — fails because some emitters
        run pip install across many isolated jobs).
        """
        engine_pip = self._engine_pip_install_command(config)
        if engine_pip:
            return f"pip install --quiet data-product-forge && {engine_pip}"
        return "pip install --quiet data-product-forge"

    def _engine_runtime_env_vars(self, config: "PipelineConfig") -> Dict[str, str]:
        """Per-engine env vars (e.g. AIRBYTE_PROJECT_DIR for engine='airbyte').

        Returns ``{}`` when the engine has no exec-time env-var needs
        (dlt, meltano, dbt, duckdb) so callers can skip cleanly.
        """
        from ._engine_specs import render_runner_env_vars

        return render_runner_env_vars(
            runner_host_override=getattr(config, "runner_host_override", "") or "",
            engine=getattr(config, "engine", None),
        )

    def _engine_runtime_notes(self, config: "PipelineConfig", *, indent: str = "# ") -> str:
        """Operator-facing runtime notes (REQUIRES: …) as comment lines.

        Default ``indent='# '`` matches YAML / HCL / shell conventions
        used by every CI system except Jenkins (which overrides with
        ``// ``). Returns ``""`` for engines with no runtime needs.
        """
        from ._engine_specs import render_runtime_notes

        return render_runtime_notes(
            getattr(config, "engine", None),
            indent=indent,
        )

    def _security_audit_block(self, complexity: "PipelineComplexity") -> Dict[str, Any]:
        """Return a CI-system-agnostic security + compliance audit payload.

        For ``ADVANCED`` and ``ENTERPRISE`` tiers the generated pipelines
        must include a security/compliance signal so operators can wire
        SAST + policy + audit into their delivery process. We DON'T pin
        a specific scanner here — different orgs have different licensed
        tooling — but we DO emit:

        * a short shell body that exercises ``fluid`` 's own security
          surface (``fluid validate --security-only``,
          ``fluid policy-apply``, ``fluid audit --compliance``), and
        * the canonical step name + comment-banner with the keywords
          (``security scan``, ``vulnerability``, ``policy``, ``audit``,
          ``osv-scanner``, ``sast``) so CI assertions and operator search
          both succeed.

        Returns ``{"name", "comment", "body"}`` — each subclass adapts
        the trio into its native primitive (Azure DevOps stage / Bitbucket
        step / CircleCI job / Tekton task). Returns an empty dict when
        complexity ≤ STANDARD so the helper is a no-op for tiers that
        don't need it.
        """
        if complexity not in (PipelineComplexity.ADVANCED, PipelineComplexity.ENTERPRISE):
            return {}
        # The shell body. Single POSIX sh body so every CI system can
        # paste it as-is. ``|| true`` on the optional scanner so a
        # missing osv-scanner binary doesn't fail the pipeline — operators
        # promote it from "advisory" to "blocking" by removing the
        # ``|| true`` once the binary is on the runner.
        body = (
            "set -eu\n"
            "# FLUID security scan + compliance audit — advanced/enterprise tier.\n"
            "# Surfaces: SAST signal (via fluid validate), policy check\n"
            "# (via fluid policy-apply --mode dry-run), and audit / SBOM /\n"
            "# vulnerability scan (via fluid audit + optional osv-scanner).\n"
            "fluid validate --security-only || true\n"
            "if [ -f dist/artifacts/policy/bindings.json ]; then\n"
            "  fluid policy-apply dist/artifacts/policy/bindings.json "
            '--mode dry-run --env "${FLUID_ENV:-dev}" || true\n'
            "fi\n"
            "fluid audit --compliance --output runtime/compliance-report.json || true\n"
            "# Optional: OSV-Scanner vulnerability scan if the binary is on the runner.\n"
            "if command -v osv-scanner >/dev/null 2>&1; then\n"
            "  osv-scanner scan source -r . --format sarif "
            "--output runtime/osv-results.sarif || true\n"
            "fi\n"
        )
        comment_lines = [
            "Security + compliance audit (advanced/enterprise tier).",
            "Runs SAST-style fluid validate, policy enforcement dry-run,",
            "compliance audit + SBOM, and an optional OSV-Scanner",
            "vulnerability scan. Any SCA scanner can replace it without",
            "breaking the rest of the stage.",
        ]
        return {
            "name": "Security and Compliance Audit",
            "comment": comment_lines,
            "body": body,
        }

    def _credential_banner(
        self,
        comment_prefix: str,
        ci_system_name: str,
        secret_surface_hint: str,
    ) -> str:
        """Render a provider-agnostic credential-model banner.

        Every FLUID-aware CI pipeline needs the same thing: make the
        provider's env vars (SNOWFLAKE_* / GOOGLE_APPLICATION_CREDENTIALS
        / AWS_* / AZURE_* / DMM_*) available to the shell steps that
        call ``fluid``. *How* those env vars arrive differs per CI
        system — GitHub Actions uses ``secrets`` mapped via ``env:``,
        GitLab injects CI/CD variables automatically, Jenkins offers
        either agent env passthrough or the ``credentials()`` DSL, etc.

        This helper emits the common "what env vars fluid expects"
        paragraph plus a system-specific pointer so the generated
        file is self-documenting — no hard-coded Snowflake-only list.

        Arguments:
          comment_prefix: ``# `` for YAML / shell; ``// `` for Groovy.
          ci_system_name: human-readable name shown in the banner.
          secret_surface_hint: one-line hint on how THIS system
            surfaces secrets (e.g. "Settings → Secrets and variables
            → Actions"). Kept short — the rest is pattern-agnostic.
        """
        p = comment_prefix
        lines = [
            f"{p}FLUID CI/CD Pipeline — {ci_system_name}",
            f"{p}",
            f"{p}Credential model (provider-agnostic):",
            f"{p}  fluid's credential resolver reads provider auth from the runner",
            f"{p}  environment. Each provider expects its own env vars:",
            f"{p}    Snowflake → SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER / SNOWFLAKE_PASSWORD /",
            f"{p}                SNOWFLAKE_ROLE / SNOWFLAKE_WAREHOUSE / SNOWFLAKE_DATABASE",
            f"{p}    GCP       → GOOGLE_APPLICATION_CREDENTIALS (or Workload Identity via OIDC)",
            f"{p}    AWS       → AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (or OIDC role)",
            f"{p}    Azure     → AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_SUBSCRIPTION_ID",
            f"{p}    Catalog   → DMM_API_URL / DMM_API_KEY (only if using `fluid publish`)",
            f"{p}  See ``fluid_build.credentials.resolver`` for the full resolver chain.",
            f"{p}",
            f"{p}How to surface them in {ci_system_name}:",
            f"{p}  {secret_surface_hint}",
            f"{p}",
        ]
        return "\n".join(lines) + "\n"

    def _get_oidc_steps(self, oidc_provider: Optional[str]) -> List[Dict[str, Any]]:
        """Get OIDC authentication steps for GitHub Actions deploy jobs."""
        if oidc_provider == "gcp":
            return [
                {
                    "name": "Authenticate to Google Cloud (OIDC)",
                    "id": "auth",
                    "uses": _pin_action("google-github-actions/auth@v2"),
                    "with": {
                        "workload_identity_provider": "projects/${{ vars.GCP_PROJECT_NUMBER }}/locations/global/workloadIdentityPools/${{ vars.WIF_POOL }}/providers/${{ vars.WIF_PROVIDER }}",
                        "service_account": "${{ vars.GCP_SA_EMAIL }}",
                    },
                }
            ]
        elif oidc_provider == "aws":
            return [
                {
                    "name": "Configure AWS Credentials (OIDC)",
                    "uses": _pin_action("aws-actions/configure-aws-credentials@v4"),
                    "with": {
                        "role-to-assume": "${{ vars.AWS_ROLE_ARN }}",
                        "aws-region": "${{ vars.AWS_REGION }}",
                    },
                }
            ]
        elif oidc_provider == "azure":
            return [
                {
                    "name": "Azure Login (OIDC)",
                    "uses": _pin_action("azure/login@v2"),
                    "with": {
                        "client-id": "${{ vars.AZURE_CLIENT_ID }}",
                        "tenant-id": "${{ vars.AZURE_TENANT_ID }}",
                        "subscription-id": "${{ vars.AZURE_SUBSCRIPTION_ID }}",
                    },
                }
            ]
        return []

    def _get_deploy_job_permissions(self, oidc_provider: Optional[str]) -> Dict[str, str]:
        """Get permissions for a deployment job."""
        perms = {"contents": "read"}
        if oidc_provider:
            perms["id-token"] = "write"
        return perms

    def _generate_env_ci_example(self, oidc_provider: Optional[str] = None) -> str:
        """Generate .env.ci.example content with required secrets per provider."""
        lines = [
            "# Required CI/CD Secrets for FLUID Pipeline",
            "# Copy these to your CI provider's secret store",
            "#",
            "# FLUID Configuration",
            "FLUID_LOG_LEVEL=INFO",
            "# CONTRACT=contract.fluid.yaml",
            "",
        ]
        if oidc_provider == "gcp":
            lines += [
                "# GCP Workload Identity Federation (OIDC — no stored secrets needed!)",
                "# Configure these as GitHub Actions variables (vars), not secrets:",
                "# GCP_PROJECT_NUMBER=123456789",
                "# WIF_POOL=fluid-pool",
                "# WIF_PROVIDER=github-provider",
                "# GCP_SA_EMAIL=fluid@project.iam.gserviceaccount.com",
            ]
        elif oidc_provider == "aws":
            lines += [
                "# AWS OIDC (no stored secrets needed!)",
                "# Configure these as GitHub Actions variables (vars), not secrets:",
                "# AWS_ROLE_ARN=arn:aws:iam::123456789:role/fluid-deploy",
                "# AWS_REGION=us-east-1",
            ]
        elif oidc_provider == "azure":
            lines += [
                "# Azure Federated Identity (OIDC — no stored secrets needed!)",
                "# Configure these as GitHub Actions variables (vars), not secrets:",
                "# AZURE_CLIENT_ID=00000000-0000-0000-0000-000000000000",
                "# AZURE_TENANT_ID=00000000-0000-0000-0000-000000000000",
                "# AZURE_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000",
            ]
        else:
            lines += [
                "# Provider Authentication",
                "# Prefer OIDC/Workload Identity Federation over stored secrets.",
                "# See: fluid forge --ci github_actions --oidc-provider gcp",
                "#",
                "# If OIDC is not available, configure these as CI secrets:",
                "# SNOWFLAKE_ACCOUNT=your_account",
                "# SNOWFLAKE_USER=your_user",
                "# SNOWFLAKE_PASSWORD=your_password  # Use key-pair auth instead!",
                "# GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json",
                "# AWS_ACCESS_KEY_ID=AKIA...  # Use OIDC instead!",
                "# AWS_SECRET_ACCESS_KEY=...",
            ]
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 11-stage pipeline helpers (Phase 7-rest)
    #
    # ``_stage_specs()`` returns the canonical, provider-neutral list of
    # the 11 pipeline stages. Every CI-system subclass iterates this list
    # and wraps ``_render_stage_command(spec, config)`` in its native
    # primitive (GitHub Actions step, GitLab job, Azure stage, etc.).
    #
    # Keeping the command strings here — not in each subclass — ensures
    # that upgrading the canonical contract (e.g. "stage 6 now passes a
    # new flag") propagates to every CI system without N-way drift.
    # JenkinsTemplate predates this helper and keeps its own inline
    # renderer for historical reasons; the two are kept in lockstep via
    # the assertions in ``tests/test_pipeline_templates_branches.py``.
    # ------------------------------------------------------------------

    def _stage_specs(self) -> List["StageSpec"]:
        """Return the 11 pipeline stages in order.

        Toggle defaults follow the same semantics Jenkins uses:
        stages 10 (publish) and 11 (schedule-sync) default OFF so they
        must be opt-in per run, matching the "don't push by default"
        principle. All structural stages (1-9) default ON.

        Stage 3 (generate artifacts) is controlled by the subclass's
        :attr:`config.generates_artifacts` rather than the static default
        here — the subclass applies the override when rendering.
        """
        return [
            StageSpec(
                num=1,
                slug="bundle",
                display="bundle",
                toggle_param="RUN_STAGE_1_BUNDLE",
                default_run=True,
                command=(
                    'fluid bundle "${CONTRACT:-contract.fluid.yaml}" '
                    "--format tgz --out runtime/bundle.tgz"
                ),
            ),
            StageSpec(
                num=2,
                slug="validate",
                display="validate",
                toggle_param="RUN_STAGE_2_VALIDATE",
                default_run=True,
                command=('fluid validate "${CONTRACT:-contract.fluid.yaml}" --strict'),
            ),
            StageSpec(
                num=3,
                slug="generate_artifacts",
                display="generate artifacts",
                toggle_param="RUN_STAGE_3_GENERATE_ARTIFACTS",
                default_run=True,  # overridden by config.generates_artifacts at render time
                command=(
                    'fluid generate artifacts "${CONTRACT:-contract.fluid.yaml}" '
                    "--out dist/artifacts/"
                ),
            ),
            StageSpec(
                num=4,
                slug="validate_artifacts",
                display="validate artifacts",
                toggle_param="RUN_STAGE_4_VALIDATE_ARTIFACTS",
                default_run=True,
                command="fluid validate-artifacts dist/artifacts/ --strict",
            ),
            StageSpec(
                num=5,
                slug="diff",
                display="diff (drift gate)",
                toggle_param="RUN_STAGE_5_DIFF",
                default_run=True,
                command=(
                    'fluid diff "${CONTRACT:-contract.fluid.yaml}" '
                    '--exit-on-drift --env "${FLUID_ENV:-dev}"'
                ),
            ),
            StageSpec(
                num=6,
                slug="plan",
                display="plan",
                toggle_param="RUN_STAGE_6_PLAN",
                default_run=True,
                command=(
                    'fluid plan "${CONTRACT:-contract.fluid.yaml}" '
                    '--out runtime/plan.json --env "${FLUID_ENV:-dev}"'
                ),
            ),
            StageSpec(
                num=7,
                slug="apply",
                display="apply",
                toggle_param="RUN_STAGE_7_APPLY",
                default_run=True,
                # Stage 7 uses POSIX ``set --`` + if/then/fi so
                # parameter-controlled flags (mode, allow-data-loss,
                # no-verify-*, build-id) stay as individual argv
                # tokens regardless of unquoted env-var expansion. This
                # is the security-hardened pattern from stage 11 applied
                # here — avoids the ``${APPLY_BUILD_FLAG}`` argument-
                # smuggling bug fixed in commit D2.
                #
                # NO_VERIFY_DIGEST is the single CI-operator knob; when
                # true it appends BOTH --no-verify-plan-binding and
                # --no-verify-federation so the one-knob waives the
                # plan-binding gate and the federation upstream-digest
                # gate together (the digest gate is split into two
                # narrowly-scoped flags at the CLI).
                command=(
                    "set -eu; "
                    'set -- runtime/plan.json --mode "${APPLY_MODE:-amend}" '
                    '--env "${FLUID_ENV:-dev}" --yes '
                    "--report runtime/apply-report.html; "
                    'if [ -n "${APPLY_BUILD_ID:-}" ]; then '
                    'set -- "$@" --build "$APPLY_BUILD_ID"; fi; '
                    'if [ "${ALLOW_DATA_LOSS:-false}" = "true" ]; then '
                    'set -- "$@" --allow-data-loss; fi; '
                    'if [ "${NO_VERIFY_DIGEST:-false}" = "true" ]; then '
                    'set -- "$@" --no-verify-plan-binding --no-verify-federation; fi; '
                    'fluid apply "$@"'
                ),
            ),
            StageSpec(
                num=8,
                slug="policy_apply",
                display="policy apply",
                toggle_param="RUN_STAGE_8_POLICY_APPLY",
                default_run=True,
                # Self-gates on bindings.json existence so reference-only
                # contracts (that delegate policy upstream) skip cleanly.
                command=(
                    "if [ -f dist/artifacts/policy/bindings.json ]; then "
                    "fluid policy-apply dist/artifacts/policy/bindings.json "
                    '--mode enforce --env "${FLUID_ENV:-dev}"; '
                    "fi"
                ),
            ),
            StageSpec(
                num=9,
                slug="verify",
                display="verify",
                toggle_param="RUN_STAGE_9_VERIFY",
                default_run=True,
                # ``fluid verify`` accepts ``--out``, NOT ``--report``
                # (the latter is apply's flag). Generated stage-9 commands
                # used the wrong flag historically; fixed in this revision
                # so generated CI YAML actually runs.
                command=(
                    'fluid verify "${CONTRACT:-contract.fluid.yaml}" --strict '
                    '--env "${FLUID_ENV:-dev}" --out runtime/verify-report.json'
                ),
            ),
            StageSpec(
                num=10,
                slug="publish",
                display="publish",
                toggle_param="RUN_STAGE_10_PUBLISH",
                default_run=False,  # opt-in — typically branch-gated to main
                # ``PUBLISH_TARGETS`` is a space-separated list the shell
                # expands into ``--target X --target Y ...``. Falls back
                # to ``--target ${CATALOG:-datamesh-manager}`` so legacy
                # single-target config keeps working.
                command=(
                    'if [ -n "${PUBLISH_TARGETS:-}" ]; then '
                    'TARGETS=""; for t in $PUBLISH_TARGETS; do '
                    'TARGETS="$TARGETS --target $t"; done; '
                    'fluid publish "${CONTRACT:-contract.fluid.yaml}" $TARGETS '
                    '--env "${FLUID_ENV:-dev}"; '
                    "else "
                    'fluid publish "${CONTRACT:-contract.fluid.yaml}" '
                    '--target "${CATALOG:-datamesh-manager}" '
                    '--env "${FLUID_ENV:-dev}"; '
                    "fi"
                ),
            ),
            StageSpec(
                num=11,
                slug="schedule_sync",
                display="schedule sync",
                toggle_param="RUN_STAGE_11_SCHEDULE_SYNC",
                default_run=False,  # opt-in — Path-A only
                # Scheduler-variant params (SCHEDULER, SCHEDULER_DESTINATION,
                # SCHEDULER_ENVIRONMENT_NAME, SCHEDULER_LOCATION,
                # SCHEDULER_WORKSPACE, SCHEDULE_SYNC_DRY_RUN) flow through
                # env vars. Same POSIX ``set --`` + if/then/fi pattern
                # as stage 7 so empty params never reach argv. This is
                # the security-hardened Jenkins stage-11 pattern.
                command=(
                    "set -eu; "
                    # Self-gate: skip cleanly when there's nothing to
                    # sync. Three cases are collapsed into one INFO-
                    # level skip: (a) reference-only contract where
                    # stage 3 auto-skipped the schedule emitter,
                    # (b) stage 3 was toggled off so dist/artifacts/
                    # never materialised, (c) contract has no
                    # orchestration.engine so fluid generate
                    # schedule produced zero DAGs. In all three the
                    # pre-stage-11 pipeline (bundle → apply →
                    # verify) may have succeeded and the correct
                    # posture is "nothing to do, move on" — not
                    # FAILURE. Matches stage 8's bindings.json gate.
                    "if [ ! -d dist/artifacts/schedule ] || "
                    '[ -z "$(ls -A dist/artifacts/schedule 2>/dev/null)" ]; then '
                    'echo "no dist/artifacts/schedule/ DAGs to sync '
                    "— skipping stage 11 (reference-only contract, "
                    "stage 3 not run, or no orchestration.engine "
                    'configured)"; exit 0; fi; '
                    'set -- --scheduler "$SCHEDULER" '
                    "--dags-dir dist/artifacts/schedule/ "
                    '--env "${FLUID_ENV:-dev}"; '
                    'if [ -n "${SCHEDULER_DESTINATION:-}" ]; then '
                    'set -- "$@" --destination "$SCHEDULER_DESTINATION"; fi; '
                    'if [ -n "${SCHEDULER_ENVIRONMENT_NAME:-}" ]; then '
                    'set -- "$@" --environment-name "$SCHEDULER_ENVIRONMENT_NAME"; fi; '
                    'if [ -n "${SCHEDULER_LOCATION:-}" ]; then '
                    'set -- "$@" --location "$SCHEDULER_LOCATION"; fi; '
                    'if [ -n "${SCHEDULER_WORKSPACE:-}" ]; then '
                    'set -- "$@" --workspace "$SCHEDULER_WORKSPACE"; fi; '
                    'if [ "${SCHEDULE_SYNC_DRY_RUN:-false}" = "true" ]; then '
                    'set -- "$@" --dry-run; fi; '
                    'fluid schedule-sync "$@"'
                ),
            ),
        ]

    def _render_stage_command(self, spec: "StageSpec", config: "PipelineConfig") -> str:
        """Return the fully-rendered sh command body for a stage.

        When ``config.workdir`` is set (subfolder checkout), prepends
        ``cd "<workdir>" && `` so the fluid CLI sees the contract file
        regardless of the CI system's default working directory. For a
        compound command (containing ``; `` or starting with ``set -eu``),
        the ``cd`` and the command are joined inside a single-line
        equivalent that keeps the compound structure intact under POSIX
        sh — wrapping in parentheses would spawn a subshell and prevent
        ``set -eu`` from propagating to parent shell flags.

        Each sh body uses ``"${CONTRACT:-contract.fluid.yaml}"`` +
        ``"${FLUID_ENV:-dev}"`` defaults so Build Now works without the
        operator pre-setting every env var. Credential-bearing env vars
        (SNOWFLAKE_*, AWS_*, DMM_*) are NOT defaulted here — they come
        from the CI system's secret store per the credential banner.
        """
        body = spec.command
        if config.workdir:
            # Escape double-quotes in workdir just in case (defence in
            # depth — argparse rejects most garbage upstream, but the
            # generated file is ultimately shell-interpreted).
            safe_workdir = config.workdir.replace('"', '\\"')
            body = f'cd "{safe_workdir}" && {body}'
        return body

    def _stage_default_run(self, spec: "StageSpec", config: "PipelineConfig") -> bool:
        """Return whether a stage should run by default for this config.

        Stage 3 (generate artifacts) is the one override: it turns OFF
        for reference-only contracts (where ``config.generates_artifacts``
        is False — the contract points at externally-owned dbt/Airflow
        artifacts and fluid wouldn't own them). All other stages follow
        the static default on the :class:`StageSpec`.
        """
        if spec.slug == "generate_artifacts":
            return bool(config.generates_artifacts)
        return spec.default_run

    def _render_install_setup(self, config: "PipelineConfig") -> str:
        """Return the shared install-setup sh script for the Setup stage.

        Every non-Jenkins CI system runs this as its first step. The
        output is a single POSIX sh body that:

        - ``pypi`` mode: pip-installs ``data-product-forge`` with
          optional TestPyPI overrides (``FLUID_PIP_INDEX_URL``,
          ``FLUID_PIP_EXTRA_INDEX_URL``, ``FLUID_ALLOW_PRERELEASE``,
          ``FLUID_PACKAGE_SPEC`` — matches the Jenkins build-param
          semantics so operators can pilot TestPyPI releases without
          editing Groovy or YAML).
        - ``dev-source`` mode: expects ``/forge-cli-src`` bind mount
          with the forge-cli checkout; exports ``PYTHONPATH=/forge-cli-src``.
          Fails loud with an actionable message if the mount is
          missing. Only supported on systems that run self-hosted
          runners (GitHub Actions, GitLab, Azure DevOps, Tekton).
          Bitbucket / CircleCI pass this check with a warning because
          they're hosted-only; operators who need dev-source there
          must bake forge-cli into the container image instead.

        The rendered body uses ``set -eu`` so a failed install halts
        the pipeline rather than silently moving to the next step.
        """
        mode = getattr(config, "install_mode", "pypi") or "pypi"
        # Per-engine pip extras (e.g. ``airbyte>=0.20,<1`` for engine='airbyte').
        # Appended to the install body so the 11-stage path picks up
        # engine deps the same way the BASIC paths do via _install_command().
        engine_pip = self._engine_pip_install_command(config)
        engine_install = f"\n{engine_pip}" if engine_pip else ""
        if mode == "dev-source":
            return (
                "set -eu\n"
                'if [ ! -d "/forge-cli-src" ]; then\n'
                "  echo 'FATAL: dev-source install mode requires "
                "/forge-cli-src bind mount' >&2\n"
                "  exit 2\n"
                "fi\n"
                'export PYTHONPATH="/forge-cli-src:${PYTHONPATH:-}"\n'
                'python -c "import fluid_build" || (echo \'FATAL: '
                "fluid_build import failed; check /forge-cli-src' >&2 && exit 3)\n"
                "fluid --version" + engine_install
            )
        # pypi mode — TestPyPI overrides + optional --pre
        return (
            "set -eu\n"
            "python -m pip install --upgrade pip\n"
            'INDEX_FLAGS=""\n'
            'if [ -n "${FLUID_PIP_INDEX_URL:-}" ]; then\n'
            '  INDEX_FLAGS="--index-url \\"${FLUID_PIP_INDEX_URL}\\""\n'
            "fi\n"
            'if [ -n "${FLUID_PIP_EXTRA_INDEX_URL:-}" ]; then\n'
            '  INDEX_FLAGS="$INDEX_FLAGS --extra-index-url \\"${FLUID_PIP_EXTRA_INDEX_URL}\\""\n'
            "fi\n"
            'PRE_FLAG=""\n'
            'if [ "${FLUID_ALLOW_PRERELEASE:-false}" = "true" ]; then\n'
            '  PRE_FLAG="--pre"\n'
            "fi\n"
            'SPEC="${FLUID_PACKAGE_SPEC:-data-product-forge}"\n'
            'sh -c "python -m pip install $INDEX_FLAGS $PRE_FLAG $SPEC"\n'
            "fluid --version" + engine_install
        )

    def _stage_toggle_defaults(self, config: "PipelineConfig") -> Dict[str, bool]:
        """Return ``{toggle_param: default_bool}`` for the 11 stages.

        Each CI-system subclass uses this to declare its native build-
        parameter surface (GitHub Actions workflow_dispatch inputs,
        GitLab variables, Azure DevOps parameters, etc.) without
        hardcoding the list. Keys are UPPER_SNAKE (e.g.
        ``RUN_STAGE_3_GENERATE_ARTIFACTS``) to match Jenkins and the
        env-var form every CI system accepts.
        """
        return {
            spec.toggle_param: self._stage_default_run(spec, config) for spec in self._stage_specs()
        }

    def _eleven_stage_parameters(self, config: "PipelineConfig") -> List[Tuple[str, str, str, str]]:
        """Return the canonical build-parameter declaration set.

        Each tuple is ``(name, kind, default, description)`` where:
        - ``kind`` ∈ {``boolean``, ``string``, ``choice``}
        - ``default`` is a string (``"true"``/``"false"`` for booleans)
        - description is human-readable

        Every non-Jenkins CI subclass iterates this list to emit its
        native parameter declaration dialect (GitHub Actions
        ``workflow_dispatch.inputs``, GitLab ``variables:``, Azure
        DevOps ``parameters:``, etc.). Keeping one list here avoids
        6-way drift — adding / renaming a parameter flows to every
        system automatically.
        """
        params: List[Tuple[str, str, str, str]] = [
            # Global
            (
                "CONTRACT",
                "string",
                "contract.fluid.yaml",
                "Contract path relative to workspace (or workdir when set).",
            ),
            (
                "FLUID_ENV",
                "string",
                "dev",
                "Environment overlay (dev | staging | prod).",
            ),
        ]
        # 11 stage toggles
        for spec in self._stage_specs():
            params.append(
                (
                    spec.toggle_param,
                    "boolean",
                    "true" if self._stage_default_run(spec, config) else "false",
                    f"Stage {spec.num}: toggle {spec.display}.",
                )
            )
        # Apply-mode matrix
        params.extend(
            [
                (
                    "APPLY_MODE",
                    "choice:dry-run,create-only,amend,amend-and-build,replace,replace-and-build",
                    "amend",
                    "Stage 7: apply mode. ``replace*`` variants require ALLOW_DATA_LOSS=true.",
                ),
                (
                    "APPLY_BUILD_ID",
                    "string",
                    "",
                    "Stage 7: build ID for amend-and-build / replace-and-build. Empty skips.",
                ),
                (
                    "ALLOW_DATA_LOSS",
                    "boolean",
                    "false",
                    "Stage 7: gate for replace / replace-and-build modes.",
                ),
                (
                    "NO_VERIFY_DIGEST",
                    "boolean",
                    "false",
                    "Stage 7: emergency escape — waives BOTH the plan-binding "
                    "and federation upstream-digest gates "
                    "(--no-verify-plan-binding --no-verify-federation). Audit log flag.",
                ),
                (
                    "PUBLISH_TARGETS",
                    "string",
                    "datamesh-manager",
                    "Stage 10: space-separated catalog targets.",
                ),
                (
                    "SCHEDULER",
                    "choice:,airflow,mwaa,composer,astronomer,prefect,dagster",
                    "",
                    "Stage 11: scheduler target. Blank = no-op.",
                ),
                (
                    "SCHEDULER_DESTINATION",
                    "string",
                    "",
                    "Stage 11: airflow/mwaa destination URL (s3:, gs:, az:, ssh:, scp:, file:).",
                ),
                (
                    "SCHEDULER_ENVIRONMENT_NAME",
                    "string",
                    "",
                    "Stage 11: composer env name or astronomer deployment name.",
                ),
                (
                    "SCHEDULER_LOCATION",
                    "string",
                    "",
                    "Stage 11: GCP region for composer.",
                ),
                (
                    "SCHEDULER_WORKSPACE",
                    "string",
                    "",
                    "Stage 11: prefect workspace or dagster-cloud deployment name.",
                ),
                (
                    "SCHEDULE_SYNC_DRY_RUN",
                    "boolean",
                    "false",
                    "Stage 11: --dry-run (log planned subprocess argv without executing).",
                ),
            ]
        )
        # Install-mode (pypi mode only gets the TestPyPI overrides)
        if getattr(config, "install_mode", "pypi") == "pypi":
            params.extend(
                [
                    (
                        "FLUID_PACKAGE_SPEC",
                        "string",
                        "data-product-forge",
                        "pip package spec. Pin with 'data-product-forge==X.Y.Z'.",
                    ),
                    (
                        "FLUID_PIP_INDEX_URL",
                        "string",
                        "",
                        "Primary pip index. Blank = stable PyPI; set TestPyPI URL for pilot builds.",
                    ),
                    (
                        "FLUID_PIP_EXTRA_INDEX_URL",
                        "string",
                        "",
                        "Fallback pip index for transitive deps.",
                    ),
                    (
                        "FLUID_ALLOW_PRERELEASE",
                        "boolean",
                        "false",
                        "Pass pip --pre (pulls alpha/rc releases).",
                    ),
                ]
            )
        return params


@dataclass(frozen=True)
class StageSpec:
    """Canonical spec for one of the 11 pipeline stages.

    Immutable so subclasses can't mutate the shared list they receive
    from :meth:`BasePipelineTemplate._stage_specs`. Each subclass
    iterates the specs in order and wraps
    :meth:`BasePipelineTemplate._render_stage_command(spec, config)`
    in its native CI primitive (GitHub Actions ``steps:``, GitLab
    ``script:``, Azure DevOps ``steps:``, Bitbucket ``script:``,
    CircleCI ``steps:``, Tekton ``taskSpec.steps``).
    """

    num: int
    """1-11."""

    slug: str
    """snake_case identifier; matches the toggle-param suffix and test
    assertion strings. Example: ``bundle``, ``generate_artifacts``,
    ``schedule_sync``."""

    display: str
    """Human-readable stage name for display in CI UI. Example:
    ``bundle``, ``generate artifacts``, ``schedule sync``. Note the
    spaces — this is the label that appears in pipeline visualisations
    and build logs."""

    toggle_param: str
    """UPPER_SNAKE boolean parameter name that each CI system exposes
    to let operators skip the stage. Example: ``RUN_STAGE_1_BUNDLE``.
    Systems that support per-stage conditionals (Jenkins, GitHub
    Actions, GitLab, Azure DevOps, CircleCI, Tekton) emit ``when:``
    / ``rules:`` / ``condition:`` clauses referencing this name;
    systems that don't (Bitbucket) ignore the toggle and emit the
    stage unconditionally."""

    default_run: bool
    """Whether the stage runs by default when the operator doesn't
    override the toggle param. True for stages 1-9 (structural);
    False for stages 10 (publish) and 11 (schedule-sync) — both are
    opt-in because they push beyond the CI environment."""

    command: str
    """Shell body executed by the stage. Uses POSIX sh syntax
    (``set --`` + ``if/then/fi``, not bash arrays) so it runs under
    any CI system's default shell. References env vars with the
    ``${VAR:-default}`` expansion idiom so fresh environments work
    without every parameter pre-set."""
