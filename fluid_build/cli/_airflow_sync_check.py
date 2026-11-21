# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Wait until Airflow has parsed the DAG files ``fluid schedule-sync`` pushed.

What a deploy needs to know is whether the files it pushed are live: imported by the
scheduler's Dag processor, with no import error. Airflow's API answers that only
indirectly, so this module settles it from what both majors report (measured on Airflow
2.10.5, 3.0.6 and 3.4.0):

* every clean parse of a file stamps its Dags' ``last_parsed_time`` and, on Airflow 3,
  ``bundle_version``; a failed parse leaves both, flags the Dags ``has_import_errors``
  (and stale, on Airflow 3) and stamps the file's import error again;
* Airflow 3.4 can take ~10 s to parse a file, re-parsed every ``min_file_process_interval``
  seconds, so a parse that was already running when the files landed is common. It read
  the old file. A Dag therefore counts as parsed from the synced file once two clean
  parses that ended after the sync started have been seen: the second began after the
  first ended. A failure counts the same way, from two import-error stamps.
* Where the Dags report a bundle version (an Airflow 3 Git bundle) and the pushed version
  is known (the commit a ``git+ssh`` push made, or ``--airflow-bundle-version``), the Dag
  at that version is the proof, after one parse. Import errors count from the moment the
  bundle is known to be at that version: from ``/api/v2/dagBundles`` (Airflow 3.4), or the
  earliest parse of a pushed Dag at that version.
* Airflow 3 shows the import error of a file with no registered Dag only to a user who may
  read all import errors, so a Dag that never appears is reported as not parsed, never
  taken for a clean file.

Files whose Dag ids cannot be read from the source (a factory, a loop) are judged by the
Dags Airflow lists for them and by their import errors; they never hold the wait open.

Times come from the server: the reference is the ``Date`` header of the first answer
after the push, plus one second for its resolution. ``_sleep`` and ``_monotonic`` are
module attributes so tests can drive the wait on a simulated clock.
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from fluid_build.cli._airflow_api import AirflowApi, AirflowApiError, DagState, ImportErrorRecord

_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic

DEFAULT_POLL_S = 10.0
#: A DAG file larger than this is not read for its Dag ids (Airflow parses it regardless).
_MAX_SOURCE_BYTES = 4 * 1024 * 1024

E_IMPORT_ERRORS = "schedule_sync_airflow_import_errors"
E_NOT_PARSED = "schedule_sync_airflow_not_parsed"
E_UNREACHABLE = "schedule_sync_airflow_unreachable"


# ── the files ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DagFile:
    """A pushed file Airflow will parse: its path under ``--dags-dir`` and its Dag ids.

    ``dynamic`` means no Dag id could be read from the source; Airflow decides.
    """

    path: str
    dag_ids: Tuple[str, ...] = ()
    dynamic: bool = False


