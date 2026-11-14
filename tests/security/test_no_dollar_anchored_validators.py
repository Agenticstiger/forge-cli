# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Static gate: no whole-string validator in ``fluid_build/`` ends in ``$`` under ``.match``.

Python's ``$`` matches at the end of the string and also just before a final
``\\n``. So ``re.match(r"^[a-z]+$", "orders\\n")`` succeeds, and the caller goes on
to use ``"orders\\n"``. Contract YAML produces such values easily (a ``|`` block
scalar ends in one), and so do ``.env`` files and ``%0A`` in a URL. #788 and #797
fixed the known validators with ``\\Z`` or ``fullmatch``. This test keeps the
class from coming back.

**The scan** walks the AST and imports nothing from ``fluid_build/``. It finds
every ``re.match`` / ``re.search`` call, and every ``.match`` / ``.search`` call on a
compiled pattern or reference to one (``filter(RX.match, names)``). It follows the
pattern back to where it was written: a literal, a module or class constant, a
dict, tuple or comprehension of patterns, an import from another module, a
helper's return value, a property. A call is reported when its pattern

* has an alternative anchored at the start (``^`` or ``\\A``; ``.match`` anchors
  there anyway) that can end at an unescaped ``$``. It is not reported under
  ``MULTILINE``, or when the ``$`` follows a repeat that consumes a newline anyway
  (``\\s*$``, ``[^/]*$``). The pattern is read with the parser ``re`` itself uses,
  so groups, alternatives, escapes and ``VERBOSE`` read the way ``re`` reads them; or
* cannot be read: it is compiled at run time from data (a schema ``pattern``), or
  it is a ``re.Pattern`` parameter, so the caller chooses it.

A call that uses ``fullmatch``, ``sub``, ``findall`` or ``split`` is not reported,
since ``$`` cannot leave a newline unchecked there.

**Fix a report** with ``\\Z`` or ``fullmatch``. Where the ``$`` is deliberate, add
the call to ``_ACCEPTED`` with the reason. The key is (file, function, pattern),
so a new function that reuses a listed pattern is reported again. An entry that no
longer matches a call fails ``test_every_accepted_entry_still_matches_a_call``,
so the list can only shrink.

