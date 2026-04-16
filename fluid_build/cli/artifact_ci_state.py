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

"""CI state file — the committed record of what produced committed CI files.

``ci-state.json`` is the **one exception** to "everything under
``.fluid/`` is engineer-personal state".  It lives under
``<product>/.fluid/ci-state.json`` (same hidden directory as the
per-engineer receipts and copilot memory) but it is **committed to
git** because:

1. The CI files it describes (``.github/workflows/*.yml``,
   ``.gitlab-ci.yml``, ``Jenkinsfile``, etc.) are themselves committed,
   and the state that describes "what inputs produced these files"
   must travel with them for cross-machine drift detection to work.

2. When Alice runs ``fluid forge --ci github_actions`` on her laptop
   and commits both the workflow file and ``ci-state.json``, Bob on
   another clone running ``fluid forge`` can read the recorded sha256
   and decide whether Alice's file is pristine (safe to regenerate
   silently) or hand-edited (prompt before clobbering).

3. Gitignoring ``ci-state.json`` would silently break drift detection
   across machines — the sha256 from Alice's run would never reach
   Bob, so Bob's forge run would skip every CI file out of caution.

The file's consumers in slices 7 and 8:

* **Slice 7** (this slice): ``_scaffold_ci_pipeline`` writes
  ``ci-state.json`` immediately after ``write_pipeline_files`` succeeds,
  recording the provider, complexity, environments, options, and the
  sha256 of every generated file.

* **Slice 8** (next): ``_resolve_ci_choice`` reads the recorded provider
  ahead of personal memory, and the collision check in
  ``_scaffold_ci_pipeline`` uses the recorded sha256 to distinguish
  pristine files (silent overwrite) from hand-edited ones (prompt).
"""

from __future__ import annotations

__all__ = [
    "CIStateDocument",
    "CIStateDriftReport",
    "build_ci_state_payload",
    "classify_ci_drift",
    "load_ci_state",
    "write_ci_state",
]

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.artifact_envelope import dump_json_with_envelope
from fluid_build.cli.artifact_paths import product_ci_state_path

LOG = logging.getLogger("fluid.cli.artifact_ci_state")


@dataclass
class CIStateDocument:
    """In-memory representation of the ci-state payload."""

    provider: str
    complexity: str
    environments: List[str] = field(default_factory=list)
    options: Dict[str, Any] = field(default_factory=dict)
    files: List[Dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "complexity": self.complexity,
            "environments": list(self.environments),
            "options": dict(self.options),
            "files": list(self.files),
        }

    def recorded_sha(self, rel_path: str) -> Optional[str]:
        """Return the sha256 recorded for *rel_path*, or ``None`` if missing."""
        for entry in self.files:
            if entry.get("path") == rel_path:
                sha = entry.get("sha256")
                return sha if isinstance(sha, str) else None
        return None


@dataclass
class CIStateDriftReport:
    """Per-file classification of CI files against the recorded ci-state.

    Attributes:
        pristine:
            Files whose on-disk sha256 matches the recorded value.  Safe
            to silently overwrite on regeneration — the user has not
            touched them.
        drifted:
            Files that exist on disk with a DIFFERENT sha256 than
            recorded — the user has hand-edited them.  Callers must
            prompt before overwriting, or skip to preserve edits.
        missing_from_disk:
            Files recorded in ci-state that no longer exist on disk.
            Treated as pristine for regeneration purposes (nothing to
            preserve).
        missing_from_state:
            Files about to be written that don't appear in ci-state at
            all.  Typical for the first-ever generation or when a new
            file type is added.  Callers fall back to the pre-slice-8
            collision behavior (skip if the file already exists).
    """

    pristine: List[str] = field(default_factory=list)
    drifted: List[str] = field(default_factory=list)
    missing_from_disk: List[str] = field(default_factory=list)
    missing_from_state: List[str] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.drifted)


def build_ci_state_payload(
    *,
    provider: str,
    complexity: str,
    environments: Optional[List[str]] = None,
    options: Optional[Dict[str, Any]] = None,
    written_files: List[Path],
    product_root: Path,
    body_contents: Optional[Dict[str, str]] = None,
) -> CIStateDocument:
    """Build a :class:`CIStateDocument` from the files that were just written.

    *written_files* is the list returned by
    :func:`pipeline_generator.write_pipeline_files`.  Each path is
    re-anchored relative to *product_root* so the state file stays
    portable across clones.

    The recorded ``sha256`` is the hash of the **original (pre-header)
    body** when *body_contents* is supplied — not the hash of the
    on-disk bytes.  This is intentional: the DO-NOT-EDIT header carries
    a fresh timestamp on every regeneration, so two pristine runs of
    the same command produce different on-disk bytes.  Hashing the
    pre-header body gives us a stable identity the drift classifier
    can use: a file is "pristine" iff its body (header stripped) still
    matches the recorded sha.

    *body_contents* is a ``{filename: content}`` dict of the original
    generator output (the dict passed to
    :func:`pipeline_generator.write_pipeline_files`).  When not
    supplied, falls back to hashing the on-disk bytes (legacy behavior,
    intended for tests that don't care about drift detection).
    """
    files: List[Dict[str, Any]] = []
    root = Path(product_root).resolve()
    for path in written_files:
        abs_path = Path(path).resolve()
        try:
            rel = str(abs_path.relative_to(root))
        except ValueError:
            rel = str(abs_path)

        sha: Optional[str] = None
        if body_contents is not None and rel in body_contents:
            sha = hashlib.sha256(body_contents[rel].encode("utf-8")).hexdigest()
        else:
            try:
                sha = hashlib.sha256(abs_path.read_bytes()).hexdigest()
            except OSError:
                sha = None

        entry: Dict[str, Any] = {"path": rel}
        if sha is not None:
            entry["sha256"] = sha
        try:
            entry["size"] = abs_path.stat().st_size
        except OSError:
            pass
        files.append(entry)

    return CIStateDocument(
        provider=provider,
        complexity=complexity,
        environments=list(environments or []),
        options=dict(options or {}),
        files=files,
    )


