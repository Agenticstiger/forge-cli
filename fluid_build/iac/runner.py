# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from .credentials import TOFU_WRAPPER_ENV_NAMES

# Per-command wall-clock cap. A hung ``tofu`` (e.g., an unauthenticated
# interactive auth prompt that ``-input=false`` did not catch, or a cloud
# API that hangs the keepalive) is a real prod-risk; CI workflow timeouts
# are too coarse a backstop. Default 30min — long enough for a real apply
# of dozens of resources, short enough that a stuck process surfaces.
# Override with ``FLUID_TOFU_TIMEOUT_SECONDS`` if a particular apply
# legitimately exceeds it (e.g., Lake Formation tag propagation, which
# can take many minutes for the eventual consistency to settle).
_DEFAULT_TOFU_TIMEOUT_SECONDS = 1800
_MIN_REQUIRED_VERSION = (1, 12, 0)
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


#: All a ``tofu --version`` probe gets: where programs are, the home directory,
#: what a Go binary needs on Windows, and the names setup-opentofu's wrapper
#: needs (``TOFU_CLI_PATH``, ``GITHUB_OUTPUT``), without which it fails in GitHub
#: Actions. No credential, proxy or OpenTofu setting: a version check needs none.
VERSION_PROBE_ENV_NAMES: Tuple[str, ...] = (
    "PATH",
    "HOME",
    "SYSTEMROOT",
    "USERPROFILE",
    "PATHEXT",
    *sorted(TOFU_WRAPPER_ENV_NAMES),
)

_VERSION_RE = re.compile(r"OpenTofu\s+v?(\d+)\.(\d+)\.(\d+)", re.IGNORECASE)