**Prior art.** No ruff, pylint, bandit or dlint rule covers this
(astral-sh/ruff#15017 proposes one). CodeQL's ``py/regex/unmatchable-dollar`` is
a different bug. The Semgrep registry rule ``check-validation-regex``, ported from
Brakeman's ``CheckValidationRegex``, covers Ruby only, and Semgrep CE cannot
follow a pattern across files or through a helper's return value. This test
borrows Brakeman's rule (a validator anchors at both ends of the string) and the
shape of this repo's other AST gates: ``tests/test_no_unencoded_text_io.py``,
``tests/test_no_reserved_tld_defaults.py`` and
``tests/fragments/test_no_raw_readers.py``.

**Not covered.** An unanchored ``.search(r"\\.ya?ml$")`` is a suffix check, not a
whole-string validator, so it is not reported. The scan does not follow a pattern
into a registry filled by ``.append`` or item assignment, into a rule object
(``Rule(name, re.compile(...))``), into an attribute only a subclass sets, or
through ``+=``, ``getattr``, ``functools.partial`` or a star import, and a ``.match``
on a parameter that is not annotated ``re.Pattern`` is not reported.
``fluid_build/`` holds no validator of these shapes today. A pattern built with
``%`` or ``str.format`` cannot be read, so it is reported as built at run time.
JSON-schema ``pattern`` keywords run inside jsonschema, out of this scan's sight:
``schema_manager.schema_validator`` reads their ``$`` as ECMA-262 does, and a
validation that does not go through it is a separate gap.
"""

from __future__ import annotations

import ast
import pathlib
import re
import textwrap
import warnings
from collections import Counter
from dataclasses import dataclass, field
from typing import (
    Callable,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import pytest

try:  # Python 3.11+: the parser ``re`` itself uses
    _sre = re._constants
    _sre_parse = re._parser
except AttributeError:  # Python 3.10, where ``re`` is one module
    import sre_constants as _sre  # type: ignore[no-redef]
    import sre_parse as _sre_parse  # type: ignore[no-redef]

pytestmark = [pytest.mark.unit]

_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "fluid_build"

#: The calls that leave a final newline unchecked when the pattern ends in ``$``.
_MATCHERS = frozenset({"match", "search"})

#: The modules whose ``match`` / ``search`` / ``compile`` the scan reads.
_REGEX_MODULES = frozenset({"re", "regex"})

#: At most this many values are kept per expression, so a long ``+`` chain stays cheap.
_MAX_VALUES = 32

#: At most this much of a string's start and of its end is kept.
_MAX_TEXT = 4096

#: Calls that hand back what they are given: ``tuple([...])``, ``MappingProxyType({...})``.
_PASS_THROUGH = frozenset(
    {"dict", "frozenset", "list", "MappingProxyType", "reversed", "set", "sorted", "tuple"}
)

_NEWLINE = ord("\n")
#: ``\s``, ``\D`` and ``\W`` contain ``\n``.
_CLASSES_WITH_NEWLINE = frozenset(
    {_sre.CATEGORY_SPACE, _sre.CATEGORY_NOT_DIGIT, _sre.CATEGORY_NOT_WORD}
)
_REPEATS = frozenset(
    {_sre.MAX_REPEAT, _sre.MIN_REPEAT, getattr(_sre, "POSSESSIVE_REPEAT", _sre.MAX_REPEAT)}
)
_ATOMIC_GROUP = getattr(_sre, "ATOMIC_GROUP", None)


# ── values ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Str:
    """A string as far as the scan can read it: a known prefix and suffix, or all of it.

    ``head`` / ``tail`` are ``None`` when nothing is known of that end."""

    head: Optional[str]
    tail: Optional[str]
    whole: bool
    origin: str = field(compare=False)

    @classmethod
    def of(cls, text: str, origin: str) -> "_Str":
        return cls(text, text, True, origin)

    def show(self) -> str:
        if self.whole:
            return self.head or ""
        return f"{self.head or ''}…{self.tail or ''}"


@dataclass(frozen=True)
class _Pattern:
    """A regex: the argument of ``re.compile``, or the pattern given to ``re.match``."""

    source: Optional[_Str]  # None: built at run time from a value the scan cannot read
    flags: Optional[int]  # None: the flags argument cannot be read
    what: str = field(compare=False)  # source text of the pattern argument
    origin: str = field(compare=False)


@dataclass(frozen=True)
class _Passed:
    """A parameter annotated ``re.Pattern``: the caller chooses the pattern."""

    name: str
    origin: str = field(compare=False)


@dataclass(frozen=True)
class _Seq:
    """A tuple, list or set display, one value set per position."""

    items: Tuple[Tuple["_Value", ...], ...]


@dataclass(frozen=True)
class _Map:
    """A dict display. A key is ``None`` when it is not a string literal."""

    entries: Tuple[Tuple[Optional[str], Tuple["_Value", ...]], ...]


@dataclass(frozen=True, eq=False)
class _ModuleRef:
    name: str


@dataclass(frozen=True, eq=False)
class _Function:
    node: Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda]
    scope: "_Scope"  # the function's own scope


@dataclass(frozen=True, eq=False)
class _Class:
    scope: "_Scope"  # the class body


@dataclass(frozen=True, eq=False)
class _Instance:
    scope: "_Scope"  # the class body


_Value = Union[_Str, _Pattern, _Passed, _Seq, _Map, _ModuleRef, _Function, _Class, _Instance]


class _Binding(NamedTuple):
    """One way a name gets its value, evaluated only when the name is read."""

    expr: Optional[ast.expr] = None
    scope: Optional["_Scope"] = None
    iterate: bool = False  # bound by a ``for``: the value is an element of ``expr``
    path: Tuple[int, ...] = ()  # the position inside a tuple target
    value: Optional[_Value] = None  # known when the binding is made (def, class)
    imported: Optional[Tuple[str, str]] = None  # (module, name) of a ``from`` import


class _Scope:
    """The names one module, class, function or comprehension binds."""

    def __init__(self, module: "_Module", kind: str, parent: Optional["_Scope"]) -> None:
        self.module = module
        self.kind = kind
        self.parent = parent  # the next scope a name is looked up in (classes are skipped)
        self.bindings: Dict[str, List[_Binding]] = {}
        self.params: Dict[str, Tuple[ast.arg, Optional[ast.expr], "_Scope"]] = {}
        self.globals: Set[str] = set()
        self.self_name: Optional[str] = None  # a method's first parameter
        self.cls: Optional["_Scope"] = None  # a method's class body
        self.attrs: Dict[str, List[_Binding]] = {}  # a class's ``self.X = ...`` assignments
        self.bases: List[Tuple[ast.expr, "_Scope"]] = []  # a class's base classes

    def bind(self, name: str, binding: _Binding) -> None:
        self.bindings.setdefault(name, []).append(binding)


class _Module:
    def __init__(self, name: str, path: pathlib.Path) -> None:
        self.name = name
        self.path = path
        #: Held for the whole scan: the scan keys nodes by ``id()``, which a freed node gives up.
        self.tree: Optional[ast.Module] = None
        self.scope = _Scope(self, "module", None)
        #: Every ``.match`` / ``.search`` call or reference: (node, scope, function name).
        self.calls: List[Tuple[ast.expr, _Scope, str]] = []
        #: id(node) -> the scope of a comprehension or lambda.
        self.scopes: Dict[int, _Scope] = {}


class Finding(NamedTuple):
    path: str  # relative to the checkout, e.g. ``fluid_build/cli/security.py``
    function: str  # the qualified name of the function that makes the call
    pattern: str  # the pattern text, or ``<run time: ...>`` / ``<parameter: ...>``
    line: int
    call: str
    origin: str  # where the pattern was written or compiled

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.path, self.function, self.pattern)

    def show(self) -> str:
        return (
            f"{self.path}:{self.line} in {self.function}: {self.call}\n"
            f"      pattern {self.pattern!r}, from {self.origin}"
        )


# ── reading a module ────────────────────────────────────────────────────


class _Builder(ast.NodeVisitor):
    """Record every binding and every ``.match`` / ``.search`` call of one module."""

    def __init__(self, module: _Module) -> None:
        self.module = module
        self.scope = module.scope
        self.names: List[str] = []
        self._called: Set[int] = set()  # id() of the ``x.match`` nodes that are called

    # scopes

    def _lookup_parent(self) -> _Scope:
        """The scope a nested function looks names up in: Python skips class bodies."""
        scope = self.scope
        while scope.kind == "class":
            assert scope.parent is not None
            scope = scope.parent
        return scope

    def _function(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda]) -> None:
        outer = self.scope
        for expr in getattr(node, "decorator_list", []):
            self.visit(expr)
        args = node.args
        for expr in args.defaults + [d for d in args.kw_defaults if d is not None]:
            self.visit(expr)
        inner = _Scope(self.module, "function", self._lookup_parent())
        positional = args.posonlyargs + args.args
        defaults: List[Optional[ast.expr]] = [None] * (len(positional) - len(args.defaults))
        defaults += list(args.defaults)
        for arg, default in zip(positional, defaults, strict=True):
            inner.params[arg.arg] = (arg, default, outer)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            inner.params[arg.arg] = (arg, default, outer)
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                inner.params[arg.arg] = (arg, None, outer)
        if outer.kind == "class" and positional and not _decorated(node, "staticmethod"):
            inner.self_name = positional[0].arg
            inner.cls = outer
        if isinstance(node, ast.Lambda):
            self.module.scopes[id(node)] = inner
        else:
            self._bind_name(node.name, _Binding(value=_Function(node, inner)))
        self.scope = inner
        self.names.append(getattr(node, "name", "<lambda>"))
        for stmt in node.body if isinstance(node.body, list) else [node.body]:
            self.visit(stmt)
        self.names.pop()
        self.scope = outer

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in node.decorator_list + node.bases + [kw.value for kw in node.keywords]:
            self.visit(expr)
        outer = self.scope
        body = _Scope(self.module, "class", self._lookup_parent())
        body.bases = [(base, outer) for base in node.bases]
        self._bind_name(node.name, _Binding(value=_Class(body)))
        self.scope = body
        self.names.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.names.pop()
        self.scope = outer

    def _comprehension(self, node: ast.AST, generators: List[ast.comprehension]) -> None:
        outer = self.scope
        inner = _Scope(self.module, "function", self._lookup_parent())
        self.module.scopes[id(node)] = inner
        for index, gen in enumerate(generators):
            # The first iterable is evaluated outside the comprehension.
            self.scope = outer if index == 0 else inner
            self.visit(gen.iter)
            self.scope = inner
            self._bind_target(gen.target, gen.iter, outer if index == 0 else inner, True, ())
            for cond in gen.ifs:
                self.visit(cond)
        for child in ("elt", "key", "value"):
            expr = getattr(node, child, None)
            if expr is not None:
                self.visit(expr)
        self.scope = outer

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node, node.generators)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node, node.generators)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node, node.generators)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node, node.generators)

    # bindings

    def _bind_name(self, name: str, binding: _Binding) -> None:
        if name in self.scope.globals:
            self.module.scope.bind(name, binding)
        else:
            self.scope.bind(name, binding)

    def _bind_target(
        self,
        target: ast.expr,
        expr: ast.expr,
        scope: _Scope,
        iterate: bool,
        path: Tuple[int, ...],
    ) -> None:
        if isinstance(target, ast.Name):
            self._bind_name(target.id, _Binding(expr, scope, iterate, path))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for index, elt in enumerate(target.elts):
                if not isinstance(elt, ast.Starred):
                    self._bind_target(elt, expr, scope, iterate, path + (index,))
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == self.scope.self_name
            and self.scope.cls is not None
        ):
            binding = _Binding(expr, scope, iterate, path)
            self.scope.cls.attrs.setdefault(target.attr, []).append(binding)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_target(target, node.value, self.scope, False, ())
            self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self._bind_target(node.target, node.value, self.scope, False, ())

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind_target(node.target, node.value, self.scope, False, ())

    def visit_For(self, node: Union[ast.For, ast.AsyncFor]) -> None:
        self.visit(node.iter)
        self._bind_target(node.target, node.iter, self.scope, True, ())
        for stmt in node.body + node.orelse:
            self.visit(stmt)

    visit_AsyncFor = visit_For

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.globals.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self._bind_name(alias.asname, _Binding(value=_ModuleRef(alias.name)))
            else:
                top = alias.name.split(".")[0]
                self._bind_name(top, _Binding(value=_ModuleRef(top)))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            package = self.module.name.split(".")
            if self.module.path.name != "__init__.py":
                package = package[:-1]
            package = package[: len(package) - (node.level - 1)]
            base = ".".join(package + ([base] if base else []))
        for alias in node.names:
            if alias.name != "*":
                binding = _Binding(imported=(base, alias.name))
                self._bind_name(alias.asname or alias.name, binding)

    # calls

    def _record(self, node: ast.expr) -> None:
        self.module.calls.append((node, self.scope, ".".join(self.names) or "<module>"))

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in _MATCHERS:
            self._record(node)
            self._called.add(id(node.func))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        loaded = isinstance(node.ctx, ast.Load)
        if node.attr in _MATCHERS and loaded and id(node) not in self._called:
            self._record(node)  # a reference, such as ``filter(RX.match, names)``
        self.generic_visit(node)


# ── following a value back ──────────────────────────────────────────────


def _dedupe(values: Sequence[_Value]) -> List[_Value]:
    out: List[_Value] = []
    for value in values:
        if value not in out:
            out.append(value)
        if len(out) >= _MAX_VALUES:
            break
    return out


def _concat(left: _Str, right: _Str, origin: str) -> _Str:
    """``left + right``, keeping what is known of the start and of the end."""
    if left.whole and right.whole:
        text = f"{left.head}{right.head}"
        if len(text) <= _MAX_TEXT:
            return _Str.of(text, origin)
        return _Str(text[:_MAX_TEXT], text[-_MAX_TEXT:], False, origin)
    head, tail = left.head, right.tail
    if left.whole:
        head = f"{left.head}{right.head}" if right.head is not None else left.head or None
    if right.whole:
        tail = f"{left.tail}{right.tail}" if left.tail is not None else right.tail or None
    return _Str(
        head[:_MAX_TEXT] if head is not None else None,
        tail[-_MAX_TEXT:] if tail is not None else None,
        False,
        origin,
    )


def _is_regex_module(values: Sequence[_Value]) -> bool:
    return any(isinstance(v, _ModuleRef) and v.name in _REGEX_MODULES for v in values)


def _decorated(node: ast.AST, *names: str) -> bool:
    """Whether ``node`` carries a decorator called one of ``names`` (``functools.x`` too)."""
    for decorator in getattr(node, "decorator_list", []):
        if isinstance(decorator, ast.Call):
            decorator = decorator.func
        if getattr(decorator, "id", None) in names or getattr(decorator, "attr", None) in names:
            return True
    return False


def _keyword(call: ast.Call, name: str) -> Optional[ast.expr]:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _own_returns(node: ast.AST) -> Iterator[ast.Return]:
    """The ``return`` statements of a function, not of the functions nested in it."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, ast.Return) and child.value is not None:
            yield child
        elif not isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            stack.extend(ast.iter_child_nodes(child))


class _Scan:
    def __init__(self, package: pathlib.Path) -> None:
        self.package = package
        self.checkout = package.parent
        self._modules: Dict[str, Optional[_Module]] = {}
        self._active: Set[Tuple[int, str]] = set()
        self._memo: Dict[Tuple[int, str], List[_Value]] = {}
        self._cut = False  # a cycle guard cut the evaluation in progress short
        self.calls_seen = 0
        self.unreadable: List[str] = []

    # modules

    def _rel(self, path: pathlib.Path) -> str:
        return path.relative_to(self.checkout).as_posix()

    def _origin(self, scope: _Scope, node: ast.AST) -> str:
        return f"{self._rel(scope.module.path)}:{getattr(node, 'lineno', 0)}"

    def _path_of(self, name: str) -> Optional[pathlib.Path]:
        if name != self.package.name and not name.startswith(self.package.name + "."):
            return None
        rel = pathlib.Path(*name.split("."))
        for candidate in (
            self.checkout / rel.with_suffix(".py"),
            self.checkout / rel / "__init__.py",
        ):
            if candidate.is_file():
                return candidate
        return None

    def module(self, name: str) -> Optional[_Module]:
        if name not in self._modules:
            path = self._path_of(name)
            self._modules[name] = self._load(name, path) if path else None
        return self._modules[name]

    def _load(self, name: str, path: pathlib.Path) -> Optional[_Module]:
        module = _Module(name, path)
        try:
            with warnings.catch_warnings():
                # An invalid escape sequence is the linter's business, not this gate's.
                warnings.simplefilter("ignore")
                module.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            _Builder(module).visit(module.tree)
        except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            self.unreadable.append(f"{self._rel(path)}: {type(exc).__name__}: {exc}")
            return None
        return module

    # names

    def _guarded(self, key: Tuple[int, str], compute: Callable[[], List[_Value]]) -> List[_Value]:
        """``compute()`` once per key. A key met again while it is being computed reads as
        nothing, and a result cut short that way is not kept. Callers get a copy."""
        if key in self._memo:
            return list(self._memo[key])
        if key in self._active:
            self._cut = True
            return []
        outer_cut, self._cut = self._cut, False
        self._active.add(key)
        try:
            result = compute()
            if not self._cut:
                self._memo[key] = result
            return list(result)
        finally:
            self._active.discard(key)
            self._cut = outer_cut or self._cut

    def _name(self, name: str, scope: _Scope) -> List[_Value]:
        current: Optional[_Scope] = scope
        while current is not None:
            if name in current.params or name in current.bindings:
                return self._bound(current, name)
            current = current.parent
        return []

    def _bound(self, scope: _Scope, name: str) -> List[_Value]:
        return self._guarded((id(scope), name), lambda: self._bound_now(scope, name))

    def _bound_now(self, scope: _Scope, name: str) -> List[_Value]:
        out: List[_Value] = []
        if name in scope.params:
            arg, default, outer = scope.params[name]
            if name == scope.self_name and scope.cls is not None:
                return [_Instance(scope.cls)]
            if arg.annotation is not None and self._names_pattern(arg.annotation, outer):
                out.append(_Passed(name, self._origin(scope, arg)))
            out += self.values(default, outer)
        for binding in scope.bindings.get(name, []):
            out += self._evaluate(binding)
        return _dedupe(out)

    def _names_pattern(self, annotation: ast.expr, scope: _Scope) -> bool:
        """Whether an annotation names ``re.Pattern`` / ``typing.Pattern``, or an alias of it."""
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return False
        for node in ast.walk(annotation):
            if isinstance(node, ast.Attribute) and node.attr == "Pattern":
                return True
            if isinstance(node, ast.Name) and (
                node.id == "Pattern" or self._imports_pattern(node.id, scope)
            ):
                return True
        return False

    @staticmethod
    def _imports_pattern(name: str, scope: _Scope) -> bool:
        current: Optional[_Scope] = scope
        while current is not None and name not in current.bindings:
            current = current.parent
        bindings = current.bindings[name] if current is not None else []
        return any(b.imported is not None and b.imported[1] == "Pattern" for b in bindings)

    def _evaluate(self, binding: _Binding) -> List[_Value]:
        if binding.value is not None:
            return [binding.value]
        if binding.imported is not None:
            return self._module_attr(*binding.imported)
        values = self.values(binding.expr, binding.scope)
        if binding.iterate:
            values = self._elements(values)
        for index in binding.path:
            values = [
                item
                for value in values
                if isinstance(value, _Seq) and index < len(value.items)
                for item in value.items[index]
            ]
        return values

    def _module_attr(self, module_name: str, attr: str) -> List[_Value]:
        """``module.attr``: what the module binds, or else its submodule of that name."""
        module = self.module(module_name)
        out: List[_Value] = []
        if module is not None and attr in module.scope.bindings:
            out = self._bound(module.scope, attr)
        if not out and self._path_of(f"{module_name}.{attr}") is not None:
            # Also the case for ``from . import sub`` in a package's own ``__init__``.
            out = [_ModuleRef(f"{module_name}.{attr}")]
        return out

    def _class_attr(self, body: _Scope, attr: str) -> List[_Value]:
        return self._guarded((id(body), f".{attr}"), lambda: self._class_attr_now(body, attr))

    def _class_attr_now(self, body: _Scope, attr: str) -> List[_Value]:
        out: List[_Value] = []
        if attr in body.bindings:
            out += self._bound(body, attr)
        for binding in body.attrs.get(attr, []):
            out += self._evaluate(binding)
        for base, scope in body.bases:
            if isinstance(base, ast.Subscript):  # a generic base, ``Base[int]``
                base = base.value
            for value in self.values(base, scope):
                if isinstance(value, _Class):
                    out += self._class_attr(value.scope, attr)
        return _dedupe(out)

    def _attribute(self, bases: Sequence[_Value], attr: str) -> List[_Value]:
        out: List[_Value] = []
        for base in bases:
            if isinstance(base, _ModuleRef):
                out += self._module_attr(base.name, attr)
            elif isinstance(base, _Pattern) and attr == "pattern" and base.source is not None:
                out.append(base.source)
            elif isinstance(base, _Instance) and attr == "__class__":
                out.append(_Class(base.scope))
            elif isinstance(base, (_Class, _Instance)):
                for value in self._class_attr(base.scope, attr):
                    is_property = isinstance(value, _Function) and _decorated(
                        value.node, "property", "cached_property"
                    )
                    if is_property and isinstance(base, _Instance):
                        out += self._returns(value)
                    else:
                        out.append(value)
        return _dedupe(out)

    @staticmethod
    def _elements(values: Sequence[_Value]) -> List[_Value]:
        return _dedupe(
            [item for v in values if isinstance(v, _Seq) for pos in v.items for item in pos]
        )

    # expressions: one reader per node type, dispatched the way ast.NodeVisitor does

    def values(self, expr: Optional[ast.expr], scope: Optional[_Scope]) -> List[_Value]:
        """Every value ``expr`` can have that the scan can read, flow-insensitively."""
        if expr is None or scope is None:
            return []
        reader = getattr(self, f"_read_{type(expr).__name__}", None)
        return reader(expr, scope) if reader is not None else []

    def _read_Constant(self, expr: ast.Constant, scope: _Scope) -> List[_Value]:
        value = expr.value
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        return [_Str.of(value, self._origin(scope, expr))] if isinstance(value, str) else []

    def _read_JoinedStr(self, expr: ast.JoinedStr, scope: _Scope) -> List[_Value]:
        origin = self._origin(scope, expr)
        result: Optional[_Str] = None
        for part in expr.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                piece = _Str.of(part.value, origin)
            else:
                inner: List[_Str] = []
                if (
                    isinstance(part, ast.FormattedValue)
                    and part.format_spec is None
                    and part.conversion == -1
                ):
                    inner = [v for v in self.values(part.value, scope) if isinstance(v, _Str)]
                piece = inner[0] if len(inner) == 1 else _Str(None, None, False, origin)
            result = piece if result is None else _concat(result, piece, origin)
        return [result if result is not None else _Str.of("", origin)]

    def _read_BinOp(self, expr: ast.BinOp, scope: _Scope) -> List[_Value]:
        if not isinstance(expr.op, ast.Add):
            return []
        origin = self._origin(scope, expr)
        lefts = [v for v in self.values(expr.left, scope) if isinstance(v, _Str)]
        rights = [v for v in self.values(expr.right, scope) if isinstance(v, _Str)]
        return _dedupe([_concat(a, b, origin) for a in lefts for b in rights])

    def _read_Name(self, expr: ast.Name, scope: _Scope) -> List[_Value]:
        return self._name(expr.id, scope)

    def _read_Attribute(self, expr: ast.Attribute, scope: _Scope) -> List[_Value]:
        return self._attribute(self.values(expr.value, scope), expr.attr)

    def _read_Subscript(self, expr: ast.Subscript, scope: _Scope) -> List[_Value]:
        index = expr.slice
        key = index.value if isinstance(index, ast.Constant) else None
        out: List[_Value] = []
        for base in self.values(expr.value, scope):
            if isinstance(base, _Map):
                out += self._lookup(base, key if isinstance(key, str) else None)
            elif isinstance(base, _Seq):
                known = isinstance(key, int) and -len(base.items) <= key < len(base.items)
                if known and len(base.items) > 1:
                    out += base.items[key]
                else:
                    out += [v for pos in base.items for v in pos]
        return _dedupe(out)

    @staticmethod
    def _lookup(mapping: _Map, key: Optional[str]) -> List[_Value]:
        hits = [vs for k, vs in mapping.entries if key is not None and k == key]
        chosen = hits if hits else [vs for _, vs in mapping.entries]
        return _dedupe([v for vs in chosen for v in vs])

    def _read_Dict(self, expr: ast.Dict, scope: _Scope) -> List[_Value]:
        entries: List[Tuple[Optional[str], Tuple[_Value, ...]]] = []
        for key, value in zip(expr.keys, expr.values, strict=True):
            if key is None:  # ``**other``
                entries += [
                    e for m in self.values(value, scope) if isinstance(m, _Map) for e in m.entries
                ]
                continue
            text = key.value if isinstance(key, ast.Constant) else None
            entries.append(
                (text if isinstance(text, str) else None, tuple(self.values(value, scope)))
            )
        return [_Map(tuple(entries))]

    def _read_Tuple(self, expr: Union[ast.Tuple, ast.List, ast.Set], scope: _Scope) -> List[_Value]:
        items: List[Tuple[_Value, ...]] = []
        for elt in expr.elts:
            if isinstance(elt, ast.Starred):
                items.append(tuple(self._elements(self.values(elt.value, scope))))
            else:
                items.append(tuple(self.values(elt, scope)))
        keeps_positions = isinstance(expr, ast.Tuple) and not any(
            isinstance(elt, ast.Starred) for elt in expr.elts
        )
        if not keeps_positions:
            items = [tuple(_dedupe([v for pos in items for v in pos]))]
        return [_Seq(tuple(items))]

    _read_List = _read_Set = _read_Tuple

    def _read_ListComp(
        self, expr: Union[ast.ListComp, ast.SetComp, ast.GeneratorExp], scope: _Scope
    ) -> List[_Value]:
        inner = scope.module.scopes.get(id(expr))
        return [_Seq((tuple(self.values(expr.elt, inner)),))] if inner else []

    _read_SetComp = _read_GeneratorExp = _read_ListComp

    def _read_DictComp(self, expr: ast.DictComp, scope: _Scope) -> List[_Value]:
        inner = scope.module.scopes.get(id(expr))
        return [_Map(((None, tuple(self.values(expr.value, inner))),))] if inner else []

    def _read_Lambda(self, expr: ast.Lambda, scope: _Scope) -> List[_Value]:
        inner = scope.module.scopes.get(id(expr))
        return [_Function(expr, inner)] if inner else []

    def _read_IfExp(self, expr: ast.IfExp, scope: _Scope) -> List[_Value]:
        return _dedupe(self.values(expr.body, scope) + self.values(expr.orelse, scope))

    def _read_BoolOp(self, expr: ast.BoolOp, scope: _Scope) -> List[_Value]:
        return _dedupe([v for operand in expr.values for v in self.values(operand, scope)])

    def _read_NamedExpr(self, expr: ast.NamedExpr, scope: _Scope) -> List[_Value]:
        return self.values(expr.value, scope)

    # calls

    def _read_Call(self, expr: ast.Call, scope: _Scope) -> List[_Value]:
        func = expr.func
        if isinstance(func, ast.Attribute):
            bases = self.values(func.value, scope)
            if _is_regex_module(bases):
                return self._regex_call(func.attr, expr, scope)
            known = self._method(func.attr, bases, expr, scope)
            if known is not None:
                return known
            # Read the callee from ``bases``: reading ``func`` again would evaluate the
            # receiver twice, which doubles the work at every call in a chain.
            callees, name = self._attribute(bases, func.attr), func.attr
        else:
            callees, name = self.values(func, scope), getattr(func, "id", "")
        if not callees:
            return self._builtin(name, expr, scope)
        out: List[_Value] = []
        for callee in callees:
            if isinstance(callee, _Function):
                out += self._returns(callee)
            elif isinstance(callee, _Class):
                out.append(_Instance(callee.scope))
        return _dedupe(out)

    def _regex_call(self, attr: str, expr: ast.Call, scope: _Scope) -> List[_Value]:
        if attr == "compile":
            return self._compiled(expr, scope, flags_at=1)
        if attr == "escape":
            # An escaped string anchors nothing: its ``$`` comes out as ``\$``.
            return [_Str("", "", False, self._origin(scope, expr))]
        return []

    def _method(
        self, attr: str, bases: Sequence[_Value], expr: ast.Call, scope: _Scope
    ) -> Optional[List[_Value]]:
        """``str.join``, and the dict methods that hand back what the dict holds."""
        separators = [b for b in bases if isinstance(b, _Str)]
        if attr == "join" and separators and expr.args:
            parts = self._elements(self.values(expr.args[0], scope))
            return self._joined(separators, parts, self._origin(scope, expr))
        maps = [b for b in bases if isinstance(b, _Map)]
        if not maps:
            return None
        if attr == "get" and expr.args:
            key = expr.args[0]
            text = key.value if isinstance(key, ast.Constant) else None
            out = [
                v for m in maps for v in self._lookup(m, text if isinstance(text, str) else None)
            ]
            return _dedupe(out + self.values(expr.args[1] if len(expr.args) > 1 else None, scope))
        if attr == "values":
            return [_Seq((tuple(_dedupe([v for m in maps for _, vs in m.entries for v in vs])),))]
        if attr == "items":
            origin = self._origin(scope, expr)
            pairs = [
                _Seq(((_Str.of(k, origin),) if k else (), vs)) for m in maps for k, vs in m.entries
            ]
            return [_Seq((tuple(pairs),))]
        return None

    @staticmethod
    def _joined(separators: List[_Str], parts: List[_Value], origin: str) -> List[_Value]:
        """``sep.join(parts)``. Joined by ``|``, the parts are the alternatives of one
        pattern, so each is read on its own. Otherwise the result starts and ends the
        way the parts do, where they agree."""
        strings = [p for p in parts if isinstance(p, _Str)]
        if not strings or len(strings) != len(parts):
            return []
        if all(s.whole and s.head == "|" for s in separators):
            return list(strings)
        heads = {s.head for s in strings}
        tails = {s.tail for s in strings}
        head = heads.pop() if len(heads) == 1 else None
        tail = tails.pop() if len(tails) == 1 else None
        return [_Str(head, tail, False, origin)]

    def _builtin(self, name: str, expr: ast.Call, scope: _Scope) -> List[_Value]:
        """The builtins (and ``MappingProxyType``) that pass a container through."""
        first = expr.args[0] if expr.args else None
        if name == "type" and first is not None:
            return [_Class(v.scope) for v in self.values(first, scope) if isinstance(v, _Instance)]
        if name == "enumerate" and first is not None:
            elements = self._elements(self.values(first, scope))
            return [_Seq((tuple(_Seq(((), (e,))) for e in elements),))]
        if name not in _PASS_THROUGH:
            return []
        out = self.values(first, scope)
        if name == "dict" and expr.keywords:
            keywords = [(kw.arg, tuple(self.values(kw.value, scope))) for kw in expr.keywords]
            out.append(_Map(tuple(keywords)))
        return out

    def _compiled(self, expr: ast.Call, scope: _Scope, flags_at: int) -> List[_Value]:
        """The pattern(s) ``re.compile`` / ``re.match`` / ``re.search`` is given."""
        pattern = expr.args[0] if expr.args else _keyword(expr, "pattern")
        flags_expr = expr.args[flags_at] if len(expr.args) > flags_at else _keyword(expr, "flags")
        flags = 0 if flags_expr is None else self._flags(flags_expr, scope)
        what = ast.unparse(pattern) if pattern is not None else "?"
        origin = self._origin(scope, expr)
        out: List[_Value] = []
        for value in self.values(pattern, scope):
            if isinstance(value, (_Pattern, _Passed)):
                out.append(value)
            elif isinstance(value, _Str) and value.tail is not None:
                out.append(_Pattern(value, flags, what, value.origin))
            elif isinstance(value, _Str):
                # A string whose end the scan cannot read counts as built at run time.
                out.append(_Pattern(None, flags, what, origin))
        if not out:
            out.append(_Pattern(None, flags, what, origin))
        return _dedupe(out)

    def _flags(self, expr: ast.expr, scope: _Scope) -> Optional[int]:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, int):
            return expr.value
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            left, right = self._flags(expr.left, scope), self._flags(expr.right, scope)
            return None if left is None or right is None else left | right
        if isinstance(expr, ast.Attribute) and _is_regex_module(self.values(expr.value, scope)):
            flag = getattr(re, expr.attr, None)
            return int(flag) if isinstance(flag, int) else None
        if isinstance(expr, ast.Name):
            current: Optional[_Scope] = scope
            while current is not None and expr.id not in current.bindings:
                current = current.parent
            key = (id(current), f"<flags {expr.id}>")
            if current is None or key in self._active:
                return None
            self._active.add(key)
            try:
                found = {
                    self._flags(b.expr, b.scope)
                    for b in current.bindings[expr.id]
                    if b.expr is not None and b.scope is not None and not b.path
                }
            finally:
                self._active.discard(key)
            if len(found) == 1:
                return found.pop()
        return None

    def _returns(self, function: _Function) -> List[_Value]:
        key = (id(function.node), "<return>")
        return self._guarded(key, lambda: self._returns_now(function))

    def _returns_now(self, function: _Function) -> List[_Value]:
        node = function.node
        if isinstance(node, ast.Lambda):
            return self.values(node.body, function.scope)
        out: List[_Value] = []
        for ret in _own_returns(node):
            out += self.values(ret.value, function.scope)
        return _dedupe(out)

    # the check

    def findings(self) -> List[Finding]:
        out: List[Finding] = []
        for path in sorted(self.package.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.unreadable.append(f"{self._rel(path)}: {type(exc).__name__}: {exc}")
                continue
            if ".match" not in text and ".search" not in text:
                continue
            rel = path.relative_to(self.checkout).with_suffix("")
            parts = list(rel.parts[:-1]) if rel.name == "__init__" else list(rel.parts)
            module = self.module(".".join(parts))
            if module is None:
                continue
            for node, scope, function in module.calls:
                out += self._check(module, node, scope, function)
        return sorted(set(out))

    def _check(
        self, module: _Module, node: ast.expr, scope: _Scope, function: str
    ) -> List[Finding]:
        func = node.func if isinstance(node, ast.Call) else node
        assert isinstance(func, ast.Attribute)
        rel = self._rel(module.path)
        try:
            bases = self.values(func.value, scope)
            if not _is_regex_module(bases):
                patterns = [b for b in bases if isinstance(b, (_Pattern, _Passed))]
            elif isinstance(node, ast.Call):
                patterns = self._compiled(node, scope, flags_at=2)
            else:
                patterns = []  # ``re.match`` handed on: its pattern is given elsewhere
        except RecursionError:
            self.unreadable.append(f"{rel}:{node.lineno}: too deeply nested to follow")
            return []
        if patterns:
            self.calls_seen += 1
        shown = ast.unparse(node)
        if len(shown) > 100:
            shown = shown[:97] + "..."
        out = []
        for pattern in patterns:
            label = _gap(pattern, func.attr)
            if label is not None:
                out.append(Finding(rel, function, label, node.lineno, shown, pattern.origin))
        return out


# ── the anchor rule ─────────────────────────────────────────────────────
#
# Items come from ``re``'s own parser: ``(opcode, argument)`` pairs, where a group,
# a repeat or an alternation holds parsed sub-patterns of its own.


def _matches_newline(item: Tuple[object, object], flags: int) -> bool:
    """Whether one parsed character item (``.``, ``\\n``, ``[^/]``, ``\\s``) matches ``\\n``."""
    op, av = item
    if op is _sre.ANY:
        return bool(flags & re.DOTALL)
    if op is _sre.LITERAL:
        return av == _NEWLINE
    if op is _sre.NOT_LITERAL:
        return av != _NEWLINE
    if op is _sre.IN:
        negated = any(sub_op is _sre.NEGATE for sub_op, _ in av)
        hit = any(
            (sub_op is _sre.LITERAL and sub_av == _NEWLINE)
            or (sub_op is _sre.RANGE and sub_av[0] <= _NEWLINE <= sub_av[1])
            or (sub_op is _sre.CATEGORY and sub_av in _CLASSES_WITH_NEWLINE)
            for sub_op, sub_av in av
        )
        return hit != negated
    return False


def _absorbs_newline(items: List[Tuple[object, object]], flags: int) -> bool:
    """True when the last item repeats, without bound, something that matches ``\\n``."""
    if not items:
        return False
    op, av = items[-1]
    if op in _REPEATS:
        _, high, sub = av
        return (
            high == _sre.MAXREPEAT and len(sub.data) == 1 and _matches_newline(sub.data[0], flags)
        )
    if op is _sre.SUBPATTERN:
        _, add, delete, sub = av
        return _absorbs_newline(sub.data, (flags | add) & ~delete)
    if op is _sre.BRANCH:
        return all(_absorbs_newline(branch.data, flags) for branch in av[1])
    return False


def _ends_at_dollar(
    items: List[Tuple[object, object]], flags: int, before: Sequence[Tuple[object, object]] = ()
) -> bool:
    """True when a match can end at a ``$`` that lets a final newline through.

    ``before`` holds the items ahead of ``items`` in the enclosing sequence: what
    precedes a ``$`` that opens a group or an alternative."""
    if not items:
        return False
    op, av = items[-1]
    preceding = [*before, *items[:-1]]
    if op is _sre.AT and av is _sre.AT_END:
        return not flags & re.MULTILINE and not _absorbs_newline(preceding, flags)
    if op is _sre.SUBPATTERN:
        _, add, delete, sub = av
        return _ends_at_dollar(sub.data, (flags | add) & ~delete, preceding)
    if op is _sre.BRANCH:
        return any(_ends_at_dollar(branch.data, flags, preceding) for branch in av[1])
    if op is _ATOMIC_GROUP:
        return _ends_at_dollar(av.data, flags, preceding)
    return False


def _anchored_at_start(items: List[Tuple[object, object]]) -> bool:
    if not items:
        return False
    op, av = items[0]
    if op is _sre.AT:
        return av in (_sre.AT_BEGINNING, _sre.AT_BEGINNING_STRING)
    if op is _sre.SUBPATTERN:
        return _anchored_at_start(av[3].data)
    if op is _sre.BRANCH:
        return all(_anchored_at_start(branch.data) for branch in av[1])
    return False


def _alternatives(
    items: List[Tuple[object, object]], flags: int
) -> Iterator[Tuple[List[Tuple[object, object]], int]]:
    """The top-level alternatives, looking into a group that wraps the whole pattern."""
    if len(items) == 1:
        op, av = items[0]
        if op is _sre.BRANCH:
            for branch in av[1]:
                yield from _alternatives(branch.data, flags)
            return
        if op is _sre.SUBPATTERN:
            _, add, delete, sub = av
            yield from _alternatives(sub.data, (flags | add) & ~delete)
            return
    yield items, flags


def _gap(pattern: Union[_Pattern, _Passed], method: str) -> Optional[str]:
    """The finding label when this pattern lets a final newline through ``method``."""
    if isinstance(pattern, _Passed):
        return f"<parameter: {pattern.name}>"
    source = pattern.source
    if source is None:
        return f"<run time: {pattern.what}>"
    # A string read only in part stands an inert group in for the part it cannot read.
    text = source.head if source.whole else f"{source.head or ''}(?:x){source.tail or ''}"
    try:
        parsed = _sre_parse.parse(text or "", pattern.flags or 0)
    except (re.error, OverflowError, RecursionError):
        return source.show() if (source.tail or "").endswith("$") else None
    for items, flags in _alternatives(parsed.data, parsed.state.flags):
        anchored = method == "match" or _anchored_at_start(items)
        if anchored and _ends_at_dollar(items, flags):
            return source.show()
    return None


# ── the gate ────────────────────────────────────────────────────────────

#: Calls whose ``$`` stays, keyed by (file, function, pattern), each with its reason.
_ACCEPTED: Dict[Tuple[str, str, str], str] = {
    # The value cannot end in a newline when it is matched: it was stripped, split
    # or rewritten just before, and that value is the one used afterwards.
    (
        "fluid_build/__init__.py",
        "_version_from_git_checkout",
        r"(?P<tag>[^-]+)-(?P<distance>\d+)-g(?P<sha>[0-9a-f]+)$",
    ): "stripped `git describe` output; only the captured tag and distance are used",
    (
        "fluid_build/build_runners/_databricks_load.py",
        "staging_volume",
        r"^/Volumes/([^/]+)/([^/]+)/([^/]+)/?$",
    ): "matches the stripped variable, and each captured part must then pass validate_ident",
    (
        "fluid_build/build_runners/_late_arrival.py",
        "parse_iso_duration",
        r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
        r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    ): "matches text.strip(); the span is built from the captured numbers",
    (
        "fluid_build/cli/_acquisition_stage_ext.py",
        "_validate_cron",
        r"^[0-9*/,\-?LW#]+$",
    ): "each field comes from value.split()",
    (
        "fluid_build/cli/import_workflow/dbt.py",
        "_safe_tags",
        r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$",
    ): "every run of characters outside [a-z0-9] was replaced with '-' just before",
    (
        "fluid_build/cli/provider_init.py",
        "run",
        r"^[a-z][a-z0-9_]*$",
    ): "matches the stripped provider name, which is the one used",
    (
        "fluid_build/cli/schedule_sync.py",
        "_parse_commit_author",
        r"^(.+?)\s*<([^<>@\s]+@[^<>@\s]+)>$",
    ): "the author is stripped, and control characters (\\n too) are refused just before",
    (
        "fluid_build/copilot/agents/ai_ready_agent.py",
        "_normalize_allowed_models",
        r"^[a-z0-9][a-z0-9-_.]*[a-z0-9]$|^[a-z0-9]$",
    ): "the token was stripped and its whitespace replaced with '-' just before",
    (
        "fluid_build/engines/dbt/_types.py",
        "split_parameterized",
        r"^([A-Za-z_][A-Za-z0-9_ ]*?)\s*\(\s*([^()]*)\s*\)$",
    ): "matches fluid_type.strip() and returns the captured parts",
    (
        "fluid_build/exporters/sodacl.py",
        "_convert_dq_rule",
        r"^[A-Za-z_][A-Za-z0-9_]*$",
    ): "the selector is stripped where it is read",
    (
        "fluid_build/forge/federation.py",
        "_secret_ref_allowlist",
        r"^(?:.*@)?(?:\[[^\]]+\]|[^:\[\]]+):\d+$",
    ): "matches the stripped host, which is the one kept",
    (
        "fluid_build/iac/provider_match.py",
        "detect_cloud_declarations",
        r"^[a-z]{2}-[a-z]+-\d+$",
    ): "matches region.strip(), and a match only records that AWS is declared",
    (
        "fluid_build/iac/providers/databricks_jobs.py",
        "_notifications",
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ): "the target is stripped where it is read",
    (
        "fluid_build/iac/providers/databricks_jobs.py",
        "_run_as",
        r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$",
    ): "the account is stripped where it is read",
    (
        "fluid_build/iac/providers/databricks_jobs.py",
        "_seconds",
        r"^(\d+)\s*([smhd]?)$",
    ): "the text is stripped; the seconds come from the captured numbers",
    (
        "fluid_build/iac/providers/databricks_jobs.py",
        "_seconds",
        r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$",
    ): "the text is stripped; the seconds come from the captured numbers",
    (
        "fluid_build/iac/providers/databricks_retention.py",
        "iso_duration_seconds",
        r"^P(?!$)(?:(\d{1,12})Y)?(?:(\d{1,12})M)?(?:(\d{1,12})W)?(?:(\d{1,12})D)?"
        r"(?:T(?=\d)(?:(\d{1,12})H)?(?:(\d{1,12})M)?(?:(\d{1,12})S)?)?$",
    ): "matches text.strip(); the seconds come from the captured numbers",
    (
        "fluid_build/llm/providers.py",
        "_parse_param_size",
        r"^([\d.]+)\s*([BM]?)$",
    ): "the text is stripped; the size comes from the captured number",
    (
        "fluid_build/providers/__init__.py",
        "_is_valid_name",
        r"^[a-z0-9_]+$",
    ): "both callers pass _normalize_name() output, which is stripped",
    (
        "fluid_build/providers/_path_safety.py",
        "safe_filename_stem",
        r"^[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]$|^[A-Za-z0-9_]$",
    ): "matches the stripped text and returns that",
    (
        "fluid_build/providers/gcp/util/names.py",
        "normalize_bucket_name",
        r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$",
    ): "every character outside [a-z0-9-] was replaced with '-' just before",
    (
        "fluid_build/providers/odcs/mappers/sla.py",
        "_as_availability_pct",
        r"^(100(\.0+)?|\d{2}(\.\d+)?|\d{1}\d(\.\d+)?)%$",
    ): "matches stripped text, or a percentage formatted from a float",
    (
        "fluid_build/providers/odcs/mappers/sla.py",
        "_as_iso_duration",
        r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(\d+H)?(\d+M)?(\d+S)?)?$",
    ): "matches value.strip() and returns that",
    (
        "fluid_build/providers/quality_engine.py",
        "_parse_duration_seconds",
        r"^(\d+)\s*([smhd])$",
    ): "the text is stripped; the seconds come from the captured number",
    (
        "fluid_build/providers/quality_engine.py",
        "_parse_duration_seconds",
        r"^P(\d+)W$",
    ): "the text is stripped; the seconds come from the captured number",
    (
        "fluid_build/providers/snowflake/plan/planner.py",
        "_validate_policy_signature",
        r"^\(\s*[A-Za-z_][A-Za-z0-9_]*\s+[A-Za-z_][A-Za-z0-9_(),\s]*\)"
        r"(\s+RETURNS\s+[A-Za-z_][A-Za-z0-9_]*)?$",
    ): "matches the stripped signature and returns that",
    (
        "fluid_build/schema_manager.py",
        "SchemaVersion.parse",
        r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-(.+))?$",
    ): "matches version_str.strip() and keeps the captured parts",
    (
        "fluid_build/util/cron.py",
        "_values",
        r"^(\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)(?:/([0-9]+))?$",
    ): "the expression's whitespace is collapsed and it is split into fields first",
    # The value is another function's output, which cannot end in a newline.
    (
        "fluid_build/iac/providers/aws.py",
        "_hive_decimal",
        r"^\((\d+)(?:,(\d+))?\)$",
    ): "the only caller passes column_types.split_type() output, which holds no whitespace",
    (
        "fluid_build/iac/providers/gcp.py",
        "_bq_outcome",
        r"^\((\d+)(?:,(\d+))?\)$",
    ): "the value is column_types.split_type() output, which holds no whitespace",
    (
        "fluid_build/iac/providers/databricks.py",
        "_scale_zero_warnings",
        r"^decimal(\([0-9]+\))?$",
    ): "the value is dbt_types.sql_type() output",
    (
        "fluid_build/iac/providers/databricks.py",
        "_type_problem",
        r"^[a-z_]+(<[a-z_]+>)?(\([0-9]{1,4}(,[0-9]{1,4})?\))?$",
    ): "the only caller passes dbt_types.sql_type() output",
    # Refusals and detectors: a match rejects or labels the value, so `$` errs on
    # the safe side here, and \Z would loosen them.
    (
        "fluid_build/build_runners/hooks/dlp_scan.py",
        "_classify_value",
        r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
    ): "a PII detector: a match labels the value as an email",
    (
        "fluid_build/build_runners/hooks/dlp_scan.py",
        "_classify_value",
        r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$",
    ): "a PII detector: a match labels the value as an IP address",
    (
        "fluid_build/build_runners/hooks/dlp_scan.py",
        "_classify_value",
        r"^\d{3}-\d{2}-\d{4}$",
    ): "a PII detector: a match labels the value as an SSN",
    (
        "fluid_build/providers/aws/util/validation.py",
        "ResourceValidator.validate_s3_bucket_name",
        r"^\d+\.\d+\.\d+\.\d+$",
    ): "refuses a bucket name shaped like an IP address",
    (
        "fluid_build/providers/gcp/util/names.py",
        "normalize_bucket_name",
        r"^\d+\.\d+\.\d+\.\d+$",
    ): "refuses a bucket name shaped like an IP address",
    (
        "fluid_build/providers/gcp/util/names.py",
        "validate_name",
        r"^\d+\.\d+\.\d+\.\d+$",
    ): "refuses a bucket name shaped like an IP address",
    # Parsers: only the captured groups are used, and nothing is validated.
    (
        "fluid_build/build_runners/_retention.py",
        "parse_iso_duration",
        r"^P(?!$)(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
    ): "returns a timedelta built from the captured numbers; every caller keeps that",
    (
        "fluid_build/build_runners/duckdb/runner.py",
        "_canonical_cursor_text",
        r"(?P<head>.*?)(?P<off>Z|[+-]\d{2}:?(?:\d{2})?)$",
    ): "rebuilds a cursor timestamp from its captured parts and refuses nothing",
    (
        "fluid_build/cli/version_cmd.py",
        "_bundled_schema_versions",
        r"^fluid-schema-(\d+\.\d+(?:\.\d+)?)\.json$",
    ): "reads the version out of a bundled schema's file name",
    (
        "fluid_build/iac/providers/databricks.py",
        "_canonical_type",
        r"^decimal\(([0-9]{1,4})(?:,([0-9]{1,4}))?\)$",
    ): "spells decimal(p) as decimal(p,0) from the captured digits and refuses nothing",
    (
        "fluid_build/iac/providers/databricks_retention.py",
        "_part_selects",
        r"^(@)?(\d*\+)?(.*?)(\+\d*)?$",
    ): "the selectors are split on whitespace first, and only the captured parts are used",
    (
        "fluid_build/providers/_duckdb_sandbox.py",
        "DuckDBAllowlist.with_locations",
        r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<host>[^/?#]+)(?P<path>/[^?#]*)?$",
    ): "the host and path classes admit \\n, so the groups take a final newline under \\Z too",
    (
        "fluid_build/providers/_duckdb_sandbox.py",
        "_remote",
        r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<host>[^/?#]+)(?P<path>/[^?#]*)?$",
    ): "the host and path classes admit \\n, so the groups take a final newline under \\Z too",
    (
        "fluid_build/providers/_duckdb_sandbox.py",
        "is_remote_location",
        r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<host>[^/?#]+)(?P<path>/[^?#]*)?$",
    ): "the host and path classes admit \\n, so the groups take a final newline under \\Z too",
    (
        "fluid_build/schema_manager.py",
        "FluidSchemaManager._load_bundled_schemas",
        r"^fluid-schema-(\d+\.\d+(?:\.\d+)?)\.json$",
    ): "reads the version out of a bundled schema's file name",
    (
        "fluid_build/schema_manager.py",
        "_discover_bundled_versions",
        r"^fluid-schema-(\d+\.\d+(?:\.\d+)?)\.json$",
    ): "reads the version out of a bundled schema's file name",
    (
        "fluid_build/schema_manager.py",
        "_schema_version_sort_key",
        r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-(.+))?$",
    ): "turns the captured numbers into a sort key",
    # Judgment calls, and a known gap left for a follow-up.
    (
        "fluid_build/providers/snowflake/util/names.py",
        "quote_identifier",
        r"^[A-Z_][A-Z0-9_]*$",
    ): (
        "decides quoting and refuses nothing: under \\Z, ORDERS\\n would be quoted as the "
        "object 'ORDERS\\n'. The identifier validators upstream are the guard"
    ),
    (
        "fluid_build/cli/import_workflow/dbt.py",
        "_recency_window",
        r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(\d+H)?(\d+M)?(\d+S)?)?$",
    ): (
        "follow-up: returns fluid_window verbatim, and the duration parsers downstream "
        "accept a final newline, so \\Z would drop the window. Strip it and return the "
        "stripped value, as sla._as_iso_duration does"
    ),
    # Patterns read at run time.
    (
        "fluid_build/build_runners/hooks/quality_gate.py",
        "_check",
        "<run time: pat>",
    ): (
        "follow-up: a contract's regex gate runs with re.match, so '^[A-Z]{3}$' passes "
        "'ABC\\n'. The hook is not wired into a live chain yet (see its module note); "
        "settle match against fullmatch before it is"
    ),
    (
        "fluid_build/schema_manager.py",
        "_locate_error",
        "<run time: p>",
    ): (
        "mirrors jsonschema, which applies patternProperties with re.search; anything "
        "else would point at a different key from the one the validator rejected"
    ),
    (
        "fluid_build/schema_manager.py",
        "_pattern_keyword",
        "<run time: _end_anchored(pattern)>",
    ): (
        "jsonschema's pattern keyword: _end_anchored() turns every $ into \\Z, and .search "
        "is what JSON Schema itself runs"
    ),
    (
        "fluid_build/schema_manager.py",
        "_pattern_keyword",
        "<run time: pattern>",
    ): "only picks the error message, after the \\Z check above has refused the value",
}

