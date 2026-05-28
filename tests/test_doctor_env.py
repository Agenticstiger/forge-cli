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

"""Tests for ``fluid doctor --env`` — H9 kill-switch discoverability.

Borrow-before-build: ``aws configure list`` / ``gcloud config list`` /
``gh config list`` all surface CLI-recognised env vars as a flat table
with NAME / VALUE / SOURCE / DESCRIPTION. These tests pin that shape
plus the 8 named UX kill switches from the audit.
"""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fluid_build.cli import doctor

LOG = logging.getLogger(__name__)


# ── catalog: the 8 kill switches must be in the listed set ─────────────


REQUIRED_KILL_SWITCHES = (
    "FLUID_FORGE_NO_PICKER",
    "FLUID_FORGE_NO_PREVIEW",
    "FLUID_FORGE_NO_WELCOME",
    "FLUID_FORGE_NO_STREAMING_PREVIEW",
    "FLUID_COPILOT_JUDGE",
    "FLUID_COPILOT_ENRICHMENT",
    "FLUID_JUDGE_SELF_CRITIQUE",
    "FLUID_COPILOT_CHECKPOINT",
)


class TestEnvCatalog:
    def test_all_eight_kill_switches_are_listed(self):
        catalog_names = {row[0] for row in doctor.ENV_KILL_SWITCHES}
        for name in REQUIRED_KILL_SWITCHES:
            assert name in catalog_names, (
                f"kill switch {name} missing from doctor.ENV_KILL_SWITCHES — "
                "UX audit H9 mandated all 8 be surfaced"
            )

    def test_each_entry_has_name_default_description(self):
        for entry in doctor.ENV_KILL_SWITCHES:
            assert len(entry) == 3, f"expected (name, default, description), got {entry!r}"
            name, default, desc = entry
            assert isinstance(name, str) and name.startswith(
                "FLUID_"
            ), f"env-var name must start with FLUID_: {name!r}"
            assert default, f"every entry needs a non-empty default for {name}"
            assert desc, f"every entry needs a non-empty description for {name}"


# ── _resolve_env_state — value resolution ─────────────────────────────


class TestResolveEnvState:
    def test_unset_renders_as_unset_default(self, monkeypatch):
        monkeypatch.delenv("FLUID_FORGE_NO_PICKER", raising=False)
        value, source = doctor._resolve_env_state("FLUID_FORGE_NO_PICKER")
        assert value == "(unset)"
        assert source == "default"

    def test_set_value_renders_with_env_source(self, monkeypatch):
        monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
        value, source = doctor._resolve_env_state("FLUID_FORGE_NO_PICKER")
        assert value == "1"
        assert source == "env"

    def test_empty_string_renders_as_quoted_empty(self, monkeypatch):
        monkeypatch.setenv("FLUID_COPILOT_JUDGE", "")
        value, source = doctor._resolve_env_state("FLUID_COPILOT_JUDGE")
        assert value == '""'
        assert source == "env"


# ── _run_env_listing — JSON output ─────────────────────────────────────


