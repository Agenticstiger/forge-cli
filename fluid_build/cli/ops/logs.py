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

"""``fluid logs <product-id> --component <name>`` — fetch logs by component.

Components:
- ``build``   — runner stdout/stderr (default)
- ``infra``   — managed-mode infra logs (Helm / Compose)
- ``server``  — Airbyte server / Kafka Connect REST logs
- ``worker``  — Airbyte / KC worker pod logs
- ``dlq``     — DLQ NDJSON contents

The function reads from the configured log roots; for components that
require a live cluster (``infra``/``server``/``worker``) it reads cached
log snapshots and falls through to a "no logs yet" notice when none are
present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator, List, Optional


class LogComponent(str, Enum):
    BUILD = "build"
    INFRA = "infra"
    SERVER = "server"
    WORKER = "worker"
    DLQ = "dlq"


@dataclass
class LogLine:
    timestamp: Optional[str]
    level: Optional[str]
    message: str
    component: str


def fetch_logs(
    state_root: Path,
    product_id: str,
    *,
    component: LogComponent = LogComponent.BUILD,
    run_id: Optional[str] = None,
    grep: Optional[str] = None,
    follow: bool = False,
    limit: int = 1000,
) -> List[LogLine]:
    """Read logs for a product from the local state-root layout.

    Layout::

        .fluid/
          runs/<product>/<build>/runs/<run-id>.json     ← build records
          logs/<product>/<build>/<component>.log        ← per-component logs
          dlq/<run-id>/<stream>.ndjson                  ← DLQ content

    The function does NOT tail in real time when ``follow=True`` here; the
    CLI command layer wires that to the OS file watcher. ``follow`` simply
    annotates the result for the caller.
    """
    lines: List[LogLine] = []
    if component is LogComponent.DLQ:
        if run_id is None:
            return []
        dlq_dir = state_root / "dlq" / run_id
        if not dlq_dir.exists():
            return []
        for f in sorted(dlq_dir.glob("*.ndjson")):
            for line in _tail(f, limit=limit):
                if grep and grep not in line:
                    continue
                lines.append(LogLine(timestamp=None, level=None, message=line, component="dlq"))
        return lines

    # Build / infra / server / worker logs are flat .log files per component.
    base = state_root / "logs" / product_id
    if not base.exists():
        return []
    for build_dir in sorted(base.iterdir()):
        log_file = build_dir / f"{component.value}.log"
        if not log_file.exists():
            continue
        for line in _tail(log_file, limit=limit):
            if grep and grep not in line:
                continue
            lines.append(_parse_log_line(line, component=component.value))
    return lines


# ── Helpers ────────────────────────────────────────────────────────────


def _tail(path: Path, *, limit: int) -> Iterator[str]:
    """Yield the last ``limit`` lines of ``path``."""
    with path.open(encoding="utf-8") as f:
        lines = f.readlines()
    yield from (l.rstrip("\n") for l in lines[-limit:])


def _parse_log_line(line: str, *, component: str) -> LogLine:
    """Parse a JSON-structured or plain text log line into a ``LogLine``.

    The runner emits structured JSON via ``structured_logging``; plain text
    is also tolerated.
    """
    import json

    try:
        data = json.loads(line)
        if isinstance(data, dict):
            return LogLine(
                timestamp=data.get("timestamp"),
                level=data.get("level"),
                message=data.get("message") or json.dumps(data),
                component=component,
            )
    except Exception:
        pass
    return LogLine(timestamp=None, level=None, message=line, component=component)
