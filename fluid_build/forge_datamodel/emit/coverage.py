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

"""Canonical-model coverage summaries for forged data models.

After a forge run, users want a quick "am I aligned with the industry
canonical model?" readout. The validator already collects warnings for
missing entities and naming drift (see
:mod:`fluid_build.forge_datamodel.emit.validator`) — this module
aggregates the same comparison into a human-readable counts block:

    TMF SID canonical-model coverage (telecommunications)
      ✓ hubs        4/4 present
      ✓ links       3/3 present
      ⚠ satellites  3/4 present — missing: sat_resource_status

The block is optional output: only printed when the caller supplies an
:class:`IndustryPack` *and* that pack carries a seed skeleton. Otherwise
the function returns ``None`` and the CLI caller simply skips the print.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, List, Optional

from fluid_build.copilot.industry.pack import IndustryPack
from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DV2Model,
)
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft

# Keep the fuzzy threshold aligned with ``validator._DRIFT_SIMILARITY_THRESHOLD``.
# The validator treats a >= 0.72 ratio as "drift" (a single warning);
# the coverage summary counts those as *present* too, since the concept
# is covered even if the name drifted. The two views stay consistent
# that way.
_DRIFT_SIMILARITY_THRESHOLD = 0.72


@dataclass
class CoverageGroup:
    """Per-entity-kind coverage tally."""

    kind: str  # "hubs", "links", "satellites", "facts", "dimensions"
    expected: int
    present: int
    missing_names: List[str]

    @property
    def is_clean(self) -> bool:
        return self.missing_names == []


@dataclass
class CoverageSummary:
    """Aggregate per-kind coverage for one forge run."""

    industry: str
    canonical_label: str
    groups: List[CoverageGroup]

    @property
    def is_clean(self) -> bool:
        return all(group.is_clean for group in self.groups)

    def render(self) -> str:
        if not self.groups:
            return ""
        header = (
            f"{self.canonical_label or self.industry} canonical-model coverage ({self.industry})"
        )
        lines = [header]
        for group in self.groups:
            marker = "✓" if group.is_clean else "⚠"
            body = f"{group.present}/{group.expected} present"
            if group.missing_names:
                body += f" — missing: {', '.join(group.missing_names)}"
            lines.append(f"  {marker} {group.kind:<12}{body}")
        return "\n".join(lines)


def compute_canonical_coverage(
    logical: LogicalDraft, pack: IndustryPack
) -> Optional[CoverageSummary]:
    """Return a :class:`CoverageSummary` or ``None`` if the pack has no skeleton."""
    if logical.technique == "data_vault_2":
        skeleton = pack.seed_dv2_skeleton
        if skeleton is None or logical.dv2 is None:
            return None
        groups = _dv2_groups(emitted=logical.dv2, skeleton=skeleton)
    elif logical.technique == "dimensional":
        skeleton = pack.seed_dimensional_skeleton
        if skeleton is None or logical.dimensional is None:
            return None
        groups = _dim_groups(emitted=logical.dimensional, skeleton=skeleton)
    else:  # pragma: no cover — schema constrains technique to the two above
        return None

    return CoverageSummary(
        industry=pack.name,
        canonical_label=pack.canonical_model.label or pack.canonical_model.primary,
        groups=groups,
    )


def _dv2_groups(*, emitted: DV2Model, skeleton: DV2Model) -> List[CoverageGroup]:
    emitted_hubs = [hub.hub_table_name for hub in emitted.hubs]
    emitted_links = [link.link_table_name for link in emitted.links]
    emitted_satellites = [sat.satellite_table_name for sat in emitted.satellites]

    return [
        _group(
            kind="hubs",
            expected_names=[hub.hub_table_name for hub in skeleton.hubs],
            emitted=emitted_hubs,
        ),
        _group(
            kind="links",
            expected_names=[link.link_table_name for link in skeleton.links],
            emitted=emitted_links,
        ),
        _group(
            kind="satellites",
            expected_names=[sat.satellite_table_name for sat in skeleton.satellites],
            emitted=emitted_satellites,
        ),
    ]


def _dim_groups(*, emitted: DimensionalModel, skeleton: DimensionalModel) -> List[CoverageGroup]:
    emitted_facts = [fact.name for fact in emitted.facts]
    emitted_dims = [dim.name for dim in emitted.dimensions]

    return [
        _group(
            kind="facts",
            expected_names=[fact.name for fact in skeleton.facts],
            emitted=emitted_facts,
        ),
        _group(
            kind="dimensions",
            expected_names=[dim.name for dim in skeleton.dimensions],
            emitted=emitted_dims,
        ),
    ]


def _group(*, kind: str, expected_names: List[str], emitted: Iterable[str]) -> CoverageGroup:
    emitted_list = list(emitted)
    present = 0
    missing: List[str] = []
    for expected in expected_names:
        if _concept_covered(expected, emitted_list):
            present += 1
        else:
            missing.append(expected)
    return CoverageGroup(
        kind=kind,
        expected=len(expected_names),
        present=present,
        missing_names=missing,
    )


def _concept_covered(expected: str, emitted: Iterable[str]) -> bool:
    """Is the canonical concept *covered* — exact or drifted but close?"""
    if expected in emitted:
        return True
    best_ratio = _best_similarity(expected, emitted)
    return best_ratio >= _DRIFT_SIMILARITY_THRESHOLD


def _best_similarity(expected: str, emitted: Iterable[str]) -> float:
    best = 0.0
    for candidate in emitted:
        ratio = SequenceMatcher(None, expected.lower(), candidate.lower()).ratio()
        if ratio > best:
            best = ratio
    return best


__all__ = [
    "CoverageGroup",
    "CoverageSummary",
    "compute_canonical_coverage",
]
