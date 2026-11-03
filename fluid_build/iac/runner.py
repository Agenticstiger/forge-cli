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
import logging
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

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
# S3-native state locking (``use_lockfile``) arrived in OpenTofu 1.10.
_S3_LOCKFILE_MIN_VERSION = (1, 10, 0)
# After SIGINT on a timeout, how long tofu gets to release its lock and exit
# before it is killed.
_TIMEOUT_GRACE_SECONDS = 30

logger = logging.getLogger(__name__)


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
    child = subprocess.Popen(
        [tofu, *args],
        cwd=workdir,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # SIGINT first: tofu's interrupt stops the operation and releases the
        # state lock. A SIGKILL leaves ``<key>.tflock`` behind and every later
        # run fails until someone runs ``tofu force-unlock``. Only a tofu that
        # ignores the interrupt for the grace period is killed.
        try:
            child.send_signal(signal.SIGINT)
            stdout, _ = child.communicate(timeout=_TIMEOUT_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            child.kill()
            stdout, _ = child.communicate()
        # Surface the partial output if any — the timeout itself is the
        # signal (apply engine treats a TofuResult with returncode != 0
        # as a failure). Use returncode=124 to mirror coreutils ``timeout``.
        return TofuResult(
            command=command,
            returncode=124,
            stdout=stdout or "",
            stderr=(
                f"`tofu {command}` exceeded the {timeout}s wall-clock "
                "limit (FLUID_TOFU_TIMEOUT_SECONDS overrides the default)."
            ),
            events=[],
        )
    proc = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
    events = _parse_json_events(proc.stdout) if "-json" in args else []
    return TofuResult(
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        events=events,
    )


def _needs_reconfigure_for_lockfile(workdir: str, config: Mapping[str, Any]) -> bool:
    """True when ``workdir`` was initialised on this same S3 state, without the lockfile.

    Adding ``use_lockfile`` to a kept workdir changes its backend config, which
    ``tofu init`` refuses without ``-reconfigure``. That is safe only when the
    recorded backend is this one: the same bucket, key and region (compared when
    the module names one), ``use_lockfile`` not already true. A recorded backend
    that differs in any of them is left to tofu's own error, never repointed.
    """
    from .state_migration import recorded_backend

    recorded = recorded_backend(Path(workdir))
    if not recorded or set(recorded) != {"s3"}:
        return False
    have = recorded["s3"] or {}
    if have.get("use_lockfile") is True:
        return False
    fields = ("bucket", "key") + (("region",) if config.get("region") else ())
    return all(have.get(k) == config.get(k) for k in fields)


def _drops_saved_lockfile(workdir: str, config: Mapping[str, Any]) -> bool:
    """True when ``workdir`` saved ``use_lockfile=true`` for this same bucket and key.

    With no ``-backend-config`` override tofu reuses that saved partial config, so
    an opt-out (or a tofu we will not pass the flag to) needs ``-reconfigure`` to
    drop it. Same bucket and key, so no state moves.
    """
    from .state_migration import recorded_backend

    recorded = recorded_backend(Path(workdir))
    if not recorded or set(recorded) != {"s3"}:
        return False
    have = recorded["s3"] or {}
    return have.get("use_lockfile") is True and all(
        have.get(k) == config.get(k) for k in ("bucket", "key")
    )


def _s3_lockfile_args(workdir: str, *, force_copy: bool = False) -> List[str]:
    """``-backend-config=use_lockfile=true`` when the module's backend is S3.

    The flag is passed at init, not emitted into the module, so the module's
    bytes (an adopter may keep them as golden files) do not change. Gated on
    the installed tofu (>= 1.10) and switched off by ``FLUID_TOFU_S3_LOCKFILE=0``
    for a role that cannot write the ``<key>.tflock`` object. ``-reconfigure``
    rides along when a kept workdir recorded this state without the lockfile, and
    when it saved the lockfile but the flag is no longer passed (so the opt-out
    takes effect). Never with ``force_copy``: tofu lets ``-reconfigure`` win over
    ``-force-copy`` and would silently skip the state copy.
    """
    try:
        with open(os.path.join(workdir, "main.tf.json"), encoding="utf-8") as fh:
            backend = json.load(fh).get("terraform", {}).get("backend", {})
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(backend, dict) or not isinstance(backend.get("s3"), dict):
        return []
    if os.environ.get("FLUID_TOFU_S3_LOCKFILE", "").strip().lower() in ("0", "false", "no", "off"):
        return _unlock_args(workdir, backend["s3"], force_copy)
    version = tofu_version()
    if version is None or version < _S3_LOCKFILE_MIN_VERSION:
        if version is None:
            reason = (
                "the OpenTofu version could not be read, so the lock flag was skipped "
                "rather than risk an unrecognised build failing init"
            )
        else:
            reason = (
                f"OpenTofu {'.'.join(str(p) for p in version)} predates 1.10, so two applies "
                "on the same product can race. Upgrade OpenTofu to 1.10 or newer, "
                "or run with --ensure-opentofu (pins a version that locks)"
            )
        logger.warning(
            json.dumps(
                {
                    "event": "s3_state_unlocked",
                    "tofu_version": ".".join(str(p) for p in version) if version else None,
                    "message": f"S3 state is not locked: {reason}.",
                }
            )
        )
        return _unlock_args(workdir, backend["s3"], force_copy)
    args = ["-backend-config=use_lockfile=true"]
    if not force_copy and _needs_reconfigure_for_lockfile(workdir, backend["s3"]):
        args.append("-reconfigure")
    return args


def _unlock_args(workdir: str, config: Mapping[str, Any], force_copy: bool) -> List[str]:
    if not force_copy and _drops_saved_lockfile(workdir, config):
        return ["-reconfigure"]
    return []


def tofu_init(
    workdir: str,
    *,
    backend: bool = True,
    env: Optional[Mapping[str, str]] = None,
    reconfigure: bool = False,
    force_copy: bool = False,
    plugin_dir: Optional[str] = None,
):
    """``tofu init`` — install providers (and initialise the backend).

    ``reconfigure`` passes ``-reconfigure``: take the module's backend as
    given and ignore the one ``.terraform/`` recorded, moving no state.
    ``force_copy`` passes ``-force-copy``, which implies ``-migrate-state``:
    copy the recorded backend's state into the module's backend without a
    prompt, overwriting whatever the destination holds (OpenTofu
    ``backendMigrateState_s_s``), so a caller checks the destination first.
    ``plugin_dir`` passes ``-plugin-dir``: providers come only from that
    directory, "as if it had been configured as a ``filesystem_mirror``"
    (opentofu.org/docs/cli/commands/init), so nothing is downloaded.
    """
    args = ["init", "-input=false", "-no-color"]
    if not backend:
        args.append("-backend=false")
    else:
        args.extend(a for a in _s3_lockfile_args(workdir, force_copy=force_copy) if a not in args)
    if reconfigure and "-reconfigure" not in args:
        args.append("-reconfigure")
    if force_copy:
        args.append("-force-copy")
    if plugin_dir:
        args.append(f"-plugin-dir={plugin_dir}")
    return _run(args, workdir=workdir, env=env, command="init")


def tofu_state_pull(workdir: str, *, env: Optional[Mapping[str, str]] = None) -> TofuResult:
    """``tofu state pull`` — the backend's current state document, as JSON text.

    A key that holds no state prints a document with an empty ``lineage``
    and serial 0 (measured on OpenTofu 1.12 against an S3 backend), not an
    error; see :mod:`fluid_build.iac.state_migration` for how that is read.
    """
    return _run(["state", "pull"], workdir=workdir, env=env, command="state-pull")


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


#: ``tofu plan -detailed-exitcode``: succeeded with a non-empty diff.
PLAN_HAS_CHANGES = 2


def tofu_plan_detailed(
    workdir: str, *, out_file: str, env: Optional[Mapping[str, str]] = None
) -> TofuResult:
    """``tofu plan -detailed-exitcode -out=<out_file>``, for reading, not applying.

    The exit code is 0 for no changes, 1 for an error and
    :data:`PLAN_HAS_CHANGES` for changes (opentofu.org/docs/cli/commands/plan),
    so a caller must read ``returncode`` rather than ``ok``. The plan refreshes
    every managed object first and writes nothing to state; the saved file is
    read back with :func:`tofu_show_plan`.
    """
    return _run(
        ["plan", "-input=false", "-no-color", "-detailed-exitcode", f"-out={out_file}"],
        workdir=workdir,
        env=env,
        command="plan-detailed",
    )


def tofu_show_plan(
    workdir: str, *, plan_file: str, env: Optional[Mapping[str, str]] = None
) -> Optional[Dict[str, Any]]:
    """The saved plan as ``tofu show -json`` prints it, or ``None``.

    ``None`` for a failed command or output that is not one JSON object, so
    a caller can tell "no plan to read" from an empty one.
    """
    result = _run(["show", "-json", plan_file], workdir=workdir, env=env, command="show-plan")
    if not result.ok:
        return None
    try:
        payload = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


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


def tofu_state_resources(
    workdir: str, *, env: Optional[Mapping[str, str]] = None
) -> List[Dict[str, Any]]:
    """Resources in the CURRENT state, from ``tofu show -json`` (no refresh).

    Read before planning, so a caller can compare what state recorded with what
    the new module asks for. Best-effort like :func:`tofu_state_list`: ``[]``
    for no state, an unreadable one, or a failed command.
    """
    result = _run(["show", "-json"], workdir=workdir, env=env, command="show-state")
    if not result.ok:
        return []
    try:
        doc = json.loads(result.stdout or "{}")
    except ValueError:
        return []
    out: List[Dict[str, Any]] = []
    stack = [((doc.get("values") or {}).get("root_module") or {})]
    while stack:
        mod = stack.pop()
        out.extend(mod.get("resources") or [])
        stack.extend(mod.get("child_modules") or [])
    return out


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


def tofu_state_rm(
    workdir: str, addresses: List[str], *, env: Optional[Mapping[str, str]] = None
) -> TofuResult:
    """``tofu state rm`` — stop tracking resources in state. The cloud objects
    stay as they are: this only forgets them (OpenTofu's ``state rm``)."""
    return _run(["state", "rm", *addresses], workdir=workdir, env=env, command="state-rm")


def planned_removals(result: TofuResult) -> List[Tuple[str, str]]:
    """``(address, resource type)`` of every resource a plan deletes or replaces.

    Read from the ``planned_change`` events of ``tofu plan -json`` (OpenTofu's
    machine-readable UI: ``change.action`` is ``delete`` or ``replace`` for the two
    that remove an object). The ``change_summary`` event's ``remove`` counts the
    same resources, so a caller can tell which of them hold data.
    """
    out: List[Tuple[str, str]] = []
    for event in result.events:
        if event.get("type") != "planned_change":
            continue
        change = event.get("change") or {}
        if change.get("action") not in ("delete", "replace"):
            continue
        resource = change.get("resource") or {}
        out.append((str(resource.get("addr") or ""), str(resource.get("resource_type") or "")))
    return out


def change_summary(result: TofuResult) -> Dict[str, int]:
    """Extract the ``{add, change, remove}`` counts from a plan/apply result."""
    for event in result.events:
        if event.get("type") == "change_summary":
            changes = event.get("changes") or {}
            return {key: int(changes.get(key, 0)) for key in ("add", "change", "remove")}
    return {"add": 0, "change": 0, "remove": 0}
