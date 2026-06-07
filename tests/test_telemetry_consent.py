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

"""Tests for the opt-in telemetry consent gate (privacy-preserving).

Borrow-before-build: the precedence ladder (DO_NOT_TRACK > dedicated env
override > persisted config flag > default) mirrors dbt-core's
anonymous-usage-stats resolution and the cross-tool DO_NOT_TRACK
convention. These tests pin FLUID's stricter *default-OFF* posture and
the guarantee that nothing is emitted without an explicit opt-in.
"""

from __future__ import annotations

import pytest

from fluid_build.cli import _telemetry_consent as tc


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Point ~/.fluid at a tmp dir and clear all telemetry env vars."""
    monkeypatch.setenv("FLUID_USER_HOME", str(tmp_path / "fluidhome"))
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("FLUID_TELEMETRY", raising=False)
    yield


class TestDefaultOff:
    def test_default_is_off_with_no_config_no_env(self):
        assert tc.telemetry_enabled() is False

    def test_default_enabled_constant_is_false(self):
        assert tc.DEFAULT_ENABLED is False

    def test_consent_not_recorded_initially(self):
        assert tc.consent_recorded() is False


class TestPrecedence:
    def test_do_not_track_forces_off_even_when_config_enabled(self, monkeypatch):
        tc.set_telemetry_enabled(True)
        assert tc.telemetry_enabled() is True
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        assert tc.telemetry_enabled() is False

    def test_do_not_track_wins_over_env_override_on(self, monkeypatch):
        monkeypatch.setenv("FLUID_TELEMETRY", "1")
        monkeypatch.setenv("DO_NOT_TRACK", "true")
        assert tc.telemetry_enabled() is False

    def test_env_override_on(self, monkeypatch):
        monkeypatch.setenv("FLUID_TELEMETRY", "1")
        assert tc.telemetry_enabled() is True

    def test_env_override_off_beats_persisted_on(self, monkeypatch):
        tc.set_telemetry_enabled(True)
        monkeypatch.setenv("FLUID_TELEMETRY", "0")
        assert tc.telemetry_enabled() is False

    def test_persisted_flag_used_when_no_env(self):
        tc.set_telemetry_enabled(True)
        assert tc.telemetry_enabled() is True
        tc.set_telemetry_enabled(False)
        assert tc.telemetry_enabled() is False


class TestPersistence:
    def test_round_trip_records_consent(self):
        assert tc.set_telemetry_enabled(True) is True
        assert tc.consent_recorded() is True
        assert tc.telemetry_enabled() is True

    def test_set_preserves_unrelated_config_keys(self):
        import yaml

        from fluid_build.paths import user_config_file

        path = user_config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"providers": {"aws": {"default_region": "eu-west-1"}}}))
        tc.set_telemetry_enabled(True)
        data = yaml.safe_load(path.read_text())
        assert data["providers"]["aws"]["default_region"] == "eu-west-1"
        assert data["telemetry"]["enabled"] is True


class TestConsentPrompt:
    def test_non_tty_does_not_prompt_or_record(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        called = {}
        monkeypatch.setattr(tc, "set_telemetry_enabled", lambda v: called.setdefault("v", v))
        tc.maybe_prompt_for_consent()
        assert "v" not in called
        assert tc.telemetry_enabled() is False

    def test_do_not_track_skips_prompt(self, monkeypatch):
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        called = {}
        monkeypatch.setattr(tc, "set_telemetry_enabled", lambda v: called.setdefault("v", v))
        tc.maybe_prompt_for_consent()
        assert "v" not in called

    def test_env_override_skips_prompt(self, monkeypatch):
        monkeypatch.setenv("FLUID_TELEMETRY", "0")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        called = {}
        monkeypatch.setattr(tc, "set_telemetry_enabled", lambda v: called.setdefault("v", v))
        tc.maybe_prompt_for_consent()
        assert "v" not in called

    def test_tty_yes_enables_and_records(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "y")
        tc.maybe_prompt_for_consent()
        assert tc.telemetry_enabled() is True
        assert tc.consent_recorded() is True

    def test_tty_default_no_records_off(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda: "")
        tc.maybe_prompt_for_consent()
        assert tc.telemetry_enabled() is False
        assert tc.consent_recorded() is True

    def test_already_recorded_does_not_reprompt(self, monkeypatch):
        tc.set_telemetry_enabled(False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def _boom():
            raise AssertionError("should not prompt when consent already recorded")

        monkeypatch.setattr("builtins.input", _boom)
        tc.maybe_prompt_for_consent()  # must not raise


class TestEgressGate:
    """The privacy guarantee: no span attrs unless opted in."""

    def test_emit_noops_when_disabled(self, monkeypatch):
        from fluid_build.cli import _ux_telemetry as uxt

        # Disabled (default). emit_telemetry_to_active_span must not even
        # reach the OTel import / span lookup.
        monkeypatch.setattr(tc, "telemetry_enabled", lambda: False)

        class _Tripwire:
            def get_current_span(self):  # pragma: no cover - must not run
                raise AssertionError("telemetry emitted while opted out")

        import sys

        monkeypatch.setitem(sys.modules, "opentelemetry", type(sys)("opentelemetry"))
        # If the gate worked we return before importing opentelemetry.trace.
        uxt.emit_telemetry_to_active_span()  # must not raise

    def test_emit_proceeds_when_enabled(self, monkeypatch):
        from fluid_build.cli import _ux_telemetry as uxt

        monkeypatch.setattr(tc, "telemetry_enabled", lambda: True)
        recorded = {}

        class _Span:
            def is_recording(self):
                return True

            def set_attribute(self, k, v):
                recorded[k] = v

        class _Trace:
            def get_current_span(self):
                return _Span()

        import sys

        fake_otel = type(sys)("opentelemetry")
        fake_otel.trace = _Trace()
        monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
        uxt.reset_telemetry()
        uxt.emit_telemetry_to_active_span()
        assert any(k.startswith("ux.") for k in recorded), recorded


class TestDescribeState:
    def test_describe_state_shape(self):
        st = tc.describe_state()
        assert set(st) >= {
            "enabled",
            "do_not_track",
            "env_override",
            "persisted",
            "consent_recorded",
            "default",
        }
        assert st["enabled"] is False
        assert st["default"] is False
