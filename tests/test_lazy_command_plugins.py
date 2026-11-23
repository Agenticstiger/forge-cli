# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Lazy loading of ``fluid_build.commands`` CLI-command plugins.

Regression suite for ``cli/bootstrap.py::_register_command_plugins`` — the
argparse adaptation of Click's ``LazyGroup`` recipe. A third-party command
plugin's module is imported, and its entry-point callable ``(subparsers) -> None``
run, ONLY when its command is actually being invoked, so a heavy module-scope import a plugin happens to
have (e.g. ``jsonschema``) never lands on the ``fluid --help`` /
``build_parser()`` cold path (tests/perf/test_startup_budget.py).

Pinned invariants:

* (a) the plugin module is NOT imported after ``build_parser()`` when its
  subcommand is not selected (``--help`` / a core command / no command);
* (b) the plugin IS imported, registered, and dispatchable when its
  subcommand is invoked — under BOTH the ``func(args)`` and
  ``func(args, logger)`` calling conventions;
* (c) a broken plugin entry point is skipped with a type-only WARNING and
  the CLI still builds (fail-isolation), and discovery failures never raise;
* (d) a plugin adds new command names only: one a core command, a core alias or
  another plugin already has is refused, the same on every Python, and the plugin is
  skipped whole;
* plus argv parsing, operator allow/block governance, and the import-free
  ``fluid --help`` plugin listing.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from typing import Any, Callable, List, Tuple

import pytest

from fluid_build.cli import bootstrap

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeEP:
    """Stand-in for ``importlib.metadata.EntryPoint``.

    ``load()`` records that it ran and returns ``target`` (or raises it, if
    ``target`` is an exception). ``target`` is the plugin ``register`` callable.
    """

    def __init__(self, name: str, target: Any) -> None:
        self.name = name
        self._target = target
        self.loaded = False

    def load(self) -> Any:
        self.loaded = True
        if isinstance(self._target, BaseException):
            raise self._target
        return self._target


def _fresh_parser(core: Tuple[str, ...] = ()) -> Tuple[argparse.ArgumentParser, Any]:
    """A minimal parser + subparsers action pre-seeded with core command names."""
    parser = argparse.ArgumentParser(prog="fluid")
    sp = parser.add_subparsers(dest="cmd")
    for name in core:
        sp.add_parser(name)
    return parser, sp


def _cmd_choices(parser: argparse.ArgumentParser) -> dict:
    for action in parser._actions:
        if getattr(action, "dest", None) == "cmd" and isinstance(
            getattr(action, "choices", None), dict
        ):
            return action.choices
    return {}


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        self.records.append(record)


def _capture_bootstrap_log() -> _Capture:
    """Attach a capture handler to the ``fluid.cli`` logger the loader uses."""
    cap = _Capture()
    bootstrap.LOG.addHandler(cap)
    bootstrap.LOG.setLevel(logging.DEBUG)
    return cap


# ---------------------------------------------------------------------------
# _requested_command — argv peek that decides lazy vs. materialize
# ---------------------------------------------------------------------------


class TestRequestedCommand:
    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["--help"], None),
            (["-h"], None),
            (["help"], None),
            (["--version"], None),
            ([], None),
            (["validate", "contract.yaml"], "validate"),
            (["custom-scaffold", "--print-schema"], "custom-scaffold"),
            # global value-taking option + separate value is stepped over
            (["--provider", "gcp", "custom-scaffold"], "custom-scaffold"),
            (["--log-level", "DEBUG", "plan"], "plan"),
            # ``--opt=value`` form does not consume a following token
            (["--provider=gcp", "apply"], "apply"),
            # boolean/flag globals are single tokens
            (["--no-color", "doctor"], "doctor"),
            (["--debug", "version"], "version"),
        ],
    )
    def test_requested_command(self, argv: List[str], expected: Any) -> None:
        assert bootstrap._requested_command(argv) == expected


# ---------------------------------------------------------------------------
# (a) NOT imported on the cold paths
# ---------------------------------------------------------------------------


