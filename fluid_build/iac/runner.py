# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Provider-agnostic OpenTofu runner — wraps the ``tofu`` CLI via subprocess.

Consumes OpenTofu's machine-readable ``-json`` output (a stable,
versioned protocol) rather than scraping human text. The runner shells
``tofu`` only — never ``terraform`` — so the execution path stays MPL.

Credentials are passed by the caller via the ``env`` mapping (a full
environment, e.g. ``os.environ | provider_env``); the runner never
embeds them in argv or in the ``.tf.json``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

# Per-command wall-clock cap. A hung ``tofu`` (e.g., an unauthenticated
# interactive auth prompt that ``-input=false`` did not catch, or a cloud
# API that hangs the keepalive) is a real prod-risk; CI workflow timeouts
# are too coarse a backstop. Default 30min — long enough for a real apply
# of dozens of resources, short enough that a stuck process surfaces.
# Override with ``FLUID_TOFU_TIMEOUT_SECONDS`` if a particular apply
# legitimately exceeds it (e.g., Lake Formation tag propagation, which
# can take many minutes for the eventual consistency to settle).
_DEFAULT_TOFU_TIMEOUT_SECONDS = 1800
_MIN_REQUIRED_VERSION = (1, 6, 0)


def _resolve_timeout() -> int:
    raw = os.environ.get("FLUID_TOFU_TIMEOUT_SECONDS")
    if not raw:
        return _DEFAULT_TOFU_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TOFU_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_TOFU_TIMEOUT_SECONDS


class TofuError(RuntimeError):
    """The ``tofu`` binary is unavailable on PATH."""


class TofuVersionError(RuntimeError):
    """The discovered ``tofu`` is older than the required minimum."""


@dataclass
class TofuResult:
    """Outcome of a single ``tofu`` invocation."""

    command: str
    returncode: int
    stdout: str
    stderr: str
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def tofu_path() -> Optional[str]:
    """Return the path to the ``tofu`` binary, or ``None`` if not installed."""
    return shutil.which("tofu")


def _require_tofu() -> str:
    path = tofu_path()
    if path is None:
        raise TofuError(
            "the `tofu` (OpenTofu) binary was not found on PATH — install it "
            "from https://opentofu.org/docs/intro/install/"
        )
    return path


_VERSION_RE = re.compile(r"OpenTofu\s+v?(\d+)\.(\d+)\.(\d+)", re.IGNORECASE)


def tofu_version() -> Optional[tuple]:
    """Return the installed ``tofu`` version tuple, or ``None`` on failure.

    Cheap probe (``tofu --version``) called once per CLI invocation and
    cached on the process; intended for the engine's startup gate, not
    for hot-path use.
    """
    path = tofu_path()
    if path is None:
        return None
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    match = _VERSION_RE.search(proc.stdout) or _VERSION_RE.search(proc.stderr)
    if not match:
        return None
    return tuple(int(p) for p in match.groups())


def require_tofu_version() -> None:
    """Fail loud if the installed ``tofu`` is older than the supported floor.

    Catches the silent-mixup case where a host has ``terraform`` installed
    but no ``tofu``, OR the case where an out-of-date ``tofu`` builds an
    incompatible plan — the apply engine would otherwise discover the
    mismatch only mid-apply, after partial state has been mutated.
    """
    version = tofu_version()
    if version is None:
        # ``tofu --version`` did not parse — keep going (a future tofu
        # output format change should not brick the engine). The
        # downstream commands will surface any real failure.
        return
    if version < _MIN_REQUIRED_VERSION:
        raise TofuVersionError(
            f"tofu {'.'.join(str(p) for p in version)} is older than the "
            f"required minimum {'.'.join(str(p) for p in _MIN_REQUIRED_VERSION)} — "
            "upgrade from https://opentofu.org/docs/intro/install/"
        )


def _parse_json_events(stdout: str) -> List[Dict[str, Any]]:
    """Parse OpenTofu's newline-delimited JSON log stream."""
    events: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a non-JSON line slipped through; ignore it
    return events


