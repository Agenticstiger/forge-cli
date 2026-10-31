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

"""Databricks jobs for dbt builds: FLUID ``builds[]`` → ``databricks_job`` + workspace files.

A dbt build whose ``execution.runtime.platform`` is ``databricks``
(:func:`~fluid_build.util.contract.is_databricks_job_build`) runs in the
workspace: one serverless job with a ``dbt_task``, which Databricks runs on the
SQL warehouse ``DATABRICKS_WAREHOUSE_ID`` names. No schema field is added
(fluid-schema 0.7.6); every input comes from the build:

* ``repository``: an ``https://`` Git URL is a GIT source. Its ref is exactly
  one of ``?branch=``, ``?tag=`` or ``?commit=``, three distinct settings in
  Databricks, and guessing a default branch is how the wrong code ships;
  ``&path=`` names a project below the repository root. Any other value is the
  local dbt project, found as the dbt runner finds it
  (``build_runners/dbt/runner.py::resolve_dbt_project_path``) and uploaded as
  ``databricks_workspace_file`` resources under the deploying identity's home,
  ``<home>/.fluid/<product>/<build>/files``: the layout Databricks Asset Bundles
  deploy to (``<home>/.bundle/<bundle>/<target>/files``). Only what dbt reads
  is uploaded (``dbt_project.yml``, the package and selector files, and the
  directories its ``*-paths`` keys name), never ``profiles.yml``, a hidden file
  or a symbolic link.
* ``properties.select`` / ``models`` / ``model`` and ``vars``: the job runs
  ``dbt deps``, then ``dbt build`` with the selectors the local runner uses
  (``build_selectors``).
* ``execution.trigger``: a cron becomes ``schedule {}`` in Quartz
  (``util/cron.py::unix_to_quartz``); ``continuous`` becomes ``continuous {}``,
  the Databricks trigger schema 0.7.6 named its own after (#734); ``manual``, or
  no trigger, a job nobody starts but a person or ``databricks jobs run-now``.
* ``execution.retries``, ``notifications``, ``runtime.timeout`` and
  ``runtime.serviceAccount``: the task's retries and timeout, the job's email
  notifications and its ``run_as``.

The catalog and schema dbt writes to are those of the first Databricks expose
the build writes (``builds[].outputs``). Everything the job would accept with
exit 0 and not honour is refused, as for tables.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlsplit, urlunsplit

from ...util.binding_paths import ENV_PLACEHOLDER_RE
from ...util.contract import builds_writing_expose, is_databricks_job_build
from ...util.cron import unix_to_quartz
from ..base import UnsupportedBindingError
from ..naming import TofuExpr, digest_ident, safe_ident, tofu_ref, unique_ident
from ..source import current_contract_source

#: The serverless environment the dbt task runs in: client version 2 and the
#: dbt adapter, installed per run (Phase 0b ran exactly this against the provider).
ENVIRONMENT_VERSION = "2"
DBT_DATABRICKS = "dbt-databricks>=1.8.0,<2.0.0"

#: The service principals a contract may name as a job's run-as, by application id
#: (comma-separated). Unset, a contract can name none: a job runs as the applier.
RUN_AS_ALLOWLIST_ENV = "FLUID_DATABRICKS_RUN_AS_ALLOWLIST"

#: Workspace upload limits. Every file is base64 inside ``main.tf.json``, and the
#: provider allows "a few megabytes" per workspace file.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PROJECT_BYTES = 20 * 1024 * 1024
MAX_PROJECT_FILES = 1000

#: The data source that names the deploying identity's home folder.
CURRENT_USER = "databricks_current_user"
_HOME = tofu_ref(f"data.{CURRENT_USER}.me.home")


def _under_home(prefix: str, suffix: str) -> TofuExpr:
    """``<prefix><home><suffix>``: the home reference stays an expression, and the
    suffix (file names come from the project) is escaped as the renderer escapes
    any other literal, so a file named ``${x}.sql`` cannot inject interpolation."""
    literal = suffix.replace("${", "$${").replace("%{", "%%{")
    return TofuExpr(prefix + _HOME + literal)


#: Top-level project files dbt reads; ``profiles.yml`` is never one of them here.
_PROJECT_FILES = (
    "dbt_project.yml",
    "packages.yml",
    "dependencies.yml",
    "package-lock.yml",
    "selectors.yml",
)
#: ``dbt_project.yml`` path keys and dbt-core's defaults for them.
_PATH_KEYS = {
    "model-paths": ("models",),
    "macro-paths": ("macros",),
    "seed-paths": ("seeds",),
    "snapshot-paths": ("snapshots",),
    "test-paths": ("tests",),
    "analysis-paths": ("analyses",),
    "docs-paths": (),
    "asset-paths": (),
}

#: Git host → the Databricks ``git_source.provider`` value.
_GIT_PROVIDERS = {
    "github.com": "gitHub",
    "gitlab.com": "gitLab",
    "bitbucket.org": "bitbucketCloud",
    "dev.azure.com": "azureDevOpsServices",
}
_GIT_REFS = ("branch", "tag", "commit")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_REPO_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

#: ``1h``, ``30m``, ``45s``, ``2d``, a bare number of seconds, or ISO 8601 ``PT1H30M``.
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd]?)$")
_ISO_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

#: An email address (notifications) and a service principal's application id (``run_as``).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_APPLICATION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class DbtJob:
    """One Databricks dbt job, as the planner lists it."""

    build_id: str
    name: str
    address: str
    source: str
    trigger: str
    files: Tuple[str, ...]
    #: Where the files go, below the applying identity's home (WORKSPACE source).
    root: str = ""


@dataclass
class CompiledJobs:
    """The job resources for one contract, and every refusal."""

    #: ``(resource type, key, owner, body)``, each claimed by the caller.
    resources: List[Tuple[str, str, str, Dict[str, Any]]] = field(default_factory=list)
    data: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    refusals: List[UnsupportedBindingError] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    jobs: List[DbtJob] = field(default_factory=list)


def _refusal(kind: str, message: str, *remediation: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-job-{kind}", message, tuple(remediation))


def _seconds(raw: Any, where: str, refusals: List[UnsupportedBindingError]) -> Optional[int]:
    """A duration in whole seconds, or ``None`` (and a refusal) when unreadable."""
    text = str(raw).strip()
    plain = _DURATION_RE.match(text.lower())
    if plain:
        return int(plain.group(1)) * _UNITS[plain.group(2)]
    iso = _ISO_DURATION_RE.match(text.upper())
    if iso and any(iso.groups()):
        hours, minutes, secs = (int(part or 0) for part in iso.groups())
        return hours * 3600 + minutes * 60 + secs
    refusals.append(
        _refusal(
            "duration",
            f"{where} is {raw!r}, which is not a duration.",
            "Write it as 45s, 30m, 2h, 1d or ISO 8601 (PT1H30M).",
        )
    )
    return None


def _trigger(
    execution: Mapping[str, Any], where: str, refusals: List[UnsupportedBindingError]
) -> Tuple[str, Dict[str, Any]]:
    """``(kind, job settings)`` for ``execution.trigger``: schedule, continuous or manual."""
    trigger = execution.get("trigger") or {}
    if not isinstance(trigger, Mapping):
        trigger = {}
    kind = str(trigger.get("type") or "").strip().lower()
    cron, schedule = trigger.get("cron"), trigger.get("schedule")
    if cron and schedule and str(cron).strip() != str(schedule).strip():
        refusals.append(
            _refusal(
                "trigger-conflict",
                f"{where} execution.trigger sets cron {cron!r} and schedule {schedule!r}; "
                "the job can run on one of them.",
                "Keep one of cron and schedule.",
            )
        )
        return "manual", {}
    expression = cron or schedule
    if kind == "continuous":
        # Databricks keeps one run going and starts the next when it ends.
        return "continuous", {"continuous": {"pause_status": "UNPAUSED"}}
    if kind in ("", "schedule") and expression:
        try:
            quartz = unix_to_quartz(str(expression)).upper()
        except ValueError as exc:
            refusals.append(
                _refusal(
                    "cron",
                    f"{where} execution.trigger: {exc}.",
                    "Write a five-field cron (minute hour day-of-month month day-of-week) "
                    "or a nickname such as @daily; to run on both some dates and some "
                    "weekdays, split the build in two.",
                )
            )
            return "schedule", {}
        timezone = str(trigger.get("timezone") or "UTC")
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(timezone)
        except (ValueError, zoneinfo.ZoneInfoNotFoundError):
            refusals.append(
                _refusal(
                    "timezone",
                    f"{where} execution.trigger.timezone {timezone!r} is not a time zone name.",
                    "Use an IANA name such as UTC or Europe/Zurich.",
                )
            )
            return "schedule", {}
        return "schedule", {
            "schedule": {
                "quartz_cron_expression": quartz,
                "timezone_id": timezone,
                "pause_status": "UNPAUSED",
            }
        }
    if kind == "schedule":
        refusals.append(
            _refusal(
                "cron",
                f"{where} execution.trigger is type schedule with no cron.",
                "Set execution.trigger.cron, or use type manual.",
            )
        )
        return "schedule", {}
    if kind in ("", "manual"):
        return "manual", {}
    refusals.append(
        _refusal(
            "trigger-unmapped",
            f"{where} execution.trigger is type {kind!r}, which the Databricks job emitter "
            "does not map: the job would never start.",
            "Use type schedule (with cron), continuous or manual.",
        )
    )
    return "manual", {}


def _retries(
    execution: Mapping[str, Any], where: str, refusals: List[UnsupportedBindingError]
) -> Dict[str, Any]:
    """Task retry settings: Databricks retries after a fixed minimum interval."""
    policy = execution.get("retries") or {}
    if not isinstance(policy, Mapping) or not policy:
        return {}
    settings: Dict[str, Any] = {}
    strategy = str(policy.get("backoffStrategy") or "fixed").strip().lower()
    if strategy != "fixed" or policy.get("maxDelay"):
        refusals.append(
            _refusal(
                "retries",
                f"{where} execution.retries asks for {strategy} backoff"
                + (" with a maxDelay" if policy.get("maxDelay") else "")
                + "; a Databricks job retries after a fixed interval.",
                "Set backoffStrategy: fixed (or remove it) and drop maxDelay; initialDelay is "
                "the interval between attempts.",
            )
        )
    attempts = policy.get("maxAttempts")
    if isinstance(attempts, int) and attempts >= 1:
        # maxAttempts counts the first run; max_retries counts only the retries.
        settings["max_retries"] = attempts - 1
    if policy.get("initialDelay"):
        delay = _seconds(
            policy["initialDelay"], f"{where} execution.retries.initialDelay", refusals
        )
        if delay is not None:
            settings["min_retry_interval_millis"] = delay * 1000
    return settings


def _notifications(
    execution: Mapping[str, Any], where: str, refusals: List[UnsupportedBindingError]
) -> Dict[str, Any]:
    """Job email notifications from ``execution.notifications``."""
    on: Dict[str, List[str]] = {"on_success": [], "on_failure": []}
    for index, entry in enumerate(execution.get("notifications") or []):
        if not isinstance(entry, Mapping):
            continue
        kind = str(entry.get("type") or "").strip().lower()
        target = str(entry.get("target") or "").strip()
        if kind != "email" or not _EMAIL_RE.match(target):
            refusals.append(
                _refusal(
                    "notification",
                    f"{where} execution.notifications[{index}] is {kind or 'untyped'} to "
                    f"{target!r}; the Databricks job emitter maps email to an address only, "
                    "and the job would notify no one.",
                    "Use type email with an address, or configure the other channel as a "
                    "notification destination on the job in Databricks.",
                )
            )
            continue
        condition = str(entry.get("condition") or "failure").strip().lower()
        for key in ("on_success", "on_failure"):
            if condition == "always" or key == f"on_{condition}":
                if target not in on[key]:
                    on[key].append(target)
    found = {key: sorted(addresses) for key, addresses in on.items() if addresses}
    return {"email_notifications": found} if found else {}


def _runtime(
    runtime: Mapping[str, Any],
    where: str,
    refusals: List[UnsupportedBindingError],
    run_as_allowed: FrozenSet[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(job settings, task settings)`` from ``execution.runtime``."""
    job: Dict[str, Any] = {}
    task: Dict[str, Any] = {}
    resources = runtime.get("resources") if isinstance(runtime.get("resources"), Mapping) else {}
    unmapped = [
        name
        for name, value in (
            ("runtime.environment", runtime.get("environment")),
            ("runtime.image", runtime.get("image")),
            ("runtime.executor", runtime.get("executor")),
            ("runtime.resources.cpu", resources.get("cpu")),
            ("runtime.resources.memory", resources.get("memory")),
            ("runtime.resources.disk", resources.get("disk")),
        )
        if value
    ]
    if unmapped:
        refusals.append(
            _refusal(
                "runtime-unmapped",
                f"{where} execution sets {', '.join(unmapped)}, which a serverless Databricks "
                "dbt job does not take: it sizes itself, and its environment holds the dbt "
                "adapter only.",
                "Remove them from a build that runs on Databricks; read settings in the dbt "
                "project from a Databricks secret instead of an environment variable.",
            )
        )
    if runtime.get("timeout"):
        timeout = _seconds(runtime["timeout"], f"{where} execution.runtime.timeout", refusals)
        if timeout is not None:
            task["timeout_seconds"] = timeout
    run_as = _run_as(runtime, where, refusals, run_as_allowed)
    if run_as:
        job["run_as"] = run_as
    return job, task


