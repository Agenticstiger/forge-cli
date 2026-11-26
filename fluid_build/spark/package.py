# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Spark build that runs the team's own prebuilt PySpark package, by its sha256 digest.

``builds[].properties.package`` (0.7.7 preview) names a ``.whl`` or ``.zip`` the team
published, its sha256 and the function to call. forge's runner owns everything around
that function, so the package computes rows and never writes:

- **The bytes.** ``fluid apply`` copies the package beside the job config, keyed by its
  digest (:func:`fluid_build.spark.names.package_key`): on AWS OpenTofu copies the S3 object
  only when S3's SHA-256 checksum of it is the pin (as the Glue jars are,
  ``iac/providers/aws_glue_jars.py``); on ``runtime: local`` forge copies the file after
  hashing it. The runner hashes what it reads again and imports only those bytes, from a
  copy of its own, as pip's hash-checking mode installs only a pinned artifact.
- **The call.** The entry point is an entry-point object reference (the packaging spec's
  ``importable.module:object.attr``), called as ``fn(spark, context)`` and returning one
  DataFrame, as a dbt Python model's ``model(dbt, session)`` returns the DataFrame dbt writes.
- **The checks and the write.** The job config carries the output's declared columns and
  types (``package.schema``) and the query that gives each row its dead-letter reason
  (``sql.check``: a required value that is null, at any depth, with
  :func:`fluid_build.spark.dead_letter.output_check_select`), as a stream's does. The runner
  writes the passing rows with ``overwritePartitions``, as a SQL batch build does, and the
  rest to the dead letters (:data:`fluid_build.spark.dead_letter.BATCH_COLUMNS`).
- **Where packages come from.** The operator lists the places in
  :data:`PACKAGE_ALLOWLIST_ENV`; a package's code runs inside the job with its role, so an
  unset list allows none (``spark/policy.py``).

Pure functions and the standard library: compiled by forge-cli, never by the runner.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from fluid_build.providers._sql_safety import validate_ident

