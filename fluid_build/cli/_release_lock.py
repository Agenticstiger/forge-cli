# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR --artifacts LOCK.yaml``: the external files a release seals.

A product can run code Forge does not build: a PySpark package that writes its own output, a
hand-written Airflow DAG, a file another job reads. The lock names each one, the sha256 of its
bytes and the commit of the repository it was built from::

    artifacts:
      - name: orders-jobs          # its name in the release: external/orders-jobs/<file>
        kind: spark-package        # spark-package (.whl, .zip), airflow-dag (.py, .zip) or file
        path: dist/orders_jobs-1.4.0-py3-none-any.whl  # relative to the lock's directory
        sha256: 9f86d081...        # 64 hex characters, as sha256sum prints them
        source:
          repo: https://git.example.com/data/orders-jobs
          commit: 4b825dc642cb6eb9a060e54bf8d69288fbee4904
          ref: refs/tags/v1.4.0    # optional

The run refuses a lock it cannot seal before any stage runs: an entry the format does not allow,
a path that is absolute, hidden or leads out of the lock's directory (by ``..`` or a link), a link,
or a file whose bytes are not the sha256 the lock names. The release then copies each file to ``external/<name>/<file>``, hashes the copy again,
and records the entries in its ``identity``, sorted by name and without the paths on this
machine, so two clean runs with the same lock and the same bytes give the same identity and a
changed file gives a new one. Each entry is shaped as a SLSA ``resolvedDependencies`` item
(``name``, ``digest``) whose ``source`` is the repository and commit, as ``_attestation.py``
records the product's own (``git+<repo>@<ref>``, ``digest.gitCommit``). The repository, commit
and ref pass the checks and the normalizer the CI's own source does in ``_ship_release``.