def load_ci_state(product_root: Path) -> Optional[CIStateDocument]:
    """Load ``<product>/.fluid/ci-state.json`` and return the doc.

    Returns ``None`` if the file does not exist, is unparseable, or has
    a mismatched envelope kind.  Unknown top-level envelope fields
    (``schema_version``, ``generated_at``, ``generated_by``) are
    silently ignored.
    """
    path = product_ci_state_path(product_root)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("kind") not in (None, "CIState"):
        return None

    provider = raw.get("provider")
    complexity = raw.get("complexity")
    if not isinstance(provider, str) or not isinstance(complexity, str):
        return None

    environments = raw.get("environments") or []
    if not isinstance(environments, list):
        environments = []
    options = raw.get("options") or {}
    if not isinstance(options, dict):
        options = {}
    files = raw.get("files") or []
    if not isinstance(files, list):
        files = []

    return CIStateDocument(
        provider=provider,
        complexity=complexity,
        environments=[str(e) for e in environments if isinstance(e, str)],
        options=dict(options),
        files=[entry for entry in files if isinstance(entry, dict)],
    )


def classify_ci_drift(
    product_root: Path,
    generated_files: Dict[str, str],
    *,
    state: Optional[CIStateDocument] = None,
) -> CIStateDriftReport:
    """Classify each file in *generated_files* against the recorded ci-state.

    *generated_files* is the ``{filename: content}`` dict returned by
    ``PipelineTemplateGenerator.generate_pipeline`` — we don't care
    about the content here, only the filenames.

    Rules:

    * If there is no recorded state (*state* is ``None`` or missing):
      every file with an existing on-disk path goes into
      ``missing_from_state``; callers fall back to the legacy skip-on-
      collision behavior.
    * If a file appears in ci-state and is on disk with matching
      sha256: ``pristine``.
    * If a file appears in ci-state and is on disk with different
      sha256: ``drifted``.
    * If a file appears in ci-state but is no longer on disk:
      ``missing_from_disk``.
    * If a file is about to be written but isn't in ci-state at all:
      ``missing_from_state``.

    The classification does NOT include new files that are about to be
    written and don't already exist on disk — those are always safe to
    create and never show up in the drift report.
    """
    root = Path(product_root).resolve()
    report = CIStateDriftReport()

    if state is None:
        for filename in generated_files:
            if (root / filename).exists():
                report.missing_from_state.append(filename)
        return report

    for filename in generated_files:
        target = root / filename
        rel = str(Path(filename))
        recorded = state.recorded_sha(rel)

        if recorded is None:
            if target.exists():
                report.missing_from_state.append(filename)
            continue

        if not target.exists():
            report.missing_from_disk.append(filename)
            continue

        try:
            raw = target.read_text(encoding="utf-8")
        except OSError:
            # Can't read the file — treat as drifted to be safe.
            report.drifted.append(filename)
            continue

        # Recorded sha is computed against the original (pre-header)
        # body, so strip the provenance header before hashing the
        # on-disk content.
        body = _strip_header(raw, filename)
        disk_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()

        if disk_sha == recorded:
            report.pristine.append(filename)
        else:
            report.drifted.append(filename)

    return report


def _strip_header(text: str, filename: str) -> str:
    """Remove the DO-NOT-EDIT provenance header from a CI file's text.

    Matches the header shape produced by
    :func:`fluid_build.cli.pipeline_generator._render_header`: a
    contiguous block of comment lines at the top of the file starting
    with ``Generated by fluid-cli`` and ending with a ``Schema:`` line.

    Uses the same comment-prefix rule as the writer (``//`` for
    Jenkinsfile, ``#`` otherwise) so a file stripped here round-trips
    through the classifier cleanly.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return text

    comment_prefix = "//" if Path(filename).name in {"Jenkinsfile"} else "#"

    first = lines[0].lstrip()
    if not first.startswith(f"{comment_prefix} Generated by fluid-cli"):
        return text

    # Walk comment lines until we hit the schema line or a non-comment.
    stripped_count = 0
    for line in lines:
        stripped = line.lstrip()
        if not stripped.startswith(comment_prefix):
            break
        stripped_count += 1
        if "Schema:" in stripped:
            break

    return "".join(lines[stripped_count:])


def write_ci_state(
    doc: CIStateDocument,
    product_root: Path,
    *,
    command: str,
    tool_version: str,
    logger: Optional[logging.Logger] = None,
) -> Optional[Path]:
    """Serialise *doc* to ``<product>/.fluid/ci-state.json``.

    Returns the written path on success or ``None`` on filesystem error
    (write failures never propagate — CI state is best-effort, same
    discipline as the receipts).
    """
    log = logger or LOG
    try:
        target = product_ci_state_path(product_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = dump_json_with_envelope(
            doc.to_payload(),
            kind="CIState",
            command=command,
            tool_version=tool_version,
        )
        target.write_text(body, encoding="utf-8")
        log.debug("ci_state_written", extra={"path": str(target)})
        return target
    except OSError as exc:
        log.debug("ci_state_write_failed", extra={"error": str(exc)})
        return None