class TestLazyNoImportOnColdPath:
    def test_not_loaded_on_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ep = FakeEP("my-plugin", lambda sp: sp.add_parser("my-plugin"))
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(sys, "argv", ["fluid", "--help"])

        parser, sp = _fresh_parser(core=("validate",))
        bootstrap._register_command_plugins(sp)

        assert ep.loaded is False, "plugin must NOT be imported on the --help path"
        assert "my-plugin" not in _cmd_choices(parser)

    def test_not_loaded_on_core_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ep = FakeEP("my-plugin", lambda sp: sp.add_parser("my-plugin"))
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(sys, "argv", ["fluid", "validate", "contract.yaml"])

        _parser, sp = _fresh_parser(core=("validate",))
        bootstrap._register_command_plugins(sp)

        assert ep.loaded is False, "a core command must not trigger a plugin import"

    def test_not_loaded_on_no_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ep = FakeEP("my-plugin", lambda sp: sp.add_parser("my-plugin"))
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(sys, "argv", ["fluid"])

        _parser, sp = _fresh_parser(core=("validate",))
        bootstrap._register_command_plugins(sp)

        assert ep.loaded is False


# ---------------------------------------------------------------------------
# (b) imported + registered + dispatchable when invoked
# ---------------------------------------------------------------------------


