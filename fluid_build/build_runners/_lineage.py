# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""OpenLineage emission for acquisition runs.

The wire format is owned by the official ``openlineage-python`` client
rather than hand-rolled here. FLUID's internal
:class:`~fluid_build.api.lineage.RunEvent` is translated by
:func:`to_openlineage` into ``openlineage.client.event_v2.RunEvent`` and
serialised by their ``Serde``, so ``producer``, ``schemaURL`` and the
nested ``run`` / ``job`` objects are stamped by the reference
implementation instead of being reinvented.

Borrowed-not-built per /borrow-before-build:
  PyPI:   https://pypi.org/project/openlineage-python/
  GitHub: https://github.com/OpenLineage/OpenLineage/tree/main/client/python
  Spec:   https://openlineage.io/spec/2-0-2/OpenLineage.json

Three defects in the previous hand-rolled encoder are fixed here, all of
which made every emitted payload non-conformant and therefore rejected by
Marquez, DataHub and any other OpenLineage consumer:

1. ``producer`` and ``schemaURL`` were absent. Both are required by
   ``BaseEvent``.
2. ``run`` and ``job`` were flattened to snake_case top-level keys
   (``run_id`` / ``job_namespace`` / ``job_name``). The spec requires
   nested ``run`` and ``job`` objects.
3. ``runId`` carried FLUID's native run id, which is a sortable base32
   string, not the ``format: uuid`` the spec requires. It is now mapped
   through ``generate_static_uuid`` (deterministic UUIDv7, so the
   OpenLineage id stays sortable exactly like the FLUID one) and the
   native id is preserved in a custom run facet for correlation.

Three emitters:

- :class:`NullLineageEmitter` drops events. The default when no endpoint
  is configured.
- :class:`BufferedLineageEmitter` captures events in memory for tests and
  ``--dry-run`` audit.
- :class:`HttpLineageEmitter` POSTs conformant OpenLineage JSON.

:func:`resolve_lineage_emitter` is the single factory every runner reaches
through. It honours the *standard* ``OPENLINEAGE_URL`` /
``OPENLINEAGE_API_KEY`` environment variables, so an existing Marquez or
DataHub deployment starts receiving FLUID lineage with no FLUID-specific
configuration at all. ``FLUID_OPENLINEAGE_*`` overrides them when the two
need to differ.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Reuse the canonical SSRF post-DNS-resolution gates (RFC1918,
# link-local 169.254.0.0/16 for AWS/GCP metadata, loopback, reserved;
# the broad gate fails closed on DNS errors).
from fluid_build._net import _hostname_is_link_local, _hostname_is_private
from fluid_build.api.lineage import LineageEmitter, RunEvent

LOG = logging.getLogger("fluid.acquire.lineage")

#: Truthy tokens for the boolean environment overrides in this module.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: OpenLineage requires a ``producer`` URI identifying the emitting tool.
_PRODUCER_BASE = "https://github.com/Agenticstiger/forge-cli"

#: Namespace for the custom run facet carrying FLUID's native run id.
FLUID_RUN_FACET_KEY = "fluid_run"

#: Namespace for the custom run facet carrying the engine's own run
#: telemetry (``engine``, ``duration_seconds``, per-engine extras).
#: ``RunResult.facets`` is a *flat* dict of scalars; a spec ``RunFacet``
#: must be an object carrying ``_producer`` + ``_schemaURL``, so the whole
#: engine payload is nested under this one conformant facet rather than
#: spilled as bare scalars into ``run.facets``.
FLUID_ENGINE_FACET_KEY = "fluid_engine"

#: Default OpenLineage receiver path. Matches Marquez and the OpenLineage
#: client's own default so ``OPENLINEAGE_URL=https://marquez.example`` works
#: without further configuration.
DEFAULT_LINEAGE_ENDPOINT_PATH = "api/v1/lineage"


def producer_uri() -> str:
    """The ``producer`` URI stamped onto every emitted event.

    Version-pinned so a consumer can tell which FLUID release produced a
    given event, which is what OpenLineage intends the field for.
    """
    try:
        from fluid_build import __version__

        return f"{_PRODUCER_BASE}/tree/v{__version__}"
    except Exception:  # noqa: BLE001 - version lookup must never break emission
        return _PRODUCER_BASE


def _parse_event_time(event_time: str) -> datetime:
    """Parse FLUID's ISO-8601 timestamp, defaulting to UTC.

    ``utc_now_iso`` emits ``%Y-%m-%dT%H:%M:%SZ``. ``fromisoformat`` handles
    the ``Z`` suffix only from 3.11, so normalise it first. Any unparseable
    value falls back to now, because a slightly wrong timestamp is better
    than a dropped event on an observability path.
    """
    try:
        normalised = event_time.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalised)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def _safe_event_time(event_time: str) -> str:
    """Return an ISO-8601 string the OpenLineage client will accept.

    The client validates ``eventTime`` with ``dateutil.parser.isoparse`` and
    raises on anything malformed. A well-formed value is passed through
    untouched so the emitted timestamp matches the run record exactly;
    anything unparseable is replaced with now, because dropping a run event
    over a bad timestamp would be a worse outcome than a slightly wrong one.
    """
    try:
        from dateutil import parser

        parser.isoparse(event_time)
        return event_time
    except Exception:  # noqa: BLE001
        LOG.debug("Unparseable lineage event_time, substituting current time")
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id_to_uuid(run_id: str, event_time: str) -> str:
    """Map a FLUID run id onto the ``format: uuid`` the spec requires.

    FLUID run ids are sortable base32 strings (``01<ts_b32><rand>``), not
    UUIDs. ``generate_static_uuid`` is the OpenLineage client's own helper
    for precisely this case (its docstring names "external runId"), and it
    returns a UUIDv7, so the derived id stays monotonic for increasing
    timestamps just like the FLUID one. Deterministic, so re-emitting the
    same run produces the same OpenLineage id.

    ``event_time`` must be the instant the **run** started, identical for
    every event of that run. ``generate_static_uuid`` folds the instant into
    the UUIDv7 timestamp field, so seeding it with each event's own
    timestamp produced a *different* ``runId`` for START and COMPLETE of the
    same run — which makes the pair uncorrelatable in Marquez/DataHub.
    """
    from openlineage.client.uuid import generate_static_uuid

    return str(generate_static_uuid(_parse_event_time(event_time), run_id.encode("utf-8")))


