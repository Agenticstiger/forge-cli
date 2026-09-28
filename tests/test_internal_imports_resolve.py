# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Every import of a ``fluid_build`` module or name resolves.

An import of something that does not exist fails silently when it sits
inside a function, behind ``try/except``, or in a string handed to
``importlib.import_module``: the ``except`` branch runs instead, and the
command reports a default as a result. That is how ``fluid contract-tests``
passed every contract without running a test, how the Command Center catalog
never connected, and how ``fluid version`` reported GCP as not installed on
a machine that had it. Ruff and the test suite were green through all of it,
because nothing imported those paths with the failure visible.

This guard reads every ``*.py`` under ``fluid_build/`` and collects:

* ``import fluid_build.x`` and ``from fluid_build.x import y``, at any depth
  (inside functions, ``try`` blocks, ``if TYPE_CHECKING``), with relative
  imports resolved against the importing module;
* string imports with a constant argument: ``importlib.import_module("...")``,
  ``__import__("...")``, and the ``_imp("module", "attr")`` helpers in
  ``cli/bootstrap.py`` and ``_contract_loader.py``;
* ``_try_register(sp, "<module>", ...)`` in ``cli/bootstrap.py``, which
  imports ``fluid_build.cli.<module>`` and swallows the ``ImportError``, so
  a misspelt module is a command that quietly never appears.

A module path built at runtime (an f-string, a lookup table) is out of reach
of a static walk; those call sites need their own tests.

A module resolves when ``importlib.util.find_spec`` finds it. A name resolves
when it is a submodule, when the target module binds it at top level, or,
failing both, when importing the module and reading the attribute succeeds.
The runtime step is what makes ``sys.modules`` aliases (the relocated
``cli/forge_copilot_*`` shims) and PEP 562 ``__getattr__`` exports resolve;
reading the source alone reports those as missing.

Approach borrowed from pylint's ``E0401``/``E0611`` (import-error,
no-name-in-module) and from the internal-import guards in jgong5/ATOM#332 and
novogratz/ai-twitter-bot#106, which make the same point this file does: a
static reading has to be backed by a real import, or a lazy export reads as
a missing one.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional, Set, Tuple

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PACKAGE = "fluid_build"

#: Call names whose first constant-string argument is a module path.
_STRING_IMPORTERS = frozenset({"import_module", "__import__", "_imp"})

#: fluid_build modules whose names can only be checked by importing them,
#: where that import needs a third-party package that is an optional extra.
#: Key: the fluid_build module. Value: why it is optional. An entry is used
#: only when the import fails on a package outside fluid_build; a module
#: that is missing, or a name that is missing, fails whatever is listed here.
OPTIONAL_THIRD_PARTY: Dict[str, str] = {}


@dataclass(frozen=True)
class ImportRef:
    """One import site: ``name`` is None for a bare module import."""

    path: str
    lineno: int
    module: str
    name: Optional[str]

    def render(self) -> str:
        target = f"{self.module}:{self.name}" if self.name else self.module
        return f"{self.path}:{self.lineno} -> {target}"


# ── collection ───────────────────────────────────────────────────────────