def _run_as(
    runtime: Mapping[str, Any],
    where: str,
    refusals: List[UnsupportedBindingError],
    run_as_allowed: FrozenSet[str],
) -> Optional[Dict[str, str]]:
    """The job's ``run_as`` for ``execution.runtime.serviceAccount``, or ``None``
    (with a refusal unless it is unset): a service principal the operator lists."""
    account = str(runtime.get("serviceAccount") or "").strip()
    if not account:
        return None
    if not _APPLICATION_ID_RE.match(account):
        # A user is never taken from a contract: the identity that applies it is
        # often a workspace admin, who may set any user, so a contract author could
        # run their dbt code as anyone.
        refusals.append(
            _refusal(
                "run-as",
                f"{where} execution.runtime.serviceAccount is {account!r}, not a service "
                "principal's application id. A job runs as a service principal from a "
                "contract, never as a user.",
                "Set it to the service principal's application id (a UUID), or remove it "
                "to run the job as the identity that applies the contract.",
            )
        )
    elif account.lower() in run_as_allowed:
        return {"service_principal_name": account.lower()}
    else:
        # Nor a service principal the operator has not listed: the identity that
        # applies may hold Service Principal User on several, so a contract alone
        # could run its dbt code as a more privileged one.
        refusals.append(
            _refusal(
                "run-as-unlisted",
                f"{where} execution.runtime.serviceAccount names service principal "
                f"{account.lower()}, which {RUN_AS_ALLOWLIST_ENV} does not list: a job runs as "
                "a service principal only when the operator says so, never on the contract's "
                "word alone.",
                f"Where fluid apply runs, add it to {RUN_AS_ALLOWLIST_ENV} (application ids, "
                "comma-separated), or remove serviceAccount to run the job as the identity "
                "that applies the contract.",
            )
        )
    return None


