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

"""Receipt builder — per-run manifest of everything init/forge wrote.

After a successful ``fluid init`` or ``fluid forge`` run, the CLI writes
a JSON receipt listing every file it created/updated/skipped, with
sha256s and the inputs that drove the run.  The receipt lives under
``<workspace>/.fluid/init-receipt.json`` (init) or
``<product>/.fluid/forge-receipt.json`` (forge) and is *gitignored* —
it's a machine-local record, not team state.

The receipt enables three things:

1. **Dry-run diffing.**  ``fluid init --dry-run`` produces the same JSON
   with ``action: "would-create"`` and no filesystem writes.  Re-running
   for real produces an identical shape with ``action: "create"``, so the
   two can be diffed to prove parity.

2. **Rollback.**  A future ``fluid uninstall <run_id>`` reads the receipt
   and removes only paths whose sha256 still matches what was recorded —
   refusing to touch anything the user has since hand-edited.

3. **Auditability.**  CI can attach the receipt as a build artifact.
   ``git log`` on a teammate's clone has no idea what your machine did;
   the receipt does.

Design notes
------------

* Receipts are last-only.  Each run overwrites the previous receipt.
  There is no append-only history (per the UX decision in the plan —
  can be added later if users ask).
* Receipts are gitignored.  See the git-policy matrix in the plan.
* The receipt class does not call ``ArtifactWriter`` to write itself —
  it builds a ``ReceiptDocument`` that the caller hands to the writer.
  This keeps receipts testable in isolation without filesystem I/O.
"""

from __future__ import annotations

__all__ = [
    "ReceiptBuilder",
    "ReceiptDocument",
    "ReceiptEntry",
    "generate_run_id",
]

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional

from fluid_build.cli.artifact_writer import ArtifactWriter, ArtifactWriteRecord

ReceiptFlow = Literal["ai", "template", "blank", "demo", "copilot", "guided", "forge"]


@dataclass
class ReceiptEntry:
    """One row in the receipt's ``artifacts`` list."""

    path: str  # relative to the receipt's scope root when possible
    action: str
    sha256: Optional[str] = None
    size: int = 0
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"path": self.path, "action": self.action}
        if self.sha256 is not None:
            result["sha256"] = self.sha256
        if self.size:
            result["size"] = self.size
        if self.reason:
            result["reason"] = self.reason
        return result


@dataclass
class ReceiptDocument:
    """Full receipt payload — what the caller hands to the writer."""

    run_id: str
    flow: str
    dry_run: bool
    artifacts: List[ReceiptEntry] = field(default_factory=list)
    inputs: Dict[str, Any] = field(default_factory=dict)
    skipped: List[ReceiptEntry] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        """Return the payload to wrap inside the envelope.

        The envelope (``schema_version``/``kind``/``generated_at``/
        ``generated_by``) is added by
        :func:`fluid_build.cli.artifact_envelope.dump_json_with_envelope`
        when the receipt is written.
        """
        return {
            "run_id": self.run_id,
            "flow": self.flow,
            "dry_run": self.dry_run,
            "artifacts": [e.to_dict() for e in self.artifacts],
            "skipped": [e.to_dict() for e in self.skipped],
            "inputs": dict(self.inputs),
        }


def generate_run_id() -> str:
    """Return a compact, time-sortable, machine-unique run identifier.

    Format: ``<hex-timestamp>-<hex-random>`` — e.g.
    ``018f7c1d3a42-9a2f1d03``.  Sortable lexicographically because the
    timestamp comes first.  Uses 4 bytes (8 hex chars) of randomness so
    rapid-fire generation (e.g. tests in a tight loop) does not produce
    collisions even when the ms-resolution timestamp is identical.

    No dependencies on ``uuid`` / ``ulid`` so the module stays
    stdlib-only.
    """
    # Milliseconds since epoch, hex-encoded (fits in 11 chars until ~year 2527)
    ts_hex = f"{int(time.time() * 1000):011x}"
    rand_hex = secrets.token_hex(4)  # 8 hex chars ~ 4B values
    return f"{ts_hex}-{rand_hex}"


