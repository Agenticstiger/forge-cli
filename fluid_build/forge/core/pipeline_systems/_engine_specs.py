# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Engine pip-spec registry for ``fluid generate ci`` Jenkinsfile/CI emitters.

Each FLUID acquisition or transformation build uses an engine
(``dlt`` / ``airbyte`` / ``meltano`` / ``debezium`` / ``kafka_connect``
/ ``duckdb`` / ``dbt``). The CI runner needs the engine's Python
package(s) AND any source-kind / sink-platform-specific extras to be
``pip install``-ed before the FLUID 11-stage pipeline runs. Without
that, stage-7 apply hits ``ModuleNotFoundError`` for the engine's
runtime deps (e.g. ``No module named 'dlt'`` or ``'sqlalchemy'``).

Per /borrow-before-build receipts (search 2026-05):

- **Meltano** (https://docs.meltano.com/guide/plugin-management/) —
  per-source / per-target plugin registry with ``pip_url``. We mirror
  the *vocabulary* (per-source + per-target package list) but skip the
  full plugin-system layer; a flat dict keeps the registry small.
- **dlt** (https://dlthub.com/docs/reference/command-line-interface) —
  ``dlt init <source> <destination>`` emits requirements.txt with
  ``dlt[<extras>]`` pinned. We compute the same extras list at CI
  generate time from the contract's ``builds[].properties.source.kind``
  and the bound destination ``binding.platform``.
- **dbt-core** (https://docs.getdbt.com/docs/supported-data-platforms)
  — adapters are separate pip packages keyed by warehouse. We map
  ``binding.platform`` → adapter package name.
- **PyAirbyte** (https://docs.airbyte.com/using-airbyte/pyairbyte/)
  — single ``airbyte`` pip package; source connectors run as Docker
  images downloaded at runtime, no per-source pip extras needed.
- **Debezium / Kafka Connect** — JVM-based; not pip-installable. We
  emit a comment instead of a no-op, so operators using the Kafka
  Connect engine know they need a separate Connect cluster.

Adding a new engine = one new entry in ``_ENGINE_SPECS`` + (if
needed) extending the per-source / per-sink lookups. Keep the
registry the source of truth — the CI emitters import from here, not
the other way around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class EngineBootstrap:
    """Resolved pip-install plan for a (engine, source_kind, sink_platform).

    ``packages`` are pip specs ready to splice into a single
    ``pip install <packages...>`` command. ``notes`` carries any
    operator-facing caveats (e.g. "engine is JVM-based; pip cannot
    install it") that the CI emitter renders as a comment.

    Empty ``packages`` is valid — means "no engine-side pip work
    needed" (PyAirbyte's source connectors via Docker, Debezium /
    Kafka Connect cluster managed externally).
    """

    packages: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class EngineRuntime:
    """Per-engine exec-time requirements (env vars, bind mounts, services).

    Distinct from ``EngineBootstrap`` (pip packages, build-time): these
    are facts the CI runner needs at job-execution time. Each CI emitter
    queries this and surfaces the requirements in its own dialect:

    - ``env_vars`` — splice into Jenkins ``environment {}``, GHA ``env:``,
      GitLab ``variables:``, Tekton ``Task.spec.steps[].env``, etc.
    - ``needs_docker_socket`` — runner needs ``/var/run/docker.sock``
      bind-mounted (or a sibling ``dind`` container reachable via
      ``DOCKER_HOST``). PyAirbyte spawns source connectors as Docker
      images; debezium/kafka_connect runners can also use this when
      operators choose to launch a side-car Connect cluster from CI.
    - ``needs_external_services`` — symbolic names for managed services
      the engine assumes are reachable (e.g. ``kafka_connect_cluster``,
      ``kerberos_kdc``). Operators wire the connection details via env;
      the registry just declares the dependency so dashboards / docs
      can show "this CI job needs X reachable".
    - ``notes`` — operator-facing one-liners the CI emitter renders as
      comments (Jenkinsfile ``//``, GHA ``#``, Tekton YAML ``#``).

    The empty default (``EngineRuntime()``) means "no special runtime
    needed beyond the engine's pip packages" — true for dlt, meltano,
    dbt, duckdb. Pattern borrowed from Dagster's ``required_resource_keys``
    (declarative resource dependency, queryable from generators).
    """

    env_vars: Dict[str, str] = field(default_factory=dict)
    needs_docker_socket: bool = False
    needs_external_services: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ── dlt: extras chosen by source kind + sink platform ──────────────────

# dlt's ``[sql_database]`` extra brings sqlalchemy + a generic SQL source;
# ``[snowflake]`` / ``[bigquery]`` / etc. add the destination adapter.
# Source kinds that aren't generic SQL (filesystem, kafka) use a
# different extra. Add new entries here as new source kinds land.
_DLT_SOURCE_EXTRAS: Dict[str, List[str]] = {
    "postgres": ["sql_database"],
    "postgresql": ["sql_database"],
    "mysql": ["sql_database"],
    "mariadb": ["sql_database"],
    "mssql": ["sql_database"],
    "sqlserver": ["sql_database"],
    "oracle": ["sql_database"],
    "snowflake": ["sql_database"],
    "bigquery": ["sql_database"],
    "redshift": ["sql_database"],
    "s3": ["filesystem"],
    "gcs": ["filesystem"],
    "azure-blob": ["filesystem"],
    "filesystem": ["filesystem"],
    "rest": [],   # rest_api source ships in dlt core, no extra needed
    "api": [],
    "kafka": [],  # dlt has no first-party Kafka source — operator wires custom
}

# dlt's destination extras keyed by FLUID-canonical sink platform name.
_DLT_SINK_EXTRAS: Dict[str, List[str]] = {
    "snowflake": ["snowflake"],
    "bigquery": ["bigquery"],
    "redshift": ["redshift"],
    "postgres": ["postgres"],
    "postgresql": ["postgres"],
    "duckdb": ["duckdb"],
    "athena": ["athena"],
    "databricks": ["databricks"],
    "mssql": ["mssql"],
    "filesystem": ["filesystem"],
    "s3": ["filesystem"],
    "gcs": ["filesystem"],
    "azure-blob": ["filesystem"],
}

# Extra non-dlt packages required by specific dlt source kinds.
# tap-postgres / dlt-sql-database use psycopg3 (NOT psycopg2). Other
# dialects bring their own dialect via SQLAlchemy.
_DLT_SOURCE_EXTRA_PACKAGES: Dict[str, List[str]] = {
    "postgres": ["psycopg[binary]>=3.1"],
    "postgresql": ["psycopg[binary]>=3.1"],
    "mysql": ["pymysql>=1.1"],
    "mariadb": ["pymysql>=1.1"],
    "mssql": ["pyodbc>=5.0"],
    "sqlserver": ["pyodbc>=5.0"],
    "oracle": ["oracledb>=2.0"],
}


# ── Meltano: Singer tap + target packages by source kind / sink platform ──

# meltanolabs-* are the SDK-rebuilt Singer plugins maintained by Meltano
# (https://hub.meltano.com/). Older Singer-IO taps (tap-postgres without
# the meltanolabs- prefix) exist but the SDK rebuilds are the supported
# path going forward. Add new entries as the lab onboards more sources.
_MELTANO_SOURCE_PACKAGES: Dict[str, List[str]] = {
    "postgres": ["meltanolabs-tap-postgres>=0.8"],
    "postgresql": ["meltanolabs-tap-postgres>=0.8"],
    "mysql": ["meltanolabs-tap-mysql"],
    "mssql": ["meltanolabs-tap-mssql"],
    "sqlserver": ["meltanolabs-tap-mssql"],
    "snowflake": ["tap-snowflake"],
    "bigquery": ["tap-bigquery"],
    "salesforce": ["tap-salesforce"],
    "stripe": ["tap-stripe"],
    "github": ["tap-github"],
    "rest": ["tap-rest-api-msdk"],
    "csv": ["meltanolabs-tap-csv"],
}

_MELTANO_SINK_PACKAGES: Dict[str, List[str]] = {
    "snowflake": ["meltanolabs-target-snowflake>=0.18"],
    "bigquery": ["target-bigquery"],
    "redshift": ["target-redshift"],
    "postgres": ["meltanolabs-target-postgres"],
    "postgresql": ["meltanolabs-target-postgres"],
    "duckdb": ["target-duckdb"],
    "csv": ["target-csv"],
    "jsonl": ["target-jsonl"],
}


# ── dbt: adapter package by warehouse platform ─────────────────────────

# dbt-core is the engine; the per-warehouse adapter is a separate pip
# package. Versions pin to the dbt 1.7+ series (matches the lab's
# A1/A2/B1/B2 dbt projects).
_DBT_PLATFORM_ADAPTERS: Dict[str, List[str]] = {
    "snowflake": ["dbt-snowflake>=1.7"],
    "bigquery": ["dbt-bigquery>=1.7"],
    "redshift": ["dbt-redshift>=1.7"],
    "postgres": ["dbt-postgres>=1.7"],
    "postgresql": ["dbt-postgres>=1.7"],
    "databricks": ["dbt-databricks>=1.7"],
    "spark": ["dbt-spark>=1.7"],
    "duckdb": ["dbt-duckdb>=1.7"],
    "athena": ["dbt-athena-community>=1.7"],
    "trino": ["dbt-trino>=1.7"],
    "clickhouse": ["dbt-clickhouse>=1.7"],
}


def resolve_engine_bootstrap(
    engine: Optional[str],
    *,
    source_kind: Optional[str] = None,
    sink_platform: Optional[str] = None,
) -> EngineBootstrap:
    """Compute the pip install plan for a (engine, source, sink) combo.

    Returns ``EngineBootstrap(packages=[], notes=[])`` for unknown or
    missing engines — caller treats that as "no engine-side bootstrap
    needed" and emits no extra pip install line.

    Engines covered:

    - ``dlt`` — ``dlt[source_extras + sink_extras]`` plus per-source
      driver packages (psycopg, pymysql, pyodbc).
    - ``airbyte`` — single ``airbyte>=0.45``; source connectors via
      Docker at runtime.
    - ``meltano`` — per-source tap + per-sink target Singer packages.
    - ``debezium`` / ``kafka_connect`` — JVM-based; emit a note
      instead of pip packages so the CI emitter can render an operator-
      visible comment.
    - ``duckdb`` — ``duckdb>=1.0`` standalone (or via ``dlt[duckdb]``
      when used as a dlt destination).
    - ``dbt`` — ``dbt-core`` + the warehouse-specific adapter
      (``dbt-snowflake`` / ``dbt-bigquery`` / etc.) keyed off the
      contract's binding platform.
    """
    if not engine:
        return EngineBootstrap()
    e = engine.strip().lower()
    src = (source_kind or "").strip().lower() or None
    snk = (sink_platform or "").strip().lower() or None

    if e == "dlt":
        return _resolve_dlt(src, snk)
    if e == "airbyte":
        # PyAirbyte's source connectors are downloaded as Docker images
        # at runtime; no per-source pip extras needed. Single base spec.
        # Floor at >=0.20 (last version that publishes 3.13 wheels);
        # cap with major bound so future breaking changes don't bite.
        # Operators on Python 3.12 / 3.11 will pick the latest 0.x.
        return EngineBootstrap(packages=["airbyte>=0.20,<1"])
    if e == "meltano":
        return _resolve_meltano(src, snk)
    if e == "duckdb":
        return EngineBootstrap(packages=["duckdb>=1.0"])
    if e == "dbt":
        return _resolve_dbt(snk)
    if e in ("debezium", "kafka_connect"):
        return EngineBootstrap(
            packages=[],
            notes=[
                f"engine={e!r} is JVM-based — no Python package to install. "
                "Run a separate Kafka Connect / Debezium cluster (e.g. via "
                "docker-compose) and point the contract's connector config "
                "at it. The Jenkinsfile only needs forge-cli + the Snowflake "
                "destination adapter installed.",
            ],
        )
    # Unknown engine — caller can still proceed; surface as a note so
    # operators see "we don't know about this engine" rather than a
    # silent miss.
    return EngineBootstrap(
        packages=[],
        notes=[
            f"engine={engine!r} not in _ENGINE_SPECS registry — operator "
            "must add the right pip packages to FLUID_EXTRA_PIP_SPECS env "
            "var or the Jenkinsfile bootstrap.",
        ],
    )


def _resolve_dlt(source_kind: Optional[str], sink_platform: Optional[str]) -> EngineBootstrap:
    extras = []
    if source_kind:
        extras.extend(_DLT_SOURCE_EXTRAS.get(source_kind, []))
    if sink_platform:
        extras.extend(_DLT_SINK_EXTRAS.get(sink_platform, []))
    extras = sorted(set(extras))  # dedup + stable order
    if extras:
        base = f"dlt[{','.join(extras)}]>=1.0"
    else:
        base = "dlt>=1.0"
    packages = [base]
    if source_kind and source_kind in _DLT_SOURCE_EXTRA_PACKAGES:
        packages.extend(_DLT_SOURCE_EXTRA_PACKAGES[source_kind])
    return EngineBootstrap(packages=packages)


def _resolve_meltano(
    source_kind: Optional[str], sink_platform: Optional[str]
) -> EngineBootstrap:
    packages: List[str] = []
    notes: List[str] = []
    if source_kind:
        src_pkgs = _MELTANO_SOURCE_PACKAGES.get(source_kind)
        if src_pkgs:
            packages.extend(src_pkgs)
        else:
            notes.append(
                f"meltano source.kind={source_kind!r} not in registry — "
                "find the right Singer tap at hub.meltano.com and add via "
                "FLUID_EXTRA_PIP_SPECS env var."
            )
    if sink_platform:
        snk_pkgs = _MELTANO_SINK_PACKAGES.get(sink_platform)
        if snk_pkgs:
            packages.extend(snk_pkgs)
        else:
            notes.append(
                f"meltano sink platform={sink_platform!r} not in registry — "
                "find the right Singer target at hub.meltano.com and add via "
                "FLUID_EXTRA_PIP_SPECS env var."
            )
    return EngineBootstrap(packages=packages, notes=notes)


def _resolve_dbt(sink_platform: Optional[str]) -> EngineBootstrap:
    packages: List[str] = ["dbt-core>=1.7"]
    notes: List[str] = []
    if sink_platform:
        adapter_pkgs = _DBT_PLATFORM_ADAPTERS.get(sink_platform)
        if adapter_pkgs:
            packages.extend(adapter_pkgs)
        else:
            notes.append(
                f"dbt adapter for platform={sink_platform!r} not in registry — "
                "see https://docs.getdbt.com/docs/supported-data-platforms and "
                "add the pip package via FLUID_EXTRA_PIP_SPECS env var."
            )
    return EngineBootstrap(packages=packages, notes=notes)


__all__ = [
    "EngineBootstrap",
    "EngineRuntime",
    "resolve_engine_bootstrap",
    "resolve_engine_runtime",
    "render_pip_install_command",
    "render_bootstrap_shell_section",
    "render_runner_env_vars",
    "render_runtime_notes",
]


# ── Per-engine runtime requirements (env vars, docker socket, services) ──
#
# Keep this table next to ``resolve_engine_bootstrap`` so adding a new
# engine = one entry here + one in ``_ENGINE_SPECS``. CI emitters never
# branch on engine name themselves — they query this resolver, which
# keeps "what airbyte needs" out of seven separate emitter files.


def resolve_engine_runtime(engine: Optional[str]) -> EngineRuntime:
    """Return per-engine exec-time runtime requirements.

    Generators use this to decide which env vars to inject into the
    ``environment {}`` block, whether to surface a "bind-mount the
    docker socket" comment, and which external services to mention in
    operator-facing notes.

    Engines covered:

    - ``airbyte`` — needs ``/var/run/docker.sock`` (PyAirbyte spawns
      source connectors as Docker images on the host daemon) and the
      ``AIRBYTE_PROJECT_DIR`` env var pinned to a writable temp dir
      (PyAirbyte caches ``Path.cwd()`` at module import for connector
      mount dirs; CI runners typically import from ``/`` which makes
      ``/<connector>`` unwritable).
    - ``debezium`` / ``kafka_connect`` — JVM-based, run as a separate
      Connect cluster. The runner can spawn a side-car cluster via
      Docker (needs the socket) OR connect to an existing cluster via
      ``KAFKA_CONNECT_URL``. We declare the docker-socket need as an
      affordance; operators can ignore it if pointing at an existing
      cluster.
    - ``dlt`` / ``meltano`` / ``dbt`` / ``duckdb`` — pure Python in-
      process; no special runtime requirements beyond the engine's pip
      packages.
    - Unknown engines → empty (caller treats as "no extra runtime
      needs"). The bootstrap resolver already emits a note for unknowns.
    """
    if not engine:
        return EngineRuntime()
    e = engine.strip().lower()

    if e == "airbyte":
        # The two ``/tmp/...`` strings below trip bandit B108
        # (hardcoded_tmp_directory) but they are env-var VALUES emitted
        # into generated CI files for PyAirbyte's connector executor,
        # not paths used by Python's tempfile module. The /tmp prefix
        # is REQUIRED because PyAirbyte's docker executor mounts the
        # host's tempfile.gettempdir() into the spawned source-connector
        # container at /airbyte/tmp; the path must exist symmetrically
        # on host + runner for DinD to work (operator bind-mounts
        # /tmp/airbyte host->runner). The B108 warning does not apply
        # to declarative env-var emission.
        return EngineRuntime(
            env_vars={
                "AIRBYTE_PROJECT_DIR": "/tmp/airbyte",  # nosec B108
                # PyAirbyte's docker executor uses tempfile.gettempdir() (default
                # ``/tmp``) as the host source for the ``/airbyte/tmp`` bind in
                # the spawned connector container. When the FLUID runner is
                # itself in a container talking to the HOST docker daemon (DinD),
                # the daemon resolves the host-side path AS-IS — the runner
                # container's ``/tmp`` is invisible to the host. Pinning
                # ``AIRBYTE_TEMP_DIR`` to a subdir of ``AIRBYTE_PROJECT_DIR``
                # (which the lab/CI runner bind-mounts symmetrically host↔
                # container) makes the path resolve on both sides. Without
                # this, the spawned connector hits ``NoSuchFileException:
                # /airbyte/tmp/tmpXXX.json`` because the host's ``/tmp`` is
                # empty (the temp file lives in the runner container's ``/tmp``).
                "AIRBYTE_TEMP_DIR": "/tmp/airbyte/tmp",  # nosec B108
            },
            needs_docker_socket=True,
            notes=[
                "engine='airbyte': PyAirbyte spawns source connectors as "
                "Docker images on the host daemon. The CI runner needs "
                "/var/run/docker.sock bind-mounted (Jenkins controller, "
                "GHA self-hosted, GitLab DinD, K8s privileged sidecar).",
                "AIRBYTE_PROJECT_DIR pinned to /tmp/airbyte: PyAirbyte "
                "caches Path.cwd() at module-import time and reuses it "
                "for connector mount dirs. CI runners typically import "
                "from / (root) which makes /<connector-name> unwritable.",
                "AIRBYTE_TEMP_DIR pinned to /tmp/airbyte/tmp: PyAirbyte "
                "passes its config file via tempfile.gettempdir() and "
                "mounts THAT path into the connector container. With DinD "
                "the host daemon needs the same path on both sides, so "
                "we keep the temp dir inside the bind-mounted project dir.",
                "REQUIRES BIND-MOUNT: /tmp/airbyte (host) → /tmp/airbyte "
                "(runner). The path MUST be identical on both sides — when "
                "the runner tells the host docker daemon `docker run -v "
                "/tmp/airbyte/tmp:/airbyte/tmp ...`, the daemon resolves "
                "`/tmp/airbyte/tmp` against the HOST filesystem (it has no "
                "view of the runner container's paths). Asymmetric mounts "
                "(e.g. `<host>/runtime/airbyte:/tmp/airbyte` in the runner's "
                "compose) make the runner write succeed but the spawned "
                "connector hit NoSuchFileException because the host's "
                "`/tmp/airbyte/tmp` is empty.",
            ],
        )

    if e in ("debezium", "kafka_connect"):
        return EngineRuntime(
            needs_docker_socket=True,
            needs_external_services=["kafka_connect_cluster"],
            notes=[
                f"engine={e!r}: JVM-based; runs as a separate Kafka "
                "Connect cluster. Two options for the CI runner: (a) "
                "spawn a side-car Connect cluster via Docker (needs "
                "/var/run/docker.sock + a connect-distributed image), "
                "or (b) connect to an existing cluster via "
                "KAFKA_CONNECT_URL env var. Option (b) is recommended "
                "for prod; option (a) is convenient for the lab.",
            ],
        )

    # dlt / meltano / dbt / duckdb / unknown — pure Python, no runtime
    # extras beyond what the engine's pip packages already bring in.
    return EngineRuntime()


# ── CI-system-agnostic rendering helpers ───────────────────────────────
#
# These return strings ready to splice into ANY CI system's shell step:
# Jenkins ``sh ''' '''``, GitHub Actions ``run: |``, GitLab CI ``script:``,
# Tekton ``Task.spec.steps[].script``, etc. Each CI emitter wraps the
# returned text in its own indentation / quoting; the helpers don't
# assume any one CI dialect.
#
# Why these live here (not in each CI emitter): forge-cli targets seven
# CI systems (jenkins / github_actions / gitlab_ci / circle_ci /
# azure_devops / bitbucket / tekton). Without shared helpers the
# engine-aware bootstrap drifts across emitters. One source of truth
# here = one place to add a new engine, fix a pin, or rotate a default.


def render_pip_install_command(
    bootstrap: EngineBootstrap,
    *,
    pip_executable: str = "pip",
    extra_args: str = "--quiet",
) -> str:
    """Render a single ``pip install`` line for the engine's packages.

    Returns ``""`` (empty) when ``bootstrap.packages`` is empty — caller
    skips the line cleanly without needing to write a guard. Output is
    a one-liner with deduplicated, alphabetically-sorted packages so
    the command is reproducible across runs.

    Examples
    --------
    >>> from ._engine_specs import resolve_engine_bootstrap, render_pip_install_command
    >>> b = resolve_engine_bootstrap("dlt", source_kind="postgres", sink_platform="snowflake")
    >>> render_pip_install_command(b)
    'pip install --quiet "dlt[snowflake,sql_database]>=1.0" "psycopg[binary]>=3.1"'
    """
    if not bootstrap.packages:
        return ""
    # Quote each spec so shells don't interpret ``>`` / ``[`` / ``]``.
    quoted = " ".join(f'"{pkg}"' for pkg in sorted(set(bootstrap.packages)))
    return f"{pip_executable} install {extra_args} {quoted}".strip()


def render_bootstrap_shell_section(
    engine: Optional[str],
    source_kind: Optional[str],
    sink_platform: Optional[str],
    *,
    pip_executable: str = "pip",
    include_pydantic_settings: bool = True,
    indent: str = "  ",
) -> str:
    """Render a multi-line bootstrap shell section: forge-cli hard-deps
    + per-engine pip extras + operator-visible notes.

    Output is plain shell (no Jenkins / GitHub Actions / etc. syntax).
    Each emitter wraps in its own shell step. ``indent`` is the leading
    indentation for each line (matches the emitter's nesting).

    ``include_pydantic_settings=True`` (default) prepends a
    ``pip install pydantic-settings>=2.0,<3`` line — required by
    forge-cli's credentials introspector. Set False when forge-cli is
    installed from PyPI (where it pulls pydantic-settings transitively)
    OR when the operator manages it externally.

    Returns ``""`` when there's nothing to install (no engine + no
    hard deps requested) so the caller can skip the whole section.
    """
    bootstrap = resolve_engine_bootstrap(
        engine, source_kind=source_kind, sink_platform=sink_platform
    )
    lines: List[str] = []

    if include_pydantic_settings:
        lines.append(
            f"{indent}# forge-cli hard-dep — required by the credential introspector."
        )
        lines.append(
            f'{indent}{pip_executable} install --quiet "pydantic-settings>=2.0,<3"'
        )

    if bootstrap.notes:
        for note in bootstrap.notes:
            # Wrap multi-line notes by line; each becomes a shell comment.
            for chunk in note.split("\n"):
                lines.append(f"{indent}# {chunk}".rstrip())

    pip_line = render_pip_install_command(bootstrap, pip_executable=pip_executable)
    if pip_line:
        lines.append(
            f"{indent}# Per-engine pip extras (engine={engine!r}, source={source_kind!r}, "
            f"sink={sink_platform!r})."
        )
        lines.append(f"{indent}{pip_line}")

    if not lines:
        return ""
    return "\n".join(lines)


def render_runner_env_vars(
    runner_host_override: str = "",
    *,
    engine: Optional[str] = None,
) -> Dict[str, str]:
    """Return CI-runner env vars derived from PipelineConfig knobs + engine.

    Combines:

    - ``FLUID_RUNNER_HOST_OVERRIDE`` when set — the FLUID acquisition
      runners (dlt / airbyte / meltano source adapters) read it to
      rewrite contract-author ``host: localhost`` to a host-reachable
      address (``host.docker.internal`` etc.) when running inside a
      container.
    - Per-engine ``env_vars`` from :func:`resolve_engine_runtime` — e.g.
      ``AIRBYTE_PROJECT_DIR=/tmp/airbyte`` for ``engine='airbyte'``. The
      registry owns the engine→env-var mapping; this helper just merges.

    Returns ``{}`` when there's nothing to emit (no override + no engine-
    specific env) so callers can skip the env block cleanly. Operator-
    set ``runner_host_override`` wins over engine defaults when keys
    collide (preserves operator intent).
    """
    env: Dict[str, str] = {}
    # Engine-declared env vars first; operator overrides win on key clash.
    runtime = resolve_engine_runtime(engine)
    env.update(runtime.env_vars)
    if runner_host_override:
        env["FLUID_RUNNER_HOST_OVERRIDE"] = runner_host_override
    return env


def render_runtime_notes(
    engine: Optional[str],
    *,
    indent: str = "// ",
    include_external_services: bool = True,
) -> str:
    """Render per-engine runtime notes as commented lines for any CI file.

    Each CI emitter wraps the output in its own indentation (Jenkinsfile
    ``//``, GHA / GitLab / Tekton ``#``). Default ``indent='// '``
    matches Jenkins / Groovy comment syntax; pass ``indent='# '`` for
    YAML-style CIs.

    Surfaces:

    - The ``EngineRuntime.notes`` text (e.g. why the docker socket is
      needed, why ``AIRBYTE_PROJECT_DIR`` is pinned).
    - When ``needs_docker_socket=True``: a ``REQUIRES: …`` line so
      operators eyeballing the generated file see the bind-mount
      requirement before they hit a runtime error.
    - When ``needs_external_services``: a ``REQUIRES SERVICE: …`` line
      per service.

    Returns ``""`` when the engine has no runtime requirements (dlt,
    meltano, dbt, duckdb) so the caller can skip the comment block.
    """
    runtime = resolve_engine_runtime(engine)
    if not (runtime.notes or runtime.needs_docker_socket or runtime.needs_external_services):
        return ""

    lines: List[str] = []
    if runtime.needs_docker_socket:
        lines.append(
            f"{indent}REQUIRES: /var/run/docker.sock bind-mounted into the "
            "runner (engine spawns containers at exec time)."
        )
    if include_external_services:
        for svc in runtime.needs_external_services:
            lines.append(f"{indent}REQUIRES SERVICE: {svc}")
    for note in runtime.notes:
        # Wrap multi-line notes as separate comment lines.
        for chunk in note.split("\n"):
            lines.append(f"{indent}{chunk}".rstrip())
    return "\n".join(lines)