def run_as_allowlist(env: Mapping[str, str]) -> FrozenSet[str]:
    """The service principals (application ids, lower-cased) ``RUN_AS_ALLOWLIST_ENV``
    lets a contract name as a job's run-as."""
    listed = str(env.get(RUN_AS_ALLOWLIST_ENV) or "").split(",")
    return frozenset(item.strip().lower() for item in listed if item.strip())


def _git_source(
    repository: str, where: str, refusals: List[UnsupportedBindingError]
) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
    """``(git_source, project directory in the repository)`` for a Git URL, or ``None``."""
    parts = urlsplit(repository)
    remedy = (
        "Name the repository as https://<host>/<owner>/<repo>?tag=<tag> (or ?branch= / "
        "?commit=), with &path=<dir> when the dbt project is not at its root."
    )
    if parts.scheme != "https" or not parts.hostname:
        refusals.append(
            _refusal(
                "git-url",
                f"{where} repository {repository!r} is not an https:// Git URL; Databricks "
                "clones a dbt project's repository over HTTPS.",
                remedy,
            )
        )
        return None
    if parts.username or parts.password:
        refusals.append(
            _refusal(
                "git-url",
                f"{where} repository carries a user or password in its URL, which the job "
                "settings would store in clear.",
                "Remove it from the URL, and give Databricks a Git credential (User Settings > "
                "Linked accounts, or databricks_git_credential).",
            )
        )
        return None
    host = parts.hostname.lower()
    provider = _GIT_PROVIDERS.get(host) or (
        "azureDevOpsServices" if host.endswith(".visualstudio.com") else None
    )
    if provider is None:
        refusals.append(
            _refusal(
                "git-host",
                f"{where} repository is on {host}, a Git host the Databricks job emitter does not "
                "map (github.com, gitlab.com, bitbucket.org, dev.azure.com).",
                "Use one of those hosts, or a local project directory, which is uploaded to "
                "the workspace.",
            )
        )
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    unknown = sorted(set(query) - set(_GIT_REFS) - {"path"})
    refs = [name for name in _GIT_REFS if name in query]
    values = {name: query[name] for name in query}
    if unknown or len(refs) != 1 or any(len(v) != 1 for v in values.values()):
        refusals.append(
            _refusal(
                "git-ref",
                f"{where} repository {repository!r} must name exactly one ref (branch, tag or "
                "commit), once"
                + (f"; {', '.join(unknown)} is not a repository setting" if unknown else "")
                + ". Databricks would otherwise run whatever the default branch holds.",
                remedy,
            )
        )
        return None
    ref, value = refs[0], values[refs[0]][0]
    valid = _COMMIT_RE.match(value) if ref == "commit" else _GIT_REF_RE.match(value)
    project = values.get("path", [None])[0]
    if not valid or (
        project is not None
        and (
            not _REPO_PATH_RE.match(project)
            or project.startswith("/")
            or ".." in PurePosixPath(project).parts
        )
    ):
        refusals.append(
            _refusal(
                "git-ref",
                f"{where} repository names {ref} {value!r}"
                + (f" and path {project!r}" if project is not None else "")
                + ", which is not a valid "
                + ("commit hash" if ref == "commit" else "Git ref")
                + (" or a relative directory" if project is not None else "")
                + ".",
                remedy,
            )
        )
        return None
    url = urlunsplit(("https", parts.netloc, parts.path, "", ""))
    return {"url": url, "provider": provider, ref: value}, project


