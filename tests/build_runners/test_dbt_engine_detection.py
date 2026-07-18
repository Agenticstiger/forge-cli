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

"""dbt engine detection — Python dbt-core v1 vs Fusion (dbt Core v2).

Fusion compiles its adapters into the binary and its ``dbt --version``
output is a single banner line — it lists NO adapter plugins. The legacy
adapter probe substring-matched the adapter name in that output, so every
Fusion user got a False probe and was silently punted to the Docker
pip-install-dbt-core fallback (slow, requires Docker, inverts the user's
engine choice). These tests pin the engine-aware replacement:

* ``_parse_dbt_engine`` — pure classifier over both ``--version`` shapes.
* ``_detect_dbt_engine`` — the cached subprocess probe.
* ``_dbt_command_supports_adapter`` — Fusion consults the built-in
  adapter set and NEVER falls back to Docker; core keeps the substring
  probe.
* ``build_dbt_command`` — Fusion + snowflake stays native even when
  Docker is available.
* ``_run_dbt_parse_gate`` — honours ``$DBT_EXECUTABLE`` like the runner.

No real dbt binary is ever spawned: the only subprocess seam is
``runner.subprocess.run`` (via ``_dbt_version_output``) and every test
monkeypatches it with fixture output.

Fixture provenance (borrow-before-build receipts):

* Fusion banner ``dbt Fusion 2.0.0-preview.126`` — the literal example in
  dbt-labs/docs.getdbt.com ``website/docs/docs/dbt-versions/dbt-versions.md``
  ("Checking your version", https://docs.getdbt.com/docs/dbt-versions).
* Core multi-line shape — same page, dbt Core versioning section.
* Built-in adapter matrix — https://docs.getdbt.com/docs/fusion/supported-features
  (Snowflake GA; BigQuery/Redshift preview; Databricks private preview;
  Spark + DuckDB Fusion-CLI beta) plus the ``dbt system install-drivers``
  driver set (postgres, salesforce).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fluid_build.build_runners.dbt.runner import (
    FUSION_BUILTIN_ADAPTERS,
    _dbt_command_supports_adapter,
    _dbt_version_output,
    _detect_dbt_engine,
    _parse_dbt_engine,
    build_dbt_command,
)
from fluid_build.cli import generate_speed_transformation as gst

# The exact single-line banner documented at docs.getdbt.com/docs/dbt-versions.
FUSION_VERSION_OUTPUT = "dbt Fusion 2.0.0-preview.126\n"

# The classic Python dbt-core v1 multi-line shape (same docs page).
CORE_VERSION_OUTPUT = (
    "Core:\n"
    "  - installed: 1.8.0\n"
    "  - latest:    1.8.0 - Up to date!\n"
    "\n"
    "Plugins:\n"
    "  - snowflake: 1.9.0 - Up to date!\n"
)


@pytest.fixture(autouse=True)
def _reset_probe_caches(monkeypatch: pytest.MonkeyPatch):
    """Clear every lru-cached probe before AND after each test, and keep
    ``$DBT_EXECUTABLE`` out of the picture so a dev shell can't skew
    command composition."""
    monkeypatch.delenv("DBT_EXECUTABLE", raising=False)
    for fn in (_dbt_version_output, _detect_dbt_engine, _dbt_command_supports_adapter):
        fn.cache_clear()
    yield
    for fn in (_dbt_version_output, _detect_dbt_engine, _dbt_command_supports_adapter):
        fn.cache_clear()


def _fake_version_run(output: str):
    """A ``subprocess.run`` stand-in returning ``output`` on stdout."""

    def _run(cmd, **kwargs):
        assert cmd[1:] == ["--version"], f"unexpected dbt argv: {cmd}"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    return _run


# ── _parse_dbt_engine — pure classifier ──────────────────────────────────


class TestParseDbtEngine:
    def test_fusion_docs_banner(self):
        assert _parse_dbt_engine(FUSION_VERSION_OUTPUT) == ("fusion", "2.0.0-preview.126")

    def test_fusion_ga_banner_without_prerelease_tag(self):
        assert _parse_dbt_engine("dbt Fusion 2.0.0\n") == ("fusion", "2.0.0")

    def test_fusion_hyphenated_banner(self):
        # Defensive: the repo/binary is named dbt-fusion; tolerate that form.
        assert _parse_dbt_engine("dbt-fusion 2.0.0-beta.1\n") == ("fusion", "2.0.0-beta.1")

    def test_fusion_banner_without_version_still_classifies(self):
        flavor, version = _parse_dbt_engine("dbt Fusion\n")
        assert flavor == "fusion"
        assert version == ""

    def test_core_multi_line_shape(self):
        assert _parse_dbt_engine(CORE_VERSION_OUTPUT) == ("core", "1.8.0")

    def test_core_pre_one_dot_zero_shape(self):
        assert _parse_dbt_engine("installed version: 0.21.1\n") == ("core", "0.21.1")

    def test_bare_two_x_banner_is_fusion(self):
        # Some builds print just "dbt <version>" — 2.x is the Rust engine.
        assert _parse_dbt_engine("dbt 2.0.0-preview.92\n") == ("fusion", "2.0.0-preview.92")

    def test_bare_one_x_banner_is_core(self):
        assert _parse_dbt_engine("dbt 1.7.14\n") == ("core", "1.7.14")

    def test_plugins_header_alone_is_core(self):
        assert _parse_dbt_engine("Plugins:\n  - snowflake: 1.11.4\n") == ("core", "")

    def test_garbage_is_unknown(self):
        assert _parse_dbt_engine("bash: dbt: command not found\n") == ("unknown", "")

    def test_empty_is_unknown(self):
        assert _parse_dbt_engine("") == ("unknown", "")


# ── _detect_dbt_engine — cached subprocess probe ─────────────────────────


class TestDetectDbtEngine:
    def test_detects_fusion(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _fake_version_run(FUSION_VERSION_OUTPUT),
        )
        assert _detect_dbt_engine("/opt/fusion/dbt") == ("fusion", "2.0.0-preview.126")

    def test_detects_core(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _fake_version_run(CORE_VERSION_OUTPUT),
        )
        assert _detect_dbt_engine("/opt/core/dbt") == ("core", "1.8.0")

    def test_unrunnable_binary_is_unknown(self, monkeypatch: pytest.MonkeyPatch):
        def _boom(cmd, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr("fluid_build.build_runners.dbt.runner.subprocess.run", _boom)
        assert _detect_dbt_engine("/opt/broken/dbt") == ("unknown", "")

    def test_timeout_is_unknown(self, monkeypatch: pytest.MonkeyPatch):
        def _slow(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

        monkeypatch.setattr("fluid_build.build_runners.dbt.runner.subprocess.run", _slow)
        assert _detect_dbt_engine("/opt/slow/dbt", timeout=0.3) == ("unknown", "")

    def test_probe_is_cached_per_executable(self, monkeypatch: pytest.MonkeyPatch):
        calls = {"n": 0}

        def _run(cmd, **kwargs):
            calls["n"] += 1
            return SimpleNamespace(returncode=0, stdout=FUSION_VERSION_OUTPUT, stderr="")

        monkeypatch.setattr("fluid_build.build_runners.dbt.runner.subprocess.run", _run)
        assert _detect_dbt_engine("/opt/fusion/dbt")[0] == "fusion"
        assert _detect_dbt_engine("/opt/fusion/dbt")[0] == "fusion"
        assert calls["n"] == 1

    def test_short_budget_miss_does_not_poison_full_budget_call(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The welcome scan probes with a tiny timeout; a miss there must
        not cache-poison the runner's full-budget detection (timeout is
        part of the cache key)."""

        def _timeout_only_when_short(cmd, **kwargs):
            if kwargs.get("timeout", 10.0) < 1.0:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])
            return SimpleNamespace(returncode=0, stdout=CORE_VERSION_OUTPUT, stderr="")

        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _timeout_only_when_short,
        )
        assert _detect_dbt_engine("/opt/core/dbt", timeout=0.3) == ("unknown", "")
        assert _detect_dbt_engine("/opt/core/dbt") == ("core", "1.8.0")


