# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Spark runs of ``runtime: local``: spark-submit on the machine ``fluid apply`` runs on.

A local run is what an EMR run is, on a laptop or a lab host (an EMR 7.x image, or any
Spark 3.5):

- **Artifacts.** The job config and the content-addressed runner, in the layout OpenTofu
  uploads on AWS (``_fluid/spark/<product>/<build>/``, :mod:`fluid_build.spark.names`),
  under the operator's :data:`LOCAL_ROOT_ENV`: an ``s3://`` bucket the AWS SDK reaches
  through its own endpoint settings (``AWS_ENDPOINT_URL_S3`` names SeaweedFS or MinIO),
  or a directory. Nothing else writes them locally, so forge writes them when it starts a
  run; spark-submit reads them from there, as EMR does.
- **Submission.** ``spark-submit`` (:data:`SUBMIT_EXECUTABLE_ENV`) with the operator's
  Spark properties file (:data:`PROPERTIES_FILE_ENV`: the catalog, the S3 store, their
  credentials, the master), as a list argv, never through a shell. forge adds the settings
  its job needs (:func:`merge_conf`), as an EMR run adds them to its cluster's.
- **State.** One directory per run under ``.fluid/<product>/runtime/spark/<build>/runs/``
  beside the contract. A supervisor (``_local_supervisor.py``, run with ``python -I -S``)
  waits for spark-submit and records how it ended. An exclusive ``flock`` the run's
  processes hold says whether the run is still going; a reused pid cannot fake it.
- **The runner runs from the run's own directory.** spark-submit runs the runner as a
  script, so its directory is first on the driver's ``sys.path``: run from the project's
  tree, a ``json.py`` placed beside it would be imported in its place. So a directory root's
  runner is pulled, its sha256 checked, into the run's directory, which forge has just
  created under a name nobody can guess (an ``s3://`` root's is downloaded by spark-submit
  into a directory of its own), and the driver runs with ``PYTHONSAFEPATH`` set. Every
  path under ``.fluid`` must resolve inside it: a link out of it is refused.

Every variable here is the operator's: a project's dotenv file cannot set one
(``credentials/protected_env.py``). POSIX only, for ``fcntl.flock``.

Borrowed: the argv shape of Apache Airflow's ``SparkSubmitHook`` (Apache-2.0); the
liveness probe of tox-dev's ``filelock`` (``LOCK_EX | LOCK_NB`` on a lock its holder
keeps open); the ``.properties`` reading rules of jwodder's ``javaproperties`` (MIT),
which follow ``java.util.Properties.load``, as spark-submit reads ``--properties-file``
(Spark 3.5 ``Utils.getPropertiesFromFile``, then ``trimExceptCRLF``).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from fluid_build.build_runners.spark._adapters import (
    CANCELLED,
    CANCELLING,
    FAILED,
    PENDING,
    RUNNING,
    SUCCEEDED,
    RunInfo,
    SparkRunError,
    failed_release_uri,
    release_config_uri,
    stop_request_body,
    stop_request_uri,
)
from fluid_build.build_runners.spark._desired import (
    RESTART_WINDOW_S,
    STOP_KEY_FLAG,
    DesiredRun,
    config_digest,
)
from fluid_build.spark.names import RUNNER_FILE

#: Operator-only: the spark-submit to run, an absolute path or a name on ``PATH``.
#: Unset: ``spark-submit`` on ``PATH``.
SUBMIT_EXECUTABLE_ENV = "FLUID_SPARK_SUBMIT_EXECUTABLE"
#: Operator-only: a Spark properties file (spark-defaults.conf syntax) passed to every
#: local run as ``--properties-file``: the catalog the job writes (Lakekeeper's
#: ``spark.sql.catalog.fluid.*``), the S3 store, their credentials, the master. Its values
#: never go into the command line.
PROPERTIES_FILE_ENV = "FLUID_SPARK_SUBMIT_PROPERTIES_FILE"
#: Operator-only: where local runs' artifacts live, ``s3://<bucket>[/<prefix>]`` or an
#: absolute directory. Unset: ``.fluid/spark-artifacts`` beside the contract. spark-submit
#: and the runner read an ``s3://`` root through Hadoop: on a Spark other than an EMR image
#: the properties file maps the scheme (``spark.hadoop.fs.s3.impl`` to S3A, with its
#: endpoint), and spark-submit, which downloads the runner before ``--packages`` load, needs
#: hadoop-aws on its own classpath (``spark.driver.extraClassPath``).
LOCAL_ROOT_ENV = "FLUID_SPARK_LOCAL_ROOT"
DEFAULT_ROOT = Path(".fluid") / "spark-artifacts"
#: Ended runs kept per build, their logs included; older ones are removed.
KEEP_ENDED = 10
#: How long a cancelled run has to stop after SIGTERM before its processes are killed.
STOP_GRACE_S = 60.0
#: How long a start waits for the supervisor to say spark-submit has started.
START_WAIT_S = 10.0
SUPERVISOR = Path(__file__).with_name("_local_supervisor.py")
RUN_FILE, STATUS_FILE, CANCEL_FILE, LOCK_FILE, LOG_FILE = (
    "run.json",
    "status.json",
    "cancel",
    "lock",
    "driver.log",
)
#: forge's settings that spark-submit would let a file's value replace whole: forge
#: passes the union with the file's (a Maven coordinate once, the file's version first).
_UNION_KEYS = ("spark.jars.packages", "spark.sql.extensions")
#: forge's settings the file's value replaces: the image's Iceberg runtime, a mirror.
_FILE_KEYS = ("spark.jars", "spark.jars.repositories")
_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
#: The names a run's environment keeps: a POSIX shell variable's. spark-submit is a bash
#: script, so a name bash would read as an exported function (``BASH_FUNC_<name>%%``) or
#: any name no shell could have set is dropped before it reaches bash.
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: What makes bash run code or trace itself as it starts: a script to source
#: (``BASH_ENV``, ``ENV``), options (``SHELLOPTS``, ``BASHOPTS``: xtrace among them) and
#: the trace's prompt and descriptor (``PS4`` expands command substitutions).
_SHELL_STARTUP_ENV = frozenset({"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4", "BASH_XTRACEFD"})
#: Spark's redaction pattern (``spark.redaction.regex``) as of Spark 3.5 and 4.0
#: (``core/.../internal/config/package.scala``): Spark 3.4 and earlier lack
#: ``credential``, which an Iceberg REST catalog's ``spark.sql.catalog.<c>.credential``
#: names, so forge passes it unless the operator's file sets its own.
REDACTION_REGEX = "(?i)secret|password|token|access[.]?key|credential"
_S3_ROOT = re.compile(r"s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](?:/[A-Za-z0-9._=/-]*)?")


