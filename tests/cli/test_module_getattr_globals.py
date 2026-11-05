# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A module-level ``__getattr__`` (PEP 562) serves ``module.NAME`` only.

A function in the same module that reads such a name as a bare global gets
NameError at run time, because a global lookup never consults the module's
``__getattr__``. Two did: the forge interview's data-modeling question
(``_DATA_VAULT_2_ALIASES``) and ``fluid ai test`` with no provider configured
(``detect_ollama_available``).
"""

from __future__ import annotations

import argparse
import builtins
import dis
import importlib
import re
import types
from pathlib import Path

import pytest

import fluid_build

_PACKAGE = Path(fluid_build.__file__).parent
_HAS_GETATTR = re.compile(r"^def __getattr__\(", re.M)


def _modules_with_module_getattr() -> list[str]:
    names = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if _HAS_GETATTR.search(path.read_text(encoding="utf-8")):
            rel = path.relative_to(_PACKAGE.parent).with_suffix("")
            names.append(".".join(rel.parts).removesuffix(".__init__"))
    return names


def _unresolvable_globals(module: types.ModuleType) -> dict[str, set[str]]:
    """Bare global names a function of *module* reads that the module cannot supply."""
    namespace = vars(module)
    loads: dict[str, set[str]] = {}
    stored: set[str] = set()  # assigned at run time through a ``global`` statement

    def walk(code: types.CodeType, owner: str) -> None:
        for ins in dis.get_instructions(code):
            if not isinstance(ins.argval, str):
                continue
            if ins.opname == "LOAD_GLOBAL":
                loads.setdefault(ins.argval, set()).add(owner)
            elif ins.opname == "STORE_GLOBAL":
                stored.add(ins.argval)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                walk(const, owner)

    def visit(obj: object, owner: str) -> None:
        func = getattr(obj, "__func__", obj)
        # Only functions written in this file run with this module's globals
        # (a decorator's wrapper or a generated dataclass method does not).
        if isinstance(func, types.FunctionType) and func.__globals__ is namespace:
            walk(func.__code__, owner)

    for name, obj in list(namespace.items()):
        if isinstance(obj, type) and obj.__module__ == module.__name__:
            for attr, value in vars(obj).items():
                visit(value, f"{name}.{attr}")
        else:
            visit(obj, name)
    return {
        name: owners
        for name, owners in loads.items()
        if name not in namespace and name not in stored and not hasattr(builtins, name)
    }


@pytest.mark.parametrize("module_name", _modules_with_module_getattr())
def test_no_function_reads_a_name_only_getattr_could_supply(module_name: str) -> None:
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # an optional extra this environment lacks
        pytest.skip(f"{module_name} needs an optional package: {exc}")
    assert _unresolvable_globals(module) == {}


def test_ai_test_with_no_provider_reports_it_instead_of_crashing(monkeypatch) -> None:
    from fluid_build.cli import _ai_setup_test, ai_setup

    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(ai_setup, "_load_ai_config", lambda *a, **k: None)
    monkeypatch.setattr(ai_setup, "detect_ollama_available", lambda *a, **k: False, raising=False)

    config, error = _ai_setup_test._resolve_ai_test_config(argparse.Namespace())
    assert config is None
    assert "No AI provider configured" in error
