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

"""Coverage for V2.3.4 — :class:`AuditReportGenerator`.

The generator is the cumulative-trail reader to complement
``write_audit_event``: until V2.3.4 operators had to grep the JSON
files by hand to answer "what did the staged pipeline do in workspace
X yesterday?" The generator turns that into a one-call query with
event-type, time-window, and payload-predicate filters.

The pins below cover three behavioural contracts:

1. **Walk** — every parseable JSON document under the audit root is
   surfaced; malformed files are skipped (logged, not raised) so a
   single bad file doesn't poison the rest of the report.
2. **Filter** — every filter knob (``event_filter`` as string OR
   callable, ``since`` / ``until`` window, ``payload_filter``
   callable) honours its semantics independently AND in combination.
3. **Aggregate** — ``counts_by_event`` and ``window`` round-trip
   through ``to_dict`` so a CLI consumer can ``json.dumps`` the
   summary directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fluid_build.copilot.store.audit_trail import (
    AuditEvent,
    AuditReport,
    AuditReportGenerator,
    write_audit_event,
)


def _seed_event(
    root: Path,
    name: str,
    *,
    timestamp: datetime,
    payload=None,
) -> Path:
    """Hand-write an audit document with a controlled timestamp.

    ``write_audit_event`` always uses ``datetime.now(...)``, which is
    fine for production but useless for ``since/until`` tests. We
    write the file directly so the report's window filter has
    something to discriminate."""
    root.mkdir(parents=True, exist_ok=True)
    stamp = timestamp.strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{stamp}_{name}.json"
    document = {
        "event": name,
        "timestamp_utc": timestamp.isoformat(),
        "payload": payload or {},
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------
# Walk + parse
# ---------------------------------------------------------------------


class TestWalkEvents:
    def test_missing_root_yields_nothing(self, tmp_path: Path):
        gen = AuditReportGenerator(root=tmp_path / "does-not-exist")
        assert list(gen.walk_events()) == []

    def test_empty_root_yields_nothing(self, tmp_path: Path):
        gen = AuditReportGenerator(root=tmp_path)
        assert list(gen.walk_events()) == []

    def test_walk_yields_events_in_filename_order(self, tmp_path: Path):
        """File names follow ``YYYYMMDDTHHMMSSZ_event.json`` so a
        sorted glob returns events in chronological order. The
        report's CLI consumers depend on this ordering."""
        t = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        _seed_event(tmp_path, "second", timestamp=t + timedelta(seconds=1))
        _seed_event(tmp_path, "first", timestamp=t)
        _seed_event(tmp_path, "third", timestamp=t + timedelta(seconds=2))

        gen = AuditReportGenerator(root=tmp_path)
        names = [e.event for e in gen.walk_events()]
        assert names == ["first", "second", "third"]

    def test_malformed_file_skipped(self, tmp_path: Path):
        """A single corrupt JSON file must not abort the walk —
        forensic timelines often contain decades of audit; one bad
        file shouldn't lose everything."""
        t = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        _seed_event(tmp_path, "valid", timestamp=t, payload={"k": "v"})
        # Write a file that's NOT JSON.
        (tmp_path / "20260425T120001Z_corrupt.json").write_text("not json {", encoding="utf-8")
        gen = AuditReportGenerator(root=tmp_path)
        names = [e.event for e in gen.walk_events()]
        assert names == ["valid"]


# ---------------------------------------------------------------------
# AuditEvent.from_document round-trip
# ---------------------------------------------------------------------


