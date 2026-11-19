# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The stage journal ``fluid ship`` keeps per release and target.

The journal is on with ``--journal-dir DIR`` or ``--release-dir`` (which keeps
it in ``.fluid/ship-journal``). The target is the product and the environment
(``DIR/<product id>/<env>/``); each release in it, named by the bundle's
digest, has one append-only JSON Lines file (``<digest hex>.jsonl``). Every
stage leaves two events: ``started``, written and fsynced before the stage
runs, and ``finished``, with its status, exit code and receipts (each report
file the attempt wrote and its digest, the plan's digests, the local OpenTofu
states' lineage and serial). Nothing is rewritten: a rerun adds attempts, as
a Step Functions redrive stays in the execution's history, and a line cut off
by a crash is ignored, as SQLite's WAL ignores an uncommitted frame. One run
at a time holds a target: an advisory lock on ``DIR/<product>/<env>/.lock``.

Before a stage that changes the target runs (apply, except in dry run;
policy-apply, except in check mode; publish; schedule-sync, except in dry
run), ship reads what earlier attempts left:

* an attempt of that stage, in any release of the target, that started and
  never finished or finished ``unknown`` (ship interrupted or killed, the
  stage killed by a signal, a CI timeout): what it changed is not known. The
  stage is refused until the operator has inspected the target and passes
  ``--acknowledge-inspected STAGE``, which the journal records. An unknown
  apply blocks policy-apply, publish and schedule-sync the same way: they
  would act on a target nobody has looked at;
* an apply in this release that did not pass: it may have changed part of the
  target, so the plan it ran is stale. Apply is refused until stage 6 has
  planned again through ship and ``runtime/plan.json`` is that plan;
* anything else reruns. A stage that changes nothing always reruns.

Nothing is rolled back: recovery goes forward (inspect, plan again, apply
again), never through an inverse nobody can prove.

The journal is evidence only where the checkout cannot write it: keep DIR
outside the checkout and out of reach of runs at a lower trust level (a CI
cache a pull request can fill is such a reach). In the checkout (the
``--release-dir`` default) it guards a working copy against its own reruns.
Ship never follows a symbolic link below DIR (or below the project, for the
default), reads only regular files with one link, and refuses a journal
file larger than :data:`MAX_FILE_BYTES`.

Standard library only at module scope (``fluid --help`` imports ``ship``).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Dict, Iterator, List, Optional, Sequence, Tuple

from . import _ship_stages as st

JOURNAL_SCHEMA = "fluid-ship-journal/1"
DEFAULT_JOURNAL_DIR = ".fluid/ship-journal"
LOCK_NAME = ".lock"

#: The outcome of an attempt that started and has no known end.
UNKNOWN = "unknown"

#: A journal file ship wrote stays far below this (an event is well under
#: 4 KiB); anything larger is refused rather than read.
MAX_FILE_BYTES = 8 << 20
#: All the release files of one target together.
MAX_TARGET_BYTES = 64 << 20

#: The reports each stage writes, recorded with their digest when this attempt
#: wrote them.
_STAGE_FILES: Dict[int, Sequence[str]] = {
    1: (st.BUNDLE_PATH,),
    2: (st.VALIDATE_REPORT,),
    3: (st.ARTIFACTS_MANIFEST,),
    4: (st.VALIDATE_ARTIFACTS_REPORT,),
    5: ("runtime/diff-report.json",),
    6: (st.PLAN_PATH, st.PLAN_HTML_PATH),
    7: ("runtime/apply-report.html",),
    9: ("runtime/verify-report.json",),
    11: (st.SCHEDULE_SYNC_REPORT,),
}

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no newline translation
#: Directory descriptors (POSIX): every name below DIR is opened relative to
#: its parent's descriptor, so no component can be swapped for a link.
_DIR_FD = os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd and bool(_DIRECTORY)

#: Stages that can change the target (see :func:`side_effects` for when).
TARGET_STAGES = frozenset({st.APPLY_STAGE, 8, 10, 11})

#: Stages that act on what apply left, so an unknown apply blocks them too.
_AFTER_APPLY = frozenset({8, 10, 11})


class JournalError(Exception):
    """The journal cannot be opened, read or written: the stage must not run."""


def side_effects(num: int, argv: Sequence[str]) -> bool:
    """Whether stage ``num``, run as ``argv``, changes something outside the
    checkout: apply (not a dry run), policy-apply (not in check mode), publish,
    and schedule-sync (not a dry run)."""
    args = list(argv)
    mode = args[args.index("--mode") + 1] if "--mode" in args[:-1] else None
    if num == st.APPLY_STAGE:
        return mode != "dry-run"
    if num == 8:
        return mode != "check"
    if num == 10:
        return True
    if num == 11:
        return "--dry-run" not in args
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def bundle_digest(root: Path) -> Optional[str]:
    """The MANIFEST digest of ``runtime/bundle.tgz``, which names the release;
    ``None`` when there is no readable bundle."""
    import tarfile
    import zlib

    from fluid_build.forge.core.bundle import read_tar_member_bounded

    path = root / st.BUNDLE_PATH
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            manifest = json.loads(read_tar_member_bounded(tar, "MANIFEST.json").decode("utf-8"))
    except (OSError, ValueError, KeyError, EOFError, zlib.error, tarfile.TarError, RecursionError):
        return None  # a truncated gzip raises EOFError, a corrupt one zlib.error
    digest = manifest.get("digest") if isinstance(manifest, dict) else None
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    hex_part = digest[len("sha256:") :]
    if len(hex_part) != 64 or any(c not in "0123456789abcdef" for c in hex_part):
        return None
    return digest


def plan_digests(root: Path) -> Dict[str, str]:
    """``bundleDigest`` and ``planDigest`` of ``runtime/plan.json`` (empty when
    there is no readable plan)."""
    path = root / st.PLAN_PATH
    try:
        if path.is_symlink():
            return {}
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(plan, dict):
        return {}
    return {k: plan[k] for k in ("bundleDigest", "planDigest") if isinstance(plan.get(k), str)}


def _tofu_states(root: Path) -> List[Dict[str, Any]]:
    """The lineage and serial of each local OpenTofu state ``fluid apply``
    keeps under ``.fluid/iac/<provider>/<id>/``: which state version the
    attempt left. A remote backend's state is not read (no network), and no
    state reached through a link is."""
    states: List[Dict[str, Any]] = []
    iac = root / ".fluid" / "iac"
    try:
        if iac.parent.is_symlink() or iac.is_symlink():
            return states
        found = sorted(iac.glob("*/*/terraform.tfstate"))
    except OSError:
        return states
    for path in found:
        try:
            if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)):
                continue
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(body, dict):
            states.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "lineage": body.get("lineage"),
                    "serial": body.get("serial"),
                }
            )
    return states