def tofu_version() -> Optional[tuple]:
    """Return the installed ``tofu`` version tuple, or ``None`` on failure.

    Cheap probe (``tofu --version``) called once per CLI invocation and
    cached on the process; intended for the engine's startup gate, not
    for hot-path use. It runs with :data:`VERSION_PROBE_ENV_NAMES` only, never
    this process's whole environment.
    """
    path = tofu_path()
    if path is None:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env={n: os.environ[n] for n in VERSION_PROBE_ENV_NAMES if n in os.environ},
        )
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
    env: Mapping[str, str],
    command: str,
) -> TofuResult:
    if env is None:  # Popen(env=None) hands tofu this process's whole environment
        raise TypeError(
            "tofu needs an explicit environment (iac.credentials.build_tofu_env): "
            "env=None would hand it this process's whole environment"
        )
    tofu = _require_tofu()
    timeout = _resolve_timeout()
    child = subprocess.Popen(
        [tofu, *args],
        cwd=workdir,
        env=dict(env),
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
    env: Mapping[str, str],
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


def tofu_state_pull(workdir: str, *, env: Mapping[str, str]) -> TofuResult:
    """``tofu state pull`` — the backend's current state document, as JSON text.

    A key that holds no state prints a document with an empty ``lineage``
    and serial 0 (measured on OpenTofu 1.12 against an S3 backend), not an
    error; see :mod:`fluid_build.iac.state_migration` for how that is read.
    """
    return _run(["state", "pull"], workdir=workdir, env=env, command="state-pull")


def tofu_validate(workdir: str, *, env: Mapping[str, str]) -> TofuResult:
    """``tofu validate`` — check config syntax + provider-schema correctness."""
    return _run(["validate", "-no-color"], workdir=workdir, env=env, command="validate")


def tofu_plan(workdir: str, *, out_file: str = "tfplan", env: Mapping[str, str]) -> TofuResult:
    """``tofu plan`` — write a binary plan to ``out_file`` for later apply."""
    return _run(
        ["plan", "-input=false", "-no-color", "-json", f"-out={out_file}"],
        workdir=workdir,
        env=env,
        command="plan",
    )


#: ``tofu plan -detailed-exitcode``: succeeded with a non-empty diff.
PLAN_HAS_CHANGES = 2


def tofu_plan_detailed(workdir: str, *, out_file: str, env: Mapping[str, str]) -> TofuResult:
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
    workdir: str, *, plan_file: str, env: Mapping[str, str]
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


def tofu_apply(workdir: str, *, plan_file: str = "tfplan", env: Mapping[str, str]) -> TofuResult:
    """``tofu apply`` — apply a previously-saved plan file (no re-prompt)."""
    return _run(
        ["apply", "-input=false", "-no-color", "-json", plan_file],
        workdir=workdir,
        env=env,
        command="apply",
    )


def tofu_destroy(workdir: str, *, env: Mapping[str, str]) -> TofuResult:
    """``tofu destroy`` — tear down everything in state (used by rollback)."""
    return _run(
        ["destroy", "-input=false", "-no-color", "-json", "-auto-approve"],
        workdir=workdir,
        env=env,
        command="destroy",
    )


def tofu_state_list(workdir: str, *, env: Mapping[str, str]) -> List[str]:
    """``tofu state list`` — resource addresses currently tracked in state.

    Returns an empty list when there is no state yet (fresh workdir) or the
    command fails — callers treat "not in state" as "not yet adopted".
    """
    result = _run(["state", "list"], workdir=workdir, env=env, command="state-list")
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def tofu_state_resources(workdir: str, *, env: Mapping[str, str]) -> List[Dict[str, Any]]:
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
    env: Mapping[str, str],
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
    env: Mapping[str, str],
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


def tofu_state_rm(workdir: str, addresses: List[str], *, env: Mapping[str, str]) -> TofuResult:
    """``tofu state rm`` — stop tracking resources in state. The cloud objects
    stay as they are: this only forgets them (OpenTofu's ``state rm``)."""
    return _run(["state", "rm", *addresses], workdir=workdir, env=env, command="state-rm")


def _resource_events(
    result: TofuResult, kind: str, body: str
) -> Iterator[Tuple[str, Mapping[str, Any]]]:
    """``(action, resource)`` of each ``kind`` event of a ``-json`` stream (OpenTofu's
    machine-readable UI): a ``planned_change`` carries them under ``change``, an
    ``apply_*`` hook event under ``hook``."""
    for event in result.events:
        if event.get("type") == kind:
            detail = event.get(body) or {}
            yield str(detail.get("action") or ""), detail.get("resource") or {}


def planned_removals(result: TofuResult) -> List[Tuple[str, str]]:
    """``(address, resource type)`` of every resource a plan deletes or replaces.

    Read from the ``planned_change`` events of ``tofu plan -json`` (OpenTofu's
    machine-readable UI: ``change.action`` is ``delete`` or ``replace`` for the two
    that remove an object). The ``change_summary`` event's ``remove`` counts the
    same resources, so a caller can tell which of them hold data.
    """
    return [
        (str(resource.get("addr") or ""), str(resource.get("resource_type") or ""))
        for action, resource in _resource_events(result, "planned_change", "change")
        if action in ("delete", "replace")
    ]


def errored_resources(result: TofuResult) -> List[Tuple[str, str]]:
    """``(address, action)`` of every resource ``tofu apply -json`` failed on, from its
    ``apply_errored`` events (the diagnostic that follows one carries no address)."""
    return [
        (str(resource.get("addr") or ""), action)
        for action, resource in _resource_events(result, "apply_errored", "hook")
    ]


def planned_deletes(shown: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """``{"address", "type", "before"}`` of each managed resource a saved plan, as ``tofu
    show -json`` prints it, only deletes (a replaced one comes back, so it is not one)."""
    deletes: List[Dict[str, Any]] = []
    for change in shown.get("resource_changes") or []:
        detail = change.get("change") if isinstance(change, Mapping) else None
        if not isinstance(detail, Mapping) or change.get("mode", "managed") != "managed":
            continue
        if list(detail.get("actions") or []) != ["delete"]:
            continue
        before = detail.get("before")
        deletes.append(
            {
                "address": str(change.get("address") or ""),
                "type": str(change.get("type") or ""),
                "before": before if isinstance(before, Mapping) else {},
            }
        )
    return deletes


def change_summary(result: TofuResult) -> Dict[str, int]:
    """Extract the ``{add, change, remove}`` counts from a plan/apply result."""
    for event in result.events:
        if event.get("type") == "change_summary":
            changes = event.get("changes") or {}
            return {key: int(changes.get(key, 0)) for key in ("add", "change", "remove")}
    return {"add": 0, "change": 0, "remove": 0}


def _managed_changes(shown: Mapping[str, Any]) -> Iterator[Tuple[Mapping[str, Any], List[str]]]:
    """``(resource change, its actions)`` of each managed resource in a saved plan, as
    ``tofu show -json`` prints it (opentofu.org/docs/internals/json-format)."""
    for change in shown.get("resource_changes") or []:
        detail = change.get("change") if isinstance(change, Mapping) else None
        if not isinstance(detail, Mapping) or change.get("mode", "managed") != "managed":
            continue
        yield change, [str(action) for action in detail.get("actions") or []]


def saved_plan_changes(shown: Mapping[str, Any]) -> Dict[str, int]:
    """The ``{add, change, remove}`` counts of a saved plan, as :func:`change_summary`
    reads them from a ``tofu plan -json`` stream: a replace counts as one add and one
    remove, an import or a forget as neither."""
    counts = {"add": 0, "change": 0, "remove": 0}
    for _change, actions in _managed_changes(shown):
        counts["add"] += actions.count("create")
        counts["change"] += actions.count("update")
        counts["remove"] += actions.count("delete")
    return counts


def saved_plan_removals(shown: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """``(address, resource type)`` of every resource a saved plan deletes or replaces:
    :func:`planned_removals` for a plan read back with ``tofu show -json``."""
    return [
        (str(change.get("address") or ""), str(change.get("type") or ""))
        for change, actions in _managed_changes(shown)
        if "delete" in actions
    ]


def gitlab_plan_report(shown: Mapping[str, Any]) -> str:
    """The GitLab ``artifacts:reports:terraform`` file for a saved plan.

    Byte for byte what the GitLab OpenTofu component writes
    (gitlab.com/components/opentofu ``src/gitlab-tofu.sh``, ``write_plan_json``
    at 9e5d830a): ``jq -r`` over ``tofu show -json`` with its ``plan_jq_filter``,
    which counts the ``create``, ``update`` and ``delete`` actions of every
    resource change (data sources included), pretty-printed with two spaces.
    """
    actions = [
        action
        for change in shown.get("resource_changes") or []
        if isinstance(change, Mapping) and isinstance(change.get("change"), Mapping)
        for action in change["change"].get("actions") or []
    ]
    report = {key: actions.count(key) for key in ("create", "update", "delete")}
    return json.dumps(report, indent=2) + "\n"
