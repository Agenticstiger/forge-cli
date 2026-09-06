#!/usr/bin/env python3
"""PostToolUse — record the answer the PreToolUse dialog could not.

PreToolUse asks and exits. At that moment the owner has not answered yet, so
the asking process can never write the outcome. But PostToolUse fires only if
the tool actually ran, and the tool only runs on allow — so THE TOOL RUNNING IS
THE PROOF OF ALLOW. That pairing is the whole mechanism.

On deny the tool never runs, this never fires, and the pending note left by
PreToolUse is garbage-collected on its next invocation.

What gets written is deliberately unfinished: a grace entry with a placeholder
reason and a hard 90-day expiry, so it shows up in review as a decision someone
still owes a sentence for. A waiver that costs nothing to create is a rule that
does not exist; a waiver with no expiry is a permanent hole wearing the word
"temporary".

This is the only part of the guardrail that writes to the repo. It appends one
entry to one hand-maintained file and nothing else.

    "PostToolUse": [
      { "matcher": "Edit|Write",
        "hooks": [ { "type": "command",
                     "command": "python3 tools/product_guardrail/hook_posttooluse.py",
                     "timeout": 10 } ] }
    ]
"""

from __future__ import annotations

# Python puts a script's OWN DIRECTORY on sys.path[0] before anything else
# runs. This file's directory is one an attacker may be able to write to — it is
# vendored into public repos and the hooks import this on every edit — so a file
# dropped here named ast.py, fnmatch.py or tokenize.py would shadow the standard
# library and execute. Removing my own sys.path.insert was not enough; the
# interpreter adds it regardless.
#
# Scrubbed here, at the very top, using ONLY `sys` — a built-in module that
# cannot itself be shadowed by a file — and string operations, so that not one
# shadowable import has happened yet when this runs.
import os as _os
import sys as _sys

# REALPATH on both sides. CPython only abspaths __file__ (keeping symlinks and
# "./" segments) while it realpaths sys.path[0], so a raw string compare removed
# nothing whenever the two spellings differed — which is EVERY "./path/x.py"
# invocation, and any checkout reached through a symlink. `os` is imported here
# rather than later precisely because it is needed to do the comparison right;
# it is a built-in extension module on every supported platform, so it cannot be
# shadowed by a file in this directory.
_HERE_REAL = _os.path.dirname(_os.path.realpath(_os.path.abspath(__file__)))
_sys.path[:] = [
    _p for _p in _sys.path
    if _p not in ("", ".")
    and _os.path.realpath(_p or ".") != _HERE_REAL
]
del _HERE_REAL

import ast
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# No .pyc beside the checker: a stale cache next to it is a guardrail running
# yesterday's logic while reporting today's.
sys.dont_write_bytecode = True