def _run(
    args: List[str],
    *,
    workdir: str,
    env: Optional[Mapping[str, str]],
    command: str,
) -> TofuResult:
    tofu = _require_tofu()
    timeout = _resolve_timeout()
    try:
        proc = subprocess.run(
            [tofu, *args],
            cwd=workdir,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # Surface the partial output if any — the timeout itself is the
        # signal (apply engine treats a TofuResult with returncode != 0
        # as a failure). Use returncode=124 to mirror coreutils ``timeout``.
        return TofuResult(
            command=command,
            returncode=124,
            stdout=(
                (exc.stdout or b"").decode("utf-8", errors="replace")
                if isinstance(exc.stdout, (bytes, bytearray))
                else (exc.stdout or "")
            ),
            stderr=(
                f"`tofu {command}` exceeded the {timeout}s wall-clock "
                "limit (FLUID_TOFU_TIMEOUT_SECONDS overrides the default)."
            ),
            events=[],
        )
    events = _parse_json_events(proc.stdout) if "-json" in args else []
    return TofuResult(
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        events=events,
    )


def tofu_init(workdir: str, *, backend: bool = True, env: Optional[Mapping[str, str]] = None):
    """``tofu init`` — install providers (and initialise the backend)."""
    args = ["init", "-input=false", "-no-color"]
    if not backend:
        args.append("-backend=false")
    return _run(args, workdir=workdir, env=env, command="init")


def tofu_validate(workdir: str, *, env: Optional[Mapping[str, str]] = None) -> TofuResult:
    """``tofu validate`` — check config syntax + provider-schema correctness."""
    return _run(["validate", "-no-color"], workdir=workdir, env=env, command="validate")


def tofu_plan(
    workdir: str, *, out_file: str = "tfplan", env: Optional[Mapping[str, str]] = None
) -> TofuResult:
    """``tofu plan`` — write a binary plan to ``out_file`` for later apply."""
    return _run(
        ["plan", "-input=false", "-no-color", "-json", f"-out={out_file}"],
        workdir=workdir,
        env=env,
        command="plan",
    )


def tofu_apply(
    workdir: str, *, plan_file: str = "tfplan", env: Optional[Mapping[str, str]] = None
) -> TofuResult:
    """``tofu apply`` — apply a previously-saved plan file (no re-prompt)."""
    return _run(
        ["apply", "-input=false", "-no-color", "-json", plan_file],
        workdir=workdir,
        env=env,
        command="apply",
    )


def tofu_destroy(workdir: str, *, env: Optional[Mapping[str, str]] = None) -> TofuResult:
    """``tofu destroy`` — tear down everything in state (used by rollback)."""
    return _run(
        ["destroy", "-input=false", "-no-color", "-json", "-auto-approve"],
        workdir=workdir,
        env=env,
        command="destroy",
    )


def tofu_state_list(workdir: str, *, env: Optional[Mapping[str, str]] = None) -> List[str]:
    """``tofu state list`` — resource addresses currently tracked in state.

    Returns an empty list when there is no state yet (fresh workdir) or the
    command fails — callers treat "not in state" as "not yet adopted".
    """
    result = _run(["state", "list"], workdir=workdir, env=env, command="state-list")
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def tofu_prior_state_resources(
    workdir: str,
    *,
    plan_file: str = "tfplan",
    env: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Resources in the refreshed pre-apply state, from ``tofu show -json``.

    ``tofu plan -out=<plan_file>`` records the state as refreshed from the
    cloud, so this is what the provider *actually* has right now — including
    attributes ``lifecycle.ignore_changes`` suppressed from the diff, which
    ``resource_changes`` by definition does not carry.

    Best-effort like :func:`tofu_state_list`: an unreadable / absent plan or
    an older ``tofu`` returns ``[]`` and callers treat that as "nothing to
    compare", never as an apply failure.
    """
    result = _run(["show", "-json", plan_file], workdir=workdir, env=env, command="show-plan")
    if not result.ok:
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return []
    root = ((payload.get("prior_state") or {}).get("values") or {}).get("root_module") or {}
    resources = root.get("resources")
    return [r for r in resources if isinstance(r, dict)] if isinstance(resources, list) else []


def tofu_import(
    workdir: str,
    address: str,
    resource_id: str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> TofuResult:
    """``tofu import`` — adopt one pre-existing cloud resource into state.

    Best-effort by design: importing a resource that does not exist fails
    with a clear provider error, which the caller tolerates (the resource
    is then created by ``tofu apply`` instead).
    """
    return _run(
        ["import", "-input=false", "-no-color", address, resource_id],
        workdir=workdir,
        env=env,
        command="import",
    )


def change_summary(result: TofuResult) -> Dict[str, int]:
    """Extract the ``{add, change, remove}`` counts from a plan/apply result."""
    for event in result.events:
        if event.get("type") == "change_summary":
            changes = event.get("changes") or {}
            return {key: int(changes.get(key, 0)) for key in ("add", "change", "remove")}
    return {"add": 0, "change": 0, "remove": 0}
