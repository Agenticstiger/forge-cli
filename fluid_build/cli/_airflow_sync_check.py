# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Wait until Airflow has parsed the DAG files ``fluid schedule-sync`` pushed.

What a deploy needs to know is whether the files it pushed are live: imported by the
scheduler's DAG processor, with no import error. Airflow's API answers that only
indirectly, so this module settles it from what both majors report (measured on Airflow
2.10.5, 3.0.6 and 3.4.0):

* every clean parse of a file stamps its DAGs' ``last_parsed_time`` and, on Airflow 3,
  ``bundle_version``; a failed parse leaves both, flags the DAGs ``has_import_errors``
  (and stale, on Airflow 3) and stamps the file's import error again;
* a parse that was already running when the files landed read the old file, and Airflow
  runs one per DAG processor (each HA scheduler on Airflow 2, each DAG processor on
  Airflow 3). When a processor saves a clean parse, Airflow stores the file's source as
  it is on disk at that moment and serves it at ``dagSources``. A file is live once that
  stored source is the pushed file and Airflow has saved two clean parses since, the
  later at least 60 s after the parse it showed when the source first matched: Airflow
  stops a parse after ``dag_file_processor_timeout`` (50 s by default), so that parse
  began after the push. Two parses also cover a second processor that finishes an old
  parse late where that timeout was raised;
* where the stored source cannot be read (the user may not read DAG code), two clean
  parses 60 s apart pass the file as ``unproven``: parsed with no import error, but not
  shown to be the pushed file;
* a failure counts from two stamps of the file's import error, and is final once they are
  60 s apart;
* where the DAGs report a bundle version (an Airflow 3 Git bundle) and the pushed version
  is known (the commit a ``git+ssh`` push made, or ``--airflow-bundle-version``), the DAG
  at that version is the proof, after one parse. Import errors count from the moment the
  bundle is known to be at that version: from ``/api/v2/dagBundles`` (Airflow 3.4), or the
  earliest parse of a pushed DAG at that version;
* Airflow 3 shows the import error of a file with no registered DAG only to a user who may
  read all import errors, so a DAG that never appears is reported as not parsed, never
  taken for a clean file.

Files whose DAG ids cannot be read from the source (a factory, a loop) are judged by the
DAGs Airflow lists for them and by their import errors, and the wait is for them too.
A file that builds no DAG at module level (a helper module) is not waited for; an import
error Airflow shows for it during the wait still fails the check.

