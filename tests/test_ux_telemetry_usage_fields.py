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

"""Tests for the ``provider`` + ``run_completed`` UX-telemetry usage fields.

Borrow-before-build: the shape mirrors what dbt-core (``adapter_type`` +
"whether the invocation succeeded") and Next.js (``*_COMPLETED`` events)
capture, and follows the OpenTelemetry semantic-convention guidance to keep
span attributes low-cardinality / enum-like. These pin:

* ``provider`` normalises to a bounded enum-like slug (never a raw model id,
  endpoint, key, or free-form string),
* ``run_completed`` reflects success vs. failure so a completion *rate* is
  computable,
* both project onto the ``forge.invocation`` span **only** behind the existing
  default-OFF consent gate (``DO_NOT_TRACK`` / ``FLUID_TELEMETRY=0`` win).
"""

from __future__ import annotations

import sys

import pytest

from fluid_build.cli import _telemetry_consent as tc
from fluid_build.cli import _ux_telemetry as uxt

# Import the forge_modes entry point at module scope so its ``_template_mode``
# submodule is loaded in the production-safe order. Importing ``_template_mode``
# first (which isort would otherwise arrange) hits a latent
# ``_template_mode``<->``forge_modes`` circular-import — pre-existing and not
# under test here.
from fluid_build.cli import forge_modes as _forge_modes  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Point ~/.fluid at a tmp dir, clear telemetry env, reset the record."""
    monkeypatch.setenv("FLUID_USER_HOME", str(tmp_path / "fluidhome"))
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("FLUID_TELEMETRY", raising=False)
    uxt.reset_telemetry()
    yield
    uxt.reset_telemetry()


# ---------------------------------------------------------------------------
# provider normalisation (low-cardinality / no-PII guarantee)
# ---------------------------------------------------------------------------


class TestProviderNormalisation:
    @pytest.mark.parametrize(
        "raw",
        ["anthropic", "openai", "gemini", "ollama", "claude-code", "local", "litellm"],
    )
    def test_known_providers_pass_through(self, raw):
        tel = uxt.get_telemetry()
        tel.record_provider(raw)
        assert tel.provider == raw

    def test_case_and_whitespace_folded(self):
        tel = uxt.get_telemetry()
        tel.record_provider("  Anthropic  ")
        assert tel.provider == "anthropic"

    def test_unknown_provider_collapses_to_other(self):
        tel = uxt.get_telemetry()
        # An unexpected / potentially PII-bearing value must never leak as-is.
        tel.record_provider("acme-internal-model@10.0.0.5:8080")
        assert tel.provider == "other"

    def test_blank_does_not_clobber_previous(self):
        tel = uxt.get_telemetry()
        tel.record_provider("gemini")
        tel.record_provider("")
        tel.record_provider(None)
        assert tel.provider == "gemini"

    def test_normalize_helper_returns_empty_for_falsy(self):
        assert uxt._normalize_provider("") == ""
        assert uxt._normalize_provider(None) == ""
        assert uxt._normalize_provider("   ") == ""


# ---------------------------------------------------------------------------
# run_completed
# ---------------------------------------------------------------------------


class TestRunCompleted:
    def test_defaults_false(self):
        assert uxt.get_telemetry().run_completed is False

    def test_mark_completed_true(self):
        tel = uxt.get_telemetry()
        tel.mark_completed(True)
        assert tel.run_completed is True

    def test_mark_completed_false(self):
        tel = uxt.get_telemetry()
        tel.mark_completed(True)
        tel.mark_completed(False)
        assert tel.run_completed is False

    def test_mark_completed_default_arg_is_true(self):
        tel = uxt.get_telemetry()
        tel.mark_completed()
        assert tel.run_completed is True


# ---------------------------------------------------------------------------
# span-attribute projection
# ---------------------------------------------------------------------------


class TestSpanAttributes:
    def test_new_fields_present_and_typed(self):
        tel = uxt.get_telemetry()
        tel.record_provider("gemini")
        tel.mark_completed(True)
        attrs = tel.to_span_attributes()
        assert attrs["ux.provider"] == "gemini"
        assert attrs["ux.run_completed"] is True
        # OTel-safe scalar types only.
        assert isinstance(attrs["ux.provider"], str)
        assert isinstance(attrs["ux.run_completed"], bool)

    def test_defaults_are_emit_safe(self):
        attrs = uxt.get_telemetry().to_span_attributes()
        assert attrs["ux.provider"] == ""
        assert attrs["ux.run_completed"] is False


# ---------------------------------------------------------------------------
# Egress gate — the exact live-test-plan scenario (enabled -> emit lands)
# ---------------------------------------------------------------------------


def _install_recording_span(monkeypatch) -> dict:
    """Mirror TestEgressGate.test_emit_proceeds_when_enabled's fake OTel."""
    recorded: dict = {}

    class _Span:
        def is_recording(self):
            return True

        def set_attribute(self, k, v):
            recorded[k] = v

    class _Trace:
        def get_current_span(self):
            return _Span()

    fake_otel = type(sys)("opentelemetry")
    fake_otel.trace = _Trace()
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    return recorded