def dag_files(dags_dir: Path) -> List[DagFile]:
    """The ``*.py`` files under ``dags_dir`` Airflow would parse, with their Dag ids.

    Airflow's safe-mode rule decides which files may hold a Dag (the bytes contain both
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
        ids = _dag_ids(data) if len(data) <= _MAX_SOURCE_BYTES else ()
        found.append(DagFile(path="/".join(parts), dag_ids=ids, dynamic=not ids))
    return found


def _dag_ids(source: bytes) -> Tuple[str, ...]:
    """The literal Dag ids a module defines, in source order; ``()`` if none can be read.

    ``DAG(dag_id=...)`` / ``DAG("...")`` (any ``X.DAG``), ``@dag(dag_id=...)`` and a bare
    ``@dag`` (the function name), with the id a string literal, a module-level string
    constant, or an f-string or ``+`` of those.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ()
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
    """One file's outcome: ``clean``, ``broken``, ``pending`` or ``unchecked``."""

    path: str
    dag_ids: Tuple[str, ...]
    state: str = "pending"
    #: ``bundle-version`` or ``parse-time``, for a clean file.
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

    def to_report(self) -> Dict[str, Any]:
        from fluid_build.observability.secret_redactor import redact_secret_text

        return {
            "result": "passed" if self.passed else "failed",
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
    clean_parses: Set[datetime] = field(default_factory=set)


@dataclass
class _FileSeen:
    file: DagFile
    result: FileResult
    failed_parses: Set[datetime] = field(default_factory=set)
    error_now: Optional[ImportErrorRecord] = None
    latest_error: Optional[ImportErrorRecord] = None


_NEVER = datetime.min.replace(tzinfo=timezone.utc)


def _stamp(error: ImportErrorRecord) -> datetime:
    return error.timestamp or _NEVER


def _last_line(stack: str) -> str:
    lines = [line.strip() for line in stack.strip().splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


class _Wait:
    def __init__(
        self,
        client: AirflowApi,
        files: Sequence[DagFile],
        *,
        expected_version: Optional[str],
        version_explicit: bool,
        bundle: Optional[str],
    ) -> None:
        self.client = client
        self.expected = expected_version
        self.explicit = version_explicit
        self.bundle = bundle
        self.files = [_FileSeen(f, FileResult(path=f.path, dag_ids=f.dag_ids)) for f in files]
        self.dags: Dict[str, _DagSeen] = {}
        self.t0: Optional[datetime] = None
        self.local_clock = False
        self.bundle_at_version: Optional[datetime] = None
        self.versioned_seen = False
        #: Where the pushed files landed, as Airflow names them (an absolute folder, or ""
        #: for a bundle-relative path), learned from the Dags of files forge read ids from.
        self.anchors: Set[str] = set()

    # one poll ────────────────────────────────────────────────────────────

    def poll(self) -> None:
        errors = self.client.import_errors()
        if self.t0 is None:
            now = self.client.server_now()
            if now is None:
                now, self.local_clock = datetime.now(timezone.utc), True
            self.t0 = now + timedelta(seconds=1)
        if self.expected and self.client.dialect == "v2":
            self._read_bundles()
        listing: Optional[List[DagState]] = None
        if any(fs.file.dynamic and fs.result.state not in ("clean", "broken") for fs in self.files):
            listing = self.client.dags()
        undecided = [fs for fs in self.files if fs.result.state not in ("clean", "broken")]
        # Files with known Dag ids first: their Dags show where the files landed, which
        # then tells a file of ours from a same-named one in another directory.
        for fs in sorted(undecided, key=lambda f: f.file.dynamic):
            if fs.file.dynamic:
                self._attribute(fs, listing or [])
            for dag_id in fs.result.dag_ids:
                if self._dag_clean(dag_id)[0]:
                    continue
                state = self.client.dag(dag_id)
                self._observe(dag_id, state)
                if state is not None and not fs.file.dynamic:
                    self._learn_anchor(fs.file.path, state)
        self._read_errors(errors)
        for fs in self.files:
            if fs.result.state not in ("clean", "broken"):
                self._decide(fs)

    def _read_bundles(self) -> None:
        for bundle in self.client.bundles() or []:
            if self.bundle and bundle.name != self.bundle:
                continue
            if bundle.version is not None:
                self.versioned_seen = True
            if bundle.version == self.expected and self.bundle_at_version is None:
                # The bundle switched to the pushed version at or before this refresh.
                self.bundle_at_version = bundle.last_refreshed

    def _attribute(self, fs: _FileSeen, listing: List[DagState]) -> None:
        """The Dags Airflow lists for a file forge read no id from, read afresh each poll."""
        mine = [d for d in listing if self._is_ours(d, fs.file.path) and self._in_bundle(d)]
        fs.result.dag_ids = tuple(dict.fromkeys(d.dag_id for d in mine))

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

    def _observe(self, dag_id: str, state: Optional[DagState]) -> None:
        seen = self.dags.setdefault(dag_id, _DagSeen())
        seen.last = state
        if state is None:
            return
        if state.bundle_version is not None:
            self.versioned_seen = True
        stamp = state.last_parsed_time
        if (
            stamp is not None
            and self.t0 is not None
            and stamp >= self.t0
            and state.active
            and not state.has_import_errors
        ):
            seen.clean_parses.add(stamp)

    def _read_errors(self, errors: List[ImportErrorRecord]) -> None:
        reference = self._error_reference()
        for fs in self.files:
            fs.error_now = None
        for error in errors:
            if self.bundle and error.bundle_name not in (None, self.bundle):
                continue
            for fs in self.files:
                if fs.result.state in ("clean", "broken") or not self._at(
                    error.filename, fs.file.path
                ):
                    continue
                fs.error_now = error
                if fs.latest_error is None or _stamp(error) >= _stamp(fs.latest_error):
                    fs.latest_error = error
                stamp = error.timestamp
                if reference is not None and stamp is not None and stamp >= reference:
                    fs.failed_parses.add(stamp)

    def _versioned(self) -> bool:
        return bool(self.expected) and (self.explicit or self.versioned_seen)

    def _error_reference(self) -> Optional[datetime]:
        """From when an import error is an error of the synced files."""
        if not self._versioned():
            return self.t0
        at_version = [
            seen.last.last_parsed_time
            for seen in self.dags.values()
            if seen.last is not None
            and seen.last.bundle_version == self.expected
            and seen.last.last_parsed_time is not None
        ]
        candidates = [t for t in [self.bundle_at_version, *at_version] if t is not None]
        return min(candidates) if candidates else None

    # deciding ────────────────────────────────────────────────────────────

    def _dag_clean(self, dag_id: str) -> Tuple[bool, str, str]:
        """``(clean, evidence, why not)`` for one Dag, from what has been seen of it."""
        seen = self.dags.get(dag_id)
        state = seen.last if seen else None
        if state is None:
            return False, "", f"Airflow lists no Dag {dag_id} yet"
        if self.bundle and state.bundle_name not in (None, self.bundle):
            return False, "", f"Dag {dag_id} is in bundle {state.bundle_name}, not {self.bundle}"
        if state.has_import_errors:
            return False, "", f"Airflow reports an import error for the file of Dag {dag_id}"
        if not state.active:
            return False, "", f"Airflow marks Dag {dag_id} stale"
        if self.expected and (self.explicit or state.bundle_version is not None):
            want = self.expected[:12]
            if state.bundle_version == self.expected:
                return True, "bundle-version", f"at bundle version {want}"
            if state.bundle_version is None:
                return (
                    False,
                    "",
                    f"Airflow reports no bundle version for Dag {dag_id} (its bundle is not "
                    f"versioned), so it cannot reach {want}",
                )
            return (
                False,
                "",
                f"Dag {dag_id} is still at bundle version {state.bundle_version[:12]}, "
                f"waiting for {want}",
            )
        parses = len(seen.clean_parses) if seen else 0
        if parses >= 2:
            return True, "parse-time", "parsed twice since the sync"
        if parses == 1:
            return (
                False,
                "",
                f"Airflow parsed Dag {dag_id} once since the sync; that parse may have read "
                "the previous file",
            )
        return False, "", f"Airflow has not parsed Dag {dag_id} since the sync"

    def _decide(self, fs: _FileSeen) -> None:
        result = fs.result
        if fs.latest_error is not None:
            result.error = _last_line(fs.latest_error.stack_trace)
        if fs.error_now is not None and len(fs.failed_parses) >= 2:
            result.state, result.evidence = "broken", ""
            result.detail = f"import error: {result.error}" if result.error else "import error"
            return
        if not result.dag_ids:
            result.state = "unchecked" if fs.file.dynamic else "pending"
            result.detail = "forge read no Dag id from the file and Airflow lists none for it"
            return
        verdicts = [self._dag_clean(dag_id) for dag_id in result.dag_ids]
        waiting = [why for clean, _, why in verdicts if not clean]
        if waiting:
            result.state, result.detail = "pending", "; ".join(waiting)
            return
        evidence = {e for _, e, _ in verdicts}
        result.state = "clean"
        result.evidence = "bundle-version" if evidence == {"bundle-version"} else "parse-time"
        result.detail = (
            verdicts[0][2]
            if len(set(v[2] for v in verdicts)) == 1
            else "; ".join(v[2] for v in verdicts)
        )

    def blocking(self) -> List[_FileSeen]:
        """Files the wait is for: a dynamic file Airflow lists no Dag for never holds it open."""
        return [fs for fs in self.files if not (fs.file.dynamic and not fs.result.dag_ids)]

    def done(self) -> bool:
        # With nothing to wait for yet (every file dynamic, none listed by Airflow), keep
        # polling: Airflow may still list Dags for them before the deadline.
        blocking = self.blocking()
        return bool(blocking) and all(fs.result.state in ("clean", "broken") for fs in blocking)


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
    )
    start = monotonic()
    deadline = start + wait_s
    polls = 0
    transient: Optional[AirflowApiError] = None
    fatal: Optional[AirflowApiError] = None
    reported: Dict[str, str] = {}
    while True:
        polls += 1
        try:
            wait.poll()
            transient = None
        except AirflowApiError as exc:
            if not exc.transient:
                fatal = exc
                break
            transient = exc
        for fs in wait.files:
            if (
                on_change
                and reported.get(fs.result.path) != fs.result.state
                and fs.result.state
                in (
                    "clean",
                    "broken",
                )
            ):
                on_change(fs.result)
            reported[fs.result.path] = fs.result.state
        if wait.done():
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(poll_s, remaining))
    return _conclude(wait, polls, monotonic() - start, wait_s, fatal, transient, indirect)