class TestEnvListingJson:
    def _capture_json_run(self, monkeypatch, env=None):
        for name, _, _ in doctor.ENV_KILL_SWITCHES:
            monkeypatch.delenv(name, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        args = SimpleNamespace(env=True, json=True)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = doctor._run_env_listing(args, LOG)
        return rc, buf.getvalue()

    def test_json_shape_includes_required_fields(self, monkeypatch):
        rc, out = self._capture_json_run(monkeypatch)
        assert rc == 0
        payload = json.loads(out)
        assert "env" in payload
        assert isinstance(payload["env"], list)
        # Required keys on every row.
        for row in payload["env"]:
            assert {"name", "value", "source", "default", "description"} <= row.keys()

    def test_json_lists_all_eight_required_switches(self, monkeypatch):
        rc, out = self._capture_json_run(monkeypatch)
        rows = json.loads(out)["env"]
        names = {row["name"] for row in rows}
        for required in REQUIRED_KILL_SWITCHES:
            assert (
                required in names
            ), f"missing required kill switch in --env --json output: {required}"

    def test_set_env_var_renders_with_value_and_env_source(self, monkeypatch):
        rc, out = self._capture_json_run(monkeypatch, env={"FLUID_FORGE_NO_PICKER": "1"})
        rows = json.loads(out)["env"]
        picker_row = next(r for r in rows if r["name"] == "FLUID_FORGE_NO_PICKER")
        assert picker_row["value"] == "1"
        assert picker_row["source"] == "env"

    def test_unset_env_var_renders_as_unset_default(self, monkeypatch):
        rc, out = self._capture_json_run(monkeypatch)
        rows = json.loads(out)["env"]
        picker_row = next(r for r in rows if r["name"] == "FLUID_FORGE_NO_PICKER")
        assert picker_row["value"] == "(unset)"
        assert picker_row["source"] == "default"


# ── _run_env_listing — table/text output ───────────────────────────────


class TestEnvListingTable:
    def test_table_output_lists_all_eight(self, monkeypatch, capsys):
        for name, _, _ in doctor.ENV_KILL_SWITCHES:
            monkeypatch.delenv(name, raising=False)
        args = SimpleNamespace(env=True, json=False)
        rc = doctor._run_env_listing(args, LOG)
        assert rc == 0
        # capsys captures direct stdout writes from Rich + cprint.
        captured = capsys.readouterr().out
        for name in REQUIRED_KILL_SWITCHES:
            assert name in captured, (
                f"plain/Rich --env output is missing kill switch {name}: " f"{captured!r}"
            )

    def test_table_output_renders_descriptions(self, monkeypatch, capsys):
        for name, _, _ in doctor.ENV_KILL_SWITCHES:
            monkeypatch.delenv(name, raising=False)
        args = SimpleNamespace(env=True, json=False)
        doctor._run_env_listing(args, LOG)
        captured = capsys.readouterr().out
        # Rich wraps inside cells, so descriptive phrases can split
        # across lines. Strip whitespace + newlines to make the check
        # wrap-tolerant. Pick one entry's description and assert a
        # distinct fragment survives.
        flat = "".join(captured.split())
        # FLUID_FORGE_NO_PICKER's description contains "5-mode picker".
        assert "5-modepicker" in flat or "5-mode" in captured


# ── dispatch via run() ────────────────────────────────────────────────


class TestRunDispatch:
    def test_run_with_env_flag_dispatches_to_env_listing(self, monkeypatch):
        called = {}

        def _fake_listing(args, logger):
            called["yes"] = True
            return 0

        monkeypatch.setattr(doctor, "_run_env_listing", _fake_listing)
        args = SimpleNamespace(env=True, json=False, scope=None)
        rc = doctor.run(args, LOG)
        assert rc == 0
        assert called.get("yes") is True

    def test_run_without_env_flag_does_not_dispatch_to_env_listing(self, monkeypatch):
        called = {}

        def _fake_listing(args, logger):
            called["yes"] = True
            return 0

        # Stub the heavier downstream so we don't run real checks.
        monkeypatch.setattr(doctor, "_run_env_listing", _fake_listing)
        monkeypatch.setattr(doctor, "_run_scoped", lambda *a, **k: 0)
        monkeypatch.setattr(doctor, "_check_fluid_features", lambda: (True, []))
        monkeypatch.setattr(
            doctor,
            "_check_copilot_readiness",
            lambda: SimpleNamespace(
                ready=True,
                provider="x",
                model="y",
                endpoint="z",
                auth_available=True,
                error=None,
            ),
        )
        monkeypatch.setattr(doctor, "_resolve_extended_diagnostic_script", lambda: None)
        monkeypatch.setattr(doctor, "_print_doctor_summary", lambda **kw: None)
        monkeypatch.setattr(doctor, "_print_copilot_readiness", lambda *a, **k: None)
        monkeypatch.setattr(doctor, "_print_doctor_next_steps", lambda **kw: None)

        args = SimpleNamespace(
            env=False,
            json=False,
            scope=None,
            features_only=False,
            extended=False,
            verbose=False,
        )
        rc = doctor.run(args, LOG)
        assert rc == 0
        # Crucially: when --env is not set, the env-listing helper is NOT
        # invoked.
        assert "yes" not in called


# ── argparse registration ──────────────────────────────────────────────


class TestArgparseRegistration:
    def test_env_flag_registered_on_doctor_parser(self):
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        doctor.register(subparsers)
        # Parse only the env flag — confirms argparse accepts it.
        ns = parser.parse_args(["doctor", "--env"])
        assert getattr(ns, "env", False) is True

    def test_env_with_json_flag_combination(self):
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        doctor.register(subparsers)
        ns = parser.parse_args(["doctor", "--env", "--json"])
        assert getattr(ns, "env", False) is True
        assert getattr(ns, "json", False) is True
