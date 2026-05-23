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

"""Tests for aws/plan/planner._resolve_env_templates and snowflake connection_enhanced._split_sql_statements."""

import os

from fluid_build.providers.aws.plan.planner import _resolve_env_templates
from fluid_build.providers.snowflake.connection_enhanced import SnowflakeConnection


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


# ── _split_sql_statements ──────────────────────────────────────────
class TestSplitSqlStatements:
    @staticmethod
    def _split(script: str):
        """Call _split_sql_statements without needing a real connection."""
        return SnowflakeConnection._split_sql_statements(None, script)

    def test_single_statement(self):
        assert self._split("SELECT 1;") == ["SELECT 1;"]

    def test_multiple_statements(self):
        result = self._split("SELECT 1;\nSELECT 2;")
        assert len(result) == 2

    def test_skips_comments(self):
        result = self._split("-- a comment\nSELECT 1;")
        assert result == ["SELECT 1;"]

    def test_skips_blanks(self):
        result = self._split("\n\nSELECT 1;\n\n")
        assert result == ["SELECT 1;"]

    def test_multiline_statement(self):
        result = self._split("SELECT\n  1\n  FROM t;")
        assert len(result) == 1
        assert "SELECT" in result[0]

    def test_no_trailing_semicolon(self):
        result = self._split("SELECT 1")
        assert len(result) == 1

    def test_empty(self):
        assert self._split("") == []
