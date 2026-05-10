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

"""``fluid generate ci`` subcommand.

Generates CI/CD pipeline configurations for every supported CI system.
Backed by :class:`fluid_build.forge.core.pipeline_templates.PipelineTemplateGenerator`
— the same rich generator ``fluid forge`` auto-scaffolds from — so
users of ``fluid generate ci`` get the full deploy/airflow/publish
stage set, not the legacy stub templates.

Usage:
    fluid generate ci                                   # default: gitlab
    fluid generate ci --system github                   # GitHub Actions
    fluid generate ci --system jenkins                  # Jenkins (Jenkinsfile)
    fluid generate ci --system azure                    # Azure DevOps
    fluid generate ci --system bitbucket                # Bitbucket Pipelines
    fluid generate ci --system circleci                 # CircleCI
    fluid generate ci --system tekton                   # Tekton (2 files)
    fluid generate ci contract.fluid.yaml               # specify contract
    fluid generate ci --out .github/workflows/ci.yml    # custom output (single-file only)
    fluid generate ci --complexity advanced             # multi-environment + approvals
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from typing import Dict, Optional

from ._common import CLIError
from ._io import atomic_write
from ._logging import info

# Lazy-imported inside ``run`` so ``register_subcommand`` doesn't drag
# the full pipeline_templates module in for ``--help`` invocations.


# Public → internal enum mapping. The CLI surface uses short names; the
# PipelineProvider enum uses underscore_case for historical reasons.
_SYSTEM_ALIASES: Dict[str, str] = {
    # Canonical short names (what we recommend in docs).
    "github": "github_actions",
    "gitlab": "gitlab_ci",
    "jenkins": "jenkins",
    "azure": "azure_devops",
    "bitbucket": "bitbucket",
    "circleci": "circle_ci",
    "tekton": "tekton",
    # Long-form aliases so `--system github-actions` also works.
    "github-actions": "github_actions",
    "github_actions": "github_actions",
    "gitlab-ci": "gitlab_ci",
    "gitlab_ci": "gitlab_ci",
    "azure-devops": "azure_devops",
    "azure_devops": "azure_devops",
    "circle-ci": "circle_ci",
    "circle_ci": "circle_ci",
}

# Primary output path per system. Used only when ``--out`` is NOT
# supplied *and* the chosen system emits a single file. Multi-file
# systems (Tekton, GitHub Actions enterprise) ignore ``--out`` with a
# warning and keep their canonical paths so downstream tooling can find
# them by convention.
_PRIMARY_OUTPUT: Dict[str, str] = {
    "github_actions": ".github/workflows/fluid-standard.yml",
    "gitlab_ci": ".gitlab-ci.yml",
    "jenkins": "Jenkinsfile",
    "azure_devops": "azure-pipelines.yml",
    "bitbucket": "bitbucket-pipelines.yml",
    "circle_ci": ".circleci/config.yml",
    "tekton": "tekton/pipeline.yaml",
}


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register as a subcommand of ``fluid generate``."""
    p = subparsers.add_parser(
        "ci",
        help="Generate CI/CD pipeline (GitHub, GitLab, Jenkins, Azure, Bitbucket, CircleCI, Tekton)",
        description=(
            "Generate CI/CD pipeline configuration for your FLUID project.\n"
            "Covers validate / plan / apply / airflow-sync / publish stages."
        ),
        epilog="""Examples:
  fluid generate ci                          # GitLab CI (default)
  fluid generate ci --system github          # GitHub Actions
  fluid generate ci --system jenkins         # Jenkins (Jenkinsfile)
  fluid generate ci --system azure           # Azure DevOps Pipelines
  fluid generate ci --system bitbucket       # Bitbucket Pipelines
  fluid generate ci --system circleci        # CircleCI
  fluid generate ci --system tekton          # Tekton (writes tekton/*.yaml)
  fluid generate ci --complexity advanced    # multi-env with approvals
  fluid generate ci --out .github/workflows/ci.yml   # single-file systems only
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("contract", nargs="?", default="contract.fluid.yaml", help="contract.fluid.yaml")
    p.add_argument(
        "--system",
        choices=sorted(_SYSTEM_ALIASES.keys()),
        default="gitlab",
        help="CI system (default: gitlab)",
    )
    p.add_argument(
        "--complexity",
        choices=["basic", "standard", "advanced", "enterprise"],
        default="standard",
        help=(
            "Pipeline complexity (default: standard). "
            "basic = validate+apply; standard = full workflow with testing; "
            "advanced = multi-env + approvals; enterprise = + governance/compliance."
        ),
    )
    p.add_argument(
        "--out",
        help=(
            "Output path override (single-file systems only). "
            "Multi-file systems (tekton, enterprise GitHub) keep canonical paths."
        ),
    )
    p.add_argument(
        "--no-generate-artifacts",
        action="store_true",
        help=(
            "Skip the `fluid generate transformation` and `fluid generate schedule` "
            "stages. Use for reference-only contracts (hybrid-reference dbt, "
            "external Airflow) where artifacts are owned outside fluid. "
            "Auto-detected for contracts whose builds[].pattern is hybrid-reference."
        ),
    )
    p.add_argument(
        "--install-mode",
        choices=["pypi", "dev-source"],
        default="pypi",
        help=(
            "How the generated Jenkinsfile installs fluid at build time:\n"
            "  pypi (default)  production. `pip install data-product-forge`\n"
            "                  from stable PyPI. Override the package spec\n"
            "                  via FLUID_PACKAGE_SPEC env var at build time.\n"
            "  dev-source      lab / contributor only. Installs from a\n"
            "                  /forge-cli-src bind mount in the Jenkins\n"
            "                  container. Fails LOUD if the mount is\n"
            "                  missing — no silent fallback to PyPI.\n"
            "Only the Jenkins template uses this flag today; other CI\n"
            "systems ignore it."
        ),
    )
    p.add_argument(
        "--default-publish-target",
        default=None,
        metavar="TARGET",
        help=(
            "Opt-in fallback catalog target baked into Stage 10's\n"
            "publish shell as ``${PUBLISH_TARGETS:-<TARGET>}``. When\n"
            "omitted (the default), Stage 10 emits the bare\n"
            "``${PUBLISH_TARGETS}`` form with no shell fallback.\n"
            "\n"
            "Matters for the first Pipeline-from-SCM build Jenkins\n"
            "auto-triggers after a job is created: the parameters\n"
            "block's defaults are not exported as env vars to that\n"
            "first build, so without this flag the CLI publishes to\n"
            "its built-in default (``fluid-command-center``) which\n"
            "may not be reachable. Pick the value that matches your\n"
            "team's primary catalog (e.g. ``datamesh-manager``,\n"
            "``horizon``, ``datahub``, ``collibra``).\n"
            "Only the Jenkins template consumes this today."
        ),
    )
    p.add_argument(
        "--verify-strict-default",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Default value for Jenkins parameter VERIFY_STRICT. "
            "When omitted, generated Jenkinsfiles preserve the current "
            "default of true. Only the Jenkins template consumes this today."
        ),
    )
    p.add_argument(
        "--publish-stage-default",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Default value for Jenkins parameter RUN_STAGE_10_PUBLISH. "
            "When omitted, generated Jenkinsfiles preserve the current "
            "default of false. Only the Jenkins template consumes this today."
        ),
    )
    p.add_argument(
        "--publish-include-env",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Whether the generated Jenkins Stage 10 command includes "
            '``--env \\"${FLUID_ENV:-dev}\\"`` on ``fluid publish``. '
            "Default is FALSE because ``fluid publish`` does not accept "
            "``--env``; including it makes Stage 10 die with "
            "``unrecognized arguments: --env dev``. Pass "
            "``--publish-include-env`` to opt in (operators who wrap "
            "``fluid publish`` with a custom CLI that does accept --env). "
            "Only the Jenkins template consumes this today."
        ),
    )
    p.add_argument(
        "--runner-host-override",
        default="",
        metavar="HOST",
        help=(
            "Container-runtime loopback host override. Set to "
            "``host.docker.internal`` (Docker Desktop), the bridge IP "
            "(Linux Docker), ``host.containers.internal`` (Podman), or "
            "a Service name (Kubernetes) when the contract uses "
            "``host: localhost`` and the FLUID process runs inside a "
            "container that can't reach the operator's machine via "
            "localhost. Emitted as ``FLUID_RUNNER_HOST_OVERRIDE`` in "
            "the generated pipeline's environment block — the FLUID "
            "acquisition runner reads it via apply_loopback_host_override."
        ),
    )
    p.add_argument(
        "--list-engines",
        action="store_true",
        help=(
            "Print the supported acquisition / transformation engines + "
            "their per-source / per-sink pip-install plans, then exit. "
            "Useful when authoring a new contract — see what engine names "
            "the generator recognises and which source kinds / sink "
            "platforms have first-class pip-extras support. Adding new "
            "entries lives in fluid_build/forge/core/pipeline_systems/_engine_specs.py."
        ),
    )
    p.set_defaults(generate_sub="ci", func=_run_from_generate)


def _echo_install_mode_summary(install_mode: str, out_path: Optional[str]) -> None:
    """Print a single-line, human-readable summary of the install mode.

    Shows up AFTER the ``generate_ci_ok`` JSON log event, on stdout,
    unambiguous so an operator running ``fluid generate ci`` in a
    terminal sees at a glance which mode the generated Jenkinsfile
    expects. For programmatic consumers (CI wrapping CI), the JSON log
    event is still the source of truth; this echo is UX candy for
    humans.

    Uses ``print`` rather than ``console.cprint`` because cprint routes
    through Rich which treats ``[text]`` as style markup and silently
    strips the bracket. The whole point of this echo is to surface the
    mode tag unambiguously.
    """
    from fluid_build.cli.console import cprint

    dest = out_path or "<default path>"
    # markup=False tells Rich (via cprint) not to interpret "[text]" as
    # style markup — critical because our install-mode tag uses literal
    # square brackets.
    if install_mode == "pypi":
        cprint(f"[install-mode: pypi] Jenkinsfile written -> {dest}", markup=False)
        cprint(
            "  |- Jenkins installs: pip install data-product-forge (stable PyPI)",
            markup=False,
        )
        cprint("  |  Override at build time via these Jenkins parameters:", markup=False)
        cprint(
            "  |    FLUID_PACKAGE_SPEC        = 'data-product-forge==X.Y.Z'  (pin version)",
            markup=False,
        )
        cprint(
            "  |    FLUID_PIP_INDEX_URL       = 'https://test.pypi.org/simple/'  (TestPyPI)",
            markup=False,
        )
        cprint(
            "  |    FLUID_PIP_EXTRA_INDEX_URL = 'https://pypi.org/simple/'   (fallback)",
            markup=False,
        )
        cprint(
            "  |    FLUID_ALLOW_PRERELEASE    = true  (pip --pre, alpha/rc releases)",
            markup=False,
        )
    elif install_mode == "dev-source":
        cprint(f"[install-mode: dev-source] Jenkinsfile written -> {dest}", markup=False)
        cprint(
            "  |- Jenkins installs from /forge-cli-src bind mount (LAB ONLY)",
            markup=False,
        )
        cprint("  |  Requires docker-compose:", markup=False)
        cprint(
            "  |    - ${FORGE_CLI_REPO:-../../../forge-cli}:/forge-cli-src:ro",
            markup=False,
        )
        cprint("  |  Fails LOUD if the mount is missing - no silent fallback.", markup=False)
    else:
        # Defensive - template would have raised already, but log
        # something if it didn't.
        cprint(
            f"[install-mode: {install_mode}] Jenkinsfile written -> {dest}",
            markup=False,
        )


def _run_from_generate(args, logger: logging.Logger) -> int:
    """Entry point when called via ``fluid generate ci``."""
    return run(args, logger)


def _contract_is_reference_only(contract_path: str) -> bool:
    """Detect whether a contract is reference-only (no artifact generation).

    Returns True when any build uses ``pattern: hybrid-reference`` — these
    contracts point at externally-owned dbt projects / Airflow DAGs, so
    asking fluid to generate transformations or schedules is a no-op that
    only surfaces spurious pipeline failures. Returns False on any read or
    parse error so we err on the side of keeping the generate stages
    (the legacy behavior).
    """
    try:
        import yaml

        with open(contract_path) as fh:
            contract = yaml.safe_load(fh) or {}
    except (FileNotFoundError, OSError, ImportError):
        return False
    except Exception:
        return False
    builds = contract.get("builds") or []
    if not isinstance(builds, list):
        return False
    reference_patterns = {"hybrid-reference", "reference", "external-reference"}
    for build in builds:
        if isinstance(build, dict) and build.get("pattern") in reference_patterns:
            return True
    return False


def _extract_engine_context(contract_path: str) -> dict:
    """Read ``engine`` / ``source.kind`` / first ``binding.platform`` from contract.

    Returns a dict with ``engine``, ``source_kind``, ``sink_platform`` keys
    (any may be ``None``). The CI generator passes these into PipelineConfig
    so the per-engine pip-install plan in the bootstrap stage matches what
    the apply stage will actually invoke. Read errors return an empty dict
    (caller falls back to engine-agnostic bootstrap).
    """
    try:
        import yaml

        with open(contract_path) as fh:
            contract = yaml.safe_load(fh) or {}
    except (FileNotFoundError, OSError, ImportError, Exception):
        return {}

    builds = contract.get("builds") or []
    engine = None
    source_kind = None
    if isinstance(builds, list) and builds:
        first = builds[0] if isinstance(builds[0], dict) else {}
        engine = first.get("engine")
        properties = first.get("properties") or {}
        source = properties.get("source") or {}
        if isinstance(source, dict):
            source_kind = source.get("kind")

    sink_platform = None
    exposes = contract.get("exposes") or []
    if isinstance(exposes, list) and exposes:
        first_expose = exposes[0] if isinstance(exposes[0], dict) else {}
        binding = first_expose.get("binding") or {}
        if isinstance(binding, dict):
            sink_platform = binding.get("platform")

    return {
        "engine": engine,
        "source_kind": source_kind,
        "sink_platform": sink_platform,
    }


def _git_prefix() -> Optional[str]:
    """Return the current directory's path relative to the git repo root.

    Used by ``fluid generate ci`` so Jenkins-style pipelines (which run at
    the SCM checkout root, not the contract's folder) know to cd into the
    subfolder before executing fluid commands. Returns None when not inside
    a git repo or when git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    prefix = result.stdout.strip().rstrip("/")
    return prefix or None


def run(args, logger: logging.Logger) -> int:
    try:
        from fluid_build.forge.core.pipeline_templates import (
            PipelineComplexity,
            PipelineConfig,
            PipelineProvider,
            PipelineTemplateGenerator,
        )

        # ``--list-engines`` is a discovery short-circuit. Walks the
        # shared registry and prints what engine / source / sink combos
        # the generator can produce pip-extras for. Useful when authoring
        # a new contract and wondering what's supported.
        if getattr(args, "list_engines", False):
            from fluid_build.cli.console import cprint
            from fluid_build.forge.core.pipeline_systems._engine_specs import (
                _DBT_PLATFORM_ADAPTERS,
                _DLT_SINK_EXTRAS,
                _DLT_SOURCE_EXTRAS,
                _MELTANO_SINK_PACKAGES,
                _MELTANO_SOURCE_PACKAGES,
                resolve_engine_bootstrap,
            )

            cprint("Engines recognised by ``fluid generate ci`` bootstrap:", markup=False)
            cprint("", markup=False)
            for engine in (
                "dlt",
                "airbyte",
                "meltano",
                "dbt",
                "duckdb",
                "debezium",
                "kafka_connect",
            ):
                cprint(f"  {engine}:", markup=False)
                if engine == "dlt":
                    cprint(
                        f"    sources:  {', '.join(sorted(_DLT_SOURCE_EXTRAS))}",
                        markup=False,
                    )
                    cprint(
                        f"    sinks:    {', '.join(sorted(_DLT_SINK_EXTRAS))}",
                        markup=False,
                    )
                elif engine == "meltano":
                    cprint(
                        f"    sources:  {', '.join(sorted(_MELTANO_SOURCE_PACKAGES))}",
                        markup=False,
                    )
                    cprint(
                        f"    sinks:    {', '.join(sorted(_MELTANO_SINK_PACKAGES))}",
                        markup=False,
                    )
                elif engine == "dbt":
                    cprint(
                        f"    sinks:    {', '.join(sorted(_DBT_PLATFORM_ADAPTERS))}",
                        markup=False,
                    )
                # Sample resolution for the most common combo.
                sample = resolve_engine_bootstrap(
                    engine,
                    source_kind="postgres" if engine in ("dlt", "meltano") else None,
                    sink_platform="snowflake",
                )
                cprint(
                    f"    example pip: {' '.join(sample.packages) if sample.packages else '(none — JVM or runtime-only)'}",
                    markup=False,
                )
                cprint("", markup=False)
            cprint(
                "Add new entries in fluid_build/forge/core/pipeline_systems/_engine_specs.py",
                markup=False,
            )
            return 0

        system = getattr(args, "system", "gitlab")
        canonical = _SYSTEM_ALIASES.get(system, system)
        try:
            provider = PipelineProvider(canonical)
        except ValueError:
            raise CLIError(
                1,
                "generate_ci_unknown_system",
                {"system": system, "choices": sorted(_SYSTEM_ALIASES.keys())},
            )

        complexity_value = getattr(args, "complexity", "standard") or "standard"
        try:
            complexity = PipelineComplexity(complexity_value)
        except ValueError:
            raise CLIError(
                1,
                "generate_ci_unknown_complexity",
                {"complexity": complexity_value},
            )

        contract_path = getattr(args, "contract", None) or "contract.fluid.yaml"
        no_generate_flag = bool(getattr(args, "no_generate_artifacts", False))
        generates_artifacts = not (no_generate_flag or _contract_is_reference_only(contract_path))

        install_mode = getattr(args, "install_mode", "pypi") or "pypi"
        # ``--default-publish-target`` is opt-in: None/empty → emit the
        # bare ``${PUBLISH_TARGETS}`` form (pre-flag behaviour). A
        # non-empty value activates the ``${PUBLISH_TARGETS:-<value>}``
        # shell fallback.
        raw_default_publish = getattr(args, "default_publish_target", None)
        default_publish_target = (
            raw_default_publish.strip()
            if isinstance(raw_default_publish, str) and raw_default_publish.strip()
            else None
        )
        verify_strict_default_arg = getattr(args, "verify_strict_default", None)
        publish_stage_default_arg = getattr(args, "publish_stage_default", None)
        publish_include_env_arg = getattr(args, "publish_include_env", None)

        # Engine context drives the bootstrap stage's per-engine pip
        # install. Parsing failures degrade gracefully — the bootstrap
        # falls back to "no engine extras" and the operator sets
        # FLUID_EXTRA_PIP_SPECS at build time.
        engine_context = _extract_engine_context(contract_path)

        # Operator can override the runner-host loopback target via CLI
        # flag. Default empty = don't emit the env var (fine for prod
        # CI runners where source.connection.host points at a real
        # hostname). Lab-style demos using ``host: localhost`` set this
        # to ``host.docker.internal`` (Docker Desktop) or the bridge IP.
        runner_host_override = getattr(args, "runner_host_override", "") or ""

        config = PipelineConfig(
            provider=provider,
            complexity=complexity,
            workdir=_git_prefix(),
            generates_artifacts=generates_artifacts,
            install_mode=install_mode,
            default_publish_target=default_publish_target,
            engine=engine_context.get("engine"),
            source_kind=engine_context.get("source_kind"),
            sink_platform=engine_context.get("sink_platform"),
            runner_host_override=runner_host_override,
            verify_strict_default=(
                True if verify_strict_default_arg is None else bool(verify_strict_default_arg)
            ),
            publish_stage_default=(
                False if publish_stage_default_arg is None else bool(publish_stage_default_arg)
            ),
            publish_include_env=(
                False if publish_include_env_arg is None else bool(publish_include_env_arg)
            ),
        )
        files = PipelineTemplateGenerator().generate_pipeline(config)
        if not files:
            raise CLIError(
                1,
                "generate_ci_empty",
                {"system": system, "hint": "Template returned no files."},
            )

        # ``--out`` rewrites the *primary* file's path. Non-primary
        # files (Tekton's ``tekton/tasks.yaml``, GitHub's
        # ``.env.ci.example``) keep their canonical paths so downstream
        # tooling finds them where expected. Log a hint when the system
        # emits multiple files so operators know the override only
        # touched the main one.
        out_override = getattr(args, "out", None)
        primary = _PRIMARY_OUTPUT.get(canonical)
        if out_override:
            if primary and primary in files:
                files = {
                    (out_override if rel == primary else rel): content
                    for rel, content in files.items()
                }
                if len(files) > 1:
                    info(
                        logger,
                        "generate_ci_out_secondary_files_retained",
                        system=system,
                        primary=out_override,
                    )
            elif len(files) == 1:
                only_key = next(iter(files))
                files = {out_override: files[only_key]}
            else:
                info(
                    logger,
                    "generate_ci_out_ignored_multifile",
                    system=system,
                    file_count=len(files),
                )
        for rel, content in sorted(files.items()):
            directory = os.path.dirname(rel)
            if directory:
                os.makedirs(directory, exist_ok=True)
            atomic_write(rel, content)
            info(
                logger,
                "generate_ci_ok",
                out=rel,
                system=system,
                primary=(rel == primary or rel == out_override),
            )

        # Human-readable terminal echo — unambiguous single line so
        # operators see at a glance which install mode the generated
        # CI file expects. Only emitted for systems that actually
        # consume the install-mode flag (currently Jenkins only); for
        # other systems it's a no-op.
        if canonical == "jenkins":
            primary_path = out_override or primary
            _echo_install_mode_summary(install_mode, primary_path)

        return 0
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "generate_ci_failed", {"error": str(e)})
