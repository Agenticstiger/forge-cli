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

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
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
)
from fluid_build.build_runners.spark._desired import DesiredRun
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
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)

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
        executable=_executable(env),
        properties_file=path,
        properties=_read_properties(path),
        root=_root(env, host.workspace),
        env=env,
        confine=None if (env.get(LOCAL_ROOT_ENV) or "").strip() else Path(host.workspace),
    )


def _executable(env: Mapping[str, str]) -> str:
    """The spark-submit to run: an absolute path to an executable file, or a name on
    ``PATH``, as ``DBT_EXECUTABLE`` is resolved for the dbt runner. One program, never a
    command line, and never a relative path (it would resolve in the project)."""
    configured = (env.get(SUBMIT_EXECUTABLE_ENV) or "").strip() or "spark-submit"
    if os.path.isabs(configured):
        if os.path.isfile(configured) and os.access(configured, os.X_OK):
            return configured
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
    return found


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


def submit_argv(sub: Submitter, run: DesiredRun, runner: Optional[str] = None) -> List[str]:
    """spark-submit's argv for ``run`` (bound to the artifact root): client mode, so the
    driver is the run's own process and stopping the run stops the job, as the EMR on EC2
    adapter submits a step. ``runner`` is the runner spark-submit runs (:func:`pull_runner`),
    else the one at the artifact root."""
    argv = [sub.executable, "--name", run.run_name, "--deploy-mode", "client"]
    if sub.properties_file is not None:
        argv += ["--properties-file", str(sub.properties_file)]
    conf = merge_conf(run.spark_conf, sub.properties)
    for name in sorted(conf):
        argv += ["--conf", f"{name}={conf[name]}"]
    return [*argv, runner or run.runner_uri, *run.entry_args()]


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
    files = ((run.job_uri, run.job_text.encode("utf-8")), (run.runner_uri, runner))
    if sub.on_s3:
        _put_s3(files, sub.env)
        return
    for uri, body in files:
        path = Path(uri)
        if sub.confine is not None:
            _confined(sub.confine, path.parent)  # no link under the default root leads out
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if sub.confine is not None:
                _confined(sub.confine, path.parent)
            if path.is_file() and not path.is_symlink() and path.read_bytes() == body:
                continue
            _replace(path, body)
        except OSError as exc:
            raise SparkRunError(f"{path} cannot be written: {exc.strerror}") from exc


def _replace(path: Path, body: bytes) -> None:
    """Write ``path`` whole through a temporary file nobody can have placed: a new name,
    created exclusively and never through a link, then renamed over ``path`` (a rename
    replaces a link at ``path`` itself, never its target)."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _put_s3(files: Any, env: Mapping[str, str]) -> None:
    """Upload with boto3 (the ``[aws]`` extra): its own endpoint settings
    (``AWS_ENDPOINT_URL_S3``, ``AWS_ENDPOINT_URL``) reach SeaweedFS or MinIO, which want
    path-style requests, and ignore the region the SDK insists on."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise SparkRunError(
            f"{LOCAL_ROOT_ENV} is an s3:// root, and uploading to it needs boto3: "
            "pip install 'data-product-forge[aws]'"
        ) from exc
    custom = any(env.get(v) for v in ("AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL"))
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1"
    client = boto3.client(
        "s3",
        region_name=region,
        config=Config(s3={"addressing_style": "path"}) if custom else None,
    )
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
        status = _read_json(where / STATUS_FILE)
        cancelled = (where / CANCEL_FILE).exists()
        details = ""
        if "ended" in status:
            code = status.get("exitCode")
            if cancelled:
                state, raw = CANCELLED, "CANCELLED"
            elif code == 0:
                state, raw = SUCCEEDED, "SUCCEEDED"
            else:
                state, raw = FAILED, f"FAILED (exit {code})"
                details = f"log {where / LOG_FILE}: {_log_tail(where / LOG_FILE)}"
        elif _held(where / LOCK_FILE):
            state = CANCELLING if cancelled else RUNNING if status.get("started") else PENDING
            raw = state
        elif cancelled:
            state, raw = CANCELLED, "CANCELLED"
        else:
            state, raw = FAILED, "LOST"
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
        )

    def cancel(
        self,
        run_id: str,
        *,
        grace: float = STOP_GRACE_S,
        sleep: Any = time.sleep,
        now: Any = time.monotonic,
    ) -> None:
        """Stop a run: SIGTERM to its process group, SIGKILL if it is still going after
        ``grace`` seconds. Each signal is sent while the run's lock is held, so the group
        is still the run's (a group id is not reused while a member lives)."""
        where = self._run_dir(run_id)
        (where / CANCEL_FILE).write_text(f"{time.time()}\n", encoding="utf-8")
        record = _read_json(where / RUN_FILE)
        group = record.get("pgid")
        lock = where / LOCK_FILE
        if not isinstance(group, int) or group <= 1:
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
        """Keep the active runs and the newest :data:`KEEP_ENDED` ended ones."""
        ended = [r for r in self.runs() if not r.active]
        for info in ended[KEEP_ENDED:]:
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


def start_run(host: LocalHost, run: DesiredRun, token: str, *, runner_text: str) -> str:
    """Start ``run`` on this machine and return its id once spark-submit has started.

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
    runs = LocalRuns(host, run.product, run.build)
    _confined(host.workspace, runs.dir)  # before anything is created through a link
    with _locked(runs.dir / "start.lock"):
        going = [r for r in runs.runs() if r.active and r.config_hash == run.config_hash]
        if going:
            return going[0].run_id
        created = time.time()
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(created)) + "-" + uuid.uuid4().hex[:8]
        where = runs.dir / "runs" / run_id
        where.mkdir(parents=True)
        argv = submit_argv(sub, bound, pull_runner(sub, bound, where))
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
                    env={**host.env, "PYTHONSAFEPATH": "1"},
                )
            except OSError as exc:
                raise SparkRunError(
                    f"the local run's supervisor did not start: {exc.strerror}"
                ) from exc
            _write_json(where / RUN_FILE, {**record, "pgid": process.pid})
        finally:
            os.close(fd)
    _wait_started(where, process)
    runs.prune()
    return run_id


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
    "start_run",
    "submit_argv",
    "submitter",
]
