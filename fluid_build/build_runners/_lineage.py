# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""OpenLineage emission for acquisition runs.

Two implementations:
- ``HttpLineageEmitter`` — POSTs OL events to a configured endpoint.
- ``NullLineageEmitter`` — drops events; used when emission is disabled.
- ``BufferedLineageEmitter`` — captures events in memory for tests.

Production callers wire the HTTP one with retries (``with_retry``) and a
configurable timeout. The Null one is the safe default when no endpoint
is configured.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

from fluid_build.api.lineage import LineageEmitter, RunEvent

# Reuse the canonical SSRF post-DNS-resolution gate (RFC1918,
# link-local 169.254.0.0/16 — AWS/GCP metadata — loopback, reserved;
# fails closed on DNS errors).
from ._alerter import _hostname_is_private

LOG = logging.getLogger("fluid.acquire.lineage")


class NullLineageEmitter(LineageEmitter):
    def emit(self, event: RunEvent) -> None:  # noqa: D401
        return None

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        return True


@dataclass
class BufferedLineageEmitter(LineageEmitter):
    """Captures events in memory — primarily for tests / `--dry-run` audit."""

    events: List[RunEvent] = field(default_factory=list)

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        return True


@dataclass
class HttpLineageEmitter(LineageEmitter):
    """POSTs each event to ``endpoint``. Soft-fails: emission errors are logged
    but do NOT abort the run — lineage is observability, not correctness.

    Security: ``endpoint`` is operator-configured but can be sourced
    from a foreign contract/manifest, so before any POST the endpoint
    host is run through :func:`_hostname_is_private` — a host that
    resolves to a private/loopback/link-local/cloud-metadata address is
    refused (a Bearer-token-bearing POST to ``http://169.254.169.254/``
    is exactly the SSRF-exfil shape this blocks). The request itself
    uses :mod:`httpx` with ``follow_redirects=False`` and ``verify=True``
    so the auth header is never re-sent across a 30x redirect to an
    internal host.
    """

    endpoint: str
    timeout_seconds: float = 5.0
    api_key: Optional[str] = None

    def emit(self, event: RunEvent) -> None:
        try:
            import httpx

            host = urlparse(self.endpoint).hostname
            if not host:
                LOG.warning("OpenLineage emission skipped: endpoint has no resolvable host")
                return
            if _hostname_is_private(host):
                # Fail-closed SSRF gate: refuse private/metadata targets.
                LOG.warning(
                    "OpenLineage emission skipped: endpoint host %r resolves to "
                    "a private/loopback/link-local/cloud-metadata address — "
                    "refusing to POST (SSRF guard)",
                    host,
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
            # Class-only — httpx error messages can echo the endpoint URL.
            LOG.warning("OpenLineage emission failed (non-fatal): %s", type(exc).__name__)

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        return True

    @staticmethod
    def _encode(event: RunEvent) -> dict:
        d = asdict(event)
        d["eventType"] = event.event_type.value
        d.pop("event_type", None)
        d["eventTime"] = d.pop("event_time")
        return d