def require_posix() -> None:
    """Raise unless this system has ``fcntl.flock``, which tracks a local run."""
    try:
        import fcntl  # noqa: F401
    except ImportError as exc:
        raise SparkRunError(
            "runtime local runs on Linux or macOS: a local run is tracked with fcntl.flock, "
            "which this system lacks"
        ) from exc


def _confined(workspace: Path, path: Path) -> Path:
    """``path`` resolved, if it stays inside the workspace's ``.fluid`` directory once every
    link is followed (``FileStateStore._confine``'s rule for the build runners' state).

    Raises:
        SparkRunError: it resolves outside, through a link the project holds.
    """
    fluid = Path(workspace) / ".fluid"
    if fluid.is_symlink():
        raise SparkRunError(
            f"{fluid} is a link: local Spark runs keep their state in a .fluid directory of "
            "the project's own; replace the link with a directory"
        )
    base = fluid.resolve()
    resolved = Path(path).resolve()
    if resolved != base and base not in resolved.parents:
        raise SparkRunError(
            f"{path} resolves to {resolved}, outside {base}: a link under .fluid points out "
            "of the project; remove it"
        )
    return resolved


@dataclass(frozen=True)
class LocalHost:
    """Where local runs live: the contract's directory, and the operator's environment."""

    workspace: Path
    env: Mapping[str, str] = field(default_factory=lambda: os.environ, repr=False)

    def runtime_dir(self, product: str) -> Path:
        return Path(self.workspace) / ".fluid" / product / "runtime" / "spark"


@dataclass(frozen=True)
class Submitter:
    """What the operator's environment says about local runs (:func:`submitter`)."""

    executable: str
    properties_file: Optional[Path]
    properties: Mapping[str, str]
    #: ``s3://bucket[/prefix]``, or an absolute directory, where the runs' artifacts live
    #: (spark-submit and the runner read a plain path as a local file).
    root: str
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: The workspace whose ``.fluid`` holds the default root: every artifact written
    #: there must resolve inside it. ``None`` for a root the operator names.
    confine: Optional[Path] = None

    @property
    def on_s3(self) -> bool:
        return self.root.startswith("s3://")


def submitter(host: LocalHost) -> Submitter:
    """The operator's spark-submit, properties file and artifact root, checked.

    Raises:
        SparkRunError: one of them is unusable; the message names the variable.
    """
    env = host.env
    path = _properties_path(env)
    return Submitter(
        executable=_executable(env, Path(host.workspace)),
        properties_file=path,
        properties=_read_properties(path),
        root=_root(env, host.workspace),
        env=env,
        confine=None if (env.get(LOCAL_ROOT_ENV) or "").strip() else Path(host.workspace),
    )


def _executable(env: Mapping[str, str], workspace: Path) -> str:
    """The spark-submit to run: an absolute path to an executable file, or a name on
    ``PATH``, as ``DBT_EXECUTABLE`` is resolved for the dbt runner. One program, never a
    command line, never a relative path, and never one inside the project (the project
    would choose what runs): the path, links followed, must be outside ``workspace``."""
    configured = (env.get(SUBMIT_EXECUTABLE_ENV) or "").strip() or "spark-submit"
    if os.path.isabs(configured):
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return _outside(configured, workspace)
        raise SparkRunError(
            f"{SUBMIT_EXECUTABLE_ENV} names {configured}, which is not an executable file"
        )
    if os.sep in configured or configured.startswith((".", "~")):
        raise SparkRunError(
            f"{SUBMIT_EXECUTABLE_ENV} must be an absolute path or a name on PATH, not a "
            "relative path"
        )
    found = shutil.which(configured, path=env.get("PATH"))
    if not found:
        raise SparkRunError(
            f"{configured} is not on PATH: install Spark 3.5 (or use an EMR 7.x image) and "
            f"put spark-submit on PATH, or set {SUBMIT_EXECUTABLE_ENV} to its absolute path"
        )
    if not os.path.isabs(found):
        raise SparkRunError(
            f"{configured} was found through a relative PATH entry ({found}); set "
            f"{SUBMIT_EXECUTABLE_ENV} to its absolute path"
        )
    return _outside(found, workspace)