Snapshot = Dict[str, Optional[Tuple[int, int, int, int]]]


def snapshot(num: int, root: Path) -> Snapshot:
    """Each of stage ``num``'s report files as it is before the stage runs
    (inode, size, mtime, ctime; ``None`` when absent), so its receipts name
    only the files this attempt wrote, whatever the clock's resolution."""
    out: Snapshot = {}
    for rel in _STAGE_FILES.get(num, ()):
        try:
            s = os.lstat(root / rel)
            out[rel] = (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        except OSError:
            out[rel] = None
    return out


def receipts(num: int, root: Path, before: Snapshot) -> Dict[str, Any]:
    """What stage ``num`` left behind in this attempt: each of its report files
    the attempt wrote (changed since ``before``) with its digest, plus the
    plan's digests (stages 6 and 7) and the local OpenTofu states (stage 7)."""
    files: Dict[str, str] = {}
    now = snapshot(num, root)
    for rel, stamp in now.items():
        if stamp is None or stamp == before.get(rel):
            continue
        path = root / rel
        try:
            if path.is_symlink() or not path.is_file():
                continue
            files[rel] = _sha256(path)
        except OSError:
            continue
    out: Dict[str, Any] = {"files": files}
    if num in (6, st.APPLY_STAGE):
        out["plan"] = plan_digests(root)
    if num == st.APPLY_STAGE:
        out["tofu_states"] = _tofu_states(root)
    return out


@dataclass
class Attempt:
    """One attempt of one stage, as the journal's events tell it."""

    stage: int
    number: int
    seq: int  # the line of its ``started`` event: orders attempts in one file
    started: Dict[str, Any]
    finished: Optional[Dict[str, Any]] = None
    acknowledged: bool = False

    @property
    def outcome(self) -> str:
        """``passed``, ``failed``, ``skipped-*`` or ``unknown`` (started, no end)."""
        if self.finished is None:
            return UNKNOWN
        return str(self.finished.get("status") or UNKNOWN)

    @property
    def changed_target(self) -> bool:
        """Whether this attempt was one that could change the target."""
        return bool(self.started.get("side_effects"))

    @property
    def unresolved(self) -> bool:
        """It could change the target, nobody knows what it did, and nobody
        has said they looked."""
        return self.changed_target and self.outcome == UNKNOWN and not self.acknowledged


@dataclass
class History:
    """One release file, replayed: each stage's attempts in order."""

    attempts: Dict[int, List[Attempt]] = field(default_factory=dict)
    _index: Dict[Tuple[int, int], Attempt] = field(default_factory=dict)
    lines: int = 0

    @classmethod
    def parse(cls, data: bytes) -> "History":
        """A line without its newline was cut off by a crash and never
        committed; a line that is not an event is skipped."""
        history = cls()
        chunks = data.split(b"\n")
        for seq, raw in enumerate(chunks[:-1]):
            try:
                event = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                continue
            if isinstance(event, dict):
                history.replay(event, seq)
        history.lines = len(chunks) - 1
        return history

    def replay(self, event: Dict[str, Any], seq: int) -> None:
        kind, stage = event.get("event"), event.get("stage")
        if not isinstance(stage, int) or isinstance(stage, bool) or stage not in st.STAGE_NAMES:
            return
        tries = self.attempts.setdefault(stage, [])
        if kind == "started":
            attempt = Attempt(stage, len(tries) + 1, seq, event)
            tries.append(attempt)
            self._index[(stage, attempt.number)] = attempt
            return
        number = event.get("attempt")
        target = self._index.get((stage, number)) if isinstance(number, int) else None
        if target is None:
            return
        if kind == "finished" and target.finished is None:
            target.finished = event
        elif kind == "acknowledged":
            target.acknowledged = True

    def get(self, stage: int) -> List[Attempt]:
        return self.attempts.get(stage) or []


class _TargetDir:
    """``DIR/<product>/<env>/``, reached from ``base`` without following a
    link at any component below it. ``base`` itself (an explicit
    ``--journal-dir``, or the project for the default) is the operator's."""

    def __init__(self, base: Path, parts: Sequence[str], *, create: bool) -> None:
        self.path = base.joinpath(*parts)
        self.fd: Optional[int] = None
        if create:
            base.mkdir(parents=True, exist_ok=True)  # the operator's: created as given
        if not _DIR_FD:  # pragma: no cover - no directory descriptors (Windows)
            current = base
            for part in parts:
                current = current / part
                if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                    raise JournalError(f"{current} is a symbolic link, not a journal directory")
                if create:
                    current.mkdir(mode=0o700, exist_ok=True)
            return
        fd = os.open(base, os.O_RDONLY | _DIRECTORY)
        refused = {getattr(errno, "ENOTDIR", None), getattr(errno, "ELOOP", None)}
        try:
            for depth, part in enumerate(parts, start=1):
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
                except OSError as exc:
                    if exc.errno not in refused:
                        raise
                    raise JournalError(
                        f"{base.joinpath(*parts[:depth])} is not a directory (a symbolic link "
                        "is never followed below the journal directory)"
                    ) from None
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def open(self, name: str, flags: int, mode: int = 0o600) -> int:
        """Open ``name`` in the target, never through a link, and only when it
        is a regular file with one link."""
        flags |= _NOFOLLOW | _BINARY
        try:
            if self.fd is not None:
                fd = os.open(name, flags, mode, dir_fd=self.fd)
            else:  # pragma: no cover - Windows: no O_NOFOLLOW, so look first
                path = self.path / name
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    raise JournalError(f"{path} is a symbolic link")
                fd = os.open(path, flags, mode)
        except OSError as exc:
            if exc.errno is not None and exc.errno == getattr(errno, "ELOOP", None):
                raise JournalError(f"{self.path / name} is a symbolic link") from None
            raise
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise JournalError(f"{self.path / name} is not a regular file ship wrote")
        return fd

    def names(self) -> List[str]:
        return sorted(os.listdir(self.fd if self.fd is not None else self.path))

    def fsync(self) -> None:
        """Make a new file's directory entry durable (best effort)."""
        if self.fd is None:  # pragma: no cover - Windows
            return
        try:
            os.fsync(self.fd)
        except OSError:
            pass

    def close(self) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)


