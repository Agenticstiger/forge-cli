# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An Iceberg expose's maintenance (``exposes[].iceberg.maintenance``), read and checked.

::

    exposes:
      - exposeId: orders
        binding: {platform: aws, format: iceberg, location: {database: silver, table: orders}}
        iceberg:
          maintenance:
            backend: spark            # spark (forge's runner, the default) | glue
            schedule: "0 3 * * *"     # spark: when its maintenance DAG runs
            compaction: {targetFileSizeMb: 512}
            rewriteManifests: {}
            expireSnapshots: {olderThan: P5D, retainLast: 3}
            removeOrphanFiles: {olderThan: P3D}
            actions:                  # spark: a plugin's actions (fluid_build.maintenance_actions)
              - {name: erase, kind: gdpr-erasure, options: {requests: gdpr.requests}}

``backend: spark`` runs it with forge's Spark runner, on the runtime and as the role of the
``engine: spark`` build that writes the table, while that build's stream keeps running. A table
no forge build writes (its streams run on the team's own runner) names its own runtime
instead, with ``runtime`` in place of ``build``: an EMR one::

    maintenance:
      schedule: "0 3 * * *"
      runtime: {runtime: emr-serverless, applicationId: 00f1abcd2345,
                executionRoleArn: "arn:aws:iam::123456789012:role/orders-maintenance"}
      compaction: {}
      removeOrphanFiles: {olderThan: P7D}

or forge's local runtime, which runs spark-submit on the machine ``fluid apply`` runs on and
maintains a table in an Iceberg REST catalog on ``binding.platform: local`` (it takes no role and
no target, and ignores the AWS keys)::

    maintenance:
      runtime: {runtime: local}
      compaction: {}

forge cannot see that writer's commit delay, so removing orphan files there needs an explicit
``olderThan`` (one under three days is warned about, :func:`maintenance_warnings`).
``backend: glue`` hands compaction, snapshot retention and orphan deletion to the AWS Glue Data
Catalog's table optimizers (``aws_glue_catalog_table_optimizer``), which ``fluid apply``
provisions and AWS runs. :mod:`fluid_build.maintenance` says where each part runs.

``iceberg.compaction.targetFileSizeMb`` and ``iceberg.snapshotRetention`` (``maxSnapshotAgeMs``,
``minSnapshotsToKeep``), which nothing read before, are the defaults of the maintenance's own
``compaction.targetFileSizeMb``, ``expireSnapshots.olderThan`` and ``retainLast``; a value given
in both places must be the same. They declare no maintenance on their own.

A run takes the steps in this order: the plugin's actions, compaction, the manifests, the
snapshots, the orphans. A deleted row leaves the files it was in only when compaction rewrites
them and the snapshots that held them expire, which is why the actions come first.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

#: The backends a table's maintenance may use; the first is the default.
BACKENDS: Tuple[str, ...] = ("spark", "glue")

#: The keys of a maintenance block that declare work, in the order a run takes it.
ACTION_KEYS: Tuple[str, ...] = (
    "actions",
    "compaction",
    "rewriteManifests",
    "expireSnapshots",
    "removeOrphanFiles",
)

_BLOCK_KEYS = frozenset(
    {
        "backend",
        "schedule",
        "timezone",
        "build",
        "runtime",
        "roleArn",
        "runEveryHours",
        *ACTION_KEYS,
    }
)
#: The EMR runtimes a table no forge build writes may be maintained on, each with a target the
#: team already runs.
EMR_RUNTIMES: Tuple[str, ...] = ("emr-serverless", "emr-ec2", "emr-eks")
#: The runtimes a table no forge build writes may be maintained on: the EMR ones and forge's
#: local runtime, which maintains a REST-catalog table on platform local (Glue's one job is a
#: build's).
RUNTIMES: Tuple[str, ...] = (*EMR_RUNTIMES, "local")
#: The keys of ``runtime``: the ones of a build's ``parameters.spark`` that say where a run
#: runs and as whom (``$defs/sparkRuntime``).
_RUNTIME_KEYS = frozenset(
    {
        "runtime",
        "applicationId",
        "clusterId",
        "virtualClusterId",
        "releaseLabel",
        "executionRoleArn",
        "sparkConf",
    }
)
#: The target each runtime needs: a table no forge build writes runs on one the team has.
_RUNTIME_TARGET = {
    "emr-serverless": "applicationId",
    "emr-ec2": "clusterId",
    "emr-eks": "virtualClusterId",
}
_COMPACTION_KEYS = frozenset(
    {
        "enabled",
        "strategy",
        "targetFileSizeMb",
        "minInputFiles",
        "deleteFileThreshold",
        "maxCommits",
        "maxConcurrentRewrites",
    }
)
_EXPIRE_KEYS = frozenset({"enabled", "olderThan", "retainLast", "cleanExpiredFiles"})
_ORPHAN_KEYS = frozenset({"enabled", "olderThan"})
_MANIFEST_KEYS = frozenset({"enabled"})
_ACTION_ITEM_KEYS = frozenset({"name", "kind", "options"})

#: Compaction strategies by backend: Glue's optimizer also orders by z-order.
STRATEGIES: Mapping[str, Tuple[str, ...]] = {
    "spark": ("binpack", "sort"),
    "glue": ("binpack", "sort", "z-order"),
}

#: The shortest orphan retention a contract may set. A file younger than the longest write
#: (a stream's micro-batch and its commit retries included) is not an orphan yet, and
#: deleting it corrupts the table (Iceberg's maintenance guide). Iceberg's default is 3 days.
MIN_ORPHAN_SECONDS = 86400
DEFAULT_ORPHAN_SECONDS = 3 * 86400
#: The shortest snapshot age a contract may expire: an incremental reader may still be on a
#: younger one, and a stream finds its last committed epoch in recent snapshots. A day, as
#: Glue's retention optimizer allows no less.
MIN_EXPIRE_SECONDS = 86400
#: Iceberg's defaults for expire_snapshots: older than five days, keeping the last one.
DEFAULT_EXPIRE_SECONDS = 5 * 86400
DEFAULT_RETAIN_LAST = 1
#: Iceberg's default ``partial-progress.max-commits``.
DEFAULT_MAX_COMMITS = 10
#: Iceberg's default ``max-concurrent-file-group-rewrites``, written out so the job config says
#: how many file groups a run rewrites at once beside a live writer.
DEFAULT_MAX_CONCURRENT_REWRITES = 5
#: How many plugin actions one table may declare.
MAX_ACTIONS = 20
#: Under this, removing orphan files of a table forge does not write is warned about: its
#: writer's commit delay is not one forge can see (Iceberg's own default is 3 days).
WARN_ORPHAN_SECONDS = DEFAULT_ORPHAN_SECONDS

_ROLE_ARN = re.compile(r"arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}\Z")
_ACTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_KIND = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_DURATION = re.compile(
    r"P(?=\d|T\d)(?:(\d{1,7})W)?(?:(\d{1,7})D)?"
    r"(?:T(?=\d)(?:(\d{1,7})H)?(?:(\d{1,7})M)?(?:(\d{1,7})S)?)?\Z"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_OPTION_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


@dataclass(frozen=True)
class Compaction:
    """``compaction``: Iceberg's ``rewrite_data_files`` (spark) or Glue's compaction optimizer."""

    strategy: str = "binpack"
    target_file_size_mb: Optional[int] = None
    min_input_files: Optional[int] = None
    delete_file_threshold: Optional[int] = None
    max_commits: int = DEFAULT_MAX_COMMITS
    max_concurrent_rewrites: Optional[int] = None


@dataclass(frozen=True)
class ExpireSnapshots:
    """``expireSnapshots``: Iceberg's ``expire_snapshots`` or Glue's retention optimizer."""

    older_than_seconds: int = DEFAULT_EXPIRE_SECONDS
    retain_last: int = DEFAULT_RETAIN_LAST
    clean_expired_files: bool = True


@dataclass(frozen=True)
class RemoveOrphanFiles:
    """``removeOrphanFiles``: Iceberg's ``remove_orphan_files`` or Glue's orphan optimizer."""

    older_than_seconds: int = DEFAULT_ORPHAN_SECONDS


@dataclass(frozen=True)
class CustomAction:
    """One ``actions[]`` entry: a plugin's kind and its options (JSON values only)."""

    name: str
    kind: str
    options: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class TableMaintenance:
    """One Iceberg expose's maintenance, read and checked."""

    expose_id: str
    backend: str
    database: str
    table: str
    schedule: Optional[str] = None
    timezone: str = "UTC"
    #: The ``engine: spark`` build that writes the table (backend spark): its runtime and
    #: role run the maintenance.
    build_id: Optional[str] = None
    #: The runtime a table no forge build writes is maintained on (backend spark, in place of
    #: ``build_id``): ``runtime``, and on EMR its target and ``executionRoleArn`` and optionally
    #: ``releaseLabel`` and ``sparkConf``, as a build's ``parameters.spark`` spells them. A
    #: local runtime names no target and no role.
    runtime: Optional[Mapping[str, Any]] = field(default=None, hash=False)
    #: The role Glue's optimizers run as (backend glue).
    role_arn: Optional[str] = None
    #: How often Glue runs the retention and orphan optimizers (backend glue).
    run_every_hours: int = 24
    compaction: Optional[Compaction] = None
    rewrite_manifests: bool = False
    expire: Optional[ExpireSnapshots] = None
    orphans: Optional[RemoveOrphanFiles] = None
    actions: Tuple[CustomAction, ...] = ()


def parse_duration(value: Any) -> Optional[int]:
    """Seconds in an ISO-8601 duration of weeks, days, hours, minutes and seconds, or None."""
    if not isinstance(value, str):
        return None
    match = _DURATION.match(value.strip())
    if not match:
        return None
    weeks, days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return (((weeks * 7 + days) * 24 + hours) * 60 + minutes) * 60 + seconds


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _plain(name: str) -> str:
    """``name`` as a DAG file and a plan action spell it (``fluid_apply.dag_filename_for``)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _whole(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _enabled(block: Any) -> bool:
    return isinstance(block, Mapping) and block.get("enabled", True) is not False


def _check_keys(
    block: Mapping[str, Any], allowed: FrozenSet[str], where: str, errors: List[str]
) -> None:
    unknown = sorted(str(k) for k in block if k not in allowed)
    if unknown:
        errors.append(f"{where} has keys it does not read: {', '.join(unknown)}")


def _block(
    raw: Any, allowed: FrozenSet[str], where: str, errors: List[str]
) -> Optional[Mapping[str, Any]]:
    """``raw`` when it is an enabled mapping (its keys checked); None when it is absent or
    ``enabled: false``."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        errors.append(f"{where} must be a mapping")
        return None
    _check_keys(raw, allowed, where, errors)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append(f"{where}.enabled must be true or false")
    return raw if _enabled(raw) else None


def _read_compaction(
    raw: Any, backend: str, legacy: Mapping[str, Any], where: str, errors: List[str]
) -> Optional[Compaction]:
    block = _block(raw, _COMPACTION_KEYS, where, errors)
    if block is None:
        return None
    if legacy.get("enabled") is False:
        errors.append(f"{where}: iceberg.compaction.enabled is false; drop it or this block")
    strategy = block.get("strategy", "binpack")
    if strategy not in STRATEGIES[backend]:
        errors.append(
            f"{where}.strategy must be one of {', '.join(STRATEGIES[backend])} on backend {backend}"
        )
    # The legacy field is forge's runner's default; Glue's optimizer reads the table property.
    default = legacy.get("targetFileSizeMb") if backend == "spark" else None
    size = block.get("targetFileSizeMb", default)
    if block.get("targetFileSizeMb") is not None and default not in (
        None,
        block.get("targetFileSizeMb"),
    ):
        errors.append(
            f"{where}.targetFileSizeMb and iceberg.compaction.targetFileSizeMb differ; keep one"
        )
    if size is not None and not _whole(size, 1, 1024 * 1024):
        source = (
            f"{where}.targetFileSizeMb"
            if "targetFileSizeMb" in block
            else ("iceberg.compaction.targetFileSizeMb")
        )
        errors.append(f"{source} must be a whole number of megabytes")
    if block.get("targetFileSizeMb") is not None and backend == "glue":
        errors.append(
            f"{where}.targetFileSizeMb: Glue's optimizer reads the table's "
            "write.target-file-size-bytes property, not a setting of its own; leave it out"
        )
    for key, low, high in (
        ("minInputFiles", 1, 1_000_000),
        ("deleteFileThreshold", 1, 1_000_000),
        ("maxCommits", 1, 1000),
        ("maxConcurrentRewrites", 1, 1000),
    ):
        if block.get(key) is not None and not _whole(block.get(key), low, high):
            errors.append(f"{where}.{key} must be a whole number from {low} to {high}")
    if backend == "glue":
        for key in ("maxCommits", "maxConcurrentRewrites"):
            if key in block:
                errors.append(f"{where}.{key} is forge's runner's; Glue's optimizer takes none")
    return Compaction(
        strategy=str(strategy),
        target_file_size_mb=size if _whole(size, 1, 1024 * 1024) else None,
        min_input_files=block.get("minInputFiles"),
        delete_file_threshold=block.get("deleteFileThreshold"),
        max_commits=(
            block["maxCommits"] if _whole(block.get("maxCommits"), 1, 1000) else DEFAULT_MAX_COMMITS
        ),
        max_concurrent_rewrites=block.get("maxConcurrentRewrites"),
    )


def _read_expire(
    raw: Any, backend: str, legacy: Mapping[str, Any], where: str, errors: List[str]
) -> Optional[ExpireSnapshots]:
    block = _block(raw, _EXPIRE_KEYS, where, errors)
    if block is None:
        return None
    older = DEFAULT_EXPIRE_SECONDS
    legacy_ms = legacy.get("maxSnapshotAgeMs")
    if block.get("olderThan") is not None:
        parsed = parse_duration(block.get("olderThan"))
        if parsed is None or parsed < MIN_EXPIRE_SECONDS:
            errors.append(f"{where}.olderThan must be an ISO-8601 duration of at least P1D")
        else:
            older = parsed
            if _whole(legacy_ms, 0, 2**63) and legacy_ms != parsed * 1000:
                errors.append(
                    f"{where}.olderThan and iceberg.snapshotRetention.maxSnapshotAgeMs differ; "
                    "keep one"
                )
    elif legacy_ms is not None:
        if not _whole(legacy_ms, MIN_EXPIRE_SECONDS * 1000, 2**63):
            errors.append(
                "iceberg.snapshotRetention.maxSnapshotAgeMs must be at least 86400000 (a day) "
                f"for {where} to expire by it"
            )
        else:
            older = legacy_ms // 1000
    retain = block.get("retainLast", legacy.get("minSnapshotsToKeep", DEFAULT_RETAIN_LAST))
    if "retainLast" in block and legacy.get("minSnapshotsToKeep") not in (None, retain):
        errors.append(
            f"{where}.retainLast and iceberg.snapshotRetention.minSnapshotsToKeep differ; keep one"
        )
    if not _whole(retain, 1, 100_000):
        source = (
            f"{where}.retainLast"
            if "retainLast" in block
            else "iceberg.snapshotRetention.minSnapshotsToKeep"
        )
        errors.append(f"{source} must be a whole number from 1 to 100000")
        retain = DEFAULT_RETAIN_LAST
    clean = block.get("cleanExpiredFiles", True)
    if not isinstance(clean, bool):
        errors.append(f"{where}.cleanExpiredFiles must be true or false")
        clean = True
    if backend == "spark" and clean is False:
        errors.append(
            f"{where}.cleanExpiredFiles: forge's runner deletes the files only the expired "
            "snapshots held; false is for backend glue"
        )
    if backend == "glue" and older % 86400:
        source = (
            f"{where}.olderThan"
            if block.get("olderThan") is not None
            else "iceberg.snapshotRetention.maxSnapshotAgeMs"
        )
        errors.append(f"{source} must be whole days on backend glue")
    return ExpireSnapshots(
        older_than_seconds=older, retain_last=int(retain), clean_expired_files=clean
    )


def _read_orphans(
    raw: Any, backend: str, where: str, errors: List[str]
) -> Optional[RemoveOrphanFiles]:
    block = _block(raw, _ORPHAN_KEYS, where, errors)
    if block is None:
        return None
    older = DEFAULT_ORPHAN_SECONDS
    if block.get("olderThan") is not None:
        parsed = parse_duration(block.get("olderThan"))
        if parsed is None or parsed < MIN_ORPHAN_SECONDS:
            errors.append(
                f"{where}.olderThan must be an ISO-8601 duration of at least P1D: a file younger "
                "than the longest write is not an orphan yet, and deleting it corrupts the table"
            )
        else:
            older = parsed
    if backend == "glue" and older % 86400:
        errors.append(f"{where}.olderThan must be whole days on backend glue")
    return RemoveOrphanFiles(older_than_seconds=older)


def option_problems(value: Any, where: str, depth: int = 0) -> List[str]:
    """What keeps a plugin action's ``options`` from reaching the job config: JSON values only,
    no control character, no ``${``, nothing the redactor masks, and no key the redactor reads as
    a credential's (``password``, ``apiKey``, ``routing_key``, in any case or spelling), whatever
    its value is. A value is never quoted."""
    from fluid_build.alerts.routes import secret_option_key
    from fluid_build.observability.secret_redactor import redact_secret_text

    if depth > 4:
        return [f"{where} is nested deeper than 4 levels"]
    if value is None or isinstance(value, (bool, int)):
        return []
    if isinstance(value, float):
        return [] if math.isfinite(value) else [f"{where} is not a finite number"]
    if isinstance(value, str):
        if len(value) > 1000:
            return [f"{where} is longer than 1000 characters"]
        if _CONTROL.search(value) or "${" in value:
            return [f"{where} holds a control character or '${{' (value not shown)"]
        if redact_secret_text(value) != value:
            return [
                f"{where} holds what looks like a secret (value not shown); a plugin reads "
                + "its secrets itself"
            ]
        return []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for index, item in enumerate(value[:50]):
            out += option_problems(item, f"{where}[{index}]", depth + 1)
        return out + ([f"{where} has more than 50 items"] if len(value) > 50 else [])
    if isinstance(value, Mapping):
        out = []
        for key, item in list(value.items())[:50]:
            if not isinstance(key, str) or not _OPTION_KEY.match(key):
                out.append(f"{where} has a key that is not a plain name (key not shown)")
                continue
            if secret_option_key(key):
                out.append(
                    f"{where}.{key} is named like a secret; a plugin reads its secrets itself, "
                    "never from the contract (value not shown)"
                )
                continue
            out += option_problems(item, f"{where}.{key}", depth + 1)
        return out + ([f"{where} has more than 50 keys"] if len(value) > 50 else [])
    return [f"{where} is a {type(value).__name__}, not a JSON value"]


def _read_actions(
    raw: Any, backend: str, where: str, errors: List[str]
) -> Tuple[CustomAction, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        errors.append(f"{where} must be a list")
        return ()
    if backend != "spark" and raw:
        errors.append(f"{where}: a plugin's actions run with forge's Spark runner (backend spark)")
        return ()
    if len(raw) > MAX_ACTIONS:
        errors.append(f"{where} holds more than {MAX_ACTIONS} actions")
        return ()
    out: List[CustomAction] = []
    seen = set()
    for index, item in enumerate(raw):
        at = f"{where}[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{at} must be a mapping")
            continue
        _check_keys(item, _ACTION_ITEM_KEYS, at, errors)
        name, kind = item.get("name"), item.get("kind")
        if not isinstance(name, str) or not _ACTION_NAME.match(name):
            errors.append(f"{at}.name must be a plain name (letters, digits, '-' and '_')")
            continue
        if not isinstance(kind, str) or not _KIND.match(kind):
            errors.append(f"{at}.kind must name a plugin's kind (lower case, digits, '-' and '_')")
            continue
        if name in seen:
            errors.append(f"{at}.name {name!r} is an earlier action's too")
            continue
        seen.add(name)
        options = item.get("options", {})
        if not isinstance(options, Mapping):
            errors.append(f"{at}.options must be a mapping")
            continue
        problems = option_problems(options, f"{at}.options")
        errors += problems
        if not problems:
            out.append(CustomAction(name=name, kind=kind, options=dict(options)))
    return tuple(out)


#: Path segments a listing resolves (``x/..`` is ``x``'s parent) that Iceberg keeps as written.
_DOT_SEGMENTS = frozenset({".", ".."})


def location_dot_segment_errors(contract: Mapping[str, Any]) -> List[str]:
    """One message for each expose whose ``binding.location.path`` holds a ``.`` or ``..``
    segment, whether or not it declares maintenance (an object-store location: one with a
    bucket, or on AWS; a local file's path is left alone). Neither Iceberg
    nor an IAM policy resolves one: the table is kept at ``<warehouse>/x/..`` as written, and a
    listing, a grant or a write that does resolve it reaches the neighbour (``x/..`` is the
    parent of ``x``)."""
    errors: List[str] = []
    for index, expose in enumerate(contract.get("exposes") or []):
        if not isinstance(expose, Mapping):
            continue
        binding = _mapping(expose.get("binding"))
        location = _mapping(binding.get("location"))
        path = location.get("path")
        # an object-store key (a bucket, or AWS, where the bucket may be derived): a local
        # file's ``./out/x.parquet`` is a path on a disk, where the dots mean what they say
        if not isinstance(path, str) or not (
            location.get("bucket") or str(binding.get("platform") or "").lower() == "aws"
        ):
            continue
        # a backslash is a separator to some parsers, so it splits a segment as ``/`` does
        parts = path.replace("\\", "/").split("/")
        if any(part.strip() in _DOT_SEGMENTS for part in parts):
            name = expose.get("exposeId") or index
            errors.append(
                f"exposes[{name}] binding.location.path holds a '.' or '..' segment, which a "
                "table's location keeps as written and a listing, a grant or a write resolves "
                "(x/.. is the parent of x), so it can reach files that are not the table's; "
                "write the path without one"
            )
    return errors


def _object_location(expose: Mapping[str, Any]) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """``(bucket, path segments)`` of an AWS expose's S3 location, as forge places it
    (``providers/aws/util/warehouse.normalize_location``: the binding's ``bucket`` and ``path``,
    ``<database>/<table>/`` by default); None for an expose with no S3 location (another
    platform, or a REST catalog's table, whose location that catalog assigns)."""
    from fluid_build.providers._iceberg_catalog import is_glue_cataloged
    from fluid_build.providers.aws.util.warehouse import normalize_location

    binding = _mapping(expose.get("binding"))
    loc = _mapping(binding.get("location"))
    if str(binding.get("platform") or "") != "aws":
        return None
    named = bool(loc.get("bucket")) or loc.get("path") is not None
    if not named and not (loc.get("database") and loc.get("table")):
        return None
    iceberg = str(binding.get("format") or "").lower() == "iceberg"
    if iceberg and not named and not is_glue_cataloged(binding):
        return None
    bucket, path = normalize_location(loc, account_ref="<account>")
    return bucket, tuple(part for part in path.split("/") if part)


def table_location(contract: Mapping[str, Any], expose_id: str) -> Optional[str]:
    """``s3://<bucket>/<path>/`` of the expose's table as forge places it, or None when the
    catalog assigns it (an unnamed bucket reads ``<account>-fluid-data``)."""
    for expose in contract.get("exposes") or []:
        if isinstance(expose, Mapping) and str(expose.get("exposeId")) == expose_id:
            found = _object_location(expose)
            if found is not None:
                return f"s3://{found[0]}/{'/'.join(found[1])}/"
    return None


def _orphan_scope_problem(contract: Mapping[str, Any], expose: Mapping[str, Any]) -> str:
    """Why removing orphan files under the expose's table location would reach files that are
    not the table's, or ``""``. Iceberg (and Glue's optimizer) deletes every file under the
    location that no snapshot of this table references, so the location must be the table's
    alone: never a bucket root, never the parent or the child of another expose's location
    (compared segment by segment, so ``pre`` and ``prefix-other`` do not nest). Tables outside
    the contract cannot be seen here: the location must not hold theirs either."""
    mine = _object_location(expose)
    if mine is None:
        return ""
    bucket, parts = mine
    if any(part in _DOT_SEGMENTS for part in parts):
        return (
            "the table's location holds a '.' or '..' segment, which Iceberg keeps as written "
            "and the file listing resolves (x/.. is the bucket root), so what removing orphan "
            "files would reach cannot be told; write binding.location.path without one"
        )
    if not parts:
        return (
            f"the table's location is the root of bucket {bucket}, so removing orphan files "
            "would delete every file there that this table does not reference; give the table "
            "a prefix of its own (binding.location.path)"
        )
    for other in contract.get("exposes") or []:
        if not isinstance(other, Mapping) or other is expose:
            continue
        theirs = _object_location(other)
        if theirs is None or theirs[0] != bucket:
            continue
        if any(part in _DOT_SEGMENTS for part in theirs[1]):
            return (
                f"expose {other.get('exposeId')}'s location in bucket {bucket} holds a '.' or "
                "'..' segment, so whether removing orphan files here would reach its files "
                "cannot be told; write its binding.location.path without one"
            )
        shorter = min(len(parts), len(theirs[1]))
        if parts[:shorter] == theirs[1][:shorter]:
            return (
                f"the table's location s3://{bucket}/{'/'.join(parts)}/ and expose "
                f"{other.get('exposeId')}'s s3://{bucket}/{'/'.join(theirs[1])}/ hold one "
                "another, so removing orphan files would delete the other's files; give each "
                "table a prefix of its own"
            )
    return ""


def _iceberg_location(expose: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str], str]:
    """``(database, table, platform)`` of an Iceberg expose's binding; None parts otherwise."""
    binding = _mapping(expose.get("binding"))
    if str(binding.get("format") or "").lower() != "iceberg":
        return None, None, ""
    loc = _mapping(binding.get("location"))
    database, table = loc.get("database"), loc.get("table")
    return (
        database if isinstance(database, str) and database else None,
        table if isinstance(table, str) and table else None,
        str(binding.get("platform") or ""),
    )


def _writers(contract: Mapping[str, Any], expose_id: str) -> List[Mapping[str, Any]]:
    """The forge Spark builds (``engine: spark`` with parameters.spark) whose output is
    ``expose_id``: a Spark build writes one expose, its ``outputs[0]``."""
    from fluid_build.spark.compiler import spark_builds

    return [
        b
        for b in spark_builds(contract)
        if isinstance(b.get("outputs"), list) and b["outputs"] and str(b["outputs"][0]) == expose_id
    ]


def _check_cron(value: Any, where: str, errors: List[str]) -> Optional[str]:
    from fluid_build.schedulers.airflow.fluid_apply import ScheduleRenderError, _validate_schedule

    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}.schedule is required on backend spark: the cron its DAG runs on")
        return None
    try:
        cron = _validate_schedule(value, "maintenance")
    except ScheduleRenderError as exc:
        # The renderer names a build's trigger; this is the table's schedule.
        why = str(exc).split(f"trigger schedule {value!r}", 1)[-1].lstrip(": ")
        errors.append(f"{where}.schedule {value!r} is not a cron Airflow loads: {why}")
        return None
    if cron == "@once":
        errors.append(f"{where}.schedule: maintenance recurs, and @once runs it one time")
        return None
    return cron