def _module_name(root: Path, path: Path) -> Tuple[str, bool]:
    """Dotted name of *path* under *root*, and whether it is a package."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _absolute(importer: str, is_package: bool, level: int, module: Optional[str]) -> str:
    """Resolve ``from <level dots><module> import`` against *importer*."""
    if level == 0:
        return module or ""
    base = importer.split(".")
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join(base + ([module] if module else []))


def _in_package(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _constant_str(node: Optional[ast.expr]) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(call: ast.Call) -> Optional[str]:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _refs_in_file(root: Path, path: Path, package: str) -> Iterator[ImportRef]:
    importer, is_package = _module_name(root, path)
    rel = path.relative_to(root).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _in_package(alias.name, package):
                    yield ImportRef(rel, node.lineno, alias.name, None)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(importer, is_package, node.level, node.module)
            if not _in_package(module, package):
                continue
            for alias in node.names:
                name = None if alias.name == "*" else alias.name
                yield ImportRef(rel, node.lineno, module, name)
        elif isinstance(node, ast.Call):
            fn = _call_name(node)
            if fn in _STRING_IMPORTERS and node.args:
                module_arg = _constant_str(node.args[0])
                if module_arg is None or not _in_package(module_arg, package):
                    continue
                attr = _constant_str(node.args[1]) if fn == "_imp" and len(node.args) > 1 else None
                yield ImportRef(rel, node.lineno, module_arg, attr)
            elif fn == "_try_register" and len(node.args) > 1:
                cli_module = _constant_str(node.args[1])
                if cli_module is not None:
                    yield ImportRef(rel, node.lineno, f"{package}.cli.{cli_module}", None)


def collect_refs(root: Path, package: str) -> List[ImportRef]:
    """Every import of *package* made by a module under ``root/package``."""
    refs: List[ImportRef] = []
    for path in sorted((root / package).rglob("*.py")):
        refs.extend(_refs_in_file(root, path, package))
    return refs


# ── resolution ───────────────────────────────────────────────────────────


_TRY_NODES: Tuple[type, ...] = tuple(
    t for t in (getattr(ast, "Try", None), getattr(ast, "TryStar", None)) if t is not None
)


def _rebinds_itself(tree: ast.Module) -> bool:
    """True for a ``sys.modules[__name__] = other`` shim: its own source says
    nothing about the names the import actually yields."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "modules"
                ):
                    return True
    return False