Every comparison is between stamps of Airflow's DAG processors: a parse or an import
error counts when it is newer than what Airflow had recorded at the first read after the
push, so a clock skew between this machine and Airflow cannot make an old parse look new.
``_sleep`` and ``_monotonic`` are module attributes so tests can drive the wait on a
simulated clock.
"""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from fluid_build.cli._airflow_api import AirflowApi, AirflowApiError, DagState, ImportErrorRecord

_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic

DEFAULT_POLL_S = 10.0
#: A DAG file larger than this is not read for its DAG ids (Airflow parses it regardless).
_MAX_SOURCE_BYTES = 4 * 1024 * 1024
#: Airflow stops a parse after ``dag_file_processor_timeout`` (50 s by default on both
#: majors). A parse stamped this long after a stamp that came after the push began after
#: the push; the 10 s on top absorb a clock skew between DAG-processor hosts.
PARSE_SPAN_S = 60.0
_PARSE_SPAN = timedelta(seconds=PARSE_SPAN_S)

E_IMPORT_ERRORS = "schedule_sync_airflow_import_errors"
E_NOT_PARSED = "schedule_sync_airflow_not_parsed"
E_UNREACHABLE = "schedule_sync_airflow_unreachable"
E_UNPROVEN = "schedule_sync_airflow_unproven"


# ── the files ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DagFile:
    """A pushed file Airflow will parse: its path under ``--dags-dir`` and its DAG ids.

    ``dynamic`` means the file builds DAGs whose ids could not be read from the source
    (a factory, a loop); Airflow decides. ``helper`` means it builds no DAG at module
    level (a module DAG files import), so it is not expected to show one. ``source`` is
    the text Airflow stores for the file once a processor has read it.
    """

    path: str
    dag_ids: Tuple[str, ...] = ()
    dynamic: bool = False
    helper: bool = False
    source: Optional[str] = None


def dag_files(dags_dir: Path) -> List[DagFile]:
    """The ``*.py`` files under ``dags_dir`` Airflow would parse, with their DAG ids.

    Airflow's safe-mode rule decides which files may hold a DAG (the bytes contain both
    ``airflow`` and ``dag``, any case: ``might_contain_dag_via_default_heuristic``);
    hidden paths, ``__pycache__`` and symlinks are skipped. ``.airflowignore`` is not read.
    """
    found: List[DagFile] = []
    for path in sorted(dags_dir.rglob("*.py")):
        parts = path.relative_to(dags_dir).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        lowered = data.lower()
        if b"airflow" not in lowered or b"dag" not in lowered:
            continue
        small = len(data) <= _MAX_SOURCE_BYTES
        ids = _dag_ids(data) if small else ()
        helper = not ids and small and not _builds_dags(data)
        found.append(
            DagFile(
                path="/".join(parts),
                dag_ids=ids,
                dynamic=not ids and not helper,
                helper=helper,
                source=normal_source(data.decode("utf-8", "replace")),
            )
        )
    return found


def normal_source(text: str) -> str:
    """Source text as Airflow stores it: read in text mode, so every line ends in ``\\n``."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _builds_dags(source: bytes) -> bool:
    """Whether module-level code calls something named like a DAG builder.

    ``DAG(...)``, ``create_dag(...)``, ``build_dags()``: a call whose name holds ``dag``,
    outside function and class bodies. A module that only defines helpers builds none.
    A source that does not parse counts as building DAGs: Airflow will report on it.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return True
    stack: List[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call) and "dag" in (_callee(node.func) or "").lower():
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


#: Airflow's dag_id grammar (``KEY_REGEX``) and length cap; anything else is no DAG id.
_DAG_ID = re.compile(r"[\w.-]{1,250}")


def _dag_ids(source: bytes) -> Tuple[str, ...]:
    """The literal DAG ids a module defines, in source order; ``()`` if none can be read.

    ``DAG(dag_id=...)`` / ``DAG("...")`` (any ``X.DAG``), ``@dag(dag_id=...)`` and a bare
    ``@dag`` (the function name), with the id a string literal, a module-level string
    constant, or an f-string or ``+`` of those. A source too deep for the parser (or for
    this reader's recursion) reads as no id: Airflow then decides.
    """
    try:
        return tuple(i for i in _read_dag_ids(source) if _DAG_ID.fullmatch(i))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ()


def _read_dag_ids(source: bytes) -> Tuple[str, ...]:
    tree = ast.parse(source)
    constants: Dict[str, str] = {}
    for statement in tree.body:
        target: Optional[ast.expr] = None
        value: Optional[ast.expr] = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        if isinstance(target, ast.Name):
            literal = _literal(value, constants)
            if literal is not None:
                constants[target.id] = literal
    found: List[Tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node.func) == "DAG":
            literal = _literal(_id_argument(node), constants)
            if literal:
                found.append((node.lineno, node.col_offset, literal))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                call = deco if isinstance(deco, ast.Call) else None
                if _callee(call.func if call else deco) != "dag":
                    continue
                argument = _id_argument(call) if call else None
                literal = node.name if argument is None else _literal(argument, constants)
                if literal:
                    found.append((deco.lineno, deco.col_offset, literal))
    return tuple(dict.fromkeys(dag_id for _, _, dag_id in sorted(found)))


def _callee(node: Any) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _id_argument(call: ast.Call) -> Optional[ast.AST]:
    for keyword in call.keywords:
        if keyword.arg == "dag_id":
            return keyword.value
    return call.args[0] if call.args else None


def _literal(node: Any, constants: Dict[str, str]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.JoinedStr):
        pieces: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                pieces.append(value.value)
            elif (
                isinstance(value, ast.FormattedValue)
                and value.conversion == -1
                and value.format_spec is None
            ):
                inner = _literal(value.value, constants)
                if inner is None:
                    return None
                pieces.append(inner)
            else:
                return None
        return "".join(pieces)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left, constants), _literal(node.right, constants)
        return None if left is None or right is None else left + right
    return None


# ── the result ───────────────────────────────────────────────────────────


@dataclass
class FileResult:
    """One file's outcome: ``clean``, ``unproven``, ``broken``, ``pending`` or ``unchecked``
    (a helper module, not waited for)."""

    path: str
    dag_ids: Tuple[str, ...]
    state: str = "pending"
    #: ``bundle-version`` or ``source`` for a clean file, ``parse-time`` for an unproven one.
    evidence: str = ""
    detail: str = ""
    #: The last line of the latest import error Airflow showed for the file.
    error: str = ""

    def to_report(self) -> Dict[str, Any]:
        from fluid_build.observability.secret_redactor import redact_secret_text

        return {
            "path": self.path,
            "dag_ids": list(self.dag_ids),
            "state": self.state,
            "evidence": self.evidence,
            "detail": redact_secret_text(self.detail),
            "error": redact_secret_text(self.error),
        }


@dataclass
class CheckResult:
    passed: bool
    files: List[FileResult]
    event: Optional[str]
    message: str
    notes: List[str] = field(default_factory=list)
    polls: int = 0
    waited_s: float = 0.0
    #: ``False`` for a pass where some file parsed cleanly but its source could not be
    #: read back, so the parse is not proven to be of the pushed file.
    proven: bool = True

    def to_report(self) -> Dict[str, Any]:
        from fluid_build.observability.secret_redactor import redact_secret_text

        return {
            "result": ("passed" if self.proven else "unproven") if self.passed else "failed",
            "event": self.event,
            "message": redact_secret_text(self.message),
            "notes": [redact_secret_text(note) for note in self.notes],
            "polls": self.polls,
            "waited_s": round(self.waited_s, 1),
            "files": [f.to_report() for f in self.files],
        }


# ── the wait ─────────────────────────────────────────────────────────────


@dataclass
class _DagSeen:
    last: Optional[DagState] = None
    #: Whether Airflow parsed it from the pushed file (``None``: not read yet).
    ours: Optional[bool] = None
    #: The parse Airflow had already recorded when the wait first read the DAG: not one
    #: of the synced file. ``absent`` marks a DAG Airflow did not list then.
    baseline: Optional[datetime] = None
    read: bool = False
    absent: bool = False
    clean_parses: Set[datetime] = field(default_factory=set)
    #: The parse recorded when Airflow's stored source was first seen to be the pushed
    #: file, and the clean parses after it.
    match_baseline: Optional[datetime] = None
    clean_after_match: Set[datetime] = field(default_factory=set)


@dataclass
class _FileSeen:
    file: DagFile
    result: FileResult
    failed_parses: Set[datetime] = field(default_factory=set)
    error_now: Optional[ImportErrorRecord] = None
    latest_error: Optional[ImportErrorRecord] = None
    #: A decision that stands: clean, unproven, or broken from failures far enough apart.
    final: bool = False
    #: The import error Airflow had already stamped when the wait first read the file.
    error_baseline: Optional[datetime] = None
    errors_read: bool = False
    #: Airflow's stored source for the file: ``unread``, ``match`` (the pushed file),
    #: ``differs`` or ``unreadable``.
    source_state: str = "unread"


_NEVER = datetime.min.replace(tzinfo=timezone.utc)
#: Line breaks and control characters, kept out of every console line built from server
#: or file text.
_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_HEX = re.compile(r"[0-9a-fA-F]+")
#: How often a long wait says what it still waits for.
_PROGRESS_EVERY_S = 60.0


def _stamp(error: ImportErrorRecord) -> datetime:
    return error.timestamp or _NEVER


def _one_line(text: Any) -> str:
    return _UNPRINTABLE.sub(" ", str(text))


def _last_line(stack: str) -> str:
    lines = [line.strip() for line in stack.strip().splitlines() if line.strip()]
    return _one_line(lines[-1][:300]) if lines else ""


def _version_matches(reported: Optional[str], expected: str) -> bool:
    """Whether a bundle version is the expected one; an abbreviated commit (7 or more hex
    digits, ``git rev-parse --short``) matches the full one Airflow reports."""
    if reported is None:
        return False
    if reported == expected:
        return True
    return (
        len(expected) >= 7
        and _HEX.fullmatch(expected) is not None
        and _HEX.fullmatch(reported) is not None
        and reported.lower().startswith(expected.lower())
    )


def _spans(stamps: Set[datetime]) -> bool:
    """Two stamps or more, the latest at least :data:`PARSE_SPAN_S` after the earliest."""
    return len(stamps) >= 2 and max(stamps) - min(stamps) >= _PARSE_SPAN


def _proven_after_match(seen: _DagSeen) -> bool:
    """Two clean parses after the one Airflow showed when the stored source first matched,
    the later at least :data:`PARSE_SPAN_S` after it."""
    after, since = seen.clean_after_match, seen.match_baseline
    return since is not None and len(after) >= 2 and max(after) - since >= _PARSE_SPAN


class _Wait:
    def __init__(
        self,
        client: AirflowApi,
        files: Sequence[DagFile],
        *,
        expected_version: Optional[str],
        version_explicit: bool,
        bundle: Optional[str],
        indirect: bool,
    ) -> None:
        self.client = client
        self.expected = expected_version
        self.explicit = version_explicit
        self.bundle = bundle
        self.indirect = indirect
        self.files = [_FileSeen(f, FileResult(path=f.path, dag_ids=f.dag_ids)) for f in files]
        self.dags: Dict[str, _DagSeen] = {}
        self.listed_at_start: Optional[Set[str]] = None
        self.bundle_at_version: Optional[datetime] = None
        self.versioned_seen = False
        #: Where the pushed files landed, as Airflow names them (an absolute folder, or ""
        #: for a bundle-relative path), learned from the DAGs of files forge read ids from.
        self.anchors: Set[str] = set()

    # one poll ────────────────────────────────────────────────────────────

    def poll(self) -> None:
        errors = self.client.import_errors()
        if self.expected and self.client.dialect == "v2":
            self._read_bundles()
        open_files = [fs for fs in self.files if not fs.final]
        listing = self.client.dags() if any(fs.file.dynamic for fs in open_files) else []
        if listing and self.listed_at_start is None:
            self.listed_at_start = {d.dag_id for d in listing}
        elif self.listed_at_start is None and any(fs.file.dynamic for fs in open_files):
            self.listed_at_start = set()
        # Files with known DAG ids first: their DAGs show where the files landed, which
        # then tells a file of ours from a same-named one in another directory.
        for fs in sorted(open_files, key=lambda f: f.file.dynamic):
            if fs.file.helper:
                continue
            if fs.file.dynamic:
                self._attribute(fs, listing)
            for dag_id in fs.result.dag_ids:
                state = self.client.dag(dag_id)
                self._observe(dag_id, state, fs.file.path)
                if state is not None and not fs.file.dynamic and self.dags[dag_id].ours:
                    self._learn_anchor(fs.file.path, state)
            if self._needs_source(fs):
                self._read_source(fs)
        self._read_errors(errors, open_files)
        for fs in open_files:
            self._decide(fs)

    def _read_bundles(self) -> None:
        for bundle in self.client.bundles() or []:
            if self.bundle and bundle.name != self.bundle:
                continue
            if bundle.version is not None:
                self.versioned_seen = True
            if (
                self.expected
                and _version_matches(bundle.version, self.expected)
                and self.bundle_at_version is None
            ):
                # The bundle switched to the pushed version at or before this refresh.
                self.bundle_at_version = bundle.last_refreshed

    def _attribute(self, fs: _FileSeen, listing: List[DagState]) -> None:
        """The DAGs Airflow lists for a file forge read no id from.

        Airflow's list leaves stale DAGs out, and a failed parse makes a DAG stale on
        Airflow 3, so a DAG once listed stays with the file while Airflow flags it with an
        import error. One that is stale without an error, gone, or found to live in another
        file is dropped: the file no longer defines it.
        """
        rel = fs.file.path
        kept = [dag_id for dag_id in fs.result.dag_ids if self._still_defines(dag_id, rel)]
        listed = [d.dag_id for d in listing if self._is_ours(d, rel) and self._in_bundle(d)]
        for dag_id in listed:
            if dag_id not in (self.listed_at_start or set()):
                # Not listed when the wait began: every parse of it is a new one.
                self.dags.setdefault(dag_id, _DagSeen()).absent = True
        fs.result.dag_ids = tuple(dict.fromkeys([*kept, *listed]))

    def _still_defines(self, dag_id: str, rel: str) -> bool:
        seen = self.dags.get(dag_id)
        state = seen.last if seen else None
        if state is None:
            return seen is None  # listed, not read yet: keep; read and gone: drop
        return self._is_ours(state, rel) and (state.active or state.has_import_errors)

    def _learn_anchor(self, rel: str, state: DagState) -> None:
        for where in (state.fileloc, state.relative_fileloc):
            name = where.replace("\\", "/")
            if name == rel:
                self.anchors.add("")
            elif name.endswith("/" + rel):
                self.anchors.add(name[: -len(rel)])

    def _at(self, name: str, rel: str) -> bool:
        """Whether Airflow's file name is the pushed file ``rel``."""
        name = name.replace("\\", "/")
        if not name:
            return False
        if self.anchors:
            return any(name == anchor + rel for anchor in self.anchors)
        return name == rel or name.endswith("/" + rel)

    def _is_ours(self, dag: DagState, rel: str) -> bool:
        return self._at(dag.fileloc, rel) or self._at(dag.relative_fileloc, rel)

    def _in_bundle(self, dag: DagState) -> bool:
        return not self.bundle or dag.bundle_name in (None, self.bundle)

    def _observe(self, dag_id: str, state: Optional[DagState], rel: str) -> None:
        """Record one read of a DAG.

        A parse counts when it is newer than the one Airflow had recorded at the first
        read after the push (or the DAG was not listed then). Both stamps come from the
        DAG processor, so a skew between its clock and this machine's, or the API
        server's, cannot make an old parse look new.
        """
        seen = self.dags.setdefault(dag_id, _DagSeen())
        first, seen.read = not seen.read, True
        seen.last = state
        if state is None:
            seen.absent = True
            return
        # A DAG of that id parsed from another file (another team's copy, an old path
        # Airflow still parses) says nothing about the pushed one.
        seen.ours = self._is_ours(state, rel)
        if state.bundle_version is not None:
            self.versioned_seen = True
        stamp = state.last_parsed_time
        if stamp is None:
            return
        if first and not seen.absent:
            seen.baseline = stamp
            return
        newer = seen.baseline is None or stamp > seen.baseline
        if seen.ours and newer and state.active and not state.has_import_errors:
            seen.clean_parses.add(stamp)
            if seen.match_baseline is not None and stamp > seen.match_baseline:
                seen.clean_after_match.add(stamp)

    def _needs_source(self, fs: _FileSeen) -> bool:
        """Whether this file's proof now hangs on Airflow's stored source."""
        if fs.file.source is None or fs.source_state == "unreadable" or not fs.result.dag_ids:
            return False
        verdicts = [self._dag_clean(dag_id) for dag_id in fs.result.dag_ids]
        if all(evidence == "bundle-version" for _, evidence, _ in verdicts):
            return False
        return any(
            (seen := self.dags.get(dag_id)) is not None and bool(seen.clean_parses)
            for dag_id in fs.result.dag_ids
        )

    def _read_source(self, fs: _FileSeen) -> None:
        """Compare the source Airflow stored for the file with the pushed file.

        Airflow stores a file's source when a processor syncs a clean parse: 2.x reads it
        from disk at that moment, so a parse that began before the file landed can store
        the new source. The parses that count as proof are therefore the ones after the
        parse Airflow showed when the stored source was first seen to be the pushed file.
        It is read again at every poll: a source that changes again resets the count.
        """
        state = next(
            (
                seen.last
                for seen in (self.dags.get(dag_id) for dag_id in fs.result.dag_ids)
                if seen is not None and seen.last is not None and seen.ours
            ),
            None,
        )
        if state is None:
            return
        stored = self.client.dag_source(state)
        if stored is None:
            fs.source_state = "unreadable"
            return
        matched = normal_source(stored) == fs.file.source
        fs.source_state = "match" if matched else "differs"
        for dag_id in fs.result.dag_ids:
            seen = self.dags.get(dag_id)
            if seen is None:
                continue
            if not matched:
                seen.match_baseline, seen.clean_after_match = None, set()
            elif seen.match_baseline is None:
                # The parse Airflow shows now may have begun before the push: count after it.
                # A DAG Airflow does not list yet gets every parse counted: the previous
                # file never registered it, so no parse of that file can show it clean.
                stamp = seen.last.last_parsed_time if seen.last is not None else None
                seen.match_baseline = stamp or _NEVER

    def _read_errors(self, errors: List[ImportErrorRecord], open_files: List[_FileSeen]) -> None:
        """Record this poll's import errors of the pushed files.

        Airflow stamps a file's import error again at every failed parse. Without a
        bundle version, a stamp counts when it is newer than the one Airflow showed at
        the first read; with one, from when the bundle is known to be at that version.
        """
        for fs in open_files:
            fs.error_now = None
        for error in errors:
            if self.bundle and error.bundle_name not in (None, self.bundle):
                continue
            for fs in open_files:
                if not self._at(error.filename, fs.file.path):
                    continue
                fs.error_now = error
                if fs.latest_error is None or _stamp(error) >= _stamp(fs.latest_error):
                    fs.latest_error = error
        versioned = self._versioned()
        reference = self._version_reference() if versioned else None
        for fs in open_files:
            current = fs.error_now
            stamp = current.timestamp if current is not None else None
            if not fs.errors_read:
                fs.errors_read, fs.error_baseline = True, stamp
                if not versioned:
                    continue
            if stamp is None:
                continue
            if versioned:
                if reference is not None and stamp >= reference:
                    fs.failed_parses.add(stamp)
            elif fs.error_baseline is None or stamp > fs.error_baseline:
                fs.failed_parses.add(stamp)

    def _versioned(self) -> bool:
        return bool(self.expected) and (self.explicit or self.versioned_seen)

    def _version_reference(self) -> Optional[datetime]:
        """From when an import error is an error of the pushed bundle version."""
        if not self.expected:
            return None
        at_version = [
            seen.last.last_parsed_time
            for seen in self.dags.values()
            if seen.last is not None
            and seen.ours
            and _version_matches(seen.last.bundle_version, self.expected)
            and seen.last.last_parsed_time is not None
        ]
        candidates = [t for t in [self.bundle_at_version, *at_version] if t is not None]
        return min(candidates) if candidates else None

    # deciding ────────────────────────────────────────────────────────────

    def _dag_clean(self, dag_id: str) -> Tuple[bool, str, str]:
        """``(clean, evidence, why not)`` for one DAG, from what has been seen of it."""
        seen = self.dags.get(dag_id)
        dag_id = _one_line(dag_id)  # a DAG Airflow listed for a dynamic file: its own text
        state = seen.last if seen else None
        if seen is None or state is None:
            return False, "", f"Airflow lists no DAG {dag_id} yet"
        if seen.ours is False:
            where = _one_line(state.relative_fileloc or state.fileloc)[-120:]
            return False, "", f"Airflow lists DAG {dag_id} from {where}, not from the synced file"
        if self.bundle and state.bundle_name not in (None, self.bundle):
            bundle = _one_line(state.bundle_name)[:60]
            return False, "", f"DAG {dag_id} is in bundle {bundle}, not {self.bundle}"
        if state.has_import_errors:
            return False, "", f"Airflow reports an import error for the file of DAG {dag_id}"
        if not state.active:
            return False, "", f"Airflow marks DAG {dag_id} stale"
        if self.expected and (self.explicit or state.bundle_version is not None):
            want = _one_line(self.expected[:12])
            if _version_matches(state.bundle_version, self.expected):
                return True, "bundle-version", f"at bundle version {want}"
            if state.bundle_version is None:
                return (
                    False,
                    "",
                    f"Airflow reports no bundle version for DAG {dag_id} (its bundle is not "
                    f"versioned), so it cannot reach {want}",
                )
            return (
                False,
                "",
                f"DAG {dag_id} is still at bundle version {_one_line(state.bundle_version[:12])}, "
                f"waiting for {want}",
            )
        parses = len(seen.clean_parses)
        if parses >= 2:
            return True, "parse-time", "parsed twice since the sync"
        if parses == 1:
            return (
                False,
                "",
                f"Airflow parsed DAG {dag_id} once since the sync; that parse may have read "
                "the previous file",
            )
        return False, "", f"Airflow has not parsed DAG {dag_id} since the sync"

    def _decide(self, fs: _FileSeen) -> None:
        result = fs.result
        if fs.latest_error is not None:
            result.error = _last_line(fs.latest_error.stack_trace)
        failing = fs.error_now is not None and len(fs.failed_parses) >= 2
        # Failures further apart than a parse can run are not all parses of the previous
        # file that were still running when the files landed.
        fresh_failure = failing and _spans(fs.failed_parses)
        if fs.file.helper:
            # A module DAG files import shows no DAG of its own, so the wait is not for it;
            # an import error Airflow shows for it meanwhile still fails the check.
            if fresh_failure:
                result.state, result.evidence, fs.final = "broken", "", True
                result.detail = f"import error: {result.error}" if result.error else "import error"
            else:
                result.state = "unchecked"
                result.detail = "builds no DAG at module level (a helper module): not waited for"
            return
        if failing:
            result.state, result.evidence = "broken", ""
            result.detail = f"import error: {result.error}" if result.error else "import error"
            # Where Airflow reads the destination on its own schedule, the failures may be
            # the previous file's: that verdict waits for the deadline, a fix may land.
            fs.final = fresh_failure and not (self.indirect and not self._versioned())
            return
        if not result.dag_ids:
            result.state = "pending"
            result.detail = (
                f"Airflow shows an import error for it: {result.error}"
                if fs.error_now is not None
                else "Airflow lists no DAG from it and shows no import error for it yet"
            )
            return
        verdicts = [self._dag_clean(dag_id) for dag_id in result.dag_ids]
        waiting = [why for clean, _, why in verdicts if not clean]
        if waiting:
            result.state, result.detail = "pending", "; ".join(waiting)
            return
        if all(evidence == "bundle-version" for _, evidence, _ in verdicts):
            result.state, result.evidence, fs.final = "clean", "bundle-version", True
            result.detail = "; ".join(dict.fromkeys(v[2] for v in verdicts))
            return
        parse_time = [
            self.dags[dag_id]
            for dag_id, (_, evidence, _) in zip(result.dag_ids, verdicts, strict=True)
            if evidence != "bundle-version"
        ]
        span = f"{PARSE_SPAN_S:.0f}s"
        result.state = "pending"
        if fs.source_state == "match":
            if all(_proven_after_match(seen) for seen in parse_time):
                result.state, result.evidence, fs.final = "clean", "source", True
                result.detail = "Airflow stores the pushed file and has parsed it since"
            else:
                result.detail = (
                    "Airflow stores the pushed file; waiting for two parses after that, the "
                    f"later at least {span} on"
                )
            return
        if fs.source_state == "unreadable" or fs.file.source is None:
            if all(_spans(seen.clean_parses) for seen in parse_time):
                result.state, result.evidence, fs.final = "unproven", "parse-time", True
                result.detail = (
                    "parsed with no import error, but Airflow's stored source could not be "
                    "read (the user may not read DAG code), so the parse is not proven to be "
                    "of the pushed file"
                )
            else:
                result.detail = (
                    f"parsed twice since the sync; waiting for a parse at least {span} after "
                    "the first, which cannot be one that began before the push"
                )
            return
        result.detail = "Airflow's stored source is not the pushed file yet"

    def blocking(self) -> List[_FileSeen]:
        """Every file the wait is for: all but helper modules."""
        return [fs for fs in self.files if not fs.file.helper]

    def done(self) -> bool:
        return all(fs.final for fs in self.blocking())

    def progress(self, elapsed: float) -> str:
        waiting = [fs.result for fs in self.blocking() if not fs.final]
        shown = "; ".join(f"{_one_line(r.path)} ({r.detail})" for r in waiting[:3])
        more = f"; and {len(waiting) - 3} more" if len(waiting) > 3 else ""
        return f"still waiting ({elapsed:.0f}s): {len(waiting)} DAG file(s) not live yet: {shown}{more}"


def wait_for_parse(
    client: AirflowApi,
    files: Sequence[DagFile],
    *,
    wait_s: float,
    poll_s: float = DEFAULT_POLL_S,
    expected_version: Optional[str] = None,
    version_explicit: bool = False,
    bundle: Optional[str] = None,
    indirect: bool = False,
    sleep: Optional[Callable[[float], None]] = None,
    monotonic: Optional[Callable[[], float]] = None,
    on_change: Optional[Callable[[FileResult], None]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> CheckResult:
    """Poll ``client`` until every pushed file is clean or broken, or ``wait_s`` runs out."""
    sleep = sleep or _sleep
    monotonic = monotonic or _monotonic
    wait = _Wait(
        client,
        files,
        expected_version=expected_version,
        version_explicit=version_explicit,
        bundle=bundle,
        indirect=indirect,
    )
    start = monotonic()
    deadline = start + wait_s
    # A request never outlives the wait: its timeout is capped to the time left.
    client.set_deadline(deadline, monotonic)
    polls = 0
    last_progress = start
    transient: Optional[AirflowApiError] = None
    fatal: Optional[AirflowApiError] = None
    announced: Set[str] = set()
    while True:
        polls += 1
        try:
            wait.poll()
            transient = None
        except AirflowApiError as exc:
            if not exc.transient:
                fatal = exc
                break
            if exc.out_of_time:
                break  # the wait ran out: the last poll's outcome stands
            transient = exc
        for fs in wait.files:
            if on_change and fs.final and fs.result.path not in announced:
                on_change(fs.result)
                announced.add(fs.result.path)
        if wait.done():
            break
        now = monotonic()
        if on_progress and now - last_progress >= _PROGRESS_EVERY_S:
            last_progress = now
            on_progress(wait.progress(now - start))
        remaining = deadline - now
        if remaining <= 0:
            break
        sleep(min(poll_s, remaining))
    client.set_deadline(None, monotonic)
    return _conclude(wait, polls, monotonic() - start, wait_s, fatal, transient)


def _conclude(
    wait: _Wait,
    polls: int,
    waited: float,
    wait_s: float,
    fatal: Optional[AirflowApiError],
    transient: Optional[AirflowApiError],
) -> CheckResult:
    results = [fs.result for fs in wait.files]
    notes = _notes(wait)

    def done(passed: bool, event: Optional[str], message: str) -> CheckResult:
        return CheckResult(passed, results, event, message, notes, polls, waited)

    if fatal is not None:
        message = fatal.message + (f" {fatal.fix}" if fatal.fix else "")
        return done(False, fatal.event, message)
    broken = [r for r in results if r.state == "broken"]
    if broken:
        listed = "; ".join(f"{_one_line(r.path)} ({r.error or 'import error'})" for r in broken)
        return done(
            False,
            E_IMPORT_ERRORS,
            f"Airflow could not import {len(broken)} of {len(results)} synced DAG file(s): "
            f"{listed}.",
        )
    blocking = wait.blocking()
    if not blocking:
        return done(
            False,
            E_NOT_PARSED,
            "nothing was checked: every synced file builds no DAG at module level.",
        )
    total = len(blocking)
    pending = [fs.result for fs in blocking if fs.result.state not in ("clean", "unproven")]
    if pending and transient is not None:
        return done(False, E_UNREACHABLE, f"Airflow stopped answering: {transient.message}")
    if pending:
        listed = "; ".join(f"{_one_line(r.path)} ({r.detail})" for r in pending)
        unlisted = any(not r.dag_ids for r in pending)
        return done(
            False,
            E_NOT_PARSED,
            f"Airflow did not show {len(pending)} of {total} synced DAG file(s) live within "
            f"{wait_s:.0f}s: {listed}. Raise --airflow-wait if Airflow parses slowly here "
            "(min_file_process_interval)"
            + (
                "; Airflow lists a new file only every dag_dir_list_interval (Airflow 2) or "
                "refresh_interval (Airflow 3), 300 s by default"
                if unlisted
                else ""
            )
            + ".",
        )
    live = [r for r in results if r.state == "clean"]
    unproven = [r for r in results if r.state == "unproven"]
    if unproven:
        listed = "; ".join(f"{_one_line(r.path)} ({r.detail})" for r in unproven)
        result = done(
            True,
            E_UNPROVEN,
            f"{len(live)} of {total} synced DAG file(s) are live in Airflow; {len(unproven)} "
            f"parsed with no import error but not proven: {listed}.",
        )
        result.proven = False
        return result
    return done(True, None, f"{len(live)} synced DAG file(s) are live in Airflow.")


def _notes(wait: _Wait) -> List[str]:
    notes: List[str] = []
    hidden = [
        fs.result.path
        for fs in wait.blocking()
        if fs.result.state == "pending" and fs.latest_error is None
    ]
    if hidden and wait.client.dialect == "v2":
        notes.append(
            "Airflow 3 shows the import error of a file with no registered DAG only to a user "
            "who may read all import errors; if "
            + ", ".join(_one_line(h) for h in hidden)
            + " fails to import, this user may not see why."
        )
    helpers = [_one_line(fs.result.path) for fs in wait.files if fs.file.helper]
    if helpers:
        notes.append(
            "Not checked, as they build no DAG at module level (helper modules): "
            + ", ".join(helpers)
            + "."
        )
    return notes
