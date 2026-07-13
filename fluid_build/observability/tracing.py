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

"""OpenTelemetry tracing wrapper for the 11-stage FLUID pipeline.

Opt-in via the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var —
no endpoint = no spans = zero overhead. When enabled, every decorated
stage entry point (:func:`cli.bundle.run`, :func:`cli.plan.run`, etc.)
emits one span ``fluid.<stage>`` with these attributes:

- ``fluid.stage``         — ``bundle`` / ``validate`` / ``plan`` / ...
- ``fluid.env``           — deployment env (``dev`` / ``staging`` / ``prod``)
- ``fluid.provider``      — target provider (``snowflake`` / ``bigquery`` / ...)
- ``fluid.bundle_digest`` — present on stages that consume a bundle
- ``fluid.plan_digest``   — present on stages that consume a plan
- ``fluid.mode``          — present on apply (``amend`` / ``replace`` / ...)
- ``fluid.duration_ms``   — wall-clock; filled at span close
- ``fluid.exit_code``     — 0 on success, non-zero on CLIError

**Soft-import:** ``opentelemetry-sdk`` and ``opentelemetry-exporter-otlp``
are optional deps. When they're not installed OR when
``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, :func:`traced_stage` returns
the wrapped function unchanged — the decorator is a no-op. This lets
teams that don't want OTEL opt out by simply not setting the env var;
teams that want it install via ``data-product-forge[observability]``
and point the env var at their collector.

**Security note:** span attributes are filtered through the existing
:class:`fluid_build.observability.secret_redactor.SecretRedactingFilter`
so credential-shaped values (SNOWFLAKE_PASSWORD, AWS_SECRET_ACCESS_KEY,
JWTs, etc.) that end up in attribute values are redacted before
emission to the OTEL collector. The OTEL SDK doesn't itself redact
span attributes — this wrapper is the defence-in-depth layer.

Usage::

    from fluid_build.observability.tracing import traced_stage

    @traced_stage("bundle")
    def run(args):
        # args.env, args.provider, etc. are read by the decorator
        # to populate span attributes.
        ...
"""

from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Callable, Dict, Optional

from fluid_build.observability.secret_redactor import redact_secret_text

# Soft-import OTEL. When absent, the decorator returns wrapped
# callables unchanged — zero runtime overhead for opted-out setups.
#
# NB: the OTLP-http *span exporter* is deliberately NOT imported here — it
# drags the ``requests`` + ``google.protobuf`` stacks (~140 modules). Because
# ``cli/validate.py`` imports ``traced_stage`` at module scope and ``validate``
# registers during ``build_parser()``, a module-level exporter import would
# land ``requests`` + ``google.protobuf`` on the ``fluid --help`` cold path in
# any env that has ``opentelemetry`` installed. The exporter is imported lazily
# inside :func:`_get_tracer` (first traced span, endpoint configured) instead.
# Pinned by tests/perf/test_startup_budget.py.
try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import Resource as _OtelResource
    from opentelemetry.sdk.trace import TracerProvider as _OtelTracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor as _OtelBatchSpanProcessor,
    )

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover — skipped when optional deps absent
    _OTEL_AVAILABLE = False
    _otel_trace = None  # type: ignore[assignment]
    _OtelResource = None  # type: ignore[assignment]
    _OtelTracerProvider = None  # type: ignore[assignment]
    _OtelBatchSpanProcessor = None  # type: ignore[assignment]


_logger = logging.getLogger(__name__)

# Tracer + provider are set lazily on the first traced call so that
# importing this module doesn't initialise OTEL (and so that a test
# can clear the env var + re-init without module reloads).
_tracer: Any = None
_tracer_provider_initialised: bool = False


def _otel_enabled() -> bool:
    """Return True when OTEL should emit spans.

    Two conditions must hold:
    1. ``opentelemetry-sdk`` + exporter-otlp are installed.
    2. ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var is set (and non-empty).

    Other common OTEL env vars (``OTEL_SERVICE_NAME``,
    ``OTEL_RESOURCE_ATTRIBUTES``) are honoured by the SDK directly when
    the exporter initialises; we don't special-case them here.
    """
    if not _OTEL_AVAILABLE:
        return False
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return bool(endpoint)


