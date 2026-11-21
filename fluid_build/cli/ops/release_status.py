# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid runs status <product> --release-dir DIR``: which release was in force, and so which
version of the code ran.

A job that Forge does not run (an existing job on its own runner, scheduled by Airflow) leaves
no run record here. What it leaves is the sealed release ``fluid ship --release-dir DIR``
publishes at each deploy: ``release.json`` (``fluid-release/1``), written last into a directory
that is renamed into place and never changed. This reads those records and answers "which release
of this product was in force on that date": its version(s), the revision of the code the CI built
it from, the run that sealed it. Each environment is answered on its own: a dev release sealed
between two prod ones is not what was in force in prod.

The records are the reader's own files, not trusted input: a directory is read one level deep,
a link is never followed (the index is opened with ``O_NOFOLLOW`` and checked on the open handle),
an index is read up to :data:`MAX_INDEX_BYTES`, one that does not parse or is another product's is
skipped (and counted), and every text that comes out of a record is cleaned of control characters
and cut, so a record cannot rewrite the terminal. Nothing authenticates a record: ``intact`` says
its ``identity_digest`` matches its ``identity``, which anyone who writes a record can arrange;
the sealed time and the source are not in that digest at all.

Standard library only at module scope (``fluid --help`` imports the ops commands).
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

#: Release directories read in one directory, and bytes of one ``release.json``.
MAX_ENTRIES = 2_000
MAX_INDEX_BYTES = 1_048_576
#: Characters of any text taken from a record.
_TEXT = 200
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
#: What a reader of a record is told about it, once, under an answer.
NOT_AUTHENTICATED = (
    "Records are read as written. 'intact' means a record's identity_digest matches its identity; "
    "it does not say who wrote the record, and its sealed time and source are outside that digest."
)


def clean(value: Any, limit: int = _TEXT) -> str:
    """``value`` as text with every non-printable character (a control character, a bidirectional
    mark, a line separator) replaced by ``?``, and cut at ``limit``."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass
class Release:
    """One sealed release of a product, as its ``release.json`` says."""

    path: str
    product_id: str
    sealed_at: datetime
    release_id: str
    expose_versions: Dict[str, str] = field(default_factory=dict)
    environment: Optional[str] = None
    forge_version: Optional[str] = None
    run_id: Optional[str] = None
    source: Dict[str, str] = field(default_factory=dict)
    bundle_digest: Optional[str] = None
    #: False when ``identity_digest`` is not the digest of ``identity``: the record was changed
    #: after it was sealed, or was never one. True proves little: whoever writes a record can
    #: compute the digest, and the sealed time and the source are outside it.
    intact: bool = True

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["sealed_at"] = self.sealed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return {k: v for k, v in out.items() if v not in (None, {}, "")}


@dataclass
class Found:
    """What a scan of the release directories found for one product."""

    releases: List[Release] = field(default_factory=list)
    #: Why a directory under a root was not a release of this product, counted by reason.
    skipped: Dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def parse_day(text: str) -> Optional[date]:
    """``YYYY-MM-DD`` as a date, or None. Nothing else: ``date.fromisoformat`` also takes
    ``20261114`` and week dates, depending on the Python version."""
    if not _DAY.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _moment(value: Any) -> Optional[datetime]:
    """An ISO 8601 time as UTC, or None."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)  # an offset can push year 1 or 9999 out of range
    except (ValueError, OverflowError):
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int) -> Optional[str]:
    """``value`` cleaned, when it is text; None for anything else (a record can hold any JSON,
    and a deeply nested value must not be turned into text)."""
    return clean(value, limit) if isinstance(value, str) and value else None


