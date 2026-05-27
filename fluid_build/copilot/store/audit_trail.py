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

"""Audit-trail writer + reader/aggregator for staged forge operations.

Two complementary surfaces:

* :func:`write_audit_event` — fire-and-forget writer used by every
  mutating MCP tool, the staged coordinator, and the CLI subcommands
  that write to disk. Each call lands one timestamped JSON file under
  ``~/.fluid/store/audit/`` (override with ``root``). When a non-null
  :class:`Store` backend is configured via ``FLUID_STORE_BACKEND``,
  the same event is **also** written through ``Store.put`` under the
  ``audit`` namespace — Postgres/Sqlite/Vector backends therefore see
  every event without losing the on-disk fallback. The file write
  remains canonical so a Store outage never breaks audit capture.

* :class:`AuditReportGenerator` — the cumulative-trail reader. Walks
  the audit directory, parses every event document, and emits an
  aggregated report (filtered by event type, time window, or payload
  predicate). Closes plan-gap V2.3.4: until now operators had to grep
  the JSON files by hand to answer "what did the staged pipeline do
  in workspace X yesterday?" — now ``AuditReportGenerator`` is the
  one-line answer.

The generator is deliberately I/O-light and side-effect free:
``walk_events`` returns an iterator so a caller listing 50k audit
events doesn't pay the memory cost of materializing the full list.
``generate_report`` collects on demand.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

_log = logging.getLogger(__name__)


# Namespace name used when audit events round-trip through the Store
# interface. Kept as a string literal here to avoid an import cycle —
# ``copilot.store.namespaces`` already declares ``AUDIT_NAMESPACE``
# with the same value; tests cross-check both via
# ``test_audit_writes_through_store``.
_AUDIT_NAMESPACE = "audit"


def _build_event_key(event: str, when: datetime) -> str:
    """Build a chronologically-sortable Store key for an audit event.

    Mirrors the file-naming convention so a side-by-side ``store.query``
    + on-disk ``ls`` produce the same ordering. Slashes in ``event``
    are flattened so ``mcp/update_entity`` doesn't blow up the key
    delimiter on backends that interpret ``/`` (e.g. FileBackend).
    """
    aware = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
    timestamp = aware.strftime("%Y%m%dT%H%M%SZ")
    micro = f"{aware.microsecond:06d}"
    safe_event = (event or "event").strip().replace("/", "_") or "event"
    # Microseconds disambiguate events emitted in the same second so we
    # don't lose write traffic on the (namespace, key) primary key.
    return f"{timestamp}_{micro}_{safe_event}"


def _route_to_store(event: str, document: Dict[str, Any], when: datetime) -> bool:
    """Attempt to mirror ``document`` to the configured Store backend.

    Returns ``True`` when the write landed on a non-Null backend so the
    caller can log appropriately. Any exception from the Store layer is
    swallowed — the file write is canonical and we never want audit
    capture to crash on a flaky Postgres/Sqlite connection.

    When ``FLUID_STORE_BACKEND`` is unset/``file``/``null`` we skip the
    Store hop entirely (FileBackend would just duplicate the on-disk
    write performed below; NullBackend is a no-op).
    """
    backend_raw = os.environ.get("FLUID_STORE_BACKEND")
    if not backend_raw:
        return False
    backend = backend_raw.strip().lower()
    if backend in {"", "file", "null", "none", "0", "disabled"}:
        return False
    try:
        # Imported lazily so the audit_trail module never costs anyone
        # the psycopg / sqlite import on the happy file-only path, and
        # so a circular import via ``copilot.store.factory`` (which
        # already imports from ``copilot.store.backends.*``) is
        # impossible at module-load time.
        from .factory import resolve_store
    except Exception as exc:  # pragma: no cover — defensive
        _log.debug("fluid.copilot.audit_trail.store_import_failed: %s", exc)
        return False
    try:
        store = resolve_store()
    except Exception as exc:
        _log.debug("fluid.copilot.audit_trail.store_resolve_failed: %s", exc)
        return False
    key = _build_event_key(event, when)
    try:
        store.put(
            _AUDIT_NAMESPACE,
            key,
            document,
            metadata={"event": event, "timestamp_utc": document.get("timestamp_utc")},
        )
        return True
    except Exception as exc:
        _log.debug("fluid.copilot.audit_trail.store_put_failed: %s", exc)
        return False


def write_audit_event(
    event: str,
    *,
    payload: Dict[str, Any],
    root: Optional[Path] = None,
) -> Path:
    base = (root or (Path.home() / ".fluid" / "store" / "audit")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    when = datetime.now(timezone.utc)
    timestamp = when.strftime("%Y%m%dT%H%M%SZ")
    path = base / f"{timestamp}_{event}.json"
    document = {
        "event": event,
        "timestamp_utc": when.isoformat(),
        "payload": payload,
    }
    # File write is the canonical fallback (always lands somewhere) +
    # the source of truth for ``AuditReportGenerator``'s on-disk walker.
    path.write_text(json.dumps(document, indent=2, sort_keys=True, default=str), encoding="utf-8")
    # MEMORY-E2E-A finding #54: dual-write through the configured Store
    # so Postgres/Sqlite/Vector backends actually see audit traffic.
    # File write is canonical; the Store mirror is best-effort. When
    # an explicit ``root`` is passed (tests, sandboxed callers) we
    # honour the existing isolation contract by skipping the Store
    # hop — the test is asking "write a file here", not "fan out to
    # the user's global Store".
    if root is None:
        _route_to_store(event, document, when)
    return path


# ---------------------------------------------------------------------
# Report generator (V2.3.4)
# ---------------------------------------------------------------------


@dataclass
class AuditEvent:
    """One parsed audit document.

    Carries the original ``event`` name, parsed ``timestamp_utc``,
    and ``payload`` dict alongside the on-disk ``source_path`` so
    consumers (the CLI report, downstream forensic tooling) can
    drill from a summary entry back to the source file.
    """

    event: str
    timestamp: datetime
    payload: Dict[str, Any]
    source_path: Path

    @classmethod
    def from_document(cls, path: Path, document: Dict[str, Any]) -> "AuditEvent":
        ts_raw = document.get("timestamp_utc") or ""
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            # Documents from older callers may use a different format;
            # fall back to the file's mtime so the event is still
            # ordered correctly.
            ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return cls(
            event=str(document.get("event") or ""),
            timestamp=ts,
            payload=document.get("payload") or {},
            source_path=path,
        )


@dataclass
class AuditReport:
    """Aggregated summary of audit events over a window.

    ``events`` carries the (potentially-filtered) event list ordered
    by ``timestamp`` ascending. ``counts_by_event`` is a quick
    histogram for the operator's eye-line. ``window`` records the
    filter range so a printed report can name the period it covers.
    """

    events: List[AuditEvent] = field(default_factory=list)
    counts_by_event: Dict[str, int] = field(default_factory=dict)
    window: Dict[str, Optional[datetime]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly summary suitable for ``fluid memory show audit``
        output or downstream serialization."""
        return {
            "window": {
                "from": self.window.get("from").isoformat() if self.window.get("from") else None,
                "to": self.window.get("to").isoformat() if self.window.get("to") else None,
            },
            "total_events": len(self.events),
            "counts_by_event": dict(self.counts_by_event),
            "events": [
                {
                    "event": e.event,
                    "timestamp": e.timestamp.isoformat(),
                    "payload": e.payload,
                    "source_path": str(e.source_path),
                }
                for e in self.events
            ],
        }