def _get_tracer() -> Any:
    """Return the module tracer, initialising on first call.

    Returns None when OTEL is disabled — callers must guard with
    :func:`_otel_enabled` before invoking tracer methods.
    """
    global _tracer, _tracer_provider_initialised
    if not _otel_enabled():
        return None
    if _tracer is not None:
        return _tracer
    if not _tracer_provider_initialised:
        try:
            # Deferred exporter import (see the module-scope soft-import note):
            # keeps ``requests`` + ``google.protobuf`` off the ``fluid --help``
            # cold path. A missing exporter package is caught by the broad
            # ``except`` below → tracing degrades to a no-op, never a hard error.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter as _OtelOTLPSpanExporter,
            )

            resource = _OtelResource.create(
                {
                    "service.name": os.environ.get("OTEL_SERVICE_NAME", "fluid"),
                    "service.namespace": "fluid-build",
                }
            )
            provider = _OtelTracerProvider(resource=resource)
            exporter = _OtelOTLPSpanExporter()
            provider.add_span_processor(_OtelBatchSpanProcessor(exporter))
            _otel_trace.set_tracer_provider(provider)
            _tracer_provider_initialised = True
        except Exception as exc:
            # OTEL init failure shouldn't break the CLI. Log once at
            # DEBUG so operators who DO want tracing notice the gap.
            _logger.debug("otel_init_failed: %s", exc)
            return None
    _tracer = _otel_trace.get_tracer("fluid_build")
    return _tracer


def _safe_attr(value: Any) -> Any:
    """Redact secret-shaped values before they're attached to a span.

    OTEL span attributes must be str/int/float/bool or list of those.
    Dicts / objects are flattened to ``repr`` then redacted. Returns
    an empty string for None so the attribute is dropped cleanly by
    OTEL when absent (OTEL skips empty-string-valued attrs).
    """
    if value is None:
        return ""
    if isinstance(value, (bool, int, float)):
        return value
    return redact_secret_text(str(value))


def _args_to_attrs(args: Any) -> dict:
    """Pull 0.7.1-pipeline-relevant attributes off an argparse Namespace.

    Reads the common pipeline fields defensively — fields that don't
    exist on a particular command's args are simply absent from the
    span, not errored. Secret-shaped values are scrubbed via
    :func:`_safe_attr`.
    """
    attrs: dict = {}
    for attr_name, spec_key in [
        ("env", "fluid.env"),
        ("provider", "fluid.provider"),
        ("mode", "fluid.mode"),
        ("scheduler", "fluid.scheduler"),
        ("target", "fluid.target"),
        ("dry_run", "fluid.dry_run"),
        ("strict", "fluid.strict"),
    ]:
        if hasattr(args, attr_name):
            v = getattr(args, attr_name, None)
            if v is not None:
                attrs[spec_key] = _safe_attr(v)
    return attrs


