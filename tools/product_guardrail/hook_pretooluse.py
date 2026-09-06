#!/usr/bin/env python3
"""PreToolUse — put an off-canon edit to the owner before it is written.

The guardrail does not silently rewrite and it does not silently fail. When an
edit would introduce vocabulary that is not the product's, this returns
`permissionDecision: "ask"` and the confirmation dialog carries the term, the
canonical term, the file and the reason — enough to decide in one read.

CONTENT COMES FROM STDIN, NEVER FROM A COMMAND-LINE INTERPOLATION. A hook
configured as `... ${tool_input.content}` runs a shell with the proposed file
body on the command line, so a markdown file containing $(...) executes on the
owner's machine on every Write. Interpolating `${tool_input.file_path}` is
narrow enough to be defensible; content is categorically not.

NEW FINDINGS ONLY. The file on disk is scanned before, the proposal after, and
only the difference is reported. Findings are keyed on rule + term + normalised
context rather than line number, because inserting a paragraph shifts every
line below it and a hook that then re-reports the whole file is a hook people
turn off.

ON ANY INTERNAL ERROR THIS EXITS 0. A broken guardrail must never block the
owner's editing — the opposite of the CI policy, deliberately. CI is where
blindness must be loud; the keyboard is where it must be silent.

Configured in .claude/settings.json:

    "PreToolUse": [
      { "matcher": "Edit|Write",
        "hooks": [ { "type": "command",
                     "command": "python3 tools/product_guardrail/hook_pretooluse.py",
                     "timeout": 10 } ] }
    ]

No `if:` predicate, deliberately: which files are product copy is a question
profile.py already answers, and a glob that answers it a second time is a
second definition to drift. The cost of asking the profile is ~30 ms.
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

import hashlib
import json
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

MAX_SHOWN = 5
PENDING_TTL = 600  # seconds; a dialog left unanswered this long is abandoned


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


def _apply_edits(before, tool_input):
    """Reproduce what Edit is about to do, in memory.

    Same semantics Edit uses: first occurrence unless replace_all. If the
    anchor does not match we fall back to scanning the replacement text alone,
    which is a fragment rather than a file but still catches a new term.
    """
    edits = tool_input.get("edits")
    if not edits:
        edits = [{
            "old_string": tool_input.get("old_string", ""),
            "new_string": tool_input.get("new_string", ""),
            "replace_all": tool_input.get("replace_all", False),
        }]
    text = before
    applied = False
    for e in edits:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        if old and old in text:
            text = text.replace(old, new) if e.get("replace_all") \
                else text.replace(old, new, 1)
            applied = True
    if not applied:
        return "\n".join(e.get("new_string", "") for e in edits), True
    return text, False


def _findings(check, payload, profile, rel, raw):
    c = check.classify_path(rel, profile)
    if not c:
        return None
    kind, surface = c
    scan = check.Scan()
    check.scan_one(payload, profile, rel, raw, kind, surface, scan)
    from datetime import date
    today = date.today().isoformat()
    out = []
    for f in scan.findings:
        state, g = check.grace_for(f, profile.GRACE, today)
        if state == "GRACED":
            continue
        if f.severity == "FAIL":
            out.append(f)
    return out


def _reason(rel, fragment, new):
    surface = new[0].surface if new else ""
    head = (f"Product Guardrail — {len(new)} new "
            f"{'finding' if len(new) == 1 else 'findings'} in\n{rel}"
            f"  (surface: {surface})")
    if fragment:
        head += "\n(scanned as a fragment: the edit anchor did not match the file)"
    lines = [head, ""]
    for i, f in enumerate(new[:MAX_SHOWN], 1):
        n = f"{i}. " if len(new) > 1 else ""
        lines.append(f"  {n}[{f.rule}]  {f.term!r}  ->  {f.want!r}")
        if f.line:
            lines.append(f"     line {f.line}: {f.excerpt}")
        elif f.excerpt:
            lines.append(f"     {f.excerpt}")
        if f.why:
            lines.append(f"     {f.why}")
        lines.append("")
    if len(new) > MAX_SHOWN:
        lines.append(f"  … and {len(new) - MAX_SHOWN} more")
        lines.append("")
    lines.append("Allow -> the edit is written as proposed AND a 90-day grace "
                 "entry is\n         appended to tools/product_guardrail/"
                 "profile.py under your name.")
    lines.append("Deny  -> nothing is written. Fix the term and retry.")
    return "\n".join(lines)


def _gc_pending(root):
    """Drop stale notes on EVERY invocation.

    This used to live inside _record_pending, which the `if not new: return 0`
    early return skips — so on a clean edit nothing was collected, and a note
    left behind by a DENIED edit survived indefinitely.
    """
    try:
        d = _git_dir(root) / "product-guardrail"
        if not d.is_dir():
            return
        now = time.time()
        for stale in d.glob("pending-*.json"):
            try:
                if now - stale.stat().st_mtime > PENDING_TTL:
                    stale.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _record_pending(root, rel, new, content):
    """Leave a note the PostToolUse recorder can find.

    PreToolUse cannot record the answer: at `ask` time the owner has not
    answered and this process has exited. The tool running AT ALL is the proof
    of allow, so the pair is what makes the decision durable. Under .git/,
    which is already ignored — never in the worktree.
    """
    try:
        d = _git_dir(root) / "product-guardrail"
        d.mkdir(parents=True, exist_ok=True)
        now = time.time()
        key = hashlib.sha256(rel.encode()).hexdigest()[:16]
        (d / f"pending-{key}.json").write_text(json.dumps({
            "rel": rel,
            "at": now,
            # Binds the note to THIS proposal. Without it the recorder would
            # consume the note on any later edit to the same path — including
            # one made after the owner denied this very edit.
            "content_sha": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "findings": [dict(rule=f.rule, term=f.term, want=f.want,
                              line=f.line, excerpt=f.excerpt)
                         for f in new],
        }), encoding="utf-8")
    except OSError:
        pass  # the waiver record is a convenience; never block the edit for it


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    try:
        tool = event.get("tool_name", "")
        if tool not in ("Edit", "Write", "MultiEdit"):
            return 0
        ti = event.get("tool_input") or {}
        fp = ti.get("file_path")
        if not fp:
            return 0

        root = _repo_root()
        if root is None:
            return 0
        _gc_pending(root)
        try:
            rel = Path(fp).resolve().relative_to(root).as_posix()
        except ValueError:
            return 0  # outside this repo

        check = _load_check()
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

        if check.classify_path(rel, profile) is None:
            return 0  # not a product surface — stay invisible

        path = root / rel
        # check.py's BOM-aware reader, not a bare utf-8 decode: a tracked
        # UTF-16 markdown file exists in one of these repos, and a plain decode
        # made the hook error on every edit to it.
        before = check._read_text(path) if path.exists() else ""
        fragment = False
        if tool == "Write":
            after = ti.get("content", "")
        else:
            after, fragment = _apply_edits(before, ti)

        old = _findings(check, payload, profile, rel, before) or []
        now = _findings(check, payload, profile, rel, after)
        if now is None:
            return 0
        seen = {}
        for f in old:
            seen[f.key()] = seen.get(f.key(), 0) + 1
        new = []
        for f in now:
            k = f.key()
            if seen.get(k):
                seen[k] -= 1
            else:
                new.append(f)

        if not new:
            return 0  # invisible when it has nothing to say

        _record_pending(root, rel, new, after)
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": _reason(rel, fragment, new),
        }}, sys.stdout)
        return 0

    except Exception as exc:  # noqa: BLE001 — see the module docstring
        # Type only. repr() of a UnicodeDecodeError contains the ENTIRE object
        # being decoded, so a single non-UTF-8 byte in a scanned file dumped
        # that file's whole content to stderr. The sibling recorder already
        # avoided this; the fix was never carried over until now.
        print(f"product-guardrail hook error (edit allowed): "
              f"{type(exc).__name__}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
