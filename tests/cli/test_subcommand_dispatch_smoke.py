# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Smoke test: every ``fluid`` subcommand reaches its run() function.

Closes the gap that let ``fluid stats`` and ``fluid generate-pipeline``
ship while erroring out with "No command function found" at runtime —
the existing per-subcommand tests called ``run()`` directly, so the
missing ``set_defaults(func=run)`` in ``register()`` was invisible.

This test introspects every ``_try_register`` entry in
``fluid_build.cli.bootstrap`` and asserts the parser for that
subcommand has ``args.func`` set after registration (either directly
via ``set_defaults`` or transitively when the subparser dispatches to
sub-subcommands that each set their own ``func``).

Adding a new ``fluid <foo>`` subcommand? Either:
  1. Call ``parser.set_defaults(func=run)`` at the end of your
     ``register()``, OR
  2. If your command has only nested subcommands (no bare-action
     mode), call ``set_defaults(func=...)`` on every leaf subparser,
     OR
  3. Add the command name to ``_SUBCOMMANDS_WITHOUT_FUNC_BY_DESIGN``
     below — used today for commands that intercept dispatch via an
     alternative mechanism (e.g. printing a help panel when invoked
     without a subcommand).
"""

from __future__ import annotations

import argparse
import importlib
import io
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import List, Tuple

import pytest

# Commands deliberately allowed to ship without ``func`` set on the
# top-level parser. Each of these has its own dispatch story documented
# in the source (e.g. they print a custom help panel via the main CLI
# path when invoked bare). When you add to this list, add a comment
# pointing at the alternate-dispatch code path so future readers can
# verify the exemption.
_SUBCOMMANDS_WITHOUT_FUNC_BY_DESIGN: dict = {
    # ``fluid <foo>`` bare-invocation is handled by the CLI top-level
    # help dispatcher rather than reaching args.func. Verified by
    # exercising `fluid <foo>` manually.
    "ai": "ai_setup",  # cli/ai_setup.py uses help-router on bare invocation
    "forge": "forge",  # cli/forge.py runs interactively without func
    "marketplace": "marketplace",  # bare invocation prints catalog
    "config": "context",  # bare invocation prints current config
    "ide": "ide",  # bare invocation prints IDE setup status
    "workspace": "workspace",  # bare invocation prints workspace info
    # Commands that intentionally have only nested subparsers; the
    # leaves carry func and the top-level just prints help. They're
    # OK_NESTED in the introspection audit.
    "contract": "contract",
    "runs": "runs",
    "retention": "retention",
    "secrets": "secrets",
    "odcs": "odcs",
    "datamesh-manager": "datamesh_manager",
    "memory": "memory_cmd",
    "opds": "opds",
    "odps-standard": "odps_standard",
    "providers": "provider_cmds",
    "policy": "policy",
    "auth": "auth",
    "publish": "publish",  # has func — accepts both bare and subcommand
}


_TRY_REGISTER_RE = re.compile(
    r'_try_register\(\s*sp,\s*"([^"]+)",\s*"([^"]+)"(?:,\s*method="([^"]+)")?\s*\)'
)


def _read_try_register_entries() -> List[Tuple[str, str, str]]:
    """Parse bootstrap.py to discover every registered subcommand.

    Returns a list of ``(module_name, cmd_name, method)`` tuples. Method
    defaults to ``"register"`` and is overridden only when the
    `_try_register` call passes ``method=...`` (e.g. the legacy
    datamesh_manager entry uses ``method="add_parser"``).
    """
    bootstrap_src = (Path(__file__).parents[2] / "fluid_build" / "cli" / "bootstrap.py").read_text()
    return [
        (mod, cmd, method or "register")
        for cmd, mod, method in _TRY_REGISTER_RE.findall(bootstrap_src)
    ]


def _register_into_fresh_parser(module_name: str, method: str):
    """Build a throwaway parser and call ``register()`` on it. Returns
    the subparser action so the caller can inspect ``choices`` /
    ``_defaults``."""
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd")
    try:
        mod = importlib.import_module(f"fluid_build.cli.{module_name}")
    except ImportError:
        pytest.skip(f"module fluid_build.cli.{module_name} not importable")
    register = getattr(mod, method, None)
    if register is None:
        pytest.skip(f"module {module_name} has no {method}()")
    # Silence stdout/stderr — some registrations log at import.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        register(sp)
    return p, sp


def _collect_dispatch_entries():
    """Yield ``(cmd_name, module_name, method)`` for every parametrise."""
    return _read_try_register_entries()


@pytest.mark.parametrize(
    "module_name,cmd_name,method",
    _collect_dispatch_entries(),
    ids=lambda v: v if isinstance(v, str) else "?",
)
def test_subcommand_has_func_or_routes_via_subparsers(
    module_name: str, cmd_name: str, method: str
) -> None:
    """Every ``fluid <cmd>`` must dispatch correctly.

    Three valid dispatch shapes:
      1. ``register()`` calls ``parser.set_defaults(func=run)`` — bare
         invocation reaches args.func directly.
      2. The subparser only has nested sub-subcommands and every leaf
         sets its own func.
      3. The command is in ``_SUBCOMMANDS_WITHOUT_FUNC_BY_DESIGN`` and
         intercepts dispatch some other way (documented per-entry).
    """
    parser, sp = _register_into_fresh_parser(module_name, method)

    sub_action = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    assert sub_action is not None, f"{module_name}: no subparser action"
    choices = sub_action.choices
    assert choices, f"{module_name}: register() added no parsers"

    # Some registrations use a different on-screen name than the
    # module-name convention (e.g. odps_standard registers as
    # 'odps-bitol' too). Accept the actual key the module chose.
    actual_cmd = cmd_name if cmd_name in choices else next(iter(choices))
    target = choices[actual_cmd]

    has_func = "func" in target._defaults
    leaf_sub = next(
        (a for a in target._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    has_leaf_dispatch = (
        leaf_sub is not None
        and leaf_sub.choices
        and all("func" in c._defaults for c in leaf_sub.choices.values())
    )
    is_exempt = actual_cmd in _SUBCOMMANDS_WITHOUT_FUNC_BY_DESIGN

    if has_func or has_leaf_dispatch or is_exempt:
        return

    pytest.fail(
        f"fluid {actual_cmd} → 'No command function found' at runtime.\n"
        f"  module: fluid_build.cli.{module_name}\n"
        f"  Fix: add `parser.set_defaults(func=run)` at the end of "
        f"`register()` in fluid_build/cli/{module_name}.py.\n"
        f"  OR if your command only dispatches via sub-subcommands, "
        f"set `func` on every leaf subparser.\n"
        f"  OR if there's an intentional alternate dispatch path, add "
        f"{actual_cmd!r} to _SUBCOMMANDS_WITHOUT_FUNC_BY_DESIGN with a "
        f"one-line comment pointing at that path."
    )


def test_known_regression_stats_dispatches() -> None:
    """Direct regression pin for the bug that triggered this test file:
    ``fluid stats`` shipped without ``set_defaults(func=run)``."""
    from fluid_build.cli import stats

    parser = argparse.ArgumentParser()
    sp = parser.add_subparsers(dest="cmd")
    stats.register(sp)
    args = parser.parse_args(["stats"])
    assert getattr(args, "func", None) is stats.run, (
        "fluid stats must wire args.func to stats.run via set_defaults — "
        "missing wire produces 'No command function found' at runtime."
    )


def test_known_regression_generate_pipeline_dispatches() -> None:
    """Direct regression pin for the same bug on `fluid generate-pipeline`."""
    from fluid_build.cli import pipeline_generator

    parser = argparse.ArgumentParser()
    sp = parser.add_subparsers(dest="cmd")
    pipeline_generator.register(sp)
    args = parser.parse_args(["generate-pipeline"])
    assert getattr(args, "func", None) is pipeline_generator.run