def _load_check():
    """Import check.py BY PATH, without putting this directory on sys.path.

    `sys.path.insert(0, HERE)` left the repo's own directory ahead of the
    standard library for the rest of the process, and check.py imports ast,
    fnmatch, tokenize and argparse — so a file named ast.py dropped in beside
    it would shadow the stdlib and execute on the next edit anyone made. These
    hooks run on EVERY Edit/Write, so that turned writing one innocuous-looking
    file into a deferred code-execution primitive.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_pg_check", HERE / "check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

GRACE_DAYS = 90
PENDING_TTL = 600  # seconds; must match hook_pretooluse.py
PLACEHOLDER = "allowed at authoring time — REPLACE THIS with the actual reason"


def _git_dir(root):
    """The REAL .git directory. In a worktree, `.git` is a FILE pointing
    elsewhere, so `root / ".git" / ...` raised NotADirectoryError — swallowed —
    and the waiver was never written while the dialog still told the owner it
    had been. This team works in worktrees; the audit trail was missing for all
    of them."""
    r = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=root,
                       capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else root / ".git"


def _repo_root():
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE,
                       capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def _owner(root):
    r = subprocess.run(["git", "config", "user.name"], cwd=root,
                       capture_output=True, text=True)
    # NEVER the literal "unknown": the self-test rejects a grace entry with
    # that owner, so writing one bricks the gate the same way the dict(...)
    # syntax did. Fall back to something that is at least attributable.
    name = r.stdout.strip() if r.returncode == 0 else ""
    if name and name.lower() != "unknown":
        return name
    import getpass
    try:
        return f"{getpass.getuser()} (git user.name unset)"
    except Exception:
        return "recorded at authoring time (git user.name unset)"


def _append_grace(profile_path, entries):
    """Insert entries at the head of GRACE, so the newest is the most visible.

    Anchored on the literal `GRACE = [`. If that anchor is not found the
    profile has been restructured; say so and change nothing rather than
    guessing where a list starts.
    """
    # One writer at a time. This is a read-modify-write of a file the checker
    # also reads, and two edits in flight could interleave and lose a waiver or
    # collide on the temp file. An exclusive lock beside the profile is enough:
    # the only writers are these hooks on one machine.
    lock = profile_path.with_suffix(".py.lock")
    lock_fd = None
    try:
        lock_fd = os.open(str(lock), os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError:
        if lock_fd is not None:
            os.close(lock_fd)
        lock_fd = None  # locking is best-effort; never block the edit

    try:
        return _append_grace_locked(profile_path, entries)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
                lock.unlink(missing_ok=True)
            except OSError:
                pass


def _append_grace_locked(profile_path, entries):
    src = profile_path.read_text(encoding="utf-8")
    anchor = "GRACE = ["
    if src.count(anchor) != 1:
        return False
    # DICT LITERALS, not dict(...) calls. profile.py is parsed with
    # ast.literal_eval, which rejects a Call — so the previous version wrote a
    # file its own checker could not load. One click of Allow left the gate
    # exiting 2 forever and both hooks silently disabled.
    block = "".join(
        f'\n    {{"path": {e["path"]!r}, "rule": {e["rule"]!r},\n'
        f'     "reason": {e["reason"]!r},\n'
        f'     "owner": {e["owner"]!r}, "decided": {e["decided"]!r}, '
        f'"expires": {e["expires"]!r}}},'
        for e in entries
    )
    updated = src.replace(anchor, anchor + block, 1)
    # Validate with the SAME loader the checker uses, not merely ast.parse.
    # ast.parse only proves the file is syntactically Python; it happily
    # accepted the dict(...) calls this used to write, which check.py then
    # refused to load. A waiver that bricks the gate is worse than no waiver.
    tmp_check = HERE / "check.py"
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_pg_check_validate", tmp_check)
    _chk = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_chk)
    # Unique per process: a shared name is a collision between two hooks.
    _probe = profile_path.with_suffix(f".py.{os.getpid()}.probe")
    _probe.write_text(updated, encoding="utf-8")
    try:
        _chk._load_data(_probe)
    finally:
        _probe.unlink(missing_ok=True)
    # Write via a sibling temp file and rename. Path.write_text truncates on
    # open, so an encoding error mid-write left profile.py at ZERO BYTES —
    # reproduced under a latin-1 locale, where the em dash in the placeholder
    # raised and the hook then swallowed the error and exited 0.
    tmp = profile_path.with_suffix(f".py.{os.getpid()}.tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, profile_path)
    return True


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        if event.get("tool_name", "") not in ("Edit", "Write", "MultiEdit"):
            return 0
        fp = (event.get("tool_input") or {}).get("file_path")
        root = _repo_root()
        if not fp or root is None:
            return 0
        try:
            rel = Path(fp).resolve().relative_to(root).as_posix()
        except ValueError:
            return 0

        check = _load_check()
        pending = (_git_dir(root) / "product-guardrail"
                   / f"pending-{hashlib.sha256(rel.encode()).hexdigest()[:16]}.json")
        if not pending.exists():
            return 0  # nothing was asked about this file — normal, stay quiet
        note = json.loads(pending.read_text(encoding="utf-8"))
        pending.unlink()

        # A note is proof of allow ONLY for the edit it was written for.
        # Keyed on the path alone and never aged, it turned an explicit DENY
        # into a waiver: the note survived the refusal, and the next unrelated
        # edit to the same file consumed it and recorded a grace entry for the
        # finding the owner had just refused. Reproduced end to end, and again
        # with a note aged thirty days.
        # The TTL exists so a DENIED note cannot be consumed by a later edit.
        # It must not discard an APPROVAL: the owner may leave the dialog open
        # for a while, and silently losing their answer is worse than the stale
        # note it was written to prevent. The content hash below already proves
        # this is the very edit that was asked about — that is the real guard,
        # and it does not expire. An aged note without a content hash (only
        # possible for a note written before this change) is still dropped.
        aged = time.time() - float(note.get("at", 0)) > PENDING_TTL
        if aged and not note.get("content_sha"):
            return 0
        proposed = note.get("content_sha")
        if proposed:
            # Over the DECODED text, matching what the pre-hook hashed. Hashing
            # raw bytes could never agree for a BOM'd or UTF-16 file.
            actual = hashlib.sha256(
                check._read_text(root / rel).encode("utf-8")).hexdigest()
            if actual != proposed:
                return 0  # what landed is not what was asked about

        payload = check.load_payload()
        # The hooks used to skip this entirely, so a tampered payload was
        # trusted at the keyboard even in a repo whose CI would refuse it.
        # Warn and stand down rather than block: a guardrail must never stop
        # someone editing, but it must not quietly vouch for a file either.
        stored = getattr(payload, "CANON_ID", None)
        if not stored or stored != check.compute_canon_id(payload):
            print("product-guardrail: canon_payload.py does not match its "
                  "CANON_ID — not vouching for it. Restore it from the canon "
                  "emitter.", file=sys.stderr)
            return 0
        profile = check.load_profile(None)
        c = check.classify_path(rel, profile)
        if not c:
            return 0
        kind, surface = c
        raw = check._read_text(root / rel)
        scan = check.Scan()
        check.scan_one(payload, profile, rel, raw, kind, surface, scan)

        # Only rules whose finding SURVIVED the write. If the owner allowed the
        # edit and then fixed the term in the same breath, there is nothing to
        # waive and nothing should be written.
        live = {f.rule for f in scan.findings if f.severity == "FAIL"}
        asked = {f["rule"] for f in note.get("findings", [])}
        rules = sorted(live & asked)
        if not rules:
            return 0

        already = {(g["path"], g["rule"]) for g in profile.GRACE}
        today = dt.date.today()
        entries = [
            # "./" + rel so path_matches sees a "/" and anchors it as an exact
            # path. A bare basename like "README.md" is a segment glob that
            # waives that filename in EVERY directory of the repo.
            dict(path=("./" + rel if "/" not in rel else rel),
                 rule=r, reason=PLACEHOLDER, owner=_owner(root),
                 decided=today.isoformat(),
                 expires=(today + dt.timedelta(days=GRACE_DAYS)).isoformat())
            for r in rules if (rel, r) not in already
        ]
        if not entries:
            return 0

        profile_path = HERE / "profile.py"
        if not _append_grace(profile_path, entries):
            print("product-guardrail: could not find `GRACE = [` in "
                  f"{profile_path} — no waiver recorded. Add it by hand.",
                  file=sys.stderr)
            return 0

        print(f"Product Guardrail: recorded a {GRACE_DAYS}-day waiver in "
              f"tools/product_guardrail/profile.py for "
              f"{', '.join(rules)} in {rel} — edit the reason before you commit.",
              file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 — never break the tool that succeeded
        # Type and message only. `repr(exc)` on a UnicodeEncodeError prints
        # the offending payload, which dumped profile.py's contents to stderr.
        print(f"product-guardrail recorder error: {type(exc).__name__}",
              file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