#: The entries that cover more than one call, with the number read; every other entry
#: covers one. So a new call with a listed pattern in a listed function is reported.
_ACCEPTED_CALLS: Dict[Tuple[str, str, str], int] = {
    (
        "fluid_build/providers/odcs/mappers/sla.py",
        "_as_availability_pct",
        r"^(100(\.0+)?|\d{2}(\.\d+)?|\d{1}\d(\.\d+)?)%$",
    ): 2,
}


@pytest.fixture(scope="module")
def tree() -> Tuple[_Scan, List[Finding]]:
    scanner = _Scan(_PACKAGE)
    return scanner, scanner.findings()


def test_the_scan_reads_every_module_and_follows_patterns(
    tree: Tuple[_Scan, List[Finding]],
) -> None:
    """A module the scan could not read, or a walk that followed no pattern, would let
    the gate pass without checking anything."""
    scanner, _ = tree
    assert scanner.unreadable == [], "the scan could not read:\n  " + "\n  ".join(
        scanner.unreadable
    )
    assert scanner.calls_seen > 150, f"only {scanner.calls_seen} calls had a pattern to follow"


def test_no_validator_lets_a_final_newline_through(tree: Tuple[_Scan, List[Finding]]) -> None:
    _, found = tree
    new = [f for f in found if f.key not in _ACCEPTED]
    assert not new, (
        "These calls run a pattern that ends in `$` with .match or .search, so a value "
        "with a final newline passes. End the pattern in \\Z or call fullmatch. If the "
        "`$` is deliberate, add the call to _ACCEPTED with the reason:\n  "
        + "\n  ".join(f.show() for f in new)
    )