class TestLazyLoadOnInvoke:
    def test_loaded_and_registered_when_invoked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def register(sp: Any) -> None:
            sp.add_parser("my-plugin").set_defaults(func=lambda args: 0)

        ep = FakeEP("my-plugin", register)
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(sys, "argv", ["fluid", "my-plugin", "--flag"])

        parser, sp = _fresh_parser(core=("validate",))
        bootstrap._register_command_plugins(sp)

        assert ep.loaded is True
        assert "my-plugin" in _cmd_choices(parser)

    def test_one_arg_plugin_func_dispatches_under_two_arg_convention(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Click-style ``func(args)`` plugin must dispatch under the CLI's
        ``func(args, logger)`` convention (the pre-existing crash this fixes)."""
        seen: dict = {}

        def register(sp: Any) -> None:
            # One-arg func, exactly like data-product-forge-custom-scaffold.
            sp.add_parser("one-arg").set_defaults(func=lambda args: seen.update(args=args) or 7)

        ep = FakeEP("one-arg", register)
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(sys, "argv", ["fluid", "one-arg"])

        parser, sp = _fresh_parser(core=("validate",))
        bootstrap._register_command_plugins(sp)

        ns = parser.parse_args(["one-arg"])
        # The CLI dispatcher always calls func(args, logger) — this must not
        # raise "takes 1 positional argument but 2 were given".
        rc = ns.func(ns, logging.getLogger("test"))
        assert rc == 7
        assert seen["args"] is ns

    def test_two_arg_plugin_func_still_receives_logger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict = {}

        def register(sp: Any) -> None:
            def run(args: Any, logger: Any) -> int:
                seen["args"], seen["logger"] = args, logger
                return 0

            sp.add_parser("two-arg").set_defaults(func=run)

        ep = FakeEP("two-arg", register)
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(sys, "argv", ["fluid", "two-arg"])

        parser, sp = _fresh_parser(core=("validate",))
        bootstrap._register_command_plugins(sp)

        ns = parser.parse_args(["two-arg"])
        log = logging.getLogger("test")
        ns.func(ns, log)
        assert seen["args"] is ns and seen["logger"] is log

    def test_nested_subcommand_func_is_adapted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plugins that register nested subcommands get their leaf funcs
        adapted too (recursive wrap)."""
        seen: dict = {}

        def register(sp: Any) -> None:
            top = sp.add_parser("group")
            nested = top.add_subparsers(dest="sub")
            nested.add_parser("leaf").set_defaults(
                func=lambda args: seen.setdefault("hit", True) or 0
            )

        ep = FakeEP("group", register)
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(sys, "argv", ["fluid", "group", "leaf"])

        parser, sp = _fresh_parser(core=("validate",))
        bootstrap._register_command_plugins(sp)

        ns = parser.parse_args(["group", "leaf"])
        ns.func(ns, logging.getLogger("test"))
        assert seen.get("hit") is True


# ---------------------------------------------------------------------------
# Signature adapter unit
# ---------------------------------------------------------------------------


class TestFuncAdapter:
    def test_one_arg(self) -> None:
        calls: List[Any] = []
        w = bootstrap._adapt_plugin_command_func(lambda args: calls.append(args) or 3)
        assert w("A", "LOGGER") == 3
        assert calls == ["A"]

    def test_two_arg(self) -> None:
        calls: List[Tuple[Any, Any]] = []

        def fn(args: Any, logger: Any) -> int:
            calls.append((args, logger))
            return 0

        bootstrap._adapt_plugin_command_func(fn)("A", "L")
        assert calls == [("A", "L")]

    def test_varargs_gets_both(self) -> None:
        w = bootstrap._adapt_plugin_command_func(lambda *a: len(a))
        assert w("A", "L") == 2

    def test_marks_adapted_to_avoid_double_wrap(self) -> None:
        w = bootstrap._adapt_plugin_command_func(lambda args: 0)
        assert getattr(w, "__fluid_adapted__", False) is True

    def test_wrap_is_idempotent(self) -> None:
        """An already-adapted func on a parser is not re-wrapped."""
        parser = argparse.ArgumentParser()
        adapted = bootstrap._adapt_plugin_command_func(lambda args: 0)
        parser.set_defaults(func=adapted)
        bootstrap._wrap_plugin_parser_funcs(parser)
        assert parser._defaults["func"] is adapted


# ---------------------------------------------------------------------------
# (c) fail-isolation
# ---------------------------------------------------------------------------


class TestFailIsolation:
    def test_broken_plugin_skipped_others_still_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
        good = FakeEP("good", lambda sp: sp.add_parser("good"))
        # A secret-shaped exception message must NEVER reach the log handler.
        bad = FakeEP("bad", ImportError("boom password=hunter2"))
        # ``bad`` first so we prove a failure doesn't drop the rest.
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [bad, good])
        monkeypatch.setattr(sys, "argv", ["fluid", "good"])

        cap = _capture_bootstrap_log()
        try:
            parser, sp = _fresh_parser(core=("validate",))
            bootstrap._register_command_plugins(sp)  # must not raise
        finally:
            bootstrap.LOG.removeHandler(cap)

        assert "good" in _cmd_choices(parser), "a good plugin must still load after a bad one"
        msgs = [r.getMessage() for r in cap.records]
        assert any("Failed to load CLI plugin bad" in m and "ImportError" in m for m in msgs)
        # Type-only logging: the plugin-supplied exception text (and its
        # embedded secret) must never be interpolated into the log line.
        assert not any("hunter2" in m for m in msgs)

    def test_register_call_failure_is_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def crashing_register(_sp: Any) -> None:
            raise RuntimeError("register blew up")

        ep = FakeEP("crashy", crashing_register)
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [ep])
        monkeypatch.setattr(sys, "argv", ["fluid", "crashy"])

        cap = _capture_bootstrap_log()
        try:
            _parser, sp = _fresh_parser(core=("validate",))
            bootstrap._register_command_plugins(sp)  # must not raise
        finally:
            bootstrap.LOG.removeHandler(cap)

        msgs = [r.getMessage() for r in cap.records]
        assert any("Failed to load CLI plugin crashy" in m and "RuntimeError" in m for m in msgs)
        assert not any("register blew up" in m for m in msgs)  # type-only

    def test_discovery_failure_is_isolated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> List[Any]:
            raise RuntimeError("discovery exploded")

        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", boom)
        monkeypatch.setattr(sys, "argv", ["fluid", "whatever"])

        cap = _capture_bootstrap_log()
        try:
            _parser, sp = _fresh_parser(core=("validate",))
            bootstrap._register_command_plugins(sp)  # must not raise
        finally:
            bootstrap.LOG.removeHandler(cap)

        msgs = [r.getMessage() for r in cap.records]
        assert any("CLI plugin discovery failed" in m and "RuntimeError" in m for m in msgs)