class ReceiptBuilder:
    """Thin facade that turns ``ArtifactWriter.records`` into a receipt.

    Typical usage inside a command handler::

        writer = ArtifactWriter(command="fluid init --blank", dry_run=args.dry_run)
        receipt = ReceiptBuilder(flow="blank", dry_run=args.dry_run)

        # ... hand the writer to every scaffolding helper ...

        receipt.record_writes(writer, scope_root=workspace_root)
        receipt.set_inputs(template=args.template, domain=args.domain)
        doc = receipt.build_document()

        # The caller then serializes doc via dump_json_with_envelope
        # and hands the bytes to the writer for the final write.

    The builder is intentionally passive — it does not trigger any file
    I/O.  That decouples receipt construction from the writer's dry-run
    logic and makes unit tests fast.
    """

    def __init__(
        self,
        *,
        flow: str,
        dry_run: bool = False,
        run_id: Optional[str] = None,
    ):
        self.run_id = run_id or generate_run_id()
        self.flow = flow
        self.dry_run = dry_run
        self._entries: List[ReceiptEntry] = []
        self._skipped: List[ReceiptEntry] = []
        self._inputs: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_writes(
        self,
        writer: ArtifactWriter,
        *,
        scope_root: Optional[Path] = None,
    ) -> None:
        """Copy every record from *writer* into this receipt.

        When ``scope_root`` is given, paths are stored relative to it so
        the receipt is portable across machines.  Paths outside the scope
        (e.g. user-level files) are left absolute.
        """
        for rec in writer.records:
            entry = _record_to_entry(rec, scope_root=scope_root)
            if rec.action == "skip":
                self._skipped.append(entry)
            else:
                self._entries.append(entry)

    def record_entry(
        self,
        path: Path,
        *,
        action: str,
        sha256: Optional[str] = None,
        size: int = 0,
        reason: Optional[str] = None,
        scope_root: Optional[Path] = None,
    ) -> None:
        """Manually add a single receipt entry.

        Useful for receipts produced by helpers that don't thread the
        writer all the way down (e.g. legacy code paths being migrated
        gradually).  New code should prefer :meth:`record_writes`.
        """
        entry = ReceiptEntry(
            path=_relative_path(path, scope_root),
            action=action,
            sha256=sha256,
            size=size,
            reason=reason,
        )
        if action == "skip":
            self._skipped.append(entry)
        else:
            self._entries.append(entry)

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def set_inputs(self, **kwargs: Any) -> None:
        """Record the inputs that drove this run (template, domain, etc.).

        Keys with ``None`` values are skipped so the receipt stays tidy.
        Values are stored as-is — callers are responsible for not passing
        secrets.
        """
        for key, value in kwargs.items():
            if value is None:
                continue
            self._inputs[key] = value

    def merge_inputs(self, data: Mapping[str, Any]) -> None:
        """Merge a dict of inputs into the builder's input set."""
        self.set_inputs(**dict(data))

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build_document(self) -> ReceiptDocument:
        """Return the :class:`ReceiptDocument` ready for serialisation."""
        return ReceiptDocument(
            run_id=self.run_id,
            flow=self.flow,
            dry_run=self.dry_run,
            artifacts=list(self._entries),
            skipped=list(self._skipped),
            inputs=dict(self._inputs),
        )

    # ------------------------------------------------------------------
    # Debug helpers
    # ------------------------------------------------------------------

    @property
    def entries(self) -> List[ReceiptEntry]:
        """The non-skipped entries recorded so far (read-only copy)."""
        return list(self._entries)

    @property
    def skipped(self) -> List[ReceiptEntry]:
        """The skipped entries recorded so far (read-only copy)."""
        return list(self._skipped)

    @property
    def inputs(self) -> Dict[str, Any]:
        """The inputs recorded so far (read-only copy)."""
        return dict(self._inputs)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _record_to_entry(
    rec: ArtifactWriteRecord,
    *,
    scope_root: Optional[Path],
) -> ReceiptEntry:
    return ReceiptEntry(
        path=_relative_path(rec.path, scope_root),
        action=rec.action,
        sha256=rec.sha256,
        size=rec.size,
        reason=rec.reason,
    )


def _relative_path(path: Path, scope_root: Optional[Path]) -> str:
    """Return *path* relative to *scope_root* when possible.

    Falls back to the absolute path string when *path* is outside the
    scope (e.g. a user-level file written to ``~/.fluid/``).
    """
    if scope_root is None:
        return str(path)
    try:
        return str(Path(path).resolve().relative_to(Path(scope_root).resolve()))
    except ValueError:
        return str(path)