def test_every_accepted_entry_still_matches_a_call(tree: Tuple[_Scan, List[Finding]]) -> None:
    _, found = tree
    keys = {f.key for f in found}
    stale = sorted(key for key in _ACCEPTED if key not in keys)
    assert not stale, "These _ACCEPTED entries match no call any more. Remove them:\n  " + (
        "\n  ".join(map(repr, stale))
    )


def test_every_accepted_entry_covers_the_calls_it_was_read_for(
    tree: Tuple[_Scan, List[Finding]],
) -> None:
    """A second call with the same pattern in the same function was not read when the
    entry was written, so it must not pass on the entry's reason."""
    _, found = tree
    counts = Counter(f.key for f in found if f.key in _ACCEPTED)
    changed = sorted(key for key, n in counts.items() if n != _ACCEPTED_CALLS.get(key, 1))
    assert not changed, (
        "These _ACCEPTED entries now cover a different number of calls. Read the new "
        "call; if its `$` is deliberate too, set the count in _ACCEPTED_CALLS:\n  "
        + "\n  ".join(f.show() for key in changed for f in found if f.key == key)
    )


def test_every_accepted_entry_gives_its_reason() -> None:
    assert all(reason.strip() for reason in _ACCEPTED.values())
    assert set(_ACCEPTED_CALLS) <= set(_ACCEPTED)


