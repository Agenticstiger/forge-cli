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

"""Single choke point for every file the CLI writes during init/forge.

Before this module, write sites were scattered across ``workspace_config.py``,
``forge_contract_factory.py``, ``forge_copilot_memory.py``,
``forge_copilot_personal_memory.py``, and ``pipeline_generator.py``.  Each one
had its own ``Path.write_text`` / ``yaml.dump`` / ``json.dumps`` call, its
own dry-run branch (when it had one at all), and its own idea of whether to
log or print on success.

The ``ArtifactWriter`` class in this module is the one place every write
goes through.  Benefits:

1. **Dry-run becomes universal** — set ``dry_run=True`` on construction and
   every call that flows through the writer automatically honours it.
2. **Receipts become free** — a future ``ReceiptBuilder`` (slice 2) plugs in
   here, not into every caller.
3. **Envelope becomes free** — slice 4's envelope helper is called from one
   place, not nine.
4. **Tests become simple** — mock one class, assert the method calls.

Slice 1 (this slice) introduces the skeleton and wraps the existing write
functions so they all delegate through the writer.  No behavior change for
existing flows — flat contracts still serialize exactly as before.
Subsequent slices add envelope emission, receipts, sha256 recording, and
the typed per-artifact methods described in the plan.
"""

from __future__ import annotations

__all__ = [
    "ArtifactWriter",
    "ArtifactWriteAction",
    "ArtifactWriteRecord",
]

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional, Union

LOG = logging.getLogger("fluid.cli.artifact_writer")

ArtifactWriteAction = Literal["create", "update", "would-create", "unchanged", "skip"]


@dataclass
class ArtifactWriteRecord:
    """One entry recorded for each call that flows through the writer.

    Used by ``ArtifactWriter.records`` (slice 1) and by the forthcoming
    ``ReceiptBuilder`` (slice 2) to build the init/forge receipt JSON.
    """

    path: Path
    action: ArtifactWriteAction
    size: int = 0
    sha256: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class ArtifactWriter:
    """Single I/O choke point for init/forge artifact writes.

    Parameters
    ----------
    command:
        Human-readable command string, e.g. ``"fluid init --blank"``.  Used by
        future envelope/receipt machinery as ``generated_by.command``.
    tool_version:
        The fluid-cli version string.  Slice 4 starts writing it into
        envelopes; slice 1 just records it so callers don't have to thread it.
    dry_run:
        When ``True``, no bytes are written to disk.  Every method still
        computes the target path and records a ``would-create`` action so
        callers (and a future receipt) see the same shape as a real run.
    logger:
        Optional logger; defaults to ``fluid.cli.artifact_writer``.

    Example
    -------
    ::

        writer = ArtifactWriter(command="fluid forge --blank", tool_version="0.42.1")
        writer.write_bytes(path, b"...", action_hint="create")
        writer.write_text(path, "...", action_hint="create")
        for record in writer.records:
            print(record.path, record.action, record.sha256)
    """

    command: str
    tool_version: str = ""
    dry_run: bool = False
    logger: logging.Logger = field(default_factory=lambda: LOG)
    records: List[ArtifactWriteRecord] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Low-level primitives — the two functions every callable eventually hits
    # ------------------------------------------------------------------

    def write_bytes(
        self,
        path: Union[str, Path],
        data: bytes,
        *,
        action_hint: ArtifactWriteAction = "create",
    ) -> Path:
        """Write raw bytes to *path*, recording the action.

        Behavior:
        - ``dry_run=True`` → record ``would-create``, write nothing, return the path.
        - Target exists and bytes are identical → record ``unchanged``, skip the write.
        - Target exists with different bytes → record ``update``.
        - Target does not exist → record ``create`` (or whatever ``action_hint`` says).

        The target directory is created if missing (``parents=True``).
        """
        p = Path(path)
        sha = _sha256_hex(data)

        if self.dry_run:
            self._record(
                p, action="would-create", size=len(data), sha256=sha
            )
            return p

        existing_sha: Optional[str] = None
        existed = p.exists()
        if existed:
            try:
                existing_sha = _sha256_hex(p.read_bytes())
            except OSError as exc:  # pragma: no cover - filesystem edge case
                self.logger.debug("Could not read existing file %s: %s", p, exc)

        if existed and existing_sha == sha:
            self._record(p, action="unchanged", size=len(data), sha256=sha)
            return p

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        action: ArtifactWriteAction = "update" if existed else action_hint
        self._record(p, action=action, size=len(data), sha256=sha)
        return p

    def write_text(
        self,
        path: Union[str, Path],
        text: str,
        *,
        encoding: str = "utf-8",
        action_hint: ArtifactWriteAction = "create",
    ) -> Path:
        """Write *text* to *path*, recording the action.

        Thin wrapper around :meth:`write_bytes` that encodes first.
        """
        return self.write_bytes(
            path, text.encode(encoding), action_hint=action_hint
        )

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    def skip(
        self,
        path: Union[str, Path],
        *,
        reason: str,
    ) -> None:
        """Record that *path* was deliberately not written.

        Used by callers that decide at the last moment not to write (e.g. a
        collision check, a dry-run branch, or a user decline).  Produces a
        ``skip`` entry in the records list with a human-readable reason.
        """
        p = Path(path)
        self._record(p, action="skip", reason=reason)

    def clear(self) -> None:
        """Reset the records list.  Useful between runs in long-lived tests."""
        self.records.clear()

    def _record(
        self,
        path: Path,
        *,
        action: ArtifactWriteAction,
        size: int = 0,
        sha256: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.records.append(
            ArtifactWriteRecord(
                path=path,
                action=action,
                size=size,
                sha256=sha256,
                reason=reason,
            )
        )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