def _as_run_facet(payload: Dict[str, Any], *, schema_path: str) -> Dict[str, Any]:
    """Wrap ``payload`` as a spec-conformant ``RunFacet`` object.

    ``#/$defs/RunFacet`` inherits ``BaseFacet``, whose ``required`` list is
    ``["_producer", "_schemaURL"]``, so every value in ``run.facets`` must be
    an object carrying both. Keys already present in ``payload`` win, so a
    facet that arrives pre-stamped is passed through unchanged.
    """
    producer = producer_uri()
    return {
        "_producer": producer,
        "_schemaURL": f"{_PRODUCER_BASE}/blob/main/{schema_path}",
        **payload,
    }


def to_openlineage(event: RunEvent) -> Any:
    """Translate a FLUID ``RunEvent`` into a spec-conformant OpenLineage event.

    Returns ``openlineage.client.event_v2.RunEvent``. The official client
    supplies ``producer`` and ``schemaURL`` defaults, but we set ``producer``
    explicitly so it identifies FLUID rather than the client library.
    """
    from openlineage.client.event_v2 import (
        InputDataset,
        Job,
        OutputDataset,
        Run,
        RunState,
    )
    from openlineage.client.event_v2 import (
        RunEvent as OpenLineageRunEvent,
    )

    producer = producer_uri()
    event_time = _safe_event_time(event.event_time)

    run_facets: Dict[str, Any] = dict(event.run_facets or {})
    # Preserve FLUID's native run id so an operator can correlate an
    # OpenLineage event back to the .fluid run record and the CLI output.
    run_facets[FLUID_RUN_FACET_KEY] = {
        "_producer": producer,
        "_schemaURL": f"{_PRODUCER_BASE}/blob/main/fluid_build/api/lineage.py",
        "fluidRunId": event.run_id,
    }

    # Seed the UUIDv7 from the RUN's start instant, not this event's, so every
    # event of one run carries the same ``runId``.
    run_uuid_seed_time = _safe_event_time(event.run_started_at or event.event_time)
    run = Run(runId=run_id_to_uuid(event.run_id, run_uuid_seed_time), facets=run_facets)
    job = Job(namespace=event.job_namespace, name=event.job_name)

    inputs = [
        InputDataset(namespace=d.namespace, name=d.name, facets=dict(d.facets or {}))
        for d in (event.inputs or [])
    ]
    outputs = [
        OutputDataset(namespace=d.namespace, name=d.name, facets=dict(d.facets or {}))
        for d in (event.outputs or [])
    ]

    return OpenLineageRunEvent(
        eventTime=event_time,
        eventType=RunState[event.event_type.value],
        run=run,
        job=job,
        inputs=inputs,
        outputs=outputs,
        producer=producer,
    )


