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

"""Contract-vs-contract semantic diff (the version-history side of ``fluid diff``).

Public entry point: :func:`compare_contracts(baseline, new) -> ChangelogReport`.
The output is the structured envelope from ``changelog_types``; CLI/render
formatting lives in ``cli/diff.py`` (text / json / markdown emitters).

The classifier is fluid-schema-native — it understands acquisition engines,
``agentPolicy``, ``sovereignty``, and the SDP/ADP/CDP × Bronze/Silver/Gold
cross-check directly, rather than treating contracts as generic schema docs.
"""

from __future__ import annotations

from typing import Any, Mapping

from .changelog_rules import (
    diff_agent_policy,
    diff_columns,
    diff_consumes,
    diff_metadata,
    diff_quality_severity,
    diff_sovereignty,
    iter_expose_pairs,
)
from .changelog_types import Change, ChangelogReport


def compare_contracts(baseline: Mapping[str, Any], new: Mapping[str, Any]) -> ChangelogReport:
    """Return a structured changelog comparing two parsed contracts.

    ``baseline`` is treated as "old"; ``new`` as the candidate. Idempotent —
    passing the same dict twice produces an empty report.
    """
    report = ChangelogReport()

    if not isinstance(baseline, Mapping) or not isinstance(new, Mapping):
        raise TypeError(
            "compare_contracts: both arguments must be Mapping; "
            f"got {type(baseline).__name__} / {type(new).__name__}"
        )

    # Top-level metadata churn — info-level signals.
    report.extend(diff_metadata(baseline, new))

    # Consumes / upstream lineage.
    report.extend(diff_consumes(baseline, new))

    # Agent policy + sovereignty.
    report.extend(diff_agent_policy(baseline, new))
    report.extend(diff_sovereignty(baseline, new))

    # Per-expose schema and quality.
    for expose_id, idx, base_expose, new_expose in iter_expose_pairs(baseline, new):
        if base_expose is None and new_expose is not None:
            report.add(
                Change(
                    path=f"exposes[{idx}]",
                    kind="expose_added",
                    severity="non_breaking",
                    description=f"expose '{expose_id}' added",
                    before=None,
                    after={"id": expose_id},
                )
            )
            continue
        if new_expose is None and base_expose is not None:
            report.add(
                Change(
                    path=f"exposes[{idx}]",
                    kind="expose_removed",
                    severity="breaking",
                    description=(
                        f"expose '{expose_id}' removed " f"(downstream consumers will fail)"
                    ),
                    before={"id": expose_id},
                    after=None,
                )
            )
            continue
        # Both present — diff inside.
        assert base_expose is not None and new_expose is not None
        report.extend(diff_columns(base_expose, new_expose, expose_id, idx))
        report.extend(diff_quality_severity(base_expose, new_expose, expose_id, idx))

    return report


def render_text(report: ChangelogReport) -> str:
    """Plain-text render: grouped by severity with one-line entries.

    Used by ``fluid diff --baseline ...`` default output. Kept simple so the
    output is grep-friendly in CI logs.
    """
    lines: list[str] = []

    def _section(title: str, changes: list[Change]) -> None:
        if not changes:
            return
        lines.append(title)
        lines.append("-" * len(title))
        for c in changes:
            lines.append(f"  {c.kind:32s} {c.path}: {c.description}")
        lines.append("")

    _section(f"BREAKING ({len(report.breaking)})", report.breaking)
    _section(f"NON-BREAKING ({len(report.non_breaking)})", report.non_breaking)
    _section(f"INFO ({len(report.info)})", report.info)

    if not lines:
        lines.append("No changes detected.")

    lines.append(
        f"Summary: {len(report.breaking)} breaking, "
        f"{len(report.non_breaking)} non-breaking, {len(report.info)} info"
    )
    return "\n".join(lines)


def render_markdown(report: ChangelogReport) -> str:
    """Markdown render — for PR-comment bots that paste this into a discussion."""
    parts: list[str] = ["# Contract changelog", ""]

    def _section(title: str, changes: list[Change], emoji: str) -> None:
        if not changes:
            return
        parts.append(f"## {emoji} {title} ({len(changes)})")
        parts.append("")
        for c in changes:
            parts.append(f"- **{c.kind}** `{c.path}` — {c.description}")
        parts.append("")

    _section("Breaking", report.breaking, "")
    _section("Non-breaking", report.non_breaking, "")
    _section("Info", report.info, "")

    if report.total == 0:
        parts.append("_No changes detected._")
        parts.append("")

    parts.append(
        f"**Summary:** {len(report.breaking)} breaking · "
        f"{len(report.non_breaking)} non-breaking · "
        f"{len(report.info)} info"
    )
    return "\n".join(parts)