def _project_files(
    project: Path, where: str, refusals: List[UnsupportedBindingError]
) -> List[Tuple[str, bytes]]:
    """``(path relative to the project, bytes)`` for every file dbt reads, sorted."""
    import yaml

    try:
        config = yaml.safe_load((project / "dbt_project.yml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        refusals.append(
            _refusal(
                "project-unreadable",
                f"{where} dbt_project.yml in {project} could not be read "
                f"({type(exc).__name__}).",
                "Fix it; dbt parse reads it the same way.",
            )
        )
        return []
    config = config if isinstance(config, Mapping) else {}
    found: Dict[str, Path] = {}
    links: List[str] = []
    for name in _PROJECT_FILES:
        path = project / name
        if path.is_symlink():
            links.append(name)
        elif path.is_file():
            found[name] = path
    for key, default in _PATH_KEYS.items():
        entries = config.get(key, list(default))
        for entry in [entries] if isinstance(entries, str) else entries or []:
            rel = PurePosixPath(str(entry))
            problem = _path_key_problem(project, rel)
            if problem is not None:
                refusals.append(
                    _refusal(
                        "project-path",
                        f"{where} dbt_project.yml {key} names {str(entry)!r}, {problem}; only "
                        "the directories dbt reads, inside the project, are uploaded.",
                        "Name a plain directory inside the project, or use a GIT source.",
                    )
                )
                continue
            _walk(project, project / rel, found, links)
    for name in links:
        refusals.append(
            _refusal(
                "project-link",
                f"{where} {name} in the dbt project is a symbolic link; the upload follows no "
                "link, so it could not reach a file outside the project.",
                "Replace the link with the file itself, or use a GIT source.",
            )
        )
    files = []
    total = 0
    for rel in sorted(found):
        data = found[rel].read_bytes()
        if len(data) > MAX_FILE_BYTES:
            refusals.append(
                _refusal(
                    "file-size",
                    f"{where} {rel} is {len(data)} bytes; a workspace file holds "
                    f"{MAX_FILE_BYTES} at most here.",
                    "Load data that size as a table, not a dbt seed; or use a GIT source.",
                )
            )
            continue
        total += len(data)
        files.append((rel, data))
    if total > MAX_PROJECT_BYTES or len(files) > MAX_PROJECT_FILES:
        refusals.append(
            _refusal(
                "project-size",
                f"{where} the dbt project uploads {len(files)} files, {total} bytes; the "
                f"workspace upload takes {MAX_PROJECT_FILES} files and {MAX_PROJECT_BYTES} "
                "bytes at most, since each is a resource in the module.",
                "Use a GIT source for a project this size.",
            )
        )
    return files


def _path_key_problem(project: Path, rel: PurePosixPath) -> Optional[str]:
    """Why a ``*-paths`` entry is not a plain directory inside the project, or ``None``.

    Every component is checked, not just the last: ``lnk/leak`` with ``lnk`` a
    link would walk wherever ``lnk`` points, and ``.git`` holds the checkout's
    credentials (``actions/checkout`` persists a token in ``.git/config``).
    """
    if rel.is_absolute() or ".." in rel.parts:
        return "outside the project"
    if not rel.parts:
        return "the project root, which would upload everything in it"
    hidden = [part for part in rel.parts if part.startswith(".")]
    if hidden:
        return f"the hidden directory {hidden[0]!r}"
    for depth in range(1, len(rel.parts) + 1):
        if project.joinpath(*rel.parts[:depth]).is_symlink():
            return f"through the symbolic link {'/'.join(rel.parts[:depth])!r}"
    return None


def _walk(project: Path, base: Path, found: Dict[str, Path], links: List[str]) -> None:
    """Add every regular, visible file under ``base``; collect the links met.

    A file is added only if it resolves inside the project, whatever the walk
    met on the way (:func:`_path_key_problem` checks the entry itself first).
    """
    root = project.resolve()
    if base.is_symlink():
        links.append(base.relative_to(project).as_posix())
        return
    if not base.is_dir():
        return  # dbt tolerates a missing default directory
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames):
            if name.startswith(".") or name == "__pycache__":
                dirnames.remove(name)
            elif (here / name).is_symlink():
                links.append((here / name).relative_to(project).as_posix())
                dirnames.remove(name)
        for name in filenames:
            path = here / name
            if name.startswith(".") or name == "profiles.yml":
                continue
            rel = path.relative_to(project).as_posix()
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                links.append(rel)
            elif path.is_file():
                found[rel] = path