class TestEgressGateNewFields:
    def test_provider_and_completion_land_when_enabled(self, monkeypatch):
        monkeypatch.setattr(tc, "telemetry_enabled", lambda: True)
        recorded = _install_recording_span(monkeypatch)

        tel = uxt.get_telemetry()
        tel.record_provider("gemini")
        tel.mark_completed(True)
        uxt.emit_telemetry_to_active_span()

        assert recorded.get("ux.provider") == "gemini"
        assert recorded.get("ux.run_completed") is True

    def test_do_not_track_forces_no_emit(self, monkeypatch):
        # DO_NOT_TRACK is the universal kill switch — even a persisted opt-in
        # and a would-be recording span must produce nothing.
        tc.set_telemetry_enabled(True)
        monkeypatch.setenv("DO_NOT_TRACK", "1")

        class _Tripwire:
            def get_current_span(self):  # pragma: no cover - must not run
                raise AssertionError("telemetry emitted while DO_NOT_TRACK set")

        fake_otel = type(sys)("opentelemetry")
        fake_otel.trace = _Tripwire()
        monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)

        uxt.get_telemetry().mark_completed(True)
        uxt.emit_telemetry_to_active_span()  # must not raise / must not emit

    def test_fluid_telemetry_zero_forces_no_emit(self, monkeypatch):
        monkeypatch.setenv("FLUID_TELEMETRY", "0")

        class _Tripwire:
            def get_current_span(self):  # pragma: no cover - must not run
                raise AssertionError("telemetry emitted while FLUID_TELEMETRY=0")

        fake_otel = type(sys)("opentelemetry")
        fake_otel.trace = _Tripwire()
        monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)

        uxt.get_telemetry().mark_completed(True)
        uxt.emit_telemetry_to_active_span()

    def test_default_off_emits_nothing(self, monkeypatch):
        # Nothing set: default is OFF, so no span lookup happens at all.
        class _Tripwire:
            def get_current_span(self):  # pragma: no cover - must not run
                raise AssertionError("telemetry emitted with no opt-in")

        fake_otel = type(sys)("opentelemetry")
        fake_otel.trace = _Tripwire()
        monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)

        uxt.get_telemetry().record_provider("gemini")
        uxt.emit_telemetry_to_active_span()


# ---------------------------------------------------------------------------
# emit_forge_run_span — completion-rate denominator (failure path)
# ---------------------------------------------------------------------------


def _patch_traced_span(monkeypatch) -> dict:
    """Capture the attrs passed to a forge.invocation span."""
    captured: dict = {}

    class _CM:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _fake(name, attributes=None):
        captured["name"] = name
        captured["attrs"] = dict(attributes or {})
        return _CM()

    import fluid_build.observability.tracing as _tracing

    monkeypatch.setattr(_tracing, "traced_span", _fake)
    return captured


class TestEmitForgeRunSpan:
    def test_emits_ux_and_base_attrs_when_enabled(self, monkeypatch):
        monkeypatch.setattr(tc, "telemetry_enabled", lambda: True)
        captured = _patch_traced_span(monkeypatch)

        uxt.get_telemetry().record_provider("gemini")
        uxt.get_telemetry().mark_completed(False)
        uxt.emit_forge_run_span({"fluid.flow": "forge"})

        assert captured["name"] == "forge.invocation"
        assert captured["attrs"]["fluid.flow"] == "forge"
        assert captured["attrs"]["ux.provider"] == "gemini"
        assert captured["attrs"]["ux.run_completed"] is False

    def test_base_attrs_emit_but_ux_suppressed_when_disabled(self, monkeypatch):
        # Consent OFF: operational fluid.* attrs still emit (non-PII), but the
        # behavioural ux.* attrs must not.
        monkeypatch.setattr(tc, "telemetry_enabled", lambda: False)
        captured = _patch_traced_span(monkeypatch)

        uxt.get_telemetry().record_provider("gemini")
        uxt.emit_forge_run_span({"fluid.flow": "forge"})

        assert captured["attrs"] == {"fluid.flow": "forge"}
        assert not any(k.startswith("ux.") for k in captured["attrs"])

    def test_incomplete_helper_marks_false_and_emits(self, monkeypatch):
        import logging

        # Safe: forge_modes (imported at module scope) already loaded
        # _template_mode in the correct order.
        from fluid_build.cli import _template_mode as tm

        monkeypatch.setattr(tc, "telemetry_enabled", lambda: True)
        captured = _patch_traced_span(monkeypatch)

        uxt.get_telemetry().mark_completed(True)  # pretend a stale True
        tm._emit_incomplete_forge_span(logging.getLogger("test"))

        assert uxt.get_telemetry().run_completed is False
        assert captured["attrs"]["ux.run_completed"] is False
        assert captured["attrs"]["fluid.flow"] == "forge"
