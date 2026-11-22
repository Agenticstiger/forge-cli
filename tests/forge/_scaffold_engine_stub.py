# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A stand-in for the custom-scaffold engine's library entry point, for tests.

The real engine (``data-product-forge-custom-scaffold``) is an optional extra, so CI does
not install it. :func:`install` writes this package to a directory and returns it for
``sys.path`` (or a subprocess's ``PYTHONPATH``). It has the engine's 0.5.0 surface that
``fluid generate artifacts`` uses: ``generate()``, ``GenerationResult`` / ``GeneratedFile``,
``validate()`` and the ``EngineError`` tree.

What a run writes comes from the contract: each pattern's ``variables.files`` maps a path
to its text. ``HOOKS`` (a module attribute) lets a test bend the engine in-process:

* ``validate_errors``: strings ``validate()`` appends;
* ``raise``: the name of an error class ``generate()`` raises, after writing one file;
* ``raise_message``: that error's message;
* ``after``: a callable ``(out_dir)`` run after the files are written (plant a link, an
  unreported file);
* ``report``: a callable ``(rows) -> rows`` that changes what ``generate()`` reports;
* ``calls``: a list each call's ``(out_dir, contract_dir)`` is appended to.
"""

from __future__ import annotations

from pathlib import Path

NAME = "data_product_forge_custom_scaffold"

SOURCE = '''
"""Test stand-in for the custom-scaffold engine (see tests/forge/_scaffold_engine_stub.py)."""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Tuple

__version__ = "0.5.0"
ENGINE_VERSION = __version__
HOOKS = {}
DEFAULT_DIALECT = object()


class EngineError(RuntimeError):
    pass


class ContractError(EngineError):
    pass


class BundleError(EngineError):
    pass


class PlanError(EngineError):
    pass


class RenderError(EngineError):
    pass


class UnsafePathError(RenderError):
    pass


class ApplyError(EngineError):
    def __init__(self, message, *, run=None):
        super().__init__(message)
        self.run = run


class OutputError(EngineError):
    def __init__(self, message, *, run=None):
        super().__init__(message)
        self.run = run


@dataclass(frozen=True)
class GeneratedFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class GenerationResult:
    files: Tuple[GeneratedFile, ...]
    dry_run: bool
    run: Any = field(default=None, compare=False, repr=False)


def validate(extensions, errors):
    block = extensions.get("customScaffold") if isinstance(extensions, dict) else None
    if block is None:
        return
    if not isinstance(block, dict) or not isinstance(block.get("libraries"), list):
        errors.append("extensions.customScaffold.<root>: libraries is required")
    errors.extend(HOOKS.get("validate_errors", []))


def _row(root, rel):
    data = (Path(root) / rel).read_bytes()
    return GeneratedFile(rel, hashlib.sha256(data).hexdigest(), len(data))


def generate(contract, out_dir, *, contract_dir=None, dry_run=False, pattern_filter=None,
             library_filter=None, pin=False, dialect=DEFAULT_DIALECT):
    HOOKS.setdefault("calls", []).append((str(out_dir), None if contract_dir is None else str(contract_dir)))
    block = (contract.get("extensions") or {}).get("customScaffold")
    if not block:
        return GenerationResult((), dry_run)
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    written = []
    for pattern in block.get("patterns") or []:
        for rel, text in sorted(((pattern.get("variables") or {}).get("files") or {}).items()):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(rel)
            if HOOKS.get("raise"):
                raise globals()[HOOKS["raise"]](HOOKS.get("raise_message", "stub failure"))
    lock = "# fluid-scaffold.lock (stub)\\nlibraries: {}\\n"
    (root / "fluid-scaffold.lock").write_text(lock, encoding="utf-8")
    written.append("fluid-scaffold.lock")
    if HOOKS.get("after"):
        HOOKS["after"](root)
    rows = tuple(_row(root, rel) for rel in sorted(set(written)))
    if HOOKS.get("report"):
        rows = tuple(HOOKS["report"](rows))
    return GenerationResult(rows, dry_run)
'''


#: The submodule an installed engine's ``fluid_build.extension_validators`` and
#: ``fluid_build.extension_schemas`` entry points name, so ``fluid validate`` still finds
#: them when the stand-in shadows an installed engine.
VALIDATION = '''
"""Test stand-in for the engine's validation module."""

from data_product_forge_custom_scaffold import validate  # noqa: F401


def get_extension_schema(fluid_version=None):
    return {"type": "object"}
'''


def install(root: Path) -> Path:
    """Write the stand-in package under ``root`` and return ``root`` (for ``sys.path``)."""
    package = root / NAME
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(SOURCE, encoding="utf-8")
    (package / "validation.py").write_text(VALIDATION, encoding="utf-8")
    return root
