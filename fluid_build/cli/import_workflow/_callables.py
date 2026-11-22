# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a Python task's function submits, read from its syntax: never imported, never run.

A ``PythonOperator`` runs a function, and a team that submits its Spark jobs from Airflow often
does it from one: its own library's ``run_step(script_path=..., config=...)``, or a function of the
DAG file that calls EMR itself. :class:`Follower` reads both, for each task that names a
``python_callable`` (and each TaskFlow ``@task`` function):

* **A function the conventions name** under ``submit_helpers``, with where its script, its
  arguments and its config file are among its arguments: a keyword or a position, then keys into
  it (``additional_params.script_path``), or a text made of those (``s3://{bucket}/{key}``). It is
  read from what the task passes (``op_kwargs`` by name, ``op_args`` by position: both are
  templated fields), or from the call when a function of the scan calls it. Its own code is not
  read: it usually lives in a library outside the scan.
* **A function of the DAG file, or of another file of the scan**, whose body submits the work:
  ``add_job_flow_steps`` or ``run_job_flow`` with their ``Steps`` (boto3, and the EMR hook's
  ``create_job_flow``), ``start_job_run`` or ``submit_job`` with a ``jobDriver`` (EMR on EKS, EMR
  Serverless), a command it runs (``subprocess.run`` and the like, ``os.system``), or a
  ``spark-submit`` argument list it builds. Its parameters hold what the task passes, so a script
  given in ``op_kwargs`` is read where the body uses it, and a function the body calls is followed
  in turn, its parameters holding what that call passes.
* Either of those through ``functools.partial`` (its own arguments come first) or a ``lambda``.

A function imported from outside the scanned files is not read: what the tasks run of those is
counted (:attr:`Follower.unread`), so the report can name the functions a person may describe.
The conventions name a project's own submit functions as OpenLineage's configuration names an
extractor by operator class; unlike an extractor, nothing named is imported.

Bounded: a function is followed at most :data:`MAX_DEPTH` calls deep and never into itself, one
DAG file goes through at most :data:`MAX_VISITS` calls and lists of the bodies it follows (and the
nodes walked to find them, the parameters of each call, and two characters of each command line
read: the file's own functions are walked once, another file's each time, so that no syntax of a
file the module index let go is kept), and a task's function given the same arguments again is
read once, whichever DAG file of the scan runs it. Every value is folded (:mod:`._fold`); the
folding of the bodies is lent by the folders and counted against one budget per DAG file, as large
as a module's, so that neither the file's own operators nor another file's tasks lose to it. The
DAG files of one scan spend at most :data:`MAX_VISITS_PER_SCAN` visits and
:data:`MAX_LENT_STEPS_PER_SCAN` lent steps between them (:class:`FollowBudget`), so many small
files that run one heavy function do not add up.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ._airflow_dags import (
    NO_SCRIPT,
    Submit,
    make_submit,
    spark_submits_in_text,
    spark_submits_in_tokens,
    starts_a_submit,
    step_submits,
)
from ._fold import (
    ABSENT,
    MAX_STEPS_PER_MODULE,
    MAX_STRING,
    MAX_STRING_WORK,
    FoldedDict,
    FoldedTuple,
    Folder,
    Scope,
    Unresolved,
    is_scalar,
    lookup,
)
from ._pysource import Module, qualified_name, snippet

#: Calls a body is followed through into another function: the task's own function is depth 0.
MAX_DEPTH = 3
#: Calls and argument lists of the bodies one DAG file's tasks run, gone through in all (and the
#: syntax nodes walked to find them, once per function, and the parameters of each call).
MAX_VISITS = 200_000
#: What the DAG files of one scan spend between them: visits, and the steps and characters of
#: string of the folding lent to their functions' bodies. A file's own bound keeps one file from
#: taking it all; this one keeps many files from adding up.
MAX_VISITS_PER_SCAN = 500_000
MAX_LENT_STEPS_PER_SCAN = 500_000
MAX_LENT_CHARS_PER_SCAN = 16_000_000
#: What tasks' functions submitted, kept between the files of a scan.
MAX_SHARED_RESULTS = 20_000
#: The characters of a command line read for the cost of one visit (a visit and two characters
#: of a command take about as long).
_CHARS_PER_VISIT = 2
#: A set of arguments larger than this (values, characters of text) is not a key of what was
#: read with it: a task with one is read on its own.
_MAX_KEY_ITEMS = 256
_MAX_KEY_CHARS = 20_000

#: What a submit helper submits to, by the kind the conventions give it (the operators' kinds).
HELPER_KINDS: Dict[str, str] = {
    "spark_submit": "spark-submit",
    "emr_add_steps": "emr-step",
    "emr_create_job_flow": "emr-step",
    "emr_containers": "emr-eks",
    "emr_serverless": "emr-serverless",
    "glue_job": "glue",
    "databricks_submit": "databricks",
}

#: Where a value is among a call's arguments: its position or keyword, then the keys into it.
Place = Tuple[Union[int, str], Tuple[str, ...]]
#: A text made of literal parts and places (``s3://{bucket}/{key}``); a place alone is the value.
Template = Tuple[Union[str, Place], ...]

#: Calls that run a command: the first argument is the command, as words or as a line.
_RUNNERS = frozenset(
    {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "os.system",
        "os.popen",
    }
)
_PARTIAL = "functools.partial"


@dataclass(frozen=True)
class SubmitHelper:
    """A project function that submits Spark work, as the conventions describe it."""

    script: Template
    runtime: str = "spark-submit"
    args: Optional[Place] = None
    config: Optional[Template] = None


def place_text(place: Place) -> str:
    """A place as the conventions write it: ``additional_params.script_path``, ``0``."""
    where, keys = place
    return ".".join([str(where), *keys])


@dataclass
class _Arguments:
    """What a call passes, by position and by keyword. ``complete`` is False when a ``*spread``
    or ``**spread`` did not fold (an argument not listed may still be passed), and ``why`` says
    why."""

    positional: List[Any] = field(default_factory=list)
    keywords: Dict[str, Any] = field(default_factory=dict)
    complete: bool = True
    why: str = ""

    def then(self, other: "_Arguments") -> "_Arguments":
        """These arguments followed by ``other``'s: ``partial(f, a)`` called with ``b``."""
        return _Arguments(
            [*self.positional, *other.positional],
            {**self.keywords, **other.keywords},
            self.complete and other.complete,
            self.why or other.why,
        )


@dataclass
class _Target:
    """What a call or a ``python_callable`` runs: a helper the conventions name, a function whose
    body is read, or (neither) a function that is not read."""

    shown: str
    helper: Optional[SubmitHelper] = None
    helper_name: str = ""
    function: Optional[ast.AST] = None
    module: Optional[Module] = None
    folder: Optional[Folder] = None
    #: What ``functools.partial`` gives the function before the caller's own arguments.
    bound: Optional[_Arguments] = None
    #: Whether it is a name a person can give under ``submit_helpers``.
    named: bool = True


class _TooBig(Exception):
    pass


def _why(value: Any, otherwise: str) -> str:
    return value.why if isinstance(value, Unresolved) else otherwise


def _as_text(value: Any) -> Optional[str]:
    """A folded value as the text it is in a path: a string or a number; ``None`` otherwise."""
    if isinstance(value, str):
        return value
    if is_scalar(value) and value is not None and not isinstance(value, bool):
        return str(value)
    return None


def _keyword(call: ast.Call, *names: str) -> Optional[ast.expr]:
    for keyword in call.keywords:
        if keyword.arg in names:
            return keyword.value
    return None


def _key(arguments: _Arguments) -> Optional[Tuple[Any, ...]]:
    """``arguments`` as a key of what a function was read with, or ``None`` when they are too
    large to be one."""
    if len(arguments.positional) + len(arguments.keywords) > _MAX_KEY_ITEMS:
        return None
    left = [_MAX_KEY_ITEMS, _MAX_KEY_CHARS]

    def keyed(value: Any) -> Any:
        left[0] -= 1
        if isinstance(value, str):
            left[1] -= len(value)
        if left[0] < 0 or left[1] < 0:
            raise _TooBig
        if isinstance(value, Unresolved):
            left[1] -= len(value.why) + len(value.text)
            return ("?", value.why, value.text)
        if isinstance(value, dict):
            items = tuple((repr(k), keyed(v)) for k, v in value.items())
            return ("{", getattr(value, "complete", True), items)
        if isinstance(value, (list, tuple)):
            kind = type(value).__name__
            return ("[", kind, getattr(value, "complete", True), tuple(keyed(v) for v in value))
        return ("=", type(value).__name__, value)

    try:
        return (
            tuple(keyed(v) for v in arguments.positional),
            tuple(sorted((k, keyed(v)) for k, v in arguments.keywords.items())),
            arguments.complete,
            arguments.why,
        )
    except (_TooBig, TypeError):  # TypeError: a value that is not hashable
        return None


class FollowBudget:
    """What the followers of one scan (one for each DAG file) spend between them, so that many
    files do not add up past what one scan may cost: visits and lent folding, each also bounded
    per file. It also keeps what a task's function submitted, by where the function is and what
    it was given, so the files that run one function with the same arguments read it once (a read
    that stopped early is not kept)."""

    def __init__(self) -> None:
        self.visits_left = MAX_VISITS_PER_SCAN
        self.lent_left = [MAX_LENT_STEPS_PER_SCAN, MAX_LENT_CHARS_PER_SCAN]
        #: The DAG files whose tasks were cut short by this budget, not by their own.
        self.cut_files = 0
        self.read: Dict[Tuple[Any, ...], List[Submit]] = {}


class Follower:
    """Reads what the Python tasks of one DAG file submit (see the module docstring)."""

    def __init__(
        self,
        module: Module,
        folder: Folder,
        helpers: Mapping[str, SubmitHelper],
        budget: Optional[FollowBudget] = None,
    ) -> None:
        self.module = module
        self.folder = folder
        #: What the scan's followers spend between them (a file read alone has its own).
        self.budget = budget if budget is not None else FollowBudget()
        #: The helpers by the last part of their name; the longest name that matches wins.
        self._by_last: Dict[str, List[Tuple[str, SubmitHelper]]] = {}
        for name, helper in helpers.items():
            self._by_last.setdefault(name.rsplit(".", 1)[-1], []).append((name, helper))
        for found in self._by_last.values():
            found.sort(key=lambda item: -len(item[0]))
        #: What is left of :data:`MAX_VISITS`; once it (or the scan's) runs out, ``cut_short`` is
        #: set, and ``scan_cut`` when it was the scan's.
        self.visits_left = MAX_VISITS
        self.cut_short = False
        self.scan_cut = False
        #: How many times following stopped: a read that stopped is not kept for other tasks.
        self._cuts = 0
        #: The functions the tasks run that are not read, as written, with how many tasks run each.
        self.unread: Dict[str, int] = {}
        #: The files of other modules whose folding ran out while their functions were followed.
        self.tired: Set[str] = set()
        #: What folding the bodies of the functions may still cost, in this file and in the
        #: others its tasks reach: it is the reading file's to spend (see :meth:`Folder.lent`),
        #: as much as a module may, so neither its own operators nor another file's tasks lose
        #: to it.
        self._lent_left = [MAX_STEPS_PER_MODULE, MAX_STRING_WORK]
        self._lent: Set[int] = set()
        #: The calls and lists of each body walked, by the function: kept for this file's own
        #: functions only. Another file's body is walked each time it is read (charged as visits):
        #: a key that is its syntax would keep the whole function alive after the module index
        #: lets the file go.
        self._bodies: Dict[ast.AST, List[ast.AST]] = {}
        #: What a top-level function's defaults hold, by where the function is (``(file, line,
        #: column)``: a key that keeps no syntax alive). What a task's function submitted is kept
        #: on the scan's budget, by the same key and its arguments.
        self._defaults: Dict[Tuple[str, int, int], List[Tuple[str, Any]]] = {}
        #: A command line (with its holes) to the spark-submits in it: read once.
        self._commands: Dict[Any, List[Tuple[Any, List[Any]]]] = {}

    # ── what it may spend ───────────────────────────────────────────

    def _visits(self) -> int:
        return min(self.visits_left, self.budget.visits_left)

    def _charge(self, visits: int) -> None:
        self.visits_left -= visits
        self.budget.visits_left -= visits

    def _cut(self, scan: bool) -> None:
        """Stop following: ``scan`` when the scan's budget ran out before this file's did."""
        self.cut_short = True
        self.scan_cut = self.scan_cut or scan
        self._cuts += 1

    def _out_of_visits(self) -> None:
        self._cut(self.budget.visits_left < self.visits_left)

    # ── entry points ────────────────────────────────────────────────

    def task(
        self, call: ast.Call, python: ast.expr, op_args: Any, op_kwargs: Any
    ) -> Tuple[str, List[Submit]]:
        """The function a task with ``python_callable=python`` runs (as written) and what it
        submits, given ``op_args`` and ``op_kwargs`` as they fold (``None``: not given)."""
        target = self._target(python, self.module, self.folder)
        passed = self._passed(op_args, op_kwargs)
        arguments = passed if target.bound is None else target.bound.then(passed)
        if target.helper is not None:
            return target.shown, self._helper_submits(target, arguments, call.lineno)
        if target.function is not None:
            return target.shown, self._function_submits(target, arguments, call.lineno, 0, ())
        if target.named:
            self.unread[target.shown] = self.unread.get(target.shown, 0) + 1
        return target.shown, []

    def function(self, function: ast.AST) -> List[Submit]:
        """What a TaskFlow ``@task`` function of this file submits: it is called with values of
        the run, which are not known here."""
        name = getattr(function, "name", "lambda")
        target = _Target(name, function=function, module=self.module, folder=self.folder)
        arguments = _Arguments(complete=False, why=f"`{name}` is given its values when it runs")
        return self._function_submits(target, arguments, function.lineno, 0, ())  # type: ignore[attr-defined]

    # ── what is called ──────────────────────────────────────────────

    def _helper(self, qualified: Optional[str]) -> Optional[Tuple[str, SubmitHelper]]:
        if not qualified or not self._by_last:
            return None
        for name, helper in self._by_last.get(qualified.rsplit(".", 1)[-1], ()):
            if qualified == name or qualified.endswith("." + name):
                return name, helper
        return None

    def _target(self, node: ast.AST, module: Module, folder: Folder, hops: int = 0) -> _Target:
        if isinstance(node, ast.Lambda):
            return _Target("lambda", function=node, module=module, folder=folder, named=False)
        if isinstance(node, ast.Call) and qualified_name(node.func, module.imports) == _PARTIAL:
            if not node.args or isinstance(node.args[0], ast.Starred) or hops >= MAX_DEPTH:
                return _Target(snippet(node, 60), named=False)
            inner = self._target(node.args[0], module, folder, hops + 1)
            bound = self._arguments(node.args[1:], node.keywords, folder)
            inner.bound = bound if inner.bound is None else inner.bound.then(bound)
            return inner
        qualified = qualified_name(node, module.imports)
        if qualified is None:
            return _Target(snippet(node, 60), named=False)
        found = self._helper(qualified)
        if found is not None:
            return _Target(qualified, helper=found[1], helper_name=found[0])
        head = node
        while isinstance(head, ast.Attribute):
            head = head.value
        if not isinstance(head, ast.Name):
            return _Target(qualified, named=False)
        if isinstance(node, ast.Name):
            local = self._local(node, folder)
            if local is not None:
                return _Target(qualified, function=local, module=module, folder=folder)
        if head.id not in module.imports:
            return _Target(qualified)  # a name of the file that is not one of its functions
        return self._scanned(qualified, module, folder, hops)

    @staticmethod
    def _local(node: ast.Name, folder: Folder) -> Optional[ast.AST]:
        """The ``def`` the name ``node`` is bound to where it is used, when that is the only way
        it is bound there."""
        name = node.id
        if folder.rebound(name):
            return None
        scope: Optional[Scope] = folder.scope_of(node)
        while scope is not None:
            if name in scope.functions:
                return scope.functions[name]
            if (
                name in scope.values
                or name in scope.blocked
                or name in scope.classes
                or name in scope.recipes
            ):
                return None
            scope = scope.parent
        return None

    def _scanned(self, qualified: str, module: Module, folder: Folder, hops: int) -> _Target:
        """A function of another file of the scan, by the name ``module`` imports it as (a name
        that file imports in turn is followed, a few times)."""
        index = folder.index
        if index is None:
            return _Target(qualified)
        found = index.resolve(qualified, module.index_rel or module.rel)
        if isinstance(found, str) or len(found[1]) != 1:
            return _Target(qualified)
        other, (name,) = found
        other_folder = index.folder(other)
        scope = other_folder.scope_of(other.tree)
        if other_folder.rebound(name):
            return _Target(qualified)
        if name in scope.functions:
            return _Target(
                qualified, function=scope.functions[name], module=other, folder=other_folder
            )
        again = other.imports.get(name)
        if scope.blocked.get(name) == "imported" and again and hops < MAX_DEPTH:
            helper = self._helper(again)
            if helper is not None:
                return _Target(qualified, helper=helper[1], helper_name=helper[0])
            target = self._scanned(again, other, other_folder, hops + 1)
            target.shown = qualified
            return target
        return _Target(qualified)

    # ── arguments ───────────────────────────────────────────────────

    @staticmethod
    def _arguments(
        args: Sequence[ast.expr], keywords: Sequence[ast.keyword], folder: Folder
    ) -> _Arguments:
        out = _Arguments()
        for arg in args:
            if isinstance(arg, ast.Starred):
                spread = folder.fold(arg.value)
                if isinstance(spread, (list, tuple)) and getattr(spread, "complete", True):
                    out.positional.extend(spread)
                    continue
                out.complete = False
                out.why = out.why or _why(spread, "a *spread that does not fold")
                break  # the place of what follows it is not known
            out.positional.append(folder.fold(arg))
        for keyword in keywords:
            if keyword.arg is not None:
                out.keywords[keyword.arg] = folder.fold(keyword.value)
                continue
            spread = folder.fold(keyword.value)
            if isinstance(spread, dict):
                out.keywords.update((k, v) for k, v in spread.items() if isinstance(k, str))
                if not getattr(spread, "complete", True):
                    out.complete = False
                    out.why = out.why or "a **spread that is not all known"
            else:
                out.complete = False
                out.why = out.why or _why(spread, "a **spread that does not fold")
        return out

    @staticmethod
    def _passed(op_args: Any, op_kwargs: Any) -> _Arguments:
        """What Airflow calls a task's function with: ``op_args``, then ``op_kwargs``."""
        out = _Arguments()
        if op_args is not None:
            if not isinstance(op_args, (list, tuple)):
                out.complete, out.why = False, _why(op_args, "op_args is not a list")
            elif getattr(op_args, "complete", True):
                out.positional = list(op_args)
            else:  # a *spread that does not fold: what follows it is at a place not known
                out.complete, out.why = False, "op_args is not all known"
        if op_kwargs is not None:
            if isinstance(op_kwargs, dict):
                out.keywords = {k: v for k, v in op_kwargs.items() if isinstance(k, str)}
                if not getattr(op_kwargs, "complete", True):
                    out.complete = False
                    out.why = out.why or "op_kwargs is not all known"
            else:
                out.complete = False
                out.why = out.why or _why(op_kwargs, "op_kwargs is not a dict")
        return out

    @staticmethod
    def _at(place: Place, arguments: _Arguments, name: str) -> Any:
        """The value at ``place`` among ``arguments``, or why there is none (an
        :class:`Unresolved` with no text)."""
        where, keys = place
        if isinstance(where, int):
            value = arguments.positional[where] if where < len(arguments.positional) else ABSENT
        else:
            value = arguments.keywords.get(where, ABSENT)
        if value is ABSENT:
            if not arguments.complete:
                return Unresolved(arguments.why or "its arguments are not all known")
            if isinstance(where, str) and arguments.positional:
                return Unresolved(f"{name} is not given `{where}` by keyword")
            return Unresolved(f"{name} is not given its `{where}` argument")
        for depth, key in enumerate(keys):
            if isinstance(value, Unresolved):
                return value
            found = lookup(value, key)
            if found is ABSENT:
                inside = place_text((where, keys[:depth]))
                return Unresolved(f"`{key}` is not in the `{inside}` {name} is given")
            value = found
        return value

    def _render(self, template: Template, arguments: _Arguments, name: str) -> Any:
        """The text ``template`` stands for: a string, or an :class:`Unresolved` that says why and
        shows what is known."""
        if len(template) == 1 and not isinstance(template[0], str):
            return self._render_place(template[0], arguments, name)
        pieces: List[str] = []
        why = ""
        size = 0
        for part in template:
            piece, missing = (
                (part, "") if isinstance(part, str) else self._piece(part, arguments, name)
            )
            why = why or missing
            size += len(piece)
            if size > MAX_STRING:
                return Unresolved(f"it is longer than {MAX_STRING} characters", "")
            pieces.append(piece)
        text = "".join(pieces)
        return Unresolved(why, text) if why else text

    def _render_place(self, place: Place, arguments: _Arguments, name: str) -> Any:
        value = self._at(place, arguments, name)
        shown = f"{place_text(place)} of {name}"
        if isinstance(value, Unresolved):
            return value if value.text else Unresolved(value.why, shown)
        if isinstance(value, str):
            return value
        return Unresolved("it is not text", shown)

    def _piece(self, place: Place, arguments: _Arguments, name: str) -> Tuple[str, str]:
        """The text of one place of a template, and why it is not known (empty when it is)."""
        value = self._at(place, arguments, name)
        text = _as_text(value)
        if text is not None:
            return text, ""
        # what did not resolve shows in braces, as an f-string field does (a text that is a
        # template of its own already has them)
        shown = value.text if isinstance(value, Unresolved) else ""
        piece = shown if "{" in shown else "{" + (shown or place_text(place)) + "}"
        return piece, _why(value, "it is not text")

    def _helper_submits(self, target: _Target, arguments: _Arguments, line: int) -> List[Submit]:
        helper, name = target.helper, target.helper_name
        assert helper is not None
        script = self._render(helper.script, arguments, name)
        args = self._at(helper.args, arguments, name) if helper.args is not None else None
        config = self._render(helper.config, arguments, name) if helper.config else None
        listed = args if isinstance(args, (list, tuple)) else None
        return [make_submit(helper.runtime, script, listed, line, config=config)]

    # ── bodies ──────────────────────────────────────────────────────

    def _function_submits(
        self,
        target: _Target,
        arguments: _Arguments,
        line: int,
        depth: int,
        stack: Tuple[ast.AST, ...],
    ) -> List[Submit]:
        function, module, folder = target.function, target.module, target.folder
        assert function is not None and module is not None and folder is not None
        if depth > MAX_DEPTH or function in stack:
            return []
        # a task's function given the same arguments again is read once (what a function deeper
        # down submits also depends on how deep it is and on what called it: those are not kept)
        where = (module.rel, function.lineno, function.col_offset)  # type: ignore[attr-defined]
        key = (*where, _key(arguments)) if depth == 0 else None
        kept = self.budget.read
        if key is not None and key[-1] is not None and key in kept:
            return [replace(submit, line=line) for submit in kept[key]]
        if self._visits() <= 0:  # nothing left to read it with
            self._out_of_visits()
            return []
        lend = id(folder) not in self._lent  # a body inside one already lent is counted there
        if lend and (min(self._lent_left) <= 0 or min(self.budget.lent_left) <= 0):
            self._cut(min(self.budget.lent_left) <= 0 < min(self._lent_left))
            return []
        cuts, exhausted = self._cuts, False
        nodes = self._body(function, own=folder is self.folder)
        if not lend:
            found = self._read_body(function, nodes, module, folder, arguments, line, depth, stack)
        else:
            self._lent.add(id(folder))
            try:
                with folder.lent() as spent:
                    found = self._read_body(
                        function, nodes, module, folder, arguments, line, depth, stack
                    )
                    exhausted = folder.exhausted
                    if exhausted and folder is not self.folder:  # this file's: cut_short
                        self.tired.add(module.index_rel or module.rel)
            finally:
                self._lent.discard(id(folder))
            self._lent_left[0] -= spent[0]
            self._lent_left[1] -= spent[1]
            self.budget.lent_left[0] -= spent[0]
            self.budget.lent_left[1] -= spent[1]
        # what a read that stopped found is this file's to say: another file's tasks read the
        # function again, with their own budget
        whole = self._cuts == cuts and not exhausted
        if key is not None and key[-1] is not None and whole and len(kept) < MAX_SHARED_RESULTS:
            kept[key] = found
        return found

    def _read_body(
        self,
        function: ast.AST,
        nodes: Sequence[ast.AST],
        module: Module,
        folder: Folder,
        arguments: _Arguments,
        line: int,
        depth: int,
        stack: Tuple[ast.AST, ...],
    ) -> List[Submit]:
        frame = self._frame(function, module, folder, arguments, task=depth == 0)
        with folder.bound(function, frame):
            return self._scan(nodes, module, folder, line, depth, (*stack, function))

    def _body(self, function: ast.AST, own: bool) -> List[ast.AST]:
        """The calls and the argument lists of ``function``'s own body (not of a function or a
        class inside it, which run only when called), in source order: walked once for this
        file's own functions, each time for another file's (see ``_bodies``)."""
        found = self._bodies.get(function)
        if found is not None:
            return found
        body = getattr(function, "body", [])
        stack: List[ast.AST] = list(reversed(body)) if isinstance(body, list) else [body]
        out: List[ast.AST] = []
        walked = 0
        while stack and walked <= self._visits():
            node = stack.pop()
            walked += 1
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Call) or (
                isinstance(node, (ast.List, ast.Tuple)) and isinstance(node.ctx, ast.Load)
            ):
                out.append(node)
            stack.extend(reversed(list(ast.iter_child_nodes(node))))
        if stack:
            self._out_of_visits()
        self._charge(walked)
        out.sort(key=lambda n: (n.lineno, n.col_offset))  # type: ignore[attr-defined]
        if own:
            self._bodies[function] = out
        return out

    def _frame(
        self,
        function: ast.AST,
        module: Module,
        folder: Folder,
        arguments: _Arguments,
        task: bool,
    ) -> Dict[str, Any]:
        """What each parameter of ``function`` holds when it is called with ``arguments``. A
        task's function is also given the run's context by Airflow, so what it takes as
        ``**kwargs`` is not all known and a default may be the context's value: none is read.
        Each parameter costs a visit."""
        spec: ast.arguments = function.args  # type: ignore[attr-defined]
        frame: Dict[str, Any] = {}
        positional = [*spec.posonlyargs, *spec.args]
        self._charge(len(positional) + len(spec.kwonlyargs) + 2)
        if self._visits() <= 0:
            self._out_of_visits()
            return frame
        for param, value in zip(positional, arguments.positional, strict=False):
            frame[param.arg] = value
        if spec.vararg is not None:
            extra = FoldedTuple(arguments.positional[len(positional) :])
            extra.complete = arguments.complete
            frame[spec.vararg.arg] = extra
        named = {p.arg for p in [*spec.args, *spec.kwonlyargs]}
        rest = FoldedDict()
        for key, value in arguments.keywords.items():
            if key in named:
                frame.setdefault(key, value)
            else:
                rest[key] = value
        if spec.kwarg is not None:
            rest.complete = arguments.complete and not task
            frame[spec.kwarg.arg] = rest
        if arguments.complete and not task:
            for name, default in self._defaults_of(function, module, folder):
                frame.setdefault(name, default)
        # a parameter the body binds again holds what the caller passed only until it does
        scope = folder.function_scope(function)
        return {
            name: value
            for name, value in frame.items()
            if name not in scope.rebound_parameters and not folder.rebound(name)
        }

    def _defaults_of(
        self, function: ast.AST, module: Module, folder: Folder
    ) -> List[Tuple[str, Any]]:
        """What the defaults of ``function``'s parameters fold to. A def's defaults are evaluated
        once, where it is defined: those of a top-level def are folded once per file; a def
        inside a function may see that function's parameters, so its are folded each time."""
        spec: ast.arguments = function.args  # type: ignore[attr-defined]
        positional = [*spec.posonlyargs, *spec.args]
        enclosing = folder.scope_of(function)
        where = (module.rel, function.lineno, function.col_offset)  # type: ignore[attr-defined]
        top = enclosing.owner is module.tree
        if top and where in self._defaults:
            return self._defaults[where]
        pairs = list(
            zip(positional[len(positional) - len(spec.defaults) :], spec.defaults, strict=True)
        )
        pairs += [
            (p, d) for p, d in zip(spec.kwonlyargs, spec.kw_defaults, strict=True) if d is not None
        ]
        found = [(param.arg, folder.fold(default, enclosing)) for param, default in pairs]
        if top:
            self._defaults[where] = found
        return found

    def _scan(
        self,
        nodes: Sequence[ast.AST],
        module: Module,
        folder: Folder,
        line: int,
        depth: int,
        stack: Tuple[ast.AST, ...],
    ) -> List[Submit]:
        from_calls: List[Submit] = []
        from_lists: List[Submit] = []
        for node in nodes:
            if self._visits() <= 0:
                self._out_of_visits()
                break
            self._charge(1)
            if isinstance(node, ast.Call):
                from_calls.extend(self._call(node, module, folder, line, depth, stack))
            else:
                from_lists.extend(self._argv(node, folder, line))
        # an argument list the body hands to a call that was read is that call's submit
        seen = {(s.script, s.shown, s.args) for s in from_calls}
        return from_calls + [s for s in from_lists if (s.script, s.shown, s.args) not in seen]

    def _call(
        self,
        node: ast.Call,
        module: Module,
        folder: Folder,
        line: int,
        depth: int,
        stack: Tuple[ast.AST, ...],
    ) -> List[Submit]:
        """What one call of a followed body submits: a helper the conventions name, an EMR call,
        a command it runs, or what the function it calls submits."""
        qualified = qualified_name(node.func, module.imports)
        found = self._helper(qualified)
        if found is not None:
            target = _Target(found[0], helper=found[1], helper_name=found[0])
            arguments = self._arguments(node.args, node.keywords, folder)
            return self._helper_submits(target, arguments, line)
        if isinstance(node.func, ast.Attribute):
            emr = self._emr_call(node.func.attr, node, folder, line)
            if emr is not None:
                return emr
        if qualified in _RUNNERS:
            command = node.args[0] if node.args else _keyword(node, "args", "command", "cmd")
            return [] if command is None else self._command(command, folder, line)
        if depth >= MAX_DEPTH:
            return []
        target = self._target(node.func, module, folder)
        if target.helper is None and target.function is None:
            return []
        arguments = self._arguments(node.args, node.keywords, folder)
        if target.bound is not None:
            arguments = target.bound.then(arguments)
        if target.helper is not None:
            return self._helper_submits(target, arguments, line)
        return self._function_submits(target, arguments, line, depth + 1, stack)

    def _emr_call(
        self, method: str, node: ast.Call, folder: Folder, line: int
    ) -> Optional[List[Submit]]:
        """An EMR call, by its method's name and the argument that holds the work (boto3's, and
        the Airflow EMR hooks'); ``None`` for a call that is not one, which is read like any
        other (a project function of that name is followed)."""
        if method in ("add_job_flow_steps", "run_job_flow"):
            steps = _keyword(node, "Steps", "steps")
            if steps is None and method == "add_job_flow_steps" and len(node.args) >= 2:
                steps = node.args[1]  # the EMR hook's add_job_flow_steps(job_flow_id, steps)
            return None if steps is None else step_submits(folder.fold(steps), line)
        if method == "create_job_flow":  # the EMR hook's create_job_flow(job_flow_overrides)
            given = _keyword(node, "job_flow_overrides") or (node.args[0] if node.args else None)
            overrides = folder.fold(given) if given is not None else None
            listed = lookup(overrides, "Steps") if isinstance(overrides, dict) else ABSENT
            return None if listed is ABSENT else step_submits(listed, line)
        if method in ("start_job_run", "submit_job"):
            driver = _keyword(node, "jobDriver", "job_driver")
            if driver is None and method == "submit_job" and len(node.args) >= 4:
                # the EMR on EKS hook's submit_job(name, role, release, driver)
                driver = node.args[3]
            return None if driver is None else self._driver(folder.fold(driver), line)
        return None

    @staticmethod
    def _driver(driver: Any, line: int) -> List[Submit]:
        """``jobDriver`` of EMR on EKS (``sparkSubmitJobDriver``) and of EMR Serverless
        (``sparkSubmit``): the entry point and its arguments."""
        if not isinstance(driver, dict):
            return []
        out: List[Submit] = []
        for key, runtime in (
            ("sparkSubmitJobDriver", "emr-eks"),
            ("sparkSubmit", "emr-serverless"),
        ):
            spark = lookup(driver, key)
            if isinstance(spark, dict):
                entry = lookup(spark, "entryPoint")
                args = lookup(spark, "entryPointArguments")
                out.append(
                    make_submit(
                        runtime,
                        None if entry is ABSENT else entry,
                        None if args is ABSENT else args,
                        line,
                        "entryPoint",
                    )
                )
        return out

    def _command(self, node: ast.expr, folder: Folder, line: int) -> List[Submit]:
        """A command a function runs: words (``["spark-submit", ...]``) or a line."""
        if isinstance(node, (ast.List, ast.Tuple)):
            return []  # an argument list written out: read as one (see _argv)
        value = folder.fold(node)
        if isinstance(value, (list, tuple)):
            words = [w for w in value if isinstance(w, (str, Unresolved))]
            if not words or not starts_a_submit(words[0]):
                return []
            return [
                make_submit("spark-submit", s, a, line) for s, a in spark_submits_in_tokens(words)
            ]
        text, holes = folder.fold_sql(node)
        if not isinstance(text, str):
            return []
        key = (text, tuple(sorted(holes.items())))
        found = self._commands.get(key)
        if found is None:
            # reading a command line costs what a visit does for every two of its characters
            cost = 1 + len(text) // _CHARS_PER_VISIT
            if self._visits() < cost:
                self._out_of_visits()
                return []
            self._charge(cost)
            found = self._commands[key] = spark_submits_in_text(text, holes)
        return [make_submit("spark-submit", s, a, line) for s, a in found]

    @staticmethod
    def _argv(node: ast.AST, folder: Folder, line: int) -> List[Submit]:
        """An argument list a body writes out whose first word starts a ``spark-submit``
        (``["spark-submit", "--deploy-mode", "cluster", script]``, ``["bash", "-c", "..."]``):
        handed to a command runner, a hook or the project's own code, it is what runs."""
        elements = getattr(node, "elts", [])
        if not elements or isinstance(elements[0], ast.Starred):
            return []
        if not starts_a_submit(folder.fold(elements[0])):
            return []
        value = folder.fold(node)
        if not isinstance(value, (list, tuple)):
            return []
        words = [w for w in value if isinstance(w, (str, Unresolved))]
        found = [make_submit("spark-submit", s, a, line) for s, a in spark_submits_in_tokens(words)]
        # `cmd = ["spark-submit", "--master", "yarn"]` that is added to later is a piece of the
        # command, not one: a list that names no script at all is left to what it is added to
        return [s for s in found if not s.why.startswith(NO_SCRIPT)]


__all__ = [
    "HELPER_KINDS",
    "MAX_DEPTH",
    "MAX_VISITS",
    "MAX_VISITS_PER_SCAN",
    "FollowBudget",
    "Follower",
    "SubmitHelper",
    "place_text",
]
