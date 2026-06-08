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

"""
Library-level self-description for the FLUID forge engine.
Importable as: from fluid_build.describe import self_describe
Used by the CC backend to serve GET /api/v1/forge/capabilities.

Pattern adapted from `pulumi about --json` (flat object, top-level keys
per category). See borrow-before-build receipts in the feat/forge-cc-alignment
PR.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from typing import Any, Dict, List

try:
    import fluid_build

    _fluid_version = fluid_build.__version__
except Exception:
    _fluid_version = "unknown"

# Each capability flag is *derived* from whether its backing module is
# importable in this installation — never asserted as a constant. Mirrors
# pulumi `about`, which discovers plugins/runtime rather than shipping a
# static table that drifts from reality. Add a row here when a new
# capability ships a discoverable module.
_CAPABILITY_MODULES: Dict[str, str] = {
    "lineage": "fluid_build.api.lineage",
    "airflow_dag_gen": "fluid_build.cli._init_dag_helpers",
    "engine_api": "fluid_build.engine",
}


def _detect_capabilities() -> Dict[str, bool]:
    detected: Dict[str, bool] = {}
    for name, module in _CAPABILITY_MODULES.items():
        try:
            detected[name] = importlib.util.find_spec(module) is not None
        except Exception:
            detected[name] = False
    return detected


def _describe_action(action: argparse.Action) -> Dict[str, Any]:
    """One argparse option/positional -> a JSON-safe flag descriptor."""
    default = action.default
    if default is argparse.SUPPRESS or not isinstance(default, (str, int, float, bool, type(None))):
        default = None if default is argparse.SUPPRESS else str(default)
    return {
        "names": list(action.option_strings),  # [] for a positional
        "dest": action.dest,
        "positional": not action.option_strings,
        "required": bool(getattr(action, "required", False)),
        "help": action.help or "",
        "choices": [str(c) for c in action.choices] if action.choices else None,
        "default": default,
        "metavar": action.metavar,
    }


def _command_tree(parser: argparse.ArgumentParser, _depth: int = 0) -> Dict[str, Any]:
    """Introspect an argparse parser into a ``{options, subcommands}`` tree.

    Walks ``parser._actions`` for flags and the ``_SubParsersAction`` for
    subcommands — the standard argparse-introspection idiom (the same parser
    internals shtab / argparse-manpage read; we avoid a jsonargparse dependency
    to keep the CLI lightweight). Read-only: no parsing/side effects. Aliases
    (several names -> one subparser) are de-duplicated under ``aliases``.
    """
    options: List[Dict[str, Any]] = []
    subcommands: Dict[str, Any] = {}
    if _depth > 8:  # defensive recursion guard
        return {"options": options, "subcommands": subcommands}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: (a.help or "") for a in (action._choices_actions or [])}
            seen: Dict[int, str] = {}
            for name, sub in action.choices.items():
                if id(sub) in seen:
                    subcommands[seen[id(sub)]].setdefault("aliases", []).append(name)
                    continue
                seen[id(sub)] = name
                subcommands[name] = {
                    "help": helps.get(name, ""),
                    **_command_tree(sub, _depth + 1),
                }
        elif isinstance(action, argparse._HelpAction):
            continue
        else:
            options.append(_describe_action(action))
    return {"options": options, "subcommands": subcommands}


def self_describe() -> Dict[str, Any]:
    """
    Return a machine-readable description of this forge-cli installation.

    Pattern adapted from `pulumi about --json` (flat object, top-level keys
    per category). See borrow-before-build receipts.

    Returns a dict with stable keys:
      - fluid_version: str
      - python_version: str
      - schema_version: str
      - providers: list[str]
      - build_engines: list[str]
      - templates: list[str]
      - provider_engine_compatibility: dict
      - capabilities: dict[str, bool]
      - commands: dict (argparse command tree — per-command options + nested
        subcommands, so the CC can render command/flag UI dynamically and
        never lag the CLI)
      - warnings: list[str]
    """
    from fluid_build.schema_manager import FluidSchemaManager

    try:
        from fluid_build.cli.forge_copilot_runtime import build_capability_matrix

        matrix = build_capability_matrix()
    except Exception:
        matrix = {
            "providers": [],
            "templates": {},
            "build_engines": [],
            "warnings": ["capability matrix unavailable"],
        }

    try:
        from fluid_build.cli import build_parser

        commands = _command_tree(build_parser())
    except Exception as exc:  # never let describe crash on a parser issue
        commands = {"options": [], "subcommands": {}, "error": str(exc)[:160]}

    return {
        "fluid_version": _fluid_version,
        "commands": commands,
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "schema_version": FluidSchemaManager.latest_bundled_version(),
        "providers": matrix.get("providers", []),
        "build_engines": matrix.get("build_engines", []),
        "templates": list(matrix.get("templates", {}).keys()),
        "provider_engine_compatibility": matrix.get("provider_engine_compatibility", {}),
        "capabilities": _detect_capabilities(),
        "warnings": matrix.get("warnings", []),
    }
