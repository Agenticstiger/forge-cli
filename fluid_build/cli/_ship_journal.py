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
  never finished or finished ``unknown`` (interrupted, killed, a CI timeout):
  what it changed is not known. The stage is refused until the operator has
  inspected the target and passes ``--acknowledge-inspected STAGE``, which the
  journal records. An unknown apply blocks policy-apply, publish and
  schedule-sync the same way: they would act on a target nobody has looked at;
* an apply in this release that did not pass: it may have changed part of the
  target, so the plan it ran is stale. Apply is refused until stage 6 has
  planned again (or ``runtime/plan.json`` is another plan);
* anything else reruns. A stage that changes nothing always reruns.

Nothing is rolled back: recovery goes forward (inspect, plan again, apply
again), never through an inverse nobody can prove.

Standard library only at module scope (``fluid --help`` imports ``ship``).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Dict, Iterator, List, Optional, Sequence, Tuple

from . import _ship_stages as st

JOURNAL_SCHEMA = "fluid-ship-journal/1"
DEFAULT_JOURNAL_DIR = ".fluid/ship-journal"
LOCK_NAME = ".lock"

#: The outcome of an attempt that started and has no known end.
UNKNOWN = "unknown"

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
    8: (st.POLICY_BINDINGS,),
    9: ("runtime/verify-report.json",),
    11: (st.SCHEDULE_SYNC_REPORT,),
}

#: Stages that can change the target (see :func:`side_effects` for when).
TARGET_STAGES = frozenset({st.APPLY_STAGE, 8, 10, 11})

#: Stages that act on what apply left, so an unknown apply blocks them too.
_AFTER_APPLY = frozenset({8, 10, 11})


class JournalError(Exception):
    """The journal cannot be opened or written: the stage must not run."""


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

    from fluid_build.forge.core.bundle import read_tar_member_bounded

    path = root / st.BUNDLE_PATH
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            manifest = json.loads(read_tar_member_bounded(tar, "MANIFEST.json").decode("utf-8"))
    except (OSError, ValueError, KeyError, tarfile.TarError, RecursionError):
        return None
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
    attempt left. A remote backend's state is not read (no network)."""
    states: List[Dict[str, Any]] = []
    for path in sorted((root / ".fluid" / "iac").glob("*/*/terraform.tfstate")):
        try:
            if path.is_symlink():
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


def receipts(num: int, root: Path, since_ns: int) -> Dict[str, Any]:
    """What stage ``num`` left behind in this attempt: each of its report files
    the attempt wrote (modified at or after ``since_ns``) with its digest, plus
    the plan's digests (stages 6 and 7) and the local OpenTofu states (stage 7)."""
    files: Dict[str, str] = {}
    for rel in _STAGE_FILES.get(num, ()):
        path = root / rel
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_mtime_ns < since_ns:
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


Attempts = Dict[int, List[Attempt]]


def _load(path: Path) -> Attempts:
    """Replay one journal file. A line without its newline was cut off by a
    crash and never committed; a line that is not an event is skipped."""
    attempts: Attempts = {}
    try:
        data = path.read_bytes()
    except OSError:
        return attempts
    for seq, raw in enumerate(data.split(b"\n")[:-1]):
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict):
            _replay(attempts, event, seq)
    return attempts


def _replay(attempts: Attempts, event: Dict[str, Any], seq: int) -> None:
    kind, stage = event.get("event"), event.get("stage")
    if not isinstance(stage, int) or isinstance(stage, bool) or stage not in st.STAGE_NAMES:
        return
    tries = attempts.setdefault(stage, [])
    if kind == "started":
        tries.append(Attempt(stage, len(tries) + 1, seq, event))
        return
    number = event.get("attempt")
    target = next((a for a in tries if a.number == number), None)
    if target is None:
        return
    if kind == "finished" and target.finished is None:
        target.finished = event
    elif kind == "acknowledged":
        target.acknowledged = True