class TestAuditEventParse:
    def test_round_trip_preserves_all_fields(self, tmp_path: Path):
        t = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        path = _seed_event(
            tmp_path,
            "mcp_update_entity",
            timestamp=t,
            payload={"path": "/tmp/x.model.json", "entity": "Customer"},
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        evt = AuditEvent.from_document(path, document)
        assert evt.event == "mcp_update_entity"
        assert evt.timestamp == t
        assert evt.payload == {"path": "/tmp/x.model.json", "entity": "Customer"}
        assert evt.source_path == path

    def test_unparseable_timestamp_falls_back_to_mtime(self, tmp_path: Path):
        """A document with a bad ``timestamp_utc`` must not poison
        parse — the helper falls back to file mtime so the event is
        still orderable. Defends against legacy / hand-written audit
        files."""
        path = tmp_path / "20260425T120000Z_legacy.json"
        path.write_text(
            json.dumps({"event": "x", "timestamp_utc": "garbage", "payload": {}}),
            encoding="utf-8",
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        evt = AuditEvent.from_document(path, document)
        # Timestamp must be a real datetime (the file's mtime), not None.
        assert isinstance(evt.timestamp, datetime)


# ---------------------------------------------------------------------
# generate_report — filtering matrix
# ---------------------------------------------------------------------


class TestGenerateReport:
    def _seed_three(self, tmp_path: Path) -> datetime:
        """Three events: two ``mcp_update_entity``, one ``mcp_regenerate_physical``,
        spaced one minute apart. Returns the base timestamp so tests
        can build relative ``since/until`` filters."""
        base = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        _seed_event(tmp_path, "mcp_update_entity", timestamp=base, payload={"entity": "A"})
        _seed_event(
            tmp_path,
            "mcp_update_entity",
            timestamp=base + timedelta(minutes=1),
            payload={"entity": "B"},
        )
        _seed_event(
            tmp_path,
            "mcp_regenerate_physical",
            timestamp=base + timedelta(minutes=2),
            payload={"path": "/tmp/x.fluid.yaml"},
        )
        return base

    def test_no_filters_returns_everything(self, tmp_path: Path):
        self._seed_three(tmp_path)
        report = AuditReportGenerator(root=tmp_path).generate_report()
        assert len(report.events) == 3
        assert report.counts_by_event == {
            "mcp_update_entity": 2,
            "mcp_regenerate_physical": 1,
        }

    def test_event_filter_string_matches_exact(self, tmp_path: Path):
        self._seed_three(tmp_path)
        report = AuditReportGenerator(root=tmp_path).generate_report(
            event_filter="mcp_regenerate_physical"
        )
        assert len(report.events) == 1
        assert report.events[0].event == "mcp_regenerate_physical"

    def test_event_filter_callable_matches_predicate(self, tmp_path: Path):
        """The predicate form supports prefix matching, regex, etc.
        Pin a simple ``startswith`` use case so we know the callable
        path is exercised."""
        self._seed_three(tmp_path)
        report = AuditReportGenerator(root=tmp_path).generate_report(
            event_filter=lambda name: name.startswith("mcp_update")
        )
        assert len(report.events) == 2
        assert all(e.event == "mcp_update_entity" for e in report.events)

    def test_since_until_window_filters(self, tmp_path: Path):
        base = self._seed_three(tmp_path)
        # Window covering only the middle event (minute 1).
        window_start = base + timedelta(seconds=30)
        window_end = base + timedelta(seconds=90)
        report = AuditReportGenerator(root=tmp_path).generate_report(
            since=window_start,
            until=window_end,
        )
        assert len(report.events) == 1
        assert report.events[0].payload == {"entity": "B"}
        assert report.window == {"from": window_start, "to": window_end}

    def test_payload_filter_predicate(self, tmp_path: Path):
        """Payload filters let the operator answer "every event
        touching contract X" without re-grep'ing the audit dir."""
        self._seed_three(tmp_path)
        report = AuditReportGenerator(root=tmp_path).generate_report(
            payload_filter=lambda p: p.get("entity") == "B",
        )
        assert len(report.events) == 1
        assert report.events[0].payload["entity"] == "B"

    def test_filters_compose_AND(self, tmp_path: Path):
        """Multiple filters AND-combine: entity B in the right
        window must yield the single event matching both."""
        base = self._seed_three(tmp_path)
        report = AuditReportGenerator(root=tmp_path).generate_report(
            event_filter="mcp_update_entity",
            since=base + timedelta(seconds=30),
            payload_filter=lambda p: p.get("entity") == "B",
        )
        assert len(report.events) == 1
        assert report.events[0].payload["entity"] == "B"

    def test_no_match_yields_empty_report(self, tmp_path: Path):
        self._seed_three(tmp_path)
        report = AuditReportGenerator(root=tmp_path).generate_report(
            event_filter="nonexistent_event"
        )
        assert report.events == []
        assert report.counts_by_event == {}


# ---------------------------------------------------------------------
# AuditReport.to_dict — JSON-friendly serialisation
# ---------------------------------------------------------------------


class TestAuditReportToDict:
    def test_to_dict_round_trips_every_event(self, tmp_path: Path):
        base = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        _seed_event(tmp_path, "mcp_update_entity", timestamp=base, payload={"k": "v"})
        report = AuditReportGenerator(root=tmp_path).generate_report()
        as_dict = report.to_dict()

        # Must be JSON-serialisable without ``default=str`` rescues.
        as_json = json.dumps(as_dict)
        assert "mcp_update_entity" in as_json

        assert as_dict["total_events"] == 1
        assert as_dict["counts_by_event"] == {"mcp_update_entity": 1}
        # Window with no filter is None on both sides.
        assert as_dict["window"] == {"from": None, "to": None}

    def test_to_dict_window_serialises_iso_format(self, tmp_path: Path):
        base = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        _seed_event(tmp_path, "x", timestamp=base)
        end = base + timedelta(hours=1)
        report = AuditReportGenerator(root=tmp_path).generate_report(since=base, until=end)
        as_dict = report.to_dict()
        assert as_dict["window"]["from"] == base.isoformat()
        assert as_dict["window"]["to"] == end.isoformat()


# ---------------------------------------------------------------------
# Smoke: integration with write_audit_event
# ---------------------------------------------------------------------


def test_writer_and_generator_round_trip(tmp_path: Path):
    """``write_audit_event`` writes a file the generator can read
    cleanly. The two surfaces must round-trip without manual fixups."""
    write_audit_event("smoke_event", payload={"k": "v"}, root=tmp_path)
    report = AuditReportGenerator(root=tmp_path).generate_report()
    assert len(report.events) == 1
    assert report.events[0].event == "smoke_event"
    assert report.events[0].payload == {"k": "v"}