# ---------------------------------------------------------------------------
# Operator allow/block governance (names only, no import)
# ---------------------------------------------------------------------------


class TestGovernance:
    def test_entry_points_respect_blocklist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata as md

        fakes = [FakeEP("keep", object()), FakeEP("drop", object())]

        def fake_eps(group: Any = None, **_: Any) -> List[Any]:
            return list(fakes) if group == "fluid_build.commands" else []

        monkeypatch.setattr(md, "entry_points", fake_eps)
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "drop")

        names = [ep.name for ep in bootstrap._command_plugin_entry_points()]
        assert "keep" in names
        assert "drop" not in names
        # Neither is imported by discovery.
        assert all(getattr(ep, "loaded", False) is False for ep in fakes)

    def test_entry_points_respect_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata as md

        fakes = [FakeEP("keep", object()), FakeEP("other", object())]
        monkeypatch.setattr(
            md,
            "entry_points",
            lambda group=None, **_: (list(fakes) if group == "fluid_build.commands" else []),
        )
        monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "keep")

        names = [ep.name for ep in bootstrap._command_plugin_entry_points()]
        assert names == ["keep"]


# ---------------------------------------------------------------------------
# fluid --help lists installed plugins by name (import-free)
# ---------------------------------------------------------------------------


class TestHelpListing:
    def test_main_help_lists_command_plugins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from io import StringIO
        from unittest.mock import patch

        from fluid_build.cli.help_formatter import RICH_AVAILABLE

        if not RICH_AVAILABLE:
            pytest.skip("Rich required")

        from rich.console import Console

        # Import-free listing: patch installed_plugins so we don't depend on
        # the ambient environment. It reads entry-point NAMES only.
        import fluid_build.plugin_manager as pm
        from fluid_build.cli import help_formatter
        from fluid_build.cli.help_formatter import print_main_help

        monkeypatch.setattr(
            pm,
            "installed_plugins",
            lambda role=None: {
                "command": [
                    {"name": "acme-widget", "group": "fluid_build.commands", "allowed": True},
                    {"name": "blocked-one", "group": "fluid_build.commands", "allowed": False},
                ]
            },
        )

        parser = argparse.ArgumentParser(prog="fluid")
        parser.add_subparsers(dest="cmd")
        buf = StringIO()
        with patch.object(
            help_formatter, "Console", return_value=Console(file=buf, width=120, no_color=True)
        ):
            print_main_help(parser)
        out = buf.getvalue()

        assert "Plugins" in out
        assert "acme-widget" in out, "allowed command plugin must be listed by name"
        assert "blocked-one" not in out, "blocked plugins must not be listed"


# ---------------------------------------------------------------------------
# Integration: the real data-product-forge-custom-scaffold plugin (if present)
# ---------------------------------------------------------------------------


class TestRealScaffoldPluginIntegration:
    """End-to-end against the actually-installed plugin, if available.

    Skips cleanly when the plugin isn't installed (e.g. CI's lean env), so the
    suite stays green everywhere; asserts real lazy behavior where it IS
    installed (a developer/user who ``pip install``ed it alongside fluid)."""

    def _plugin_installed(self) -> bool:
        import importlib.util

        return importlib.util.find_spec("data_product_forge_custom_scaffold") is not None

    def test_real_plugin_registers_only_when_invoked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not self._plugin_installed():
            pytest.skip("data-product-forge-custom-scaffold not installed")

        from fluid_build.cli import build_parser

        # --help path: the plugin's ``custom-scaffold`` subcommand is NOT a
        # registered choice (its module was never imported).
        monkeypatch.setattr(sys, "argv", ["fluid", "--help"])
        assert "custom-scaffold" not in _cmd_choices(build_parser())

        # invoking it: the real subparser is materialized on demand.
        monkeypatch.setattr(sys, "argv", ["fluid", "custom-scaffold", "--print-schema"])
        choices = _cmd_choices(build_parser())
        assert "custom-scaffold" in choices
        # And its func was adapted to the CLI dispatch convention.
        assert callable(choices["custom-scaffold"]._defaults.get("func"))


