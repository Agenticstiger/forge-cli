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

"""On-disk discovery cache (slice UX-J).

After slice UX-G capped the discovery depth and memoized sample-file
schema inference, the remaining wall-time in ``discover_local_context``
is the BFS directory walk + per-file classification (~200-800ms on a
typical repo).  This module adds a thin disk cache
(``.fluid/discovery-cache.json``) keyed on the hash of the scanned
file set so that subsequent ``fluid forge`` runs in the same workspace
skip the walk + classification entirely when nothing changed.

The cache only saves the *classification* and *schema inference* cost.
The BFS walk to enumerate candidate files still runs (to compute the
cache key), but ``stat()`` calls are ~100x cheaper than opening +
parsing files for schema inference, so the net saving is 50-90% of
the previous discovery wall-time.

Cache misses are silent and fall through to the full scan.  Write
failures are best-effort (logged but never thrown).  The env kill-switch
``FLUID_DISCOVERY_CACHE=0`` disables caching entirely.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("fluid.cli.forge_copilot.discovery_cache")


def discovery_cache_enabled() -> bool:
    """Return True unless ``FLUID_DISCOVERY_CACHE=0``."""
    value = os.environ.get("FLUID_DISCOVERY_CACHE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def compute_file_tree_hash(paths: List[Path]) -> str:
    """Compute a stable sha256 hash of ``(path, mtime_ns, size)`` tuples.

    The hash changes whenever a file in the scanned set is added,
    removed, renamed, or modified (content or metadata).  The tuple
    is sorted by path so the hash is deterministic regardless of the
    order ``_iter_candidate_files`` yields entries.
    """
    entries: List[Tuple[str, int, int]] = []
    for p in paths:
        try:
            st = p.stat()
            entries.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            # File vanished between iteration and stat — skip it.
            continue
    entries.sort()
    blob = json.dumps(entries, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_discovery_cache(
    workspace_root: Path,
    file_tree_hash: str,
) -> Optional[Dict[str, Any]]:
    """Load a cached discovery report if the hash matches.

    Returns the cached report dict (suitable for
    ``DiscoveryReport(**cached)``) or ``None`` on any mismatch, parse
    error, or missing file.
    """
    from fluid_build.cli.artifact_paths import workspace_discovery_cache_path

    cache_path = workspace_discovery_cache_path(workspace_root)
    if not cache_path.is_file():
        return None
    try:
        doc = json.loads(cache_path.read_text(encoding="utf-8"))
        if doc.get("file_tree_hash") != file_tree_hash:
            LOG.debug(
                "Discovery cache hash mismatch (expected %s, got %s)",
                file_tree_hash,
                doc.get("file_tree_hash"),
            )
            return None
        return doc.get("report")
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Failed to read discovery cache: %s", exc)
        return None


def write_discovery_cache(
    workspace_root: Path,
    report: Any,
    file_tree_hash: str,
) -> None:
    """Persist the discovery report to ``.fluid/discovery-cache.json``.

    Best-effort: errors are logged but never raised.
    """
    from fluid_build.cli.artifact_envelope import build_envelope
    from fluid_build.cli.artifact_paths import workspace_discovery_cache_path

    cache_path = workspace_discovery_cache_path(workspace_root)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            from fluid_build import __version__ as tool_version
        except Exception:  # noqa: BLE001
            tool_version = ""

        envelope = build_envelope(
            kind="DiscoveryCache",
            command="fluid forge",
            tool_version=str(tool_version),
        )

        # Serialize the DiscoveryReport to a plain dict.  Use asdict
        # if it's a dataclass; fall back to to_prompt_payload if not.
        try:
            report_dict = asdict(report)
        except TypeError:
            report_dict = report.to_prompt_payload() if hasattr(report, "to_prompt_payload") else {}

        doc = {
            "envelope": envelope,
            "file_tree_hash": file_tree_hash,
            "report": report_dict,
        }
        cache_path.write_text(
            json.dumps(doc, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        LOG.debug("Wrote discovery cache to %s", cache_path)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Failed to write discovery cache: %s", exc)