def _read_fd(fd: int, path: Path, budget: int = MAX_FILE_BYTES) -> bytes:
    """The whole file behind ``fd`` (at most ``budget`` bytes, else refused)."""
    size = os.fstat(fd).st_size
    if size > MAX_FILE_BYTES:
        raise JournalError(
            f"{path} is {size} bytes, more than a stage journal ship writes; "
            "move it aside to start the target's journal again"
        )
    if size > budget:
        raise JournalError(
            f"the release files of {path.parent} together pass {MAX_TARGET_BYTES} bytes; "
            "move the files of releases you no longer need aside (a run reads them all)"
        )
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise JournalError(f"{path} grew past {MAX_FILE_BYTES} bytes while it was read")
        chunks.append(chunk)
    return b"".join(chunks)


def _stem(raw: str, fallback: str) -> str:
    from fluid_build.providers._path_safety import safe_filename_stem

    return safe_filename_stem(raw, fallback)


def target_stem(product_id: str) -> str:
    """The directory name a product's target has in the journal."""
    return _stem(product_id, "product")


def _parts(
    directory: Path, anchor: Optional[Path], product_id: str, environment: str
) -> Tuple[Path, List[str]]:
    tail = [_stem(product_id, "product"), _stem(environment, "env")]
    if anchor is None:
        return directory, tail
    return anchor, [*directory.relative_to(anchor).parts, *tail]


