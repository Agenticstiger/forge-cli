# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from fluid_build._env_names import ENV_NAME_RULE, is_env_name

from . import _keyless

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
    "github/codeql-action/upload-sarif@v3": "github/codeql-action/upload-sarif@d77b13a0df3134d64a457ea9003f600b09fa1c8a",  # v3.36.1
    "google-github-actions/auth@v3": "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093",  # v3.0.0
    "aws-actions/configure-aws-credentials@v6": "aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd",  # v6.3.0
    "anchore/sbom-action@v0": "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610",  # v0.24.0
    "actions/attest-build-provenance@v2": "actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be",  # v2.4.0
}


def _pin_action(action_ref: str) -> str:
    """Pin a GitHub Action reference to its SHA for supply chain security."""
    return PINNED_ACTIONS.get(action_ref, action_ref)


#: The six ``fluid apply --mode`` values, in the order the pipelines list them.
APPLY_MODES: Tuple[str, ...] = (
    "dry-run",
    "create-only",
    "amend",
    "amend-and-build",
    "replace",
    "replace-and-build",
)
#: The modes that run the contract's builds, and so the only ones
#: ``fluid apply --build-id`` accepts (``apply_build_id_requires_build_mode``).
BUILD_APPLY_MODES: Tuple[str, ...] = ("amend-and-build", "replace-and-build")
#: Stage 11 schedulers (``fluid schedule-sync --scheduler``).
SCHEDULERS: Tuple[str, ...] = ("airflow", "mwaa", "composer", "astronomer", "prefect", "dagster")
#: The PyPI distribution the pipelines install.
FLUID_PACKAGE_NAME = "data-product-forge"
#: Stage 1 writes the bundle here; stages 2, 3, 5, 6, 7 and 9 read it.
BUNDLE_PATH = "runtime/bundle.tgz"

#: Characters a generator-supplied value may not contain, because it is
#: written into a POSIX ``"${NAME:-<value>}"`` expansion inside a Groovy or
#: YAML string: ``$`` and backquote expand, ``"`` ends the quoting, ``}`` ends
#: the expansion, ``'`` ends a Groovy literal and ``\`` escapes in both.
_UNSAFE_LITERAL = re.compile(r"[$`\"'\\{}\x00-\x1f\x7f]")


def check_pipeline_literal(what: str, value: str) -> str:
    """Return ``value`` if it can be written into a generated pipeline as is.

    Generator options (``--fluid-package-spec``, ``--scheduler-destination-default``,
    the contract path) become shell fallbacks and parameter defaults in the
    generated file, so a value that could expand, end the quoting or escape
    is refused at generation time rather than escaped: none of the values
    these options take legitimately contains one.
    """
    if _UNSAFE_LITERAL.search(value):
        raise ValueError(
            f"{what} {value!r} contains a character the generated pipeline cannot "
            "carry literally ($, `, \", ', \\, {, } or a control character)"
        )
    return value


def check_pipeline_workdir(workdir: str) -> str:
    """``check_pipeline_literal`` for the workdir, which is also written into
    ``archiveArtifacts`` patterns: a glob character or a comma there would
    archive files the pipeline did not write, or split the pattern list."""
    check_pipeline_literal("workdir", workdir)
    if re.search(r"[*?,\[\]]", workdir):
        raise ValueError(
            f"workdir {workdir!r} contains a glob character or a comma, which an "
            "archiveArtifacts pattern would read as a pattern"
        )
    return workdir


