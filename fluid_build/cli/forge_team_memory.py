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

"""Team-scoped memory for the forge copilot.

Team memory lives at ``.fluid/team-memory.yaml`` in the workspace root
and is **committed to git** — every engineer on the team sees the same
conventions, decisions, and vocabulary.  This is distinct from the
per-engineer project memory (gitignored) and personal memory (home dir).

Memory precedence (highest → lowest):
    1. Explicit CLI args / interview answers
    2. Discovery report (files on disk)
    3. **Team memory** (``.fluid/team-memory.yaml``)
    4. Project memory (``.fluid/copilot-memory.json``)
    5. Personal memory (``~/.fluid/personal-memory.json``)
    6. Built-in defaults
"""

from __future__ import annotations

__all__ = [
    "TEAM_MEMORY_FILENAME",
    "TeamMemory",
    "load_team_memory",
    "scaffold_team_memory",
]

import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

LOG = logging.getLogger("fluid.cli.forge.team_memory")

TEAM_MEMORY_FILENAME = "team-memory.yaml"

# Maximum items to keep from decisions list to avoid prompt bloat.
_MAX_DECISIONS = 10

# Bounds on what one team-memory file can put into the LLM prompt. The file
# is shared through git, so its content comes from every committer, and YAML
# anchors let a few hundred bytes stand for megabytes once a nested value is
# turned into text. Only scalar values are kept, and every map, list and
# string is capped.
_MAX_FILE_BYTES = 64 * 1024
_MAX_ENTRIES = 50  # per naming / defaults map and per vocabulary list
_MAX_STRING_CHARS = 500  # per key, value, vocabulary term or decision field

# Template scaffolded on `fluid init` or when the user creates a new workspace.
TEAM_MEMORY_TEMPLATE = """\
# Team Memory — shared conventions for the AI copilot.
#
# This file is committed to git so every engineer on the team shares
# the same defaults, naming patterns, and architectural decisions.
#
# The AI copilot uses these as authoritative guidance when generating
# FLUID contracts.  Explicit CLI flags and interview answers always
# take precedence over team memory.
#
# Docs: https://agenticstiger.github.io/forge_docs/advanced/forge-copilot-memory.html

conventions:
  # Naming patterns applied to generated contracts.
  naming:
    # product_prefix: "acme"           # All product IDs start with this
    # layer_convention: medallion       # bronze / silver / gold
    column_style: snake_case

  # Default values when the engineer doesn't specify.
  defaults:
    provider: local
    # build_engine: dbt
    # domain: analytics
    # owner_team: data-team

# Architectural decisions the team has made.
# The AI uses these to avoid re-debating settled questions.
decisions: []
  # - date: "2026-01-15"
  #   decision: "Use BigQuery for all new analytics products"
  #   rationale: "Team has GCP expertise, data lake is on GCS"

# Domain-specific vocabulary the AI should prefer in contracts.
vocabulary:
  entities: []
    # - customer_id
    # - order_id
  measures: []
    # - total_revenue
    # - order_count
  dimensions: []
    # - order_date
    # - region
"""


@dataclass
class TeamMemory:
    """Validated team memory loaded from ``.fluid/team-memory.yaml``."""

    # Conventions
    naming: Dict[str, str] = field(default_factory=dict)
    defaults: Dict[str, str] = field(default_factory=dict)

    # Decisions (bounded)
    decisions: List[Dict[str, str]] = field(default_factory=list)

    # Vocabulary
    vocabulary_entities: List[str] = field(default_factory=list)
    vocabulary_measures: List[str] = field(default_factory=list)
    vocabulary_dimensions: List[str] = field(default_factory=list)

    # Source path for logging
    source_path: Optional[str] = None

    def to_prompt_payload(self) -> Dict[str, Any]:
        """Serialize to a dict safe for injection into the LLM prompt."""
        payload: Dict[str, Any] = {}

        conventions: Dict[str, Any] = {}
        if self.naming:
            conventions["naming"] = self.naming
        if self.defaults:
            conventions["defaults"] = self.defaults
        if conventions:
            payload["conventions"] = conventions

        if self.decisions:
            payload["decisions"] = self.decisions[:_MAX_DECISIONS]

        vocabulary: Dict[str, Any] = {}
        if self.vocabulary_entities:
            vocabulary["entities"] = self.vocabulary_entities
        if self.vocabulary_measures:
            vocabulary["measures"] = self.vocabulary_measures
        if self.vocabulary_dimensions:
            vocabulary["dimensions"] = self.vocabulary_dimensions
        if vocabulary:
            payload["vocabulary"] = vocabulary

        return payload

    def summary_line(self) -> str:
        """One-line summary for console output."""
        parts = []
        n_conventions = len(self.naming) + len(self.defaults)
        if n_conventions:
            parts.append(f"{n_conventions} conventions")
        if self.decisions:
            parts.append(f"{len(self.decisions)} decisions")
        n_vocab = (
            len(self.vocabulary_entities)
            + len(self.vocabulary_measures)
            + len(self.vocabulary_dimensions)
        )
        if n_vocab:
            parts.append(f"{n_vocab} vocabulary terms")
        return ", ".join(parts) if parts else "empty"


_SCALAR_TYPES = (str, bool, int, float, datetime.date)


def _scalar_text(value: Any) -> Optional[str]:
    """Return a YAML scalar as stripped, length-capped text.

    ``None`` for null, empty text, and anything that is not a scalar. Lists
    and maps are refused rather than stringified: ``str()`` of a nested value
    expands every YAML alias inside it, which is how a 417-byte file became a
    12 MB prompt.
    """
    if not isinstance(value, _SCALAR_TYPES):
        return None
    text = str(value).strip()
    return text[:_MAX_STRING_CHARS] if text else None