def test_fluid_build_imports_re_as_a_module() -> None:
    """The scan reads ``re.match(...)``, not a bare ``match(...)`` from ``from re import``."""
    offenders = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "from re import" not in text and "from regex import" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module in _REGEX_MODULES:
                offenders.append(f"{path.relative_to(_PACKAGE.parent)}:{node.lineno}")
    assert not offenders, (
        "Import the regex module itself (`import re`), so this gate can read the calls, "
        "or teach the scan the new form:\n  " + "\n  ".join(offenders)
    )


# ── the scan itself ─────────────────────────────────────────────────────


def _scan(tmp_path: pathlib.Path, **modules: str) -> List[str]:
    """Scan a throwaway ``fluid_build`` package; one ``function: pattern`` line per report.

    A module name may name a subpackage: ``pkg__init__`` becomes ``pkg/__init__.py``."""
    package = tmp_path / "fluid_build"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, source in modules.items():
        path = package / f"{name.replace('__init__', '/__init__')}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    scanner = _Scan(package)
    found = scanner.findings()
    assert scanner.unreadable == []
    return [f"{f.function}: {f.pattern}" for f in found]


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            """
            import re
            _NAME_RE = re.compile(r"^[a-z]+$")

            def check(value):
                if not _NAME_RE.match(value):
                    raise ValueError(value)
            """,
            ["check: ^[a-z]+$"],
            id="a-new-validator",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.match(r"^[a-z]+$", value)
            """,
            ["check: ^[a-z]+$"],
            id="re-match",
        ),
        pytest.param(
            """
            import re as _re

            def check(value):
                return _re.search(r"\\A[a-z]+$", value)
            """,
            [r"check: \A[a-z]+$"],
            id="re-search-anchored",
        ),
        pytest.param(
            """
            import regex

            def check(value):
                return regex.match(r"^[a-z]+$", value)
            """,
            ["check: ^[a-z]+$"],
            id="the-regex-module",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.match(r"[a-z]+$", value)
            """,
            ["check: [a-z]+$"],
            id="match-anchors-the-start-itself",
        ),
        pytest.param(
            """
            import re
            NAME = r"^[a-z]+$"

            def check(value):
                return re.match(NAME, value)
            """,
            ["check: ^[a-z]+$"],
            id="string-constant",
        ),
        pytest.param(
            """
            import re
            _PATTERNS = {"table": re.compile(r"^[a-z_]+$"), "project": re.compile(r"^[a-z-]+$")}

            def check(kind, value):
                pattern = _PATTERNS.get(kind)
                return pattern is not None and pattern.match(value)
            """,
            ["check: ^[a-z-]+$", "check: ^[a-z_]+$"],
            id="dict-get",
        ),
        pytest.param(
            """
            import re
            _PATTERNS = {"table": re.compile(r"^[a-z_]+$"), "project": re.compile(r"^[a-z-]+\\Z")}

            def check(value):
                return _PATTERNS["table"].match(value)
            """,
            ["check: ^[a-z_]+$"],
            id="dict-key",
        ),
        pytest.param(
            """
            import re
            _RAW = {"table": r"^[a-z_]+$"}
            _PATTERNS = {kind: re.compile(raw) for kind, raw in _RAW.items()}

            def check(kind, value):
                return _PATTERNS[kind].match(value)
            """,
            ["check: ^[a-z_]+$"],
            id="dict-comprehension",
        ),
        pytest.param(
            """
            import re
            from types import MappingProxyType
            _PATTERNS = MappingProxyType({"table": re.compile(r"^[a-z_]+$")})

            def check(kind, value):
                return _PATTERNS[kind].match(value)
            """,
            ["check: ^[a-z_]+$"],
            id="mapping-proxy",
        ),
        pytest.param(
            """
            import re
            _PATTERNS = dict(table=re.compile(r"^[a-z_]+$"))

            def check(kind, value):
                return _PATTERNS[kind].match(value)
            """,
            ["check: ^[a-z_]+$"],
            id="dict-keywords",
        ),
        pytest.param(
            """
            import re
            _DENY = (re.compile(r"^admin$"), re.compile(r"^root$"))

            def refused(value):
                return any(rx.match(value) for rx in _DENY)
            """,
            ["refused: ^admin$", "refused: ^root$"],
            id="tuple-in-a-comprehension",
        ),
        pytest.param(
            """
            import re
            _DENY = tuple([re.compile(r"^admin$")])

            def refused(value):
                for index, rx in enumerate(sorted(_DENY)):
                    if rx.match(value):
                        return index
            """,
            ["refused: ^admin$"],
            id="tuple-sorted-enumerate",
        ),
        pytest.param(
            """
            import re
            _FIRST = (re.compile(r"^admin$"),)
            _ALL = (*_FIRST, re.compile(r"^root\\Z"))

            def refused(value):
                return _ALL[1].match(value)
            """,
            ["refused: ^admin$"],
            id="starred-tuple",
        ),
        pytest.param(
            """
            import re
            _KINDS = {"email": re.compile(r"^[a-z]+@[a-z]+$")}

            def kinds(value):
                for name, rx in _KINDS.items():
                    if rx.match(value):
                        yield name
            """,
            ["kinds: ^[a-z]+@[a-z]+$"],
            id="dict-items-loop",
        ),
        pytest.param(
            """
            import functools
            import re

            @functools.lru_cache(maxsize=None)
            def _pattern():
                return re.compile(r"^[a-z]+$")

            def check(value):
                return _pattern().match(value)
            """,
            ["check: ^[a-z]+$"],
            id="helper-return",
        ),
        pytest.param(
            """
            import re
            _pattern = lambda: re.compile(r"^[a-z]+$")

            def check(value):
                return _pattern().match(value)
            """,
            ["check: ^[a-z]+$"],
            id="lambda-return",
        ),
        pytest.param(
            """
            import re

            class Validator:
                NAME = re.compile(r"^[a-z]+$")

                def check(self, value):
                    return self.NAME.match(value)
            """,
            ["Validator.check: ^[a-z]+$"],
            id="class-attribute",
        ),
        pytest.param(
            """
            import re

            class Validator:
                def __init__(self):
                    self._name = re.compile(r"^[a-z]+$")

                def check(self, value):
                    return self._name.match(value)
            """,
            ["Validator.check: ^[a-z]+$"],
            id="instance-attribute",
        ),
        pytest.param(
            """
            import functools
            import re

            class Validator:
                @functools.cached_property
                def name(self):
                    return re.compile(r"^[a-z]+$")

                def check(self, value):
                    return self.name.match(value)
            """,
            ["Validator.check: ^[a-z]+$"],
            id="cached-property",
        ),
        pytest.param(
            """
            import re
            from typing import Generic, TypeVar

            T = TypeVar("T")

            class Base(Generic[T]):
                NAME = re.compile(r"^[a-z]+$")

            class Child(Base[int]):
                def check(self, value):
                    return self.NAME.match(value) or type(self).NAME.match(value)
            """,
            ["Child.check: ^[a-z]+$", "Child.check: ^[a-z]+$"],
            id="generic-base-and-type-of-self",
        ),
        pytest.param(
            """
            import re

            class Validator:
                NAME = re.compile(r"^[a-z]+$")

                def check(self, value):
                    return self.__class__.NAME.match(value)
            """,
            ["Validator.check: ^[a-z]+$"],
            id="dunder-class",
        ),
        pytest.param(
            """
            import re
            _NAME_RE = re.compile(r"^[a-z]+$")
            _AGAIN_RE = re.compile(_NAME_RE.pattern, re.IGNORECASE)

            def check(value):
                return _AGAIN_RE.match(value)
            """,
            ["check: ^[a-z]+$"],
            id="pattern-attribute",
        ),
        pytest.param(
            """
            import re
            _NAME_RE = re.compile(r"^[a-z]+$")

            def names(values):
                return list(filter(_NAME_RE.match, values))
            """,
            ["names: ^[a-z]+$"],
            id="method-reference",
        ),
        pytest.param(
            """
            import re

            def check(prefix, value):
                return re.match(rf"^{re.escape(prefix)}-[0-9]+$", value)
            """,
            ["check: ^…-[0-9]+$"],
            id="f-string",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.match("|".join([r"^[a-z]+$", r"^[0-9]+\\Z"]), value)
            """,
            ["check: ^[a-z]+$"],
            id="joined-alternatives",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.compile(r"^[a-z]+$|^[0-9]+$").match(value)
            """,
            ["check: ^[a-z]+$|^[0-9]+$"],
            id="alternation",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.search(r"^[a-z]+$|^[0-9]+\\Z", value)
            """,
            [r"check: ^[a-z]+$|^[0-9]+\Z"],
            id="half-fixed-alternation",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.search(r"^[a-z]+$|[0-9]+", value)
            """,
            ["check: ^[a-z]+$|[0-9]+"],
            id="anchored-alternative-beside-an-unanchored-one",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.search(r"(?:^[a-z]+$)", value) or re.match(r"^(?:[a-z]+$|[0-9]+)", value)
            """,
            ["check: (?:^[a-z]+$)", "check: ^(?:[a-z]+$|[0-9]+)"],
            id="dollar-inside-a-group",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.match(r"^ [a-z]+ $  # the name, and nothing after it", value, re.X)
            """,
            ["check: ^ [a-z]+ $  # the name, and nothing after it"],
            id="verbose",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.match(r"^a\\\\s*$", value)
            """,
            [r"check: ^a\\s*$"],
            id="escaped-backslash-before-s-star",
        ),
        pytest.param(
            """
            import re

            def check(value):
                return re.compile(r"(?i)^[a-z]+$").match(value)
            """,
            ["check: (?i)^[a-z]+$"],
            id="inline-flags",
        ),
        pytest.param(
            """
            import re
            _FLAGS = re.MULTILINE
            _FLAGS = _FLAGS | re.IGNORECASE

            def check(value):
                return re.search(r"^[a-z]+$", value, _FLAGS)
            """,
            ["check: ^[a-z]+$"],
            id="flags-the-scan-cannot-settle-count-as-none",
        ),
        pytest.param(
            """
            import re
            if re.match(r"^v[0-9]+$", "v1"):
                pass
            """,
            ["<module>: ^v[0-9]+$"],
            id="module-level-call",
        ),
        pytest.param(
            """
            import re

            def check(schema, value):
                return re.compile(schema["pattern"]).match(value)
            """,
            ["check: <run time: schema['pattern']>"],
            id="schema-pattern-at-run-time",
        ),
        pytest.param(
            """
            import functools
            import re

            @functools.lru_cache(maxsize=None)
            def _semver_pattern(schema_text):
                return re.compile(schema_text)

            def is_semver(value, schema_text):
                return bool(_semver_pattern(schema_text).match(value))
            """,
            ["is_semver: <run time: schema_text>"],
            id="schema-pattern-from-a-helper",
        ),
        pytest.param(
            """
            import re

            def check(rule, value):
                return re.search(rule["regex"], value)
            """,
            ["check: <run time: rule['regex']>"],
            id="re-search-at-run-time",
        ),
        pytest.param(
            """
            import re

            def check(suffix, value):
                return re.compile(f"{suffix}").match(value)
            """,
            ["check: <run time: f'{suffix}'>"],
            id="f-string-with-an-unknown-end",
        ),
        pytest.param(
            """
            import re
            from typing import Optional

            _DEFAULT = re.compile(r"^[A-Za-z_]+\\Z")

            def check(value, pattern: Optional[re.Pattern[str]] = None):
                rx = pattern or _DEFAULT
                return rx.match(value)
            """,
            ["check: <parameter: pattern>"],
            id="caller-supplied-pattern",
        ),
        pytest.param(
            """
            from typing import Pattern as P

            def check(value, rx: "P[str]"):
                return rx.search(value)
            """,
            ["check: <parameter: rx>"],
            id="pattern-annotation-alias",
        ),
    ],
)
def test_the_scan_reports(tmp_path: pathlib.Path, source: str, expected: List[str]) -> None:
    assert _scan(tmp_path, check=source) == expected


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('_NAME_RE = re.compile(r"^[a-z]+\\Z")\n_NAME_RE.match(v)', id="backslash-z"),
        pytest.param('re.match(r"^[a-z]+\\Z|^[0-9]+\\Z", v)', id="alternation-fixed"),
        pytest.param('_NAME_RE = re.compile(r"^[a-z]+$")\n_NAME_RE.fullmatch(v)', id="fullmatch"),
        pytest.param('re.fullmatch(r"[a-z]+$", v)', id="re-fullmatch"),
        pytest.param('re.findall(r"^[a-z]+$", v)', id="findall"),
        pytest.param('re.compile(r"^[a-z]+$", re.MULTILINE | re.I).search(v)', id="multiline"),
        pytest.param('_FLAGS = re.M\nre.search(r"^[a-z]+$", v, _FLAGS)', id="multiline-constant"),
        pytest.param('re.search(r"(?m)^[a-z]+$", v)', id="inline-multiline"),
        pytest.param('re.search(r"(?m:^[a-z]+$)", v)', id="scoped-multiline"),
        pytest.param('re.match(r"^key:\\s*$", v)', id="whitespace-tail"),
        pytest.param('re.match(r"^[a-z]+://[^/]*$", v)', id="negated-class-tail"),
        pytest.param('re.match(r"(?is)^\\s*distinct\\s+(.+)$", v)', id="dotall-tail-in-a-group"),
        pytest.param('re.match(r"^(?:a\\s*|b\\s+)$", v)', id="every-branch-absorbs"),
        pytest.param('re.search(r"\\.ya?ml$", v)', id="unanchored-suffix-search"),
        pytest.param('re.match(r"^[0-9]+\\$", v)', id="escaped-dollar"),
        pytest.param('re.match(r"^[a-z]+", v)', id="no-end-anchor"),
        pytest.param(
            'words = ["a", "b"]\nre.compile("|".join(re.escape(w) for w in words)).search(v)',
            id="escaped-alternatives",
        ),
        pytest.param(
            'import pathlib\npathlib.PurePath(v).match("*.py")\nstore.search("ns", v)',
            id="not-a-regex",
        ),
        pytest.param(
            "class GlobPattern:\n    pass\n\ndef inner(p: GlobPattern):\n    return p.match(v)",
            id="an-annotation-that-only-contains-the-word",
        ),
    ],
)
def test_the_scan_does_not_report(tmp_path: pathlib.Path, source: str) -> None:
    body = textwrap.indent(source, "    ")
    assert _scan(tmp_path, check=f"import re\n\ndef check(v, store):\n{body}\n") == []


@pytest.mark.parametrize(
    "importer",
    [
        pytest.param("from .consts import NAME_RE\n", id="relative-import"),
        pytest.param("from fluid_build.consts import NAME_RE\n", id="absolute-import"),
        pytest.param("from fluid_build import consts\nNAME_RE = consts.NAME_RE\n", id="module"),
        pytest.param("import fluid_build.consts as c\nNAME_RE = c.NAME_RE\n", id="import-as"),
    ],
)
def test_the_scan_follows_a_pattern_across_modules(tmp_path: pathlib.Path, importer: str) -> None:
    consts = 'import re\nNAME_RE = re.compile(r"^[a-z]+$")\n'
    check = importer + "\ndef check(value):\n    return NAME_RE.match(value)\n"
    assert _scan(tmp_path, consts=consts, check=check) == ["check: ^[a-z]+$"]


def test_the_scan_follows_a_submodule_its_package_imports(tmp_path: pathlib.Path) -> None:
    """``from . import consts`` in ``pkg/__init__.py`` binds the submodule itself."""
    found = _scan(
        tmp_path,
        pkg__init__="from . import consts\n",
        **{"pkg/consts": 'import re\nNAME_RE = re.compile(r"^[a-z]+$")\n'},
        check=(
            "from fluid_build.pkg import consts\n\n"
            "def check(value):\n    return consts.NAME_RE.match(value)\n"
        ),
    )
    assert found == ["check: ^[a-z]+$"]


@pytest.mark.timeout(60)
def test_the_scan_reads_long_chains_in_linear_time(tmp_path: pathlib.Path) -> None:
    """Reading a receiver twice per call, or a name again at each use, doubles the work at
    every step, and 40 steps would never finish. A string doubled 40 times is cut short."""
    reused = "\n".join(f"X{i + 1} = X{i} or X{i}" for i in range(40))
    doubled = "\n".join(f"Y{i + 1} = Y{i} + Y{i}" for i in range(40))
    source = (
        f'import re\nX0 = re.compile(r"^[a-z]+$")\nY0 = "a"\n{reused}\n{doubled}\n\n'
        "def check(v):\n"
        f"    return v{'.strip()' * 40}.match(v) or X40.match(v) or re.match(Y40, v)\n"
    )
    assert _scan(tmp_path, check=source) == ["check: ^[a-z]+$"]


def test_a_module_the_scan_cannot_read_is_listed(tmp_path: pathlib.Path) -> None:
    package = tmp_path / "fluid_build"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "broken.py").write_text("def f(:\n    RX.match(v)\n", encoding="utf-8")
    (package / "latin1.py").write_bytes(b"# caf\xe9\nRX.match(v)\n")
    deep = " + ".join(['"a"'] * 3000)
    (package / "deep.py").write_text(f"import re\nre.match({deep}, v)\n", encoding="utf-8")
    scanner = _Scan(package)
    assert scanner.findings() == []
    listed = sorted(entry.split(":")[0] for entry in scanner.unreadable)
    assert listed == ["fluid_build/broken.py", "fluid_build/deep.py", "fluid_build/latin1.py"]