def traced_stage(stage_name: str) -> Callable[[Callable[..., int]], Callable[..., int]]:
    """Decorator that wraps a stage entry point in an OTEL span.

    Usage::

        @traced_stage("bundle")
        def run(args) -> int:
            ...

    The wrapped function signature must be ``run(args) -> int`` (the
    canonical fluid CLI subcommand shape). The decorator:

    1. Opens a span named ``fluid.<stage_name>`` on entry (when OTEL
       is enabled).
    2. Populates attributes from the argparse Namespace.
    3. Records the wall-clock duration in ms on exit.
    4. Records the exit code.
    5. Marks the span as ERROR on any exception, re-raises.

    When OTEL is disabled (no env var, or lib not installed), the
    wrapped function is returned unchanged — zero runtime cost.
    """

    def decorator(func: Callable[..., int]) -> Callable[..., int]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> int:
            tracer = _get_tracer()
            if tracer is None:
                # OTEL disabled — pass through directly.
                return func(*args, **kwargs)

            # First positional arg is typically the argparse Namespace
            # for CLI subcommands; grab attributes defensively.
            ns = args[0] if args else None
            attrs = {"fluid.stage": stage_name}
            if ns is not None:
                attrs.update(_args_to_attrs(ns))

            # Cross-stage correlation. Resolves $FLUID_RUN_ID env var,
            # then ``.fluid/run-id.txt``, then generates+persists a new
            # id. Once stamped here, every nested ``traced_span`` and
            # downstream stage shares the same id — operators query
            # ``fluid.run_id="..."`` to reconstruct a multi-stage run.
            try:
                from fluid_build.observability.run_id import get_or_create_run_id

                attrs["fluid.run_id"] = get_or_create_run_id()
            except Exception:  # pragma: no cover — defensive
                # Tracing must never break the CLI. If run_id resolution
                # fails (read-only fs, permission, etc.), continue
                # without the attribute.
                pass

            with tracer.start_as_current_span(f"fluid.{stage_name}") as span:
                for k, v in attrs.items():
                    span.set_attribute(k, v)
                start = time.monotonic()
                try:
                    result = func(*args, **kwargs)
                    duration_ms = int((time.monotonic() - start) * 1000)
                    span.set_attribute("fluid.duration_ms", duration_ms)
                    span.set_attribute("fluid.exit_code", int(result) if result is not None else 0)
                    return result
                except Exception as exc:
                    duration_ms = int((time.monotonic() - start) * 1000)
                    span.set_attribute("fluid.duration_ms", duration_ms)
                    span.set_attribute("fluid.exit_code", 1)
                    # Record exception details (redacted) without
                    # re-raising from the tracing layer itself.
                    span.set_attribute("fluid.exception_type", type(exc).__name__)
                    span.set_attribute("fluid.exception_message", _safe_attr(str(exc)))
                    if _OTEL_AVAILABLE and _otel_trace is not None:
                        span.set_status(_otel_trace.Status(_otel_trace.StatusCode.ERROR))
                    raise

        return wrapper

    return decorator


class _NoOpSpan:
    """Null-object span used when OTEL is disabled.

    Returned by :func:`traced_span` so call sites can use ``with
    traced_span(...) as span:`` unconditionally; when tracing is off
    the methods do nothing and overhead is a few attribute lookups.
    """

    def set_attribute(self, _key: str, _value: Any) -> None:  # noqa: D401
        return None

    def set_status(self, _status: Any) -> None:  # noqa: D401
        return None

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


def traced_span(name: str, attributes: Optional[Dict[str, Any]] = None) -> Any:
    """Open a child span; no-op when OTEL disabled.

    Use this inside internal orchestrators (e.g. the staged-agent
    coordinator) to emit nested spans that roll up under the top-level
    ``fluid.<stage>`` CLI span opened by :func:`traced_stage`. When
    OTEL is disabled, returns a :class:`_NoOpSpan` with zero overhead
    beyond one env-var lookup.

    The ``attributes`` mapping is scrubbed through :func:`_safe_attr`
    so secret-shaped values are redacted before emission.
    """
    tracer = _get_tracer()
    if tracer is None:
        return _NoOpSpan()
    cm = tracer.start_as_current_span(name)
    if attributes:
        # ``start_as_current_span`` returns a context manager; we can't
        # set attributes until ``__enter__`` gives us the span, so wrap
        # in a tiny shim that applies them on entry.
        return _AttributedSpan(cm, {k: _safe_attr(v) for k, v in attributes.items()})
    return cm


class _AttributedSpan:
    """Context manager that sets attributes after span enter.

    OTEL's ``start_as_current_span`` returns a context manager — the
    span object isn't available until ``__enter__``. This wrapper lets
    :func:`traced_span` accept an ``attributes`` dict and apply it
    atomically at entry, matching the ergonomics of :func:`traced_stage`.
    """

    def __init__(self, inner_cm: Any, attributes: Dict[str, Any]) -> None:
        self._inner_cm = inner_cm
        self._attributes = attributes
        self._span: Any = None

    def __enter__(self) -> Any:
        self._span = self._inner_cm.__enter__()
        for k, v in self._attributes.items():
            self._span.set_attribute(k, v)
        return self._span

    def __exit__(self, *exc: Any) -> bool:
        return bool(self._inner_cm.__exit__(*exc))


__all__ = ["traced_stage", "traced_span"]