def _is_nested(value: Any) -> bool:
    """A set value that is not a scalar (a list or a map): refused, and reported."""
    return value is not None and not isinstance(value, _SCALAR_TYPES)


def _clean_string_list(raw: Any, *, label: str, notes: List[str]) -> List[str]:
    """Normalize a YAML value to at most ``_MAX_ENTRIES`` non-empty strings."""
    if not isinstance(raw, list):
        return []
    nested = sum(1 for item in raw if _is_nested(item))
    if nested:
        notes.append(f"{label}: dropped {nested} nested item(s)")
    items = [text for text in (_scalar_text(item) for item in raw) if text]
    if len(items) > _MAX_ENTRIES:
        notes.append(f"{label}: kept the first {_MAX_ENTRIES} of {len(items)}")
    return items[:_MAX_ENTRIES]


def _clean_scalar_map(raw: Any, *, label: str, notes: List[str]) -> Dict[str, str]:
    """Normalize a YAML mapping to at most ``_MAX_ENTRIES`` scalar entries.

    A null or empty value (``domain:`` left blank) is an unset entry and is
    skipped quietly; it used to come through as the string ``"None"``.
    """
    if not isinstance(raw, dict):
        return {}
    nested = sum(1 for key, value in raw.items() if _is_nested(key) or _is_nested(value))
    if nested:
        notes.append(f"{label}: dropped {nested} nested value(s)")
    out: Dict[str, str] = {}
    for raw_key, raw_value in raw.items():
        key, value = _scalar_text(raw_key), _scalar_text(raw_value)
        if key is None or value is None:
            continue
        if len(out) == _MAX_ENTRIES:
            notes.append(f"{label}: kept the first {_MAX_ENTRIES} entries")
            break
        out[key] = value
    return out


def _read_bounded(path: Path) -> Optional[str]:
    """Read *path* as UTF-8, or ``None`` when it is over ``_MAX_FILE_BYTES``."""
    with path.open("rb") as handle:
        data = handle.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        return None
    return data.decode("utf-8")


def load_team_memory(workspace_root: Path) -> Optional[TeamMemory]:
    """Load team memory from ``.fluid/team-memory.yaml``.

    Returns ``None`` if the file does not exist.  Logs a warning and
    returns ``None`` on parse/validation errors (fail-open), and on a file
    larger than ``_MAX_FILE_BYTES``.  Only scalar values are kept, and maps,
    lists and strings are capped (see ``_MAX_ENTRIES`` and
    ``_MAX_STRING_CHARS``), because the result is sent to the LLM.
    """
    path = workspace_root / ".fluid" / TEAM_MEMORY_FILENAME
    if not path.exists():
        return None

    try:
        text = _read_bounded(path)
        if text is None:
            LOG.warning(
                "Team memory at %s is larger than %d bytes (skipping)", path, _MAX_FILE_BYTES
            )
            return None
        raw = yaml.safe_load(text)
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        LOG.warning("Could not parse team memory at %s: %s", path, exc)
        return None

    if not isinstance(raw, dict):
        LOG.warning("Team memory at %s is not a YAML mapping (skipping)", path)
        return None

    conventions = raw.get("conventions") or {}
    if not isinstance(conventions, dict):
        conventions = {}

    naming = conventions.get("naming") or {}
    if not isinstance(naming, dict):
        naming = {}

    defaults = conventions.get("defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}

    notes: List[str] = []

    raw_decisions = raw.get("decisions") or []
    if not isinstance(raw_decisions, list):
        raw_decisions = []
    decisions = []
    for entry in raw_decisions[:_MAX_DECISIONS]:
        if not isinstance(entry, dict):
            continue
        decision = _scalar_text(entry.get("decision"))
        if decision is None:
            continue
        decisions.append(
            {
                "date": _scalar_text(entry.get("date")) or "",
                "decision": decision,
                "rationale": _scalar_text(entry.get("rationale")) or "",
            }
        )

    vocabulary = raw.get("vocabulary") or {}
    if not isinstance(vocabulary, dict):
        vocabulary = {}

    tm = TeamMemory(
        naming=_clean_scalar_map(naming, label="conventions.naming", notes=notes),
        defaults=_clean_scalar_map(defaults, label="conventions.defaults", notes=notes),
        decisions=decisions,
        vocabulary_entities=_clean_string_list(
            vocabulary.get("entities"), label="vocabulary.entities", notes=notes
        ),
        vocabulary_measures=_clean_string_list(
            vocabulary.get("measures"), label="vocabulary.measures", notes=notes
        ),
        vocabulary_dimensions=_clean_string_list(
            vocabulary.get("dimensions"), label="vocabulary.dimensions", notes=notes
        ),
        source_path=str(path),
    )
    if notes:
        LOG.warning("Team memory at %s was trimmed: %s", path, "; ".join(notes))
    LOG.info("Loaded team memory from %s (%s)", path, tm.summary_line())
    return tm


def scaffold_team_memory(workspace_root: Path) -> Path:
    """Create a starter ``.fluid/team-memory.yaml`` from the built-in template.

    Returns the path to the created file.  Skips silently if the file
    already exists.
    """
    dest = workspace_root / ".fluid" / TEAM_MEMORY_FILENAME
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(TEAM_MEMORY_TEMPLATE, encoding="utf-8")
    LOG.info("Scaffolded team memory at %s", dest)
    return dest
