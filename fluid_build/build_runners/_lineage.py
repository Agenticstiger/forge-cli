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

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from fluid_build.api.lineage import LineageEmitter, RunEvent

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
    """

    endpoint: str
    timeout_seconds: float = 5.0
    api_key: Optional[str] = None

    def emit(self, event: RunEvent) -> None:
        try:
            import urllib.request

            payload = json.dumps(self._encode(event)).encode("utf-8")
            req = urllib.request.Request(
                self.endpoint,
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("OpenLineage emission failed (non-fatal): %s", exc)

    def flush(self, timeout_seconds: float = 5.0) -> bool:
        return True

    @staticmethod
    def _encode(event: RunEvent) -> dict:
        d = asdict(event)
        d["eventType"] = event.event_type.value
        d.pop("event_type", None)
        d["eventTime"] = d.pop("event_time")
        return d
