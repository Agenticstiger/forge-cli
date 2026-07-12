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

"""Lazy loading of ``fluid_build.commands`` CLI-command plugins.

Regression suite for ``cli/bootstrap.py::_register_command_plugins`` — the
argparse adaptation of Click's ``LazyGroup`` recipe. A third-party command
plugin's module + ``register()`` must be imported ONLY when its command is
actually being invoked, so a heavy module-scope import a plugin happens to
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
* plus argv parsing, operator allow/block governance, and the import-free
  ``fluid --help`` plugin listing.
"""

from __future__ import annotations

import argparse
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
