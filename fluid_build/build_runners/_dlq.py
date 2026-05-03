# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Dead-Letter Queue writer.

Records failing PII classification, schema validation, quality gates, or
destination write are routed here. Each DLQ record carries: payload,
failure reason, hook chain trace, run id, timestamp.

When ``maxRecordsBeforeAbort`` is exceeded, the run transitions to
``failed`` and the lock is released — the runner consults
``DLQOverflowError`` to decide.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# Re-exported from the typed-error catalog so a single class name has a
# single class identity. Keeping the symbol here lets existing
# ``from fluid_build.build_runners._dlq import DLQOverflowError`` imports
# (e.g., in meltano/runner.py) keep working unchanged.
from fluid_build.cli._errors import DLQOverflowError  # noqa: E402,F401

from ._acquisition_common import utc_now_iso


@dataclass
class DLQConfig:
    enabled: bool = True
    sink_format: str = "ndjson"  # "ndjson" | "json" | "parquet"
    location: Optional[str] = None  # absolute or s3:// URI
    max_records_before_abort: int = 10_000
    alert_on: List[str] = None  # type: ignore[assignment]

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "DLQConfig":
        d = d or {}
        sink = d.get("sink") or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            sink_format=sink.get("format", "ndjson"),
            location=sink.get("location"),
            max_records_before_abort=int(d.get("maxRecordsBeforeAbort", 10_000)),
            alert_on=list(d.get("alertOn", [])),
        )


class DLQWriter:
    """Append-only JSONL writer at ``location/<run-id>/<stream>.ndjson``.

    Parquet sink is left for a later pass; NDJSON is universal and works
    on any object store via standard fs.

    File handles are cached per-stream and held open for the life of the
    writer so high-throughput batches don't pay an open/close syscall
    per record. Call :meth:`close` (or use ``with`` semantics by
    holding the writer in a try/finally) to flush + release the handles.
    """

    def __init__(self, config: DLQConfig, run_id: str, default_root: Path):
        self.config = config
        self.run_id = run_id
        loc = config.location
        if loc is None or loc.startswith(("s3://", "gs://", "azure://")):
            # Cloud sinks are deferred — fall back to local file under .fluid/dlq.
            base = default_root / "dlq"
        else:
            base = Path(loc)
        self.root = base / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self._handles: Dict[str, Any] = {}

    def _handle_for(self, stream: str):
        h = self._handles.get(stream)
        if h is None:
            path = self.root / f"{stream}.ndjson"
            h = path.open("a", encoding="utf-8")
            self._handles[stream] = h
        return h

    def append(
        self,
        stream: str,
        record: Dict[str, Any],
        reason: str,
        hook_trace: Optional[List[str]] = None,
    ) -> None:
        if not self.config.enabled:
            return
        envelope = {
            "run_id": self.run_id,
            "stream": stream,
            "timestamp": utc_now_iso(),
            "reason": reason,
            "hook_trace": hook_trace or [],
            "record": record,
        }
        h = self._handle_for(stream)
        h.write(json.dumps(envelope, sort_keys=True) + "\n")
        # Flush so external readers (verify probes, alert handlers) see
        # the new line immediately. The OS still buffers the actual
        # write to disk; flush() only pushes Python's stdio buffer.
        h.flush()
        self.count += 1
        if self.count > self.config.max_records_before_abort:
            self.close()
            raise DLQOverflowError.for_run(
                count=self.count,
                cap=self.config.max_records_before_abort,
                alerts=list(self.config.alert_on or []),
            )

    def close(self) -> None:
        """Flush + close all open per-stream file handles."""
        for h in self._handles.values():
            try:
                h.flush()
                h.close()
            except Exception:  # noqa: BLE001
                pass
        self._handles.clear()

    def __enter__(self) -> "DLQWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:  # noqa: D401
        # Best-effort flush on GC. Tests rely on file contents being
        # readable mid-test; close() makes that explicit, but keep the
        # safety net for forgotten handles.
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def total(self) -> int:
        return self.count


def process_batch_with_dlq(
    *,
    records: List[Dict[str, Any]],
    hook_chain: Any,  # HookChain — kept loose to avoid import cycle
    dlq_writer: Optional["DLQWriter"],
    alerter: Optional[Any] = None,  # Alerter
    stream: str,
    run_id: str,
    product_id: str = "",
    build_id: str = "",
    ctx: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Run the hook chain over ``records`` and route DLQ items to the writer + alerter.

    Returns the post-hook ``records`` list ready for destination write.

    This is the canonical glue between the per-batch hook chain and the
    DLQ + alerting subsystems. Runners that have row-level visibility
    (Meltano / Singer, dlt, Kafka Connect with custom transforms) call
    this once per batch. Bulk-SQL runners (DuckDB COPY, Airbyte
    SQL-target) need a different integration that runs hooks against a
    materialized DuckDB view; not in this helper.
    """
    from ._alerter import AlertEvent  # local to avoid import cycle

    result = hook_chain.run(records, ctx)
    if not result.dlq:
        return result.records
    if dlq_writer is None:
        return result.records

    by_reason: Dict[str, int] = {}
    for entry in result.dlq:
        rec = entry.get("record", entry)
        reason = entry.get("reason", "hook_chain_dropped")
        trace = entry.get("hook_trace") or []
        try:
            dlq_writer.append(stream, rec, reason, trace)
        except Exception as exc:  # noqa: BLE001
            # Re-raise overflow so the runner can transition to FAILED;
            # other writer errors are best-effort and logged.
            from . import _dlq

            if isinstance(exc, _dlq.DLQOverflowError):
                raise
        by_reason[reason] = by_reason.get(reason, 0) + 1

    if alerter is not None and (ctx or {}).get("alert_on"):
        alert_on = set(ctx.get("alert_on", []))  # type: ignore[union-attr]
        for reason, count in by_reason.items():
            if reason in alert_on:
                alerter.fire(
                    AlertEvent(
                        run_id=run_id,
                        product_id=product_id,
                        build_id=build_id,
                        category=reason,
                        severity="warn",
                        message=f"{count} record(s) routed to DLQ: {reason}",
                        count=count,
                        extras={"stream": stream},
                    )
                )
    return result.records