@dataclass
class Hold:
    """A target held by this run: its directory, opened without following a
    link, and the lock that keeps every other run out until it is closed."""

    target: _TargetDir
    product_stem: str
    environment: str
    lock_fd: Optional[int] = None

    def close(self) -> None:
        if self.lock_fd is not None:
            fd, self.lock_fd = self.lock_fd, None
            try:
                os.close(fd)  # the lock goes with its descriptor
            except OSError:
                pass
        self.target.close()


def hold_target(
    directory: Path, *, product_id: str, environment: str, anchor: Optional[Path] = None
) -> Hold:
    """Create and lock ``DIR/<product>/<env>/`` for this run, before its first
    stage. ``anchor``: the directory ``directory`` lies in whose components
    are the checkout's (no link below it is followed). ``JournalError`` when
    another run holds the target or it cannot be created."""
    base, parts = _parts(directory, anchor, product_id, environment)
    try:
        target = _TargetDir(base, parts, create=True)
    except OSError as exc:
        raise JournalError(
            f"cannot create the stage journal directory {base.joinpath(*parts)}: "
            f"{exc.strerror or exc}"
        ) from None
    hold = Hold(target, parts[-2], environment)
    try:
        hold.lock_fd = _lock(target)
    except OSError as exc:
        hold.close()
        raise JournalError(
            f"cannot lock the stage journal {target.path}: {exc.strerror or exc}"
        ) from None
    except BaseException:
        hold.close()
        raise
    return hold


