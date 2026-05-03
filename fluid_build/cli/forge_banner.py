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

"""Roadmap banner helpers for staged forge surfaces."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from importlib import resources
from typing import Optional

from fluid_build.cli.console import cprint


@dataclass
class RoadmapMilestone:
    version: str
    title: str
    target_date: date


_BANNER_SURFACES = {
    "forge_data_model",
    "speed_transformation",
    "init_copilot",
    "ai_setup",
    "version",
}
_EXPIRES_ON = date(2026, 5, 7)


def _today() -> date:
    override = os.environ.get("FLUID_BANNER_TODAY")
    if override:
        try:
            return datetime.strptime(override, "%Y-%m-%d").date()
        except ValueError:
            pass
    return date.today()


def _load_roadmap_text() -> str:
    return resources.files("fluid_build.copilot").joinpath("roadmap.md").read_text(encoding="utf-8")


def load_milestones() -> list[RoadmapMilestone]:
    text = _load_roadmap_text()
    milestones: list[RoadmapMilestone] = []
    current_title: Optional[tuple[str, str]] = None
    for line in text.splitlines():
        header = re.match(r"##\s+Milestone\s+(v[\d.]+)\s+—\s+(.+)", line.strip())
        if header:
            current_title = (header.group(1), header.group(2).strip())
            continue
        target = re.match(r"Target date:\s+(\d{4}-\d{2}-\d{2})", line.strip())
        if current_title and target:
            milestones.append(
                RoadmapMilestone(
                    version=current_title[0],
                    title=current_title[1],
                    target_date=datetime.strptime(target.group(1), "%Y-%m-%d").date(),
                )
            )
            current_title = None
    return milestones


def next_milestone(today: Optional[date] = None) -> Optional[RoadmapMilestone]:
    today = today or _today()
    for milestone in load_milestones():
        if milestone.target_date >= today:
            return milestone
    milestones = load_milestones()
    return milestones[-1] if milestones else None


def banner_enabled(surface: str, *, quiet: bool = False) -> bool:
    """Show the roadmap-teaser banner only when the operator opts in.

    UX hardening pass — the banner used to display by default on
    every ``fluid forge data-model`` invocation, which interactive
    users found noisy ("v1.2 (Semantic Reuse) lands by May 07, 2026
    — see fluid roadmap"). Default is now off; opt-in via
    ``FLUID_BANNER=1`` for users who like seeing the roadmap teaser
    and milestone callout. The other gates (``--quiet``,
    ``FLUID_QUIET``, ``FLUID_NONINTERACTIVE``) keep working so a
    legacy script that disables the banner doesn't see new
    behaviour.
    """
    if quiet:
        return False
    if surface not in _BANNER_SURFACES:
        return False
    if os.environ.get("FLUID_QUIET") == "1":
        return False
    if os.environ.get("FLUID_NONINTERACTIVE") == "1":
        return False
    # Opt-in toggle. ``FLUID_BANNER=1`` (or any truthy "1/true/yes/on")
    # surfaces the banner; default off keeps daily CLI usage uncluttered.
    if os.environ.get("FLUID_BANNER", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    return _today() < _EXPIRES_ON


def print_v2_banner(surface: str, *, quiet: bool = False) -> None:
    """Print the v2 preview banner unless suppressed.

    ``quiet`` gives each caller a way to forward ``args.quiet`` from its
    argparse namespace so ``--quiet`` / ``-q`` on any subcommand
    suppresses the banner consistently alongside the ``FLUID_QUIET``
    environment variable.
    """
    if not banner_enabled(surface, quiet=quiet):
        return
    milestone = next_milestone()
    if milestone is None:
        return
    cprint("─────────────────────────────────────────────────────────────────────────")
    cprint("  forge-cli v1.0  ·  Data-Model Forge is live")
    cprint(
        f"  ▸ {milestone.version} ({milestone.title}) lands by "
        f"{milestone.target_date.strftime('%b %d, %Y')} — see fluid roadmap"
    )
    cprint("  ▸ Suppress this banner: export FLUID_QUIET=1  (or set FLUID_NONINTERACTIVE=1)")
    cprint("─────────────────────────────────────────────────────────────────────────")


def compact_next_line() -> str:
    milestone = next_milestone()
    if milestone is None:
        return ""
    return (
        f"next: {milestone.version} · {milestone.title} · by "
        f"{milestone.target_date.strftime('%b %d, %Y')} · fluid roadmap"
    )