# ── _dbt_command_supports_adapter — engine-aware probe ───────────────────


class TestSupportsAdapterEngineAware:
    def test_fusion_snowflake_probe_is_true(self, monkeypatch: pytest.MonkeyPatch):
        """THE bug this card fixes: the Fusion banner contains no adapter
        names, so the legacy substring probe returned False and punted a
        Fusion+snowflake user to the Docker fallback."""
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _fake_version_run(FUSION_VERSION_OUTPUT),
        )
        assert "snowflake" not in FUSION_VERSION_OUTPUT.lower()  # probe can't substring-match
        assert _dbt_command_supports_adapter("/opt/fusion/dbt", "snowflake") is True

    @pytest.mark.parametrize(
        "adapter", ["snowflake", "bigquery", "redshift", "databricks", "postgres"]
    )
    def test_fusion_builtin_adapters_probe_true(
        self, monkeypatch: pytest.MonkeyPatch, adapter: str
    ):
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _fake_version_run(FUSION_VERSION_OUTPUT),
        )
        assert adapter in FUSION_BUILTIN_ADAPTERS
        assert _dbt_command_supports_adapter("/opt/fusion/dbt", adapter) is True

    def test_fusion_unknown_adapter_attempts_native_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """Safe default: the Fusion adapter matrix changes per release, so
        an adapter outside the known built-in set still attempts the
        native run (never the Docker fallback) — with a WARNING so the
        operator understands a subsequent dbt failure."""
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _fake_version_run(FUSION_VERSION_OUTPUT),
        )
        with caplog.at_level("WARNING", logger="fluid.build_runners.dbt.runner"):
            assert _dbt_command_supports_adapter("/opt/fusion/dbt", "odps") is True
        assert any("dbt.adapter.unverified" in rec.message for rec in caplog.records)

    def test_core_listed_adapter_probe_true(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _fake_version_run(CORE_VERSION_OUTPUT),
        )
        assert _dbt_command_supports_adapter("/opt/core/dbt", "snowflake") is True

    def test_core_unlisted_adapter_probe_false(self, monkeypatch: pytest.MonkeyPatch):
        # v1 keeps the substring probe — an unlisted plugin means the local
        # install truly can't run the build, and Docker remains the answer.
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _fake_version_run(CORE_VERSION_OUTPUT),
        )
        assert _dbt_command_supports_adapter("/opt/core/dbt", "bigquery") is False

    def test_unrunnable_binary_probe_false(self, monkeypatch: pytest.MonkeyPatch):
        def _boom(cmd, **kwargs):
            raise OSError("no such file")

        monkeypatch.setattr("fluid_build.build_runners.dbt.runner.subprocess.run", _boom)
        assert _dbt_command_supports_adapter("/missing/dbt", "snowflake") is False