def encode_event(event: RunEvent) -> dict:
    """Serialise a FLUID ``RunEvent`` as conformant OpenLineage JSON-ready dict."""
    from openlineage.client.serde import Serde

    return Serde.to_dict(to_openlineage(event))


class NullLineageEmitter(LineageEmitter):
    def emit(self, event: RunEvent) -> None:  # noqa: D401
        return None

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        return True


@dataclass
class BufferedLineageEmitter(LineageEmitter):
    """Captures events in memory, primarily for tests and ``--dry-run`` audit."""

    events: List[RunEvent] = field(default_factory=list)

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        return True


def _warn_once(key: str, message: str) -> None:
    """Log a warning AND surface it on the CLI, at most once per process.

    A lineage refusal that only reaches ``LOG.warning`` is invisible during
    a normal ``fluid apply`` (the ``fluid.acquire.lineage`` logger is not
    printed at default verbosity), so an operator who configured
    ``OPENLINEAGE_URL`` had no way to learn that nothing shipped. Deduped by
    ``key`` because emission is per-event and a run emits at least two.
    """
    if key in _WARNED:
        return
    _WARNED.add(key)
    LOG.warning("%s", message)
    try:
        from fluid_build._console import warning as console_warning

        console_warning(message)
    except Exception:  # pragma: no cover — console is best-effort
        pass


#: Process-level dedup set for :func:`_warn_once`.
_WARNED: set = set()


def _allow_private_endpoints() -> bool:
    """Whether the lineage emitter may POST to a private/loopback endpoint.

    Defaults to **True** to match the sibling catalog registrars, which pass
    ``allow_private=True`` to :func:`~fluid_build.util.safe_http.safe_httpx_client`
    for exactly the same reason: an OpenLineage receiver (Marquez, DataHub
    GMS) is an internal service almost by definition — the project's own
    CLAUDE.md prescribes ``OPENLINEAGE_URL=http://marquez:5000/api/v1/lineage``.
    Blocking every RFC1918 address turned the headline feature of #467 into a
    silent no-op for every realistic deployment.

    Link-local / instance-metadata addresses stay blocked regardless (see
    :func:`~fluid_build._net._hostname_is_link_local`) — that is the actual
    SSRF-exfil shape. Set ``FLUID_OPENLINEAGE_ALLOW_PRIVATE=false`` to restore
    the strict public-only gate, e.g. when the endpoint is not operator-owned.
    """
    raw = os.environ.get("FLUID_OPENLINEAGE_ALLOW_PRIVATE")
    if raw is None:
        return True
    return raw.strip().lower() in _TRUTHY