def check_env_name(what: str, value: str) -> str:
    """Return ``value`` if every stage's ``--env`` accepts it.

    FLUID_ENV names an overlay file and reaches ``--env`` in every stage, and
    two stages hold it to a grammar: stage 3's scheduled DAGs pass it to
    ``fluid apply --env`` (:func:`validate_env_name`), and stage 10 is
    ``fluid publish --env`` (:func:`fluid_build._env_names.is_env_name`).
    Both are applied, reused rather than copied: a default either refuses
    fails every build that runs on the defaults at that stage, stage 10 only
    after stage 7 has applied. The net rule (letters, digits, '.', '_' and
    '-', starting with a letter, at most 64 characters) also keeps a path, a
    space, a glob or a newline out of an overlay lookup and out of an
    unquoted ``--env ${FLUID_ENV:-...}``.
    """
    from fluid_build.schedulers.airflow.fluid_apply import ScheduleRenderError, validate_env_name

    try:
        validate_env_name(value)
    except ScheduleRenderError as exc:
        raise ValueError(f"{what} {value!r} is not an environment name: {exc}") from exc
    if not is_env_name(value):
        raise ValueError(
            f"{what} {value!r} is not an environment name fluid publish --env accepts: "
            f"{ENV_NAME_RULE}"
        )
    return value


def sh_param(name: str, default: str, *, keep_blank: bool = False) -> str:
    """The POSIX expansion of pipeline parameter ``name`` with its declared default.

    A CI system does not always export its parameters to the shell: Jenkins
    runs a job's first build, and the first build after the job lost its
    parameter definitions (a restart that re-seeds jobs from job-dsl or JCasC),
    with none, and a scheduled or webhook run of the other systems may not set
    them either. So every read of a parameter falls back to the value its
    declaration defaults to, rendered from the same value. ``keep_blank``
    keeps a value the operator deliberately set to blank (``${NAME-default}``);
    otherwise blank also means the default (``${NAME:-default}``).
    """
    check_pipeline_literal(f"default of {name}", default)
    return "${" + name + ("-" if keep_blank else ":-") + default + "}"


def default_fluid_package_spec(extras: Optional[List[str]] = None) -> str:
    """``data-product-forge[<extras>]==<this version>``: the CLI that generated
    the pipeline, with the extras its contract needs. A pipeline that installs
    whatever PyPI has at run time runs commands, flags and gates it was not
    generated for."""
    from fluid_build import __version__

    wanted = sorted({e.strip() for e in (extras or []) if e and e.strip()})
    suffix = f"[{','.join(wanted)}]" if wanted else ""
    return f"{FLUID_PACKAGE_NAME}{suffix}=={__version__}"


class PipelineProvider(Enum):
    """Supported CI/CD providers"""

    GITHUB_ACTIONS = "github_actions"
    GITLAB_CI = "gitlab_ci"
    AZURE_DEVOPS = "azure_devops"
    JENKINS = "jenkins"
    BITBUCKET = "bitbucket"
    CIRCLE_CI = "circle_ci"
    TEKTON = "tekton"