def _commands(build: Mapping[str, Any], where: str, refusals: List[UnsupportedBindingError]):
    """``dbt deps``, then ``dbt build`` with the local runner's selectors and vars."""
    from ...build_runners.dbt.runner import build_selectors

    command = ["dbt", "build"]
    selectors = build_selectors(dict(build))
    if selectors:
        command += ["--select", *selectors]
    props = build.get("properties") or {}
    dbt_vars = props.get("vars") if isinstance(props, Mapping) else None
    if dbt_vars:
        if ENV_PLACEHOLDER_RE.search(json.dumps(dbt_vars)):
            refusals.append(
                _refusal(
                    "vars-template",
                    f"{where} properties.vars holds an {{{{ env.* }}}} template. The local "
                    "runner fills it from its environment; the job's settings would store the "
                    "value in clear, readable by everyone who can view the job.",
                    "Read the value in the dbt project instead (a Databricks secret), or set "
                    "a literal that is not a secret.",
                )
            )
        else:
            command += ["--vars", json.dumps(dbt_vars, sort_keys=True, separators=(",", ":"))]
    return ["dbt deps", shlex.join(command)]


def dbt_written_exposes(contract: Mapping[str, Any], tables: Sequence[Any]) -> Set[str]:
    """``expose_id`` of each table a dbt build writes, run locally or as a job.

    dbt's ``table`` materialization rebuilds such a table with CREATE OR REPLACE,
    so the module leaves its definition to dbt (``databricks.py``).
    """
    builds = [b for b in contract.get("builds") or [] if isinstance(b, Mapping)]
    written: Set[str] = set()
    for table in tables:
        for build in builds_writing_expose(table.exposure.get("exposeId"), builds):
            engine = str(build.get("engine") or "").strip().lower()
            if engine == "dbt" or engine.startswith("dbt-"):
                written.add(table.expose_id)
    return written


