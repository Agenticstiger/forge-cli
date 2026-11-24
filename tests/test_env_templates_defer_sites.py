# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The stages that leave an unset ``{{ env.NAME }}`` for a later stage are named, and few.

Every stage that renders a contract refuses an unset variable
(:mod:`fluid_build.util.env_templates`). The only exceptions are the ones that cannot know the
variable yet, and each says so with the named mode ``UNSET_DEFER``, directly or through
``resolve_contract_env_templates``. A new stage that passes a placeholder through must be added
here, with its reason, so it is a decision someone reviews and not an accident.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List

import pytest

pytestmark = pytest.mark.unit

SOURCE = Path(__file__).resolve().parents[1] / "fluid_build"

#: Callers of the deferring resolver, and why each may leave a placeholder as written.
DEFERRING_CALLERS = {
    "build_runners/base.py": "the preload of a contract a plan embeds: each build renders it strictly when it runs",
    "cli/_export_env.py": "a catalog export shows the template of a variable this machine does not hold",
    "cli/contract_validation.py": "the validation report reads the contract; fluid validate warns about the name",
    "cli/publish.py": "a catalog shows the template of a variable this machine does not hold",
}

#: Modules that name the mode itself.
MODE_USERS = {
    "_contract_loader.py": "defines the deferring resolver the callers above use",
    "forge/core/artifact_fanout.py": (
        "generate artifacts runs where the bundle is built, which need not hold the deploy "
        "environment's variables; fluid apply renders the same contract strictly"
    ),
    "util/env_templates.py": "the resolver",
}


def _python_files() -> List[Path]:
    return sorted(p for p in SOURCE.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path) -> str:
    return path.relative_to(SOURCE).as_posix()


def test_only_the_named_stages_call_the_deferring_resolver():
    calls = re.compile(r"(?<![\w.])resolve_contract_env_templates\(")
    definition = re.compile(r"def resolve_contract_env_templates\(")
    found = {}
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        stripped = definition.sub("", text)
        if calls.search(stripped):
            found[_relative(path)] = True
    assert sorted(found) == sorted(DEFERRING_CALLERS), (
        "a stage that leaves an unset {{ env.* }} for later must be named in "
        "tests/test_env_templates_defer_sites.py with its reason"
    )


def _names_the_defer_mode(tree: ast.AST) -> bool:
    """``UNSET_DEFER`` used as a name, or ``unset="defer"`` passed to a call: code, not prose."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "UNSET_DEFER":
            return True
        if isinstance(node, ast.keyword) and node.arg == "unset":
            value = node.value
            if isinstance(value, ast.Constant) and value.value == "defer":
                return True
    return False


def test_only_the_resolver_and_its_wrapper_name_the_defer_mode():
    found = sorted(
        _relative(p)
        for p in _python_files()
        if _names_the_defer_mode(ast.parse(p.read_text(encoding="utf-8")))
    )
    assert found == sorted(MODE_USERS)
