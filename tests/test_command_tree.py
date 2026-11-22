# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid describe --commands``: the command tree matches the live parser and lists plugins.

The tree is read from the parser, so a new command or flag lands in it without an edit.
These tests walk the parser independently and fail when the document misses a leaf, a
flag, an alias, a type or a plugin-added command or importer.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, Iterator, Optional, Tuple

import pytest

from fluid_build import plugin_manager
from fluid_build.cli import bootstrap, build_parser
from fluid_build.cli.import_workflow import registry as R
from fluid_build.describe import COMMAND_TREE_SCHEMA_VERSION, command_tree_document

pytestmark = pytest.mark.unit

_SKIP = (argparse._HelpAction, argparse._SubParsersAction)


def _walk(parser: argparse.ArgumentParser, path: Tuple[str, ...] = ()) -> Iterator[Tuple]:
    """Every (path, parser) pair, one per subparser object (aliases collapse)."""
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            seen = set()
            for name, sub in action.choices.items():
                if id(sub) not in seen:
                    seen.add(id(sub))
                    yield from _walk(sub, path + (name,))


def _node(doc: Dict[str, Any], path: Tuple[str, ...]) -> Dict[str, Any]:
    node = doc["commands"]
    for name in path:
        node = node["subcommands"][name]
    return node


@pytest.fixture
def no_plugins(monkeypatch):
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [])
    monkeypatch.setattr(plugin_manager, "_entry_points", lambda group: [])
    R.reset_importer_cache()
    yield
    R.reset_importer_cache()


def test_document_is_versioned_json(no_plugins) -> None:
    doc = json.loads(json.dumps(command_tree_document()))
    assert doc["schema_version"] == COMMAND_TREE_SCHEMA_VERSION == 1
    assert {"fluid_version", "commands", "importers"} <= set(doc)


def test_tree_matches_the_live_parser(no_plugins) -> None:
    doc = command_tree_document()
    leaves = 0
    for path, parser in _walk(build_parser()):
        node = _node(doc, path)
        leaves += 1
        flags = {n for a in parser._actions if not isinstance(a, _SKIP) for n in a.option_strings}
        positionals = {
            a.dest for a in parser._actions if not a.option_strings and not isinstance(a, _SKIP)
        }
        assert {n for o in node["options"] for n in o["names"]} == flags, path
        assert {o["dest"] for o in node["options"] if o["positional"]} == positionals, path
        for o in node["options"]:
            assert {"type", "choices", "default", "help", "nargs"} <= set(o), (path, o)
    assert leaves > 100


def test_aliases_are_recorded(no_plugins) -> None:
    doc = command_tree_document()
    sp = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    subs = doc["commands"]["subcommands"]
    for name, sub in sp.choices.items():
        owner = next(n for n, s in sp.choices.items() if s is sub)
        if owner != name:
            assert name in subs[owner]["aliases"]
            assert name not in subs


def test_flag_types_and_choices_are_reported(no_plugins) -> None:
    doc = command_tree_document()
    imp = {o["dest"]: o for o in _node(doc, ("import",))["options"]}
    assert imp["dry_run"]["type"] == "bool"
    assert imp["tool"]["positional"] is True
    assert "dbt" in imp["tool"]["choices"]
    assert imp["split_by"]["choices"]


class _CmdEP:
    name = "demo-cmd-plugin"

    def load(self):
        def register(sp):
            p = sp.add_parser("demo-plugin-cmd", help="added by a plugin")
            p.add_argument("--level", type=int, default=3, help="a level")

        return register


class _ImpEP:
    name = "demo-importer"
    dist: Optional[object] = None

    def load(self):  # pragma: no cover - the tree must never load importers
        raise AssertionError("importer plugin was loaded")


def test_plugin_command_and_importer_are_listed(monkeypatch) -> None:
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [_CmdEP()])
    monkeypatch.setattr(
        plugin_manager,
        "_entry_points",
        lambda group: [_ImpEP()] if group == "fluid_build.importers" else [],
    )
    R.reset_importer_cache()
    try:
        doc = command_tree_document()
    finally:
        R.reset_importer_cache()
    cmd = doc["commands"]["subcommands"]["demo-plugin-cmd"]
    assert cmd["plugin"] == "demo-cmd-plugin"
    assert cmd["options"][-1]["names"] == ["--level"] and cmd["options"][-1]["type"] == "int"
    assert {"name": "demo-importer", "source": "plugin"} in doc["importers"]
    assert {"name": "dbt", "source": "builtin"} in doc["importers"]
    assert "plugin" not in doc["commands"]["subcommands"]["validate"]


def test_plain_describe_does_not_load_command_plugins(monkeypatch) -> None:
    ep = _CmdEP()
    loaded = []
    monkeypatch.setattr(ep, "load", lambda: loaded.append(1) or (lambda sp: None))
    monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [ep])
    build_parser(["describe", "--self"])
    assert loaded == []


def test_real_entry_point_emits_the_tree() -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
    out = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", "describe", "--commands"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-500:]
    doc = json.loads(out.stdout)
    names = set(doc["commands"]["subcommands"])
    assert {"validate", "plan", "apply", "describe", "import"} <= names
    assert {i["name"] for i in doc["importers"]} >= {"dbt", "airflow", "pyspark"}