# Convenience: pinning the module-level constant that keeps ``_requested_command``
# in sync with the global options declared in cli/__init__.py::build_parser.
def test_global_value_opts_cover_declared_value_options() -> None:
    expected = {"--log-level", "--log-file", "--provider", "--project", "--region", "--config-dir"}
    assert set(bootstrap._GLOBAL_VALUE_OPTS) == expected


# Silence unused-import lints for helpers referenced only via fixtures.
_ = (Callable,)


# ---------------------------------------------------------------------------
# (d) a plugin adds new command names only
# ---------------------------------------------------------------------------


def _core_parser() -> Tuple[argparse.ArgumentParser, Any, argparse.ArgumentParser]:
    """A parser with two core commands (one with an alias and a help line each) and the parser
    of ``apply``, which a shadowing plugin would replace."""
    parser = argparse.ArgumentParser(prog="fluid")
    sp = parser.add_subparsers(dest="cmd")
    core_apply = sp.add_parser("apply", help="Apply a plan")
    core_apply.add_argument("--dry-run", action="store_true")
    sp.add_parser("validate", help="Validate a contract", aliases=["check"])
    return parser, sp, core_apply


def _run_loader(
    monkeypatch: pytest.MonkeyPatch,
    sp: Any,
    plugins: List[FakeEP],
    argv: List[str],
    **kwargs: Any,
) -> Tuple[dict, List[str]]:
    """``_register_command_plugins(sp, **kwargs)`` with ``plugins`` installed and ``argv``
    typed: ``(what it returned, the messages it logged)``."""
    monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: list(plugins))
    monkeypatch.setattr(sys, "argv", argv)
    cap = _capture_bootstrap_log()
    try:
        added = bootstrap._register_command_plugins(sp, **kwargs)
    finally:
        bootstrap.LOG.removeHandler(cap)
    return added, [r.getMessage() for r in cap.records]


def _shadow_messages(messages: List[str]) -> List[str]:
    return [m for m in messages if "cli_plugin_command_shadow" in m]


def _help_names(sp: Any) -> List[str]:
    return [action.dest for action in sp._choices_actions]


def _add_parser_py310(self: Any, name: str, **kwargs: Any) -> argparse.ArgumentParser:
    """``_SubParsersAction.add_parser`` as Python 3.10 has it: no check for a name that is
    already taken, so the second ``add_parser`` of a name replaces the first without a word.
    (3.11 and later raise ``ArgumentError: conflicting subparser``, bpo-39716.)"""
    if kwargs.get("prog") is None:
        kwargs["prog"] = f"{self._prog_prefix} {name}"
    aliases = kwargs.pop("aliases", ())
    if "help" in kwargs:
        help_text = kwargs.pop("help")
        self._choices_actions.append(self._ChoicesPseudoAction(name, aliases, help_text))
    parser = self._parser_class(**kwargs)
    self._name_parser_map[name] = parser
    for alias in aliases:
        self._name_parser_map[alias] = parser
    return parser