@dataclass
class HttpLineageEmitter(LineageEmitter):
    """POSTs each event to ``endpoint``.

    Soft-fails: emission errors are logged but do NOT abort the run,
    because lineage is observability, not correctness.

    Security: ``endpoint`` comes from the environment via
    :func:`resolve_lineage_emitter`, which is a trusted source, but a gate
    is kept unconditional as defence in depth for any future caller that
    constructs this emitter from less trustworthy configuration. Before any
    POST the endpoint host is resolved and checked:

    * ``allow_private=False`` — the broad :func:`_hostname_is_private` gate
      (private, loopback, link-local, reserved; fails closed on DNS error).
    * ``allow_private=True`` (the default, see :func:`_allow_private_endpoints`)
      — the narrow :func:`_hostname_is_link_local` gate, so an internal
      Marquez/DataHub receiver works while a Bearer-token-bearing POST to
      ``http://169.254.169.254/`` is still refused.

    The request uses :mod:`httpx` with ``follow_redirects=False`` and
    ``verify=True`` so the auth header is never re-sent across a 30x
    redirect to an internal host.

    The gate runs per-emit rather than once at construction, which is what
    makes it resistant to DNS rebinding.
    """

    endpoint: str
    timeout_seconds: float = 5.0
    api_key: Optional[str] = None
    allow_private: bool = True

    def emit(self, event: RunEvent) -> None:
        try:
            import httpx

            host = urlparse(self.endpoint).hostname
            if not host:
                _warn_once(
                    "no-host",
                    "OpenLineage emission skipped: endpoint has no resolvable host",
                )
                return
            if self.allow_private:
                if _hostname_is_link_local(host):
                    _warn_once(
                        f"link-local:{host}",
                        f"OpenLineage emission skipped: endpoint host {host!r} resolves to "
                        "a link-local / cloud instance-metadata address, refusing to POST "
                        "(SSRF guard)",
                    )
                    return
            elif _hostname_is_private(host):
                # Strict mode (FLUID_OPENLINEAGE_ALLOW_PRIVATE=false).
                _warn_once(
                    f"private:{host}",
                    f"OpenLineage emission skipped: endpoint host {host!r} resolves to "
                    "a private/loopback/link-local/cloud-metadata address, refusing to "
                    "POST (SSRF guard). Unset FLUID_OPENLINEAGE_ALLOW_PRIVATE=false to "
                    "allow an internal receiver.",
                )
                return

            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                verify=True,
            ) as client:
                resp = client.post(self.endpoint, json=self._encode(event), headers=headers)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            # Class-only, because httpx error messages can echo the endpoint URL.
            # Surfaced on the CLI too: "lineage configured but nothing shipped"
            # is precisely the state an operator must not have to guess at.
            _warn_once(
                f"transport:{type(exc).__name__}",
                f"OpenLineage emission failed (non-fatal): {type(exc).__name__}",
            )

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        return True

    @staticmethod
    def _encode(event: RunEvent) -> dict:
        return encode_event(event)


def _dataset_namespace(kind: Optional[str]) -> str:
    """Namespace for a dataset identified only by its connector kind.

    Deliberately derived from ``kind`` alone and never from the resolved
    connection. ``ConnectionSpec.raw`` carries host, user and password after
    env interpolation, and lineage events travel to a third-party receiver,
    so folding the connection into the namespace would exfiltrate
    credentials into someone else's catalog. OpenLineage's naming
    convention wants ``postgres://host:port`` here; emitting the bare
    scheme is the conservative subset that cannot leak.
    """
    return kind or "fluid"


