# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for ``fluid_build.observability.tracing``.

The wrapper has two modes:
1. OTEL disabled (default — no OTEL_EXPORTER_OTLP_ENDPOINT) → decorator
   is a no-op and returns the wrapped function unchanged.
2. OTEL enabled → decorator opens a span with canonical attributes,
   records duration + exit code, marks spans ERROR on exceptions.

Mode 2 tests install an in-memory span exporter via OTEL's InMemorySpan
test helper. If opentelemetry-sdk isn't installed, those tests are
skipped (we don't want to force the optional dep on every contributor).
"""

from __future__ import annotations

import argparse
import os
from unittest.mock import patch

import pytest

from fluid_build.observability import tracing

# -----------------------------------------------------------------------------
# Mode 1: OTEL disabled — decorator is a no-op
# -----------------------------------------------------------------------------


class TestOtelDisabled:
    """When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset (or opentelemetry
    isn't installed), the decorator returns the wrapped function
    verbatim and no span machinery runs. This is the default for every
    caller that doesn't opt in."""

    def test_passes_through_when_env_var_unset(self, monkeypatch):
        """No env var = no OTEL = wrapped function runs as if
        undecorated. Critical: zero import-time side effects, zero
        runtime overhead."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        calls = []

        @tracing.traced_stage("bundle")
        def fn(args):
            calls.append(args)
            return 7

        ns = argparse.Namespace(env="dev", provider="snowflake")
        assert fn(ns) == 7
        assert calls == [ns]

    def test_passes_through_when_env_var_empty(self, monkeypatch):
        """Explicit empty value is treated identically to unset."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

        @tracing.traced_stage("plan")
        def fn(args):
            return 0

        assert fn(argparse.Namespace()) == 0

    def test_exception_propagates_unchanged(self, monkeypatch):
        """When OTEL is disabled, exceptions must propagate without
        being wrapped, masked, or re-raised as a different type."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

        @tracing.traced_stage("apply")
        def fn(args):
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            fn(argparse.Namespace())


# -----------------------------------------------------------------------------
# _otel_enabled
# -----------------------------------------------------------------------------


class TestOtelEnabled:
    def test_enabled_when_endpoint_set_and_lib_available(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        # Assume opentelemetry IS installed in the test env (if not,
        # skip — the _OTEL_AVAILABLE sentinel short-circuits).
        if not tracing._OTEL_AVAILABLE:
            pytest.skip("opentelemetry not installed in this env")
        assert tracing._otel_enabled() is True

    def test_disabled_when_endpoint_blank(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
        assert tracing._otel_enabled() is False

    def test_disabled_when_lib_unavailable(self, monkeypatch):
        """Simulate the soft-import fallback path by patching the
        module-level _OTEL_AVAILABLE sentinel to False."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
        assert tracing._otel_enabled() is False


# -----------------------------------------------------------------------------
# _safe_attr — secret redaction on attribute values
# -----------------------------------------------------------------------------


class TestSafeAttr:
    """Span attributes must be scrubbed of credential-shaped values
    before being shipped to the OTEL collector. The OTEL SDK itself
    doesn't redact — this wrapper is the only gate."""

    def test_preserves_simple_primitives(self):
        assert tracing._safe_attr("dev") == "dev"
        assert tracing._safe_attr(42) == 42
        assert tracing._safe_attr(True) is True
        assert tracing._safe_attr(3.14) == 3.14

    def test_empty_string_for_none(self):
        """OTEL drops empty-string-valued attrs; this lets us safely
        pass a None-carrying arg through without producing a literal
        'None' string on the span."""
        assert tracing._safe_attr(None) == ""

    def test_redacts_secret_shaped_strings(self):
        """Real Bearer tokens / API keys that might leak into an arg
        repr must be masked before being attached to a span."""
        # The underlying redact_secret_text handles these patterns;
        # we confirm our wrapper doesn't short-circuit the redactor.
        # The existing redactor masks BOTH the 'Bearer' token AND the
        # JWT payload; we assert the raw payload doesn't survive and
        # that SOME redaction marker is present.
        sample = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.x"
        masked = tracing._safe_attr(sample)
        assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.x" not in masked
        assert (
            "REDACTED" in masked or "***" in masked
        ), f"expected some redaction marker in masked={masked!r}"


# -----------------------------------------------------------------------------
# _args_to_attrs — namespace → attribute dict
# -----------------------------------------------------------------------------


class TestArgsToAttrs:
    def test_pulls_known_fields(self):
        ns = argparse.Namespace(env="prod", provider="snowflake", mode="replace", dry_run=True)
        attrs = tracing._args_to_attrs(ns)
        assert attrs == {
            "fluid.env": "prod",
            "fluid.provider": "snowflake",
            "fluid.mode": "replace",
            "fluid.dry_run": True,
        }

    def test_ignores_absent_fields(self):
        """A Namespace missing every pipeline field produces an empty
        attribute dict — not an error. Every CLI command has different
        args; the decorator must tolerate partial sets."""
        ns = argparse.Namespace(something_unrelated="x")
        attrs = tracing._args_to_attrs(ns)
        assert attrs == {}

    def test_drops_none_valued_fields(self):
        """A field present on the Namespace but set to None is dropped.
        Apply's --build arg is often None, for example."""
        ns = argparse.Namespace(env="dev", mode=None, provider=None)
        attrs = tracing._args_to_attrs(ns)
        assert attrs == {"fluid.env": "dev"}


# -----------------------------------------------------------------------------
# Mode 2: OTEL enabled — spans are emitted with canonical attributes
# -----------------------------------------------------------------------------


@pytest.fixture()
def in_memory_exporter(monkeypatch):
    """Install an in-memory span exporter so tests can assert on the
    actual spans emitted. Uses opentelemetry-sdk's
    ``InMemorySpanExporter`` test helper.

    Resets the tracer-provider state before and after so tests are
    isolated — OTEL's ``set_tracer_provider`` is sticky otherwise.
    """
    if not tracing._OTEL_AVAILABLE:
        pytest.skip("opentelemetry-sdk not installed in this env")

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    # Install fresh tracer provider with in-memory exporter.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The module's lazy init would create its own provider; pre-set
    # one that the module will pick up via trace.get_tracer_provider.
    trace.set_tracer_provider(provider)
    # Reset module cache so _get_tracer re-uses our provider.
    monkeypatch.setattr(tracing, "_tracer", None)
    monkeypatch.setattr(tracing, "_tracer_provider_initialised", True)
    yield exporter


class TestSpanEmission:
    """End-to-end: decorator + real SDK + in-memory exporter."""

    def test_span_has_canonical_name_and_stage_attr(self, in_memory_exporter):
        @tracing.traced_stage("bundle")
        def fn(args):
            return 0

        fn(argparse.Namespace(env="dev", provider="snowflake"))
        spans = in_memory_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "fluid.bundle"
        assert span.attributes["fluid.stage"] == "bundle"
        assert span.attributes["fluid.env"] == "dev"
        assert span.attributes["fluid.provider"] == "snowflake"
        assert span.attributes["fluid.exit_code"] == 0
        assert "fluid.duration_ms" in span.attributes

    def test_span_records_exit_code_from_return_value(self, in_memory_exporter):
        @tracing.traced_stage("apply")
        def fn(args):
            return 2  # conventional "config error" code

        fn(argparse.Namespace())
        span = in_memory_exporter.get_finished_spans()[0]
        assert span.attributes["fluid.exit_code"] == 2

    def test_exception_marks_span_error_and_propagates(self, in_memory_exporter):
        """Exceptions raised inside the wrapped function must:
        1. Propagate to the caller (not be masked).
        2. Mark the span status as ERROR.
        3. Record the exception type + redacted message.
        4. Still record wall-clock duration."""
        from opentelemetry import trace

        @tracing.traced_stage("plan")
        def fn(args):
            raise RuntimeError("something exploded")

        with pytest.raises(RuntimeError, match="something exploded"):
            fn(argparse.Namespace())

        span = in_memory_exporter.get_finished_spans()[0]
        assert span.attributes["fluid.exit_code"] == 1
        assert span.attributes["fluid.exception_type"] == "RuntimeError"
        assert "something exploded" in span.attributes["fluid.exception_message"]
        assert "fluid.duration_ms" in span.attributes
        assert span.status.status_code == trace.StatusCode.ERROR

    def test_exception_message_redacted(self, in_memory_exporter):
        """Secret-shaped values inside an exception message must be
        scrubbed before being attached as a span attribute. Worst case
        to prevent: a credential leaking from DB error text like
        'auth failed for password=xYz123' into OTEL collector."""

        @tracing.traced_stage("apply")
        def fn(args):
            raise RuntimeError(
                "connection failed: Bearer " "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.leak"
            )

        with pytest.raises(RuntimeError):
            fn(argparse.Namespace())

        span = in_memory_exporter.get_finished_spans()[0]
        msg = span.attributes["fluid.exception_message"]
        # Raw JWT fragment must be scrubbed before reaching OTEL.
        assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.leak" not in msg


# -----------------------------------------------------------------------------
# Module surface
# -----------------------------------------------------------------------------


class TestModuleSurface:
    def test_traced_stage_exported(self):
        """Public API is a single decorator."""
        assert "traced_stage" in tracing.__all__
        assert callable(tracing.traced_stage)

    def test_import_has_zero_side_effects(self, monkeypatch):
        """Importing this module must not initialise OTEL, hit the
        network, or raise even when the optional deps are absent.
        Critical property for the CLI startup graph."""
        # This test just asserts the import succeeded (we're already
        # past it via the top-of-file ``from fluid_build.observability
        # import tracing``). The real check is that every other test
        # in this file gets to run without the import blocking.
        assert hasattr(tracing, "traced_stage")
