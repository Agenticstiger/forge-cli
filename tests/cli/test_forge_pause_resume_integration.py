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

"""Integration tests for the pause/resume wiring in ``run_ai_copilot_mode``.

These exercise the S2 + S3 fixes from ``02-pause-resume.md``: the SIGINT
handler must be installed against the active run-id BEFORE any LLM work,
and the resume id (from ``--resume`` or freshly minted) must flow
through to the coordinator's ``StageSession.run_id`` so the
``skip_if_done`` blocks find the cached stages.

We don't spawn a real subprocess — instead we drive
``run_ai_copilot_mode`` directly with stubbed copilot / runtime
dependencies, then assert on:

* ``install_pause_handler`` was called with the expected ``run_id`` and
  the marker writes under ``.fluid/agents/<run_id>/``.
* ``StageSession.run_id`` was stamped before ``coordinator.from_tables``
  / ``from_intent`` / ``from_catalog`` runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from fluid_build.cli import _signal_handler


@pytest.fixture(autouse=True)
def _reset_handler_state():
    _signal_handler.reset_handler_state()
    yield
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    _signal_handler.reset_handler_state()


# ---------------------------------------------------------------------------
# S2 — SIGINT handler installed during run_ai_copilot_mode
# ---------------------------------------------------------------------------


def _build_args(**kwargs) -> argparse.Namespace:
    """Build a minimal argparse.Namespace mirroring ``fluid forge`` flags."""
    defaults = {
        "_resume_run_id": None,
        "_resume_explicit": False,
        "target_dir": None,
        "non_interactive": True,
        "llm_provider": None,
        "llm_model": None,
        "llm_endpoint": None,
        "llm_routing_model": None,
        "llm_routing_endpoint": None,
        "tiered": False,
        "require_llm": False,
        "discover": False,
        "discovery_path": None,
        "memory": False,
        "save_memory": False,
        "fragments": False,
        "no_fragments": False,
        "no_generate": False,
        "yes": True,
        "show_work": False,
        "apply_enrichment": False,
        "data_product_type": None,
        "transform_engine": None,
        "refine": None,
        "from_product": [],
        "from_product_list": None,
        "from_workspace": [],
        "also_emit": None,
        "seed_from": None,
        "seed_allow_remote": False,
        "seed_no_remote": False,
        "dry_run": False,
        "domain": None,
        "provider": None,
        "scaffold": None,
        "agent_loop": False,
        "context": None,
        "template": None,
        "blank": False,
        "_force_llm_setup": False,
        "_enable_copilot_recovery": False,
        "_implicit_mode": False,
        "show_memory": False,
        "reset_memory": False,
        "quiet": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _drive_forge_mode(
    args, tmp_path, monkeypatch, *, install_side_effect=None, minimal_side_effect=None
):
    """Shared driver for run_ai_copilot_mode against stubs.

    ``install_side_effect`` is the body of the SIGINT handler installer
    (default: capture args). ``minimal_side_effect`` is what
    ``_create_project_minimal`` does (default: return True so the mode
    succeeds).
    """
    monkeypatch.setenv("FLUID_FORGE_NO_WELCOME", "1")
    monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
    monkeypatch.setenv("FLUID_FORGE_NO_PREVIEW", "1")
    monkeypatch.chdir(tmp_path)

    captured: Dict[str, Any] = {}

    def _default_install(*, run_id, run_dir, get_state, saver=None, **_):
        captured["run_id"] = run_id
        captured["run_dir"] = Path(run_dir)
        captured["saver_type"] = type(saver).__name__ if saver else None
        captured["state_callback"] = get_state

    monkeypatch.setattr(
        "fluid_build.cli._signal_handler.install_pause_handler",
        install_side_effect or _default_install,
    )

    # Make the auto-CI scaffolder a no-op so it doesn't prompt.
    monkeypatch.setattr(
        "fluid_build.cli.forge_modes._scaffold_ci_pipeline_impl",
        lambda *a, **kw: (None, None),
    )

    # Stub _create_project_minimal so we don't hit the LLM.
    def _default_minimal(**kw):
        return True

    monkeypatch.setattr(
        "fluid_build.cli.forge_modes._create_project_minimal",
        minimal_side_effect or _default_minimal,
    )

    copilot_class = MagicMock()

    from fluid_build.cli.forge_modes import run_ai_copilot_mode

    target = Path(args.target_dir) if args.target_dir else tmp_path / "default-product"
    target.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("test_drive_forge")
    rc = run_ai_copilot_mode(
        args,
        logger,
        copilot_class=copilot_class,
        get_cli_arg_fn=lambda a, k, d=None: getattr(a, k, d),
        load_context_fn=lambda *a, **kw: {},
        get_target_directory_fn=lambda a, n: target,
        context_error_cls=ValueError,
        build_interview_summary_fn=lambda c: {},
        console_factory=None,
    )
    return rc, captured


def test_pause_handler_installs_with_resume_id_when_provided(tmp_path, monkeypatch):
    """When ``args._resume_run_id`` is set, the handler must use that id
    (so the .paused marker lands in the right run-dir for resume)."""
    target = tmp_path / "my-product"
    target.mkdir()

    args = _build_args(_resume_run_id="20260527-100000-resumed", target_dir=str(target))

    rc, captured = _drive_forge_mode(args, tmp_path, monkeypatch)

    # Handler wired with the explicit resume run-id BEFORE any work.
    assert captured.get("run_id") == "20260527-100000-resumed"
    expected_run_dir = target / ".fluid" / "agents" / "20260527-100000-resumed"
    assert captured["run_dir"] == expected_run_dir


def test_pause_handler_uses_freshly_minted_run_id_when_no_resume(tmp_path, monkeypatch):
    """When no ``--resume`` flag is set, the mode mints a fresh run-id
    in the canonical format (``YYYYMMDD-HHMMSS-<6hex>``) and installs
    the handler against it."""
    target = tmp_path / "fresh-product"
    args = _build_args(_resume_run_id=None, target_dir=str(target))

    rc, captured = _drive_forge_mode(args, tmp_path, monkeypatch)

    rid = captured.get("run_id", "")
    assert rid
    # Args was stamped with the active run-id so the runtime can flow it
    # through to the coordinator.
    assert getattr(args, "_active_run_id", None) == rid

    # The state callback returns a dict the SIGINT handler consumes.
    state = captured["state_callback"]()
    assert "current_stage" in state
    assert "stage_name" in state
    assert "cost_so_far" in state
    # At the very start of a run, no stages completed yet.
    assert state["current_stage"] == 0
    assert state["stage_name"] == "starting"


def test_real_sigint_handler_writes_paused_marker(tmp_path, monkeypatch):
    """Real (un-mocked) install_pause_handler + manual SIGINT.

    Drives run_ai_copilot_mode with the REAL signal handler wired
    in, then fires SIGINT during _create_project_minimal. The
    handler must:

    * Write ``.paused`` under ``<target>/.fluid/agents/<run_id>/``
    * Exit 130

    This is the regression test for the headline finding in
    ``02-pause-resume.md``: "Real SIGINT bubbles through to
    forge_modes.py:1046's bare ``except KeyboardInterrupt`` ...
    No ``.paused`` marker, no run directory, no resume hint."
    """
    monkeypatch.setenv("FLUID_FORGE_NO_WELCOME", "1")
    monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
    monkeypatch.setenv("FLUID_FORGE_NO_PREVIEW", "1")
    monkeypatch.chdir(tmp_path)

    target = tmp_path / "sigint-product"
    target.mkdir()

    # Replace sys.exit so the handler doesn't terminate the test process.
    exit_calls: list[int] = []

    def _no_exit(code: int = 0) -> None:
        exit_calls.append(code)
        raise SystemExit(code)

    monkeypatch.setattr("sys.exit", _no_exit)
    monkeypatch.setattr(
        "fluid_build.cli.forge_modes._scaffold_ci_pipeline_impl",
        lambda *a, **kw: (None, None),
    )

    # Stub _create_project_minimal to fire SIGINT.
    def _minimal_that_pauses(**kw):
        os.kill(os.getpid(), signal.SIGINT)
        return True

    monkeypatch.setattr(
        "fluid_build.cli.forge_modes._create_project_minimal",
        _minimal_that_pauses,
    )

    args = _build_args(target_dir=str(target))
    copilot_class = MagicMock()

    from fluid_build.cli.forge_modes import run_ai_copilot_mode

    logger = logging.getLogger("test_real_sigint")

    with pytest.raises(SystemExit) as exc:
        run_ai_copilot_mode(
            args,
            logger,
            copilot_class=copilot_class,
            get_cli_arg_fn=lambda a, k, d=None: getattr(a, k, d),
            load_context_fn=lambda *a, **kw: {},
            get_target_directory_fn=lambda a, n: target,
            context_error_cls=ValueError,
            build_interview_summary_fn=lambda c: {},
            console_factory=None,
        )

    # SIGINT handler exited 130.
    assert exc.value.code == 130
    assert 130 in exit_calls

    # ``.paused`` marker landed under target/.fluid/agents/<active_run_id>/
    active_id = getattr(args, "_active_run_id", None)
    assert active_id, "args._active_run_id should be stamped before SIGINT"
    paused_marker = target / ".fluid" / "agents" / active_id / ".paused"
    assert paused_marker.is_file(), f"missing pause marker at {paused_marker}"

    # Marker payload carries the right shape (cost_so_far_usd present).
    payload = json.loads(paused_marker.read_text(encoding="utf-8"))
    assert "paused_at" in payload
    assert "cost_so_far_usd" in payload


# ---------------------------------------------------------------------------
# S3 — resume_run_id flows through context to the coordinator's session
# ---------------------------------------------------------------------------


def test_resume_run_id_flows_to_stage_session_run_id(tmp_path, monkeypatch):
    """The runtime's ``_generate_staged_copilot_artifacts`` must stamp
    ``context['_resume_run_id']`` onto the session BEFORE invoking the
    coordinator. This is what makes ``skip_if_done`` find cached
    stages on a resume.

    Strategy: drive the runtime function with real (cheap) discovery
    + fake coordinator. We don't need a full LLM round-trip — we
    just need to assert that the session's run_id matches the
    context's _resume_run_id when the coordinator gets invoked.
    """
    from fluid_build.cli import forge_copilot_runtime as runtime_mod
    from fluid_build.copilot.agents.base import StageSession

    captured: Dict[str, Any] = {}

    # Spy on StageSession to capture run_id post-mutation.
    original_session_cls = StageSession

    def _spying_session(**kwargs):
        sess = original_session_cls(**kwargs)
        # Wrap setattr to capture run_id assignment.
        original_setattr = sess.__class__.__setattr__

        def _logging_setattr(s, name, value):
            if name == "run_id":
                captured["session_run_id"] = value
            original_setattr(s, name, value)

        sess.__class__.__setattr__ = _logging_setattr
        return sess

    class _FakeResult:
        physical = None
        contract = {}
        logical = None

    class _FakeCoordinator:
        def from_intent(self, session, *, intent, technique, engine, include_physical):
            captured["coordinator_called"] = True
            captured["session_run_id_at_call"] = getattr(session, "run_id", None)
            return _FakeResult()

        def from_tables(self, *a, **kw):
            return _FakeResult()

        def from_catalog(self, *a, **kw):
            return _FakeResult()

    monkeypatch.setattr(
        "fluid_build.copilot.agents.coordinator.StageCoordinator",
        lambda **kw: _FakeCoordinator(),
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.base.StageSession",
        _spying_session,
    )
    # Resolve store should return some object — null backend works.
    monkeypatch.setattr(
        "fluid_build.copilot.store.factory.resolve_store",
        lambda **kw: None,
    )

    # Force the source-select path to "synthesized" so from_intent is
    # called via the fallback BusinessIntent path.
    monkeypatch.setattr(
        runtime_mod,
        "_select_staged_source",
        lambda *a, **kw: {"kind": "synthesized"},
    )
    monkeypatch.setattr(
        runtime_mod,
        "_build_business_intent_from_context",
        lambda *a, **kw: object(),
    )

    # Use a real DiscoveryReport with sensible defaults so
    # _build_scaffold_decision doesn't blow up.
    from fluid_build.cli.forge_copilot_discovery import DiscoveryReport

    discovery = DiscoveryReport(workspace_roots=[str(tmp_path)])

    class _FakeLlmConfig:
        provider = "test"
        model = "test"
        routing_model = None

    context: Dict[str, Any] = {
        "_resume_run_id": "20260527-099999-aaaaaa",
        "project_goal": "test product",
    }

    runtime_mod._generate_staged_copilot_artifacts(
        context,
        llm_config=_FakeLlmConfig(),
        discovery_report=discovery,
        project_memory=None,
        team_memory=None,
        capability_matrix={},
        logger=logging.getLogger("test_resume_propagation"),
    )

    assert captured.get(
        "coordinator_called"
    ), "coordinator.from_intent was never invoked — staged path bailed?"
    assert captured.get("session_run_id_at_call") == "20260527-099999-aaaaaa", (
        "session.run_id was not stamped from context['_resume_run_id'] before "
        "coordinator dispatch — see the wiring in "
        "fluid_build/cli/forge_copilot_runtime.py."
    )


# ---------------------------------------------------------------------------
# Defensive — bare except KeyboardInterrupt still returns 130
# ---------------------------------------------------------------------------


def test_bare_keyboard_interrupt_safety_net_returns_130(tmp_path, monkeypatch):
    """If install_pause_handler bombs (e.g., signal.signal raises in a
    threaded context), the bare ``except KeyboardInterrupt`` further
    down must still catch and return 130. This is the fallback when
    the dedicated handler couldn't be installed.

    Setup:
      * install_pause_handler raises before installing → no handler
      * _create_project_minimal raises KeyboardInterrupt
      * Expected: rc == 130 from the bare except in run_ai_copilot_mode
    """
    monkeypatch.setenv("FLUID_FORGE_NO_WELCOME", "1")
    monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
    monkeypatch.setenv("FLUID_FORGE_NO_PREVIEW", "1")
    monkeypatch.chdir(tmp_path)

    # Make install_pause_handler raise so no SIGINT handler is wired.
    monkeypatch.setattr(
        "fluid_build.cli._signal_handler.install_pause_handler",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("install failed")),
    )
    monkeypatch.setattr(
        "fluid_build.cli.forge_modes._scaffold_ci_pipeline_impl",
        lambda *a, **kw: (None, None),
    )

    def _minimal_kbi(**kw):
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        "fluid_build.cli.forge_modes._create_project_minimal",
        _minimal_kbi,
    )

    target = tmp_path / "fallback"
    target.mkdir()
    args = _build_args(target_dir=str(target))
    copilot_class = MagicMock()

    from fluid_build.cli.forge_modes import run_ai_copilot_mode

    logger = logging.getLogger("test_bare_kbi_fallback")
    rc = run_ai_copilot_mode(
        args,
        logger,
        copilot_class=copilot_class,
        get_cli_arg_fn=lambda a, k, d=None: getattr(a, k, d),
        load_context_fn=lambda *a, **kw: {},
        get_target_directory_fn=lambda a, n: target,
        context_error_cls=ValueError,
        build_interview_summary_fn=lambda c: {},
        console_factory=None,
    )
    assert rc == 130
