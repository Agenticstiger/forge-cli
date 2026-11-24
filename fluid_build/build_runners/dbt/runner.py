# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Runtime dbt build runner.

Locates a build's dbt project, generates or discovers a ``profiles.yml``,
composes a ``dbt build`` command (with container fallback when the local
dbt can't load the required adapter), executes it, and reports results.

Entry points:

- :func:`resolve_dbt_project_path` — locate the build's dbt project root.
- :func:`build_dbt_command` — compose the ``dbt build`` argv.
- :func:`execute_dbt_build` — run the command with retry/iteration logic.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import yaml

from fluid_build._console import cprint, success
from fluid_build._console import error as console_error
from fluid_build.util.contract import (
    SCHEDULER_TRIGGER_TYPES,
    is_continuous_build,
    trigger_type_of,
)
from fluid_build.util.env_templates import EnvTemplateError

from .._path_safety import confine_to_workspace
from ..base import (
    SENSITIVE_ENV_KEY_RE,
    _print_typed_error,
    _resolve_env_placeholders,
    leave_to_its_scheduler,
    refuse_continuous_trigger,
    unknown_trigger,
)
from .profiles import (
    DbtProfileError,
    _list_profile_targets,
    _load_dbt_project_config,
    _resolve_dbt_profile_name,
    _resolve_dbt_target_name,
    resolve_dbt_profiles_dir,
)

LOG = logging.getLogger("fluid.build_runners.dbt.runner")

# Any env key starting with one of these prefixes is forwarded into the dbt
# container. Covers the common adapter conventions. For adapters that use bare,
# unprefixed env vars (e.g. libpq's ``PGHOST``/``PGPASSWORD``) see
# ``DBT_ENV_EXACT_KEYS`` below. For anything else, users can extend the list via
# the ``FLUID_DBT_FORWARD_ENV`` env var (see ``_user_configured_forward_env``).
DBT_ENV_PREFIXES = (
    "SNOWFLAKE_",
    "DBT_",
    "GCP_",
    "GOOGLE_",
    "AWS_",
    "REDSHIFT_",
    "DATABRICKS_",
    "CLICKHOUSE_",
    "TRINO_",
    "STARBURST_",
    "SPARK_",
    "ATHENA_",
)

# Adapters whose ecosystems use bare, unprefixed env names (prefixing would be
# too broad to auto-forward safely). These are matched exactly.
DBT_ENV_EXACT_KEYS = frozenset(
    {
        # libpq / dbt-postgres
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
        "PGDATABASE",
        "PGSSLMODE",
        "PGSSLCERT",
        "PGSSLKEY",
        "PGSSLROOTCERT",
        # dbt in the container would otherwise load the mounted project's
        # .env, which the CLI read through credentials.protected_env
        "PYTHON_DOTENV_DISABLED",
    }
)


def resolve_dbt_project_path(contract_path: Path, build: Dict[str, Any]) -> Optional[Path]:
    """Resolve the dbt project root for a build, confined to the contract's workspace.

    Returns ``None`` if no ``dbt_project.yml`` is found, or if the resolved
    path escapes the workspace boundary (path-traversal guard). The
    workspace boundary is pattern-aware via :func:`resolve_workspace_root`:
    inline builds (default) confine to ``contract.parent``; hybrid-reference
    builds widen to the nearest enclosing repo root so they can reach
    shared dbt projects in sibling directories.
    """
    repository = build.get("repository", "./")
    build_id = str(build.get("id", "unknown"))
    project_dir = (contract_path.parent / repository).resolve()
    if (project_dir / "dbt_project.yml").exists():
        from .._path_safety import resolve_workspace_root

        workspace_root = resolve_workspace_root(contract_path, build)
        return confine_to_workspace(
            project_dir,
            workspace_root,
            build_id=build_id,
            kind="dbt",
            logger=LOG,
        )
    return None


def _resolve_dbt_executable() -> Optional[str]:
    """Resolve a usable ``dbt`` binary.

    Search order:
      1. ``$DBT_EXECUTABLE`` if it points at an absolute / relative path.
      2. ``shutil.which($DBT_EXECUTABLE or "dbt")`` — relies on PATH.
      3. ``Path(sys.executable).parent / "dbt"`` — the venv-sibling
         fallback. When users run ``.venv/bin/python -m fluid_build.cli``
         without activating the venv, ``shutil.which`` doesn't see
         ``.venv/bin/dbt`` because the venv's bin dir isn't on PATH.
         The dbt binary IS sitting next to the python interpreter
         that's executing right now, so we look there before giving up.

    Hardening on the venv-sibling fallback:

    * ``is_file()`` rejects directory matches (a ``dbt`` directory next
      to ``python`` would otherwise be returned as a string and explode
      at ``subprocess.run`` time with EACCES instead of a clean error).
    * ``os.access(..., os.X_OK)`` rejects non-executable matches (e.g.
      a stale ``dbt.dist-info/`` artifact). An attacker who can write
      to the venv's ``bin/`` is past the trust boundary, but defending
      against accidental misuse keeps the failure mode loud and early.
    """
    configured = os.getenv("DBT_EXECUTABLE", "dbt")
    if os.path.sep in configured or configured.startswith((".", "~")):
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return str(candidate)
        return None
    found = shutil.which(configured)
    if found:
        return found
    # Venv-sibling fallback: works when running via ``.venv/bin/python``.
    sibling = Path(sys.executable).parent / configured
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    return None


def _configured_dbt_command_prefix() -> Optional[List[str]]:
    """Parse ``$DBT_EXECUTABLE`` into a command prefix, validating the
    program token before it can be prepended to a subprocess argv.

    ``DBT_EXECUTABLE`` may be a multi-token wrapper (e.g.
    ``"poetry run dbt"`` or ``"uv run dbt"``), so it is split with
    ``shlex``. The first token is the program that will actually be
    ``exec``'d — a hostile or garbage value there would otherwise be
    prepended to ``dbt build ...`` unchecked. Resolve it the same way
    :func:`_resolve_dbt_executable` resolves a bare executable:

    - an absolute / relative path must point at an existing file;
    - a bare name must resolve on ``PATH`` via ``shutil.which``.

    A value that resolves to neither is rejected: we warn-log and return
    ``None`` so ``build_dbt_command`` falls through to its normal
    executable discovery instead of trusting an unverifiable override.
    """
    configured = os.getenv("DBT_EXECUTABLE")
    if not configured:
        return None
    try:
        parts = shlex.split(configured)
    except ValueError as exc:
        # Unbalanced quotes etc. — don't let a malformed value through.
        LOG.warning("DBT_EXECUTABLE is not a parseable command (%s); ignoring.", exc)
        return None
    if not parts:
        return None

    program = parts[0]
    if os.path.sep in program or program.startswith((".", "~")):
        # Explicit path form — must be an existing file.
        candidate = Path(program).expanduser()
        resolved = candidate.is_file()
    else:
        # Bare name — must resolve on PATH.
        resolved = shutil.which(program) is not None
    if not resolved:
        LOG.warning(
            "DBT_EXECUTABLE program token %r does not resolve to an "
            "executable (not on PATH and not an existing file); ignoring "
            "the override and falling back to normal dbt discovery.",
            program,
        )
        return None
    return parts


def _is_flaglike_selector(value: str) -> bool:
    """True when a selector value would be parsed by dbt as its own flag.

    A ``--select`` value is spread as a standalone argv element, so a value
    like ``--full-refresh`` or ``--target`` (or the short ``-x`` form) would
    be consumed by dbt's own arg parser as a flag rather than treated as a
    node selector — an argument-injection vector. We treat any value whose
    first non-space character is ``-`` as flag-like and drop it.
    """
    return value.strip().startswith("-")


def _normalize_selectors(raw: Any) -> List[str]:
    """Normalize ``properties.select`` / ``properties.models`` into a clean
    list of dbt node selectors.

    Flag-like values (those starting with ``-``) are dropped with a warning so
    a contract cannot inject dbt flags (e.g. ``--full-refresh``, ``--target``)
    through a selector field. This mirrors the existing guards in
    :func:`build_dbt_command`: ``--target`` is allowlisted against the
    profile's real targets and ``--vars`` is neutralized via ``json.dumps``;
    ``--select`` was the asymmetry. Fail-closed: the malicious token never
    reaches the subprocess argv.
    """
    if raw is None:
        items: List[str] = []
    elif isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = [str(item) for item in raw if str(item).strip()]
    else:
        items = [str(raw)]

    cleaned: List[str] = []
    for item in items:
        if _is_flaglike_selector(item):
            LOG.warning(
                "dbt.selector.rejected reason=flag-like-value value=%r "
                "(a --select value starting with '-' would be parsed by dbt "
                "as a flag; dropping it)",
                item,
            )
            continue
        cleaned.append(item)
    return cleaned


def build_selectors(build: Dict[str, Any]) -> List[str]:
    """The ``--select`` values a dbt build runs with: ``properties.select`` (or the
    legacy ``models``), else ``+<model>+`` for a build with one output at most.

    The Databricks job for the build (``iac/providers/databricks_jobs.py``) selects
    with this too, so a local run and the workspace job build the same nodes.
    """
    props = build.get("properties") or {}
    selectors = _normalize_selectors(props.get("select") or props.get("models"))
    if not selectors:
        model = props.get("model")
        outputs = build.get("outputs") or []
        if model and len(outputs) <= 1:
            selectors = [f"+{model}+"]
    return selectors


#: PyPI distributions whose dbt plugin goes by another name. dbt lists a plugin
#: under its import package, ``dbt/adapters/<name>/`` (dbt-core
#: ``version.py::_get_adapter_plugin_names``), and that is the profile's
#: ``type:``. ``dbt-athena-community`` wraps ``dbt-athena``, whose package is
#: ``athena``.
_ADAPTER_PLUGIN_NAMES = {"athena-community": "athena"}


#: The adapter each ``execution.runtime.platform`` gets a profile for
#: (``profiles.py``), for an engine that names no adapter (``dbt``, ``dbt-core``).
_PLATFORM_ADAPTERS = {
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "gcp": "bigquery",
    "duckdb": "duckdb",
    "redshift": "redshift",
    "aws": "redshift",
    "postgres": "postgres",
    "postgresql": "postgres",
    "spark": "spark",
    "athena": "athena",
    "aws-athena": "athena",
    "glue": "glue",
    "aws-glue": "glue",
    "databricks": "databricks",
}


def _infer_dbt_adapter(build: Dict[str, Any]) -> Optional[str]:
    """The dbt plugin name a build needs (``athena`` for ``dbt-athena-community``)."""
    engine = str(build.get("engine") or "").strip().lower()
    # ``dbt-core`` names dbt itself, not an adapter: its platform says which.
    if engine.startswith("dbt-") and len(engine) > 4 and engine != "dbt-core":
        name = engine[4:]
        # An import package spells a distribution's ``-`` as ``_``.
        return _ADAPTER_PLUGIN_NAMES.get(name, name.replace("-", "_"))

    execution = build.get("execution")
    runtime = execution.get("runtime") if isinstance(execution, dict) else None
    platform = runtime.get("platform") if isinstance(runtime, dict) else None
    return _PLATFORM_ADAPTERS.get(str(platform or "").strip().lower())


# ── dbt engine detection (Python dbt-core v1 vs Fusion / dbt Core v2) ─────
#
# dbt Fusion (the Rust engine, open-sourced into dbt-labs/dbt-core as dbt
# Core v2.0) prints a single banner line from ``dbt --version``::
#
#     $ dbt --version
#     dbt Fusion 2.0.0-preview.126
#
# (docs.getdbt.com/docs/dbt-versions, "Checking your version"). Python
# dbt-core v1 prints the familiar multi-line shape with an adapter list::
#
#     Core:
#       - installed: 1.8.0
#       - latest:    1.8.0 - Up to date!
#     Plugins:
#       - snowflake: 1.9.0 - Up to date!
#
# The old adapter probe substring-matched the adapter name in that output —
# correct for v1 (adapters are pip-installed plugins and listed), but Fusion
# compiles its adapters in and lists nothing, so every Fusion user was
# silently punted to the Docker pip-install-dbt-core fallback. The helpers
# below classify the engine first and only substring-match on v1.

# Adapters known to be built into the Fusion engine (no pip install — the
# drivers ship with the binary via ``dbt system install-drivers``). Sources:
# docs.getdbt.com/docs/fusion/supported-features (Snowflake GA; BigQuery /
# Redshift preview; Databricks private preview; Spark + DuckDB Fusion-CLI
# beta) and the driver set shipped by ``dbt system install-drivers``
# (adds postgres + salesforce). The matrix grows per Fusion release, so
# membership here only tunes logging — under Fusion we ALWAYS attempt the
# native run rather than falling back to Docker (see
# ``_dbt_command_supports_adapter``).
FUSION_BUILTIN_ADAPTERS = frozenset(
    {
        "snowflake",
        "bigquery",
        "redshift",
        "databricks",
        "postgres",
        "spark",
        "duckdb",
        "salesforce",
    }
)

# ``dbt Fusion 2.0.0-preview.126`` / ``dbt-fusion 2.0.0-beta.1`` — version
# group optional so a bare "dbt fusion" banner still classifies.
_FUSION_BANNER_RE = re.compile(r"\bdbt[\s-]+fusion\b[^0-9]*([0-9][\w.+-]*)?", re.IGNORECASE)
# v1 multi-line: ``installed: 1.8.0``; pre-1.0: ``installed version: 0.21.1``.
_CORE_INSTALLED_RE = re.compile(r"\binstalled(?:\s+version)?:\s*v?([0-9][\w.+-]*)", re.IGNORECASE)
# Single bare banner line ``dbt 2.0.0`` (some Fusion builds drop "Fusion").
# ``dbt 2.0.1`` (the proprietary v2 build) and ``dbt-oss 2.0.4`` (the
# Apache-2.0 build of the same v2 engine) both answer with a bare banner.
# The ``-oss`` suffix is why this has to be optional: without it every
# dbt-oss install classified as ("unknown", ""), and every capability gate
# hanging off the detected engine silently took its most conservative branch.
_BARE_VERSION_RE = re.compile(
    r"^\s*dbt(?:-oss)?\s+v?([0-9][\w.+-]*)\s*$", re.IGNORECASE | re.MULTILINE
)


@functools.lru_cache(maxsize=None)
def _dbt_version_output(dbt_executable: str, timeout: float = 10.0) -> Optional[str]:
    """Run ``<dbt> --version`` once and cache the combined output.

    This is the ONLY subprocess seam for engine detection + the adapter
    probe — tests monkeypatch ``subprocess.run`` (or this function) and no
    real dbt binary is ever needed. Returns ``None`` when the binary can't
    be executed (missing, non-executable, timeout); ``timeout`` is part of
    the cache key so a short-budget probe (welcome scan) can never poison
    the runner's full-budget call.
    """
    try:
        result = subprocess.run(
            [dbt_executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{result.stdout}\n{result.stderr}"


def _parse_dbt_engine(output: str) -> Tuple[str, str]:
    """Classify a ``dbt --version`` output into ``(flavor, version)``.

    ``flavor`` is ``"fusion"`` (Rust engine / dbt Core v2, compiled-in
    adapters), ``"core"`` (Python dbt-core v1, pip-installed adapter
    plugins), or ``"unknown"``. ``version`` is the detected semver string
    (possibly ``""`` when the banner carries none). Pure function — the
    fixture surface for both output shapes lives in
    ``tests/build_runners/test_dbt_engine_detection.py``.
    """
    fusion = _FUSION_BANNER_RE.search(output)
    if fusion:
        return ("fusion", fusion.group(1) or "")

    installed = _CORE_INSTALLED_RE.search(output)
    if installed:
        return ("core", installed.group(1))

    bare = _BARE_VERSION_RE.search(output)
    if bare:
        version = bare.group(1)
        try:
            major = int(version.split(".", 1)[0])
        except ValueError:
            major = 0
        # Only the Rust engine versions as 2.x; the Python engine stays on
        # the 1.x LTS line, and its banner always carries Core:/Plugins:.
        return ("fusion", version) if major >= 2 else ("core", version)

    lowered = output.lower()
    if "core:" in lowered or "plugins:" in lowered:
        return ("core", "")
    return ("unknown", "")


@functools.lru_cache(maxsize=None)
def _detect_dbt_engine(dbt_executable: str, timeout: float = 10.0) -> Tuple[str, str]:
    """Detect the engine flavor + version of a dbt executable.

    Returns ``("fusion"|"core"|"unknown", version)``. Cached per
    ``(executable, timeout)`` — Fusion answers in milliseconds while Python
    dbt-core takes seconds, so short-budget callers (welcome scan) pass a
    small ``timeout`` and simply get ``("unknown", "")`` on overrun.
    """
    output = _dbt_version_output(dbt_executable, timeout)
    if output is None:
        return ("unknown", "")
    return _parse_dbt_engine(output)


@functools.lru_cache(maxsize=None)
def _dbt_command_supports_adapter(dbt_executable: str, adapter: str) -> bool:
    """True when ``dbt_executable`` can run builds for ``adapter``.

    Engine-aware:

    * **Fusion** — adapters are compiled in and NOT listed by
      ``--version``, so substring matching is meaningless. Consult
      :data:`FUSION_BUILTIN_ADAPTERS` and return True either way (the
      matrix changes per Fusion release; attempting the native run and
      letting dbt fail loud beats silently inverting the user's engine
      choice with the Docker pip-install-dbt-core fallback). Unknown
      adapters get a WARNING so the operator understands a subsequent
      dbt failure.
    * **Core / unknown** — v1 lists its plugins under ``Plugins:`` by
      plugin name (``athena`` for ``dbt-athena-community``), matched exactly;
      output without that section keeps the substring probe. An unrunnable
      executable still returns False (Docker fallback preserved).
    """
    output = _dbt_version_output(dbt_executable)
    if output is None:
        return False

    flavor, version = _parse_dbt_engine(output)
    if flavor == "fusion":
        if adapter.lower() not in FUSION_BUILTIN_ADAPTERS:
            LOG.warning(
                "dbt.adapter.unverified engine=fusion version=%s adapter=%r — "
                "not in the known Fusion built-in adapter set %s; attempting "
                "the native run anyway (set DBT_EXECUTABLE to a dbt-core v1 "
                "install if this adapter needs the Python plugin ecosystem).",
                version or "?",
                adapter,
                sorted(FUSION_BUILTIN_ADAPTERS),
            )
        else:
            LOG.debug(
                "dbt.adapter.builtin engine=fusion version=%s adapter=%s",
                version or "?",
                adapter,
            )
        return True

    plugins = _listed_plugins(output)
    if plugins is not None:
        return adapter.lower() in plugins
    lowered = output.lower()
    return adapter.lower() in lowered or f"dbt-{adapter.lower()}" in lowered


#: One plugin line of ``dbt --version``: ``  - athena:   1.10.1   - Update available!``.
_PLUGIN_LINE_RE = re.compile(r"^\s*-\s*([A-Za-z0-9_]+)\s*:", re.MULTILINE)


def _listed_plugins(output: str) -> Optional[FrozenSet[str]]:
    """The plugin names dbt-core v1 lists under ``Plugins:``, or ``None`` when the
    output has no such section (the substring probe then decides)."""
    start = output.lower().find("plugins:")
    if start < 0:
        return None
    return frozenset(name.lower() for name in _PLUGIN_LINE_RE.findall(output[start:]))


def _user_configured_forward_env() -> List[str]:
    """Return extra env keys/prefixes declared via ``FLUID_DBT_FORWARD_ENV``.

    The env var is a comma-separated list. Entries ending in ``_`` are treated
    as prefixes (``FOO_*``); everything else is an exact key. This is the escape
    hatch for adapters whose conventions aren't built-in.
    """
    raw = os.getenv("FLUID_DBT_FORWARD_ENV", "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _env_is_user_forwarded(key: str, patterns: List[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("_") and key.startswith(pattern):
            return True
        if key == pattern:
            return True
    return False


def _collect_dbt_container_env() -> Dict[str, str]:
    """The variables the dbt container inherits (``docker run -e NAME``).

    The adapter prefixes and exact names above, ``FLUID_DBT_FORWARD_ENV``,
    and what the project's dotenv files set. dbt in the container used to
    read the mounted project's ``.env`` itself (``env_var('TARGET_SCHEMA')``);
    ``PYTHON_DOTENV_DISABLED`` stops that, so the CLI passes on the values its
    own filtered read exported instead (``dotenv_store.project_exports``),
    never a name ``credentials.protected_env`` refused.
    """
    from fluid_build.credentials.dotenv_store import project_exports

    env_vars: Dict[str, str] = {}
    extra_patterns = _user_configured_forward_env()
    for key, value in os.environ.items():
        if not value:
            continue
        if (
            key.startswith(DBT_ENV_PREFIXES)
            or key in DBT_ENV_EXACT_KEYS
            or _env_is_user_forwarded(key, extra_patterns)
        ):
            env_vars[key] = value
    for key, value in project_exports().items():
        env_vars.setdefault(key, value)
    return env_vars


def _rewrite_dbt_args_for_container(
    args: List[str],
    project_dir: Path,
    profiles_dir: Optional[Path],
) -> List[str]:
    rewritten: List[str] = []
    project_str = str(project_dir)
    profiles_str = str(profiles_dir) if profiles_dir else None
    for arg in args:
        if arg == project_str:
            rewritten.append("/workspace/project")
        elif profiles_str and arg == profiles_str:
            rewritten.append("/workspace/profiles")
        else:
            rewritten.append(arg)
    return rewritten


#: The image the fallback installs dbt into: ``python:3.12-slim`` pinned by digest,
#: the entry of ``forge/core/pipeline_systems/_base.py::PINNED_IMAGES`` (which
#: ``scripts/check_pinned_actions.py`` keeps current; a test keeps the two equal).
#: A copy, not an import: that module reaches ``cli``, and build_runners must not.
BOOTSTRAP_IMAGE = (
    "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
)

#: An image reference, ``[registry[:port]/]name[:tag]`` (a tag may hold capitals).
_IMAGE_REF = r"[a-z0-9][a-z0-9._/:-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
#: The same, pinned by digest: ``...@sha256:<digest>``.
_DIGEST_IMAGE_RE = re.compile(_IMAGE_REF + r"@sha256:[0-9a-f]{64}")

#: The top-level packages the fallback installs, pinned exactly: dbt-core and one
#: adapter per plugin name (their dependencies resolve within these packages' own
#: ranges; nothing is hash-locked). Resolved on pypi.org on 2026-11-19: each adapter's dbt-core, dbt-adapters
#: and dbt-common ranges hold dbt-core 1.11.17, and every set resolves from wheels
#: alone for CPython 3.12 on Linux x86_64 and aarch64 (dbt-core 1.12 needs
#: dbt-core-experimental-parser, published as an sdist only). dbt-athena-community
#: 1.11.1 ran on AWS. The names match ``_engine_specs._DBT_PLATFORM_ADAPTERS``.
DBT_CORE_PIN = "dbt-core==1.11.17"
CONTAINER_ADAPTER_PINS: Dict[str, str] = {
    "athena": "dbt-athena-community==1.11.1",
    "bigquery": "dbt-bigquery==1.12.4",
    "clickhouse": "dbt-clickhouse==1.10.3",
    "databricks": "dbt-databricks==1.13.0",
    "duckdb": "dbt-duckdb==1.11.1",
    "postgres": "dbt-postgres==1.11.0",
    "redshift": "dbt-redshift==1.11.2",
    "snowflake": "dbt-snowflake==1.12.5",
    "spark": "dbt-spark==1.11.1",
    "trino": "dbt-trino==1.11.0",
}

#: An exact requirement, ``name[extras]==version``: nothing for a shell to read.
_EXACT_PIN_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9._,-]+\])?"
    r"==[0-9][A-Za-z0-9.+!_-]*"
)