@dataclass
class Journal:
    """The journal of one release (bundle digest) in one target (product, env)."""

    path: Path
    environment: str
    bundle_digest: str
    history: History = field(default_factory=History)
    #: The other releases' journals in this target, read when it was opened.
    others: Dict[str, History] = field(default_factory=dict)
    _dir: Optional[_TargetDir] = None
    _fd: Optional[int] = None
    _hold: Optional[Hold] = None  # set when this journal took the hold itself

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def attempts(self) -> Dict[int, List[Attempt]]:
        return self.history.attempts

    @classmethod
    def read(
        cls,
        directory: Path,
        *,
        product_id: str,
        environment: str,
        digest: str,
        anchor: Optional[Path] = None,
    ) -> "Journal":
        """The journal as it is, for ``--explain``: nothing is created or
        locked. ``JournalError`` when it cannot be read as a journal."""
        base, parts = _parts(directory, anchor, product_id, environment)
        journal = cls(
            base.joinpath(*parts, f"{digest[len('sha256:'):]}.jsonl"), environment, digest
        )
        try:
            target = _TargetDir(base, parts, create=False)
        except FileNotFoundError:
            return journal  # nothing journaled for this target yet
        except OSError as exc:
            raise JournalError(f"cannot read the stage journal {journal.path}: {exc}") from None
        try:
            journal._dir = target
            try:
                fd = target.open(journal.name, os.O_RDONLY | _NONBLOCK)
            except FileNotFoundError:
                fd = None
            if fd is not None:
                try:
                    journal.history = History.parse(_read_fd(fd, journal.path))
                finally:
                    os.close(fd)
            journal._read_others()
        except OSError as exc:
            raise JournalError(f"cannot read the stage journal {journal.path}: {exc}") from None
        finally:
            journal._dir = None
            target.close()
        return journal

    @classmethod
    def open(
        cls,
        directory: Path,
        *,
        product_id: str,
        environment: str,
        digest: str,
        run_id: str,
        anchor: Optional[Path] = None,
    ) -> "Journal":
        """Hold the target (see :func:`hold_target`), then open this release's
        file in it. The journal releases the hold when it is closed."""
        hold = hold_target(directory, product_id=product_id, environment=environment, anchor=anchor)
        try:
            journal = cls.open_in(hold, product_id=product_id, digest=digest, run_id=run_id)
        except BaseException:
            hold.close()
            raise
        journal._hold = hold
        return journal

    @classmethod
    def open_in(cls, hold: Hold, *, product_id: str, digest: str, run_id: str) -> "Journal":
        """Open (creating) this release's file in a held target and read every
        release's. ``JournalError`` when the bundle names another product than
        the one held, or the journal cannot be read or written."""
        if _stem(product_id, "product") != hold.product_stem:
            raise JournalError(
                f"the bundle's product id {product_id!r} is not the contract's, whose stage "
                f"journal this run holds ({hold.target.path})"
            )
        path = hold.target.path / f"{digest[len('sha256:'):]}.jsonl"
        journal = cls(path, hold.environment, digest)
        journal._dir = hold.target
        try:
            journal._open_own(product_id, run_id)
        except OSError as exc:
            journal.close()
            raise JournalError(
                f"cannot write the stage journal {journal.path}: {exc.strerror or exc}"
            ) from None
        except BaseException:
            journal.close()
            raise
        return journal

    def _open_own(self, product_id: str, run_id: str) -> None:
        assert self._dir is not None
        flags = os.O_RDWR | os.O_APPEND
        try:
            self._fd = self._dir.open(self.name, flags | os.O_CREAT | os.O_EXCL)
            new = True
            self._dir.fsync()
        except FileExistsError:
            self._fd = self._dir.open(self.name, flags | _NONBLOCK)
            new = False
        data = _read_fd(self._fd, self.path)
        if data and not data.endswith(b"\n"):
            # End the line a crash cut off, so the next event starts its own
            # line; the torn one stays unreadable and is skipped.
            self._write(self._fd, b"\n")
            data += b"\n"
        self.history = History.parse(data)
        self._read_others()
        if new:
            self._append(
                {
                    "event": "opened",
                    "schema": JOURNAL_SCHEMA,
                    "product_id": product_id,
                    "environment": self.environment,
                    "bundle_digest": self.bundle_digest,
                    "run_id": run_id,
                }
            )

    def _read_others(self) -> None:
        assert self._dir is not None
        self.others = {}
        budget = MAX_TARGET_BYTES
        for name in self._dir.names():
            if not name.endswith(".jsonl") or name == self.name:
                continue
            try:
                fd = self._dir.open(name, os.O_RDONLY | _NONBLOCK)
            except FileNotFoundError:  # removed since it was listed
                continue
            try:
                data = _read_fd(fd, self._dir.path / name, budget)
            finally:
                os.close(fd)
            budget -= len(data)
            self.others[name] = History.parse(data)

    def _every_release(self) -> Iterator[Tuple[str, History]]:
        yield self.name, self.history
        yield from self.others.items()

    def last(self, stage: int) -> Optional[Attempt]:
        tries = self.history.get(stage)
        return tries[-1] if tries else None

    def _unresolved(self, stage: int) -> Optional[Tuple[str, Attempt]]:
        for name, history in self._every_release():
            for attempt in history.get(stage):
                if attempt.unresolved:
                    return name, attempt
        return None

    def refusal(
        self,
        num: int,
        argv: Sequence[str],
        root: Path,
        acknowledged: Collection[int],
        *,
        replans_first: bool = False,
    ) -> Optional[str]:
        """Why stage ``num`` must not run now in this target, or ``None``.
        ``acknowledged``: the stages ``--acknowledge-inspected`` names;
        ``replans_first``: stage 6 runs before it (``--explain``), so a stale
        plan is replaced by then."""
        if not side_effects(num, argv):
            return None
        for stage in (num, st.APPLY_STAGE) if num in _AFTER_APPLY else (num,):
            found = self._unresolved(stage)
            if found is None or stage in acknowledged:
                continue
            name, attempt = found
            release = "this release" if name == self.name else f"release {name[:12]}"
            blocks = "" if stage == num else f", and stage {num} {st.STAGE_NAMES[num]} acts on it"
            return (
                f"stage {stage} {st.STAGE_NAMES[stage]} started in {release} for env "
                f"{self.environment} at {attempt.started.get('at')} and has no known outcome "
                f"(attempt {attempt.number}), so what it changed in the target is unknown{blocks}. "
                f"Inspect the target, then run again with --acknowledge-inspected "
                f"{st.STAGE_NAMES[stage]}"
            )
        if num == st.APPLY_STAGE and not replans_first:
            return self._stale_plan(root)
        return None

    def _stale_plan(self, root: Path) -> Optional[str]:
        """An apply in this release that changed the target and did not pass
        ran a plan the target may have moved on from. Only a plan stage 6 wrote
        through ship after it, still the one in ``runtime/plan.json``, is new."""
        failed = [
            a
            for a in self.history.get(st.APPLY_STAGE)
            if a.changed_target and a.outcome != "passed"
        ]
        if not failed:
            return None
        latest = failed[-1]
        replans = [a for a in self.history.get(6) if a.seq > latest.seq and a.outcome == "passed"]
        if replans:
            wrote = ((replans[-1].finished or {}).get("receipts") or {}).get("plan") or {}
            now = plan_digests(root).get("planDigest")
            if now is not None and now == wrote.get("planDigest"):
                return None
            stale = "runtime/plan.json is not the plan stage 6 wrote since"
        else:
            stale = "the plan it ran is stale"
        return (
            f"stage 7 apply {latest.outcome} in attempt {latest.number} for this release, so "
            f"it may have changed part of the target and {stale}. Plan again against the "
            "target as it is now, then apply (--stage plan-apply)"
        )

    @staticmethod
    def _write(fd: int, data: bytes) -> None:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"short write ({written} of {len(data)} bytes)")
        os.fsync(fd)

    def _append(self, event: Dict[str, Any], fd: Optional[int] = None) -> Dict[str, Any]:
        """One line, one write, then fsync: a line is in the journal whole, or
        (cut off by a crash) ignored when read."""
        from fluid_build.cli._ship_report import utc_now

        record = {"at": utc_now(), **event}
        data = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
        target = self._fd if fd is None else fd
        if target is None:
            raise JournalError(f"the stage journal {self.path} is closed")
        try:
            self._write(target, data)
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or exc
            raise JournalError(f"cannot write the stage journal {self.path}: {reason}") from None
        if fd is None:
            self.history.replay(record, self.history.lines)
            self.history.lines += 1
        return record

    def acknowledge(self, num: int, run_id: str) -> List[Tuple[str, int]]:
        """Record that the operator inspected the target after each attempt of
        stage ``num`` with no known outcome, in whichever release's file holds
        it. Returns ``(release, attempt number)`` for each one recorded."""
        here: List[Tuple[str, int]] = []
        for name, history in list(self._every_release()):
            for attempt in history.get(num):
                if not attempt.unresolved:
                    continue
                event = {
                    "event": "acknowledged",
                    "stage": num,
                    "name": st.STAGE_NAMES[num],
                    "attempt": attempt.number,
                    "run_id": run_id,
                    "note": "the operator inspected the target (--acknowledge-inspected)",
                }
                if name == self.name:
                    self._append(event)
                    here.append(("this release", attempt.number))
                else:
                    self._append_other(name, event)
                    attempt.acknowledged = True
                    here.append((f"release {name[:12]}", attempt.number))
        return here

    def _append_other(self, name: str, event: Dict[str, Any]) -> None:
        if self._dir is None:
            raise JournalError(f"the stage journal {self.path} is closed")
        try:
            fd = self._dir.open(name, os.O_RDWR | os.O_APPEND | _NONBLOCK)
        except OSError as exc:
            raise JournalError(
                f"cannot write the stage journal {self._dir.path / name}: {exc.strerror or exc}"
            ) from None
        try:
            if _torn_tail(fd):  # a crash cut its last line: end it first
                self._write(fd, b"\n")
            self._append(event, fd)
        except OSError as exc:
            raise JournalError(
                f"cannot write the stage journal {self._dir.path / name}: {exc.strerror or exc}"
            ) from None
        finally:
            os.close(fd)

    def started(
        self,
        num: int,
        argv: Sequence[str],
        run_id: str,
        root: Path,
        *,
        shown: Optional[Sequence[str]] = None,
    ) -> None:
        """Open an attempt of stage ``num``. ``shown``: ``argv`` as it may be
        written down (credential-bearing flag values masked); ``argv`` itself
        decides whether the attempt can change the target."""
        event: Dict[str, Any] = {
            "event": "started",
            "stage": num,
            "name": st.STAGE_NAMES[num],
            "attempt": len(self.history.get(num)) + 1,
            "run_id": run_id,
            "argv": list(argv if shown is None else shown),
            "side_effects": side_effects(num, argv),
        }
        if num == st.APPLY_STAGE:
            event["plan"] = plan_digests(root)  # the plan it is about to run
        self._append(event)

    def finished(
        self,
        num: int,
        *,
        status: str,
        exit_code: Optional[int],
        reason: Optional[str],
        receipts: Dict[str, Any],
    ) -> None:
        latest = self.last(num)
        if latest is None or latest.finished is not None:
            return  # never started in this journal: nothing to close
        self._append(
            {
                "event": "finished",
                "stage": num,
                "name": st.STAGE_NAMES[num],
                "attempt": latest.number,
                "status": status,
                "exit_code": exit_code,
                "reason": reason,
                "receipts": receipts,
            }
        )

    def digest(self) -> Optional[str]:
        """The journal file's digest as it is now (``None`` when unreadable)."""
        if self._fd is None:
            return None
        try:
            return "sha256:" + hashlib.sha256(_read_fd(self._fd, self.path)).hexdigest()
        except (OSError, JournalError):
            return None

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                os.close(fd)
            except OSError:
                pass
        self._dir = None  # the hold's: closed with it
        if self._hold is not None:
            hold, self._hold = self._hold, None
            hold.close()


def _lock(target: _TargetDir) -> int:
    """An advisory, exclusive, non-blocking lock on the target's ``.lock``
    (POSIX ``flock``), held while the descriptor is open. Where ``fcntl`` does
    not exist no lock is taken: the journal still records, and two concurrent
    runs are the operator's to avoid."""
    fd = target.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | _NONBLOCK)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        held = {getattr(errno, n, None) for n in ("EWOULDBLOCK", "EAGAIN", "EACCES")}
        if exc.errno in held:
            raise JournalError(
                f"another fluid ship run holds this target's stage journal ({target.path})"
            ) from None
        raise JournalError(
            f"cannot lock the stage journal {target.path}: {exc.strerror or exc}"
        ) from None
    return fd


def _torn_tail(fd: int) -> bool:
    """Whether the file behind ``fd`` ends without a newline (a crash cut its
    last line). Its offset moves; an ``O_APPEND`` write still appends."""
    if os.fstat(fd).st_size == 0:
        return False
    os.lseek(fd, -1, os.SEEK_END)
    return os.read(fd, 1) != b"\n"