def _outside(executable: str, workspace: Path) -> str:
    """``executable`` unless it is inside the project: its path as named, or as resolved
    with links followed. A link the project holds (``proj/bin/spark-submit -> /bin/sh``)
    is the project's choice, wherever it points."""
    named, project = Path(executable).absolute(), Path(workspace).resolve()
    resolved = named.resolve()
    through = any(p.resolve() == project for p in (named, *named.parents))
    if through or resolved == project or project in resolved.parents:
        raise SparkRunError(
            f"the spark-submit to run ({executable}) is inside the project ({project}), "
            f"which would choose the program forge starts; set {SUBMIT_EXECUTABLE_ENV} to "
            "a spark-submit outside it"
        )
    return executable


def _properties_path(env: Mapping[str, str]) -> Optional[Path]:
    raw = (env.get(PROPERTIES_FILE_ENV) or "").strip()
    if not raw:
        return None
    if not os.path.isabs(raw):
        raise SparkRunError(f"{PROPERTIES_FILE_ENV} must be an absolute path")
    if not os.path.isfile(raw):
        raise SparkRunError(f"{PROPERTIES_FILE_ENV} names {raw}, which is not a file")
    return Path(raw)


def _read_properties(path: Optional[Path]) -> Dict[str, str]:
    if path is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) else "it is not UTF-8"
        raise SparkRunError(f"{PROPERTIES_FILE_ENV}: {path} cannot be read ({reason})") from exc
    try:
        return parse_properties(text)
    except ValueError as exc:
        raise SparkRunError(f"{PROPERTIES_FILE_ENV}: {path}: {exc}") from None


def _root(env: Mapping[str, str], workspace: Path) -> str:
    raw = (env.get(LOCAL_ROOT_ENV) or "").strip()
    if not raw:
        # Inside the project: it must stay there (a link out of it would send forge's writes
        # where the project points them).
        return str(_confined(Path(workspace), Path(workspace) / DEFAULT_ROOT))
    if raw.startswith("s3://"):
        if not _S3_ROOT.fullmatch(raw) or ".." in raw or "//" in raw[5:]:
            raise SparkRunError(
                f"{LOCAL_ROOT_ENV}: expected s3://<bucket>[/<prefix>] with a prefix of letters, "
                "digits and . _ = / -"
            )
        return raw.rstrip("/")
    if not os.path.isabs(raw) or "\x00" in raw:
        raise SparkRunError(
            f"{LOCAL_ROOT_ENV}: expected s3://<bucket>[/<prefix>] or an absolute path"
        )
    return str(Path(raw).resolve())


# --- the operator's properties file --------------------------------------------------

_WHITESPACE = " \t\f"
_ESCAPES = {"t": "\t", "n": "\n", "f": "\f", "r": "\r"}


def parse_properties(text: str) -> Dict[str, str]:
    """A ``.properties`` file as ``java.util.Properties.load`` reads it, then each value
    trimmed as spark-submit trims it (``trimExceptCRLF``). Later keys win.

    Lines end at ``\\n``, ``\\r`` or ``\\r\\n``; a line ending in an odd number of
    backslashes goes on to the next, whose leading blanks are dropped; a line whose first
    non-blank character is ``#`` or ``!`` is a comment. The key ends at the first
    unescaped ``=``, ``:`` or blank, then blanks, one ``=`` or ``:`` and blanks are
    skipped. ``\\t \\n \\f \\r \\uXXXX`` are escapes, and ``\\`` before any other character
    is that character.

    Raises:
        ValueError: a malformed ``\\uXXXX``, naming the line, never a value.
    """
    lines = re.split(r"\r\n|\r|\n", text)
    found: Dict[str, str] = {}
    index = 0
    while index < len(lines):
        number = index + 1
        line = lines[index].lstrip(_WHITESPACE)
        index += 1
        if not line or line[0] in "#!":
            continue
        while _continues(line):
            line = line[:-1]
            if index >= len(lines):
                break
            line += lines[index].lstrip(_WHITESPACE)
            index += 1
        key, value = _split_entry(line)
        try:
            found[_unescape(key)] = _trim_except_crlf(_unescape(value))
        except ValueError:
            raise ValueError(f"line {number}: malformed \\uxxxx escape") from None
    return found


def _continues(line: str) -> bool:
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _split_entry(line: str) -> Tuple[str, str]:
    end = 0
    while end < len(line):
        char = line[end]
        if char == "\\":
            end += 2
            continue
        if char in "=:" or char in _WHITESPACE:
            break
        end += 1
    value = end
    while value < len(line) and line[value] in _WHITESPACE:
        value += 1
    if value < len(line) and line[value] in "=:":
        value += 1
    while value < len(line) and line[value] in _WHITESPACE:
        value += 1
    return line[:end], line[value:]


