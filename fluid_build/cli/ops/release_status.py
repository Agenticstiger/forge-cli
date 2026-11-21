# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid runs status <product> --release-dir DIR``: which release was in force, and so which
version of the code ran.

A job that Forge does not run (an existing job on its own runner, scheduled by Airflow) leaves
no run record here. What it leaves is the sealed release ``fluid ship --release-dir DIR``
publishes at each deploy: ``release.json`` (``fluid-release/1``), written last into a directory
that is renamed into place and never changed. This reads those records and answers "which release
of this product was in force on that date": its version(s), the revision of the code the CI built
it from, the run that sealed it.

The records are the reader's own files, not trusted input: a directory is read one level deep,
a link is never followed, an index is read up to :data:`MAX_INDEX_BYTES`, one that does not parse
or is another product's is skipped (and counted), and every text that comes out of a record is
cleaned of control characters and cut, so a record cannot rewrite the terminal.

Standard library only at module scope (``fluid --help`` imports the ops commands).
"""

from __future__ import annotations

import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: Release directories read in one directory, and bytes of one ``release.json``.
MAX_ENTRIES = 2_000
MAX_INDEX_BYTES = 1_048_576
#: Characters of any text taken from a record.
_TEXT = 200


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
    #: after it was sealed, or was never one.
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


def _moment(value: Any) -> Optional[datetime]:
    """An ISO 8601 time as UTC, or None."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_index(directory: Path) -> Optional[Dict[str, Any]]:
    """``directory/release.json`` as an object, or None: absent, not a regular file, too big,
    not JSON or not an object. A link is not followed."""
    import json

    from fluid_build.util.safe_yaml import read_bounded_file

    path = directory / "release.json"
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        data = read_bounded_file(path, MAX_INDEX_BYTES)
        parsed = json.loads(data) if data is not None else None
    except (OSError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _release(directory: Path, shown: str, index: Mapping[str, Any]) -> Optional[Release]:
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
    bundle = _mapping(identity.get("bundle")).get("manifest_digest")
    return Release(
        path=clean(shown),
        product_id=clean(product, 120),
        sealed_at=sealed,
        release_id=clean(digest, 80),
        expose_versions=dict(list(versions.items())[:50]),
        environment=clean(identity["environment"], 40) if identity.get("environment") else None,
        forge_version=(
            clean(identity["forge_version"], 40) if identity.get("forge_version") else None
        ),
        run_id=clean(provenance["run_id"], 80) if provenance.get("run_id") else None,
        source=source,
        bundle_digest=clean(bundle, 80) if isinstance(bundle, str) else None,
        intact=intact,
    )


def find_releases(roots: Sequence[str], product_id: str) -> Found:
    """The sealed releases of ``product_id`` under ``roots``. A root that holds a
    ``release.json`` is one release; any other directory is read one level deep, in name order,
    up to :data:`MAX_ENTRIES` entries (the rest are counted as not read).

    Raises:
        NotADirectoryError: a root is not a directory (a link to one is read as it points).
    """
    found = Found()
    seen = set()
    for root in roots:
        top = Path(root)
        if not top.is_dir():
            raise NotADirectoryError(f"--release-dir {clean(root)}: not a directory")
        candidates: List[Tuple[Path, str]] = []
        if (top / "release.json").exists():
            candidates.append((top, root))
        else:
            try:
                names = sorted(os.listdir(top))
            except OSError:
                found.skip("a directory that cannot be listed")
                continue
            if len(names) > MAX_ENTRIES:
                found.skip(f"entries past the first {MAX_ENTRIES:,} of a directory")
                names = names[:MAX_ENTRIES]
            for name in names:
                if name.startswith(".") or ".partial-" in name:
                    continue  # a run that did not publish
                child = top / name
                try:
                    is_directory = stat.S_ISDIR(os.lstat(child).st_mode)  # a link is not one
                except OSError:
                    continue
                if is_directory:
                    candidates.append((child, f"{root.rstrip(os.sep)}{os.sep}{name}"))
        for directory, shown in candidates:
            index = _read_index(directory)
            if index is None:
                found.skip("a directory with no readable release.json")
                continue
            release = _release(directory, shown, index)
            if release is None:
                found.skip("a release.json that is not a fluid-release/1 record")
            elif release.product_id != product_id:
                found.skip("a release of another product")
            elif (release.release_id, release.path) not in seen:
                seen.add((release.release_id, release.path))
                found.releases.append(release)
    found.releases.sort(key=lambda r: (r.sealed_at, r.release_id))
    return found


@dataclass
class InForce:
    """The answer for a moment or a day: the release in force when it began, and the ones
    sealed during it."""

    start: datetime
    end: Optional[datetime]
    at_start: Optional[Release]
    during: List[Release]


def in_force(releases: Sequence[Release], on: Optional[date], now: datetime) -> InForce:
    """Which release was in force at ``now`` (``on`` is None), or on the UTC day ``on``: the latest
    sealed before the day began, and every one sealed during it."""
    if on is None:
        before = [r for r in releases if r.sealed_at <= now]
        return InForce(now, None, before[-1] if before else None, [])
    start = datetime.combine(on, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    before = [r for r in releases if r.sealed_at < start]
    during = [r for r in releases if start <= r.sealed_at < end]
    return InForce(start, end, before[-1] if before else None, during)


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
    if not release.intact:
        lines.append(
            "  NOT INTACT: its identity_digest is not the digest of its identity; the record "
            "was changed after it was sealed"
        )
    return lines


def report(product_id: str, answer: InForce, found: Found, on: Optional[date]) -> List[str]:
    """The text answer."""
    out = [f"{clean(product_id, 120)}: {len(found.releases)} sealed release(s) read"]
    when = f"on {on.isoformat()}" if on else "now"
    if answer.at_start:
        out += [
            f"In force {when if on is None else 'at the start of ' + on.isoformat()}:",
            *describe(answer.at_start),
        ]
    elif not answer.during:
        out.append(f"No release of {clean(product_id, 120)} was sealed on or before {when}.")
    if answer.during:
        out.append(f"Sealed during {on.isoformat() if on else 'the day'}:")
        for release in answer.during:
            out += describe(release)
    for why, count in sorted(found.skipped.items()):
        out.append(f"not read: {count} x {why}")
    return out
