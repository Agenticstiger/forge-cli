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

"""Guard the mcp 1.x/2.x rename boundary, which fails silently.

SDK 2.0 renamed model fields from camelCase to snake_case **on attribute
access**. The official migration guide is explicit that the old spelling is
still accepted as a constructor kwarg but not as an attribute, and that
``model_dump()`` without ``by_alias=True`` emits snake_case keys that peers
do not recognise, with no error raised.

Both failures are silent in the same direction: a ``getattr`` for the old
spelling returns the default, so an error reads as success and a schema reads
as empty; a wrongly-shaped dump goes out on the wire and the far side simply
does not find the fields. Neither raises, so neither shows up as a test
failure elsewhere. That is what this file is for.

The audit behind the ``mcp>=1.20,<3.0`` ceiling found zero of either. These
tests pin that result so it cannot drift back unnoticed, in the same spirit
as ``tests/perf/test_startup_budget.py``, which guards the cold-path import
budget an ordinary test cannot express.

``fluid_build/_mcp_compat.py`` is exempt: it is the one module allowed to
know about both generations, and ``attr()`` exists precisely to read both
spellings.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "fluid_build"
_SEAM = "_mcp_compat.py"

#: Renamed camelCase -> snake_case on attribute access in SDK 2.0.
#: Source: https://py.sdk.modelcontextprotocol.io/migration
RENAMED_FIELDS = frozenset(
    {
        "inputSchema",
        "outputSchema",
        "isError",
        "nextCursor",
        "mimeType",
        "structuredContent",
        "serverInfo",
        "clientInfo",
        "protocolVersion",
        "uriTemplate",
        "listChanged",
        "progressToken",
    }
)


def _imports_the_sdk(tree: ast.AST) -> bool:
    """True when the module imports the ``mcp`` SDK itself.

    ``startswith("mcp")`` rather than a substring test: forge has its own
    ``fluid_build.cli.mcp`` and ``fluid_build.output_ports.mcp`` packages, and
    a substring match silently counts those as SDK importers. Getting this
    wrong inflates the module count and makes the coverage look broader than
    it is, which is how a guard ends up guarding nothing.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mcp"):
            return True
        if isinstance(node, ast.Import) and any(a.name.startswith("mcp") for a in node.names):
            return True
    return False


def _modules_handling_sdk_objects() -> list[Path]:
    """Modules that import the SDK, plus forge's own mcp packages.

    Both matter. The SDK importers construct and read SDK models directly.
    Forge's own ``mcp`` packages pass those models around and build the
    dicts that feed them, so a renamed spelling is just as wrong there.
    """
    out: list[Path] = []
    for path in sorted(_ROOT.rglob("*.py")):
        if path.name == _SEAM:
            continue
        source = path.read_text(encoding="utf-8")
        if "mcp" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - not our problem to police
            continue
        if _imports_the_sdk(tree) or "mcp" in path.parts:
            out.append(path)
    return out


def test_the_scan_actually_finds_the_sdk_modules() -> None:
    """Guard the guard: a broken detector would pass everything vacuously."""
    modules = _modules_handling_sdk_objects()
    # Measured at 20 on 2026-09-14. The floor is deliberately below that so
    # deleting one module does not fail the suite, but high enough that a
    # broken detector returning a handful cannot pass.
    assert len(modules) >= 15, (
        f"only found {len(modules)} modules handling SDK objects; detector broken? "
        "A guard that scans nothing passes everything."
    )


@pytest.mark.parametrize("path", _modules_handling_sdk_objects(), ids=lambda p: p.name)
def test_no_attribute_read_uses_a_renamed_camelcase_spelling(path: Path) -> None:
    """``obj.isError`` returns the default on 2.x instead of raising.

    Route the read through ``_mcp_compat.attr(obj, "is_error", "isError")``,
    which tries both spellings.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        f"{path.name}:{node.lineno} .{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in RENAMED_FIELDS
    ]
    assert not offenders, (
        "renamed SDK field read by its camelCase spelling: "
        + ", ".join(offenders)
        + ". On SDK 2.x this reads as the default instead of raising. "
        "Use fluid_build._mcp_compat.attr(obj, snake, camel)."
    )


@pytest.mark.parametrize("path", _modules_handling_sdk_objects(), ids=lambda p: p.name)
def test_no_getattr_reaches_for_a_renamed_camelcase_spelling(path: Path) -> None:
    """The same failure, spelled ``getattr(obj, "isError", None)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "getattr" or len(node.args) < 2:
            continue
        target = node.args[1]
        if isinstance(target, ast.Constant) and target.value in RENAMED_FIELDS:
            offenders.append(f"{path.name}:{node.lineno} {target.value!r}")
    assert not offenders, (
        "getattr for a renamed SDK field: "
        + ", ".join(offenders)
        + ". Use fluid_build._mcp_compat.attr(obj, snake, camel)."
    )
