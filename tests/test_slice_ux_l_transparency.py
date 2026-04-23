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

"""Slice UX-L: regression tests for AI mode transparency and user feedback."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from fluid_build.cli.forge_copilot_discovery import DiscoveryReport
from fluid_build.cli.forge_ui import print_forge_performance_summary


def _make_console() -> Any:
    """Create a Rich Console that writes to a StringIO buffer."""
    try:
        from rich.console import Console

        buf = StringIO()
        return Console(file=buf, force_terminal=True, width=80), buf
    except ImportError:
        pytest.skip("Rich not available")


class TestPerformanceSummaryPanel:
    """The panel must render the right lines for different stat shapes."""

    def test_single_shot_basic_panel(self):
        console, buf = _make_console()
        stats = {
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "streaming": True,
            "discovery_cache_hit": True,
            "discovery_files": 12,
            "skills_loaded": True,
            "skills_precompiled": True,
            "skills_label": "Telecommunications",
            "interview_skipped": True,
            "generation_time_s": 15.1,
        }
        print_forge_performance_summary(console, stats)
        output = buf.getvalue()
        assert "Google Gemini" in output
        assert "gemini-2.5-flash" in output
        assert "cache hit" in output
        assert "Telecommunications" in output
        assert "precompiled" in output
        assert "skipped" in output
        assert "15.1s" in output

    def test_agent_loop_panel(self):
        console, buf = _make_console()
        stats = {
            "provider": "openai",
            "model": "gpt-4o",
            "streaming": True,
            "agent_loop_rounds": 5,
            "agent_loop_tool_calls": 7,
            "discovery_files": 42,
            "discovery_cache_hit": False,
            "skills_loaded": False,
            "generation_time_s": 19.3,
        }
        print_forge_performance_summary(console, stats)
        output = buf.getvalue()
        assert "agent-loop" in output
        assert "5 rounds" in output
        assert "7 tool calls" in output
        assert "not installed" in output

    def test_retry_panel(self):
        console, buf = _make_console()
        stats = {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "streaming": True,
            "discovery_files": 42,
            "discovery_cache_hit": False,
            "discovery_scan_ms": 812,
            "skills_loaded": False,
            "generation_attempts": 2,
            "generation_time_s": 28.4,
        }
        print_forge_performance_summary(console, stats)
        output = buf.getvalue()
        assert "2 attempts" in output
        assert "812ms" in output

    def test_routing_model_shown(self):
        console, buf = _make_console()
        stats = {
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-latest",
            "routing_model": "claude-3-5-haiku-latest",
            "streaming": True,
            "discovery_files": 5,
            "discovery_cache_hit": True,
            "skills_loaded": False,
            "generation_time_s": 10.0,
        }
        print_forge_performance_summary(console, stats)
        output = buf.getvalue()
        assert "haiku" in output
        assert "Routing" in output

    def test_skills_tip_shown_when_not_installed(self):
        console, buf = _make_console()
        stats = {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "streaming": True,
            "discovery_files": 1,
            "discovery_cache_hit": True,
            "skills_loaded": False,
            "generation_time_s": 5.0,
        }
        print_forge_performance_summary(console, stats)
        output = buf.getvalue()
        assert "fluid skills install" in output

    def test_no_tip_when_skills_installed(self):
        console, buf = _make_console()
        stats = {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "streaming": True,
            "discovery_files": 1,
            "discovery_cache_hit": True,
            "skills_loaded": True,
            "skills_label": "Finance",
            "skills_precompiled": True,
            "generation_time_s": 5.0,
        }
        print_forge_performance_summary(console, stats)
        output = buf.getvalue()
        assert "fluid skills install" not in output

    def test_empty_stats_does_not_crash(self):
        console, buf = _make_console()
        print_forge_performance_summary(console, {})
        # Should render something minimal or nothing — just no crash.

    def test_none_console_does_not_crash(self):
        print_forge_performance_summary(None, {"provider": "test"})


class TestDiscoveryReportCacheFields:
    def test_default_fields(self):
        r = DiscoveryReport(workspace_roots=["."])
        assert r.cache_hit is False
        assert r.scan_time_ms == 0

    def test_cache_hit_field_set(self):
        r = DiscoveryReport(workspace_roots=["."], cache_hit=True)
        assert r.cache_hit is True


class TestInterviewSkipMarker:
    def test_is_context_sufficient_sets_marker(self):
        from fluid_build.cli.forge_copilot_interview import (
            bootstrap_interview_state,
            is_context_sufficient,
        )

        state = bootstrap_interview_state(
            {
                "project_goal": "Sales analytics",
                "data_sources": "warehouse tables",
                "use_case": "analytics",
            },
            discovery_report=MagicMock(
                existing_contracts=[],
                provider_hints=[],
                sample_files=[],
            ),
        )
        assert is_context_sufficient(state.normalized_context)
