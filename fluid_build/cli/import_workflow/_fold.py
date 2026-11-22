# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Reading a value out of Python syntax, without running it.

:class:`Folder` turns an expression of a parsed module into the value the syntax fixes:
a string, a number, a list, a dict. What the syntax does not fix (a command-line argument, an
environment variable nobody told us, the result of a call, a name assigned twice) comes back
as an :class:`Unresolved` that says why and carries the expression's text, so a report can
show it and nothing is guessed.

What it folds, and nothing else:

* literals, tuples, lists, sets and dicts (a ``**spread`` or a ``*star`` that does not fold
  leaves the container marked incomplete: :func:`lookup` then answers "unknown", never "absent");
* f-strings with plain fields, ``+``, ``"%s" % x`` and ``"{}".format(x)`` with plain fields,
  and a few pure ``str`` methods. These run on this module's own code, never on ``str.format``
  or ``%`` of the project's text, so a hostile format string (``{:>999999999}``) cannot spend
  memory, and results over :data:`MAX_STRING` characters (:data:`MAX_SQL_STRING` for SQL, read
  with :meth:`Folder.fold_sql`) are refused;
* a name assigned ONCE in its scope (a function, then the module), a class attribute of a
  class in the same module, and a constant of another module of the scanned tree (see
  :class:`ModuleIndex`); a name assigned twice, a parameter, a loop variable or a name from a
  star import is unresolved. A list or a string that is built by ``name += ...`` statements
  after its one assignment, each at the top level of its body or inside a ``for`` over a dict or
  a list that folds, folds to the whole of it where it is used after the last of them
  (:class:`Recipe`);
* ``os.environ[...]``, ``os.environ.get(...)`` and ``os.getenv(...)``, from the operator's
  ``variables`` (conventions), else unresolved: the default of ``get`` is not assumed. An Airflow
  ``Variable.get(...)`` is read the same way from the operator's ``airflow_variables``.

There is no ``eval``, no ``ast.literal_eval`` and no import of anything the project names.
"""

from __future__ import annotations

import ast
import posixpath
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ._pysource import (
    MAX_CACHED_NODES,
    Module,
    SourceBudget,
    SourceFile,
    SourceUnreadable,
    parse_module,
    qualified_name,
    read_source,
    snippet,
)

#: Deepest expression :meth:`Folder.fold` follows, and the longest string it builds.
MAX_FOLD_DEPTH = 40
MAX_STRING = 4096
#: The longest string :meth:`Folder.fold_sql` builds: SQL is a long literal (a page of a
#: report, a view) far more often than any other text the folder reads. It stays inside the
#: size of a file the scan reads, and the SQL parser has its own, lower limit
#: (``_spark_sql.MAX_SQL_CHARS``) that says so when it is passed.
MAX_SQL_STRING = 262_144
#: Characters of string one module may have its folds build (an f-string, a ``+``, a
#: ``.format``, a join): a long string built over and over from a long literal is as costly as
#: it is long, and this is the bound that keeps SQL, which may be long, as cheap as any other
#: text. A module that builds more is told so, as one that takes too many steps is.
MAX_STRING_WORK = 4_000_000
#: Most containers elements folded in one literal.
MAX_ITEMS = 2_000
#: Most nodes one top-level :meth:`Folder.fold` call may visit, and one module in all: the work a
#: hostile file can ask for is bounded, whatever its expressions refer to.
MAX_STEPS_PER_FOLD = 5_000
MAX_STEPS_PER_MODULE = 250_000
#: Most items of a ``for`` loop one recipe goes through, and statements it applies in all: a
#: list built by a loop over a dict is as long as the dict, and a hostile file can ask for more.
MAX_LOOP_ITERATIONS = 500
MAX_RECIPE_STEPS = 5_000
#: Characters of text the folded values of one module keep in its memo: a list of thousands of
#: long strings built from one long string is as big as its elements, and every element is one
#: more node to remember.
MAX_MEMO_CHARS = 4_000_000
#: Methods that change what a name holds: a name they are called on is not the value it was
#: assigned.
_MUTATORS = frozenset(
    {
        "update",
        "append",
        "extend",
        "insert",
        "pop",
        "popitem",
        "remove",
        "clear",
        "setdefault",
        "add",
        "discard",
        "sort",
        "reverse",
        "__setitem__",
        "__delitem__",
    }
)


class Unresolved:
    """A value the syntax does not fix. ``why`` says what it depends on, ``text`` shows it
    (the expression, or a template such as ``{env}_orders`` when part of it folded)."""

    __slots__ = ("why", "text", "transient")

    def __init__(self, why: str, text: str = "", transient: bool = False) -> None:
        self.why = why
        self.text = text
        #: True for a result that depends on how deep or how long the folding ran, not on the
        #: expression: it is never remembered.
        self.transient = transient

    def __repr__(self) -> str:
        return f"Unresolved({self.why!r}, {self.text!r})"


class FoldedList(list):
    """A list; ``complete`` is False when a ``*star`` did not fold, so it may hold more."""

    complete: bool = True


class FoldedTuple(FoldedList):
    """A tuple, kept apart from a list because ``"%s" % (a,)`` and ``"%s" % [a]`` differ."""


class FoldedDict(dict):
    """A dict; ``complete`` is False when a ``**spread`` did not fold, so it may hold more."""

    complete: bool = True


#: ``lookup`` answer for a key a complete container does not hold.
ABSENT = object()


def lookup(container: Any, key: Any) -> Any:
    """``container[key]`` for a folded dict: the value, :data:`ABSENT` when a complete dict
    does not hold the key, or an :class:`Unresolved` when it might (an incomplete dict, or
    ``container`` is itself unresolved or not a dict)."""
    if isinstance(container, Unresolved):
        return container
    if not isinstance(container, dict):
        return Unresolved("not a dict", "")
    if key in container:
        return container[key]
    if getattr(container, "complete", True):
        return ABSENT
    return Unresolved("the dict is built from a value that is not a literal", "")


def is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def text_of(value: Any) -> Optional[str]:
    """The string a folded value is, or ``None`` when it is anything else."""
    return value if isinstance(value, str) else None


# ── scopes ────────────────────────────────────────────────────────────


@dataclass
class _Add:
    """``name += value``: one step of a :class:`Recipe`."""

    value: ast.expr
    line: int
    end: int


@dataclass
class _Loop:
    """``for target in iterable:`` whose body holds steps of a :class:`Recipe`."""

    loop: ast.For
    body: List[Union["_Add", "_Loop"]]


@dataclass
class Recipe:
    """A name built by ``+=`` after its one assignment: ``base`` and the steps in source order. A
    step is a ``+=`` at the top level of the scope's body, or one inside a ``for`` that is
    itself at the top level (a loop over a dict that folds is read once per item). ``ready`` is
    the last line of the steps: used on a line before it, the name is not whole yet."""

    base: ast.expr
    steps: List[Union[_Add, _Loop]]
    ready: int


class _Acc:
    """What the steps of a recipe have added up to."""

    __slots__ = ("value", "complete", "budget")

    def __init__(self, value: Union[List[Any], str], complete: bool, budget: int) -> None:
        self.value = value
        self.complete = complete
        self.budget = budget


def _bind(target: ast.AST, item: Any) -> Optional[Dict[str, Any]]:
    """The names a ``for`` target takes for one item, or ``None`` when the item does not fit."""
    if isinstance(target, ast.Name):
        return {target.id: item}
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(item, (list, tuple)):
        if len(target.elts) != len(item):
            return None
        frame: Dict[str, Any] = {}
        for element, value in zip(target.elts, item, strict=True):
            inner = _bind(element, value)
            if inner is None:
                return None
            frame.update(inner)
        return frame
    return None


@dataclass
class Scope:
    """The names one module, class or function body binds.

    ``values`` holds a name assigned exactly once, with its expression; ``blocked`` holds every
    other bound name, with why it cannot be read (a parameter, a loop variable, assigned
    twice). ``classes`` holds the classes defined here.
    """

    owner: ast.AST
    parent: Optional["Scope"] = None
    values: Dict[str, ast.expr] = field(default_factory=dict)
    blocked: Dict[str, str] = field(default_factory=dict)
    classes: Dict[str, ast.ClassDef] = field(default_factory=dict)
    #: Every assignment of a name assigned more than once, in source order, when each write to it
    #: was a plain ``name = value`` (no ``+=``, unpacking, loop variable, import ...): what a later
    #: assignment starts from (``writer = writer.trigger(...)``) can then be read. A name assigned
    #: once is in ``values``; a name written in any other way is in neither.
    assignments: Dict[str, List[ast.expr]] = field(default_factory=dict)
    #: Names assigned once and then added to by ``+=`` statements (and nothing else): see
    #: :class:`Recipe`. Not in ``values``.
    recipes: Dict[str, Recipe] = field(default_factory=dict)
    #: The parameters of the function: values that come from the caller.
    parameters: Set[str] = field(default_factory=set)


#: Called with no argument, these are the names of the module (or class) body they are in; inside a
#: function they are a copy, and writing to a copy changes no name.
_NAMESPACES = frozenset({"locals", "vars"})
#: Calls that can write any name of the module, or run code the folder cannot see.
_RUNNERS = frozenset({"globals", "exec", "eval"})
_SETTERS = frozenset({"setattr", "delattr"})
_IMPORTERS = frozenset({"import_module", "__import__"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
#: ``dict.update(C, ...)`` and the like: the method of the type called on the object.
_CONTAINER_TYPES = frozenset(
    {"dict", "list", "set", "bytearray", "deque", "OrderedDict", "defaultdict", "Counter"}
)
#: ``operator.setitem(C, ...)``: the in-place functions of the ``operator`` module.
_OPERATOR_MUTATORS = frozenset(
    {"setitem", "delitem", "iadd", "iand", "iconcat", "ifloordiv", "ilshift", "imod", "imul"}
    | {"ior", "ipow", "irshift", "isub", "itruediv", "ixor", "__setitem__", "__delitem__"}
)
#: Passes over the calls of the file to follow a parameter that is changed through a helper that
#: calls a helper.
_MAX_PARAMETER_PASSES = 8


def _text_size(value: Any) -> int:
    """The characters of text a folded value holds, one level down (a string, a list of them)."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(len(item) for item in value if isinstance(item, str))
    return 0