def build_run_event(
    ctx: Any,
    *,
    event_type: Any,
    event_time: str,
    result: Any = None,
) -> RunEvent:
    """Assemble a FLUID ``RunEvent`` from an acquisition ``RunContext``.

    Best-effort throughout: every field is pulled with ``getattr`` so a
    partially-built context still yields an emittable event rather than
    breaking the run.
    """
    from fluid_build.api.lineage import DatasetFacet
    from fluid_build.observability.secret_redactor import redact_value

    source = getattr(ctx, "source", None)
    kind = getattr(source, "kind", None)
    # Stream names pass through ``_resolve_env_placeholders`` before they
    # reach us, so a contract declaring ``streams: ["{{ env.DB_PASSWORD }}"]``
    # would carry a resolved secret here. That already lands in the on-disk
    # run record, but a lineage event leaves the machine, so scrub it.
    streams = [redact_value(s) for s in (getattr(source, "streams", None) or [])]

    inputs = [DatasetFacet(namespace=_dataset_namespace(kind), name=s) for s in streams]
    if not inputs and kind:
        inputs = [DatasetFacet(namespace=_dataset_namespace(kind), name=kind)]

    product_id = getattr(ctx, "product_id", "unknown")
    build_id = getattr(ctx, "build_id", "unknown")
    outputs = [DatasetFacet(namespace="fluid", name=product_id)]

    run_facets: Dict[str, Any] = {}
    run_started_at: Optional[str] = None
    if result is not None:
        facets = getattr(result, "facets", None)
        if isinstance(facets, dict):
            # Redact before the facets leave the machine. ``result.facets``
            # is the same field ``_state.write_run_record`` scrubs through
            # ``redact_value`` before touching disk, because engine traces
            # land in it verbatim (Kafka Connect task status has carried
            # ``sasl.jaas.config`` and connector passwords). A lineage event
            # is POSTed to an external receiver, so skipping the scrub here
            # would be a strictly worse leak than the on-disk one that
            # chokepoint exists to prevent.
            redacted = redact_value(facets)
            if isinstance(redacted, dict) and redacted:
                # ``RunResult.facets`` is a FLAT dict of engine scalars
                # (``{"engine": "duckdb", "duration_seconds": 0.02}``).
                # Copying it straight into ``run.facets`` made every
                # terminal event fail spec validation — a run facet must be
                # an object with ``_producer`` + ``_schemaURL``. Nest the
                # whole payload under one conformant custom facet instead.
                run_facets[FLUID_ENGINE_FACET_KEY] = _as_run_facet(
                    redacted, schema_path="fluid_build/build_runners/_lineage.py"
                )
        started = getattr(result, "started_at", None)
        if isinstance(started, str) and started:
            run_started_at = started

    return RunEvent(
        event_type=event_type,
        event_time=event_time,
        run_id=getattr(ctx, "run_id", "unknown"),
        job_namespace="fluid",
        job_name=f"{product_id}.{build_id}",
        inputs=inputs,
        outputs=outputs,
        run_facets=run_facets,
        run_started_at=run_started_at,
    )


def emit_run_event(ctx: Any, *, event_type: Any, event_time: str, result: Any = None) -> None:
    """Emit one lineage event for ``ctx``, never raising.

    Lineage is observability, not correctness, so any failure assembling or
    emitting the event is swallowed with a debug log. The emitter itself
    already soft-fails on transport errors; this guards the assembly step.
    """
    emitter = getattr(ctx, "lineage", None)
    if emitter is None or isinstance(emitter, NullLineageEmitter):
        return
    try:
        emitter.emit(
            build_run_event(ctx, event_type=event_type, event_time=event_time, result=result)
        )
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Lineage event assembly failed (non-fatal): %s", type(exc).__name__)


def _endpoint_from_env() -> Optional[str]:
    """Resolve the lineage endpoint from environment.

    ``FLUID_OPENLINEAGE_URL`` wins, then the standard ``OPENLINEAGE_URL``.
    Honouring the standard variable is deliberate: a team that already runs
    Marquez or DataHub with ``OPENLINEAGE_URL`` set gets FLUID lineage with
    no extra configuration.
    """
    base = os.environ.get("FLUID_OPENLINEAGE_URL") or os.environ.get("OPENLINEAGE_URL")
    if not base:
        return None
    base = base.rstrip("/")
    path = (
        os.environ.get("FLUID_OPENLINEAGE_ENDPOINT")
        or os.environ.get("OPENLINEAGE_ENDPOINT")
        or DEFAULT_LINEAGE_ENDPOINT_PATH
    ).lstrip("/")
    return f"{base}/{path}"


def resolve_lineage_emitter() -> LineageEmitter:
    """Build the emitter for this process from environment configuration.

    Returns :class:`NullLineageEmitter` when no endpoint is configured, which
    keeps emission strictly opt-in and zero-cost by default.
    """
    endpoint = _endpoint_from_env()
    if not endpoint:
        return NullLineageEmitter()

    api_key = os.environ.get("FLUID_OPENLINEAGE_API_KEY") or os.environ.get("OPENLINEAGE_API_KEY")
    raw_timeout = os.environ.get("FLUID_OPENLINEAGE_TIMEOUT_SECONDS")
    try:
        timeout_seconds = float(raw_timeout) if raw_timeout else 5.0
    except ValueError:
        LOG.warning(
            "Ignoring non-numeric FLUID_OPENLINEAGE_TIMEOUT_SECONDS=%r, using 5.0",
            raw_timeout,
        )
        timeout_seconds = 5.0

    LOG.debug("OpenLineage emission enabled")
    return HttpLineageEmitter(
        endpoint=endpoint,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
        allow_private=_allow_private_endpoints(),
    )