#: Operator-only: where a build's prebuilt package may come from, comma-separated whole
#: prefixes of its ``uri``: ``s3://<bucket>/<prefix>/`` (the team's write-once artifact
#: store) or ``file:///<dir>/`` for ``runtime: local``. Unset allows none.
PACKAGE_ALLOWLIST_ENV = "FLUID_SPARK_PACKAGE_ALLOWLIST"
#: The largest package forge copies and the runner reads.
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
#: The keys of ``properties.package`` and of its ``deadLetter``.
_KEYS = {"uri", "sha256", "entryPoint", "arguments", "deadLetter"}
_DEAD_KEYS = {"enabled", "table", "maxRecordsBeforeAbort"}
DEFAULT_MAX_RECORDS = 10_000
_SEGMENT = r"(?:(?!\.{1,2}/)[A-Za-z0-9._=+-]+/)*[A-Za-z0-9._=+-]+\.(?:whl|zip)"
_S3_URI = re.compile(rf"s3://(?P<bucket>[a-z0-9][a-z0-9.-]{{1,61}}[a-z0-9])/(?P<key>{_SEGMENT})")
_FILE_URI = re.compile(rf"file://(?P<path>/{_SEGMENT})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ENTRY_POINT = re.compile(rf"{_NAME}(?:\.{_NAME})*:{_NAME}(?:\.{_NAME})*")
_ARGUMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")
_MAX_ARGUMENTS, _MAX_ARGUMENT = 64, 4096


@dataclass(frozen=True)
class PackageSpec:
    """``properties.package``, checked."""

    uri: str
    sha256: str
    entry_point: str
    arguments: Mapping[str, str]
    #: ``{"table", "maxRecordsBeforeAbort"}``, or None without dead letters.
    dead_letter: Optional[Mapping[str, Any]]

    @property
    def bucket(self) -> Optional[str]:
        match = _S3_URI.fullmatch(self.uri)
        return match["bucket"] if match else None

    @property
    def key(self) -> Optional[str]:
        match = _S3_URI.fullmatch(self.uri)
        return match["key"] if match else None

    @property
    def path(self) -> Optional[str]:
        """The file of a ``file://`` package, as this machine names it."""
        match = _FILE_URI.fullmatch(self.uri)
        return match["path"] if match else None

    @property
    def sha256_b64(self) -> str:
        """The digest as S3 reports ``checksum_sha256``: the base64 of its bytes."""
        from fluid_build.util.s3 import sha256_b64

        return sha256_b64(self.sha256)


def declared(build: Mapping[str, Any]) -> Optional[Any]:
    """``properties.package`` as the build declares it, or None."""
    props = build.get("properties")
    return props.get("package") if isinstance(props, Mapping) else None


def parse_package(spec: Any, runtime: Any, where: str) -> PackageSpec:
    """``properties.package`` of a build on ``runtime``, checked as the schema does, plus
    what the schema cannot say: an AWS runtime reads an ``s3://`` package, a local run a
    ``file:///`` one.

    Raises:
        ValueError: a key or value the runner cannot run (for a caller that skips the
            schema).
    """
    where = f"{where}: properties.package"
    if not isinstance(spec, Mapping):
        raise ValueError(f"{where}: expected an object")
    unknown = sorted(str(k) for k in set(spec) - _KEYS)
    if unknown:
        raise ValueError(f"{where}: unsupported field(s) {unknown}")
    for key in ("uri", "sha256", "entryPoint"):
        if spec.get(key) is None:
            raise ValueError(f"{where}.{key} is required")
    uri, sha, entry = spec["uri"], spec["sha256"], spec["entryPoint"]
    local = runtime == "local"
    pattern, form = (
        (_FILE_URI, "file:///<path>/<name>.whl or .zip, a file on the machine fluid apply runs on")
        if local
        else (_S3_URI, "s3://<bucket>/<key>.whl or .zip")
    )
    if not isinstance(uri, str) or len(uri) > 1024 or not pattern.fullmatch(uri):
        raise ValueError(
            f"{where}.uri: expected {form} (letters, digits and ._=+- in each segment, no . or "
            f".. segment)"
            + (
                "; runtime local reads the package from this machine"
                if local
                else "; the AWS runtimes read the package from S3"
            )
        )
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise ValueError(
            f"{where}.sha256: expected the package's sha256 as sha256sum prints it, "
            "64 lowercase hex characters"
        )
    if not isinstance(entry, str) or len(entry) > 512 or not _ENTRY_POINT.fullmatch(entry):
        raise ValueError(
            f"{where}.entryPoint: expected <module>:<attribute>, an entry-point object "
            "reference such as orders_pkg.jobs:merge_orders"
        )
    return PackageSpec(
        uri=uri,
        sha256=sha,
        entry_point=entry,
        arguments=_arguments(spec.get("arguments"), where),
        dead_letter=_dead_letter(spec.get("deadLetter"), where),
    )


def _arguments(value: Any, where: str) -> Mapping[str, str]:
    if value is None:
        return {}
    if (
        not isinstance(value, Mapping)
        or len(value) > _MAX_ARGUMENTS
        or not all(
            isinstance(k, str)
            and _ARGUMENT.fullmatch(k)
            and isinstance(v, str)
            and len(v) <= _MAX_ARGUMENT
            for k, v in value.items()
        )
    ):
        raise ValueError(
            f"{where}.arguments: expected at most {_MAX_ARGUMENTS} strings, name to value "
            f"(names of letters, digits and _.-, values of at most {_MAX_ARGUMENT} characters)"
        )
    return dict(sorted(value.items()))


def _dead_letter(value: Any, where: str) -> Optional[Mapping[str, Any]]:
    if value is None:
        return None
    where = f"{where}.deadLetter"
    if not isinstance(value, Mapping):
        raise ValueError(f"{where}: expected an object")
    unknown = sorted(str(k) for k in set(value) - _DEAD_KEYS)
    if unknown:
        raise ValueError(
            f"{where}: unsupported field(s) {unknown}; a batch build's dead letters are a table"
        )
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}.enabled: expected true or false")
    if not enabled:
        return None
    table = value.get("table")
    if table is not None:
        try:
            validate_ident(table)
        except (TypeError, ValueError):
            raise ValueError(f"{where}.table: expected a table name") from None
    cap = value.get("maxRecordsBeforeAbort", DEFAULT_MAX_RECORDS)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ValueError(f"{where}.maxRecordsBeforeAbort: expected a whole number from 0")
    return {"table": table, "maxRecordsBeforeAbort": cap}


def _listed_prefixes(environ: Optional[Mapping[str, str]]) -> list[str]:
    raw = (os.environ if environ is None else environ).get(PACKAGE_ALLOWLIST_ENV) or ""
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def store_prefix(uri: str) -> str:
    """The prefix to suggest listing for ``uri``: its folder, above the first segment that
    names a digest (``sha256-<hex>/``, as a content-addressed store lays releases out), so
    one entry covers every release's package rather than this one's."""
    head, _, _ = uri.rpartition("/")
    scheme, _, path = head.partition("://")
    segments = path.split("/")
    for index, segment in enumerate(segments[1:], 1):
        if re.search(r"(?:^|[^0-9a-f])[0-9a-f]{32,64}(?:$|[^0-9a-f])", segment):
            return f"{scheme}://{'/'.join(segments[:index])}/"
    return head + "/"


def listed(uri: str, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the operator lists ``uri``'s place: an entry is a whole prefix of it, segment
    by segment (``s3://b/code`` covers ``s3://b/code/x.whl``, never ``s3://b/codex.whl``), or
    the object itself."""
    for entry in _listed_prefixes(environ):
        if not entry.startswith(("s3://", "file:///")):
            continue
        if uri == entry or uri.startswith(entry if entry.endswith("/") else entry + "/"):
            return True
    return False