def _roots(node: ast.AST) -> List[str]:
    """The names an expression reaches into: ``CFG["t"].x`` is ``CFG``; a list, tuple, set or
    dict written in place (``[C][0]``), a conditional and an ``or`` are every name in them;
    ``vars(obj)`` is ``obj``. Anything else (a call, a literal) reaches no name."""
    names: List[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.Subscript, ast.Attribute, ast.Starred)):
            stack.append(current.value)
        elif isinstance(current, (ast.List, ast.Tuple, ast.Set)):
            stack.extend(current.elts)
        elif isinstance(current, ast.Dict):
            stack.extend(v for v in current.values if v is not None)
        elif isinstance(current, ast.IfExp):
            stack.extend((current.body, current.orelse))
        elif isinstance(current, ast.BoolOp):
            stack.extend(current.values)
        elif (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id == "vars"
            and len(current.args) == 1
        ):
            stack.append(current.args[0])  # ``vars(obj)`` is the attributes of ``obj``
        elif isinstance(current, ast.Name):
            names.append(current.id)
    return names


def _levels(node: ast.AST) -> int:
    """How many items or attributes deep ``node`` is below its name (``CFG`` 0, ``CFG["t"]`` 1)."""
    depth = 0
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
        depth += 1
    return depth


def _contents(value: ast.AST) -> List[str]:
    """The names directly inside a list, tuple, set or dict written in place."""
    if isinstance(value, ast.Dict):
        items = [v for v in value.values if v is not None]
    elif isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        items = list(value.elts)
    else:
        return []
    return [item.id for item in items if isinstance(item, ast.Name)]


def _target_exprs(target: ast.AST) -> List[ast.AST]:
    """The items and attributes assigned to by a target (``C["x"]``; a tuple holds several)."""
    found: List[ast.AST] = []
    stack = [target]
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.Tuple, ast.List)):
            stack.extend(current.elts)
        elif isinstance(current, ast.Starred):
            stack.append(current.value)
        elif isinstance(current, (ast.Subscript, ast.Attribute)):
            found.append(current)
    return found


def _rebound_names(tree: ast.AST) -> Dict[str, str]:
    """Names the file changes after they are bound, which the single assignment a scope sees does
    not fix: a ``global`` or ``nonlocal`` declaration, an item or attribute assigned
    (``CFG["t"] = ...``, also as a ``for`` or ``with`` target), a mutating method called on the
    name (``CFG.update(...)``, ``dict.update(CFG, ...)``, ``getattr(CFG, "update")(...)``,
    ``operator.setitem(CFG, ...)``), ``setattr`` or ``delattr`` on it, a helper of the file that
    changes its parameter called with it, ``del``, an assignment expression (``(T := ...)``), a
    ``match`` capture and a ``type`` alias; a name that is another name (``D = C``) is changed
    when either is, and what is held in a list or dict written in place (``D = {"c": C}``) is
    changed by an item of an item of it. A file that writes names by string where it cannot be
    told which name (``globals()``, ``exec``, ``eval``, ``vars()`` or ``locals()`` outside a
    function, ``sys.modules[__name__]``, ``setattr`` on something that is not a name) is
    ``"*"``: no name in it is trusted. Reading by string (``vars(args)``, ``self.__dict__``,
    ``"{x}".format(**locals())`` in a function) changes no constant, and is not that. One pass
    over the tree, without recursion. ``{name: why}``."""
    out: Dict[str, str] = {}
    aliases: List[Tuple[str, str, int]] = []  # (name, the name it is another name for, line)
    #: Names with a ``+=`` on them, and the line: a list is changed in place, so a name for the
    #: same list made before is changed too.
    augmented: List[Tuple[str, int]] = []
    held: Dict[str, List[str]] = {}
    #: Names changed in place inside each function, by the function.
    touched: Dict[int, Set[str]] = {}
    defs: Dict[str, ast.AST] = {}
    calls: List[Tuple[ast.Call, Optional[ast.AST]]] = []
    in_place = "is changed in place elsewhere in the file"

    def dynamic(how: str) -> None:
        out.setdefault("*", f"the file reaches names by string ({how})")

    #: Objects changed in place, seen in the walk (statements come in any order, and what a name
    #: holds is known only at the end): ``(expression, the function it is in)``.
    pending: List[Tuple[ast.AST, Optional[ast.AST]]] = []

    def touch(obj: ast.AST, owner: Optional[ast.AST]) -> None:
        pending.append((obj, owner))

    def apply(obj: ast.AST, owner: Optional[ast.AST]) -> None:
        """``obj`` (an expression for an object) is changed in place."""
        deep = _levels(obj) > 0  # an item of a name: what the name holds is changed too
        for name in _roots(obj):
            targets = [name, *(held.get(name, ()) if deep else ())]
            for each in targets:
                out.setdefault(each, f"`{each}` {in_place}")
                if owner is not None:
                    touched.setdefault(id(owner), set()).add(each)

    stack: List[Tuple[ast.AST, bool, Optional[ast.AST]]] = [(tree, False, None)]
    while stack:
        node, in_function, owner = stack.pop()
        inside = in_function or isinstance(node, _FUNCTIONS)
        here = node if isinstance(node, _DEFS) else owner
        stack.extend((child, inside, here) for child in ast.iter_child_nodes(node))
        if isinstance(node, _DEFS):
            defs.setdefault(node.name, node)
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                out.setdefault(name, f"`{name}` is rebound by a global or nonlocal declaration")
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            if isinstance(node, (ast.Assign, ast.Delete)):
                targets = list(node.targets)
            else:
                targets = [node.target]
            for target in targets:
                if isinstance(node, ast.AugAssign) and not isinstance(target, ast.Name):
                    touch(target, owner)  # ``D["k"] += [...]`` adds to the list D holds
                for item in _target_exprs(target):
                    touch(item.value, owner)  # type: ignore[attr-defined]
                if isinstance(target, ast.Name) and isinstance(node, ast.Delete):
                    out.setdefault(target.id, f"`{target.id}` is deleted elsewhere in the file")
                if isinstance(target, ast.Name) and isinstance(node, ast.AugAssign):
                    augmented.append((target.id, node.lineno))
                    if owner is not None:  # ``def add(args): args += [...]`` changes what it gets
                        touched.setdefault(id(owner), set()).add(target.id)
            if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
                names = [t.id for t in node.targets]  # type: ignore[attr-defined]
                if isinstance(node.value, ast.Name):
                    aliases.extend((n, node.value.id, node.lineno) for n in names)
                else:
                    for n in names:
                        held.setdefault(n, []).extend(_contents(node.value))
                # ``A = B = [...]``: both names are for the one list
                aliases.extend((names[0], other, node.lineno) for other in names[1:])
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for item in _target_exprs(node.target):
                touch(item.value, owner)  # type: ignore[attr-defined]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for item in _target_exprs(node.optional_vars):
                touch(item.value, owner)  # type: ignore[attr-defined]
        elif isinstance(node, ast.NamedExpr):
            name = node.target.id
            out.setdefault(name, f"`{name}` is assigned by an assignment expression")
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            out.setdefault(node.name, f"`{node.name}` is bound by a match pattern")
        elif isinstance(node, ast.MatchMapping) and node.rest:
            out.setdefault(node.rest, f"`{node.rest}` is bound by a match pattern")
        elif hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
            name = node.name.id
            out.setdefault(name, f"`{name}` is bound by a type alias")
        elif isinstance(node, ast.Call):
            _on_call(node, owner, in_function, touch, dynamic, calls)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "modules"
            and isinstance(node.slice, ast.Name)
            and node.slice.id == "__name__"
        ):
            dynamic("sys.modules[__name__]")
    for obj, owner in pending:
        apply(obj, owner)
    _follow_parameters(calls, defs, touched, apply)
    _spread_to_aliases(out, aliases, in_place, augmented)
    return out