def _unescape(text: str) -> str:
    out: List[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        index += 1
        if char != "\\":
            out.append(char)
            continue
        if index >= len(text):
            break
        char = text[index]
        index += 1
        if char == "u":
            digits = text[index : index + 4]
            if not re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                raise ValueError("malformed \\uxxxx escape")
            out.append(chr(int(digits, 16)))
            index += 4
        else:
            out.append(_ESCAPES.get(char, char))
    # Java strings are UTF-16: a pair of \u escapes can spell one character.
    return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _trim_except_crlf(value: str) -> str:
    keep = [i for i, char in enumerate(value) if char > " " or char in "\r\n"]
    return value[keep[0] : keep[-1] + 1] if keep else ""


def merge_conf(forge: Mapping[str, str], operator: Mapping[str, str]) -> Dict[str, str]:
    """The ``--conf`` settings of a local run: forge's (:func:`DesiredRun.spark_conf`,
    the contract's allowed ``sparkConf`` among them) beside the operator's file.

    spark-submit takes a ``--conf`` over the file's value for the same key, whole, and
    merges no list (Spark 3.5 ``SparkSubmitArguments.mergeDefaultSparkProperties``). So a
    list both name is passed as their union, the file's first (:data:`_UNION_KEYS`); the
    file's ``spark.jars`` and ``spark.jars.repositories`` replace forge's (the image's
    Iceberg runtime, the operator's mirror), so forge passes neither then.
    """
    out = dict(forge)
    for key in _FILE_KEYS:
        if key in operator:
            out.pop(key, None)
    for key in _UNION_KEYS:
        if key in operator and key in out:
            out[key] = _union(operator[key], out[key], by_artifact=key == "spark.jars.packages")
    return out


def _union(first: str, second: str, *, by_artifact: bool) -> str:
    seen, items = set(), []
    for raw in (*first.split(","), *second.split(",")):
        item = raw.strip()
        name = ":".join(item.split(":")[:2]) if by_artifact else item
        if item and name not in seen:
            seen.add(name)
            items.append(item)
    return ",".join(items)


def submit_argv(
    sub: Submitter,
    run: DesiredRun,
    runner: Optional[str] = None,
    restart_of: Optional[str] = None,
    stop_key: Optional[str] = None,
) -> List[str]:
    """spark-submit's argv for ``run`` (bound to the artifact root): client mode, so the
    driver is the run's own process and stopping the run stops the job, as the EMR on EC2
    adapter submits a step. ``runner`` is the runner spark-submit runs (:func:`pull_runner`),
    else the one at the artifact root; ``restart_of`` marks a restart, and ``stop_key`` names
    a stream's stop request (:meth:`DesiredRun.entry_args`)."""
    argv = [sub.executable, "--name", run.run_name, "--deploy-mode", "client"]
    if sub.properties_file is not None:
        argv += ["--properties-file", str(sub.properties_file)]
    conf = merge_conf(run.spark_conf, sub.properties)
    if "spark.redaction.regex" not in sub.properties:
        conf["spark.redaction.regex"] = REDACTION_REGEX
    for name in sorted(conf):
        argv += ["--conf", f"{name}={conf[name]}"]
    return [*argv, runner or run.runner_uri, *run.entry_args(restart_of, stop_key=stop_key)]


def pull_runner(sub: Submitter, run: DesiredRun, where: Path) -> Optional[str]:
    """The runner spark-submit runs: a directory root's, read from the layout and checked
    against its sha256, written into the run's own directory ``where``; ``None`` for an
    ``s3://`` root, whose runner spark-submit downloads into a directory of its own.

    Raises:
        SparkRunError: the layout's runner is not the one the run was compiled with.
    """
    if sub.on_s3:
        return None
    try:
        body = Path(run.runner_uri).read_bytes()
    except OSError as exc:
        raise SparkRunError(f"{run.runner_uri} cannot be read: {exc.strerror}") from exc
    if hashlib.sha256(body).hexdigest() != run.runner_sha256:
        raise SparkRunError(
            f"{run.runner_uri} is not the runner this run was compiled with (its sha256 "
            "differs); run fluid apply again"
        )
    copy = where / RUNNER_FILE
    copy.write_bytes(body)
    return str(copy)


# --- artifacts -----------------------------------------------------------------------


def publish(sub: Submitter, run: DesiredRun, runner_text: str) -> None:
    """Write the run's job config and runner where spark-submit reads them (``run`` is
    bound to ``sub``'s root): the bytes OpenTofu uploads on AWS.

    Raises:
        SparkRunError: the runner is not the one ``run`` was compiled with, or a write
            failed (the message names the place, never a credential).
    """
    runner = runner_text.encode("utf-8")
    if hashlib.sha256(runner).hexdigest() != run.runner_sha256:
        raise SparkRunError(
            f"Spark build {run.build}: the runner changed after the run was compiled; "
            "run fluid apply again"
        )
    files: Tuple[Tuple[str, bytes], ...] = (
        (run.job_uri, run.job_text.encode("utf-8")),
        (run.runner_uri, runner),
    )
    if run.package_sha256 and run.package_uri:
        # Beside the job config, where the runner looks, as OpenTofu copies it on AWS.
        files += ((run.package_uri, local_package(run, sub.env)),)
    if sub.on_s3:
        _put_s3(files, sub.env)
        return
    for uri, body in files:
        _write_local_file(Path(uri), body, sub.confine)


def _write_local_file(path: Path, body: bytes, confine: Optional[Path]) -> None:
    """Write ``path`` under a directory root, unless it already holds ``body``; under the
    default root (``confine``, the workspace) no link may lead out of ``.fluid``.

    Raises:
        SparkRunError: it cannot be written, or a link leads out (the message names it).
    """
    if confine is not None:
        _confined(confine, path.parent)  # no link under the default root leads out
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if confine is not None:
            _confined(confine, path.parent)
        if path.is_file() and not path.is_symlink() and path.read_bytes() == body:
            return
        _replace(path, body)
    except OSError as exc:
        raise SparkRunError(f"{path} cannot be written: {exc.strerror}") from exc


def bound_job_uri(host: LocalHost, run: DesiredRun) -> str:
    """Where a run of ``run`` started on this machine now reads its job config: under the
    artifact root the operator's environment names (:data:`LOCAL_ROOT_ENV`)."""
    return run.bind(local_root=_root(host.env, Path(host.workspace))).job_uri


def write_stop_request(
    host: LocalHost, run: DesiredRun, key: str, beside: Optional[str] = None
) -> str:
    """Ask the local run started with stop key ``key`` to stop between batches: the request
    beside the job config it reads (``beside``, by default ``run``'s at this machine's artifact
    root, :data:`LOCAL_ROOT_ENV`), a file or an ``s3://`` object, where the runner looks
    (``_adapters.stop_request_uri``).

    Raises:
        SparkRunError: the root is unusable, or the request cannot be written.
    """
    uri = stop_request_uri(beside or bound_job_uri(host, run), key)
    put_stop_request(host, run, uri)
    return uri


def put_stop_request(host: LocalHost, run: DesiredRun, uri: str) -> None:
    """Write the stop request at ``uri`` (beside a job config at this machine's root)."""
    _write_artifact(host, uri, stop_request_body(run))


def _write_artifact(host: LocalHost, uri: str, body: bytes) -> None:
    """Write ``body`` at ``uri`` under this machine's artifact root (an ``s3://`` root or a
    directory; under the default root no link may lead out of ``.fluid``)."""
    env = host.env
    if _root(env, Path(host.workspace)).startswith("s3://"):
        _put_s3(((uri, body),), env)
    else:
        confine = None if (env.get(LOCAL_ROOT_ENV) or "").strip() else Path(host.workspace)
        _write_local_file(Path(uri), body, confine)


def keep_release(host: LocalHost, run: DesiredRun) -> str:
    """Keep the job config a local run of ``run`` reads as its release record
    (``_adapters.release_config_uri``): the bytes :func:`publish` wrote. Its URI.

    Raises:
        SparkRunError: the root is unusable, or the record cannot be written.
    """
    bound = run.bind(local_root=_root(host.env, Path(host.workspace)))
    uri = release_config_uri(bound.job_uri, run.job_sha256 or config_digest(bound.job_text))
    _write_artifact(host, uri, bound.job_text.encode("utf-8"))
    return uri


def read_release(host: LocalHost, run: DesiredRun, job_sha256: str) -> Optional[bytes]:
    """The job config the local release record of ``job_sha256`` holds, or None when there is
    none (the caller checks the bytes against the digest).

    Raises:
        SparkRunError: the record exists but cannot be read.
    """
    return _read_artifact(host, release_config_uri(bound_job_uri(host, run), job_sha256))


def _read_artifact(host: LocalHost, uri: str) -> Optional[bytes]:
    """The bytes at ``uri`` under this machine's artifact root, or None when there are none.

    Raises:
        SparkRunError: the artifact exists but cannot be read.
    """
    if uri.startswith("s3://"):
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        try:
            body: bytes = _s3_client(host.env).get_object(Bucket=bucket, Key=key)["Body"].read()
            return body
        except Exception as exc:  # noqa: BLE001 - botocore's errors; named, then raised
            code = str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code") or "")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise SparkRunError(f"{uri} cannot be read: {code or type(exc).__name__}") from exc
    path = Path(uri)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SparkRunError(f"{uri} cannot be read: {exc.strerror}") from exc


def keep_failed(host: LocalHost, run: DesiredRun, config_hash: str, body: bytes) -> str:
    """Mark the config with ``config_hash`` as one that failed its probation, beside the job
    config at this machine's artifact root (``_adapters.failed_release_uri``). Its URI.

    Raises:
        SparkRunError: the root is unusable, or the marker cannot be written.
    """
    uri = failed_release_uri(bound_job_uri(host, run), config_hash)
    _write_artifact(host, uri, body)
    return uri


def read_failed(host: LocalHost, run: DesiredRun, config_hash: str) -> Optional[bytes]:
    """The failed-probation marker of ``config_hash``, or None when there is none.

    Raises:
        SparkRunError: the marker exists but cannot be read.
        MarkerReadDenied: an ``s3://`` root refuses the marker and the job config beside it.
    """
    from fluid_build.build_runners.spark._adapters import MarkerReadDenied, warn_no_list_bucket

    job_uri = bound_job_uri(host, run)
    uri = failed_release_uri(job_uri, config_hash)
    try:
        return _read_artifact(host, uri)
    except SparkRunError as exc:
        if not (uri.startswith("s3://") and "AccessDenied" in str(exc)):
            raise
        # Without s3:ListBucket a missing key answers AccessDenied: the job config beside it
        # reads when the marker is only missing.
        try:
            probe = _read_artifact(host, job_uri)
        except SparkRunError:
            probe = None
        if probe is None:
            raise MarkerReadDenied(
                f"the marker {uri} and the job config {job_uri} cannot be read: the identity "
                "that reconciles needs s3:GetObject on the build's prefix, and s3:ListBucket on "
                "it so a missing object reads as missing"
            ) from exc
        warn_no_list_bucket(uri)
        return None


def clear_failed(host: LocalHost, run: DesiredRun, config_hash: str) -> None:
    """Remove a failed-probation marker. Best effort: one left behind only asks for the
    operator's go-ahead again."""
    _remove_artifact(
        host, failed_release_uri(bound_job_uri(host, run), config_hash), "spark_marker_left"
    )


def remove_stop_request(host: LocalHost, uri: str) -> None:
    """Delete a stop request :func:`write_stop_request` wrote. Best effort: one left behind
    stops no other run, since its key is that run's own."""
    _remove_artifact(host, uri, "spark_stop_request_left")


def _remove_artifact(host: LocalHost, uri: str, event: str) -> None:
    try:
        if uri.startswith("s3://"):
            bucket, _, key = uri.removeprefix("s3://").partition("/")
            _s3_client(host.env).delete_object(Bucket=bucket, Key=key)
        else:
            Path(uri).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - best effort
        logging.getLogger(__name__).warning(event, extra={"uri": uri, "error": type(exc).__name__})


def local_package(run: DesiredRun, env: Mapping[str, str]) -> bytes:
    """A local package build's package: the file its ``file://`` URI names, once the file it
    resolves to sits under a place :data:`~fluid_build.spark.package.PACKAGE_ALLOWLIST_ENV`
    lists and its sha256 is the pin (the runner checks it again before importing it).

    Raises:
        SparkRunError: the file is missing, unlisted, too large, or other bytes.
    """
    from fluid_build.spark.package import MAX_PACKAGE_BYTES, PACKAGE_ALLOWLIST_ENV, listed

    where = f"Spark build {run.build}: properties.package.uri"
    path = Path(str(run.package_source or "").removeprefix("file://"))
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        raise SparkRunError(f"{where}: {path} cannot be read: {reason}") from exc
    if not listed(f"file://{real}", env):
        raise SparkRunError(
            f"{where}: {real} is not under a place {PACKAGE_ALLOWLIST_ENV} lists (a link counts "
            "as the file it names); the operator lists the folders local packages come from"
        )
    if not real.is_file():
        raise SparkRunError(f"{where}: {real} is not a file")
    if real.stat().st_size > MAX_PACKAGE_BYTES:
        raise SparkRunError(f"{where}: {real} is larger than {MAX_PACKAGE_BYTES} bytes")
    try:
        body = real.read_bytes()
    except OSError as exc:
        raise SparkRunError(f"{where}: {real} cannot be read: {exc.strerror}") from exc
    found = hashlib.sha256(body).hexdigest()
    if found != run.package_sha256:
        raise SparkRunError(
            f"{where}: {path} is not the pinned package (its sha256 is {found}, the pin "
            f"{run.package_sha256}); build the pinned version, or pin this one"
        )
    return body


def _replace(path: Path, body: bytes) -> None:
    """Write ``path`` whole through a temporary file nobody can have placed: a new name,
    created exclusively and never through a link, then renamed over ``path`` (a rename
    replaces a link at ``path`` itself, never its target)."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    # Owner-only: the run's processes are this user's, and nothing else reads these files.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best effort: the write's own error, raised next, is the one that matters
        raise


def _s3_client(env: Mapping[str, str]) -> Any:
    """A boto3 S3 client (the ``[aws]`` extra) for an ``s3://`` root: its own endpoint
    settings (``AWS_ENDPOINT_URL_S3``, ``AWS_ENDPOINT_URL``) reach SeaweedFS or MinIO, which
    want path-style requests, and ignore the region the SDK insists on. The client is the
    Spark adapters' (``_adapters.default_clients``), so boto3 stays their one lazy import
    (``.importlinter-external``)."""
    missing = (
        f"{LOCAL_ROOT_ENV} is an s3:// root, and uploading to it needs boto3: "
        "pip install 'data-product-forge[aws]'"
    )
    try:
        from botocore.config import Config
    except ImportError as exc:
        raise SparkRunError(missing) from exc

    from fluid_build.build_runners.spark import _adapters

    custom = any(env.get(v) for v in ("AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL"))
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1"
    # Bounded: an endpoint that is down fails the apply in seconds, not after botocore's
    # default 60 s timeouts and five legacy attempts.
    config = Config(
        connect_timeout=10,
        read_timeout=60,
        retries={"mode": "standard", "max_attempts": 3},
        **({"s3": {"addressing_style": "path"}} if custom else {}),
    )
    try:
        client = _adapters.default_clients("s3", region, config=config)
    except SparkRunError as exc:  # boto3 itself is missing
        raise SparkRunError(missing) from exc
    except Exception as exc:  # noqa: BLE001 - botocore's errors; named, then raised
        raise SparkRunError(
            f"{LOCAL_ROOT_ENV} is an s3:// root, and no S3 client could be made for it: "
            f"{type(exc).__name__}"
        ) from exc
    return client


def _put_s3(files: Any, env: Mapping[str, str]) -> None:
    """Upload ``files`` (``(uri, body)`` pairs) to an ``s3://`` root (:func:`_s3_client`)."""
    client = _s3_client(env)
    for uri, body in files:
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        try:
            client.put_object(Bucket=bucket, Key=key, Body=body)
        except Exception as exc:  # noqa: BLE001 - botocore's errors; named, then raised
            code = str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code") or "")
            raise SparkRunError(
                f"s3://{bucket}/{key} cannot be written: {code or type(exc).__name__}"
            ) from exc


# --- runs ----------------------------------------------------------------------------


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    _replace(path, (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _held(lock: Path) -> bool:
    """Whether a process holds the run's lock: try it without waiting, and let go."""
    import fcntl

    try:
        # Never through a link: a lock the project placed as one is no run's.
        fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # EWOULDBLOCK or EACCES: a run process holds it
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _log_tail(path: Path, lines: int = 6) -> str:
    """The last non-blank lines of a run's log, the redactor's to clean."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 16384))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    kept = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(kept[-lines:])


def _job_sha256(argv: Any) -> Optional[str]:
    """The ``--fluid-job-sha256`` a local run was started with, from its recorded argv."""
    if not isinstance(argv, list) or "--fluid-job-sha256" not in argv[:-1]:
        return None
    return str(argv[argv.index("--fluid-job-sha256") + 1])


class LocalRuns:
    """The runs of one build on this machine: list, read, cancel and prune them."""

    def __init__(self, host: LocalHost, product: str, build: str) -> None:
        self.host, self.product, self.build = host, product, build
        self.dir = host.runtime_dir(product) / build

    def where(self) -> str:
        shown = self.dir
        for base in (Path(self.host.workspace), Path(self.host.workspace).resolve()):
            try:
                shown = self.dir.relative_to(base)
                break
            except ValueError:
                continue
        return f"this machine ({shown})"

    def _run_dir(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise SparkRunError(f"{run_id!r} is not a local run id")
        return _confined(self.host.workspace, self.dir / "runs" / run_id)

    def runs(self) -> List[RunInfo]:
        """Every run of the build this machine has a record of, newest first."""
        base = self.dir / "runs"
        if not base.is_dir():
            return []
        base = _confined(self.host.workspace, base)
        infos = [
            self.get(entry.name)
            for entry in base.iterdir()
            if _RUN_ID.fullmatch(entry.name)
            and not entry.is_symlink()
            and (entry / RUN_FILE).is_file()
        ]
        return sorted(infos, key=lambda i: (i.created, i.run_id), reverse=True)

    def get(self, run_id: str) -> RunInfo:
        where = self._run_dir(run_id)
        record = _read_json(where / RUN_FILE)
        if not record:
            raise SparkRunError(f"local run {run_id} of {self.build} has no record in {where}")
        # The lock first: while any of the run's processes lives it is active, whatever
        # its status says, and once none does, the end it recorded (if any) is there.
        held = _held(where / LOCK_FILE)
        status = _read_json(where / STATUS_FILE)
        cancelled = (where / CANCEL_FILE).exists()
        signalled = status.get("stopSignal")
        details, lost = "", False
        if held:
            stopping = cancelled or signalled
            state = CANCELLING if stopping else RUNNING if status.get("started") else PENDING
            raw = state
        elif "ended" in status and status.get("exitCode") == 0:
            state, raw = SUCCEEDED, "SUCCEEDED"  # its work was done, cancelled or not
        elif cancelled:
            state, raw = CANCELLED, "CANCELLED"
        elif signalled:
            # Stopped from outside forge (its machine shut down, a kill): it died, as a run
            # whose node is lost does on a cluster. Only forge's cancel mark is a cancel.
            state, raw, lost = FAILED, f"STOPPED (signal {signalled})", True
            details = "a signal from outside forge stopped it (its machine shut down, or a kill)"
        elif "ended" in status:
            code = status.get("exitCode")
            state, raw = FAILED, f"FAILED (exit {code})"
            details = f"log {where / LOG_FILE}: {_log_tail(where / LOG_FILE)}"
        else:
            state, raw, lost = FAILED, "LOST", True
            details = (
                "its processes stopped without recording how they ended (killed, or the "
                f"machine restarted); log {where / LOG_FILE}: {_log_tail(where / LOG_FILE)}"
            )
        return RunInfo(
            run_id=run_id,
            state=state,
            raw_state=raw,
            config_hash=record.get("configHash"),
            source_id=record.get("sourceId"),
            checkpoint_id=record.get("checkpointId"),
            details=details,
            created=float(record.get("created") or 0),
            lost=lost,
            restart_of=record.get("restartOf") or None,
            stop_key=record.get("stopKey") or None,
            config_uri=record.get("jobConfig") or None,
            job_sha256=_job_sha256(record.get("argv")),
        )

    def cancel(
        self,
        run_id: str,
        *,
        grace: float = STOP_GRACE_S,
        sleep: Any = time.sleep,
        now: Any = time.monotonic,
    ) -> None:
        """Stop a run: the ``cancel`` mark, which its supervisor honours whenever it
        lands, then SIGTERM to its process group, and SIGKILL if it is still going after
        ``grace`` seconds. Each signal is sent while the run's lock is held, so the group
        is still the run's (a group id is not reused while a member lives). A run that
        has ended keeps the end it recorded."""
        where = self._run_dir(run_id)
        lock = where / LOCK_FILE
        if not _held(lock):
            return  # every process of the run has exited: nothing is left to stop
        _replace(where / CANCEL_FILE, f"{time.time()}\n".encode("utf-8"))
        group = _read_json(where / RUN_FILE).get("pgid")
        if not isinstance(group, int):
            # Not in run.json yet (an apply stopped while it started the run): the
            # supervisor's pid, its group's id, which it records before anything else.
            group = _read_json(where / STATUS_FILE).get("supervisorPid")
        if not isinstance(group, int) or not 1 < group < 2**31:
            return  # not started yet: the supervisor reads the mark and starts nothing
        deadline = now() + grace
        for signum in (signal.SIGTERM, signal.SIGKILL):
            if not _held(lock):
                return
            try:
                os.killpg(group, signum)
            except ProcessLookupError:
                return
            except PermissionError as exc:
                raise SparkRunError(
                    f"local run {run_id} of {self.build} cannot be stopped: its processes "
                    "belong to another user"
                ) from exc
            while _held(lock) and now() < deadline:
                sleep(0.25)
            deadline = now() + 5.0

    def prune(self) -> None:
        """Keep the active runs, the newest :data:`KEEP_ENDED` ended ones, and every restart
        under an hour old, which a restart budget still counts (``RESTART_WINDOW_S``)."""
        ended = [r for r in self.runs() if not r.active]
        since = time.time() - RESTART_WINDOW_S
        for info in ended[KEEP_ENDED:]:
            if info.restart_of and info.created >= since:
                continue
            self._run_dir(info.run_id)  # inside .fluid
            # The path as listed, not resolved: rmtree refuses a link there.
            shutil.rmtree(self.dir / "runs" / info.run_id, ignore_errors=True)


def builds_with_runs(host: LocalHost, product: str) -> List[str]:
    """The builds of ``product`` this machine has local runs of."""
    base = host.runtime_dir(product)
    if not base.is_dir():
        return []
    base = _confined(host.workspace, base)
    return sorted(
        entry.name
        for entry in base.iterdir()
        if not entry.is_symlink() and (entry / "runs").is_dir()
    )


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """One start at a time per build, across processes."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def start_run(
    host: LocalHost,
    run: DesiredRun,
    token: str,
    *,
    runner_text: str,
    restart_of: Optional[str] = None,
) -> str:
    """Start ``run`` on this machine and return its id once spark-submit has started;
    ``restart_of`` names the run that died, which it is started again after.

    Writes the artifacts, then a run directory with the run's record, then starts the
    supervisor detached (its own session, so a terminal's Ctrl-C does not reach a stream)
    holding the run's lock. A run of the same config another ``fluid apply`` started a
    moment before is adopted rather than doubled.

    Raises:
        SparkRunError: the operator's settings are unusable, an artifact cannot be
            written, or the supervisor does not start.
    """
    import fcntl

    require_posix()
    sub = submitter(host)
    bound = run.bind(local_root=sub.root)
    publish(sub, bound, runner_text)
    # What publish wrote, which the runner checks before it reads anything.
    bound = dataclasses.replace(bound, job_sha256=config_digest(bound.job_text))
    runs = LocalRuns(host, run.product, run.build)
    _confined(host.workspace, runs.dir)  # before anything is created through a link
    with _locked(runs.dir / "start.lock"):
        going = [r for r in runs.runs() if r.active and r.config_hash == run.config_hash]
        if going:
            return going[0].run_id
        created = time.time()
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(created)) + "-" + uuid.uuid4().hex[:8]
        where = runs.dir / "runs" / run_id
        where.mkdir(parents=True, mode=0o700)  # owner-only, as the supervisor's files
        argv = submit_argv(sub, bound, pull_runner(sub, bound, where), restart_of, stop_key=token)
        record = {
            "runId": run_id,
            "product": run.product,
            "build": run.build,
            "configHash": run.config_hash,
            "sourceId": run.source_id,
            "checkpointId": run.checkpoint_id,
            "jobConfig": bound.job_uri,
            "runner": bound.runner_uri,
            "token": token,
            "created": created,
            "argv": argv,
        }
        if restart_of:
            record["restartOf"] = restart_of
        if STOP_KEY_FLAG in argv:
            record["stopKey"] = token
        fd = os.open(where / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            # Taken before the supervisor exists and handed to it: the run is never seen
            # as stopped while it starts.
            fcntl.flock(fd, fcntl.LOCK_EX)
            _write_json(where / RUN_FILE, record)
            try:
                process = subprocess.Popen(  # noqa: S603 - list argv, no shell
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        str(SUPERVISOR),
                        "--run-dir",
                        str(where),
                        "--lock-fd",
                        str(fd),
                        "--grace",
                        str(STOP_GRACE_S),
                        "--",
                        *argv,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(fd,),
                    start_new_session=True,
                    cwd=str(where),
                    # The driver puts no script directory on sys.path (Python 3.11+).
                    env={**run_env(host.env), "PYTHONSAFEPATH": "1"},
                )
            except OSError as exc:
                shutil.rmtree(where, ignore_errors=True)  # it never ran: nothing to report
                raise SparkRunError(
                    f"the local run's supervisor did not start: {exc.strerror}"
                ) from exc
            _write_json(where / RUN_FILE, {**record, "pgid": process.pid})
        finally:
            os.close(fd)
    try:
        _wait_started(where, process)
    except BaseException:
        # Stopped while it starts (Ctrl-C, or the SIGTERM run_once turns into one): the
        # caller has no run id to cancel yet, so the run is stopped here.
        with suppress(Exception):
            runs.cancel(run_id, grace=5.0)  # the supervisor finishes a stop it outlasts
        raise
    runs.prune()
    return run_id


def run_env(env: Mapping[str, str]) -> Dict[str, str]:
    """The environment a local run starts with: ``env`` without what would make the bash
    of spark-submit run code it was not given (:data:`_ENV_NAME`,
    :data:`_SHELL_STARTUP_ENV`). A defence beside the dotenv loader's own refusal."""
    return {
        name: value
        for name, value in env.items()
        if _ENV_NAME.fullmatch(name)
        and not name.startswith("BASH_FUNC_")
        and name not in _SHELL_STARTUP_ENV
    }


def _wait_started(where: Path, process: "subprocess.Popen[bytes]") -> None:
    """Wait until the supervisor has started spark-submit (or ended), so the first read of
    the run sees it going."""
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        status = _read_json(where / STATUS_FILE)
        if "started" in status or "ended" in status or process.poll() is not None:
            return
        time.sleep(0.05)


def default_host(workspace: Optional[Path] = None) -> LocalHost:
    return LocalHost(Path(workspace) if workspace else Path.cwd())


__all__ = [
    "LOCAL_ROOT_ENV",
    "PROPERTIES_FILE_ENV",
    "REDACTION_REGEX",
    "SUBMIT_EXECUTABLE_ENV",
    "LocalHost",
    "LocalRuns",
    "Submitter",
    "builds_with_runs",
    "default_host",
    "merge_conf",
    "parse_properties",
    "publish",
    "require_posix",
    "run_env",
    "start_run",
    "submit_argv",
    "submitter",
]
