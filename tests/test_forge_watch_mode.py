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

"""``fluid forge --watch`` — regenerate the contract when source files change.

Covers the Trello card (#69d4c9cb) requirements:

1. argparse accepts ``--watch`` (parses, defaults off) and building the parser
   never imports the watch module (startup budget on the ``fluid --help`` path).
2. ``fluid forge --watch`` routes to the watch loop and nothing else.
3. The debounce coalesces a burst of N rapid changes into ONE regeneration.
4. A single change fires the regenerate callback exactly once.
5. Ctrl-C / KeyboardInterrupt exits cleanly (rc 0, no traceback).
6. The critical correctness test: regenerating (writing ``contract.fluid.yaml``
   + ``runtime/plan.json``) does NOT retrigger the watcher — no infinite loop —
   while a real source-file edit IS detected.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from unittest import mock

import pytest

from fluid_build.cli import _forge_watch as watch_mod
from fluid_build.cli import forge as forge_mod

LOGGER = logging.getLogger("test.forge.watch")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_forge_args(*extra: str) -> argparse.Namespace:
    """Parse ``forge <extra...>`` with the real production parser."""
    parser = argparse.ArgumentParser(prog="fluid")
    subparsers = parser.add_subparsers(dest="command")
    forge_mod.register(subparsers)
    return parser.parse_args(["forge", *extra])


@pytest.fixture(autouse=True)
def _clear_watch_env(monkeypatch):
    """Never inherit watch tuning knobs / offline toggles from the dev shell."""
    for var in (
        "FLUID_FORGE_WATCH_INTERVAL",
        "FLUID_FORGE_WATCH_DEBOUNCE",
        "FLUID_FORGE_OFFLINE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
    monkeypatch.setenv("FLUID_FORGE_NO_PREVIEW", "1")
    yield


# ---------------------------------------------------------------------------
# 1. Flag parsing + startup budget
# ---------------------------------------------------------------------------


def test_watch_flag_parses_true():
    args = _parse_forge_args("--watch")
    assert args.watch is True


def test_watch_flag_defaults_false():
    args = _parse_forge_args()
    assert args.watch is False


def test_building_parser_does_not_import_watch_module():
    """Startup budget: register()/parse must not pull the watch or discovery
    modules onto the ``fluid --help`` cold path."""
    for mod_name in (
        "fluid_build.cli._forge_watch",
        "fluid_build.cli.forge_copilot_discovery",
    ):
        sys.modules.pop(mod_name, None)

    parser = argparse.ArgumentParser(prog="fluid")
    subparsers = parser.add_subparsers(dest="command")
    forge_mod.register(subparsers)
    parser.parse_args(["forge", "--watch"])

    leaked = [
        m
        for m in (
            "fluid_build.cli._forge_watch",
            "fluid_build.cli.forge_copilot_discovery",
            "watchdog",
        )
        if m in sys.modules
    ]
    assert not leaked, f"heavy modules leaked onto the parser path: {leaked}"


# ---------------------------------------------------------------------------
# 2. Routing — --watch goes to the watch handler and nothing else
# ---------------------------------------------------------------------------


def test_watch_routes_to_watch_handler_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parse_forge_args("--watch", "-d", str(tmp_path / "prod"))

    called = {"watch": 0, "guided": 0, "blank": 0, "ai": 0}

    def _stub_watch(_args, _logger):
        called["watch"] += 1
        return 0

    with (
        mock.patch.object(forge_mod, "_run_watch", _stub_watch),
        mock.patch.object(
            forge_mod,
            "run_guided_mode",
            lambda *a, **k: called.__setitem__("guided", called["guided"] + 1) or 0,
        ),
        mock.patch.object(
            forge_mod,
            "_run_blank_mode",
            lambda *a, **k: called.__setitem__("blank", called["blank"] + 1) or 0,
        ),
        mock.patch.object(
            forge_mod,
            "run_ai_copilot_mode",
            lambda *a, **k: called.__setitem__("ai", called["ai"] + 1) or 0,
        ),
    ):
        rc = forge_mod.run(args, LOGGER)

    assert rc == 0
    assert called == {"watch": 1, "guided": 0, "blank": 0, "ai": 0}


# ---------------------------------------------------------------------------
# 3. Root + artifact resolution
# ---------------------------------------------------------------------------


def test_resolve_watch_roots_is_cwd_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parse_forge_args()
    roots = watch_mod._resolve_watch_roots(args)
    assert roots == [tmp_path.resolve()]


def test_resolve_watch_roots_adds_discovery_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    extra = tmp_path / "sibling"
    extra.mkdir()
    args = _parse_forge_args("--discovery-path", str(extra))
    roots = watch_mod._resolve_watch_roots(args)
    assert tmp_path.resolve() in roots
    assert extra.resolve() in roots


@pytest.mark.parametrize(
    "rel, expected",
    [
        ("contract.fluid.yaml", True),
        ("contract.fluid.json", True),
        ("runtime/plan.json", True),
        ("a/runtime/deep.json", True),
        ("model.sql", False),
        ("data.csv", False),
        ("README.md", False),
    ],
)
def test_is_watch_output_artifact(rel, expected):
    assert watch_mod._is_watch_output_artifact(Path(rel)) is expected


# ---------------------------------------------------------------------------
# 4. watch_loop — the pure, injectable core
# ---------------------------------------------------------------------------


def _scripted_snapshot(sequence):
    """Return a snapshot_fn yielding each value in *sequence*, then repeating
    the last one forever (so extra polls after the script see 'no change')."""
    it = iter(sequence)
    last = {"v": None}

    def _snap():
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"]

    return _snap


def test_watch_loop_single_change_fires_once():
    calls = {"n": 0}

    def on_change():
        calls["n"] += 1

    # baseline S0, then one changed snapshot S1 that immediately holds steady.
    snap = _scripted_snapshot([{"a": (1, 1)}, {"a": (2, 2)}, {"a": (2, 2)}])
    watch_mod.watch_loop(
        [Path(".")],
        on_change,
        snapshot_fn=snap,
        sleep_fn=lambda _s: None,
        stop_after=1,
    )
    assert calls["n"] == 1


def test_watch_loop_no_change_never_fires():
    calls = {"n": 0}
    steady = {"a": (1, 1)}
    watch_mod.watch_loop(
        [Path(".")],
        lambda: calls.__setitem__("n", calls["n"] + 1),
        snapshot_fn=lambda: steady,
        sleep_fn=lambda _s: None,
        stop_after=5,
    )
    assert calls["n"] == 0


def test_watch_loop_debounce_coalesces_burst_into_one():
    """A burst of N distinct rapid changes must collapse into ONE regenerate."""
    calls = {"n": 0}

    def on_change():
        calls["n"] += 1

    # baseline S0; then a save-storm S1->S2->S3 (three distinct snapshots)
    # before the tree settles at S3. The debounce must keep waiting through the
    # storm and fire exactly once when two consecutive reads match.
    burst = [
        {"a": (0, 0)},  # baseline
        {"a": (1, 1)},  # poll: changed -> enter debounce
        {"a": (2, 2)},  # debounce: still moving
        {"a": (3, 3)},  # debounce: still moving
        {"a": (3, 3)},  # debounce: quiescent -> fire
    ]
    snap = _scripted_snapshot(burst)
    watch_mod.watch_loop(
        [Path(".")],
        on_change,
        snapshot_fn=snap,
        sleep_fn=lambda _s: None,
        stop_after=2,
    )
    assert calls["n"] == 1, "burst of 3 changes should coalesce to a single regenerate"


def test_watch_loop_keyboard_interrupt_propagates():
    """Ctrl-C during a poll sleep propagates out of the loop (caller handles it)."""

    def boom(_s):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        watch_mod.watch_loop(
            [Path(".")],
            lambda: None,
            snapshot_fn=lambda: {"a": (1, 1)},
            sleep_fn=boom,
        )


# ---------------------------------------------------------------------------
# 5. run_watch_mode — entry point behaviour
# ---------------------------------------------------------------------------


def test_run_watch_mode_runs_initial_then_loops(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parse_forge_args("--watch")
    regen = {"n": 0}

    def regenerate(_args, _logger):
        regen["n"] += 1
        return 0

    seen = {}

    def fake_loop(roots, on_change, **kw):
        seen["roots"] = roots
        seen["kw"] = kw
        on_change()  # simulate exactly one change burst

    rc = watch_mod.run_watch_mode(
        args,
        LOGGER,
        regenerate_fn=regenerate,
        console_factory=None,
        loop_fn=fake_loop,
    )
    assert rc == 0
    # initial generation (1) + one simulated change (1)
    assert regen["n"] == 2
    assert seen["roots"] == [tmp_path.resolve()]
    assert seen["kw"]["poll_interval"] == watch_mod.DEFAULT_POLL_INTERVAL
    assert seen["kw"]["debounce_seconds"] == watch_mod.DEFAULT_DEBOUNCE_SECONDS


def test_run_watch_mode_ctrl_c_exits_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parse_forge_args("--watch")
    regen = {"n": 0}

    def regenerate(_args, _logger):
        regen["n"] += 1
        return 0

    def fake_loop(roots, on_change, **kw):
        raise KeyboardInterrupt

    rc = watch_mod.run_watch_mode(
        args,
        LOGGER,
        regenerate_fn=regenerate,
        console_factory=None,
        loop_fn=fake_loop,
    )
    assert rc == 0, "Ctrl-C must exit cleanly with rc 0"
    assert regen["n"] == 1, "the initial generation ran before Ctrl-C"


def test_run_watch_mode_missing_discovery_path_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parse_forge_args("--watch", "--discovery-path", str(tmp_path / "does-not-exist"))
    called = {"loop": 0, "regen": 0}

    rc = watch_mod.run_watch_mode(
        args,
        LOGGER,
        regenerate_fn=lambda *a, **k: called.__setitem__("regen", 1) or 0,
        console_factory=None,
        loop_fn=lambda *a, **k: called.__setitem__("loop", 1),
    )
    assert rc == 1
    assert called == {"loop": 0, "regen": 0}


def test_run_watch_mode_env_tunables_apply(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_FORGE_WATCH_INTERVAL", "0.25")
    monkeypatch.setenv("FLUID_FORGE_WATCH_DEBOUNCE", "0.1")
    args = _parse_forge_args("--watch")
    seen = {}

    watch_mod.run_watch_mode(
        args,
        LOGGER,
        regenerate_fn=lambda *a, **k: 0,
        console_factory=None,
        loop_fn=lambda roots, on_change, **kw: seen.update(kw),
    )
    assert seen["poll_interval"] == 0.25
    assert seen["debounce_seconds"] == 0.1


# ---------------------------------------------------------------------------
# 6. Integration — real filesystem snapshot, no self-retrigger
# ---------------------------------------------------------------------------


def test_snapshot_excludes_own_output_but_sees_source_change(tmp_path, monkeypatch):
    """Directly proves the two invariants that keep the loop finite:

    * writing forge's own output (``contract.fluid.yaml`` + ``runtime/``) does
      NOT change the source snapshot — so regenerating can't retrigger; and
    * editing a real source file DOES change the snapshot.
    """
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "model.sql"
    source.write_text("select 1\n", encoding="utf-8")
    roots = [tmp_path.resolve()]

    snap_before = watch_mod._snapshot_sources(roots)

    # Simulate a regeneration writing the contract + a runtime plan.
    (tmp_path / "contract.fluid.yaml").write_text("id: x\n", encoding="utf-8")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "plan.json").write_text("{}", encoding="utf-8")

    snap_after_regen = watch_mod._snapshot_sources(roots)
    assert snap_after_regen == snap_before, "forge's own output must not change the source snapshot"

    # A genuine source edit must be observed.
    source.write_text("select 2\n", encoding="utf-8")
    snap_after_edit = watch_mod._snapshot_sources(roots)
    assert snap_after_edit != snap_before, "a real source-file edit must be detected"


def test_watch_loop_end_to_end_no_self_retrigger(tmp_path, monkeypatch):
    """End-to-end over a REAL filesystem: one source edit -> exactly one
    regeneration, and the contract it writes never retriggers the loop."""
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "model.sql"
    source.write_text("select 1\n", encoding="utf-8")
    roots = [tmp_path.resolve()]

    regen_calls = {"n": 0}

    def regenerate():
        regen_calls["n"] += 1
        # Write exactly what forge writes: the contract + a runtime plan.
        (tmp_path / "contract.fluid.yaml").write_text(
            f"id: p\ngen: {regen_calls['n']}\n", encoding="utf-8"
        )
        (tmp_path / "runtime").mkdir(exist_ok=True)
        (tmp_path / "runtime" / "plan.json").write_text("{}", encoding="utf-8")

    # sleep_fn mutates the source file exactly once, on its first call, to
    # simulate an external editor writing between polls.
    state = {"mutated": False}

    def fake_sleep(_secs):
        if not state["mutated"]:
            state["mutated"] = True
            source.write_text("select 2\n", encoding="utf-8")

    watch_mod.watch_loop(
        roots,
        regenerate,
        snapshot_fn=lambda: watch_mod._snapshot_sources(roots),
        sleep_fn=fake_sleep,
        stop_after=4,
    )

    assert regen_calls["n"] == 1, (
        "exactly one regeneration expected: the single source edit triggers once, "
        "and the contract.fluid.yaml + runtime/plan.json it writes must NOT retrigger"
    )
    # The last generation is on disk and is the only one that ran.
    assert (tmp_path / "contract.fluid.yaml").read_text(encoding="utf-8").endswith("gen: 1\n")


# ---------------------------------------------------------------------------
# 7. _watch_regenerate — reuses the offline guided path, forced headless
# ---------------------------------------------------------------------------


def test_watch_regenerate_forces_non_interactive_and_calls_guided(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parse_forge_args("--watch", "-d", str(tmp_path / "prod"))
    args.non_interactive = False  # ensure the helper flips it

    seen = {}

    def _stub_guided(a, _logger):
        seen["non_interactive"] = a.non_interactive
        return 0

    with mock.patch.object(forge_mod, "run_guided_mode", _stub_guided):
        rc = forge_mod._watch_regenerate(args, LOGGER)

    assert rc == 0
    assert seen["non_interactive"] is True
    assert args.non_interactive is True