def _on_call(
    node: ast.Call,
    owner: Optional[ast.AST],
    in_function: bool,
    touch: Callable[[ast.AST, Optional[ast.AST]], None],
    dynamic: Callable[[str], None],
    calls: List[Tuple[ast.Call, Optional[ast.AST]]],
) -> None:
    """What a call can change: its receiver (``CFG.update(...)``), its first argument (``setattr``,
    ``dict.update(CFG, ...)``, ``operator.setitem(CFG, ...)``), or any name, by string."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in _MUTATORS:
            receiver = func.value
            type_name = receiver.attr if isinstance(receiver, ast.Attribute) else None
            if isinstance(receiver, ast.Name):
                type_name = receiver.id
            if type_name in _CONTAINER_TYPES and node.args:
                touch(node.args[0], owner)  # ``dict.update(CFG, ...)``
            else:
                touch(receiver, owner)
        elif func.attr in _OPERATOR_MUTATORS and node.args:
            touch(node.args[0], owner)  # ``operator.setitem(CFG, ...)``
    elif isinstance(func, ast.Call) and isinstance(func.func, ast.Name):
        if func.func.id == "getattr" and func.args:
            touch(func.args[0], owner)  # ``getattr(CFG, "update")(...)``
    elif isinstance(func, ast.Name):
        called = func.id
        if called in _RUNNERS:
            dynamic(f"{called}(...)")
        elif called in _NAMESPACES and not in_function and not node.args:
            dynamic(f"{called}(...)")
        elif called in _SETTERS and node.args:
            if not _roots(node.args[0]):
                dynamic(f"{called}(...)")
            else:
                touch(node.args[0], owner)
        elif called in _OPERATOR_MUTATORS and node.args:
            touch(node.args[0], owner)  # ``from operator import setitem``
        elif called in _IMPORTERS and any(
            isinstance(arg, ast.Name) and arg.id == "__name__" for arg in node.args
        ):
            dynamic(f"{called}(__name__)")
        else:
            calls.append((node, owner))  # a helper of the file, maybe: see _follow_parameters


def _follow_parameters(
    calls: List[Tuple[ast.Call, Optional[ast.AST]]],
    defs: Dict[str, ast.AST],
    touched: Dict[int, Set[str]],
    touch: Callable[[ast.AST, Optional[ast.AST]], None],
) -> None:
    """``def add(cfg): cfg["t"] = ...`` called as ``add(CFG)`` changes ``CFG``: a helper of this
    file that changes a parameter in place changes what it is called with (through helpers that
    call helpers too: the passes end when nothing new is found)."""
    for _ in range(_MAX_PARAMETER_PASSES):
        grew = False
        for call, owner in calls:
            helper = defs.get(call.func.id)  # type: ignore[attr-defined]
            changed = touched.get(id(helper)) if helper is not None else None
            if not changed:
                continue
            arguments = helper.args  # type: ignore[attr-defined]
            positional = [a.arg for a in [*arguments.posonlyargs, *arguments.args]]
            passed = [
                arg
                for index, arg in enumerate(call.args)
                if index < len(positional) and positional[index] in changed
            ]
            passed += [kw.value for kw in call.keywords if kw.arg in changed]
            for arg in passed:
                before = len(touched.get(id(owner), ())) if owner is not None else 0
                touch(arg, owner)
                if owner is not None and len(touched.get(id(owner), ())) > before:
                    grew = True
        if not grew:
            return


def _spread_to_aliases(
    out: Dict[str, str],
    aliases: List[Tuple[str, str, int]],
    in_place: str,
    augmented: Sequence[Tuple[str, int]] = (),
) -> None:
    """``D = C`` makes ``D`` another name for the same object: a change through either is a change
    to both. Names joined by an alias are one group, and a group with a changed name is changed.
    A name that has a ``+=`` on it (a list is added to in place) changes every name for the same
    list made before that line: ``ARGS = BASE`` then ``ARGS += [...]`` changes ``BASE`` too, while
    ``OUT = ARGS`` after the last addition only reads the finished list."""
    parent: Dict[str, str] = {}

    def find(name: str) -> str:
        while parent.get(name, name) != name:
            parent[name] = parent.get(parent[name], parent[name])
            name = parent[name]
        return name

    for left, right, _line in aliases:
        parent[find(left)] = find(right)
    last_addition = _last_additions(aliases, augmented, find)
    changed = {find(name) for name, why in out.items() if in_place in why}
    if not changed and not last_addition:
        return
    added_to = {name for name, _line in augmented}
    for left, right, line in aliases:
        before_an_addition = line < last_addition.get(find(left), 0)
        for name, other in ((left, right), (right, left)):
            # the name that is added to builds its own value; the other one changes under it
            changed_by_addition = before_an_addition and (name not in added_to or other in added_to)
            if changed_by_addition or find(name) in changed:
                out.setdefault(name, f"`{name}` {in_place}")


def _last_additions(
    aliases: List[Tuple[str, str, int]],
    augmented: Sequence[Tuple[str, int]],
    find: Callable[[str], str],
) -> Dict[str, int]:
    """The line of the last ``+=`` on a name of each group of aliases, by the group."""
    members = {name for left, right, _line in aliases for name in (left, right)}
    last: Dict[str, int] = {}
    for name, line in augmented:
        if name in members:
            root = find(name)
            last[root] = max(last.get(root, 0), line)
    return last


def _target_names(target: ast.AST) -> List[str]:
    names: List[str] = []
    stack = [target]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            stack.extend(node.elts)
        elif isinstance(node, ast.Starred):
            stack.append(node.value)
    return names


def _plain_target(target: ast.AST) -> bool:
    """A ``for`` target of names only: ``x``, ``k, v``, ``(a, b)``."""
    if isinstance(target, ast.Name):
        return True
    return isinstance(target, (ast.Tuple, ast.List)) and all(
        _plain_target(item) and not isinstance(item, ast.Starred) for item in target.elts
    )


_LEAVES = (ast.Break, ast.Continue, ast.Return, ast.Raise, ast.Yield, ast.YieldFrom, ast.Await)


def _rebinds(loop: ast.For, names: Sequence[str]) -> bool:
    """Whether the body of ``loop`` binds any of ``names`` (its own variables) again: each pass
    would then work with a value other than the item."""
    wanted = set(names)
    for statement in loop.body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                if node.id in wanted:
                    return True
            elif isinstance(node, ast.ExceptHandler):
                if node.name in wanted:
                    return True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in wanted:
                    return True
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if any(
                    (alias.asname or alias.name).split(".")[0] in wanted for alias in node.names
                ):
                    return True
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                if wanted.intersection(node.names):
                    return True
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)):  # ``case i:``, ``case [*i]``
                if node.name in wanted:
                    return True
            elif isinstance(node, ast.MatchMapping):  # ``case {**i}``
                if node.rest in wanted:
                    return True
    return False


def _increment_steps(
    body: Sequence[ast.stmt], covered: Set[int]
) -> Dict[str, List[Union[_Add, _Loop]]]:
    """The ``name += ...`` statements at the top level of ``body``, and those inside a ``for`` that
    is itself at the top level (and has no ``else``, ``break``, ``continue``, ``return`` or
    ``raise``, so each item of it is gone through once), by name and in source order. The
    statements found are added to ``covered``."""
    out: Dict[str, List[Union[_Add, _Loop]]] = {}
    for stmt in body:
        if (
            isinstance(stmt, ast.AugAssign)
            and isinstance(stmt.op, ast.Add)
            and isinstance(stmt.target, ast.Name)
        ):
            end = stmt.end_lineno or stmt.lineno
            out.setdefault(stmt.target.id, []).append(_Add(stmt.value, stmt.lineno, end))
            covered.add(id(stmt))
        elif (
            isinstance(stmt, ast.For)
            and not stmt.orelse
            and _plain_target(stmt.target)
            and not any(isinstance(node, _LEAVES) for node in ast.walk(stmt))
            and not _rebinds(stmt, _target_names(stmt.target))
        ):
            for name, steps in _increment_steps(stmt.body, covered).items():
                out.setdefault(name, []).append(_Loop(stmt, steps))
    return out


def _last_line(step: Union[_Add, _Loop]) -> int:
    if isinstance(step, _Add):
        return step.end
    return step.loop.end_lineno or step.loop.lineno


def _first_line(step: Union[_Add, _Loop]) -> int:
    return step.line if isinstance(step, _Add) else step.loop.lineno


def build_scope(owner: ast.AST, parent: Optional[Scope] = None) -> Scope:
    """The bindings of a module, class or function body, not descending into nested
    functions, classes or lambdas (each has a scope of its own)."""
    scope = Scope(owner=owner, parent=parent)
    counts: Dict[str, int] = {}
    #: Each plain assignment of a name, and the names written in any other way.
    plain: Dict[str, List[ast.expr]] = {}
    other: Set[str] = set()

    def block(name: str, why: str) -> None:
        other.add(name)
        scope.values.pop(name, None)
        if scope.blocked.get(name) == "imported":  # imported, then bound some other way here
            scope.blocked[name] = why
        else:
            scope.blocked.setdefault(name, why)

    def record(name: str, value: ast.expr) -> None:
        plain.setdefault(name, []).append(value)
        if name in scope.blocked:
            if scope.blocked[name] == "imported":
                scope.blocked[name] = f"`{name}` is assigned after it is imported"
            return
        counts[name] = counts.get(name, 0) + 1
        if counts[name] > 1:
            scope.values.pop(name, None)
            scope.blocked.setdefault(name, f"`{name}` is assigned more than once")
        else:
            scope.values[name] = value

    if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = owner.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if args.vararg:
            params.append(args.vararg)
        if args.kwarg:
            params.append(args.kwarg)
        for param in params:
            block(param.arg, f"`{param.arg}` is a parameter of the function")
            scope.parameters.add(param.arg)
    body = getattr(owner, "body", [])
    covered: Set[int] = set()
    steps = _increment_steps(body, covered) if isinstance(body, list) else {}
    stack: List[ast.AST] = list(reversed(body)) if isinstance(body, list) else []
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            block(node.name, f"`{node.name}` is a function")
        elif isinstance(node, ast.ClassDef):
            scope.classes[node.name] = node
            block(node.name, f"`{node.name}` is a class")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    record(target.id, node.value)
                else:
                    for name in _target_names(target):
                        block(name, f"`{name}` is unpacked from a value")
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                record(node.target.id, node.value)
        elif isinstance(node, ast.AugAssign):
            if id(node) not in covered:  # a step of a recipe is decided at the end
                for name in _target_names(node.target):
                    block(name, f"`{name}` is changed in place")
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for name in _target_names(node.target):
                block(name, f"`{name}` is a loop variable")
            stack.extend(reversed(node.body + node.orelse))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    for name in _target_names(item.optional_vars):
                        block(name, f"`{name}` is a context-manager result")
            stack.extend(reversed(node.body))
        elif isinstance(node, (ast.If, ast.While)):
            stack.extend(reversed(node.body + node.orelse))
        elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            for handler in node.handlers:
                if handler.name:
                    block(handler.name, f"`{handler.name}` is an exception")
                stack.extend(reversed(handler.body))
            stack.extend(reversed(node.body + node.orelse + node.finalbody))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                if name != "*":
                    other.add(name)
                    scope.blocked.setdefault(name, "imported")
                    scope.values.pop(name, None)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                block(name, f"`{name}` is declared global or nonlocal")
        elif type(node).__name__ == "Match":
            for case in node.cases:
                stack.extend(reversed(case.body))
    for name, found in steps.items():
        base = scope.values.get(name)
        if base is not None and base.lineno < _first_line(found[0]):
            ready = max(_last_line(step) for step in found)
            scope.recipes[name] = Recipe(scope.values.pop(name), found, ready)
        else:
            block(name, f"`{name}` is changed in place")
    scope.assignments = {
        name: sorted(values, key=lambda e: (e.lineno, e.col_offset))
        for name, values in plain.items()
        if len(values) > 1 and name not in other
    }
    return scope


# ── formatting, on this module's own code ─────────────────────────────

_FIELD = re.compile(r"\{(?P<name>[A-Za-z_][A-Za-z0-9_]*|\d*)\}")
_PERCENT = re.compile(r"%(?:\((?P<key>[^)]*)\))?(?P<conv>[sdr%])")


def _too_long(text: str, limit: int = MAX_STRING) -> Optional[Unresolved]:
    if len(text) > limit:
        return Unresolved(f"it is longer than {limit} characters", text[:60] + "...")
    return None


def format_template(
    fmt: str,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
    hole: Optional[Callable[[Union[int, str]], str]] = None,
    limit: int = MAX_STRING,
) -> Union[str, Unresolved]:
    """``fmt.format(*args, **kwargs)`` for plain ``{}``, ``{0}`` and ``{name}`` fields only.

    A field with a conversion, a spec, an attribute or an index (``{x!r}``, ``{x:>9}``,
    ``{x.y}``), a field whose value is unresolved, and an unbalanced brace are all
    unresolved. A field whose value is unresolved shows as itself in the text, or, when the
    caller gives ``hole``, as the token ``hole(key)`` returns for it (``key`` is the field's
    position or name; the caller reads text that must stay parseable, such as SQL).
    """
    out: List[str] = []
    auto = 0
    unresolved: Optional[str] = None
    size = len(fmt)
    i = 0
    while i < len(fmt):
        char = fmt[i]
        if char == "{":
            if fmt.startswith("{{", i):
                out.append("{")
                i += 2
                continue
            match = _FIELD.match(fmt, i)
            if match is None:
                return Unresolved("a format field with a spec, conversion or attribute", fmt)
            name = match.group("name")
            if name == "":
                key: Union[int, str] = auto
                auto += 1
            else:
                key = int(name) if name.isdigit() else name
            if isinstance(key, int):
                value = args[key] if key < len(args) else Unresolved("a missing argument")
            else:
                value = kwargs.get(key, Unresolved(f"`{key}` is not given"))
            if isinstance(value, Unresolved) or not is_scalar(value):
                if hole is not None:
                    out.append(hole(key))
                else:
                    unresolved = unresolved or (
                        value.why if isinstance(value, Unresolved) else "a value that is not text"
                    )
                    out.append(match.group(0))
            else:
                piece = str(value)
                size += len(piece)
                if size > limit:
                    return Unresolved(f"it is longer than {limit} characters", "")
                out.append(piece)
            i = match.end()
        elif char == "}":
            if fmt.startswith("}}", i):
                out.append("}")
                i += 2
                continue
            return Unresolved("an unbalanced brace in a format string", fmt)
        else:
            out.append(char)
            i += 1
    text = "".join(out)
    if unresolved is not None:
        return Unresolved(unresolved, text)
    return _too_long(text, limit) or text


def percent_template(
    fmt: str,
    operand: Any,
    hole: Optional[Callable[[Union[int, str]], str]] = None,
    limit: int = MAX_STRING,
) -> Union[str, Unresolved]:
    """``fmt % operand`` for ``%s``, ``%d``, ``%r``, ``%%`` and ``%(key)s`` only. A ``%s``
    whose value is unresolved becomes the token ``hole(key)`` returns, when given (``key`` is
    the operand's position or name)."""
    keyed = isinstance(operand, dict)
    positional: List[Any] = (
        list(operand) if isinstance(operand, (tuple, FoldedTuple)) else [operand]
    )
    out: List[str] = []
    used = 0
    pos = 0
    size = len(fmt)
    for part in re.split(r"(%(?:\([^)]*\))?[A-Za-z%])", fmt):
        if not part.startswith("%"):
            if "%" in part:
                return Unresolved("a percent format other than %s, %d, %r", fmt)
            out.append(part)
            continue
        match = _PERCENT.fullmatch(part)
        if match is None:
            return Unresolved("a percent format other than %s, %d, %r", fmt)
        if match.group("conv") == "%":
            out.append("%")
            continue
        if match.group("key") is not None:
            if not keyed:
                return Unresolved("a keyed percent format without a dict", fmt)
            operand_key: Union[int, str] = match.group("key")
            value = operand.get(operand_key, Unresolved("a missing key"))
        else:
            if keyed or pos >= len(positional):
                return Unresolved("the percent format and its operands do not match", fmt)
            operand_key = pos
            value = positional[pos]
            pos += 1
        used += 1
        if isinstance(value, Unresolved) or not is_scalar(value):
            if hole is not None and match.group("conv") == "s":
                out.append(hole(operand_key))
                continue
            return Unresolved(value.why if isinstance(value, Unresolved) else "not text", fmt)
        if match.group("conv") == "d":
            if not isinstance(value, int) or isinstance(value, bool):
                return Unresolved("%d takes a whole number", fmt)
            piece = str(value)
        elif match.group("conv") == "r":
            piece = repr(value)
        else:
            piece = str(value)
        size += len(piece)
        if size > limit:
            return Unresolved(f"it is longer than {limit} characters", "")
        out.append(piece)
    if not keyed and pos != len(positional):
        return Unresolved("the percent format and its operands do not match", fmt)
    text = "".join(out)
    return _too_long(text, limit) or text


# ── the module index ──────────────────────────────────────────────────


class ModuleIndex:
    """The modules of the scanned tree, found by dotted name, parsed on demand.

    A project's own ``from config import ORDERS_TABLE`` resolves to the ``config.py`` that is in
    the tree. The import root is not known (a jobs directory, ``src``, the repository), so a
    file ``a/b/c.py`` answers to ``a.b.c``, ``b.c`` and ``c``; of several files that answer to
    one name, the one next to the importing file wins, else the name is ambiguous and
    unresolved. ``extra`` adds modules from outside the tree (``dotted name -> file``), which the
    operator names in the conventions.
    """

    def __init__(
        self,
        files: Sequence[SourceFile],
        *,
        variables: Optional[Mapping[str, str]] = None,
        extra: Optional[Mapping[str, Path]] = None,
        budget: Optional[SourceBudget] = None,
        airflow_variables: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.variables: Mapping[str, str] = dict(variables or {})
        self.airflow_variables: Mapping[str, str] = dict(airflow_variables or {})
        self._dotted: Dict[str, List[SourceFile]] = {}
        #: Parsed modules, least recently used first, at most ``MAX_CACHED_NODES`` syntax-tree
        #: nodes between them: a scan of thousands of files keeps a few, not all, in memory.
        self._modules: "OrderedDict[str, Module]" = OrderedDict()
        self._cached_nodes = 0
        #: Files read at least once: a read of one of these again is a reload.
        self._read_once: Set[str] = set()
        #: Where a dotted name is found, by ``(name, importing file)``: the answer does not change
        #: and a name many files answer to costs a pass over them.
        self._located: Dict[Tuple[str, str], Union[Tuple[SourceFile, List[str]], str]] = {}
        self._folders: Dict[str, "Folder"] = {}
        self.unreadable: Dict[str, str] = {}
        #: What this scan has read, in all: every read of a source file counts against it.
        self.budget = budget if budget is not None else SourceBudget()
        for source in files:
            parts = source.rel[: -len(".py")].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for start in range(len(parts)):
                self._dotted.setdefault(".".join(parts[start:]), []).append(source)
        for dotted, path in (extra or {}).items():
            self._dotted.setdefault(dotted, []).append(SourceFile(rel=f"<{dotted}>", path=path))

    def load(self, source: SourceFile) -> Optional[Module]:
        """The parsed module, or ``None`` (the reason is in ``unreadable``)."""
        if source.rel in self.unreadable:
            return None
        cached = self._modules.get(source.rel)
        if cached is not None:
            self._modules.move_to_end(source.rel)
            return cached
        try:
            again = source.rel in self._read_once
            data = read_source(source.path, self.budget, reload=again)
            self._read_once.add(source.rel)
            # Room is made before the parse, not after: a file has at most as many nodes as it
            # has bytes, and two big modules alive at once is what a cache is there to prevent.
            while self._modules and self._cached_nodes + len(data) > MAX_CACHED_NODES:
                self._evict()
            module = parse_module(source.rel, data)
        except SourceUnreadable as exc:
            self.unreadable[source.rel] = str(exc)
            return None
        self._modules[source.rel] = module
        self._cached_nodes += module.nodes
        while self._cached_nodes > MAX_CACHED_NODES and len(self._modules) > 1:
            self._evict()
        return module

    def _evict(self) -> None:
        rel, old = self._modules.popitem(last=False)
        self._cached_nodes -= old.nodes
        self._folders.pop(rel, None)  # a folder is only kept with its module

    def folder(self, module: Module) -> "Folder":
        """The folder of a module, kept while the module is. A module this index did not load
        (a DAG file, read once) gets a folder of its own, kept by nobody."""
        if self._modules.get(module.rel) is not module:
            return Folder(module, index=self)
        found = self._folders.get(module.rel)
        if found is None:
            found = Folder(module, index=self)
            self._folders[module.rel] = found
        return found

    def _candidates(self, dotted: str, from_rel: str) -> List[SourceFile]:
        found = self._dotted.get(dotted, [])
        if len(found) <= 1:
            return found
        here = from_rel.rpartition("/")[0]
        near = [f for f in found if f.rel.rpartition("/")[0] == here]
        return near if len(near) == 1 else found

    def _absolute(self, qualified: str, from_rel: str) -> str:
        """``qualified`` with leading dots made absolute against the importing file."""
        level = len(qualified) - len(qualified.lstrip("."))
        if level == 0:
            return qualified
        package = from_rel.split("/")[:-1]
        if level > 1:
            package = package[: len(package) - (level - 1)] if level - 1 <= len(package) else []
        rest = qualified[level:]
        return ".".join([*package, rest] if rest else package)

    def resolve(self, qualified: str, from_rel: str) -> Union[Tuple[Module, List[str]], str]:
        """The module holding ``qualified`` and the attribute chain inside it, or the reason
        there is none (as text).

        ``config.paths.ORDERS`` is the module ``config.paths`` and ``["ORDERS"]`` (or the module
        ``config`` and ``["paths", "ORDERS"]``: the longest module name wins).
        """
        located = self._locate(qualified, from_rel)
        if isinstance(located, str):
            return located
        source, chain = located
        module = self.load(source)
        if module is None:
            return f"the module `{qualified.rsplit('.', len(chain))[0]}` cannot be read"
        return module, chain

    def _locate(self, qualified: str, from_rel: str) -> Union[Tuple[SourceFile, List[str]], str]:
        """The file holding ``qualified`` and the attribute chain inside it, or why there is
        none. Remembered: nothing here depends on what has been read."""
        key = (qualified, from_rel)
        found = self._located.get(key)
        if found is not None:
            return found
        found = self._locate_uncached(qualified, from_rel)
        self._located[key] = found
        return found

    def _locate_uncached(
        self, qualified: str, from_rel: str
    ) -> Union[Tuple[SourceFile, List[str]], str]:
        parts = self._absolute(qualified, from_rel).split(".")
        for cut in range(len(parts) - 1, 0, -1):
            dotted = ".".join(parts[:cut])
            candidates = self._candidates(dotted, from_rel)
            if len(candidates) > 1:
                return f"more than one scanned file answers to the module name `{dotted}`"
            if len(candidates) == 1:
                return candidates[0], parts[cut:]
        own = self._in_own_package(parts, from_rel)
        if own is not None:
            return own
        return f"`{qualified}` is not in the scanned files"

    def _in_own_package(
        self, parts: Sequence[str], from_rel: str
    ) -> Optional[Tuple[SourceFile, List[str]]]:
        """``teamx.dags.nightly.settings.STEP`` imported by ``nightly/dag.py`` of a scan of
        ``dags/``: the scanned directory sits below the import root, so the leading parts of the
        name are packages the scan does not see. When the name runs through the importing file's
        own directory (or one of its parents inside the scan: ``nightly``) and goes on to a file
        there (``nightly/settings.py``), it is that file: the package the importing file is in
        is the one the name ends in. A name that does not run through those directories is not
        guessed at (``teamx.weekly.settings`` from ``nightly/dag.py``)."""
        here = from_rel.split("/")[:-1]
        for depth in range(len(here), 0, -1):  # the importing file's directory, then its parents
            package = here[:depth]
            for at in range(len(parts) - 2, -1, -1):
                if parts[at] != package[-1]:
                    continue
                head = parts[: at + 1]
                common = min(len(head), len(package))
                if list(head[-common:]) != package[-common:]:
                    continue
                for cut in range(len(parts) - 1, at, -1):
                    rel = "/".join([*package, *parts[at + 1 : cut]])
                    dotted = ".".join([*package, *parts[at + 1 : cut]])
                    for source in self._dotted.get(dotted, ()):
                        if source.rel == f"{rel}/__init__.py" or (
                            cut > at + 1 and source.rel == f"{rel}.py"
                        ):
                            return source, list(parts[cut:])
        return None


# ── the folder ────────────────────────────────────────────────────────

_STR_METHODS = frozenset(
    {"lower", "upper", "strip", "lstrip", "rstrip", "replace", "removeprefix", "removesuffix"}
)
_JOINS = frozenset({"os.path.join", "posixpath.join"})
_ENV_GET = frozenset({"os.environ.get", "os.getenv"})
#: ``Variable.get`` by where ``Variable`` is imported from (Airflow 2 and 3).
_VARIABLE_GET = frozenset(
    {
        "airflow.models.Variable.get",
        "airflow.models.variable.Variable.get",
        "airflow.sdk.Variable.get",
        "airflow.sdk.definitions.variable.Variable.get",
    }
)
#: The calls a file may import a name from two places for and still mean them (a ``try`` that
#: imports ``Variable`` from Airflow 3 and falls back to Airflow 2).
_READ_THROUGH_IMPORT = _ENV_GET | _VARIABLE_GET


class Folder:
    """Folds expressions of one module. See the module docstring for what folds."""

    def __init__(
        self,
        module: Module,
        *,
        index: Optional[ModuleIndex] = None,
        variables: Optional[Mapping[str, str]] = None,
        airflow_variables: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.module = module
        self.index = index
        self.variables: Mapping[str, str] = (
            variables if variables is not None else (index.variables if index else {})
        )
        self.airflow_variables: Mapping[str, str] = (
            airflow_variables
            if airflow_variables is not None
            else (index.airflow_variables if index else {})
        )
        self._scopes: Dict[ast.AST, Scope] = {}
        self._active: Set[int] = set()
        #: While folding text that must stay parseable (SQL), an unresolved field is replaced
        #: by a token and recorded here: ``{token: what it stood for}``.
        self._holes: Optional[Dict[str, str]] = None
        #: The longest string a fold may build: :data:`MAX_STRING`, or what :meth:`fold_sql` is
        #: given. A fold under a longer limit remembers nothing, so a longer string never reaches
        #: a fold that has the shorter one.
        self._limit = MAX_STRING
        #: Characters of string the folds of this module have built (:data:`MAX_STRING_WORK`).
        self._work = 0
        self._module_scope = self._scope(module.tree, None)
        #: Folded values by ``(node, scope)``: an expression that many names refer to is folded
        #: once, so a chain of names each built from the one before costs one pass, not a tree.
        self._memo: Dict[Tuple[int, int], Any] = {}
        self._memo_chars = 0
        self._nesting = 0
        self._steps_call = 0
        self._steps_module = 0
        #: True once a fold was cut short by the work budget: some names are then reported as
        #: not constant (``Unresolved`` says so), and the importer says so once per file.
        self.exhausted = False
        self._rebound = _rebound_names(module.tree)
        #: The loop variables of the recipe being applied, innermost last: a name that is one of
        #: them is what the loop holds on this pass. Nothing is remembered while one is set.
        #: The loop variables of the steps being folded, each with the scope the loop is in: a
        #: name of that spelling that another scope binds is not the loop's.
        self._env: List[Tuple[Scope, Dict[str, Any]]] = []
        #: What a recipe came to, by ``id(recipe)``.
        self._recipe_values: Dict[int, Any] = {}

    # scopes

    def _scope(self, owner: ast.AST, parent: Optional[Scope]) -> Scope:
        scope = self._scopes.get(owner)
        if scope is None:
            scope = build_scope(owner, parent)
            self._scopes[owner] = scope
        return scope

    def scope_of(self, node: ast.AST) -> Scope:
        """The scope ``node`` is evaluated in: its nearest enclosing function (and that
        function's enclosing functions), else the module."""
        chain: List[ast.AST] = []
        parents = self.module.parents
        current: Optional[ast.AST] = node
        while current is not None:
            current = parents.get(current)
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                chain.append(current)
        scope = self._module_scope
        for owner in reversed(chain):
            scope = self._scope(owner, scope)
        return scope

    def how_bound(self, node: ast.Name) -> str:
        """How the name ``node`` is bound where it is used: ``"once"`` (one assignment),
        ``"many"`` (more than one, plain assignments only), ``"parameter"`` (a value from the
        caller), ``"imported"``, ``"unbound"`` (nothing in the file binds it: a builtin), or
        ``"other"`` (a loop variable, an unpacked name, one changed in place, a function or a
        class, a name the file changes elsewhere)."""
        name = node.id
        if self.rebound(name):
            return "other"
        scope = self._binding_scope(name, self.scope_of(node))
        if scope is None:
            return "imported" if name in self.module.imports else "unbound"
        if name in scope.values:
            return "once"
        if name in scope.recipes or name in scope.classes:
            return "other"
        if name in scope.assignments:
            return "many"
        if name in scope.parameters:
            return "parameter"
        return "imported" if scope.blocked.get(name) == "imported" else "other"

    def rebound(self, name: str) -> Optional[str]:
        """Why ``name`` cannot be trusted to hold what its assignments say: the file changes it
        elsewhere or writes names by string (see :func:`_rebound_names`). ``None`` when nothing
        does."""
        return self._rebound.get(name) or self._rebound.get("*")

    # entry points

    def fold(self, node: ast.AST, scope: Optional[Scope] = None, _depth: int = 0) -> Any:
        """The value ``node`` fixes, or an :class:`Unresolved`.

        Bounded: a call visits at most :data:`MAX_STEPS_PER_FOLD` nodes and a module at most
        :data:`MAX_STEPS_PER_MODULE` in all (past either, the answer is ``Unresolved`` and
        :attr:`exhausted` is set), and a value already folded is remembered, so many names
        built from the one before cost one pass each."""
        if _depth > MAX_FOLD_DEPTH:
            return Unresolved("it is nested too deeply", snippet(node), transient=True)
        if scope is None:
            scope = self.scope_of(node)
        if self._nesting == 0:
            self._steps_call = 0
        self._steps_call += 1
        self._steps_module += 1
        if self._steps_call > MAX_STEPS_PER_FOLD or self._steps_module > MAX_STEPS_PER_MODULE:
            self.exhausted = True
            return Unresolved("it takes too much work to fold", snippet(node), transient=True)
        if self._work > MAX_STRING_WORK:  # a module that has built its share builds no more
            self.exhausted = True
            return Unresolved("it takes too much work to fold", snippet(node), transient=True)
        key = id(node)
        if key in self._active:
            return Unresolved("it refers to itself", snippet(node))
        memo_key = (key, id(scope))
        remember = self._holes is None and self._limit == MAX_STRING and not self._env
        if remember and memo_key in self._memo:
            return self._memo[memo_key]
        self._active.add(key)
        self._nesting += 1
        try:
            method = getattr(self, f"_fold_{type(node).__name__}", None)
            if method is None:
                value: Any = Unresolved(f"a {type(node).__name__} expression", snippet(node))
            else:
                value = method(node, scope, _depth + 1)
        finally:
            self._active.discard(key)
            self._nesting -= 1
        if isinstance(value, str) and isinstance(node, (ast.JoinedStr, ast.BinOp, ast.Call)):
            # a string this fold built, not one it found
            if self._spend(len(value)) is not None:
                value = Unresolved("it takes too much work to fold", snippet(node), transient=True)
        if remember and not (isinstance(value, Unresolved) and value.transient):
            weight = _text_size(value)
            if self._memo_chars + weight <= MAX_MEMO_CHARS:
                self._memo_chars += weight
                self._memo[memo_key] = value
        return value

    def fold_sql(
        self, node: ast.AST, scope: Optional[Scope] = None, limit: int = MAX_STRING
    ) -> Tuple[Union[str, Unresolved], Dict[str, str]]:
        """:meth:`fold` for text that must stay parseable, SQL: a field of an f-string, a
        ``.format``, a ``%`` or a ``+`` that does not fold becomes a token (``__fluidhole0__``)
        instead of making the whole text unresolved. Returns the text and ``{token: what it
        stood for}``; text with no hole is exactly what :meth:`fold` gives. ``limit`` is the
        longest string it builds (:data:`MAX_SQL_STRING` for a ``spark.sql`` statement, which
        may be long; the default for any other text that must stay parseable, a command line)."""
        holes: Dict[str, str] = {}
        previous, self._holes = self._holes, holes
        previous_limit, self._limit = self._limit, limit
        try:
            value = self.fold(node, scope)
        finally:
            self._holes, self._limit = previous, previous_limit
        if isinstance(value, str) or isinstance(value, Unresolved):
            return value, holes
        return Unresolved("it is not a string", snippet(node)), holes

    def _hole_for(
        self, positional: Sequence[ast.expr], keyword: Mapping[str, ast.expr]
    ) -> Optional[Callable[[Union[int, str]], str]]:
        """The ``hole`` callback of :func:`format_template` and :func:`percent_template` while
        folding SQL: it names the hole by the expression the operand was written as."""
        if self._holes is None:
            return None

        def hole(key: Union[int, str]) -> str:
            node = positional[key] if isinstance(key, int) and key < len(positional) else None
            node = node if node is not None else keyword.get(str(key))
            return self._hole(snippet(node, 40) if node is not None else str(key))

        return hole

    def _too_long(self, text: str) -> Optional[Unresolved]:
        return _too_long(text, self._limit)

    def _spend(self, chars: int) -> Optional[Unresolved]:
        """Charge ``chars`` characters of string work to this module: ``None`` while it is within
        :data:`MAX_STRING_WORK`, else why not (and the module is :attr:`exhausted`)."""
        self._work += chars
        if self._work > MAX_STRING_WORK:
            self.exhausted = True
            return Unresolved("it takes too much work to fold", "", transient=True)
        return None

    def _hole(self, described: str) -> str:
        assert self._holes is not None
        token = f"__fluidhole{len(self._holes)}__"
        self._holes[token] = described
        return token

    def string(self, node: ast.AST, scope: Optional[Scope] = None) -> Union[str, Unresolved]:
        """:meth:`fold` that must be a string."""
        value = self.fold(node, scope)
        if isinstance(value, str) or isinstance(value, Unresolved):
            return value
        return Unresolved("it is not a string", snippet(node))

    # literals

    def _fold_Constant(self, node: ast.Constant, scope: Scope, depth: int) -> Any:
        value = node.value
        if is_scalar(value):
            if isinstance(value, str):
                return self._too_long(value) or value
            return value
        return Unresolved("a value that is not text or a number", snippet(node))

    def _items(
        self, elements: Sequence[ast.expr], scope: Scope, depth: int
    ) -> Tuple[List[Any], bool]:
        items: List[Any] = []
        complete = True
        for element in elements[:MAX_ITEMS]:
            if isinstance(element, ast.Starred):
                spread = self.fold(element.value, scope, depth)
                if isinstance(spread, (list, tuple)):
                    items.extend(spread)
                    complete = complete and getattr(spread, "complete", True)
                else:
                    complete = False
            else:
                items.append(self.fold(element, scope, depth))
        return items, complete and len(elements) <= MAX_ITEMS

    def _fold_List(self, node: ast.List, scope: Scope, depth: int) -> Any:
        items, complete = self._items(node.elts, scope, depth)
        result = FoldedList(items)
        result.complete = complete
        return result

    def _fold_Tuple(self, node: ast.Tuple, scope: Scope, depth: int) -> Any:
        items, complete = self._items(node.elts, scope, depth)
        result = FoldedTuple(items)
        result.complete = complete
        return result

    def _fold_Set(self, node: ast.Set, scope: Scope, depth: int) -> Any:
        """A set holds each item once and in no order a ``+=`` or a ``for`` may rely on: it is a
        list of its distinct items that says it is not whole."""
        items, complete = self._items(node.elts, scope, depth)
        result = FoldedList(item for index, item in enumerate(items) if item not in items[:index])
        result.complete = False
        return result

    def _fold_Dict(self, node: ast.Dict, scope: Scope, depth: int) -> Any:
        result = FoldedDict()
        complete = True
        for key_node, value_node in list(zip(node.keys, node.values, strict=True))[:MAX_ITEMS]:
            if key_node is None:  # **spread
                spread = self.fold(value_node, scope, depth)
                if isinstance(spread, dict):
                    result.update(spread)
                    complete = complete and getattr(spread, "complete", True)
                else:
                    complete = False
                continue
            key = self.fold(key_node, scope, depth)
            if isinstance(key, (str, int)) and not isinstance(key, bool):
                result[key] = self.fold(value_node, scope, depth)
            else:
                complete = False
        result.complete = complete and len(node.keys) <= MAX_ITEMS
        return result

    def _fold_JoinedStr(self, node: ast.JoinedStr, scope: Scope, depth: int) -> Any:
        if len(node.values) > MAX_ITEMS:
            return Unresolved("an f-string with too many parts", snippet(node, 60))
        parts: List[str] = []
        size = 0
        unresolved: Optional[str] = None
        for part in node.values:
            if isinstance(part, ast.Constant):
                piece = str(part.value)
            elif not isinstance(part, ast.FormattedValue):
                return Unresolved("an f-string part", snippet(node))
            else:
                simple = part.format_spec is None and part.conversion in (-1, ord("s"))
                value = self.fold(part.value, scope, depth) if simple else None
                if simple and not isinstance(value, Unresolved) and is_scalar(value):
                    piece = str(value)
                elif simple and self._holes is not None:
                    piece = self._hole(snippet(part.value, 40))
                else:
                    if not simple:
                        why = "an f-string field with a format spec or conversion"
                    elif isinstance(value, Unresolved):
                        why = value.why
                    else:
                        why = "a value that is not text"
                    unresolved = unresolved or why
                    piece = "{" + snippet(part.value, 40) + "}"
            size += len(piece)
            if size > self._limit:
                return Unresolved(f"it is longer than {self._limit} characters", "")
            parts.append(piece)
        text = "".join(parts)
        if unresolved is not None:
            return Unresolved(unresolved, text)
        return text

    def _fold_BinOp(self, node: ast.BinOp, scope: Scope, depth: int) -> Any:
        if isinstance(node.op, ast.Add):
            left = self.fold(node.left, scope, depth)
            right = self.fold(node.right, scope, depth)
            if self._holes is not None and (isinstance(left, str) or isinstance(right, str)):
                if isinstance(left, Unresolved):
                    left = self._hole(left.text or snippet(node.left, 40))
                if isinstance(right, Unresolved):
                    right = self._hole(right.text or snippet(node.right, 40))
            if isinstance(left, str) and isinstance(right, str):
                return self._too_long(left + right) or left + right
            if isinstance(left, list) and isinstance(right, list):
                if len(left) + len(right) > MAX_ITEMS:
                    return Unresolved("a list with too many items", snippet(node, 60))
                joined = FoldedList([*left, *right])
                joined.complete = getattr(left, "complete", True) and getattr(
                    right, "complete", True
                )
                return joined
            why = next((v.why for v in (left, right) if isinstance(v, Unresolved)), "not text")
            return Unresolved(why, snippet(node))
        if isinstance(node.op, ast.Mod):
            left = self.fold(node.left, scope, depth)
            if not isinstance(left, str):
                return Unresolved(
                    left.why if isinstance(left, Unresolved) else "not a format string",
                    snippet(node),
                )
            right = node.right
            if isinstance(right, ast.Tuple):
                positional: List[ast.expr] = list(right.elts)
            else:
                positional = [right]
            keyed = {
                key.value: value
                for key, value in zip(
                    right.keys if isinstance(right, ast.Dict) else [],
                    right.values if isinstance(right, ast.Dict) else [],
                    strict=True,
                )
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if (over := self._spend(len(left))) is not None:  # it reads the template char by char
                return over
            return percent_template(
                left,
                self.fold(right, scope, depth),
                self._hole_for(positional, keyed),
                self._limit,
            )
        return Unresolved("an arithmetic expression", snippet(node))

    def _fold_IfExp(self, node: ast.IfExp, scope: Scope, depth: int) -> Any:
        first = self.fold(node.body, scope, depth)
        second = self.fold(node.orelse, scope, depth)
        if not isinstance(first, Unresolved) and first == second:
            return first
        return Unresolved("a conditional expression", snippet(node))

    # names and attributes

    def _fold_Name(self, node: ast.Name, scope: Scope, depth: int) -> Any:
        shadow = self._shadowed(node)
        if shadow is not None:
            return Unresolved(shadow, snippet(node))
        return self.name_value(node.id, scope, depth, node)

    def _shadowed(self, node: ast.Name) -> Optional[str]:
        """Why ``node`` is not the module's or the function's name of that spelling: a
        comprehension or a lambda around it binds the name itself (the first iterable of a
        comprehension is evaluated outside it, and is left alone)."""
        parents = self.module.parents
        child: ast.AST = node
        current = parents.get(node)
        from_first_iter = False
        while current is not None:
            if isinstance(current, ast.comprehension):
                from_first_iter = child is current.iter
            elif isinstance(current, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                skip_first = from_first_iter and child is current.generators[0]
                for index, generator in enumerate(current.generators):
                    if skip_first and index == 0:
                        continue
                    if node.id in _target_names(generator.target):
                        return f"`{node.id}` is a comprehension variable"
                from_first_iter = False
            elif isinstance(current, ast.Lambda):
                args = current.args
                names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
                names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
                if node.id in names:
                    return f"`{node.id}` is a parameter of a lambda"
            elif isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                break
            child = current
            current = parents.get(current)
        return None

    def _loop_item(self, name: str, scope: Scope) -> Any:
        """What the loop being unrolled gives ``name`` in ``scope``, else :data:`ABSENT`: the
        item, when the name is the loop's own variable (the scope that binds it is the loop's)."""
        if self._env:
            binder = self._binding_scope(name, scope)
            for owner, frame in reversed(self._env):
                if name in frame and owner is binder:
                    return frame[name]
        return ABSENT

    def name_value(self, name: str, scope: Scope, depth: int, origin: ast.AST) -> Any:
        item = self._loop_item(name, scope)
        if item is not ABSENT:
            return item
        current: Optional[Scope] = scope
        while current is not None:
            if name in current.recipes:
                return self._recipe(name, current, scope, depth, origin)
            if name in current.values:
                why = self._rebound.get(name) or self._rebound.get("*")
                if why:
                    return Unresolved(why, snippet(origin))
                return self.fold(current.values[name], current, depth)
            if name in current.blocked:
                why = current.blocked[name]
                if why == "imported":
                    return self._imported(name, origin, depth)
                return Unresolved(why, snippet(origin))
            current = current.parent
        if name in self.module.imports:
            return self._imported(name, origin, depth)
        return Unresolved(f"`{name}` is not defined in this file", snippet(origin))

    def _recipe(self, name: str, owner: Scope, use: Scope, depth: int, origin: ast.AST) -> Any:
        """What a name built by ``+=`` statements holds once they are all done (see
        :class:`Recipe`). Read on a line of its own scope before the last of them, it is not whole
        yet; a name the file changes in place elsewhere is not trusted."""
        recipe = owner.recipes[name]
        why = self.rebound(name)
        if why:
            return Unresolved(why, snippet(origin))
        line = getattr(origin, "lineno", 0)
        if owner is use and origin in self.module.parents and line <= recipe.ready:
            return Unresolved(
                f"`{name}` is still being added to here (up to line {recipe.ready})",
                snippet(origin),
            )
        plain = self._holes is None and self._limit == MAX_STRING and not self._env
        if plain and id(recipe) in self._recipe_values:
            return self._recipe_values[id(recipe)]
        value = self._built(name, recipe, owner, depth)
        if plain and not (isinstance(value, Unresolved) and value.transient):
            self._recipe_values[id(recipe)] = value
        return value

    def _built(self, name: str, recipe: Recipe, scope: Scope, depth: int) -> Any:
        base = self.fold(recipe.base, scope, depth)
        if isinstance(base, Unresolved):
            return base
        if isinstance(base, FoldedTuple) or not isinstance(base, (list, str)):
            return Unresolved(f"`{name}` is not a list or a string that `+=` adds to", "")
        acc = _Acc(
            value=list(base) if isinstance(base, list) else base,
            complete=getattr(base, "complete", True),
            budget=MAX_RECIPE_STEPS,
        )
        done = self._apply(recipe.steps, acc, scope, depth)
        if isinstance(done, Unresolved):
            return done
        if isinstance(acc.value, str):
            if not acc.complete:
                return Unresolved(f"part of `{name}` is added by a value that does not fold", "")
            return self._too_long(acc.value) or acc.value
        built = FoldedList(acc.value)
        built.complete = acc.complete
        return built

    def _apply(
        self, steps: Sequence[Union[_Add, _Loop]], acc: "_Acc", scope: Scope, depth: int
    ) -> Optional[Unresolved]:
        """Go through ``steps``, adding to ``acc``. Whatever does not fold leaves the result
        incomplete; the answer is an :class:`Unresolved` only when the work ran out."""
        for step in steps:
            acc.budget -= 1
            if acc.budget < 0:
                self.exhausted = True
                acc.complete = False
                return Unresolved("it takes too much work to fold", "", transient=True)
            if isinstance(step, _Add):
                piece = self.fold(step.value, scope, depth)
                if isinstance(acc.value, str):
                    if isinstance(piece, str):
                        acc.value += piece
                        if (over := self._spend(len(acc.value))) is not None:
                            return over
                    else:
                        acc.complete = False
                elif isinstance(piece, (list, tuple)):
                    acc.value.extend(piece)
                    acc.complete = acc.complete and getattr(piece, "complete", True)
                else:
                    acc.complete = False
                if isinstance(acc.value, list) and len(acc.value) > MAX_ITEMS:
                    acc.complete = False
                    del acc.value[MAX_ITEMS:]
                continue
            items = self._loop_items(step.loop, scope, depth)
            if items is None or any(self.rebound(n) for n in _target_names(step.loop.target)):
                acc.complete = False
                continue
            if len(items) > MAX_LOOP_ITERATIONS:
                acc.complete = False
            for item in items[:MAX_LOOP_ITERATIONS]:
                frame = _bind(step.loop.target, item)
                if frame is None:
                    acc.complete = False
                    break
                self._env.append((scope, frame))
                try:
                    done = self._apply(step.body, acc, scope, depth)
                finally:
                    self._env.pop()
                if done is not None:
                    return done
        return None

    def _loop_items(self, loop: ast.For, scope: Scope, depth: int) -> Optional[List[Any]]:
        """The items a ``for`` goes through when they fold: the keys, values or pairs of a dict, or
        the items of a list or tuple; ``None`` when they are not all known."""
        node: ast.AST = loop.iter
        method = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"items", "keys", "values"}
            and not node.args
            and not node.keywords
        ):
            method, node = node.func.attr, node.func.value
        value = self.fold(node, scope, depth)
        if not getattr(value, "complete", True):
            return None
        if isinstance(value, dict):
            if method == "items":
                return list(value.items())
            return list(value.values() if method == "values" else value.keys())
        if method is None and isinstance(value, list):
            return list(value)
        return None

    def _imported(self, name: str, origin: ast.AST, depth: int) -> Any:
        qualified = self.module.imports.get(name)
        if qualified is None or self.index is None:
            return Unresolved(
                f"`{name}` is imported from outside the scanned files", snippet(origin)
            )
        return self._foreign(qualified, origin, depth)

    def _foreign(self, qualified: str, origin: ast.AST, depth: int) -> Any:
        """A constant of another module of the tree."""
        assert self.index is not None
        found = self.index.resolve(qualified, self.module.index_rel or self.module.rel)
        if isinstance(found, str):
            return Unresolved(found, snippet(origin))
        module, chain = found
        other = self.index.folder(module)
        previous, other._limit = other._limit, self._limit  # SQL in another file is SQL too
        try:
            return other.attribute_chain(chain, depth, origin)
        finally:
            other._limit = previous

    def attribute_chain(self, chain: Sequence[str], depth: int, origin: ast.AST) -> Any:
        """Fold ``chain[0]`` in this module, then (when more is named) a class attribute."""
        if not chain or depth > MAX_FOLD_DEPTH:
            return Unresolved("it is nested too deeply", snippet(origin))
        scope = self._module_scope
        if len(chain) == 1:
            return self.name_value(chain[0], scope, depth + 1, origin)
        why = self._rebound.get(chain[0]) or self._rebound.get("*")
        if why:
            return Unresolved(why, snippet(origin))
        cls = scope.classes.get(chain[0])
        if cls is None or len(chain) > 2:
            return Unresolved(f"`{'.'.join(chain)}` is not a constant", snippet(origin))
        class_scope = self._scope(cls, scope)
        # Only what the class itself binds: a module-level name of the same spelling is not
        # an attribute of it (its own body still sees the module when it is folded).
        if chain[1] in class_scope.values:
            return self.fold(class_scope.values[chain[1]], class_scope, depth + 1)
        if chain[1] in class_scope.blocked:
            return Unresolved(class_scope.blocked[chain[1]], snippet(origin))
        return Unresolved(f"`{chain[1]}` is not a constant of the class", snippet(origin))

    def _binding_scope(self, name: str, scope: Optional[Scope]) -> Optional[Scope]:
        """The nearest scope that binds ``name``, or ``None``."""
        while scope is not None:
            if (
                name in scope.values
                or name in scope.blocked
                or name in scope.classes
                or name in scope.recipes
            ):
                return scope
            scope = scope.parent
        return None

    def _is_import(self, func: ast.AST, scope: Scope) -> bool:
        """Whether the name a call starts from (``Variable`` in ``Variable.get(...)``) is the one
        the file imports: not a parameter, a class, a function or an assignment of its own."""
        head = func
        while isinstance(head, ast.Attribute):
            head = head.value
        if not isinstance(head, ast.Name):
            return False
        if self.rebound(head.id) or "*" in self.module.ambiguous:
            return False  # ``global``, ``:=``, a match capture, a star import
        places = self.module.ambiguous.get(head.id)
        if places is not None:  # imported from two places: both must be the one that is meant
            last = self.module.imports.get(head.id, head.id)
            qualified = qualified_name(func, self.module.imports) or ""
            rest = qualified[len(last) :] if qualified.startswith(last) else ""
            if not all(place + rest in _READ_THROUGH_IMPORT for place in places):
                return False
        bound = self._binding_scope(head.id, scope)
        if bound is None:
            return head.id in self.module.imports
        return bound.blocked.get(head.id) == "imported"

    def _fold_Attribute(self, node: ast.Attribute, scope: Scope, depth: int) -> Any:
        head: ast.AST = node
        while isinstance(head, ast.Attribute):
            head = head.value
        if not isinstance(head, ast.Name):
            return Unresolved("an attribute of a value that is not a constant", snippet(node))
        name = head.id
        bound = self._binding_scope(name, scope)
        if bound is not None and name in bound.classes:
            chain = (qualified_name(node, {}) or "").split(".")
            if len(chain) == 2:
                return self.attribute_chain(chain, depth, node)
        elif (bound is None and name in self.module.imports) or (
            bound is not None and bound.blocked.get(name) == "imported"
        ):
            qualified = qualified_name(node, self.module.imports)
            if qualified == "os.environ":
                return Unresolved("the environment", snippet(node))
            if self.index is not None and qualified is not None:
                return self._foreign(qualified, node, depth)
        return Unresolved("an attribute of a value that is not a constant", snippet(node))

    def _fold_Subscript(self, node: ast.Subscript, scope: Scope, depth: int) -> Any:
        qualified = qualified_name(node.value, self.module.imports)
        if qualified == "os.environ":
            return self._environment(node.slice, scope, depth, node)
        if qualified == "sys.argv":
            return Unresolved("a command-line argument", snippet(node))
        container = self.fold(node.value, scope, depth)
        key = self.fold(node.slice, scope, depth)
        if isinstance(container, dict) and isinstance(key, (str, int)):
            found = lookup(container, key)
            if found is ABSENT:
                return Unresolved("the key is not in the dict", snippet(node))
            return found
        if isinstance(container, (list, tuple)) and isinstance(key, int):
            if -len(container) <= key < len(container):
                return container[key]
        why = next(
            (v.why for v in (container, key) if isinstance(v, Unresolved)),
            "a lookup that is not a constant",
        )
        return Unresolved(why, snippet(node))

    # calls

    def _environment(self, name_node: ast.AST, scope: Scope, depth: int, origin: ast.AST) -> Any:
        key = self.fold(name_node, scope, depth)
        if not isinstance(key, str):
            return Unresolved("an environment variable", snippet(origin))
        if key in self.variables:
            return self.variables[key]
        return Unresolved(f"the environment variable {key}", snippet(origin))

    def _airflow_variable(self, node: ast.Call, scope: Scope, depth: int) -> Any:
        """``Variable.get("name", ...)`` from the operator's ``airflow_variables``, else
        unresolved: the default (``default_var`` in Airflow 2, ``default`` in Airflow 3, or the
        second argument) is not assumed, and a Variable read as JSON is not read."""
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            keyword.arg is None for keyword in node.keywords
        ):
            return Unresolved(
                "an Airflow Variable read with *arguments or **arguments", snippet(node)
            )
        name_node = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "key":
                name_node = keyword.value
        if name_node is None:
            return Unresolved("an Airflow Variable with no name", snippet(node))
        name = self.fold(name_node, scope, depth)
        if not isinstance(name, str):
            return Unresolved("an Airflow Variable whose name is not a constant", snippet(node))
        as_json = [kw.value for kw in node.keywords if kw.arg == "deserialize_json"]
        if len(node.args) >= 3:
            as_json.append(node.args[2])
        if any(not (isinstance(v, ast.Constant) and v.value is False) for v in as_json):
            return Unresolved(f"the Airflow Variable {name} is read as JSON", snippet(node))
        if name in self.airflow_variables:
            return self.airflow_variables[name]
        return Unresolved(f"the Airflow Variable {name}", snippet(node))

    def _fold_Call(self, node: ast.Call, scope: Scope, depth: int) -> Any:
        func = node.func
        qualified = qualified_name(func, self.module.imports)
        if qualified in _ENV_GET and node.args and self._is_import(func, scope):
            return self._environment(node.args[0], scope, depth, node)
        if qualified in _VARIABLE_GET and self._is_import(func, scope):
            return self._airflow_variable(node, scope, depth)
        if qualified in _JOINS and node.args and not node.keywords:
            parts = [self.fold(arg, scope, depth) for arg in node.args]
            if all(isinstance(p, str) for p in parts):
                joined = posixpath.join(*parts)
                return self._too_long(joined) or joined
            why = next((p.why for p in parts if isinstance(p, Unresolved)), "not text")
            return Unresolved(why, snippet(node))
        if qualified == "str" and len(node.args) == 1 and not node.keywords:
            value = self.fold(node.args[0], scope, depth)
            if is_scalar(value):
                return str(value)
            return Unresolved(
                value.why if isinstance(value, Unresolved) else "not text", snippet(node)
            )
        if isinstance(func, ast.Attribute):
            receiver = self.fold(func.value, scope, depth)
            if isinstance(receiver, str):
                return self._str_method(receiver, func.attr, node, scope, depth)
        return Unresolved(f"the result of a call to {snippet(func, 40)}()", snippet(node))

    def _str_method(
        self, receiver: str, method: str, node: ast.Call, scope: Scope, depth: int
    ) -> Any:
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            return Unresolved("a call with *arguments", snippet(node))
        args = [self.fold(arg, scope, depth) for arg in node.args]
        if method == "format":
            kwargs: Dict[str, Any] = {}
            for keyword in node.keywords:
                if keyword.arg is None:
                    return Unresolved("a format call with **arguments", snippet(node))
                kwargs[keyword.arg] = self.fold(keyword.value, scope, depth)
            keyed_nodes = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if (
                over := self._spend(len(receiver))
            ) is not None:  # it reads the template char by char
                return over
            return format_template(
                receiver, args, kwargs, self._hole_for(list(node.args), keyed_nodes), self._limit
            )
        if node.keywords:
            return Unresolved(f"a call to str.{method} with keywords", snippet(node))
        if method == "join" and len(args) == 1 and isinstance(args[0], (list, tuple)):
            if all(isinstance(item, str) for item in args[0]) and getattr(
                args[0], "complete", True
            ):
                if sum(len(item) for item in args[0]) + len(receiver) * len(args[0]) > self._limit:
                    return Unresolved(f"it is longer than {self._limit} characters", "")
                joined = receiver.join(args[0])
                return self._too_long(joined) or joined
            return Unresolved("a join of values that are not all text", snippet(node))
        if method in _STR_METHODS and all(isinstance(arg, str) for arg in args):
            if method in {"lower", "upper"} and not args:
                changed = getattr(receiver, method)()  # a case change can lengthen a string
                return self._too_long(changed) or changed
            if method in {"strip", "lstrip", "rstrip"} and len(args) <= 1:
                return getattr(receiver, method)(*args)
            if method == "replace" and len(args) == 2:
                old, new = args
                times = receiver.count(old) if old else len(receiver) + 1
                if len(receiver) + times * (len(new) - len(old)) > self._limit:
                    return Unresolved(f"it is longer than {self._limit} characters", "")
                replaced = receiver.replace(old, new)
                return self._too_long(replaced) or replaced
            if method in {"removeprefix", "removesuffix"} and len(args) == 1:
                return getattr(receiver, method)(args[0])
        return Unresolved(f"a call to str.{method}", snippet(node))


def unresolved_why(value: Any) -> str:
    """Why ``value`` is unresolved, for a report; empty when it is not."""
    return value.why if isinstance(value, Unresolved) else ""