def _conclude(
    wait: _Wait,
    polls: int,
    waited: float,
    wait_s: float,
    fatal: Optional[AirflowApiError],
    transient: Optional[AirflowApiError],
    indirect: bool,
) -> CheckResult:
    results = [fs.result for fs in wait.files]
    notes = _notes(wait, indirect)

    def done(passed: bool, event: Optional[str], message: str) -> CheckResult:
        return CheckResult(passed, results, event, message, notes, polls, waited)

    if fatal is not None:
        message = fatal.message + (f" {fatal.fix}" if fatal.fix else "")
        return done(False, fatal.event, message)
    broken = [r for r in results if r.state == "broken"]
    if broken:
        listed = "; ".join(f"{r.path} ({r.error or 'import error'})" for r in broken)
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
            "nothing was checked: forge read no Dag id from any synced file and Airflow lists "
            "no Dag for them.",
        )
    pending = [fs.result for fs in blocking if fs.result.state != "clean"]
    if pending and transient is not None:
        return done(False, E_UNREACHABLE, f"Airflow stopped answering: {transient.message}")
    if pending:
        listed = "; ".join(f"{r.path} ({r.detail})" for r in pending)
        return done(
            False,
            E_NOT_PARSED,
            f"Airflow did not finish parsing {len(pending)} of {len(results)} synced DAG "
            f"file(s) within {wait_s:.0f}s: {listed}. Raise --airflow-wait if Airflow parses "
            "slowly here (min_file_process_interval, and the folder listing interval for new "
            "files).",
        )
    return done(True, None, f"{len(results)} synced DAG file(s) are live in Airflow.")