@dataclass(frozen=True)
class _Build:
    """One Databricks dbt build being compiled, and what its job is named after."""

    build: Mapping[str, Any]
    build_id: str
    where: str
    product: str
    warehouse: str
    run_as_allowed: FrozenSet[str] = frozenset()

    @property
    def key(self) -> str:
        return unique_ident(f"{safe_ident(self.product)}_{self.build_id}")

    @property
    def name(self) -> str:
        return f"fluid {self.product} {self.build_id}"

    @property
    def root(self) -> str:
        """Its uploaded project, below the applying identity's home: one folder per
        product and build, never shared. Each id carries its digest
        (``digest_ident``), since one identity may apply many contracts into one
        home: ``sales.orders`` and ``sales-orders`` fold alike, and an id written
        as another's folded form would otherwise land in its folder, where the
        upload overwrites that product's models (the provider never reads a
        file's content back, so its next plan would not notice)."""
        return f"/.fluid/{digest_ident(self.product)}/{digest_ident(self.build_id)}/files"


@dataclass(frozen=True)
class _Settings:
    """What the build's ``execution`` and ``properties`` make of its job."""

    trigger: str
    job: Dict[str, Any]
    task: Dict[str, Any]
    commands: List[str]


@dataclass(frozen=True)
class _Source:
    """Where the job's dbt project comes from: a Git ref, or uploaded files."""

    git: Optional[Dict[str, Any]] = None
    directory: Optional[str] = None
    files: Tuple[Tuple[str, bytes], ...] = ()