def _check_timezone(value: Any, where: str, errors: List[str]) -> str:
    from fluid_build.schedulers.airflow.fluid_apply import ScheduleRenderError, _validate_timezone

    if value is None:
        return "UTC"
    try:
        return _validate_timezone(value, "maintenance")
    except ScheduleRenderError as exc:
        errors.append(f"{where}.timezone is not an IANA zone ({exc})")
        return "UTC"


def runtime_build(expose_id: str, runtime: Mapping[str, Any]) -> Dict[str, Any]:
    """The ``runtime`` of a table no forge build writes, as the Spark build whose
    ``parameters.spark`` it would be: what the operator's policy and the runtime field checks
    read (``iac/providers/aws_identity.py``), under the name its artifacts go by
    (``spark.names.maintenance_build``)."""
    from fluid_build.spark.names import maintenance_build

    return {
        "id": maintenance_build(expose_id),
        "engine": "spark",
        "outputs": [expose_id],
        "properties": {"parameters": {"spark": dict(runtime)}},
    }


def runtime_findings(
    expose_id: str, runtime: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> Tuple[List[str], List[str]]:
    """``(errors, missing operator entries)`` of a table's maintenance ``runtime``: the checks a
    build's ``parameters.spark`` goes through (its fields' shapes, the release label, a
    ``sparkConf`` key no contract may set, and the operator's role, target and ``sparkConf``
    lists), each message naming the runtime block. A missing entry is a warning in ``fluid
    validate`` and a refusal where a run is compiled or provisioned."""
    from fluid_build.iac.providers import aws_identity

    build = runtime_build(expose_id, runtime)
    errors, missing = aws_identity.identity_findings(build, environ)
    if runtime.get("executionRoleArn"):  # without one, the block's own error says so
        errors += aws_identity.runtime_errors(build)
    head = f"Spark build {build['id']}: parameters.spark."
    at = f"exposes[{expose_id}].iceberg.maintenance.runtime."
    return [e.replace(head, at) for e in errors], [m.replace(head, at) for m in missing]


def _read_runtime(
    contract: Mapping[str, Any], raw: Any, eid: str, where: str, errors: List[str]
) -> Optional[Dict[str, Any]]:
    """A backend-spark block's ``runtime``, checked; None (with ``errors``) when it cannot run.
    The expose must be a table the runtime can maintain (:func:`_check_sink`)."""
    from fluid_build.build_runners.spark._desired import _placement

    at = f"{where}.runtime"
    before = len(errors)
    if not isinstance(raw, Mapping):
        errors.append(f"{at} must be a mapping")
        return None
    _check_keys(raw, _RUNTIME_KEYS, at, errors)
    runtime = raw.get("runtime")
    if runtime not in RUNTIMES:
        errors.append(
            f"{at}.runtime must be one of {', '.join(RUNTIMES)}: a table no forge build writes "
            "is maintained on an EMR runtime its team runs, or on forge's local runtime "
            "(runtime glue is a build's)"
        )
        return None
    if runtime == "local":
        # As a local build: no role, no target; the AWS keys are accepted and ignored.
        conf = raw.get("sparkConf")
        if conf is not None and not isinstance(conf, Mapping):
            errors.append(f"{at}.sparkConf must be a mapping of names to strings")
        elif conf is not None and any(not isinstance(v, str) for v in conf.values()):
            errors.append(f"{at}.sparkConf values must be strings")
        else:
            errors += runtime_findings(eid, raw)[0]
        if len(errors) == before:
            _check_sink(contract, eid, True, errors)
        return dict(raw) if len(errors) == before else None
    if not isinstance(raw.get("executionRoleArn"), str) or not raw["executionRoleArn"]:
        errors.append(
            f"{at}.executionRoleArn is required: no forge build writes the table, so no "
            "build's role runs its maintenance (on emr-ec2, name the instance-profile role the "
            "cluster's steps run as)"
        )
    target = _RUNTIME_TARGET[runtime]
    if runtime == "emr-ec2":
        try:  # the build's own refusal, naming this block
            _placement(raw, runtime, "", "", {}, at, at)
        except ValueError as exc:
            errors.append(str(exc))
    elif not raw.get(target):
        errors.append(
            f"{at}.{target} is required on runtime {runtime}: forge provisions no runtime for "
            "a table it does not write"
        )
    conf = raw.get("sparkConf")
    if conf is not None and not isinstance(conf, Mapping):
        errors.append(f"{at}.sparkConf must be a mapping of names to strings")
    elif conf is not None and any(not isinstance(v, str) for v in conf.values()):
        errors.append(f"{at}.sparkConf values must be strings")
    else:
        errors += runtime_findings(eid, raw)[0]
    if len(errors) == before:
        _check_sink(contract, eid, False, errors)
    return dict(raw) if len(errors) == before else None


def _check_sink(contract: Mapping[str, Any], eid: str, local: bool, errors: List[str]) -> None:
    """The expose's format, catalog and location against the runtime, as the run's job config
    compiles them (no operator gate): a Glue-catalog table for an EMR runtime, a REST-catalog
    one on platform local for ``local``."""
    from fluid_build.spark.compiler import maintenance_sink

    try:
        maintenance_sink(contract, eid, account_ref="<account>", local=local)
    except ValueError as exc:
        errors.append(str(exc))


def _read_spark_placement(
    contract: Mapping[str, Any], raw: Mapping[str, Any], eid: str, where: str, errors: List[str]
) -> Tuple[Optional[str], str, Optional[str], Optional[Dict[str, Any]]]:
    """``(schedule, timezone, build id, runtime)`` of a backend-spark block: the build that
    writes the table, or the ``runtime`` a table no forge build writes names."""
    from fluid_build.spark.compiler import spark_parameters
    from fluid_build.streaming.compiler import managed_lane_enabled

    for key in ("roleArn", "runEveryHours"):
        if key in raw:
            errors.append(f"{where}.{key} is backend glue's")
    schedule = _check_cron(raw.get("schedule"), where, errors)
    timezone = _check_timezone(raw.get("timezone"), where, errors)
    if not managed_lane_enabled(contract):
        errors.append(
            f"{where}: backend spark runs with forge's Spark runner, which reads a contract of "
            "fluidVersion 0.7.7 or later"
        )
        return schedule, timezone, None, None
    if "runtime" in raw:
        if "build" in raw:
            errors.append(
                f"{where}: name build (the engine: spark build that writes the table) or "
                "runtime (the runtime of a table no forge build writes), not both"
            )
            return schedule, timezone, None, None
        writers = _writers(contract, eid)
        if writers:
            errors.append(
                f"{where}.runtime is for a table no forge build writes, and build "
                f"{writers[0].get('id')} writes {eid}: its runtime and role run the "
                "maintenance (drop runtime, or name the build with build)"
            )
            return schedule, timezone, None, None
        _check_maintenance_names(contract, eid, where, errors)
        return (
            schedule,
            timezone,
            None,
            _read_runtime(contract, raw.get("runtime"), eid, where, errors),
        )
    writers = _writers(contract, eid)
    named = raw.get("build")
    if named is not None:
        writers = [b for b in writers if str(b.get("id")) == str(named)]
        if not writers:
            errors.append(
                f"{where}.build must name an engine: spark build whose output is {eid}: its "
                "runtime and role run the maintenance"
            )
            return schedule, timezone, None, None
    elif len(writers) != 1:
        errors.append(
            f"{where}: backend spark runs on the runtime and role of the engine: spark build "
            f"that writes {eid}, and "
            + (
                "none does (properties.parameters.spark, outputs[0]); a table no forge build "
                "writes names its runtime with runtime"
                if not writers
                else f"{len(writers)} do; name one with build"
            )
        )
        return schedule, timezone, None, None
    build = writers[0]
    _check_maintenance_names(contract, eid, where, errors)
    if (spark_parameters(build) or {}).get("runtime") == "glue":
        errors.append(
            f"{where}: build {build.get('id')} runs on AWS Glue, whose one job is the build's "
            "own (one run at a time), so a maintenance run would wait for it; use backend glue "
            "(the Glue Data Catalog's table optimizers)"
        )
    return schedule, timezone, str(build.get("id")), None


def _check_maintenance_names(
    contract: Mapping[str, Any], eid: str, where: str, errors: List[str]
) -> None:
    """The names a backend-spark table's maintenance takes after its expose (its DAG
    ``maintain_<expose>``, its plan action, and for a table no forge build writes its artifacts):
    an id they take, held by no build."""
    from fluid_build.build_runners._ids import IdentifierViolation, validate_identifier

    try:
        validate_identifier(eid, kind="exposeId")
    except IdentifierViolation:
        errors.append(
            f"{where}: backend spark names the table's maintenance DAG and run after the "
            f"expose, and {eid!r} is not an id they take (a letter or '_' first, then letters, "
            "digits, '_', '.' or '-')"
        )
    clash = _plain(f"maintain_{eid}")
    named_like = [
        str(b.get("id"))
        for b in contract.get("builds") or []
        if isinstance(b, Mapping) and _plain(str(b.get("id"))) == clash
    ]
    if named_like:
        errors.append(
            f"{where}: build {named_like[0]} has the name this table's maintenance takes "
            f"(its plan action spark_maintain_{eid} and its DAG maintain_{eid}); rename the build"
        )


def _read_glue_placement(
    raw: Mapping[str, Any], expose: Mapping[str, Any], platform: str, where: str, errors: List[str]
) -> Tuple[Optional[str], int]:
    """``(role arn, run every hours)`` of a backend-glue block."""
    from fluid_build.providers._iceberg_catalog import is_glue_cataloged

    for key in ("schedule", "timezone", "build", "runtime"):
        if key in raw:
            errors.append(f"{where}.{key} is backend spark's: Glue runs its optimizers itself")
    role_arn = raw.get("roleArn")
    if not isinstance(role_arn, str) or not _ROLE_ARN.match(role_arn):
        errors.append(
            f"{where}.roleArn is required on backend glue: the IAM role Glue's optimizers run "
            "as (arn:aws:iam::<account>:role/<name>)"
        )
        role_arn = None
    hours = raw.get("runEveryHours", 24)
    if not _whole(hours, 3, 168):
        errors.append(f"{where}.runEveryHours must be a whole number of hours from 3 to 168")
        hours = 24
    if platform != "aws" or not is_glue_cataloged(_mapping(expose.get("binding"))):
        errors.append(
            f"{where}: backend glue maintains a table of the Glue Data Catalog (platform aws, "
            "no other location.catalog); use backend spark"
        )
    return role_arn, int(hours)


def read_table(
    contract: Mapping[str, Any], expose: Mapping[str, Any], raw: Any, errors: List[str]
) -> Optional[TableMaintenance]:
    """One expose's ``iceberg.maintenance``, read; None (with ``errors``) when it cannot run."""
    eid = str(expose.get("exposeId"))
    where = f"exposes[{eid}].iceberg.maintenance"
    before = len(errors)
    if not isinstance(raw, Mapping):
        errors.append(f"{where} must be a mapping")
        return None
    _check_keys(raw, _BLOCK_KEYS, where, errors)
    database, table, platform = _iceberg_location(expose)
    if database is None or table is None:
        errors.append(
            f"{where}: maintenance is an Iceberg table's (binding.format: iceberg, with "
            "location.database and location.table)"
        )
        return None
    backend = raw.get("backend", BACKENDS[0])
    if backend not in BACKENDS:
        errors.append(f"{where}.backend must be one of {', '.join(BACKENDS)}")
        return None
    iceberg = _mapping(expose.get("iceberg"))
    legacy_compaction = iceberg.get("compaction")
    legacy_retention = iceberg.get("snapshotRetention")
    compaction = _read_compaction(
        raw.get("compaction"),
        backend,
        legacy_compaction if isinstance(legacy_compaction, Mapping) else {},
        f"{where}.compaction",
        errors,
    )
    expire = _read_expire(
        raw.get("expireSnapshots"),
        backend,
        legacy_retention if isinstance(legacy_retention, Mapping) else {},
        f"{where}.expireSnapshots",
        errors,
    )
    orphans = _read_orphans(
        raw.get("removeOrphanFiles"), backend, f"{where}.removeOrphanFiles", errors
    )
    if orphans is not None:
        scope = _orphan_scope_problem(contract, expose)
        if scope:
            errors.append(f"{where}.removeOrphanFiles: {scope}")
    manifests = _block(
        raw.get("rewriteManifests"), _MANIFEST_KEYS, f"{where}.rewriteManifests", errors
    )
    if manifests is not None and backend == "glue":
        errors.append(
            f"{where}.rewriteManifests: Glue's optimizers rewrite no manifests; use backend spark"
        )
    actions = _read_actions(raw.get("actions"), backend, f"{where}.actions", errors)
    if not (compaction or expire or orphans or manifests is not None or actions):
        errors.append(
            f"{where} declares nothing to run: add compaction, rewriteManifests, "
            "expireSnapshots, removeOrphanFiles or actions"
        )
    schedule, timezone, build_id, role_arn, hours = None, "UTC", None, None, 24
    runtime: Optional[Dict[str, Any]] = None
    if backend == "spark":
        schedule, timezone, build_id, runtime = _read_spark_placement(
            contract, raw, eid, where, errors
        )
        if "runtime" in raw:
            _check_unwritten_table(raw, expose, where, errors)
    else:
        role_arn, hours = _read_glue_placement(raw, expose, platform, where, errors)
        if "runEveryHours" in raw and expire is None and orphans is None:
            errors.append(
                f"{where}.runEveryHours paces Glue's retention and orphan file deletion "
                "optimizers, and neither is declared (compaction runs when Glue sees fit)"
            )
    if len(errors) > before:
        return None
    return TableMaintenance(
        expose_id=eid,
        backend=str(backend),
        database=database,
        table=table,
        schedule=schedule,
        timezone=timezone,
        build_id=build_id,
        runtime=runtime,
        role_arn=role_arn,
        run_every_hours=hours,
        compaction=compaction,
        rewrite_manifests=manifests is not None,
        expire=expire,
        orphans=orphans,
        actions=actions,
    )


def _check_unwritten_table(
    raw: Mapping[str, Any], expose: Mapping[str, Any], where: str, errors: List[str]
) -> None:
    """What a table no forge build writes needs beyond a build's: forge cannot see its
    writer's commits, so removing orphan files there names its ``olderThan`` (no default).
    Expiring snapshots keeps at least the last one on every table (``_read_expire``). Its
    location is never under the roots forge keeps its own files in (``_fluid/``, which holds
    the runner and the job config, and ``_checkpoints/``)."""
    from fluid_build.iac.providers.aws_ingest import _RESERVED_ROOTS

    found = _object_location(expose)
    if found is not None and found[1] and found[1][0] in _RESERVED_ROOTS:
        errors.append(
            f"exposes[{expose.get('exposeId')}].binding.location.path starts with "
            f"{found[1][0]}/, a root forge keeps its own files in (the runner and the "
            "maintenance job config sit under _fluid/, checkpoints under _checkpoints/), and "
            "maintenance of a table there could rewrite or delete them; give the table a "
            "path of its own"
        )
    orphans = raw.get("removeOrphanFiles")
    if _enabled(orphans) and orphans.get("olderThan") is None:
        errors.append(
            f"{where}.removeOrphanFiles.olderThan is required with runtime: forge does not "
            "write the table and cannot see how long its writer's commits take, and a file "
            "younger than that is not an orphan yet; set it longer than the writer's longest "
            "commit delay (Iceberg's default is P3D)"
        )


def maintenance_warnings(tables: List[TableMaintenance]) -> List[str]:
    """What ``fluid validate`` warns about in tables that read clean: removing orphan files
    younger than :data:`WARN_ORPHAN_SECONDS` from a table no forge build writes, whose writer's
    commit delay forge cannot see."""
    out: List[str] = []
    for tm in tables:
        if tm.runtime is not None and tm.orphans is not None:
            if tm.orphans.older_than_seconds < WARN_ORPHAN_SECONDS:
                out.append(
                    f"exposes[{tm.expose_id}].iceberg.maintenance.removeOrphanFiles.olderThan "
                    "is under P3D on a table forge does not write: a file its writer has not "
                    "committed yet (a slow micro-batch, a commit retried after a conflict) is "
                    "deleted as an orphan and the table loses it; keep it longer than that "
                    "writer's longest commit delay"
                )
    return out


def read_maintenance(contract: Mapping[str, Any]) -> Tuple[List[TableMaintenance], List[str]]:
    """``(tables, errors)``: every expose that declares ``iceberg.maintenance``, read and
    checked against the contract (the build that runs it, each backend's limits). Plugin
    actions are not loaded here (:func:`fluid_build.maintenance.job.action_statements`). A
    contract that declares none has none."""
    tables: List[TableMaintenance] = []
    errors: List[str] = []
    for expose in contract.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        iceberg = expose.get("iceberg")
        if not isinstance(iceberg, Mapping) or iceberg.get("maintenance") is None:
            continue
        found = read_table(contract, expose, iceberg["maintenance"], errors)
        if found is not None:
            tables.append(found)
    return tables, errors


def _runtime_name(runtime: Any) -> Any:
    return runtime.get("runtime") if isinstance(runtime, Mapping) else None


def declares_unwritten_maintenance(
    contract: Mapping[str, Any], *, remote_only: bool = False
) -> bool:
    """Whether a backend-spark ``iceberg.maintenance`` names a ``runtime``: a table no forge
    build writes, maintained on a runtime of its own (no validation). ``remote_only`` leaves
    out a local runtime, which OpenTofu has nothing to emit for."""
    for expose in contract.get("exposes") or []:
        iceberg = expose.get("iceberg") if isinstance(expose, Mapping) else None
        block = iceberg.get("maintenance") if isinstance(iceberg, Mapping) else None
        if (
            isinstance(block, Mapping)
            and block.get("backend", "spark") == "spark"
            and block.get("runtime") is not None
            and not (remote_only and _runtime_name(block["runtime"]) == "local")
        ):
            return True
    return False


def declares_maintenance(contract: Mapping[str, Any]) -> bool:
    """Whether any expose declares ``iceberg.maintenance`` (no validation)."""
    return any(
        isinstance(e, Mapping)
        and isinstance(e.get("iceberg"), Mapping)
        and e["iceberg"].get("maintenance") is not None
        for e in contract.get("exposes") or []
    )


def spark_tables(contract: Mapping[str, Any]) -> List[TableMaintenance]:
    """The backend-spark tables, for a renderer that runs only after ``fluid validate``.

    Raises:
        ValueError: the first problem :func:`read_maintenance` finds.
    """
    tables, errors = read_maintenance(contract)
    if errors:
        raise ValueError(errors[0])
    return [t for t in tables if t.backend == "spark"]


def option_text(options: Mapping[str, Any]) -> Dict[str, Any]:
    """``options`` as plain JSON values (a copy), for the plugin and the job config."""
    import json

    return dict(json.loads(json.dumps(dict(options), sort_keys=True)))


__all__ = [
    "ACTION_KEYS",
    "BACKENDS",
    "Compaction",
    "CustomAction",
    "DEFAULT_EXPIRE_SECONDS",
    "DEFAULT_ORPHAN_SECONDS",
    "DEFAULT_RETAIN_LAST",
    "ExpireSnapshots",
    "MIN_ORPHAN_SECONDS",
    "EMR_RUNTIMES",
    "RUNTIMES",
    "RemoveOrphanFiles",
    "STRATEGIES",
    "TableMaintenance",
    "WARN_ORPHAN_SECONDS",
    "declares_maintenance",
    "declares_unwritten_maintenance",
    "maintenance_warnings",
    "option_problems",
    "option_text",
    "parse_duration",
    "read_maintenance",
    "read_table",
    "runtime_build",
    "runtime_findings",
    "spark_tables",
    "table_location",
]