@dataclass
class PipelineConfig:
    """Configuration for pipeline generation"""

    provider: PipelineProvider
    # The cloud platforms the contract (and its overlays) bind, plus "openbao"
    # when a secretRef uses vault:// or openbao://: what the pipeline
    # authenticates to without a static key (``_keyless.CLOUD_AUTH_PLATFORMS``).
    cloud_auth: Tuple[str, ...] = ()
    # Pipeline working directory, relative to the SCM checkout root. Set when
    # `fluid generate ci` is invoked from a subfolder of a git repo so the
    # generated pipeline can cd into that folder before running fluid commands.
    # None or "" => no cd wrapper (steps run at checkout root).
    workdir: Optional[str] = None
    # Whether stage 3 (`fluid generate artifacts`) runs by default. False only
    # with ``fluid generate ci --no-generate-artifacts``.
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
    # Set this (via ``fluid generate ci --default-publish-target X``) to
    # make X the ``PUBLISH_TARGETS`` parameter's default AND the shell
    # fallback every stage-10 read of it uses (a Jenkins job's first build,
    # and its first after it lost its parameter definitions, runs with no
    # parameters exported to the shell). ``None`` keeps
    # ``datamesh-manager``. Common values: ``fluid-command-center``,
    # ``datamesh-manager``, ``horizon``, ``datahub``, ``collibra``.
    default_publish_target: Optional[str] = None
    # Generation defaults for the stage-9 verify strictness (VERIFY_STRICT)
    # and the stage-10 publish switch (RUN_STAGE_10_PUBLISH), so a launchpad
    # can ask ``fluid generate ci`` for the behavior it wants directly.
    verify_strict_default: bool = True
    publish_stage_default: bool = False
    # The contract the pipeline builds, relative to ``workdir`` (the CONTRACT
    # parameter's default and every stage's fallback for it). ``fluid
    # generate ci`` sets it to the contract it was given.
    contract_path: str = "contract.fluid.yaml"
    # The stage-0 ``FLUID_PACKAGE_SPEC`` default. ``None``: this version of
    # forge-cli with ``package_extras`` (``default_fluid_package_spec``).
    fluid_package_spec: Optional[str] = None
    # Extras of ``data-product-forge`` the contract needs, across its base
    # and every overlay (``fluid generate ci`` reads them from the bindings).
    package_extras: Optional[List[str]] = None
    # The ``APPLY_MODE`` default. ``None`` keeps each system's own: dry-run
    # for Jenkins, amend for the shared stage specs.
    apply_mode_default: Optional[str] = None
    # The ``APPLY_BUILD_ID`` default: the contract's build when it has
    # exactly one. Passed to ``fluid apply`` only with a build mode.
    apply_build_id_default: str = ""
    # Stage 11 defaults: the RUN_STAGE_11_SCHEDULE_SYNC toggle, the scheduler
    # and its destination (the shared DAG root; schedule-sync writes each
    # product into its own ``<root>/<contract id>/`` and deletes only there).
    schedule_sync_default: bool = False
    scheduler_default: str = ""
    scheduler_destination_default: str = ""
    # Stage 5 reads the plan the last successful build applied for the same
    # env (``fluid diff --last-applied``), so a change the contract makes is
    # "pending" rather than drift. Jenkins only; needs the copyartifact
    # plugin, which is why it is off unless asked for.
    diff_last_applied: bool = False
    # The ``FLUID_ENV`` default: the parameter's declared default and the
    # fallback of every shell read of it (``${FLUID_ENV:-<env>}``). ``None``
    # (or blank) keeps ``dev``. A build that gets no parameters (a Jenkins
    # job's first build, its first after a restart re-seeded it) runs in it,
    # and so does one an upstream trigger starts, which Jenkins runs with the
    # defaults. Generation raises ``ValueError`` unless every stage's
    # ``--env`` accepts it (:func:`check_env_name`), however the config was
    # built.
    fluid_env_default: Optional[str] = None
    # The branch whose pushes run the apply phase; pull requests against it
    # run the plan phase (``fluid generate ci --default-branch``).
    default_branch: str = "main"
    # The opt-in jobs beside the pipeline (``fluid generate ci --add``), from
    # ``_thin.ADD_ONS``: security, compliance, sbom.
    add_ons: Tuple[str, ...] = ()

    def __post_init__(self):
        branch = check_pipeline_literal("default branch", self.default_branch or "")
        if not branch or re.search(r"[\s*?\[\]~^:]", branch) or branch.startswith("-"):
            raise ValueError(f"default branch {branch!r} is not a branch name")
        if self.package_extras is None:
            self.package_extras = []

        self.cloud_auth = _keyless.normalize(self.cloud_auth or ())


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