def compile_dbt_jobs(
    contract: Mapping[str, Any],
    tables: Sequence[Any],
    warehouse: str,
    *,
    validating: bool = False,
    run_as_allowed: FrozenSet[str] = frozenset(),
) -> CompiledJobs:
    """Every Databricks dbt job of ``contract``, with its workspace files and refusals.

    ``tables`` are the resolved Databricks tables (``databricks.py::_Table``): a
    job runs dbt against the catalog and schema of the first one its build
    writes. ``fluid validate`` compiles with ``validating`` and no contract
    location may be known; the project files are then not read.
    """
    compiled = CompiledJobs()
    builds = [b for b in contract.get("builds") or [] if isinstance(b, Mapping)]
    product = str(contract.get("id") or contract.get("name") or "product")
    for index, build in enumerate(contract.get("builds") or []):
        if not isinstance(build, Mapping) or not is_databricks_job_build(build):
            continue
        build_id = str(build.get("id") or index)
        item = _Build(
            build, build_id, f"builds[{index}] ({build_id})", product, warehouse, run_as_allowed
        )
        before = len(compiled.refusals)
        settings = _job_settings(item, compiled)
        table = _target_table(item, builds, tables, compiled.refusals)
        source = _project_source(item, compiled.refusals, validating=validating)
        if len(compiled.refusals) == before and table is not None and source is not None:
            _emit_job(compiled, item, table, settings, source)
    return compiled


def _job_settings(item: _Build, compiled: CompiledJobs) -> _Settings:
    build, where, refusals = item.build, item.where, compiled.refusals
    engine = str(build.get("engine") or "").strip().lower()
    if engine not in ("dbt", "dbt-databricks"):
        refusals.append(
            _refusal(
                "adapter",
                f"{where} is engine {engine!r} with runtime.platform databricks; a Databricks "
                "job runs dbt with the dbt-databricks adapter.",
                "Use engine dbt (or dbt-databricks), or a runtime.platform for that adapter.",
            )
        )
    execution = build.get("execution") if isinstance(build.get("execution"), Mapping) else {}
    runtime = execution.get("runtime") if isinstance(execution.get("runtime"), Mapping) else {}
    trigger, schedule = _trigger(execution, where, refusals)
    run_job, run_task = _runtime(runtime, where, refusals, item.run_as_allowed)
    props = build.get("properties") if isinstance(build.get("properties"), Mapping) else {}
    if props.get("target"):
        compiled.warnings.append(
            f"{where}: properties.target {props['target']!r} applies to local dbt runs only; "
            "the Databricks job runs dbt with the profile Databricks generates from "
            "DATABRICKS_WAREHOUSE_ID and the expose's catalog and schema."
        )
    return _Settings(
        trigger=trigger,
        job={**schedule, **_notifications(execution, where, refusals), **run_job},
        task={**_retries(execution, where, refusals), **run_task},
        commands=_commands(build, where, refusals),
    )


def _target_table(
    item: _Build,
    builds: Sequence[Mapping[str, Any]],
    tables: Sequence[Any],
    refusals: List[UnsupportedBindingError],
):
    """The first Databricks table the build writes; ``None`` and a refusal without one."""
    for table in tables:
        if any(
            b is item.build for b in builds_writing_expose(table.exposure.get("exposeId"), builds)
        ):
            return table
    refusals.append(
        _refusal(
            "no-target",
            f"{item.where} runs on Databricks but writes no Databricks expose, so the job has "
            "no catalog and schema to run dbt in.",
            "List the Databricks expose(s) it writes in builds[].outputs.",
        )
    )
    return None


def _project_source(
    item: _Build, refusals: List[UnsupportedBindingError], *, validating: bool
) -> Optional[_Source]:
    """The build's ``repository`` as a Git source or the files to upload; ``None`` if refused."""
    repository = str(item.build.get("repository") or "").strip()
    if not repository:
        refusals.append(
            _refusal(
                "repository-missing",
                f"{item.where} names no repository, so the job has no dbt project to run.",
                "Set builds[].repository to the dbt project directory (it is uploaded to the "
                "workspace) or to its Git URL with a ref (?tag=, ?branch= or ?commit=).",
            )
        )
        return None
    if "://" in repository or repository.startswith("git@"):
        git = _git_source(repository, item.where, refusals)
        return None if git is None else _Source(git=git[0], directory=git[1])
    source = current_contract_source()
    if source is None:
        if validating:
            return _Source()  # fluid validate without a file: nothing to read yet
        refusals.append(
            _refusal(
                "source-unknown",
                f"{item.where} uploads a local dbt project, and the contract's location is not "
                "known here, so the project cannot be found.",
                "Run it through fluid apply / fluid generate iac with the contract's path.",
            )
        )
        return None
    from ...build_runners.dbt.runner import resolve_dbt_project_path

    project = resolve_dbt_project_path(Path(source), dict(item.build))
    if project is None:
        refusals.append(
            _refusal(
                "project-missing",
                f"{item.where} repository {repository!r} holds no dbt_project.yml (resolved "
                f"from {Path(source).parent}), or resolves outside the contract's workspace.",
                "Point repository at the dbt project directory, relative to the contract.",
            )
        )
        return None
    return _Source(files=tuple(_project_files(project, item.where, refusals)))