class AuditReportGenerator:
    """Walk an audit directory and aggregate events into a report.

    Construction takes the audit ``root`` (defaults to
    ``~/.fluid/store/audit/``); :meth:`walk_events` yields every
    parseable :class:`AuditEvent`; :meth:`generate_report` filters and
    aggregates.

    Three filtering knobs:

    * ``event_filter`` — exact-match event name (e.g.
      ``"mcp_update_entity"``) or a callable that accepts the event
      name and returns ``True`` to keep.
    * ``since`` / ``until`` — inclusive timestamp window.
    * ``payload_filter`` — callable accepting the payload dict; useful
      for "every event involving contract path X."

    Errors per file are logged at debug level and skipped — a single
    malformed audit document must not poison the rest of the report.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (root or (Path.home() / ".fluid" / "store" / "audit")).expanduser()

    def walk_events(self) -> Iterator[AuditEvent]:
        """Yield every parseable :class:`AuditEvent` under ``root``,
        sorted by filename (== sorted by timestamp under the
        ``YYYYMMDDTHHMMSSZ_<event>.json`` convention)."""
        if not self.root.is_dir():
            return
        for path in sorted(self.root.glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # pragma: no cover — defensive
                _log.debug(
                    "fluid.copilot.audit_report.skip: %s (%s)",
                    path,
                    exc,
                )
                continue
            try:
                yield AuditEvent.from_document(path, document)
            except Exception as exc:  # pragma: no cover — defensive
                _log.debug(
                    "fluid.copilot.audit_report.parse_failed: %s (%s)",
                    path,
                    exc,
                )

    def generate_report(
        self,
        *,
        event_filter: Optional[str | Callable[[str], bool]] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        payload_filter: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> AuditReport:
        """Collect events matching every supplied filter into a
        single :class:`AuditReport`.

        All filters are AND-combined. Absent filters are wildcards —
        passing zero arguments returns every audit event under
        ``root``.
        """
        if isinstance(event_filter, str):
            event_name = event_filter

            def event_predicate(name: str) -> bool:
                return name == event_name

        else:
            event_predicate = event_filter or (lambda _name: True)

        events: List[AuditEvent] = []
        counts: Dict[str, int] = {}
        for evt in self.walk_events():
            if not event_predicate(evt.event):
                continue
            if since is not None and evt.timestamp < since:
                continue
            if until is not None and evt.timestamp > until:
                continue
            if payload_filter is not None and not payload_filter(evt.payload):
                continue
            events.append(evt)
            counts[evt.event] = counts.get(evt.event, 0) + 1

        return AuditReport(
            events=events,
            counts_by_event=counts,
            window={"from": since, "to": until},
        )


__all__ = [
    "write_audit_event",
    "AuditEvent",
    "AuditReport",
    "AuditReportGenerator",
]