#: dbt adapters that authenticate to AWS (boto3). A container sees no host AWS
#: profile (``~/.aws`` is never mounted), only the keys in its environment.
_AWS_ADAPTERS = frozenset({"athena", "glue", "redshift"})

#: Host AWS settings that name a profile or a file the container does not have.
#: Forwarded, they make boto3 in the container look for that profile or file and fail.
_HOST_ONLY_AWS_ENV = frozenset(
    {
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CA_BUNDLE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    }
)

#: The AWS settings that give boto3 in the container credentials it can use: keys,
#: or the container credentials endpoint (ECS), which it reaches over the network.
_CONTAINER_AWS_CREDENTIALS = (
    ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
    ("AWS_CONTAINER_CREDENTIALS_FULL_URI",),
    ("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",),
)

#: ``{{ env_var('NAME') }}`` or ``{{ env_var('NAME', 'default') }}``, the one template
#: an AWS profile name is commonly written as.
_ENV_VAR_TEMPLATE_RE = re.compile(
    r"\{\{\s*env_var\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*"
    r"(?:,\s*['\"]([^'\"]*)['\"]\s*)?\)\s*\}\}"
)


def _digest_pinned_image(image: str, env_name: str) -> str:
    """``image`` when it is an image reference pinned by digest; ``RuntimeError`` otherwise."""
    if _DIGEST_IMAGE_RE.fullmatch(image):
        return image
    if re.fullmatch(_IMAGE_REF, image):
        raise RuntimeError(
            f"{env_name}={image!r} names a tag, and a tag can move: pin it by digest "
            f"({image}@sha256:<digest>, from `docker buildx imagetools inspect {image}`)"
        )
    raise RuntimeError(
        f"{env_name}={image!r} is not an image reference pinned by digest "
        "([registry/]name[:tag]@sha256:<digest>)"
    )