def _emit_job(
    compiled: CompiledJobs, item: _Build, table: Any, settings: _Settings, source: _Source
) -> None:
    dbt_task: Dict[str, Any] = {
        "commands": settings.commands,
        "warehouse_id": item.warehouse,
        "catalog": table.catalog,
        "schema": table.schema,
    }
    job: Dict[str, Any] = {
        "name": item.name,
        "max_concurrent_runs": 1,
        "tags": {"fluid_product": item.product, "fluid_build": item.build_id},
        "environment": [
            {
                "environment_key": "dbt",
                "spec": {
                    "environment_version": ENVIRONMENT_VERSION,
                    "dependencies": [DBT_DATABRICKS],
                },
            }
        ],
        **settings.job,
    }
    if source.git is not None:
        job["git_source"] = source.git
        dbt_task["source"] = "GIT"
        if source.directory:
            dbt_task["project_directory"] = source.directory
    else:
        dbt_task["source"] = "WORKSPACE"
        # A workspace path as the jobs service reads it (Asset Bundles' file_path form).
        dbt_task["project_directory"] = _under_home("/Workspace", item.root)
        depends = _emit_files(compiled, item, source.files)
        if depends:
            job["depends_on"] = depends
        compiled.data.setdefault(CURRENT_USER, {})["me"] = {}
    job["task"] = [
        {"task_key": "dbt", "environment_key": "dbt", "dbt_task": dbt_task, **settings.task}
    ]
    compiled.resources.append(("databricks_job", item.key, f"{item.where} job", job))
    compiled.jobs.append(
        DbtJob(
            build_id=item.build_id,
            name=item.name,
            address=f"databricks_job.{item.key}",
            source=dbt_task["source"],
            trigger=settings.trigger,
            files=tuple(rel for rel, _ in source.files),
            root=item.root,
        )
    )


def _emit_files(
    compiled: CompiledJobs, item: _Build, files: Sequence[Tuple[str, bytes]]
) -> List[str]:
    """One ``databricks_workspace_file`` per project file, each in a
    ``databricks_directory`` created before it; the files' addresses.

    The provider creates a file's missing parent folder only when, after the upload
    failed, the parent is still missing (``resource_workspace_file.go``, 1.140.0;
    databricks/terraform-provider-databricks#6145).
    Files uploading in parallel into a new tree then fail at a first apply when a
    sibling's mkdirs made the parent in between (``models/x.sql`` creates ``files/``
    under ``dbt_project.yml``). With every folder there first, no upload meets a
    missing parent. Each folder waits on its parent folder, so a removal deletes them
    deepest first; none deletes recursively, so a folder holding anything else stays
    and says so.
    """
    folders: Dict[str, str] = {}

    def folder(rel_dir: str) -> str:
        if rel_dir not in folders:
            body: Dict[str, Any] = {
                "path": _under_home("", f"{item.root}/{rel_dir}" if rel_dir else item.root)
            }
            if rel_dir:
                body["depends_on"] = [folder(rel_dir.rpartition("/")[0])]
            key = f"{item.key}_dir_{unique_ident(rel_dir)}" if rel_dir else f"{item.key}_dir"
            compiled.resources.append(
                ("databricks_directory", key, f"{item.where} folder {rel_dir or '.'}", body)
            )
            folders[rel_dir] = f"databricks_directory.{key}"
        return folders[rel_dir]

    depends: List[str] = []
    for rel, data in files:
        key = f"{item.key}_{unique_ident(rel)}"
        compiled.resources.append(
            (
                "databricks_workspace_file",
                key,
                f"{item.where} file {rel}",
                {
                    "path": _under_home("", f"{item.root}/{rel}"),
                    "content_base64": base64.b64encode(data).decode("ascii"),
                    "depends_on": [folder(rel.rpartition("/")[0])],
                },
            )
        )
        depends.append(f"databricks_workspace_file.{key}")
    return depends


__all__ = [
    "CompiledJobs",
    "DbtJob",
    "compile_dbt_jobs",
    "dbt_written_exposes",
]