def _top_level_bindings(path: Path) -> Optional[Set[str]]:
    """Names *path* binds at module level, or None when only an import can say.

    Bindings inside top-level ``if``/``try``/``with`` blocks count: that is
    where optional imports and version shims live. A name missing from the
    set is not yet missing from the module: a star import, a module
    ``__getattr__`` or a ``sys.modules`` rebinding can still supply it, so
    the caller falls back to importing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if _rebinds_itself(tree):
        return None
    names: Set[str] = set()

    def visit(body: List[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(stmt.name)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
            elif isinstance(stmt, ast.Import):
                names.update((a.asname or a.name).split(".")[0] for a in stmt.names)
            elif isinstance(stmt, ast.ImportFrom):
                names.update(a.asname or a.name for a in stmt.names if a.name != "*")
            elif isinstance(stmt, (ast.If, ast.For, ast.While, ast.With)):
                visit(stmt.body)
                visit(getattr(stmt, "orelse", []))
            elif isinstance(stmt, _TRY_NODES):
                visit(getattr(stmt, "body", []))
                visit(getattr(stmt, "orelse", []))
                visit(getattr(stmt, "finalbody", []))
                for handler in getattr(stmt, "handlers", []):
                    visit(handler.body)

    visit(tree.body)
    return names


def _third_party_blocker(exc: BaseException, package: str) -> Optional[str]:
    """The missing package outside *package* that *exc* reports, if any."""
    missing = getattr(exc, "name", None)
    if isinstance(exc, ImportError) and missing and not _in_package(missing, package):
        return str(missing)
    return None


def _find_spec(module: str) -> Tuple[bool, Optional[BaseException]]:
    """Whether *module* exists. ``find_spec`` imports parent packages."""
    try:
        return importlib.util.find_spec(module) is not None, None
    except Exception as exc:  # noqa: BLE001 - a parent __init__ can raise anything
        return False, exc


def check_ref(ref: ImportRef, package: str, optional: Dict[str, str]) -> Optional[str]:
    """None when *ref* resolves; otherwise why it does not.

    An allow-listed module excuses only an ``ImportError`` raised while
    importing it. A module that does not exist, or a name the module does
    not have, is reported whatever the allow-list says.
    """
    exists, exc = _find_spec(ref.module)
    if exc is not None:
        if isinstance(exc, ImportError) and ref.module in optional:
            return None
        blocker = _third_party_blocker(exc, package)
        if blocker:
            return (
                f"importing a parent of {ref.module} needs {blocker!r}, which is not "
                "installed; if it is an optional extra, list the module in "
                "OPTIONAL_THIRD_PARTY with the reason"
            )
        return f"module {ref.module} does not resolve ({type(exc).__name__}: {exc})"
    if not exists:
        return f"module {ref.module} does not exist"
    if ref.name is None:
        return None

    exists, _ = _find_spec(f"{ref.module}.{ref.name}")
    if exists:
        return None
    spec = importlib.util.find_spec(ref.module)
    origin = Path(spec.origin) if spec and spec.origin and spec.has_location else None
    if origin is not None and origin.suffix == ".py":
        names = _top_level_bindings(origin)
        if names is not None and ref.name in names:
            return None
    try:
        loaded = importlib.import_module(ref.module)
    except Exception as exc:  # noqa: BLE001 - report whatever the import raised
        if isinstance(exc, ImportError) and ref.module in optional:
            return None
        blocker = _third_party_blocker(exc, package)
        if blocker:
            return (
                f"cannot check {ref.name!r}: importing {ref.module} needs {blocker!r}, "
                "which is not installed; if it is an optional extra, list the module "
                "in OPTIONAL_THIRD_PARTY with the reason"
            )
        return f"importing {ref.module} raised {type(exc).__name__}: {exc}"
    if hasattr(loaded, ref.name):
        return None
    return f"{ref.module} has no name {ref.name!r} and no submodule of that name"


def broken_refs(root: Path, package: str, optional: Dict[str, str]) -> List[Tuple[ImportRef, str]]:
    out: List[Tuple[ImportRef, str]] = []
    for ref in collect_refs(root, package):
        why = check_ref(ref, package, optional)
        if why is not None:
            out.append((ref, why))
    return out


def _format(broken: List[Tuple[ImportRef, str]]) -> str:
    return "\n".join(f"  {ref.render()}: {why}" for ref, why in broken)


# ── the guard ────────────────────────────────────────────────────────────


def test_every_fluid_build_import_resolves() -> None:
    broken = broken_refs(_REPO, _PACKAGE, OPTIONAL_THIRD_PARTY)
    assert not broken, (
        f"{len(broken)} import(s) of fluid_build modules or names that do not exist. "
        "Inside a function or a try block these fail silently and the except "
        "branch runs instead. Point each at the real module, or remove the "
        "dead path and raise a clear error:\n" + _format(broken)
    )


def test_the_scan_reaches_lazy_and_string_imports() -> None:
    """Guard the guard: a collector that sees only top-level imports passes
    exactly the defects this file exists for."""
    refs = collect_refs(_REPO, _PACKAGE)
    # Measured at 5,246 on 2026-09-28, where a walk of module-level imports
    # alone finds 3,234. The floor sits between the two.
    assert len(refs) >= 4000, f"only {len(refs)} import sites collected; collector broken?"

    lazy = [
        r
        for r in refs
        if r.path == "fluid_build/cli/version_cmd.py" and r.module == "fluid_build.providers.gcp"
    ]
    assert lazy, "the function-level, try-wrapped provider import in version_cmd.py was missed"
    registered = {r.module for r in refs if r.path == "fluid_build/cli/bootstrap.py"}
    assert "fluid_build.cli.contract_tests" in registered, "_try_register sites were missed"
    assert "fluid_build.schema_manager" in registered, "_imp(...) string imports were missed"


def test_every_allow_listed_module_exists_and_says_why() -> None:
    for module, reason in OPTIONAL_THIRD_PARTY.items():
        rel = _REPO / Path(*module.split("."))
        assert rel.with_suffix(".py").is_file() or (rel / "__init__.py").is_file(), module
        assert reason.strip(), f"{module} is allow-listed without a reason"


# ── seeded defects: the checker goes red on each shape it claims to catch ──

_SEED_FILES: Dict[str, str] = {
    "__init__.py": "",
    "real.py": """
        VALUE = 1

        def helper():
            return VALUE
    """,
    "lazy_exports.py": """
        def __getattr__(name):
            if name == "LAZY":
                return 42
            raise AttributeError(name)
    """,
    "alias_target.py": "ALIASED = True\n",
    "alias.py": """
        import sys as _sys
        from {pkg} import alias_target as _target
        _sys.modules[__name__] = _target
    """,
    "sub/__init__.py": "",
    "sub/leaf.py": "LEAF = 1\n",
    "good.py": """
        import {pkg}.real
        from {pkg}.real import VALUE, helper
        from {pkg} import sub
        from {pkg}.sub import leaf
        from .sub.leaf import LEAF

        def lazy():
            from {pkg}.lazy_exports import LAZY
            from {pkg}.alias import ALIASED
            return LAZY, ALIASED
    """,
    "bad.py": """
        import importlib

        def missing_module_in_try():
            try:
                from {pkg}.nowhere import run_tests
            except Exception:
                run_tests = None
            return run_tests

        def missing_name_in_function():
            from {pkg}.real import run_tests
            return run_tests

        def relative_to_the_wrong_package():
            from .sub._command_center import client
            return client

        def string_import():
            return importlib.import_module("{pkg}.planner")

        def _imp(mod, attr=None):
            return getattr(importlib.import_module(mod), attr) if attr else mod

        def string_import_of_a_missing_attr():
            return _imp("{pkg}.real", "emit_contract_dot")

        def _try_register(sp, module_name, profile_name):
            return None

        def register(sp):
            _try_register(sp, "misspelt_command", "misspelt")
    """,
    "cli/__init__.py": "",
}


@pytest.fixture()
def seeded_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    pkg = f"wf_import_guard_seed_{abs(hash(tmp_path)) % 10**8}"
    for rel, body in _SEED_FILES.items():
        target = tmp_path / pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body).format(pkg=pkg).lstrip(), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield pkg
    for name in [m for m in sys.modules if _in_package(m, pkg)]:
        del sys.modules[name]


def _broken_targets(root: Path, pkg: str) -> Dict[str, FrozenSet[str]]:
    out: Dict[str, Set[str]] = {}
    for ref, _ in broken_refs(root, pkg, {}):
        target = f"{ref.module}:{ref.name}" if ref.name else ref.module
        out.setdefault(Path(ref.path).name, set()).add(target.replace(pkg, "PKG"))
    return {k: frozenset(v) for k, v in out.items()}


def test_seeded_defects_each_go_red(seeded_package: str, tmp_path: Path) -> None:
    broken = _broken_targets(tmp_path, seeded_package)
    assert broken.get("bad.py") == {
        "PKG.nowhere:run_tests",
        "PKG.real:run_tests",
        "PKG.sub._command_center:client",
        "PKG.planner",
        "PKG.real:emit_contract_dot",
        "PKG.cli.misspelt_command",
    }
    assert "good.py" not in broken, f"false positives in good.py: {broken.get('good.py')}"
    assert set(broken) == {"bad.py"}, broken


def test_a_missing_third_party_parent_needs_an_allow_list_entry(
    seeded_package: str, tmp_path: Path
) -> None:
    gated = tmp_path / seeded_package / "gated"
    gated.mkdir()
    (gated / "__init__.py").write_text(
        "import wf_import_guard_no_such_dist  # noqa: F401\n", encoding="utf-8"
    )
    (gated / "impl.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / seeded_package / "uses_gated.py").write_text(
        f"from {seeded_package}.gated.impl import X\n", encoding="utf-8"
    )
    importlib.invalidate_caches()
    ref = ImportRef("uses_gated.py", 1, f"{seeded_package}.gated.impl", "X")

    why = check_ref(ref, seeded_package, {})
    assert why is not None and "wf_import_guard_no_such_dist" in why
    assert check_ref(ref, seeded_package, {ref.module: "optional extra"}) is None
