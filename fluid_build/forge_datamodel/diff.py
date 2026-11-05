# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Structural diffs for forged logical models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fluid_build.copilot.schemas.stage_outputs import LogicalDraft


def load_logical(path: Path) -> LogicalDraft:
    return LogicalDraft.model_validate_json(path.read_text(encoding="utf-8"))


def diff_logical_models(old_path: Path, new_path: Path) -> Dict[str, Any]:
    old = load_logical(old_path)
    new = load_logical(new_path)
    summary: Dict[str, Any] = {
        "old": str(old_path),
        "new": str(new_path),
        "technique": {"old": old.technique, "new": new.technique},
        "changes": [],
    }

    if old.technique != new.technique:
        summary["changes"].append(f"Technique changed from {old.technique} to {new.technique}.")

    if old.technique == "data_vault_2" and old.dv2 and new.dv2:
        summary["changes"].extend(
            _diff_named_lists(
                "hub",
                [hub.hub_table_name for hub in old.dv2.hubs],
                [hub.hub_table_name for hub in new.dv2.hubs],
            )
        )
        summary["changes"].extend(
            _diff_named_lists(
                "link",
                [link.link_table_name for link in old.dv2.links],
                [link.link_table_name for link in new.dv2.links],
            )
        )
        summary["changes"].extend(
            _diff_named_lists(
                "satellite",
                [sat.satellite_table_name for sat in old.dv2.satellites],
                [sat.satellite_table_name for sat in new.dv2.satellites],
            )
        )
    elif old.dimensional and new.dimensional:
        summary["changes"].extend(
            _diff_named_lists(
                "dimension",
                [dim.name for dim in old.dimensional.dimensions],
                [dim.name for dim in new.dimensional.dimensions],
            )
        )
        summary["changes"].extend(
            _diff_named_lists(
                "fact",
                [fact.name for fact in old.dimensional.facts],
                [fact.name for fact in new.dimensional.facts],
            )
        )

    old_metrics = {metric.name for metric in old.osi.metrics}
    new_metrics = {metric.name for metric in new.osi.metrics}
    summary["changes"].extend(_diff_named_lists("metric", sorted(old_metrics), sorted(new_metrics)))
    return summary


def _diff_named_lists(kind: str, old_items: List[str], new_items: List[str]) -> List[str]:
    changes: List[str] = []
    old_set = set(old_items)
    new_set = set(new_items)
    for item in sorted(new_set - old_set):
        changes.append(f"Added {kind} {item}.")
    for item in sorted(old_set - new_set):
        changes.append(f"Removed {kind} {item}.")
    return changes
