# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Read pyproject's extras the way pip does, for the extras-alongside-base guard.

litellm, mcp and openlineage-python sit in ``[project] dependencies`` today
and leave the base install in 0.28.0. Until then the extras that will carry
them are declared next to base (a duplicate requirement is harmless to pip
and uv), so every consumer can switch to explicit extras first.

This module only parses; ``tests/test_ci_extras_alongside_base.py`` asserts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: The project's own name, as it appears in self-referencing extras.
SELF = "data-product-forge"

_SELF_REF = re.compile(r"^data-product-forge\[([^\]]+)\]$", re.IGNORECASE)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def base_requirements(project: dict) -> List[str]:
    return list(project["dependencies"])


def extras(project: dict) -> Dict[str, List[str]]:
    return {_norm(k): list(v) for k, v in project["optional-dependencies"].items()}


def requirement_name(requirement: str) -> str:
    """The distribution a requirement string names (marker and extras dropped)."""
    head = requirement.split(";", 1)[0].strip()
    return _norm(re.split(r"[\[<>=!~ ]", head, maxsplit=1)[0])


def closure(name: str, table: Dict[str, List[str]]) -> Set[str]:
    """Every requirement ``name`` installs, following self-referencing extras.

    Self references (``data-product-forge[a,b]``) are expanded and not
    returned; every other requirement string is returned verbatim.
    """
    seen: Set[str] = set()
    out: Set[str] = set()
    stack = [_norm(name)]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for requirement in table[current]:
            match = _SELF_REF.match(requirement.strip())
            if match:
                stack.extend(_norm(part) for part in match.group(1).split(","))
            else:
                out.add(requirement)
    return out