# ── build_dbt_command — Docker fallback must NOT trigger for Fusion ──────


class TestFusionNeverFallsBackToDocker:
    def _make_project(self, tmp_path: Path) -> Path:
        project_dir = tmp_path / "dbt_project"
        project_dir.mkdir()
        (project_dir / "dbt_project.yml").write_text("name: sample\nprofile: telco\n")
        return project_dir

    def test_fusion_snowflake_stays_native_even_with_docker_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        project_dir = self._make_project(tmp_path)
        build = {
            "engine": "dbt",
            "execution": {"runtime": {"platform": "snowflake"}},
            "properties": {},
        }
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner._resolve_dbt_executable",
            lambda: "/opt/fusion/dbt",
        )
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _fake_version_run(FUSION_VERSION_OUTPUT),
        )
        # Docker IS available — the old probe would have routed there.
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.shutil.which",
            lambda name: "/usr/local/bin/docker" if name == "docker" else None,
        )

        cmd = build_dbt_command(build, project_dir)

        assert cmd[0] == "/opt/fusion/dbt"
        assert cmd[1] == "build"
        assert "docker" not in cmd

    def test_core_missing_adapter_still_falls_back_to_docker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Regression guard: the v1 path keeps its Docker fallback when the
        local install lacks the adapter plugin."""
        project_dir = self._make_project(tmp_path)
        build = {
            "engine": "dbt",
            "execution": {"runtime": {"platform": "bigquery"}},
            "properties": {},
        }
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner._resolve_dbt_executable",
            lambda: "/opt/core/dbt",
        )
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.subprocess.run",
            _fake_version_run(CORE_VERSION_OUTPUT),  # lists snowflake only
        )
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner.shutil.which",
            lambda name: "/usr/local/bin/docker" if name == "docker" else None,
        )
        container_sentinel = ["docker", "run", "dbt"]
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner._build_containerized_dbt_command",
            MagicMock(return_value=container_sentinel),
        )

        cmd = build_dbt_command(build, project_dir)

        assert cmd == container_sentinel


# ── _run_dbt_parse_gate — $DBT_EXECUTABLE parity with the runner ─────────


class TestParseGateHonorsDbtExecutable:
    def test_path_form_dbt_executable_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake_dbt = tmp_path / "bin" / "fusion-dbt"
        fake_dbt.parent.mkdir()
        fake_dbt.write_text("#!/bin/sh\n")
        fake_dbt.chmod(0o755)
        monkeypatch.setenv("DBT_EXECUTABLE", str(fake_dbt))

        fake_run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        monkeypatch.setattr("subprocess.run", fake_run)

        import logging

        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        assert gst._run_dbt_parse_gate(project_dir, logging.getLogger("test")) is True

        command = fake_run.call_args.args[0]
        assert command[0] == str(fake_dbt)
        assert command[1] == "parse"
        assert "--project-dir" in command

    def test_multi_token_wrapper_dbt_executable_is_used_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("DBT_EXECUTABLE", "poetry run dbt")
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/local/bin/poetry" if name == "poetry" else None,
        )
        fake_run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        monkeypatch.setattr("subprocess.run", fake_run)

        import logging

        assert gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test")) is True

        command = fake_run.call_args.args[0]
        assert command[:3] == ["poetry", "run", "dbt"]
        assert command[3] == "parse"

    def test_unresolvable_dbt_skips_gate_with_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # No $DBT_EXECUTABLE, nothing on PATH, no venv sibling → warn+skip.
        monkeypatch.setattr(
            "fluid_build.build_runners.dbt.runner._resolve_dbt_executable",
            lambda: None,
        )

        import logging

        assert gst._run_dbt_parse_gate(tmp_path, logging.getLogger("test")) is True