Standard library only at module scope (``fluid --help`` imports ``ship``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import _ship_release as release

#: What ``--artifacts`` reads when the flag is absent.
LOCK_ENV = "RELEASE_ARTIFACTS"
#: Where a release keeps the files: ``external/<name>/<file>``.
EXTERNAL_DIR = "external"
#: Entries one lock may list, and bytes of the lock itself.
MAX_ENTRIES = 256
MAX_LOCK_BYTES = 1_048_576
#: Each kind, and the file names it takes (none: any).
KINDS: Mapping[str, Tuple[str, ...]] = {
    "spark-package": (".whl", ".zip"),
    "airflow-dag": (".py", ".zip"),
    "file": (),
}
_TOP_KEYS = {"artifacts"}
_KEYS = {"name", "kind", "path", "sha256", "source"}
_SOURCE_KEYS = {"repo", "commit", "ref"}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_FILE = re.compile(r"[A-Za-z0-9._=+-]{1,255}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DRIVE = re.compile(r"[A-Za-z]:")
_MAX_PATH = 4096


class LockError(release.ReleaseError):
    """The lock cannot be sealed into a release: the run refuses it."""


@dataclass(frozen=True)
class LockEntry:
    """One entry of the lock, checked. ``path`` is as the lock writes it."""

    name: str
    kind: str
    path: str
    sha256: str
    repository: str
    commit: str
    ref: Optional[str] = None

    @property
    def release_path(self) -> str:
        """Where the release keeps the file."""
        return f"{EXTERNAL_DIR}/{self.name}/{PurePosixPath(self.path).name}"

    def identity(self) -> Dict[str, Any]:
        """The entry as the release's ``identity`` records it: no path on this machine."""
        uri = f"git+{self.repository}" + (f"@{self.ref}" if self.ref else "")
        return {
            "name": self.name,
            "kind": self.kind,
            "path": self.release_path,
            "digest": {"sha256": self.sha256},
            "source": {"uri": uri, "digest": {"gitCommit": self.commit}},
        }


@dataclass(frozen=True)
class ExternalArtifact:
    """An entry, and the file on this machine that holds its bytes."""

    entry: LockEntry
    local: Path


def parse_lock(data: Any, where: str) -> List[LockEntry]:
    """The lock's entries, checked and sorted by name. Pure: reads no file.

    Raises:
        LockError: a key, value or entry the release cannot seal.
    """
    if not isinstance(data, Mapping):
        raise LockError(f"{where}: expected a mapping with an artifacts list")
    unknown = sorted(str(k) for k in set(data) - _TOP_KEYS)
    if unknown:
        raise LockError(f"{where}: unsupported field(s) {unknown}")
    items = data.get("artifacts")
    if not isinstance(items, list) or not items:
        raise LockError(f"{where}: artifacts: expected a list of one entry or more")
    if len(items) > MAX_ENTRIES:
        raise LockError(f"{where}: artifacts: lists {len(items)} entries, at most {MAX_ENTRIES}")
    entries = [_entry(item, f"{where}: artifacts[{i}]") for i, item in enumerate(items)]
    seen: Dict[str, str] = {}
    for entry in entries:
        # One directory each: Orders and orders are one directory on many filesystems.
        key = entry.name.lower()
        if key in seen:
            raise LockError(
                f"{where}: two entries are named {seen[key]!r} and {entry.name!r}; a name holds "
                "one file's directory in the release, so each needs a name of its own"
            )
        seen[key] = entry.name
    return sorted(entries, key=lambda e: e.name)


def _entry(item: Any, where: str) -> LockEntry:
    if not isinstance(item, Mapping):
        raise LockError(f"{where}: expected a mapping")
    name = item.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise LockError(
            f"{where}.name: expected letters, digits and ._- (at most 100, starting with a "
            "letter or digit), the name of the file's directory in the release"
        )
    where = f"{where} ({name})"
    unknown = sorted(str(k) for k in set(item) - _KEYS)
    if unknown:
        raise LockError(f"{where}: unsupported field(s) {unknown}")
    missing = [key for key in sorted(_KEYS) if item.get(key) is None]
    if missing:
        raise LockError(f"{where}: {', '.join(missing)} required")
    kind = item["kind"]
    if not isinstance(kind, str) or kind not in KINDS:
        raise LockError(f"{where}.kind: expected one of {', '.join(KINDS)}")
    path = _path(item["path"], kind, where)
    sha = item["sha256"]
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise LockError(
            f"{where}.sha256: expected the file's sha256 as sha256sum prints it, "
            "64 lowercase hex characters"
        )
    repository, commit, ref = _source(item["source"], f"{where}.source")
    return LockEntry(name, kind, path, sha, repository, commit, ref)


def _path(value: Any, kind: str, where: str) -> str:
    """The entry's ``path``: relative, inside the lock's directory, through no hidden file or
    directory, every segment a name the release can hold, with the suffix its kind takes.

    Inside the lock's directory only: the lock is a file of the checkout that anyone who can
    change the checkout can change, so it must not name a file only the runner may read (a
    credential under the home directory, another checkout). Hidden names are refused for the same
    reason: a CI system keeps its token in the checkout's ``.git/config``."""
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH:
        raise LockError(f"{where}.path: expected the path of a file")
    if "\\" in value:
        raise LockError(f"{where}.path: expected / between directories, not \\")
    if value.startswith("/") or _DRIVE.match(value):
        raise LockError(
            f"{where}.path: {value!r} is absolute; a lock names files inside its own directory, "
            "by a path relative to it"
        )
    parts = PurePosixPath(value).parts
    if ".." in parts:
        raise LockError(f"{where}.path: {value!r} leads out of its directory (a .. segment)")
    for part in parts:
        if part.startswith("."):
            raise LockError(f"{where}.path: {value!r} names a hidden file or directory ({part})")
        if not _FILE.fullmatch(part):
            raise LockError(
                f"{where}.path: {part!r} has characters a release does not keep (letters, "
                "digits and ._=+- only in each segment)"
            )
    file = parts[-1]
    suffixes = KINDS[kind]
    if suffixes and not file.lower().endswith(suffixes):
        raise LockError(f"{where}.path: a {kind} is a {' or '.join(suffixes)} file")
    return value


def _source(value: Any, where: str) -> Tuple[str, str, Optional[str]]:
    """``(repository, commit, ref)``, checked and normalized as the CI's source is."""
    if not isinstance(value, Mapping):
        raise LockError(f"{where}: expected a mapping with repo, commit and an optional ref")
    unknown = sorted(str(k) for k in set(value) - _SOURCE_KEYS)
    if unknown:
        raise LockError(f"{where}: unsupported field(s) {unknown}")
    repo, commit, ref = value.get("repo"), value.get("commit"), value.get("ref")
    repository = release._repository(repo) if isinstance(repo, str) else None
    if repository is None:
        raise LockError(
            f"{where}.repo: expected the repository's https, http, ssh or git URL, or "
            "user@host:org/repo"
        )
    if "@" in repository:  # the source is recorded as git+<repo>@<ref>
        raise LockError(f"{where}.repo: a repository URL with @ in its path cannot be recorded")
    commit = commit.strip().lower() if isinstance(commit, str) else ""
    if not release._REVISION.fullmatch(commit):
        raise LockError(f"{where}.commit: expected the commit's hash, 7 to 64 hex characters")
    if ref is not None:
        ref = ref.strip() if isinstance(ref, str) else ""
        if not release._REF.fullmatch(ref):
            raise LockError(
                f"{where}.ref: expected a branch or tag name, such as refs/tags/v1.4.0 "
                "(letters, digits and ._/@+#-)"
            )
    return repository, commit, ref


def check_files(entries: Sequence[LockEntry], base: Path, where: str) -> List[ExternalArtifact]:
    """Each entry with its file, which must be a regular file (never a link) whose bytes have
    the sha256 the entry names. A relative path is read from ``base``, the lock's directory."""
    from fluid_build._contract_loader import CLIError
    from fluid_build.cli._attestation import _sha256_file
    from fluid_build.cli.security import validate_cli_path

    found: List[ExternalArtifact] = []
    root = base.resolve()
    for entry in entries:
        named = f"{where}: {entry.name}"
        local = base / entry.path
        try:
            validate_cli_path(local, mode="read", file_type="artifact", any_extension=True)
            if not local.resolve().is_relative_to(root):
                raise LockError(f"{named}: {entry.path} leads out of the lock's directory (a link)")
            release._regular_file(local)
            actual = _sha256_file(local)
        except CLIError as exc:
            raise LockError(f"{named}: {exc.message}") from None
        except LockError:
            raise
        except release.ReleaseError:
            raise LockError(
                f"{named}: {entry.path} is not a regular file (a link is not)"
            ) from None
        except (OSError, ValueError, RuntimeError) as exc:  # RuntimeError: a link loop
            why = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
            raise LockError(f"{named}: cannot read {entry.path}: {why}") from None
        if actual != entry.sha256:
            # Never the digest it has: a lock is no way to learn the hash of a file it may name.
            raise LockError(
                f"{named}: {entry.path} does not have the sha256 the lock names ({entry.sha256}); "
                f"the lock needs the one `sha256sum {entry.path}` prints"
            )
        found.append(ExternalArtifact(entry, local))
    return found


def load(lock: str) -> List[ExternalArtifact]:
    """The external files ``lock`` lists, each checked against its sha256.

    Raises:
        LockError: the lock cannot be read, or lists anything the release cannot seal.
    """
    import yaml

    from fluid_build._contract_loader import CLIError
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.util.safe_yaml import load_yaml_safe, read_bounded_text

    where = f"--artifacts {lock}"
    try:
        path = validate_cli_path(lock, mode="read", file_type="artifacts lock")
        data = load_yaml_safe(
            read_bounded_text(path, MAX_LOCK_BYTES), unique_keys=True, name=path.name
        )
    except CLIError as exc:
        raise LockError(f"{where}: {exc.message}") from None
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError, RecursionError) as exc:
        raise LockError(f"{where}: cannot read it: {exc}") from None
    return check_files(parse_lock(data, where), path.parent, where)
