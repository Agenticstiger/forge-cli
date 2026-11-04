# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for aws/plan/planner._resolve_env_templates."""

import os

from fluid_build.providers.aws.plan.planner import _resolve_env_templates


# ── _resolve_env_templates ──────────────────────────────────────────
class TestResolveEnvTemplates:
    def test_no_template(self):
        assert _resolve_env_templates("plain") == "plain"

    def test_non_string(self):
        assert _resolve_env_templates(42) == 42

    def test_resolved(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "resolved")
        assert _resolve_env_templates("{{ env.MY_VAR }}") == "resolved"

    def test_unresolved_kept(self):
        # Remove if exists
        os.environ.pop("NONEXISTENT_VAR", None)
        result = _resolve_env_templates("{{ env.NONEXISTENT_VAR }}")
        assert "NONEXISTENT_VAR" in result
        assert "{{" in result

    def test_multiple(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        result = _resolve_env_templates("{{ env.A }}-{{ env.B }}")
        assert result == "1-2"

    def test_no_braces_returns_as_is(self):
        assert _resolve_env_templates("no braces") == "no braces"

    def test_partial_braces_not_env(self):
        assert _resolve_env_templates("{{not_env}}") == "{{not_env}}"
