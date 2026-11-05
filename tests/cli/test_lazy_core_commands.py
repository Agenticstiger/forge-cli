# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Lazy CORE commands: ``fluid --help`` and ``fluid <cmd>`` import only what they run.

Pins ``cli/bootstrap.py::CORE_COMMANDS`` / ``register_core_commands(sp, argv)``
(the argparse adaptation of Click's ``LazyGroup``). Click's docs warn that lazy
loading hides import bugs, so every command's ``--help`` is run here, and the
static table is compared with the tree the eager path builds.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Dict, List, Tuple

import pytest

from fluid_build.cli import bootstrap, build_parser, main

CORE_NAMES = [n for e in bootstrap.CORE_COMMANDS for n in bootstrap._core_command_names(e)]


def _scrub_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "_ARGCOMPLETE"}
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env.pop(k, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _real_tree() -> (
    Tuple[argparse._SubParsersAction, Dict[str, Tuple[str, ...]], Dict[str, object]]
):
    """Core commands registered the eager way (no plugins): the source of truth."""
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd")
    orig = bootstrap._register_command_plugins
    bootstrap._register_command_plugins = lambda *_a, **_k: None  # type: ignore[assignment]
    try:
        bootstrap.register_core_commands(sp)
    finally:
        bootstrap._register_command_plugins = orig  # type: ignore[assignment]
    by_parser: Dict[int, List[str]] = {}
    for name, parser in sp.choices.items():
        by_parser.setdefault(id(parser), []).append(name)
    aliases = {names[0]: tuple(names[1:]) for names in by_parser.values()}
    helps = {ca.dest: ca.help for ca in sp._choices_actions}
    return sp, aliases, helps


def test_table_matches_the_real_registrations() -> None:
    """The static table cannot drift: names, order, aliases and help all match."""
    sp, aliases, helps = _real_tree()
    table_names = [name for e in bootstrap.CORE_COMMANDS for name, _h, _a in e.commands]
    assert table_names == [n for n in sp.choices if n in aliases], "names or order drifted"
    for entry in bootstrap.CORE_COMMANDS:
        for name, help_text, entry_aliases in entry.commands:
            assert aliases[name] == entry_aliases, f"{name}: aliases drifted"
            assert helps.get(name) == help_text, f"{name}: help drifted"


def test_every_module_in_the_table_registers_something() -> None:
    sp, aliases, _ = _real_tree()
    assert set(aliases) == {name for e in bootstrap.CORE_COMMANDS for name, _h, _a in e.commands}


def _tree_summary(sp: argparse._SubParsersAction) -> List[Tuple[str, object]]:
    return [(name, sp.choices[name].prog) for name in sp.choices] + [
        (ca.dest, ca.help) for ca in sp._choices_actions
    ]


def _subparsers_of(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def test_lazy_tree_lists_exactly_what_the_full_tree_lists() -> None:
    full = _subparsers_of(build_parser())
    lazy = _subparsers_of(build_parser(["--help"]))
    core_full = [n for n in full.choices if n in CORE_NAMES]
    assert list(lazy.choices) == core_full or list(lazy.choices)[: len(core_full)] == core_full
    assert [(c.dest, c.help) for c in lazy._choices_actions if c.dest in CORE_NAMES] == [
        (c.dest, c.help) for c in full._choices_actions if c.dest in CORE_NAMES
    ]


def test_lazy_tree_only_has_the_requested_command_for_real() -> None:
    lazy = _subparsers_of(build_parser(["--provider", "gcp", "validate", "c.yaml"]))
    real = {n for n, p in lazy.choices.items() if not p.get_default("_lazy_stub")}
    assert real == {"validate"}
    assert lazy.choices["plan"].get_default("_lazy_stub") is True


def test_global_options_before_the_command_find_it() -> None:
    for argv in (
        ["--provider", "gcp", "plan", "c.yaml"],
        ["--provider=gcp", "plan", "c.yaml"],
        ["--prov", "gcp", "plan", "c.yaml"],  # argparse accepts the abbreviation
        ["--log-level", "DEBUG", "--no-color", "plan", "c.yaml"],
    ):
        lazy = _subparsers_of(build_parser(argv))
        assert not lazy.choices["plan"].get_default("_lazy_stub"), argv


def test_a_stub_reached_anyway_is_reparsed_with_the_real_parser(monkeypatch, capsys) -> None:
    """The safety net in ``main``: a command the argv peek missed still runs."""
    real = bootstrap._requested_command
    calls: List[object] = []

    def blind_once(argv):
        calls.append(argv)
        return None if len(calls) == 1 else real(argv)

    monkeypatch.setattr(bootstrap, "_requested_command", blind_once)
    assert main(["version"]) == 0
    assert len(calls) >= 2
    assert "FLUID" in capsys.readouterr().out.upper()


def test_unknown_command_lists_the_same_choices(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["definitely-not-a-command"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    for name in ("validate", "plan", "apply", "odps", "datamesh-manager"):
        assert name in err


@pytest.mark.parametrize("cmd", CORE_NAMES)
def test_every_core_command_help_exits_zero(cmd: str, capsys) -> None:
    try:
        rc = main([cmd, "--help"])
    except SystemExit as e:
        rc = e.code or 0
    assert rc == 0, f"`fluid {cmd} --help` failed"
    assert capsys.readouterr().out.strip(), f"`fluid {cmd} --help` printed nothing"


def test_nested_subcommand_help_works(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["forge", "data-model", "from-intent", "--help"])
    assert exc.value.code == 0
    assert "from-intent" in capsys.readouterr().out


def _run(code: str) -> List[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=_scrub_env(),
    ).stdout.splitlines()


_PROBE = (
    "import sys, io, contextlib;"
    "from fluid_build.cli import main;"
    "buf = io.StringIO();"
    "ctx = contextlib.redirect_stdout(buf); ctx.__enter__();"
    "rc = main({argv!r});"
    "ctx.__exit__(None, None, None);"
    "assert rc == 0, rc;"
    "print(len(sys.modules));"
    "print(','.join(m for m in {watch!r} if m in sys.modules))"
)

# Gone from `fluid --help`: the command modules and what they pull in. (``yaml``
# is not: ``observability/config.py`` imports it, which ``cli/__init__`` loads.)
NOT_ON_HELP = (
    "pydantic",
    "fluid_build.iac.providers",
    "fluid_build.copilot",
    "fluid_build.forge_datamodel",
    "fluid_build.cli.plan",
    "fluid_build.cli.apply",
    "fluid_build.cli.validate",
    "fluid_build.cli.forge",
    "fluid_build.cli.ops",
    "fluid_build.forge.core",
)


def test_help_imports_no_command_module() -> None:
    out = _run(_PROBE.format(argv=["--help"], watch=NOT_ON_HELP))
    assert out[1] == "", f"`fluid --help` imported {out[1]}"


def test_validate_help_imports_validate_but_not_forge_or_copilot() -> None:
    watch = (
        "fluid_build.cli.validate",
        "fluid_build.cli.forge",
        "fluid_build.cli.plan",
        "fluid_build.cli.apply",
        "fluid_build.copilot",
        "fluid_build.forge_datamodel",
        "litellm",
    )
    out = _run(_PROBE.format(argv=["validate", "--help"], watch=watch))
    loaded = set(filter(None, out[1].split(",")))
    assert loaded == {"fluid_build.cli.validate"}, loaded


def test_the_global_option_tables_match_the_parser():
    # _requested_command steps past global options by these two tables; one the
    # parser gains but the tables miss would be read as the command name.
    value_opts, flag_opts = set(), set()
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        longs = {o for o in action.option_strings if o.startswith("--") and o != "--help"}
        (flag_opts if action.nargs == 0 else value_opts).update(longs)
    assert value_opts == set(bootstrap._GLOBAL_VALUE_OPTS)
    assert flag_opts == set(bootstrap._GLOBAL_FLAG_OPTS)


def test_help_on_a_fresh_home_lists_the_commands_and_creates_fluid_home(tmp_path):
    # The eager tree created ~/.fluid on every run, so the first-run panel never
    # replaced `fluid --help`; lazy commands must keep that.
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "COLUMNS": "120"}
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env.pop(var, None)
    for _ in range(2):
        done = subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", "--help"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert done.returncode == 0, done.stderr
        assert "First Time" not in done.stdout
        assert "validate" in done.stdout
    assert (home / ".fluid").is_dir()
