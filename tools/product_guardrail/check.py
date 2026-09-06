#!/usr/bin/env python3
"""Product Guardrail — fail when a product surface departs from the canon.

The vocabulary authority is `tools/canon.py` in the canon repo. It never
travels:
three of the five product repos are PUBLIC, and that file's prose names things
that must not be published. What travels is `canon_payload.py` — a generated,
leak-gated subset — plus this engine, byte-identical in every repo.

WHAT THIS IS FOR. The product's own Concepts page, whose job is to teach
what FLUID is, names zero canon components. Across every product repo the approved
vocabulary is essentially absent: Data Product Factory 0, Computational
Compliance 0, FLUID Fabric Ecosystem 0, Forge Engine 1. The product speaks an
entirely different language from the thing we sell. A near-miss checker cannot
see that — it only fires on a name that ALMOST appears — so this ships a
`coverage` report alongside the violation rules. Without it the gate would go
green on day one in every repo while proving nothing, which is the exact
failure mode this project has been bitten by before.

THREE FILES, TWO OWNERS.
    canon_payload.py  GENERATED upstream, identical everywhere. Never hand-edit:
                      CANON_ID is a digest of the registries plus this engine's
                      own rule semantics, so an edit fails THIS repo's CI,
                      offline, on the PR that made it.
    check.py          this engine. Identical everywhere. Covered by CANON_ID.
    profile.py        hand-written per repo: what to scan, what to skip, which
                      locale, the grace list. Never compared across repos.

Keeping repo-specific facts out of the shared files is the whole design. If
scope or grace ever leaks into the payload, the copies stop being identical
and the scheme collapses.

SURFACES. Four classes, because the middle two hold all the false positives:
    product copy   what a USER of the product reads      -> enforced
    developer prose comments and docstrings ABOUT it     -> advisory
    identifiers    CommandCenterLayout.tsx, imports,
                   env vars, markdown link destinations  -> never scanned
    history        CHANGELOG, archives, agent memory     -> never scanned
Rewriting a changelog to a name adopted after that release is falsification,
not compliance.

    python3 tools/product_guardrail/check.py            # self-test, id, scan
    python3 tools/product_guardrail/check.py --self-test
    python3 tools/product_guardrail/check.py --strict   # advisory also fails

Exit 0 clean · 1 the vocabulary is off canon · 2 the checker could not do its
job. The 1/2 split is load-bearing: CI must be able to tell "this is wrong"
from "this is blind", because only the second means fix the script and do not
trust the green.

Stdlib only.
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
# No bytecode cache anywhere in this tree: CANON_ID digests SOURCE, while an
# import executes whatever the loader picks — and a __pycache__ directory is
# hidden by .gitignore, so the two could differ invisibly.
_sys.dont_write_bytecode = True

import argparse
import ast
import fnmatch
import hashlib
import io
import json
import re
import subprocess
import sys
import tokenize
from datetime import date
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent

PRODUCT = "product copy"
DEV = "developer prose"


class Blind(Exception):
    """The checker could not do its job. Always exit 2, never 1."""


# ---------------------------------------------------------------------------
# payload + profile
# ---------------------------------------------------------------------------
def _load_data(path: Path):
    """Read a .py file as DATA. Never execute it.

    Both files this loads — the payload and the profile — are pure literal
    assignments, and both used to be imported, which executed whatever was in
    them. That made writing a file under this directory a deferred
    code-execution primitive: the payload is vendored into public repos where a
    fork PR can edit it, and both hooks import it on every single Edit/Write,
    so the code ran on the next unrelated edit with no command ever invoked.

    Parsing instead of importing removes the primitive outright, and makes an
    injected statement a LOUD FAILURE rather than a silent execution: anything
    that is not a literal assignment raises Blind naming the construct.
    """
    try:
        tree = ast.parse(_read_text(path), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
        raise Blind(f"cannot parse {path.name}: {exc}")

    ns = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                raise Blind(
                    f"{path.name} line {node.lineno}: only literal values are "
                    f"allowed here. This file is data and is never executed."
                )
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    raise Blind(f"{path.name} line {node.lineno}: odd assignment")
                ns[target.id] = value
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        elif isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        else:
            raise Blind(
                f"{path.name} line {node.lineno}: {type(node).__name__} is not "
                f"allowed. This file is data, not code, and is never executed."
            )
    return SimpleNamespace(**ns)


def load_payload():
    p = HERE / "canon_payload.py"
    if not p.exists():
        raise Blind(f"no canon payload at {p} — run the emitter in the canon repo")
    return _load_data(p)


def load_profile(explicit: Path | None):
    p = explicit or (HERE / "profile.py")
    if not p.exists():
        raise Blind(
            f"no profile at {p}. The profile is hand-written per repo and says "
            f"what to scan; without it this would either scan nothing (a silent "
            f"pass) or everything (a useless one)."
        )
    return _load_data(p)


# ---------------------------------------------------------------------------
# CANON_ID — semantic digest of registries + this engine's rules
# ---------------------------------------------------------------------------
def compute_canon_id(payload, payload_source=None, readme_source=None) -> str:
    """Digest the registries and this engine, identically on every Python.

    The first version hashed `ast.dump(ast.parse(...))` so that comment-only
    edits would not churn the digest. That was wrong in a way no local run
    revealed: ast.dump's output CHANGES BETWEEN PYTHON VERSIONS, so a payload
    emitted on 3.14 computed a different digest on the 3.11/3.12 that every
    repo's CI pins — the check exited 2, BLIND, in all of them. Developed on
    one interpreter, shipped to another.

    So the engine is hashed as normalised SOURCE TEXT: line endings unified,
    trailing whitespace dropped, blank lines dropped. Deterministic on every
    interpreter. The cost is that a comment edit does change the digest — which
    is honest, and the fix is the same one-command re-emit either way.

    `public_why` is included deliberately. It is the only free-prose field in
    the payload and the one whose text ships verbatim into public repos, so an
    edit to it must be caught by the same guard as an edit to a regex.
    """
    def norm(entries, keys):
        return sorted(
            [tuple(str(e.get(k, "")) for k in keys) for e in entries]
        )

    def normalise(text):
        return "\n".join(
            line.rstrip() for line in text.replace("\r\n", "\n").split("\n")
            if line.strip()
        )

    # EVERY file that travels, not just this one. The README claims the digest
    # makes a per-file edit report BLIND; it covered only check.py and the
    # payload, so editing a hook in a vendored copy — and the hooks are what run
    # on every Edit/Write — was invisible to it. profile.py is deliberately
    # absent: it is hand-written per repo and must differ.
    engine_parts = []
    for name in ("check.py", "hook_pretooluse.py", "hook_posttooluse.py",
                 "install-hooks.sh", "README.md", ".gitignore"):
        # README.md differs by audience — public repos receive a redacted one —
        # so the emitter passes the variant it is about to write. Reading the
        # private README while stamping the public payload made every public
        # repo report a mismatch.
        if name == "README.md" and readme_source is not None:
            text = readme_source
        else:
            f = HERE / name
            text = _read_text(f) if f.exists() else None
        engine_parts.append(name + "\n" +
                            (normalise(text) if text is not None else "<absent>"))
    engine = "\n".join(engine_parts)

    # The payload's OWN SOURCE, not just the values parsed out of it. The first
    # version digested a fixed set of keys, so anything else in the file —
    # another statement, an extra key, a comment carrying a leaked filename —
    # was invisible to the digest while the file was reported authentic. The
    # CANON_ID line is excluded so the digest does not have to contain itself.
    # The emitter passes the text it is ABOUT to write; at that moment the
    # file on disk is still the previous version, so reading it would digest
    # the wrong bytes and every consumer would report a mismatch.
    raw_payload = (payload_source
                   if payload_source is not None
                   else _read_text(HERE / "canon_payload.py"))
    payload_src = normalise("\n".join(
        line for line in raw_payload.split("\n")
        if not _CANON_ID_LINE.match(line)
    ))

    material = {
        "approved": norm(payload.APPROVED, ("name", "near", "variants",
                                            "public_why", "must_flag",
                                            "must_not_flag")),
        "retired": norm(payload.RETIRED, ("id", "pattern", "instead", "allow",
                                          "public_why", "must_flag",
                                          "must_not_flag")),
        "never": norm(payload.NEVER_BRANDS, ("name", "pattern", "instead",
                                             "allow", "public_why",
                                             "must_flag", "must_not_flag")),
        "definitions": norm(payload.DEFINITIONS, ("term", "status",
                                                  "public_why")),
        "engine": engine,
        "payload_source": payload_src,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# markdown masking
#
# `strip_code` is lifted verbatim from a sibling repository's relative-link
# checker, which already solved "blank the code, keep the
# line numbers" for this exact corpus. Everything after it is the additional
# masking a VOCABULARY check needs and a link check does not: all 24
# never-brand findings in one repository traced to a single FILENAME appearing
# in link destinations. A filename is an identifier, not prose.
# ---------------------------------------------------------------------------
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
# Bounded to a few lines. With bare re.S a SINGLE unpaired backtick swallowed
# everything up to the next one — potentially the rest of the document — and
# the masked-away region is silently unscanned. An inline code span that runs
# past a couple of lines is a typo, not a span.
CODE_SPAN_RE = re.compile(r"(`+)(?:(?!\1)[^\n]|\n(?!\n)){0,400}?\1", re.S)


def strip_code(text: str) -> str:
    """Blank out fenced blocks and inline code spans, preserving line numbers."""
    lines = text.split("\n")
    out: list[str] = []
    fence: str | None = None
    fence_indent = 0
    for line in lines:
        m = FENCE_RE.match(line)
        if fence is None:
            if m and (m.group(2)[0] == "`" or m.group(2)[0] == "~"):
                if m.group(2)[0] == "~" or "`" not in m.group(3):
                    fence = m.group(2)[0] * 3
                    fence_indent = len(m.group(1))
                    out.append("")
                    continue
            out.append(line)
        else:
            if (m and m.group(2)[0] == fence[0] and len(m.group(2)) >= 3
                    and m.group(3).strip() == ""
                    and len(m.group(1)) <= fence_indent + 3):
                fence = None
            out.append("")
    joined = "\n".join(out)
    # `_blank`, not `" " * len(...)`. CODE_SPAN_RE carries re.S, so an inline
    # code span that runs across lines matches the newlines too — replacing
    # them with spaces silently deletes lines from the document and every
    # finding after that point reports the wrong number. Measured on
    # forge-cli's README: a real hit on line 692 was reported as 549.
    return CODE_SPAN_RE.sub(_blank, joined)


_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
# [text](dest) / ![alt](dest) — keep the text, blank the destination.
_LINK_DEST = re.compile(r"(\]\()([^)]*)(\))")
_REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.M)
_AUTOLINK = re.compile(r"<https?://[^>]*>|\bhttps?://\S+|\bwww\.\S+")
# Same reasoning: `<` is common in prose ("a < b", "<400ms"). Unbounded with
# re.S, one stray `<` blanked every line up to the next `>`.
_HTML_TAG = re.compile(r"<[^<>]{0,400}>", re.S)
# A token containing a slash, or ending in a known source/doc suffix, is a path
# or a filename — an identifier, not prose.
_PATHISH = re.compile(
    r"\S*/\S*"
    r"|\b[\w.-]+\.(?:md|markdown|txt|py|ts|tsx|js|mjs|cjs|json|ya?ml|toml|ini|"
    r"cfg|sh|html?|css|lock|png|jpe?g|svg|docx?|xlsx?|pptx?|pdf)\b"
)


def _blank(m) -> str:
    """Replace a match with same-length whitespace, keeping newlines."""
    return "".join("\n" if c == "\n" else " " for c in m.group(0))


def mask_markdown(text: str) -> str:
    text = _FRONT_MATTER.sub(_blank, text)
    text = strip_code(text)
    text = _REF_DEF.sub(_blank, text)
    text = _LINK_DEST.sub(lambda m: m.group(1) + " " * len(m.group(2)) + m.group(3),
                          text)
    text = _AUTOLINK.sub(_blank, text)
    text = _HTML_TAG.sub(_blank, text)
    text = _PATHISH.sub(_blank, text)
    return text


# ---------------------------------------------------------------------------
# spans
# ---------------------------------------------------------------------------
class Span:
    """A run of scannable text, with the line it starts on and what it IS.

    `start_line` plus a newline count is enough to place any match, so a whole
    masked document and a single string literal are the same shape. That is
    what lets a near-miss regex whose `\\s+` spans a line break still report a
    usable line number.
    """

    __slots__ = ("text", "surface", "start_line", "rules")

    def __init__(self, text, surface, start_line=1, rules=None):
        self.text = text
        self.surface = surface
        self.start_line = start_line
        self.rules = rules  # None = every rule; a set = only these

    def line_of(self, pos: int) -> int:
        return self.start_line + self.text.count("\n", 0, pos)


def spans_markdown(text, surface):
    return [Span(mask_markdown(text), surface)]


def spans_text(text, surface):
    return [Span(text, surface)]


def spans_html(text, surface):
    body = re.sub(r"<(script|style)\b.*?</\1>", _blank, text,
                  flags=re.S | re.I)
    body = _HTML_TAG.sub(_blank, body)
    return [Span(body, surface)]


# Whole-file, one rule only. LICENSE / NOTICE / Dockerfile are not prose, but
# they carry the legal entity, and the entity is where the live defect is:
# forge-cli's Dockerfile stamps the wrong company into every published image's
# OCI vendor label. No docs-only scan would ever open this file.
def spans_plain_entity(text, surface):
    return [Span(text, PRODUCT, rules={"retired"})]


_COPY_KEYS = (
    "label", "title", "text", "description", "desc", "placeholder", "message",
    "help", "tooltip", "heading", "subtitle", "caption", "alt", "aria-label",
    "ariaLabel", "summary", "hint", "cta", "headline", "body",
)
_TS_COPY = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _COPY_KEYS) + r")\s*[:=]\s*"
    r"(['\"`])((?:(?!\1)[^\\]|\\.)*)\1",
    re.S,
)
# A JSX text node: between a closing '>' and the next '<', no braces (those are
# expressions, i.e. identifiers) and no quotes.
_JSX_TEXT = re.compile(r">([^<>{}'\"]{2,})<")
# Guard the URL case: `https://x` must not read as a line comment.
_TS_LINE_COMMENT = re.compile(r"(?<![:\w])//(.*)$", re.M)
_TS_BLOCK_COMMENT = re.compile(r"/\*(.*?)\*/", re.S)


def _line_at(text, pos):
    return text.count("\n", 0, pos) + 1


def spans_ts(text, surface):
    out = []
    for m in _TS_COPY.finditer(text):
        out.append(Span(m.group(2), surface, _line_at(text, m.start(2))))
    for m in _JSX_TEXT.finditer(text):
        if m.group(1).strip():
            out.append(Span(m.group(1), surface, _line_at(text, m.start(1))))
    for rx in (_TS_LINE_COMMENT, _TS_BLOCK_COMMENT):
        for m in rx.finditer(text):
            out.append(Span(m.group(1), DEV, _line_at(text, m.start(1))))
    return out


def spans_python(text, surface, all_strings=False):
    """Copy-shaped string constants, at the FILE's surface class.

    `surface`, not a hardcoded PRODUCT. A `description=` kwarg is user-facing
    in a CLI module and is an ORM column comment in a model file; which of
    those it is depends on where the file sits, and that is the profile's
    call. Hardcoding it turned a deliberately four-file surface tier into a
    whole-repo one and produced 23 hard failures in files nobody scoped in.
    Comments and docstrings stay developer prose regardless.

    forge-cli has 998 `help=` sites and 65 already carrying canon terms, none
    of them visible to any docs scan. `all_strings` is the profile's escape for
    files that ARE the copy — an error catalog, a voice file — where every
    literal is user-facing.
    """
    out = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        # Never silent. A file this checker cannot parse is a hole in the scan,
        # and a hole that prints is a hole someone can close.
        print(f"  product-guardrail: skipping unparseable Python ({exc.msg} "
              f"line {exc.lineno})", file=sys.stderr)
        return out

    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstring_nodes.add(id(body[0].value))
                out.append(Span(body[0].value.value, DEV,
                                body[0].value.lineno))

    copy_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg in _COPY_KEYS and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)):
                    copy_nodes.add(id(kw.value))
                    out.append(Span(kw.value.value, surface, kw.value.lineno))

    if all_strings:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in copy_nodes
                    and id(node) not in docstring_nodes):
                out.append(Span(node.value, surface, node.lineno))

    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out.append(Span(tok.string, DEV, tok.start[0]))
    except (tokenize.TokenError, IndentationError):
        pass  # already reported above if it was a real syntax problem
    return out


_JSON_COPY_KEYS = ("title", "description")


def spans_json(text, surface):
    """FLUID/FLUX schema files: `title` and `description` are product-facing.

    They render into the docs site and into UI hints, so they are copy even
    though they live in a data file.
    """
    out = []
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as exc:
        print(f"  product-guardrail: skipping unparseable JSON ({exc})",
              file=sys.stderr)
        return out

    values = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _JSON_COPY_KEYS and isinstance(v, str):
                    values.append(v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    # Line numbers are not recoverable from json.loads; report the file. A
    # schema description is short and greppable, so naming the string is
    # enough to find it.
    for v in values:
        out.append(Span(v, surface, 0))
    return out


EXTRACTORS = {
    ".md": spans_markdown,
    ".markdown": spans_markdown,
    ".txt": spans_text,
    ".html": spans_html,
    ".htm": spans_html,
    ".ts": spans_ts,
    ".tsx": spans_ts,
    ".js": spans_ts,
    ".mjs": spans_ts,
    ".json": spans_json,
}
PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".htm"}
ENTITY_FILENAMES = {"LICENSE", "NOTICE", "COPYING", "Dockerfile"}


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------
class Finding:
    __slots__ = ("rel", "rule", "line", "term", "want", "surface", "excerpt",
                 "why", "severity", "grace")

    def __init__(self, rel, rule, line, term, want, surface, excerpt, why):
        self.rel, self.rule, self.line = rel, rule, line
        self.term, self.want, self.surface = term, want, surface
        self.excerpt, self.why = excerpt, why
        self.severity = "FAIL"
        self.grace = None

    def key(self):
        """Identity for baseline/new-finding diffing.

        File, rule and term — and deliberately NOTHING positional. The line
        number is out because inserting a paragraph shifts every line below it,
        and a hook that then re-reports the whole file is a hook people turn
        off. The EXCERPT is out for a subtler reason that cost a test: it is a
        +/-48 character window, so appending a paragraph to a short file
        extends the window past the old end-of-file and the same finding comes
        back with a different key. Identity is therefore the count of each
        (file, rule, term): unchanged text is silent, and a term appearing one
        more time is exactly one new finding.
        """
        return (self.rel, self.rule, self.term)


_WS = re.compile(r"\s+")
# Anchored on the ASSIGNMENT, not the word. `startswith("CANON_ID")` dropped
# any column-0 line beginning with those characters — including this module's
# own docstring line and any smuggled `CANON_ID_NOTE = "..."` — from the digest
# while the file was still certified authentic. sync_guardrail.py already
# anchored properly; this is the same fix.
_CANON_ID_LINE = re.compile(r"^CANON_ID\s*=\s*['\"][0-9a-f]{64}['\"]\s*$")


def _norm(s: str) -> str:
    """Collapse whitespace. The near-miss regexes use \\s+, which legitimately
    spans line breaks; a literal newline inside a finding message does not."""
    return " ".join(s.split())


def _excerpt(text, start, end, width=48):
    lo = max(0, start - width)
    hi = min(len(text), end + width)
    return ("…" if lo else "") + _norm(text[lo:hi]) + ("…" if hi < len(text) else "")


def _window_allowed(text, start, end, allows, width=220):
    """The canon's ±220-char allow window: a document may QUOTE a forbidden term
    order to forbid it."""
    w = text[max(0, start - width):end + width]
    return any(re.search(a, w, re.I) for a in allows)


def localise(text, payload, locale):
    """Rewrite variant spellings in ADVICE text to this repo's locale.

    The payload is emitted from an en-GB canon, so `instead` and `public_why`
    say "Command Centre". Handing that to an en-US repo tells the developer to
    write the very spelling the guardrail just flagged. The names are canon;
    which spelling of them this repo uses is the profile's call, and the advice
    has to agree with the finding or nobody believes either.
    """
    if not text:
        return text
    for e in payload.APPROVED:
        variants = e.get("variants") or {}
        if not variants:
            continue
        target = variants.get(locale, e["name"])
        for spelling in variants.values():
            if spelling != target:
                text = text.replace(spelling, target)
    return text


def classify_name(matched, target, variants):
    if variants and matched in variants.values() and matched != target:
        return "name-locale"
    if matched.lower() == target.lower():
        return "name-casing"
    return "name-wrong"


def rule_names(payload, span, rel, locale):
    out = []
    for e in payload.APPROVED:
        variants = e.get("variants") or {}
        target = variants.get(locale, e["name"]) if variants else e["name"]
        for m in re.finditer(e["near"], span.text, re.I):
            matched = _norm(m.group(0))
            if matched == target:
                continue
            rule = classify_name(matched, target, variants)
            why = e.get("public_why", "")
            if rule == "name-locale":
                why = (f"This repo's product copy is {locale} "
                       f"(profile.py: LOCALE). {why}")
            out.append(Finding(rel, rule, span.line_of(m.start()), matched,
                               target, span.surface,
                               _excerpt(span.text, m.start(), m.end()),
                               localise(why, payload, locale)))
    return out


def rule_retired(payload, span, rel, locale="en-GB"):
    out = []
    for e in payload.RETIRED:
        for m in re.finditer(e["pattern"], span.text):
            if _window_allowed(span.text, m.start(), m.end(), e.get("allow", [])):
                continue
            out.append(Finding(rel, "retired", span.line_of(m.start()),
                               _norm(m.group(0)),
                               localise(e["instead"], payload, locale),
                               span.surface,
                               _excerpt(span.text, m.start(), m.end()),
                               localise(e.get("public_why", ""), payload,
                                        locale)))
    return out


def rule_never_brand(payload, span, rel, locale="en-GB"):
    out = []
    for e in payload.NEVER_BRANDS:
        for m in re.finditer(e["pattern"], span.text):
            if _window_allowed(span.text, m.start(), m.end(), e.get("allow", [])):
                continue
            out.append(Finding(rel, "never-brand", span.line_of(m.start()),
                               _norm(m.group(0)),
                               localise(e["instead"], payload, locale),
                               span.surface,
                               _excerpt(span.text, m.start(), m.end()),
                               localise(e.get("public_why", ""), payload,
                                        locale)))
    return out


RULE_SEVERITY = {
    # rule          product copy   developer prose
    "name-wrong":   ("FAIL",       "WARN"),
    "name-casing":  ("FAIL",       "WARN"),
    "name-locale":  ("FAIL",       "SKIP"),
    "retired":      ("FAIL",       "FAIL"),
    "never-brand":  ("FAIL",       "WARN"),
}


# ---------------------------------------------------------------------------
# suppressions and grace
# ---------------------------------------------------------------------------
# Capture everything after the quoted term and clean it up in code, rather
# than trying to express "an optional dash-introduced reason" as a regex. The
# regex version had `[—-]` as the separator, and in `... "Command Centre" -->`
# that class matched the first hyphen of the HTML comment terminator, leaving
# a reason of "->" — so a suppression with NO reason parsed as having one and
# the guard against bare opt-outs silently never fired.
_SUPPRESS = re.compile(
    r"product-guardrail:\s*allow\s+([a-z-]+)\s+[\"']([^\"']*)[\"']"
    r"(?P<rest>.*?)(?:-->|\*/|\n|$)",
    re.S,
)
_REASON_LEAD = "—–-:,. \t"
SUPPRESS_SCOPE = 5


def parse_suppressions(rel, raw):
    """Inline waivers, read from the RAW file — the masker blanks HTML
    comments, which is exactly where a markdown suppression lives."""
    found, bad = [], []
    for m in _SUPPRESS.finditer(raw):
        line = raw.count("\n", 0, m.start()) + 1
        reason = (m.group("rest") or "").strip().lstrip(_REASON_LEAD).strip()
        if not reason:
            # A bare opt-out is how suppressions become invisible. Make the
            # missing reason itself the finding.
            bad.append(Finding(rel, "suppression-without-reason", line,
                               m.group(1), "a reason after an em dash",
                               PRODUCT, _norm(m.group(0)),
                               "A suppression with no reason cannot be "
                               "reviewed, so it never gets removed."))
        found.append((line, m.group(1), _norm(m.group(2))))
    return found, bad


def suppressed(f, suppressions):
    for line, rule, term in suppressions:
        if rule == f.rule and term == f.term and line <= f.line <= line + SUPPRESS_SCOPE:
            return True
    return False


def grace_for(f, grace, today):
    """The canon's grace contract, plus an expiry.

    Grace is 'known findings, scheduled, owned, printed every run, suppressed
    from the exit code but never from sight — the list is meant to reach
    empty'. Nothing in the original forces it to; across six repos and several
    owners it will not empty on good intentions, so an entry that outlives its
    date stops suppressing and starts naming its owner.
    """
    for g in grace:
        # Anchored, like every other path match here. `g["path"] not in f.rel`
        # let one entry silently waive unrelated files.
        if g["rule"] != f.rule or not path_matches(f.rel, g["path"]):
            continue
        if g.get("expires") and g["expires"] < today:
            return ("EXPIRED", g)
        return ("GRACED", g)
    return (None, None)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    """UTF-8, with BOM-marked UTF-16 accepted.

    A tracked markdown file in one of these repos is UTF-16 LE with a BOM —
    almost certainly written by a Windows tool. Refusing it outright would mean
    a real document is permanently unscanned, and the alternative (guessing an
    encoding) is how mojibake gets scanned as if it were prose. A BOM is not a
    guess: it is the file declaring itself.
    """
    data = path.read_bytes()
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32-le"),
                     (b"\x00\x00\xfe\xff", "utf-32-be"),
                     (b"\xff\xfe", "utf-16-le"),
                     (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(enc)
    return data.decode("utf-8-sig")


def repo_root(start: Path) -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       cwd=start, capture_output=True, text=True)
    if r.returncode:
        raise Blind("not inside a git repository")
    return Path(r.stdout.strip())


def tracked_files(root: Path):
    """`git ls-files`, not rglob.

    Two reasons, both learned the hard way elsewhere. The largest repo carrying
    this is many gigabytes with node_modules, so rglob is the wrong tool. And the canon's own EXCLUDED
    carries a note about a run going red on an untracked .vuepress/dist build
    on one machine while the same commit was green on another — git's index
    makes untracked and ignored files structurally invisible, so two machines
    cannot disagree about one commit. A submodule appears as a single gitlink,
    which is also why a submodule directory excludes cleanly.
    """
    r = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                       capture_output=True, text=True)
    if r.returncode:
        raise Blind(f"git ls-files failed: {r.stderr.strip()}")
    return [p for p in r.stdout.split("\0") if p]


def path_matches(rel, pat):
    """Anchored path matching. NOT substring containment.

    Substring containment is how `"build/"` silently excluded all 986 files of
    forge-cli's `fluid_build/` package — including the CLI help strings the
    profile explicitly listed as product copy — and how `"forge/"` removed 84
    first-party Command Center files while excluding zero submodule files. An
    exclusion that quietly widens is worse than none, because the gate still
    reports success over the part it stopped looking at.

    Two forms, both anchored:
      contains "/"  -> a path prefix from the repo root, or that exact file.
                       "forge/" is the root submodule, never src/x/forge/.
      no "/"        -> one whole path SEGMENT, fnmatch-style, at any depth.
                       "node_modules" matches anywhere; "*.egg-info" too;
                       "CHANGELOG.md" matches that basename in any directory.
    """
    if not pat:
        return False
    if "/" in pat:
        # "./README.md" is how the recorder anchors a root-level file so it is
        # not treated as a basename glob matching every directory.
        core = pat[2:] if pat.startswith("./") else pat
        core = core.rstrip("/")
        return rel == core or rel.startswith(core + "/")
    return any(fnmatch.fnmatchcase(seg, pat) for seg in rel.split("/"))


def classify_path(rel, profile):
    if any(path_matches(rel, x) for x in profile.EXCLUDED):
        return None
    name = rel.rsplit("/", 1)[-1]
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if name in ENTITY_FILENAMES and getattr(profile, "SCAN_ENTITY_FILES", True):
        return ("entity", PRODUCT)
    if suffix == ".py":
        kind = "python"
    elif suffix in EXTRACTORS:
        kind = suffix
    else:
        return None
    in_surface = any(path_matches(rel, s) for s in profile.SURFACE_TIER)
    return (kind, PRODUCT if in_surface else DEV)


def spans_for(kind, surface, rel, text, profile):
    if kind == "entity":
        return spans_plain_entity(text, surface)
    if kind == "python":
        all_str = any(rel.endswith(s)
                      for s in getattr(profile, "PY_ALL_STRINGS", ()))
        return spans_python(text, surface, all_strings=all_str)
    return EXTRACTORS[kind](text, surface)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
class Scan:
    def __init__(self):
        self.findings = []
        self.files = 0
        self.bytes = 0
        self.spans = 0
        self.rules_evaluated = set()
        self.chars = 0          # non-whitespace characters actually handed to rules
        self.present = {}


def scan_one(payload, profile, rel, raw, kind, surface, scan):
    scan.files += 1
    scan.bytes += len(raw)
    try:
        spans = spans_for(kind, surface, rel, raw, profile)
    except Exception as exc:  # an extractor crash is blindness, not cleanliness
        raise Blind(f"extractor {kind} crashed on {rel}: "
                    f"{type(exc).__name__}")

    suppressions, bad = parse_suppressions(rel, raw)
    raw_findings = list(bad)

    for span in spans:
        scan.spans += 1
        # A Span object is not evidence that anything was scanned. Count the
        # characters the rules actually receive: a masker that blanks a whole
        # file still returns one Span, and the old floors counted that as work.
        span_chars = len(_WS.sub("", span.text))
        scan.chars += span_chars
        for name, fn in (("names", rule_names), ("retired", rule_retired),
                         ("never", rule_never_brand)):
            # An entity file (LICENSE/NOTICE/Dockerfile) is not prose; it opts
            # in to the entity rule only.
            if span.rules is not None and name not in span.rules:
                continue
            # A rule that only ever ran against empty text has not run.
            if span_chars:
                scan.rules_evaluated.add(name)
            if fn is rule_names:
                raw_findings.extend(fn(payload, span, rel, profile.LOCALE))
            else:
                raw_findings.extend(fn(payload, span, rel,
                                       profile.LOCALE))
        # Coverage counts only what a USER would read.
        if span.surface == PRODUCT:
            for e in payload.APPROVED:
                variants = e.get("variants") or {}
                target = (variants.get(profile.LOCALE, e["name"])
                          if variants else e["name"])
                n = len(re.findall(re.escape(target), span.text))
                if n:
                    scan.present[e["name"]] = scan.present.get(e["name"], 0) + n

    for f in raw_findings:
        if f.rule in RULE_SEVERITY:
            prod_sev, dev_sev = RULE_SEVERITY[f.rule]
            f.severity = prod_sev if f.surface == PRODUCT else dev_sev
            if f.severity == "SKIP":
                continue
        if suppressed(f, suppressions):
            continue
        scan.findings.append(f)


def run_scan(payload, profile, root, only=None):
    scan = Scan()
    unread = []
    today = date.today().isoformat()
    for rel in tracked_files(root):
        if only is not None and rel not in only:
            continue
        c = classify_path(rel, profile)
        if not c:
            continue
        kind, surface = c
        path = root / rel
        try:
            raw = _read_text(path)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # Silently dropping it meant the run reported success over a file
            # it never opened, and the floors never noticed because it was
            # counted in neither.
            unread.append((rel, type(exc).__name__, surface))
            continue
        # A prose file in the surface tier that yields nothing scannable is a
        # hole, not a pass. Learned upstream from a PDF with no text layer,
        # which scored exactly like a clean one.
        before = scan.chars
        scan_one(payload, profile, rel, raw, kind, surface, scan)
        # Tested on chars, not on span count. spans_markdown/_text/_html each
        # return exactly one Span unconditionally, so `scan.spans == before`
        # was never true and this branch could not fire — the guard whose whole
        # purpose is catching a file that extracted to nothing was dead code.
        if (surface == PRODUCT and kind in PROSE_SUFFIXES and raw.strip()
                and scan.chars == before):
            scan.findings.append(Finding(
                rel, "unreadable", 0, "(nothing extracted)", "readable prose",
                PRODUCT, "", "This file is in the surface tier but produced no "
                "scannable text, so a green result says nothing about it."))

    for rel, why, surf in unread:
        scan.findings.append(Finding(
            rel, "unreadable", 0, f"({why})", "a readable file", surf, "",
            "This tracked file could not be read, so nothing in it was "
            "checked. A green run says nothing about it."))

    for f in scan.findings:
        state, g = grace_for(f, profile.GRACE, today)
        if state == "GRACED":
            f.grace, f.severity = g, "GRACED"
        elif state == "EXPIRED":
            f.grace, f.severity = g, "FAIL"
            f.why = (f"grace EXPIRED {g['expires']} (owner: {g['owner']}) — "
                     f"{g['reason']}")
    return scan


def check_vacuity(scan, profile):
    """Refuse to certify a scan that did not happen.

    Generalised from a sibling repo's vocabulary gate, whose own header records
    the trap: a check that silently iterates zero files passes
    while proving nothing. A rule that never ran is the same failure one level
    up, so the active rule set is a floor too.
    """
    floors = [
        ("files scanned", scan.files, profile.MIN_FILES_SCANNED),
        ("bytes read", scan.bytes, profile.MIN_BYTES_READ),
        ("spans extracted", scan.spans, profile.MIN_SPANS_EXTRACTED),
        # The one that matters. The other three measure CONTAINERS — files
        # opened, raw bytes read, Span objects built — and all three can be
        # satisfied by a scan that handed the rules zero characters. Proven:
        # five files each opening with an unterminated code fence, every one
        # carrying a live violation, reported "canon holds" and exit 0.
        ("characters extracted", scan.chars,
         getattr(profile, "MIN_TEXT_EXTRACTED", max(1, profile.MIN_BYTES_READ // 20))),
    ]
    broken = [f"{n}: {got} (floor {want})" for n, got, want in floors if got < want]
    missing = {"names", "retired", "never"} - scan.rules_evaluated
    if missing:
        broken.append("rules that never ran: " + ", ".join(sorted(missing)))
    if broken:
        raise Blind(
            "the scan collapsed — " + "; ".join(broken)
            + ". Refusing to report success: fix the profile or this script "
              "rather than trusting a green result."
        )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def report(scan, payload, profile, strict):
    graced = [f for f in scan.findings if f.severity == "GRACED"]
    warns = [f for f in scan.findings if f.severity == "WARN"]
    fails = [f for f in scan.findings if f.severity == "FAIL"]

    def show(f, indent="      "):
        loc = f"{f.rel}:{f.line}" if f.line else f.rel
        print(f"  {loc}  [{f.surface}]")
        print(f"{indent}[{f.rule}] {f.term!r} -> {f.want!r}")
        if f.excerpt:
            print(f"{indent}{f.excerpt}")
        if f.why:
            print(f"{indent}{f.why}")

    if graced:
        print(f"\ngraced ({len(graced)}) — visible, scheduled, owned:")
        for f in graced[:20]:
            show(f)
            g = f.grace
            print(f"      grace: {g['reason']} (owner: {g['owner']}, "
                  f"expires {g.get('expires', 'never')})")
        if len(graced) > 20:
            print(f"  … and {len(graced) - 20} more")

    if warns:
        print(f"\ndeveloper-prose warnings ({len(warns)}):")
        for f in warns[:25]:
            show(f)
        if len(warns) > 25:
            print(f"  … and {len(warns) - 25} more")

    if fails:
        print(f"\nFINDINGS ({len(fails)}):")
        for f in fails[:40]:
            show(f)
        if len(fails) > 40:
            print(f"  … and {len(fails) - 40} more")

    # Coverage. Report-only, always printed. This is the rule that answers the
    # question the violation rules cannot: the product's drift is that the
    # canon vocabulary is ABSENT, and a near-miss regex never fires on a name
    # that never appears. Closing the gap means writing new product copy, which
    # is a product decision, not a lint fix — so this reports and never fails.
    total = len(payload.APPROVED)
    # Report the spelling THIS repo uses. Keying on the canonical name and
    # printing it verbatim told an en-US repo that "Command Centre" was
    # present 63 times, when what it found was "Command Center".
    shown = {}
    for e in payload.APPROVED:
        n = scan.present.get(e["name"], 0)
        if n:
            variants = e.get("variants") or {}
            shown[variants.get(profile.LOCALE, e["name"]) if variants
                  else e["name"]] = n
    present = shown
    print(f"\ncanon coverage (product surface): {len(present)} of {total} "
          f"approved names present")
    if present:
        print("  present: " + " · ".join(
            f"{k} ({v})" for k, v in sorted(present.items())))
    absent = []
    for e in payload.APPROVED:
        variants = e.get("variants") or {}
        name = variants.get(profile.LOCALE, e["name"]) if variants else e["name"]
        if name not in present:
            absent.append(name)
    if absent:
        print("  absent:  " + " · ".join(absent))

    bad = len(fails) + (len(warns) if strict else 0)
    print(f"\nscanned {scan.files} files, {scan.bytes} bytes, "
          f"{scan.spans} spans, {len(scan.rules_evaluated)} rules")
    if bad:
        print(f"{bad} finding(s) — the product vocabulary is off canon.")
        return 1
    print(f"canon holds: 0 failing findings, {len(warns)} warning(s), "
          f"{len(graced)} graced.")
    return 0


# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
def self_test(payload, profile):
    """Every travelling entry, both ways, THROUGH THE REAL EXTRACTORS.

    The registry samples prove the regexes survived the port. Pushing them
    through each format's extractor proves something the registry cannot: that
    the extractor did not silently blank everything. A masker that returns ""
    makes every rule pass, and byte counting alone would not catch it.
    """
    failures = []

    for e in payload.APPROVED:
        rx = re.compile(e["near"], re.I)
        m = rx.search(e["must_flag"])
        if not m or _norm(m.group(0)) == e["name"]:
            failures.append(f"  APPROVED {e['name']}: must_flag not a near-miss")
        m2 = rx.search(e["must_not_flag"])
        if m2 and _norm(m2.group(0)) != e["name"]:
            failures.append(f"  APPROVED {e['name']}: must_not_flag caught")

    for e in payload.RETIRED + payload.NEVER_BRANDS:
        eid = e.get("id") or e.get("name")
        allows = e.get("allow", [])

        def hit(sample):
            m = re.search(e["pattern"], sample)
            return bool(m) and not _window_allowed(sample, m.start(), m.end(),
                                                   allows)
        if not hit(e["must_flag"]):
            failures.append(f"  {eid}: must_flag NOT caught")
        if hit(e["must_not_flag"]):
            failures.append(f"  {eid}: must_not_flag WAS caught")

    # Extractor round-trip: a specimen in each format must survive extraction
    # and still be caught. Specimens are real shapes pulled from the repos.
    probe = "Data Product Studio"
    specimens = [
        ("doc.md", f"# Heading\n\nPublish from the {probe} to the catalog.\n"),
        # A filename-shaped link target. Deliberately generic: an earlier
        # version named a real document from a PRIVATE repo, and this file is
        # vendored into public ones.
        ("doc.md", f"See [{probe}](docs/SOME-RUNBOOK-NAME.md) for details.\n"),
        ("p.tsx", f'<h1>The {probe}</h1>'),
        ("p.tsx", f"const x = {{ name: 'CommandCenter', label: '{probe}' }};"),
        ("m.py", f'parser.add_argument("--x", help="Open the {probe}")'),
        ("s.json", '{"properties":{"a":{"description":"The %s runs it."}}}' % probe),
        ("index.html", f"<body><p>The {probe} ships weekly.</p></body>"),
        ("LICENSE", "Copyright 2026 Agentics Transformation Pty Ltd"),
    ]
    for name, text in specimens:
        c = classify_path(name, _ProbeProfile(profile))
        if not c:
            failures.append(f"  extractor: {name} is not classified as scannable")
            continue
        kind, surface = c
        try:
            spans = spans_for(kind, surface, name, text, profile)
        except Exception as exc:
            failures.append(f"  extractor {kind} crashed on {name}: "
                            f"{type(exc).__name__}")
            continue
        found = []
        for s in spans:
            if s.rules is not None and "names" not in s.rules:
                found += rule_retired(payload, s, name, profile.LOCALE)
                continue
            found += rule_names(payload, s, name, profile.LOCALE)
            found += rule_retired(payload, s, name, profile.LOCALE)
        if not found:
            failures.append(
                f"  extractor {kind}: specimen {name!r} produced no finding — "
                f"the extractor is blanking text it should keep")

    # A suppression with no reason must itself be a finding. The first version
    # of the parser used `[—-]` as the reason separator, which matched the
    # first hyphen of an HTML comment's `-->` and produced a reason of "->" —
    # so a bare opt-out looked reasoned and the guard never fired. A bare
    # `noqa` is how suppressions become invisible, and this is the only thing
    # standing between the codebase and one.
    for text, want_reason, label in (
        ('<!-- product-guardrail: allow name-locale "X" -->', False, "html, none"),
        ('<!-- product-guardrail: allow name-locale "X" — why -->', True, "html, em dash"),
        ('# product-guardrail: allow retired "X" - why\nnext', True, "hash, hyphen"),
        ('// product-guardrail: allow never-brand "X" — why */', True, "slash"),
        ('<!-- product-guardrail: allow name-locale "X" -->\n', False, "html, trailing nl"),
    ):
        found, bad = parse_suppressions("probe.md", text)
        if not found:
            failures.append(f"  suppression: {label} was not recognised at all")
            continue
        if want_reason and bad:
            failures.append(f"  suppression: {label} has a reason but was "
                            f"reported as reasonless")
        if not want_reason and not bad:
            failures.append(f"  suppression: {label} has NO reason and was "
                            f"accepted — bare opt-outs must be findings")

    # Masking must never change the LINE COUNT. Every masker blanks in place
    # for this reason, and one of them did not: CODE_SPAN_RE carries re.S, so a
    # multi-line inline code span was replaced by spaces, deleting newlines and
    # shifting every finding after it. A real hit on line 692 of forge-cli's
    # README reported as 549. Line numbers that quietly drift are worse than no
    # line numbers, because they send someone to the wrong place confidently.
    for label, sample in (
        ("multi-line inline code", "a\n`one\ntwo\nthree`\nb\n"),
        ("fenced block", "a\n```\nx\ny\n```\nb\n"),
        ("link with a path destination", "see [t](docs/A-B.md) here\nnext\n"),
        ("front matter", "---\nx: 1\ny: 2\n---\nbody\n"),
        ("html block", "<div\n  class='x'>\ntext\n</div>\n"),
    ):
        got = mask_markdown(sample)
        if got.count("\n") != sample.count("\n"):
            failures.append(
                f"  mask: {label} changed the line count "
                f"({sample.count(chr(10))} -> {got.count(chr(10))}); every "
                f"line number after it would be wrong")

    # The locale split is the whole Centre/Center resolution, so assert the
    # POLICY, not just the regex: this repo's spelling must pass and the other
    # one must come back as name-locale. Verified in both directions, because a
    # rule that silently stops firing is how a guardrail becomes decoration.
    for e in payload.APPROVED:
        variants = e.get("variants") or {}
        if not variants:
            continue
        target = variants.get(profile.LOCALE, e["name"])
        ok = Span(f"The {target} does the work.", PRODUCT)
        if rule_names(payload, ok, "probe.md", profile.LOCALE):
            failures.append(
                f"  locale: {target!r} is this repo's spelling but was flagged")
        for locale, spelling in sorted(variants.items()):
            if spelling == target:
                continue
            bad = Span(f"The {spelling} does the work.", PRODUCT)
            got = rule_names(payload, bad, "probe.md", profile.LOCALE)
            if not any(f.rule == "name-locale" for f in got):
                failures.append(
                    f"  locale: {spelling!r} ({locale}) should be name-locale "
                    f"in a {profile.LOCALE} repo, got "
                    f"{[f.rule for f in got] or 'nothing'}")

    # ... and the identifier must NOT flag while its sibling label does.
    ident_only = "const x = { name: 'CommandCenter', component: DataProductStudio };"
    spans = spans_ts(ident_only, PRODUCT)
    if any(rule_names(payload, s, "p.tsx", profile.LOCALE) for s in spans):
        failures.append("  extractor ts: a bare identifier was flagged as copy")

    for g in profile.GRACE:
        if not g.get("reason") or not g.get("owner") or g.get("owner") == "unknown":
            failures.append(f"  GRACE {g.get('path')!r}: needs a reason and an owner")

    if failures:
        print("product-guardrail self-test FAILED:")
        print("\n".join(failures))
        return 1
    n = len(payload.APPROVED) + len(payload.RETIRED) + len(payload.NEVER_BRANDS)
    print(f"product-guardrail self-test passed: {n} entries both ways, "
          f"{len(specimens)} extractor specimens")
    return 0


class _ProbeProfile:
    """Self-test specimens are named, not real. Treat every one as surface."""

    def __init__(self, real):
        self.EXCLUDED = ()
        self.SURFACE_TIER = ("",)
        self.SCAN_ENTITY_FILES = True
        self.LOCALE = real.LOCALE


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Product Guardrail")
    ap.add_argument("--strict", action="store_true",
                    help="developer-prose warnings also fail")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--canon-id", action="store_true",
                    help="print and verify the payload digest")
    ap.add_argument("--profile", type=Path)
    ap.add_argument("--root", type=Path)
    ap.add_argument("--stdin-content", action="store_true",
                    help="scan proposed content from stdin instead of the tree")
    ap.add_argument("--as", dest="as_path",
                    help="the repo-relative path stdin content will become")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    a = ap.parse_args(argv)

    try:
        payload = load_payload()
        profile = load_profile(a.profile)
        root = a.root or repo_root(HERE)

        stored = getattr(payload, "CANON_ID", None)
        actual = compute_canon_id(payload)
        # An ABSENT digest used to pass, because the guard read `if stored and
        # stored != actual`. Deleting one line was therefore the easiest way to
        # disable the tamper check — and deleting the line is exactly what
        # someone editing a vendored file would do to make it stop complaining.
        # No digest is not "nothing to check"; it is a payload that cannot be
        # trusted.
        if not stored:
            raise Blind(
                "the payload carries no CANON_ID, so nothing can be verified "
                "against it. Re-emit it from the canon repo rather than "
                "hand-writing one."
            )
        if stored != actual:
            raise Blind(
                f"CANON_ID mismatch — payload says {stored[:12]}…, this tree "
                f"computes {actual[:12]}…. canon_payload.py or check.py has "
                f"been hand-edited; restore them from the canon emitter."
            )
        if a.canon_id:
            print(actual)
            return 0

        if a.self_test:
            # 2, not 1: a failing self-test means the CHECKER is broken, which
            # the documented contract calls blindness. Returning 1 told CI the
            # prose was wrong.
            return 2 if self_test(payload, profile) else 0

        if a.stdin_content:
            if not a.as_path:
                raise Blind("--stdin-content requires --as PATH")
            raw = sys.stdin.read()
            c = classify_path(a.as_path, profile)
            if not c:
                return 0
            kind, surface = c
            scan = Scan()
            scan_one(payload, profile, a.as_path, raw, kind, surface, scan)
            today = date.today().isoformat()
            for f in scan.findings:
                state, g = grace_for(f, profile.GRACE, today)
                if state == "GRACED":
                    f.grace, f.severity = g, "GRACED"
            if a.format == "json":
                print(json.dumps([
                    dict(rel=f.rel, rule=f.rule, line=f.line, term=f.term,
                         want=f.want, surface=f.surface, excerpt=f.excerpt,
                         why=f.why, severity=f.severity)
                    for f in scan.findings], indent=2))
            else:
                for f in scan.findings:
                    print(f"{f.rel}:{f.line} [{f.rule}] {f.term!r} -> {f.want!r}")
            return 1 if any(f.severity == "FAIL" for f in scan.findings) else 0

        # The registries are verified BEFORE they are allowed to pass anything,
        # the same ordering check_all.py uses: a corrupt registry must fail
        # loudly rather than quietly bless a tree.
        if self_test(payload, profile):
            # A failing self-test means the registry or an extractor is
            # broken, which is blindness, not a vocabulary finding. Returning 1
            # here told CI "the prose is wrong" when the truth was "this
            # checker cannot be trusted".
            return 2
        scan = run_scan(payload, profile, root)
        check_vacuity(scan, profile)
        return report(scan, payload, profile, a.strict)

    except Blind as exc:
        print(f"\nproduct-guardrail BLIND: {exc}", file=sys.stderr)
        print("(exit 2 — this is not 'the vocabulary is clean'.)",
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        # Any unexpected failure is also blindness. Letting it escape as a
        # traceback gives a non-zero code CI cannot distinguish from a real
        # finding, and 1 would actively mislead.
        print(f"\nproduct-guardrail BLIND: unexpected {type(exc).__name__}: "
              f"{exc}", file=sys.stderr)
        print("(exit 2 — the checker failed; do not read this as clean.)",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