def _bootstrap_image() -> str:
    """The digest-pinned image the fallback installs dbt into."""
    override = os.getenv("DBT_BOOTSTRAP_IMAGE")
    if override:
        return _digest_pinned_image(override, "DBT_BOOTSTRAP_IMAGE")
    return BOOTSTRAP_IMAGE


def _container_packages(adapter: str) -> List[str]:
    """The exact requirements the fallback installs for ``adapter``.

    ``DBT_ADAPTER_PACKAGE`` replaces the adapter's pin: one or more exact
    requirements (``dbt-glue==1.12.4``), with :data:`DBT_CORE_PIN` added unless
    it names dbt-core itself. An adapter without a pin is refused, as is an
    override that is not an exact pin, so no top-level package floats.
    """
    override = os.getenv("DBT_ADAPTER_PACKAGE")
    if override:
        packages = override.replace(",", " ").split()
        loose = [pkg for pkg in packages if not _EXACT_PIN_RE.fullmatch(pkg)]
        if loose or not packages:
            raise RuntimeError(
                f"DBT_ADAPTER_PACKAGE must list exact pins (name==version), not "
                f"{', '.join(map(repr, loose)) or repr(override)}: the dbt container "
                "installs only pinned packages"
            )
    else:
        pin = CONTAINER_ADAPTER_PINS.get(adapter)
        if pin is None:
            raise RuntimeError(
                f"forge has no pinned package for the dbt adapter {adapter!r}, so it will "
                "not install one in a container. Install the adapter next to your dbt, or "
                "set DBT_ADAPTER_PACKAGE to an exact pin (for example dbt-<name>==1.2.3)"
            )
        packages = [pin]
    # PEP 503 names: dbt_core and dbt.core are dbt-core too.
    names = {
        re.sub(r"[-_.]+", "-", re.split(r"[\[=]", pkg, maxsplit=1)[0]).lower() for pkg in packages
    }
    if "dbt-core" not in names:
        packages = [DBT_CORE_PIN, *packages]
    return packages