@dataclass
class Journal:
    """The journal of one release (bundle digest) in one target (product, env)."""

    path: Path
    environment: str
    bundle_digest: str
    attempts: Attempts = field(default_factory=dict)
    #: The other releases' journals in this target, read when it was opened.
    others: Dict[Path, Attempts] = field(default_factory=dict)
    _fd: Optional[int] = None
    _lock_fd: Optional[int] = None
    _lines: int = 0

    @staticmethod
    def location(directory: Path, product_id: str, environment: str, digest: str) -> Path:
        from fluid_build.providers._path_safety import safe_filename_stem

        target = (
            directory
            / safe_filename_stem(product_id, "product")
            / safe_filename_stem(environment, "env")
        )
        return target / f"{digest[len('sha256:'):]}.jsonl"

    @classmethod
    def read(cls, directory: Path, *, product_id: str, environment: str, digest: str) -> "Journal":
        """The journal as it is, for ``--explain``: nothing is created or locked."""
        path = cls.location(directory, product_id, environment, digest)
        journal = cls(path, environment, digest)
        journal._read_all()
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
    ) -> "Journal":
        """Lock the target for this run, open (creating) this release's file and
        read every release's. ``JournalError`` when another run holds the target
        or the journal cannot be written."""
        path = cls.location(directory, product_id, environment, digest)
        journal = cls(path, environment, digest)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            journal._lock_fd = _lock(path.parent / LOCK_NAME)
            new = not path.exists()
            journal._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            if new:
                _fsync_dir(path.parent)
            elif _torn(path):
                # End the line a crash cut off, so the next event starts its own
                # line; the torn one stays unreadable and is skipped.
                os.write(journal._fd, b"\n")
                os.fsync(journal._fd)
            journal._read_all()
            if new:
                journal._append(
                    {
                        "event": "opened",
                        "schema": JOURNAL_SCHEMA,
                        "product_id": product_id,
                        "environment": environment,
                        "bundle_digest": digest,
                        "run_id": run_id,
                    }
                )
        except OSError as exc:
            journal.close()
            raise JournalError(f"cannot write the stage journal {path}: {exc.strerror or exc}")
        except JournalError:
            journal.close()
            raise
        return journal

    def _read_all(self) -> None:
        self.attempts = _load(self.path)
        try:
            self._lines = self.path.read_bytes().count(b"\n")
        except OSError:
            self._lines = 0
        self.others = {
            p: _load(p)
            for p in sorted(self.path.parent.glob("*.jsonl"))
            if p.name != self.path.name and not p.is_symlink()
        }

    def _every_release(self) -> Iterator[Tuple[Path, Attempts]]:
        yield self.path, self.attempts
        yield from self.others.items()

    def last(self, stage: int) -> Optional[Attempt]:
        tries = self.attempts.get(stage) or []
        return tries[-1] if tries else None

    def _unresolved(self, stage: int) -> Optional[Tuple[Path, Attempt]]:
        for path, attempts in self._every_release():
            for attempt in attempts.get(stage) or []:
                if attempt.unresolved:
                    return path, attempt
        return None

    def refusal(
        self, num: int, argv: Sequence[str], root: Path, acknowledged: Collection[int]
    ) -> Optional[str]:
        """Why stage ``num`` must not run now in this target, or ``None``.
        ``acknowledged``: the stages ``--acknowledge-inspected`` names."""
        if not side_effects(num, argv):
            return None
        for stage in (num, st.APPLY_STAGE) if num in _AFTER_APPLY else (num,):
            found = self._unresolved(stage)
            if found is None or stage in acknowledged:
                continue
            path, attempt = found
            name = st.STAGE_NAMES[stage]
            release = "this release" if path == self.path else f"release {path.stem[:12]}"
            blocks = "" if stage == num else f", and stage {num} {st.STAGE_NAMES[num]} acts on it"
            return (
                f"stage {stage} {name} started in {release} for env {self.environment} at "
                f"{attempt.started.get('at')} and has no known outcome (attempt "
                f"{attempt.number}), so what it changed in the target is unknown{blocks}. "
                f"Inspect the target, then run again with --acknowledge-inspected {name}"
            )
        if num == st.APPLY_STAGE:
            return self._stale_plan(root)
        return None

    def _stale_plan(self, root: Path) -> Optional[str]:
        """An apply in this release that changed the target and did not pass
        ran a plan the target may have moved on from. A plan stage 6 wrote
        after it, or a plan with another digest, is a new plan."""
        failed = [
            a
            for a in self.attempts.get(st.APPLY_STAGE) or []
            if a.changed_target and a.outcome != "passed"
        ]
        if not failed:
            return None
        latest = failed[-1]
        if any(a.seq > latest.seq and a.outcome == "passed" for a in self.attempts.get(6) or []):
            return None
        ran = (latest.started.get("plan") or {}).get("planDigest")
        now = plan_digests(root).get("planDigest")
        if isinstance(ran, str) and now is not None and now != ran:
            return None
        return (
            f"stage 7 apply {latest.outcome} in attempt {latest.number} for this release, so "
            "it may have changed part of the target and the plan it ran is stale. Plan "
            "again against the target as it is now, then apply (--stage plan-apply)"
        )

    def _append(self, event: Dict[str, Any], fd: Optional[int] = None) -> None:
        """One line, one write, then fsync: a line is in the journal whole, or
        (cut off by a crash) ignored when read."""
        from fluid_build.cli._ship_report import utc_now

        record = {"at": utc_now(), **event}
        data = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
        own = fd is None
        fd = self._fd if own else fd
        if fd is None:
            raise JournalError(f"the stage journal {self.path} is closed")
        try:
            written = os.write(fd, data)
            if written != len(data):
                raise OSError(f"short write ({written} of {len(data)} bytes)")
            os.fsync(fd)
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or exc
            raise JournalError(f"cannot write the stage journal {self.path}: {reason}") from None
        if own:
            _replay(self.attempts, record, self._lines)
            self._lines += 1

    def acknowledge(self, num: int, run_id: str) -> List[int]:
        """Record that the operator inspected the target after each attempt of
        stage ``num`` with no known outcome, in whichever release's file holds
        it. Returns the attempt numbers acknowledged in this release."""
        here: List[int] = []
        for path, attempts in list(self._every_release()):
            for attempt in attempts.get(num) or []:
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
                if path == self.path:
                    self._append(event)
                    here.append(attempt.number)
                else:
                    self._append_other(path, event)
                    attempt.acknowledged = True
        return here

    def _append_other(self, path: Path, event: Dict[str, Any]) -> None:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise JournalError(f"cannot write the stage journal {path}: {exc.strerror or exc}")
        try:
            self._append(event, fd)
        finally:
            os.close(fd)

    def started(self, num: int, argv: Sequence[str], run_id: str, root: Path) -> None:
        tries = self.attempts.get(num) or []
        event: Dict[str, Any] = {
            "event": "started",
            "stage": num,
            "name": st.STAGE_NAMES[num],
            "attempt": len(tries) + 1,
            "run_id": run_id,
            "argv": list(argv),
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
        try:
            return _sha256(self.path)
        except OSError:
            return None

    def close(self) -> None:
        for name in ("_fd", "_lock_fd"):
            fd = getattr(self, name)
            if fd is not None:
                setattr(self, name, None)
                try:
                    os.close(fd)  # the lock goes with its descriptor
                except OSError:
                    pass


def _lock(path: Path) -> int:
    """An advisory, exclusive, non-blocking lock on ``path`` (POSIX ``flock``),
    held while the descriptor is open. Where ``fcntl`` does not exist no lock is
    taken: the journal still records, and two concurrent runs are the
    operator's to avoid."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise JournalError(
            f"another fluid ship run holds this target's stage journal ({path.parent})"
        ) from None
    return fd


def _torn(path: Path) -> bool:
    """Whether the file's last line lacks its newline (a crash mid-write)."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            return False
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) != b"\n"


def _fsync_dir(directory: Path) -> None:
    """Make a new file's directory entry durable (best effort: not every
    platform opens a directory)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
