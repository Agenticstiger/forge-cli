# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The pin table behind every GitHub Action a generated pipeline uses.

``fluid generate ci`` writes each ``uses:`` through ``_pin_action``, which maps
``owner/repo@vN`` to a commit in ``PINNED_ACTIONS``. These pipelines land in
users' repositories, so:

* a reference the table does not hold raises; it used to be written as given,
  a tag that can move;
* every entry is one line the monthly checker (``scripts/check_pinned_actions.py``)
  can read, pinned to 40 hex, with a version comment in the key's major;
* every reference a generator pins is a key of the table, and no generator
  writes a ``uses:`` around the pin (an AST scan, so it covers branches the
  generated-output test in ``tests/test_generate_ci_keyless.py`` never takes);
* the entries refreshed on 2026-11-22 are no older than those releases.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Dict, Iterator, Tuple

import pytest

from fluid_build.forge.core.pipeline_systems import _base
from fluid_build.forge.core.pipeline_systems._base import PINNED_ACTIONS, _pin_action

ROOT = Path(__file__).resolve().parents[4]
SYSTEMS = ROOT / "fluid_build" / "forge" / "core" / "pipeline_systems"

_KEY = re.compile(r"^(?P<path>[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)@v(?P<major>\d+)$")
_VALUE = re.compile(r"^(?P<path>[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)@(?P<sha>[0-9a-f]{40})$")
#: A ``uses:`` line written into a template string, pinned to anything but a commit.
_LOOSE_USES = re.compile(r"uses:\s*[\w.-]+/[\w./-]+@(?![0-9a-f]{40}\b)\S+")


def _checker() -> ModuleType:
    path = ROOT / "scripts" / "check_pinned_actions.py"
    spec = importlib.util.spec_from_file_location("check_pinned_actions_for_pins", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _comments() -> Dict[str, str]:
    """Each entry's ``# vX.Y.Z`` comment, read as the monthly checker reads it."""
    source = (SYSTEMS / "_base.py").read_text(encoding="utf-8")
    return {
        f"{m['owner']}/{m['repo']}@{m['tag']}": m["pinned_version"]
        for m in _checker().ENTRY_RE.finditer(source)
    }


# ── _pin_action ──────────────────────────────────────────────────────────────


def test_a_reference_the_table_does_not_hold_raises() -> None:
    with pytest.raises(_base.UnpinnedActionError) as caught:
        _pin_action("actions/checkout@v99")
    assert isinstance(caught.value, LookupError)
    message = str(caught.value)
    assert "actions/checkout@v99" in message
    assert "pipeline_systems/_base.py" in message


def test_a_reference_the_table_holds_maps_to_its_commit() -> None:
    assert _pin_action("actions/checkout@v4") == PINNED_ACTIONS["actions/checkout@v4"]


# ── the table ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(PINNED_ACTIONS))
def test_every_entry_is_one_readable_line_pinned_to_a_commit(key: str) -> None:
    value = PINNED_ACTIONS[key]
    k, v = _KEY.match(key), _VALUE.match(value)
    assert k is not None and v is not None, (key, value)
    assert k["path"] == v["path"], f"{key} is pinned to another action: {value}"
    comments = _comments()
    assert key in comments, (
        f"scripts/check_pinned_actions.py cannot read {key}: keep the entry on one line, "
        "with its version in a # vX.Y.Z comment"
    )
    assert comments[key].split(".")[0] == f"v{k['major']}", (key, comments[key])


_FLOORS: Dict[str, Tuple[int, int, int]] = {
    "actions/checkout@v4": (4, 4, 0),
    "google/osv-scanner-action/osv-scanner-action@v2": (2, 7, 1),
    "github/codeql-action/upload-sarif@v3": (3, 39, 4),
    "anchore/sbom-action@v0": (0, 24, 3),
}


@pytest.mark.parametrize("key, floor", sorted(_FLOORS.items()))
def test_refreshed_entries_are_not_older_than_their_releases(
    key: str, floor: Tuple[int, int, int]
) -> None:
    version = tuple(int(part) for part in _comments()[key].lstrip("v").split("."))
    assert version >= floor, f"{key} is pinned to {_comments()[key]}"


# ── the generators ───────────────────────────────────────────────────────────


def _trees() -> Iterator[Tuple[str, ast.Module]]:
    for path in sorted(SYSTEMS.glob("*.py")):
        yield path.name, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_every_reference_a_generator_pins_is_in_the_table() -> None:
    calls = []
    for name, tree in _trees():
        for node in ast.walk(tree):
            func = node.func if isinstance(node, ast.Call) else None
            if isinstance(func, ast.Name) and func.id in ("_pin_action", "pin"):
                calls.append((name, node.lineno, node.args[0] if node.args else None))
    assert len(calls) >= 10, calls  # the scan sees the generators' calls (14 today)
    for name, line, arg in calls:
        assert isinstance(arg, ast.Constant) and isinstance(
            arg.value, str
        ), f"{name}:{line} pins a reference built at run time"
        assert arg.value in PINNED_ACTIONS, f"{name}:{line} pins {arg.value}, not in the table"


def test_no_generator_writes_uses_around_the_pin() -> None:
    around = []
    for name, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "uses"
                        and not (
                            isinstance(value, ast.Call)
                            and isinstance(value.func, ast.Name)
                            and value.func.id in ("_pin_action", "pin")
                        )
                    ):
                        around.append(f"{name}:{value.lineno}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _LOOSE_USES.search(node.value):
                    around.append(f"{name}:{node.lineno}")
    assert not around, f"a uses: not pinned through _pin_action: {around}"