class TestAPluginAddsNewNamesOnly:
    def test_a_plugin_adding_a_core_command_is_skipped_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def register(sp: Any) -> None:
            sp.add_parser("acme-extra", help="Taken back with the plugin")
            sp.add_parser("apply", help="Shadows the core command")
            sp.add_parser("acme-later")  # never reached

        ep = FakeEP("acme", register)
        parser, sp, core_apply = _core_parser()

        added, messages = _run_loader(monkeypatch, sp, [ep], ["fluid", "acme-extra"])

        assert ep.loaded is True
        choices = _cmd_choices(parser)
        assert choices["apply"] is core_apply
        assert set(choices) == {"apply", "validate", "check"}  # nothing of the plugin is left
        assert _help_names(sp) == ["apply", "validate"]  # nor a line of it in the help
        assert "acme" not in added
        assert parser.parse_args(["apply", "--dry-run"]).dry_run is True
        (warning,) = _shadow_messages(messages)
        assert "'acme'" in warning and "'apply'" in warning

    @pytest.mark.parametrize(
        "register",
        [
            lambda sp: sp.add_parser("check"),
            lambda sp: sp.add_parser("fresh", aliases=["validate"]),
        ],
        ids=["an alias as the name", "a new name with a core name as its alias"],
    )
    def test_a_core_alias_or_name_is_reserved_in_either_position(
        self, monkeypatch: pytest.MonkeyPatch, register: Callable[[Any], Any]
    ) -> None:
        parser, sp, core_apply = _core_parser()
        validate = _cmd_choices(parser)["validate"]

        added, messages = _run_loader(monkeypatch, sp, [FakeEP("acme", register)], ["fluid", "x"])

        choices = _cmd_choices(parser)
        assert choices["validate"] is validate and choices["check"] is validate
        assert "fresh" not in choices and added == {}
        assert len(_shadow_messages(messages)) == 1

    def test_the_refusal_is_a_typed_cli_error_raised_before_anything_is_registered(self) -> None:
        _parser, sp, _core_apply = _core_parser()
        guard, refused = bootstrap._guard_subparsers(sp, "acme", {"apply"})

        with pytest.raises(bootstrap.PluginCommandShadowError) as info:
            guard.add_parser("apply")

        err = info.value
        assert isinstance(err, bootstrap.CLIError)
        assert (err.plugin, err.command) == ("acme", "apply")
        assert err.error_slug == "ERR_CLI_PLUGIN_COMMAND_SHADOW"
        assert refused == ["apply"]
        assert guard.add_parser("fresh", help="ok") is sp.choices["fresh"]  # a new name is fine
        assert _help_names(sp) == ["apply", "validate", "fresh"]

    def test_a_plugin_that_catches_the_refusal_and_goes_on_is_still_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def register(sp: Any) -> None:
            sp.add_parser("acme-extra", help="Taken back")
            with contextlib.suppress(Exception):  # what a careless plugin does
                sp.add_parser("apply")
            sp.add_parser("acme-later", help="Taken back too")

        parser, sp, core_apply = _core_parser()

        added, messages = _run_loader(
            monkeypatch, sp, [FakeEP("careless", register)], ["fluid", "acme-extra"]
        )

        assert {"acme-extra", "acme-later"}.isdisjoint(_cmd_choices(parser)) and added == {}
        assert _help_names(sp) == ["apply", "validate"]
        assert _cmd_choices(parser)["apply"] is core_apply
        assert len(_shadow_messages(messages)) == 1

    def test_the_plugin_is_handed_the_real_subparsers_state_and_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only ``add_parser`` differs: what a plugin sets on the object it gets is the real
        action's, and the object still is an ``argparse._SubParsersAction``. Aliases given as a
        generator survive the check."""
        seen: dict = {}

        def register(sp: Any) -> None:
            seen["isinstance"] = isinstance(sp, argparse._SubParsersAction)
            sp.metavar = "COMMAND"
            sp.add_parser("acme-new", aliases=(alias for alias in ["acme-nw"]))

        parser, sp, _core_apply = _core_parser()

        added, messages = _run_loader(monkeypatch, sp, [FakeEP("acme", register)], ["fluid", "x"])

        assert seen["isinstance"] is True and sp.metavar == "COMMAND"
        assert added == {"acme": ["acme-new", "acme-nw"]} and not _shadow_messages(messages)
        assert parser.parse_args(["acme-nw"]).cmd == "acme-nw"

    def test_the_other_plugins_still_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shadow = FakeEP("shadow", lambda sp: sp.add_parser("apply"))
        good = FakeEP("good", lambda sp: sp.add_parser("good-new").set_defaults(func=lambda a: 5))
        parser, sp, core_apply = _core_parser()

        added, messages = _run_loader(monkeypatch, sp, [shadow, good], ["fluid", "good-new"])

        assert added == {"good": ["good-new"]}
        assert _cmd_choices(parser)["apply"] is core_apply
        ns = parser.parse_args(["good-new"])
        assert ns.func(ns, logging.getLogger("test")) == 5  # and its func is adapted
        assert len(_shadow_messages(messages)) == 1

    def test_the_second_of_two_plugins_with_one_new_name_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def first(sp: Any) -> None:
            sp.add_parser("shared", help="The first plugin's").set_defaults(func=lambda a: 1)

        def second(sp: Any) -> None:
            sp.add_parser("second-only", help="Taken back")
            sp.add_parser("shared", help="Would replace the first plugin's")

        parser, sp, _core_apply = _core_parser()

        added, messages = _run_loader(
            monkeypatch, sp, [FakeEP("one", first), FakeEP("two", second)], ["fluid", "shared"]
        )

        choices = _cmd_choices(parser)
        assert added == {"one": ["shared"]}
        assert "second-only" not in choices
        assert parser.parse_args(["shared"]).func(None, logging.getLogger("t")) == 1
        assert _help_names(sp) == ["apply", "validate", "shared"]
        (warning,) = _shadow_messages(messages)
        assert "'two'" in warning and "'shared'" in warning

    def test_a_plugin_cannot_repeat_its_own_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def register(sp: Any) -> None:
            sp.add_parser("dup")
            sp.add_parser("dup")

        parser, sp, _core_apply = _core_parser()

        added, messages = _run_loader(
            monkeypatch, sp, [FakeEP("twice", register)], ["fluid", "dup"]
        )

        assert "dup" not in _cmd_choices(parser) and added == {}
        assert len(_shadow_messages(messages)) == 1

    def test_a_core_name_is_reserved_even_when_the_profile_leaves_it_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``forge`` is a core command the stable build profile does not register, so it is not
        in ``choices``; a plugin still may not take it."""
        parser, sp = _fresh_parser(core=("validate",))
        assert "forge" not in _cmd_choices(parser)

        added, messages = _run_loader(
            monkeypatch, sp, [FakeEP("acme", lambda s: s.add_parser("forge"))], ["fluid", "forge"]
        )

        assert "forge" not in _cmd_choices(parser) and added == {}
        (warning,) = _shadow_messages(messages)
        assert "'forge'" in warning

    def test_every_core_name_and_alias_is_reserved(self) -> None:
        reserved = bootstrap._reserved_command_names()
        for entry in bootstrap.CORE_COMMANDS:
            assert set(bootstrap._core_command_names(entry)) <= reserved

    def test_the_warning_carries_the_names_never_the_plugins_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def register(sp: Any) -> None:
            try:
                sp.add_parser("apply")
            except bootstrap.PluginCommandShadowError:
                raise RuntimeError("boom password=hunter2") from None

        _parser, sp, _core_apply = _core_parser()

        _added, messages = _run_loader(
            monkeypatch, sp, [FakeEP("acme", register)], ["fluid", "acme-extra"]
        )

        assert len(_shadow_messages(messages)) == 1
        assert not any("hunter2" in m or "boom" in m for m in messages)

    def test_a_typed_error_the_plugin_raised_itself_is_a_load_failure_by_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only what the stand-in refused is named in the warning: a plugin that raises the
        typed error with text of its own gets the type-only message of any failing plugin."""

        def register(_sp: Any) -> None:
            raise bootstrap.PluginCommandShadowError("x", "token=hunter2")

        _parser, sp, _core_apply = _core_parser()

        _added, messages = _run_loader(
            monkeypatch, sp, [FakeEP("forger", register)], ["fluid", "acme-extra"]
        )

        assert not _shadow_messages(messages)
        assert any(
            "Failed to load CLI plugin forger: PluginCommandShadowError" in m for m in messages
        )
        assert not any("hunter2" in m for m in messages)

    def test_a_refused_plugin_is_neither_loaded_nor_warned_about_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runs: List[int] = []

        def register(sp: Any) -> None:
            runs.append(1)
            sp.add_parser("apply")

        _parser, sp, _core_apply = _core_parser()
        ep = FakeEP("acme", register)

        _added, first = _run_loader(monkeypatch, sp, [ep], ["fluid", "acme-extra"])
        _added, second = _run_loader(monkeypatch, sp, [ep], ["fluid", "acme-extra"], force=True)

        assert runs == [1]
        assert len(_shadow_messages(first)) == 1 and not _shadow_messages(second)

    def test_force_loads_every_plugin_and_refuses_the_shadowing_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``fluid describe --commands`` loads every plugin whatever command was typed."""
        shadow = FakeEP("shadow", lambda sp: sp.add_parser("apply"))
        good = FakeEP("good", lambda sp: sp.add_parser("good-new"))
        parser, sp, core_apply = _core_parser()

        added, _ = _run_loader(monkeypatch, sp, [shadow, good], ["fluid", "validate"])
        assert added == {} and not shadow.loaded and not good.loaded  # a core command: no load

        added, messages = _run_loader(
            monkeypatch, sp, [shadow, good], ["fluid", "validate"], force=True
        )

        assert added == {"good": ["good-new"]}
        assert _cmd_choices(parser)["apply"] is core_apply
        assert len(_shadow_messages(messages)) == 1

    def test_python_310_silent_replacement_is_refused_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(argparse._SubParsersAction, "add_parser", _add_parser_py310)
        # The stand-in for 3.10 really replaces a taken name without a word ...
        _p, plain, original = _core_parser()
        assert plain.add_parser("apply") is not original
        assert plain.choices["apply"] is not original

        # ... and the loader still refuses it, whatever argparse does.
        def register(sp: Any) -> None:
            sp.add_parser("acme-extra", help="Taken back")
            sp.add_parser("apply", help="Would replace the core command")

        parser, sp, core_apply = _core_parser()

        added, messages = _run_loader(
            monkeypatch, sp, [FakeEP("acme", register)], ["fluid", "acme-extra"]
        )

        assert _cmd_choices(parser)["apply"] is core_apply
        assert "acme-extra" not in _cmd_choices(parser) and added == {}
        assert _help_names(sp) == ["apply", "validate"]
        assert len(_shadow_messages(messages)) == 1