class BasePipelineTemplate:
    """Base class for pipeline templates"""

    def __init__(self):
        self.provider_name = "unknown"
        self.file_extensions = [".yml"]

    def generate(self, config: PipelineConfig) -> Dict[str, str]:
        """Generate pipeline configuration"""
        raise NotImplementedError

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

    def _credential_banner(
        self,
        comment_prefix: str,
        ci_system_name: str,
        secret_surface_hint: str,
        cloud_auth: Tuple[str, ...] = (),
    ) -> str:
        """Render the credential-model banner every pipeline file starts with.

        The model is keyless: a generated pipeline holds no static cloud key.
        GitHub Actions and GitLab CI mint an OIDC ID token per job and the
        cloud trusts it; for them the banner lists the non-secret variables
        and what to configure on the cloud side. The other systems get the
        same honest statement and a pointer to their own OIDC or workload
        identity mechanism (see :mod:`._keyless`).

        Arguments:
          comment_prefix: ``# `` for YAML / shell; ``// `` for Groovy.
          ci_system_name: human-readable name shown in the banner.
          secret_surface_hint: one-line hint on where THIS system keeps
            variables (e.g. "Settings → Secrets and variables → Actions").
          cloud_auth: the platforms the contract binds, from
            ``PipelineConfig.cloud_auth``.
        """
        p = comment_prefix
        lines = [f"{p}FLUID CI/CD Pipeline — {ci_system_name}", f"{p}"]
        lines += [
            f"{p}{line}".rstrip() for line in _keyless.banner_lines(cloud_auth, ci_system_name)
        ]
        lines += [
            f"{p}",
            f"{p}Where {ci_system_name} keeps variables: {secret_surface_hint}",
            f"{p}",
        ]
        return "\n".join(lines) + "\n"

    #: The ``APPLY_MODE`` default when ``PipelineConfig.apply_mode_default``
    #: is unset. Jenkins keeps its own (dry-run).
    system_apply_mode_default = "amend"

    #: Parameters an operator may deliberately set to blank: their shell
    #: fallback applies only when the parameter is unset (``${X-default}``).
    #: For every other one a blank value also means the default.
    keep_blank_parameters = frozenset(
        {
            "APPLY_BUILD_ID",
            "SCHEDULER",
            "SCHEDULER_DESTINATION",
            "SCHEDULER_ENVIRONMENT_NAME",
            "SCHEDULER_LOCATION",
            "SCHEDULER_WORKSPACE",
            "FLUID_PIP_INDEX_URL",
            "FLUID_PIP_EXTRA_INDEX_URL",
        }
    )

    def _pipeline_defaults(self, config: Optional["PipelineConfig"] = None) -> Dict[str, str]:
        """The default of every parameter a stage reads.

        One table, rendered into the parameter declarations AND into every
        shell read of the parameter (``sh_param``), so a run that gets no
        parameters (a Jenkins job's first build, or its first after a
        restart re-seeded it) runs exactly what the declarations say.
        """

        def opt(name: str, fallback: str) -> str:
            value = getattr(config, name, None) if config is not None else None
            return value.strip() if isinstance(value, str) and value.strip() else fallback

        apply_mode = opt("apply_mode_default", self.system_apply_mode_default)
        if apply_mode not in APPLY_MODES:
            raise ValueError(f"apply mode default {apply_mode!r} is not one of {APPLY_MODES}")
        scheduler = opt("scheduler_default", "")
        if scheduler and scheduler not in SCHEDULERS:
            raise ValueError(f"scheduler default {scheduler!r} is not one of {SCHEDULERS}")
        spec = opt("fluid_package_spec", "") or default_fluid_package_spec(
            getattr(config, "package_extras", None)
        )
        fluid_env = check_env_name("FLUID_ENV default", opt("fluid_env_default", "dev"))
        defaults = {
            "CONTRACT": opt("contract_path", "contract.fluid.yaml"),
            "FLUID_ENV": fluid_env,
            "APPLY_MODE": apply_mode,
            "APPLY_BUILD_ID": opt("apply_build_id_default", ""),
            "ALLOW_DATA_LOSS": "false",
            "NO_VERIFY_DIGEST": "false",
            "PUBLISH_TARGETS": opt("default_publish_target", "datamesh-manager"),
            "SCHEDULER": scheduler,
            "SCHEDULER_DESTINATION": opt("scheduler_destination_default", ""),
            "SCHEDULER_ENVIRONMENT_NAME": "",
            "SCHEDULER_LOCATION": "",
            "SCHEDULER_WORKSPACE": "",
            "SCHEDULE_SYNC_DRY_RUN": "false",
            "FLUID_PACKAGE_SPEC": spec,
            "FLUID_PIP_INDEX_URL": "",
            "FLUID_PIP_EXTRA_INDEX_URL": "",
            "FLUID_ALLOW_PRERELEASE": "false",
        }
        for name, value in defaults.items():
            check_pipeline_literal(f"default of {name}", value)
        return defaults

    def _sh(self, name: str, defaults: Dict[str, str]) -> str:
        """``sh_param`` for ``name`` with its default from ``defaults``."""
        return sh_param(name, defaults[name], keep_blank=name in self.keep_blank_parameters)

    @staticmethod
    def _stage_toggle_default(num: int, config: "PipelineConfig") -> bool:
        """Whether stage ``num`` runs by default: every stage but the opt-in
        10 and 11, except stage 3 under ``--no-generate-artifacts``.
        ``fluid generate ci`` sets 10 and 11 on request."""
        if num == 3:
            return bool(config.generates_artifacts)
        if num == 10:
            return bool(config.publish_stage_default)
        if num == 11:
            return bool(config.schedule_sync_default)
        return True

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
          with the forge-cli checkout and installs it editable
          (``pip install -e``), which puts ``fluid`` on PATH. Fails loud
          with an actionable message if the mount is missing. Only supported on systems that run self-hosted
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
            # An editable install: it puts the ``fluid`` command on PATH and the
            # checkout's dependencies in the interpreter, and imports resolve
            # from the mount, so nothing has to outlive this script. A
            # PYTHONPATH export did not: it died with the step (GitHub) or the
            # ( ... ) subshell (Tekton, GitLab, Azure, Bitbucket, CircleCI), and
            # it never provided the command.
            return (
                "set -eu\n"
                "if [ ! -f /forge-cli-src/pyproject.toml ]; then\n"
                "  echo 'FATAL: dev-source install mode requires the forge-cli "
                "checkout mounted at /forge-cli-src' >&2\n"
                "  exit 2\n"
                "fi\n"
                "python -m pip install --quiet -e /forge-cli-src || "
                "{ echo 'FATAL: pip install -e /forge-cli-src failed' >&2; exit 3; }\n"
                "fluid --version" + engine_install
            )
        # pypi mode — TestPyPI overrides + optional --pre. Each value is ONE
        # argv token (``set --``): the index URLs used to be concatenated into
        # a string and re-parsed by ``sh -c``, so a value could add pip
        # options or run commands. ``--`` ends pip's options, so the package
        # spec cannot be one either.
        spec = self._sh("FLUID_PACKAGE_SPEC", self._pipeline_defaults(config))
        return (
            "set -eu\n"
            "python -m pip install --upgrade pip\n"
            "set -- --quiet\n"
            'if [ -n "${FLUID_PIP_INDEX_URL:-}" ]; then\n'
            '  set -- "$@" "--index-url=$FLUID_PIP_INDEX_URL"\n'
            "fi\n"
            'if [ -n "${FLUID_PIP_EXTRA_INDEX_URL:-}" ]; then\n'
            '  set -- "$@" "--extra-index-url=$FLUID_PIP_EXTRA_INDEX_URL"\n'
            "fi\n"
            'if [ "${FLUID_ALLOW_PRERELEASE:-false}" = "true" ]; then\n'
            '  set -- "$@" --pre\n'
            "fi\n"
            f'python -m pip install "$@" -- "{spec}"\n'
            "fluid --version" + engine_install
        )

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
        system automatically. The defaults are ``_pipeline_defaults``,
        the same values every stage command falls back to.
        """
        d = self._pipeline_defaults(config)
        params: List[Tuple[str, str, str, str]] = [
            # Global
            (
                "CONTRACT",
                "string",
                d["CONTRACT"],
                "Contract path relative to workspace (or workdir when set).",
            ),
            (
                "FLUID_ENV",
                "string",
                d["FLUID_ENV"],
                "Environment overlay (dev | staging | prod).",
            ),
        ]
        # 11 stage toggles
        from . import _thin  # _thin imports this module

        for stage in _thin.stages():
            params.append(
                (
                    stage.toggle,
                    "boolean",
                    "true" if self._stage_toggle_default(stage.num, config) else "false",
                    f"Stage {stage.num}: run {stage.name} (false switches it off).",
                )
            )
        # Apply-mode matrix
        params.extend(
            [
                (
                    "APPLY_MODE",
                    "choice:" + ",".join(APPLY_MODES),
                    d["APPLY_MODE"],
                    "Stage 6 plans and stage 7 applies this mode. ``replace*`` variants "
                    "require ALLOW_DATA_LOSS=true.",
                ),
                (
                    "APPLY_BUILD_ID",
                    "string",
                    d["APPLY_BUILD_ID"],
                    "Stage 7: build to run with amend-and-build / replace-and-build "
                    "(blank runs every build). Not passed with any other mode.",
                ),
                (
                    "ALLOW_DATA_LOSS",
                    "boolean",
                    d["ALLOW_DATA_LOSS"],
                    "Stage 7: gate for replace / replace-and-build modes.",
                ),
                (
                    "NO_VERIFY_DIGEST",
                    "boolean",
                    d["NO_VERIFY_DIGEST"],
                    "Stage 7: emergency escape that waives BOTH the plan-binding "
                    "and federation upstream-digest gates "
                    "(--no-verify-plan-binding --no-verify-federation). Audit log flag.",
                ),
                (
                    "PUBLISH_TARGETS",
                    "string",
                    d["PUBLISH_TARGETS"],
                    "Stage 10: space-separated catalog targets.",
                ),
                (
                    "SCHEDULER",
                    "choice:," + ",".join(SCHEDULERS),
                    d["SCHEDULER"],
                    "Stage 11: scheduler target. Blank = no-op.",
                ),
                (
                    "SCHEDULER_DESTINATION",
                    "string",
                    d["SCHEDULER_DESTINATION"],
                    "Stage 11: airflow/mwaa DAG root (s3:, gs:, az:, ssh:, scp:, file:); "
                    "each product syncs into <root>/<contract id>/.",
                ),
                (
                    "SCHEDULER_ENVIRONMENT_NAME",
                    "string",
                    d["SCHEDULER_ENVIRONMENT_NAME"],
                    "Stage 11: composer env name or astronomer deployment name.",
                ),
                (
                    "SCHEDULER_LOCATION",
                    "string",
                    d["SCHEDULER_LOCATION"],
                    "Stage 11: GCP region for composer.",
                ),
                (
                    "SCHEDULER_WORKSPACE",
                    "string",
                    d["SCHEDULER_WORKSPACE"],
                    "Stage 11: prefect workspace or dagster-cloud deployment name.",
                ),
                (
                    "SCHEDULE_SYNC_DRY_RUN",
                    "boolean",
                    d["SCHEDULE_SYNC_DRY_RUN"],
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
                        d["FLUID_PACKAGE_SPEC"],
                        "pip package spec. Defaults to the forge-cli version that generated "
                        "this pipeline, with the extras its contract needs.",
                    ),
                    (
                        "FLUID_PIP_INDEX_URL",
                        "string",
                        d["FLUID_PIP_INDEX_URL"],
                        "Primary pip index. Blank = stable PyPI; set TestPyPI URL for pilot builds.",
                    ),
                    (
                        "FLUID_PIP_EXTRA_INDEX_URL",
                        "string",
                        d["FLUID_PIP_EXTRA_INDEX_URL"],
                        "Fallback pip index for transitive deps.",
                    ),
                    (
                        "FLUID_ALLOW_PRERELEASE",
                        "boolean",
                        d["FLUID_ALLOW_PRERELEASE"],
                        "Pass pip --pre (pulls alpha/rc releases).",
                    ),
                ]
            )
        return params