def _profile_output(
    profiles_dir: Path, profile_name: Optional[str], requested_target: Optional[str]
) -> Dict[str, Any]:
    """The ``outputs`` entry dbt will read from ``profiles_dir/profiles.yml``, or
    ``{}`` when it cannot be told (the run then decides)."""
    try:
        document = yaml.safe_load((profiles_dir / "profiles.yml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return {}
    if not isinstance(document, dict):
        return {}
    profiles = [k for k in document if k != "config"]
    name = profile_name if isinstance(profile_name, str) else None
    name = name or (profiles[0] if len(profiles) == 1 and isinstance(profiles[0], str) else None)
    profile = document.get(name) if name else None
    outputs = profile.get("outputs") if isinstance(profile, dict) else None
    if not isinstance(outputs, dict):
        return {}
    requested = requested_target if isinstance(requested_target, str) else None
    target = requested if requested in outputs else profile.get("target")
    output = outputs.get(target) if isinstance(target, str) else None
    return output if isinstance(output, dict) else {}


def _profile_name_value(value: Any, env: Dict[str, str]) -> Optional[str]:
    """The AWS profile a dbt profile's ``aws_profile_name`` / ``iam_profile`` names.

    A literal is itself, and ``{{ env_var('NAME'[, 'default']) }}`` is read as dbt
    in the container renders it, from ``env`` (what the container gets). Any other
    template is dbt's to render: ``None``, the run decides.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "{{" not in text and "{%" not in text:
        return text
    match = _ENV_VAR_TEMPLATE_RE.fullmatch(text)
    if match is None:
        return None
    return (env.get(match.group(1)) or match.group(2) or "").strip() or None


def _refuse_unreadable_aws_credentials(
    adapter: str, output: Dict[str, Any], container_env: Dict[str, str]
) -> None:
    """Refuse a container run of an AWS adapter that would find no credentials.

    boto3 in the container has only what reaches it: the keys or the container
    credentials endpoint in ``container_env`` (what ``docker run -e`` passes),
    and keys written in the dbt profile itself. A dbt profile that names an AWS
    profile (``aws_profile_name``, ``iam_profile``) names one the container does
    not have, and the host's ``credential_process``, SSO, instance role or
    ``~/.aws`` never reach it. Refused before ``docker`` starts, with the two ways
    out; the message names the profile's field, never its value.
    """
    if adapter not in _AWS_ADAPTERS:
        return
    field = next(
        (
            key
            for key in ("aws_profile_name", "iam_profile")
            if _profile_name_value(output.get(key), container_env)
        ),
        None,
    )
    method = str(output.get("method", "")).lower()
    needs_keys = adapter != "redshift" or method.startswith("iam")
    has_keys = any(
        all(container_env.get(name) for name in names) for names in _CONTAINER_AWS_CREDENTIALS
    )
    has_keys = has_keys or any(
        output.get(key_id) and output.get(secret)
        for key_id, secret in (
            ("aws_access_key_id", "aws_secret_access_key"),
            ("access_key_id", "secret_access_key"),
        )
    )
    if field is None and (has_keys or not needs_keys):
        return
    package = CONTAINER_ADAPTER_PINS.get(adapter, f"dbt-{adapter}")
    if field is not None:
        reason = f"the dbt profile names an AWS profile ({field}), which a container cannot read"
        keys = (
            "export that profile's short-lived keys (eval \"$(aws configure export-credentials "
            '--profile <that profile> --format env)") and run without naming a profile '
            f"(unset AWS_PROFILE, or drop {field} from the dbt profile)"
        )
    else:
        reason = (
            "neither the environment nor the dbt profile holds AWS keys, and a container "
            "cannot read the host's AWS profiles"
        )
        keys = 'export short-lived keys (eval "$(aws configure export-credentials --format env)")'
    raise RuntimeError(
        f"the local dbt has no {adapter!r} plugin, so this build would run in a container, "
        f"and {reason}. Install the adapter next to dbt (pip install '{package}'), or {keys}"
    )


def _container_env() -> Dict[str, str]:
    """What the container gets: the dbt environment, without the host-only AWS
    settings, pip's (a project .env must not choose the index) or the
    interpreter's. A ``PYTHON*`` name passes only when it is one forge sets
    (``DBT_ENV_EXACT_KEYS``) or the operator forwards it (``FLUID_DBT_FORWARD_ENV``):
    a project .env must not steer the Python that runs pip and dbt."""
    forwarded = _user_configured_forward_env()
    return {
        key: value
        for key, value in _collect_dbt_container_env().items()
        if key not in _HOST_ONLY_AWS_ENV
        and not key.startswith("PIP_")
        and (
            not key.startswith("PYTHON")
            or key in DBT_ENV_EXACT_KEYS
            or _env_is_user_forwarded(key, forwarded)
        )
    }


def _build_containerized_dbt_command(
    adapter: str,
    args: List[str],
    project_dir: Path,
    profiles_dir: Optional[Path],
) -> List[str]:
    """``docker run`` for a dbt the local install cannot run: a digest-pinned image,
    exactly pinned packages from wheels only, the project and profiles mounted,
    and the adapter environment passed by name."""
    docker_image = os.getenv("DBT_DOCKER_IMAGE")
    if docker_image:
        image, packages = _digest_pinned_image(docker_image, "DBT_DOCKER_IMAGE"), None
    else:
        image, packages = _bootstrap_image(), _container_packages(adapter)
    # ``-e KEY`` (no ``=value``) tells docker to inherit the value from the current
    # process env. That keeps secrets out of argv (visible via ``ps``) and out of
    # the command-log line printed by ``cprint``.
    env_keys = sorted(_container_env())
    container_args = _rewrite_dbt_args_for_container(args, project_dir, profiles_dir)

    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{project_dir}:/workspace/project",
        "-w",
        "/workspace/project",
    ]
    if profiles_dir:
        cmd.extend(["-v", f"{profiles_dir}:/workspace/profiles"])
    for key in env_keys:
        cmd.extend(["-e", key])

    if packages is None:
        cmd.extend([image, "dbt", *container_args])
        return cmd

    # Wheels only: an sdist would run its build code at install time. The working
    # directory is the mounted project, so ``-I`` keeps it off sys.path (a project
    # ``pip/`` or ``ssl.py`` would run first) and ignores PYTHON* settings.
    # ``--isolated`` ignores the PIP_* variables and the user's config, but not
    # PIP_CONFIG_FILE: no PIP_* name is passed in, and PIP_CONFIG_FILE=/dev/null
    # loads no config file at all, so the packages come from PyPI.
    install_and_run = (
        "PIP_CONFIG_FILE=/dev/null python -I -m pip install --isolated "
        "--index-url https://pypi.org/simple "
        "--quiet --no-cache-dir --disable-pip-version-check "
        "--only-binary=:all: {pkgs} && {dbt_cmd}"
    ).format(
        pkgs=" ".join(shlex.quote(pkg) for pkg in packages),
        dbt_cmd=" ".join(shlex.quote(part) for part in ["dbt", *container_args]),
    )
    cmd.extend([image, "sh", "-lc", install_and_run])
    return cmd


def build_dbt_command(
    build: Dict[str, Any],
    project_dir: Path,
    *,
    profiles_dir: Optional[Path] = None,
    project_config: Optional[Dict[str, Any]] = None,
    apply_mode: Optional[str] = None,
) -> List[str]:
    """Build the dbt CLI command for a dbt-based build.

    The optional ``apply_mode`` carries the ``fluid apply --mode``
    value. When destructive (``replace`` / ``replace-and-build``) the
    command appends ``--full-refresh`` so dbt's incremental models
    rebuild from scratch instead of doing the standard merge/append.
    Non-incremental materialisations (``table`` / ``view``) ignore the
    flag — they always rebuild fresh — so the flag is safe to add
    unconditionally for destructive modes.
    """
    props = build.get("properties") or {}
    project_config = project_config or _load_dbt_project_config(project_dir)
    dbt_args = ["build", "--project-dir", str(project_dir)]

    if profiles_dir:
        dbt_args += ["--profiles-dir", str(profiles_dir)]

    profile_name = _resolve_dbt_profile_name(props, project_config)
    if profile_name:
        dbt_args += ["--profile", str(profile_name)]

    # Target selection: contract override > DBT_TARGET env > profile default.
    # We DON'T blindly forward DBT_TARGET because operator labs commonly set
    # ``DBT_TARGET=snowflake`` in their .env, but ``fluid generate`` emits
    # dbt projects with the conventional ``target: dev`` profile. Forwarding
    # ``--target snowflake`` against a dev-only profile makes dbt fail with
    # ``does not have a target named 'snowflake'``. Resolve the profile
    # targets up-front and only pass ``--target`` when the requested name
    # actually exists; otherwise let dbt use the profile's default and emit
    # a debug log so the operator knows which one was picked.
    requested_target = props.get("target") or os.getenv("DBT_TARGET")
    if requested_target:
        available = _list_profile_targets(profiles_dir, profile_name)
        if available is None or str(requested_target) in available:
            dbt_args += ["--target", str(requested_target)]
        else:
            LOG.warning(
                "dbt.target.fallback requested=%r unavailable; using profile "
                "default. Available targets: %s. Set ``properties.target`` on "
                "the build OR rename the profile target to '%s' to suppress.",
                requested_target,
                sorted(available) if available else "<unknown>",
                requested_target,
            )
            # dbt also reads DBT_TARGET from the environment automatically.
            # Without this strip, the dbt subprocess would still see the
            # operator's ``DBT_TARGET=snowflake`` and re-introduce the same
            # "no such target" failure we just dodged. Mutating os.environ
            # for the rest of this CLI process is intentional — every dbt
            # subprocess we spawn from here on should respect the fallback.
            os.environ.pop("DBT_TARGET", None)

    selectors = build_selectors(build)
    if selectors:
        dbt_args += ["--select", *selectors]

    dbt_vars = props.get("vars")
    if dbt_vars:
        dbt_args += ["--vars", json.dumps(_resolve_env_placeholders(dbt_vars))]

    # Destructive apply modes (``replace`` / ``replace-and-build``)
    # force dbt to fully rebuild incremental models. ``--full-refresh``
    # is a no-op for ``table`` / ``view`` materializations (they
    # always rebuild) so this is safe to add unconditionally for
    # destructive modes.
    if apply_mode and apply_mode.lower() in ("replace", "replace-and-build"):
        dbt_args.append("--full-refresh")

    configured_prefix = _configured_dbt_command_prefix()
    if configured_prefix:
        return configured_prefix + dbt_args

    dbt_executable = _resolve_dbt_executable()
    adapter = _infer_dbt_adapter(build)
    if dbt_executable and (not adapter or _dbt_command_supports_adapter(dbt_executable, adapter)):
        return [dbt_executable, *dbt_args]

    if adapter and shutil.which("docker"):
        _refuse_unreadable_aws_credentials(
            adapter,
            _profile_output(profiles_dir or project_dir, profile_name, requested_target),
            _container_env(),
        )
        # ``--no-partial-parse`` is safe for containers (no persistent parse cache
        # across runs) and avoids stale-cache surprises. Kept off the local path
        # where users may legitimately rely on partial-parse for fast iteration.
        container_args = [dbt_args[0], "--no-partial-parse", *dbt_args[1:]]
        return _build_containerized_dbt_command(adapter, container_args, project_dir, profiles_dir)

    if dbt_executable:
        package = CONTAINER_ADAPTER_PINS.get(adapter or "", f"dbt-{adapter}")
        raise RuntimeError(
            f"dbt executable '{dbt_executable}' has no '{adapter}' plugin. Install the "
            f"adapter next to it (pip install '{package}'), set DBT_EXECUTABLE to a dbt "
            "that has it, or make Docker available."
        )

    raise RuntimeError(
        "dbt executable not found. Install dbt, add it to PATH, set DBT_EXECUTABLE, or make Docker available for containerized execution."
    )


def _render_command_for_log(command: List[str]) -> str:
    """Render a dbt CLI command for display, redacting secret-bearing args.

    ``cprint`` does not route through the logging redactor, so redact here:

    - The value after ``--vars`` (JSON may carry resolved env secrets).
    - The value side of ``-e KEY=VALUE`` / ``--env KEY=VALUE`` when ``KEY``
      looks sensitive (password, token, …). The default containerized path
      uses ``-e KEY`` (no value) so nothing hits argv, but user-configured
      wrappers may still inline a value.
    """
    parts: List[str] = []
    redact_vars = False
    inspect_env = False
    for part in command:
        if redact_vars:
            parts.append("<redacted>")
            redact_vars = False
            continue

        if inspect_env:
            inspect_env = False
            if "=" in part:
                key, _ = part.split("=", 1)
                if SENSITIVE_ENV_KEY_RE.search(key):
                    parts.append(f"{key}=<redacted>")
                    continue
            parts.append(part)
            continue

        parts.append(part)
        if part == "--vars":
            redact_vars = True
        elif part in ("-e", "--env"):
            inspect_env = True
    return " ".join(parts)


# ── Run-record persistence (dbt build → .fluid/runs/…) ────────────────────
#
# ``fluid apply --mode amend-and-build`` shells ``dbt build`` and — until
# this hook — discarded everything but the process exit code. Parsing
# ``target/run_results.json`` (via :mod:`.artifacts`) closes the
# contract→test→result loop: each dbt node becomes a run-record ``stream``
# so ``fluid runs status`` shows per-test granularity, and ``fluid verify``
# (see ``cli/_transformation_stage_ext``) gates on error-severity failures.


def build_dbt_run_record(
    results: Any,
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    returncode: int,
) -> Dict[str, Any]:
    """Shape a canonical FLUID run record from a parsed ``RunResults``.

    Mirrors ``build_runners._acquisition_common._canonical_run_record`` (the
    shape every runner's state-store write uses) so ``fluid runs
    status/logs/diff`` and the status renderer read it with zero further
    changes. Each dbt node is projected to a ``stream`` (``name`` =
    ``unique_id``, ``records`` = test ``failures`` count) for per-node /
    per-test granularity in ``fluid runs status`` and ``fluid runs diff``.
    """
    counts = results.counts()
    error_nodes = results.error_nodes

    # State mapping: a clean exit with no error-severity node is SUCCEEDED;
    # if some nodes passed but others failed it's PARTIAL; otherwise FAILED.
    # (RunState values are lowercase — matches ops/status.py comparisons.)
    if returncode == 0 and not error_nodes:
        state = "succeeded"
    elif any(n.is_ok for n in results.results):
        state = "partial"
    else:
        state = "failed"

    streams = [
        {
            "name": n.unique_id,
            "state": n.status,
            # ``records`` is the run-diff delta axis; for a test it's the
            # failing-row count, for a model there's no natural row count so 0.
            "records": (n.failures if n.failures is not None else 0),
            "failures": n.failures,
            "execution_time": n.execution_time,
        }
        for n in results.results
    ]

    error_msg: Optional[str] = None
    if error_nodes:
        preview = ", ".join(n.unique_id for n in error_nodes[:5])
        more = "" if len(error_nodes) <= 5 else f" (+{len(error_nodes) - 5} more)"
        error_msg = f"{len(error_nodes)} dbt node(s) at error severity: {preview}{more}"
    elif returncode != 0:
        error_msg = f"dbt exited with code {returncode}"

    facets: Dict[str, Any] = {
        "engine": "dbt",
        "duration_seconds": duration_seconds,
        "returncode": returncode,
        "dbt_schema_version": results.schema_version,
        "dbt_version": results.dbt_version,
        "invocation_id": results.invocation_id,
        "elapsed_time": results.elapsed_time,
    }
    facets.update(counts)

    return {
        "run_id": run_id,
        "state": state,
        "started_at": started_at,
        "finished_at": finished_at,
        # ``records_total`` in the acquisition world = rows landed; a dbt
        # transformation's closest analogue is "nodes executed", which is
        # what ``fluid runs status`` renders as the run's headline count.
        "records_total": counts["nodes_total"],
        "streams": streams,
        "error": error_msg,
        "facets": facets,
    }


def _resolve_product_id_from_dir(contract_dir: Path, build: Dict[str, Any]) -> Optional[str]:
    """Best-effort resolve the contract id whose builds include ``build``.

    ``execute_dbt_build`` is dispatched with only ``build`` + ``project_dir``
    + ``contract_dir`` (the contract's parent dir), mirroring how ``fluid
    verify`` resolves run records relative to the contract path. We recover
    ``contract.id`` here so the record is keyed IDENTICALLY to what verify
    reads: ``<contract_dir>/.fluid/runs/<contract.id>/<build.id>/runs/``.

    When a directory holds several contracts we disambiguate by matching the
    build id; otherwise we fall back to the first contract that declares an
    ``id``. Returns ``None`` when nothing resolves — the caller then skips
    the write rather than mis-keying the record.
    """
    build_id = build.get("id")
    candidates = sorted(contract_dir.glob("*.fluid.yaml")) + sorted(
        contract_dir.glob("*.fluid.yml")
    )
    fallback: Optional[str] = None
    for path in candidates:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        cid = doc.get("id")
        if not cid:
            continue
        if fallback is None:
            fallback = str(cid)
        builds = doc.get("builds") or []
        if build_id and any(isinstance(b, dict) and b.get("id") == build_id for b in builds):
            return str(cid)
    return fallback


def _persist_dbt_run_record(
    build: Dict[str, Any],
    project_dir: Path,
    contract_dir: Path,
    *,
    returncode: int,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    product_id: Optional[str] = None,
) -> Optional[str]:
    """Parse ``target/run_results.json`` and persist a FLUID run record.

    Best-effort: any failure (missing artifact, unresolved contract id,
    state-store error) is logged at DEBUG and swallowed — recording results
    must NEVER change the build's exit code.

    Works on BOTH the local-dbt and Docker-fallback paths: docker mounts the
    project dir (``-v <project_dir>:/workspace/project``), so ``target/`` —
    and thus ``run_results.json`` — survives on the host after the container
    exits. Returns the written run id, or ``None`` when nothing was
    persisted.
    """
    try:
        from .._acquisition_common import generate_run_id
        from .._state import FileStateStore
        from .artifacts import parse_run_results

        results = parse_run_results(project_dir)
        if results is None:
            LOG.debug(
                "dbt run_results.json absent/unparseable under %s; no run record", project_dir
            )
            return None

        pid = product_id or _resolve_product_id_from_dir(contract_dir, build)
        if not pid:
            LOG.debug(
                "could not resolve contract id for dbt build %r; skipping run record",
                build.get("id"),
            )
            return None

        run_id = generate_run_id()
        record = build_dbt_run_record(
            results,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            returncode=returncode,
        )
        # Route through the FileStateStore chokepoint (the PR #272 redaction
        # funnel) — NEVER json.dump directly.
        store = FileStateStore(contract_dir / ".fluid")
        store.write_run_record(pid, build.get("id", "unknown"), record)
        return run_id
    except Exception as exc:  # noqa: BLE001 — recording must not break the build
        LOG.debug("dbt run-record persistence failed (non-fatal): %s", exc)
        return None


def _check_bound_project(build_id: str, project_dir: Path) -> None:
    """Refuse to run dbt on a project whose files are not the ones this ``fluid apply`` bound.

    ``fluid apply`` compared the checkout with the copy the bundle packs before
    anything ran (``cli/apply.py::_bind_dbt_projects``) and set the bound files for
    the run. This compares the exact directory dbt is about to read, now: a file
    changed while the apply provisioned, a project found elsewhere than the gate
    found it, or a project the bundle did not pack at all, is refused with the
    gate's event. Nothing is bound outside a ``fluid apply`` of a bundle that
    packs dbt projects, and then nothing is checked.
    """
    from .project_files import binding_active, bound_project, check_project

    if not binding_active():
        return
    bound = bound_project(build_id)
    if bound is None:
        error = (
            f"build {build_id!r} runs the dbt project at {project_dir}, which the bundle this "
            "apply binds does not pack (a repository fluid bundle reads as a Git source is "
            "not packed). dbt did not run. Name the project's directory in repository, then "
            "re-run ``fluid bundle`` and ``fluid plan``."
        )
    else:
        problem = check_project(project_dir, bound.files)
        if problem is None:
            return
        error = (
            f"the dbt project of build {build_id!r} at {project_dir} differs from the copy "
            f"{bound.bundle} packs: {problem}. dbt did not run. Re-run ``fluid bundle`` and "
            "``fluid plan`` to bind the files as they are now, or restore them."
        )
    from fluid_build._contract_loader import CLIError

    raise CLIError(
        1, "apply_plan_digest_bundle_mismatch", {"kind": "bundle-mismatch", "error": error}
    )


def execute_dbt_build(
    build: Dict[str, Any],
    project_dir: Path,
    contract_dir: Path,
    dry_run: bool = False,
    delay: int = 2,
    no_output: bool = False,
    fail_fast: bool = False,
    force_run: bool = False,
    apply_mode: Optional[str] = None,
) -> int:
    """Execute a dbt-based build.

    The optional ``apply_mode`` carries the ``fluid apply --mode``
    value through to ``build_dbt_command`` so destructive modes append
    ``--full-refresh`` to the dbt invocation (forcing incremental
    models to rebuild from scratch).
    """
    build_id = build.get("id", "unknown")
    execution = build.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    trigger = execution.get("trigger")
    trigger = trigger if isinstance(trigger, dict) else {}
    trigger_type = trigger_type_of(build)
    # A scheduler decides when such a build runs; fluid apply runs it once.
    scheduled = trigger_type in SCHEDULER_TRIGGER_TYPES

    cprint(f"\n{'=' * 80}")
    cprint(f"📋 Build: {build_id}")
    cprint(f"   dbt project: {project_dir}")
    trigger_label = trigger_type
    if scheduled and force_run:
        trigger_label = f"{trigger_type} (manual apply override)"
    cprint(f"   Trigger: {trigger_label}")

    # trigger.type continuous (schema 0.7.6) says the job's platform keeps it
    # running, and a dbt run ends: refused before a profile is prepared.
    if is_continuous_build(build):
        return refuse_continuous_trigger("dbt builds as batch runs, which end")
    # Left to its scheduler: nothing to prepare, and nothing to refuse.
    if scheduled and not force_run:
        return leave_to_its_scheduler(build_id, trigger_type, trigger.get("cron"))

    _check_bound_project(str(build_id), project_dir)

    project_config = _load_dbt_project_config(project_dir)
    try:
        profiles_dir, temp_profiles_dir = resolve_dbt_profiles_dir(
            build, project_dir, project_config
        )
    except (DbtProfileError, EnvTemplateError) as exc:
        # Refused before the profile is written, and before dbt starts.
        _print_typed_error(exc)
        LOG.error("dbt_profile_refused build_id=%s code=%s", build_id, exc.code)
        cprint(f"{'=' * 80}")
        return 1

    try:
        command = build_dbt_command(
            build,
            project_dir,
            profiles_dir=profiles_dir,
            project_config=project_config,
            apply_mode=apply_mode,
        )
    except (RuntimeError, OSError, yaml.YAMLError, EnvTemplateError) as exc:
        if temp_profiles_dir:
            temp_profiles_dir.cleanup()
        console_error(f"Unable to prepare dbt build '{build_id}': {exc}")
        cprint(f"{'=' * 80}")
        return 1

    cprint(f"   Command: {_render_command_for_log(command)}")

    if trigger_type == "manual" or (scheduled and force_run):
        iterations = 1 if scheduled else trigger.get("iterations", 1)
        delay_from_contract = trigger.get("delaySeconds")
        if delay_from_contract is not None:
            delay = delay_from_contract

        cprint(f"   Iterations: {iterations}")
        if delay > 0:
            cprint(f"   Delay: {delay}s between runs")

        if dry_run:
            cprint(f"   🔍 [DRY RUN] Would execute {iterations} time(s)")
            cprint(f"{'=' * 80}")
            return 0

        cprint(f"{'=' * 80}\n")

        successful_runs = 0
        failed_runs = 0

        try:
            for i in range(iterations):
                cprint(f"🚀 Run {i + 1}/{iterations} - {datetime.now().strftime('%H:%M:%S')}")
                cprint("-" * 80)

                from .._acquisition_common import utc_now_iso

                started_at = utc_now_iso()
                start_time = time.time()

                try:
                    result = subprocess.run(
                        command,
                        cwd=contract_dir,
                        capture_output=no_output,
                        text=True,
                    )

                    duration = time.time() - start_time

                    # Parse target/run_results.json and persist a run record —
                    # BEFORE the fail-fast early return so a failing build is
                    # still recorded. Best-effort; never changes the exit code.
                    _persist_dbt_run_record(
                        build,
                        project_dir,
                        contract_dir,
                        returncode=result.returncode,
                        started_at=started_at,
                        finished_at=utc_now_iso(),
                        duration_seconds=duration,
                    )

                    if result.returncode == 0:
                        successful_runs += 1
                        success(f"Run {i + 1} completed successfully ({duration:.2f}s)")
                    else:
                        failed_runs += 1
                        console_error(f"Run {i + 1} failed with exit code {result.returncode}")

                        if no_output:
                            if result.stdout:
                                cprint(f"dbt output:\n{result.stdout}")
                            if result.stderr:
                                cprint(f"dbt error output:\n{result.stderr}")

                        if fail_fast:
                            cprint("\n⚠️  Stopping execution (--fail-fast enabled)")
                            return 1

                except (OSError, subprocess.SubprocessError) as exc:
                    failed_runs += 1
                    console_error(f"Run {i + 1} failed with exception: {exc}")
                    if fail_fast:
                        return 1

                cprint("-" * 80)

                if i < iterations - 1 and delay > 0:
                    cprint(f"⏳ Waiting {delay}s before next run...\n")
                    time.sleep(delay)

            cprint(f"\n{'=' * 80}")
            cprint(f"📊 Execution Summary for {build_id}:")
            cprint(f"   Total runs: {iterations}")
            cprint(f"   ✅ Successful: {successful_runs}")
            cprint(f"   ❌ Failed: {failed_runs}")
            cprint(f"{'=' * 80}")

            return 0 if failed_runs == 0 else 1
        finally:
            if temp_profiles_dir:
                temp_profiles_dir.cleanup()

    return unknown_trigger(trigger_type)