class TestAShadowingPluginOnTheRealCli:
    """The real ``build_parser`` and ``fluid describe --commands``, with the plugins faked."""

    def test_build_parser_keeps_the_core_command_and_loads_the_new_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fluid_build.cli import build_parser

        seen: dict = {}

        def shadow_register(sp: Any) -> None:
            seen["apply"] = sp.choices["apply"]
            sp.add_parser("apply")

        shadow = FakeEP("shadow", shadow_register)
        good = FakeEP("good", lambda sp: sp.add_parser("good-new").set_defaults(func=lambda a: 3))
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [shadow, good])
        monkeypatch.setattr(sys, "argv", ["fluid", "good-new"])

        parser = build_parser(["good-new"])

        ns = parser.parse_args(["good-new"])
        assert ns.func(ns, logging.getLogger("test")) == 3
        assert _cmd_choices(parser)["apply"] is seen["apply"]

    def test_describe_commands_lists_the_new_name_and_marks_nothing_on_the_core_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fluid_build.describe import command_tree_document

        shadow = FakeEP("shadow", lambda sp: sp.add_parser("apply"))
        good = FakeEP("good", lambda sp: sp.add_parser("good-new", help="A new command"))
        monkeypatch.setattr(bootstrap, "_command_plugin_entry_points", lambda: [shadow, good])

        tree = command_tree_document()["commands"]["subcommands"]

        assert tree["good-new"]["plugin"] == "good"
        assert "plugin" not in tree["apply"]
