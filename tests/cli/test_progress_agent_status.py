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

"""Unit tests for :class:`fluid_build.cli.progress.AgentStatus`.

The panel is cosmetic — the invariant that matters is that it must
never break the underlying work, whatever the environment. Cover:

* enabled=False explicitly disables regardless of TTY/env
* FLUID_QUIET=1 disables
* FLUID_NO_TUI=1 disables
* non-TTY stdout disables
* exceptions from the wrapped block propagate unchanged
* rich import failures degrade to silent no-op (hard to simulate; we
  exercise the public API instead and rely on the broad-except in the
  implementation)
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from unittest import mock

import pytest

from fluid_build.cli.progress import AgentStatus, _status_disabled


@contextmanager
def _clean_env():
    saved = {k: os.environ.get(k) for k in ("FLUID_QUIET", "FLUID_NO_TUI", "FLUID_NONINTERACTIVE")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _stub_tty(is_tty: bool):
    """Patch ``sys.stdout.isatty`` to return the desired value."""
    return mock.patch.object(sys.stdout, "isatty", return_value=is_tty)


class TestAgentStatusDisabledPaths:
    def test_explicit_disable_always_wins(self):
        with _clean_env(), _stub_tty(True):
            with AgentStatus(stage="logical", agent="LogicalAgent", enabled=False) as status:
                assert status._live is None
                # No crash on tick when disabled.
                status.tick()

    def test_fluid_quiet_disables(self):
        with _clean_env(), _stub_tty(True):
            os.environ["FLUID_QUIET"] = "1"
            with AgentStatus(stage="logical", agent="LogicalAgent") as status:
                assert status._live is None

    def test_fluid_no_tui_disables(self):
        with _clean_env(), _stub_tty(True):
            os.environ["FLUID_NO_TUI"] = "1"
            with AgentStatus(stage="logical", agent="LogicalAgent") as status:
                assert status._live is None

    def test_fluid_noninteractive_disables(self):
        with _clean_env(), _stub_tty(True):
            os.environ["FLUID_NONINTERACTIVE"] = "1"
            with AgentStatus(stage="logical", agent="LogicalAgent") as status:
                assert status._live is None

    def test_non_tty_disables(self):
        with _clean_env(), _stub_tty(False):
            assert _status_disabled() is True
            with AgentStatus(stage="logical", agent="LogicalAgent") as status:
                assert status._live is None

    def test_explicit_enable_still_respects_fluid_quiet(self):
        """``enabled=True`` does not override the ``FLUID_QUIET`` kill switch."""
        with _clean_env(), _stub_tty(False):  # non-tty + explicit enable
            os.environ["FLUID_QUIET"] = "1"
            with AgentStatus(stage="logical", agent="LogicalAgent", enabled=True) as status:
                assert status._live is None


class TestAgentStatusExceptionSafety:
    def test_caller_exception_propagates_unchanged(self):
        with _clean_env(), _stub_tty(False):  # disabled path — simpler
            with pytest.raises(RuntimeError, match="boom"):
                with AgentStatus(stage="logical", agent="LogicalAgent"):
                    raise RuntimeError("boom")

    def test_tick_is_safe_when_disabled(self):
        with _clean_env(), _stub_tty(False):
            with AgentStatus(stage="logical", agent="LogicalAgent") as status:
                # Should not raise even though no Live was constructed.
                status.tick()
                status.tick()

    def test_render_produces_text(self):
        """``_render`` must return a rich Text-like object without side effects."""
        pytest.importorskip("rich")
        with _clean_env(), _stub_tty(True):
            status = AgentStatus(
                stage="logical",
                agent="LogicalAgent",
                provider="gemini",
                model="gemini-2.5-pro",
            )
            status._started_at = 0.0  # stable elapsed for rendering
            rendered = status._render()
            # rich Text supports str()
            text = str(rendered)
            assert "LogicalAgent" in text
            assert "logical" in text
            assert "gemini" in text
            assert "thinking" in text