def _notes(wait: _Wait, indirect: bool) -> List[str]:
    notes: List[str] = []
    results = [fs.result for fs in wait.files]
    if wait.local_clock:
        notes.append(
            "Airflow sent no Date header, so this machine's clock was the reference: a skewed "
            "clock can make a parse look older or newer than the sync."
        )
    if indirect and any(r.evidence != "bundle-version" for r in results if r.state == "clean"):
        notes.append(
            "Airflow reads this destination on its own schedule, so a parse after the sync can "
            "still be of the previous files. A versioned Dag bundle (an Airflow 3 Git bundle) "
            "makes the check exact; otherwise run --check-only once the files have landed."
        )
    hidden = [
        fs.result.path
        for fs in wait.blocking()
        if fs.result.state == "pending" and fs.latest_error is None
    ]
    if hidden and wait.client.dialect == "v2":
        notes.append(
            "Airflow 3 shows the import error of a file with no registered Dag only to a user "
            "who may read all import errors; if "
            + ", ".join(hidden)
            + " fails to import, this user may not see why."
        )
    for fs in wait.files:
        if fs.result.state == "unchecked":
            notes.append(
                f"{fs.result.path}: forge read no Dag id from it and Airflow lists no Dag for "
                "it, so it was checked for import errors only."
            )
    return notes
