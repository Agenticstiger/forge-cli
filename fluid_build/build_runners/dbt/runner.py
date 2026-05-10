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

Private helpers follow the legacy ``cli/execute.py`` shape so the
test-rewrite is a mechanical module-path update.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error

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
    configured = os.getenv("DBT_EXECUTABLE")
    if not configured:
        return None
    parts = shlex.split(configured)
    return parts or None


def _normalize_selectors(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return [str(raw)]


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


@functools.lru_cache(maxsize=None)
def _dbt_command_supports_adapter(dbt_executable: str, adapter: str) -> bool:
    try:
        result = subprocess.run(
            [dbt_executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    output = f"{result.stdout}\n{result.stderr}".lower()
    return adapter.lower() in output or f"dbt-{adapter.lower()}" in output


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
        delay_from_contract = trigger.get("delaySeconds", trigger.get("delay"))
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

                start_time = time.time()

                try:
                    result = subprocess.run(
                        command,
                        cwd=contract_dir,
                        capture_output=no_output,
                        text=True,
                    )

                    duration = time.time() - start_time

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