def _read_bounded(path: Path) -> Optional[bytes]:
    """The bytes of ``path``, or None: absent, a link (never followed: opened with
    ``O_NOFOLLOW``), not a regular file (checked on the open handle, so a FIFO or a device never
    blocks or runs on), or longer than :data:`MAX_INDEX_BYTES`."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = b""
        while len(data) <= MAX_INDEX_BYTES:
            chunk = os.read(fd, MAX_INDEX_BYTES + 1 - len(data))
            if not chunk:
                return data
            data += chunk
        return None  # longer than the limit
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_index(directory: Path) -> Optional[Dict[str, Any]]:
    """``directory/release.json`` as an object, or None: absent, a link, not a regular file, too
    big, not JSON or not an object."""
    import json

    data = _read_bounded(directory / "release.json")
    if data is None:
        return None
    try:
        parsed = json.loads(data)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _release(shown: str, index: Mapping[str, Any]) -> Optional[Release]:
    """The release ``index`` describes, or None when it is not a release record."""
    from fluid_build.cli import _ship_release as ship_release

    if index.get("schema") != ship_release.RELEASE_SCHEMA:
        return None
    identity, provenance = _mapping(index.get("identity")), _mapping(index.get("provenance"))
    sealed = _moment(provenance.get("finished_at")) or _moment(provenance.get("started_at"))
    product, digest = identity.get("product_id"), index.get("identity_digest")
    if sealed is None or not isinstance(product, str) or not isinstance(digest, str):
        return None
    try:
        intact = ship_release.identity_digest(identity) == digest
    except (TypeError, ValueError, RecursionError):
        intact = False
    versions = {
        clean(k, 80): clean(v, 80)
        for k, v in _mapping(identity.get("expose_versions")).items()
        if isinstance(k, str) and isinstance(v, str)
    }
    source = {
        key: clean(value, 256)
        for key, value in _mapping(provenance.get("source")).items()
        if key in ("revision", "repository", "ref") and isinstance(value, str)
    }
    return Release(
        path=clean(shown),
        product_id=clean(product, 120),
        sealed_at=sealed,
        release_id=clean(digest, 80),
        expose_versions=dict(list(versions.items())[:50]),
        environment=_text(identity.get("environment"), 40),
        forge_version=_text(identity.get("forge_version"), 40),
        run_id=_text(provenance.get("run_id"), 80),
        source=source,
        bundle_digest=_text(_mapping(identity.get("bundle")).get("manifest_digest"), 80),
        intact=intact,
    )


def _candidates(top: Path, root: str, found: Found) -> List[Tuple[Path, str]]:
    """The directories under ``root`` that may hold a release: ``root`` itself when it holds a
    ``release.json``, otherwise its subdirectories (one level, in name order, up to
    :data:`MAX_ENTRIES`; a link to a directory is not one)."""
    if (top / "release.json").exists():
        return [(top, root)]
    try:
        names = sorted(os.listdir(top))
    except OSError:
        found.skip("a directory that cannot be listed")
        return []
    if len(names) > MAX_ENTRIES:
        found.skip(f"entries past the first {MAX_ENTRIES:,} of a directory")
        names = names[:MAX_ENTRIES]
    out: List[Tuple[Path, str]] = []
    for name in names:
        if name.startswith(".") or ".partial-" in name:
            continue  # a run that did not publish
        child = top / name
        try:
            is_directory = stat.S_ISDIR(os.lstat(child).st_mode)  # a link is not one
        except OSError:
            continue
        if is_directory:
            out.append((child, f"{root.rstrip(os.sep)}{os.sep}{name}"))
    return out


def _release_in(directory: Path, shown: str, wanted: str, found: Found) -> Optional[Release]:
    """The release of ``wanted`` that ``directory`` holds, or None, with the reason counted in
    ``found``."""
    index = _read_index(directory)
    if index is None:
        found.skip("a directory with no readable release.json")
        return None
    release = _release(shown, index)
    if release is None:
        found.skip("a release.json that is not a fluid-release/1 record")
    elif release.product_id != wanted:
        found.skip("a release of another product")
    else:
        return release
    return None


def find_releases(roots: Sequence[str], product_id: str) -> Found:
    """The sealed releases of ``product_id`` under ``roots``, oldest first. A root that holds a
    ``release.json`` is one release; any other directory is read one level deep, in name order,
    up to :data:`MAX_ENTRIES` entries (the rest are counted as not read). A release reached by
    two roots, or by two spellings of one, is listed once.

    Raises:
        NotADirectoryError: a root is not a directory (a link to one is read as it points).
    """
    found = Found()
    seen: Set[Tuple[str, str]] = set()
    wanted = clean(product_id, 120)
    for root in roots:
        top = Path(root)
        if not top.is_dir():
            raise NotADirectoryError(f"--release-dir {clean(root)}: not a directory")
        for directory, shown in _candidates(top, root, found):
            release = _release_in(directory, shown, wanted, found)
            if release is None:
                continue
            key = (release.release_id, os.path.realpath(directory))
            if key not in seen:
                seen.add(key)
                found.releases.append(release)
    found.releases.sort(key=lambda r: (r.sealed_at, r.path, r.release_id))
    return found


@dataclass
class InForce:
    """The answer for a moment or a day, for one environment: the release in force when it
    began, and the ones sealed during it."""

    environment: Optional[str]
    start: datetime
    end: Optional[datetime]
    at_start: Optional[Release]
    during: List[Release]


def in_force(
    releases: Sequence[Release],
    on: Optional[date],
    now: datetime,
    environment: Optional[str] = None,
) -> InForce:
    """Which of ``releases`` (oldest first) was in force at ``now`` (``on`` is None), or on the
    UTC day ``on``: the latest sealed before the day began, and every one sealed during it. The
    caller picks the releases that answer together (:func:`in_force_by_environment`)."""
    if on is None:
        before = [r for r in releases if r.sealed_at <= now]
        return InForce(environment, now, None, before[-1] if before else None, [])
    start = datetime.combine(on, time.min, tzinfo=timezone.utc)
    try:
        end = start + timedelta(days=1)
    except OverflowError:  # the last day there is
        end = datetime.max.replace(tzinfo=timezone.utc)
    before = [r for r in releases if r.sealed_at < start]
    during = [r for r in releases if start <= r.sealed_at < end]
    return InForce(environment, start, end, before[-1] if before else None, during)


def in_force_by_environment(
    releases: Sequence[Release],
    on: Optional[date],
    now: datetime,
    environment: Optional[str] = None,
) -> List[InForce]:
    """One :func:`in_force` answer for each environment the releases name (the one without a
    recorded environment first), or only for ``environment``."""
    names: List[Optional[str]] = (
        [environment]
        if environment is not None
        else sorted(
            {r.environment for r in releases}, key=lambda name: (name is not None, name or "")
        )
    )
    return [in_force([r for r in releases if r.environment == n], on, now, n) for n in names]


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def describe(release: Release) -> List[str]:
    """The lines a release is shown as."""
    versions = ", ".join(f"{k} {v}" for k, v in release.expose_versions.items()) or "no version"
    lines = [f"  {versions}", f"  release  {release.release_id}"]
    sealed = f"  sealed   {_stamp(release.sealed_at)}"
    for label, value in (
        ("run", release.run_id),
        ("env", release.environment),
        ("forge", release.forge_version),
    ):
        if value:
            sealed += f"   {label} {value}"
    lines.append(sealed)
    code = " ".join(
        v for v in (release.source.get(k) for k in ("revision", "repository", "ref")) if v
    )
    lines.append(f"  code     {code}" if code else "  code     (the record names no source)")
    if release.bundle_digest:
        # what the Spark runner stamps on each commit as fluid.bundle-digest
        lines.append(f"  bundle   {release.bundle_digest}")
    if not release.intact:
        lines.append(
            "  NOT INTACT: its identity_digest is not the digest of its identity; the record "
            "was changed after it was sealed"
        )
    return lines


def _none_sealed(product: str, environment: Optional[str], on: Optional[date]) -> str:
    where = f" in environment {clean(environment, 40)}" if environment is not None else ""
    if on is None:
        return f"No release of {product}{where} has been sealed."
    return f"No release of {product}{where} was sealed by the end of {on.isoformat()}."


def _block(answer: InForce, on: Optional[date]) -> List[str]:
    """The lines of one environment's answer (none when nothing was in force or sealed)."""
    out: List[str] = []
    if answer.at_start:
        when = f"at the start of {on.isoformat()}" if on else "now"
        out += [f"In force {when}:", *describe(answer.at_start)]
    if answer.during and on:
        out.append(f"Sealed during {on.isoformat()}:")
        for release in answer.during:
            out += describe(release)
    return out


def report(
    product_id: str,
    answers: Sequence[InForce],
    found: Found,
    on: Optional[date],
    environment: Optional[str] = None,
) -> List[str]:
    """The text answer: one block per environment (headed when there are several);
    ``environment`` is the one the caller asked for, if any."""
    product = clean(product_id, 120)
    out = [f"{product}: {len(found.releases)} sealed release(s) read"]
    if not answers:
        out.append(_none_sealed(product, environment, on))
    for answer in answers:
        if len(answers) > 1:
            out.append(f"environment {answer.environment or '(none recorded)'}:")
        out += _block(answer, on) or [_none_sealed(product, environment, on)]
    for why, count in sorted(found.skipped.items()):
        out.append(f"not read: {count} x {why}")
    if found.releases:
        out.append(NOT_AUTHENTICATED)
    return out
