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
from typing import Any, Dict, List, Optional, Tuple

import yaml

from fluid_build._console import cprint, success
from fluid_build._console import error as console_error

from .._path_safety import confine_to_workspace
from ..base import SENSITIVE_ENV_KEY_RE, _resolve_env_placeholders
from .profiles import (
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


def _infer_dbt_adapter(build: Dict[str, Any]) -> Optional[str]:
    engine = str(build.get("engine") or "").strip().lower()
    if engine.startswith("dbt-") and len(engine) > 4:
        return engine[4:]

    execution = build.get("execution") or {}
    runtime = execution.get("runtime") or {}
    platform = str(runtime.get("platform") or "").strip().lower()
    if platform in {"snowflake", "bigquery", "duckdb", "redshift", "postgres", "spark"}:
        return platform
    if platform == "gcp":
        return "bigquery"
    if platform == "aws":
        return "redshift"
    return None


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
_BARE_VERSION_RE = re.compile(r"^\s*dbt\s+v?([0-9][\w.+-]*)\s*$", re.IGNORECASE | re.MULTILINE)


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
    * **Core / unknown** — v1 lists pip-installed adapter plugins in the
      version output; keep the substring probe. An unrunnable executable
      still returns False (Docker fallback preserved).
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

    lowered = output.lower()
    return adapter.lower() in lowered or f"dbt-{adapter.lower()}" in lowered


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


def _build_containerized_dbt_command(
    adapter: str,
    args: List[str],
    project_dir: Path,
    profiles_dir: Optional[Path],
) -> List[str]:
    # ``-e KEY`` (no ``=value``) tells docker to inherit the value from the current
    # process env. That keeps secrets out of argv (visible via ``ps``) and out of
    # the command-log line printed by ``cprint``.
    env_keys = sorted(_collect_dbt_container_env().keys())
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

    docker_image = os.getenv("DBT_DOCKER_IMAGE")
    if docker_image:
        cmd.extend([docker_image, "dbt", *container_args])
        return cmd

    bootstrap_image = os.getenv("DBT_BOOTSTRAP_IMAGE", "python:3.12-slim")
    adapter_package = os.getenv("DBT_ADAPTER_PACKAGE") or f"dbt-{adapter}"
    install_and_run = "python -m pip install --quiet {pkg} && {dbt_cmd}".format(
        pkg=shlex.quote(adapter_package),
        dbt_cmd=" ".join(shlex.quote(part) for part in ["dbt", *container_args]),
    )
    cmd.extend([bootstrap_image, "sh", "-lc", install_and_run])
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

    selectors = _normalize_selectors(props.get("select") or props.get("models"))
    if not selectors:
        model = props.get("model")
        outputs = build.get("outputs") or []
        if model and len(outputs) <= 1:
            selectors = [f"+{model}+"]
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
        # ``--no-partial-parse`` is safe for containers (no persistent parse cache
        # across runs) and avoids stale-cache surprises. Kept off the local path
        # where users may legitimately rely on partial-parse for fast iteration.
        container_args = [dbt_args[0], "--no-partial-parse", *dbt_args[1:]]
        return _build_containerized_dbt_command(adapter, container_args, project_dir, profiles_dir)

    if dbt_executable:
        raise RuntimeError(
            f"dbt executable '{dbt_executable}' does not support the required adapter '{adapter}'. "
            "Install the adapter locally, set DBT_EXECUTABLE to a compatible command, or make Docker available."
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
    execution = build.get("execution", {})
    trigger = execution.get("trigger", {})
    trigger_type = trigger.get("type", "manual")

    cprint(f"\n{'=' * 80}")
    cprint(f"📋 Build: {build_id}")
    cprint(f"   dbt project: {project_dir}")
    trigger_label = trigger_type
    if trigger_type == "schedule" and force_run:
        trigger_label = "schedule (manual apply override)"
    cprint(f"   Trigger: {trigger_label}")

    project_config = _load_dbt_project_config(project_dir)
    profiles_dir, temp_profiles_dir = resolve_dbt_profiles_dir(build, project_dir, project_config)

    try:
        command = build_dbt_command(
            build,
            project_dir,
            profiles_dir=profiles_dir,
            project_config=project_config,
            apply_mode=apply_mode,
        )
    except (RuntimeError, OSError, yaml.YAMLError) as exc:
        if temp_profiles_dir:
            temp_profiles_dir.cleanup()
        console_error(f"Unable to prepare dbt build '{build_id}': {exc}")
        cprint(f"{'=' * 80}")
        return 1

    cprint(f"   Command: {_render_command_for_log(command)}")

    if trigger_type == "manual" or (trigger_type == "schedule" and force_run):
        iterations = 1 if trigger_type == "schedule" and force_run else trigger.get("iterations", 1)
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

    if trigger_type == "schedule":
        cron = trigger.get("cron", "")
        cprint(f"   Cron: {cron}")
        cprint("   ⚠️  Scheduled execution requires Cloud Composer/Scheduler (paid tier)")
        cprint("   💡 For free tier, use trigger.type: manual with iterations")
        cprint(f"{'=' * 80}")
        return 0

    cprint(f"   ❌ Unknown trigger type: {trigger_type}")
    cprint("   Supported types: manual, schedule")
    cprint(f"{'=' * 80}")
    return 1
