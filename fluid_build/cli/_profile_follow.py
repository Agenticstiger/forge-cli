# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The report of a private release whose unit follows its child rows.

``fluid profile --privacy-unit U --follow ...`` releases the unit's expose and every table the
unit reaches (:mod:`fluid_build.profiler.dp.follow_release`). The report says what the unit is,
which tables it reaches and with what ``m_T``, which tables it treats as fixed, the budget, each
part's sensitivity, scale and share, and what is not released and why. Only what is public or
part of the release is said: never a count of rows, entities, dropped rows or children, and
never the seed. Only the standard library is imported at module scope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["report"]

_KIND = {
    "clique": "clique of",
    "levels": "level histogram of",
    "bins": "histogram of",
    "rank": "rank correlation",
    "relationship": "children per parent,",
    "link": "histogram grouped through a link,",
}


def _words(plan: Any) -> List[str]:
    """The privacy unit in plain words, and the tables it reaches and treats as fixed."""
    steps = [
        f"{edge.max_children} rows of exposes[{edge.child}] ({', '.join(edge.spec.child_columns)}) "
        f"for each row of exposes[{edge.parent}] ({', '.join(edge.spec.parent_columns)}), "
        f"through {edge.name}"
        for edge in plan.follows.edges
    ]
    reached = ", ".join(f"{each.table} (m = {each.reach})" for each in plan.tables)
    lines = [
        f"Privacy unit: one row of exposes[{plan.unit}] together with the rows that follow it, at "
        f"most " + "; then at most ".join(steps) + ". Which children a parent row keeps is "
        "decided by a keyed priority that reads only that parent's own children, never the "
        "other entities or the order of the records; a child that is dropped takes its "
        "descendants, and a row that follows no kept row is dropped. The stated loss holds for "
        "each such unit.",
        f"Tables the unit reaches, with m, the rows of the table one unit contributes: {reached}.",
    ]
    fixed = [name for name, _ in plan.fixed]
    if fixed:
        lines.append(
            "Tables the unit does not reach, which the guarantee treats as fixed (their rows "
            f"are the same in every neighbouring data set): {', '.join(fixed)}. A statistic "
            "grouped by a column of such a table protects the unit's rows and not that table's."
        )
    else:
        lines.append(
            "The release holds no table the unit does not reach; a table the profile does not "
            "hold is not reached either."
        )
    return lines


def report(
    say: Any,
    plan: Any,
    drawn: Any,
    out: Path,
    record_path: Optional[Path],
    *,
    request: Tuple[str, Optional[bool]],
    rederived: bool,
) -> None:
    """Tell the owner what was released, under which loss, and what was left out."""
    from fluid_build.cli.console import warning
    from fluid_build.profiler.dp import release

    say(
        f"Released exposes[{plan.unit}] and the tables its unit follows into under differential "
        f"privacy: noisy counts, discrete Gaussian noise, answering the request {request[0]}"
        + (" (its contract digest is this contract's)." if request[1] else "."),
        soft_wrap=True,
    )
    for line in _words(plan):
        say(line, soft_wrap=True)
    say(
        f"The profile names exposes[{plan.unit}] and its follows as its privacy unit (format "
        "1-4-0, PROFILE-SPEC section 3a); the engine cannot check that the profiler truncated "
        "the tables as stated, nor that noise was drawn, and its evidence says so.",
        soft_wrap=True,
    )
    for text in plan.warnings:
        warning(text)
    total = release.decimal_text(plan.rho)
    ranked = sum(1 for each in drawn.tables if each.kind == "rank")
    measured = f"{len(drawn.tables) - ranked} statistics"
    if ranked:
        measured += f" and {ranked} rank correlation{'s' if ranked != 1 else ''}"
    spent = f"{release.decimal_text(drawn.measurement_rho)} measuring {measured}"
    if drawn.selection_rho:
        spent = f"{release.decimal_text(drawn.selection_rho)} choosing the cliques, " + spent
    say(f"Privacy loss: rho {total} in all (zero-concentrated): {spent}.", soft_wrap=True)
    epsilon = release.epsilon_at_delta(plan.rho)
    say(
        f"For information, rho {total} is epsilon {epsilon} at delta {release.DELTA_TEXT} "
        "(epsilon = rho + 2 sqrt(rho ln(1/delta)), Bun and Steinke 2016, rounded up); the "
        "stated loss is rho.",
        soft_wrap=True,
    )
    rests = (
        "The noise is pseudorandom, drawn from HMAC-SHA256 keyed by the recorded seed, so the "
        "guarantee holds as long as HMAC-SHA256 behaves as a pseudorandom function."
    )
    if plan.selects:
        rests += (
            f" Choosing the cliques uses OpenDP's {plan.clique_method.upper()}, which OpenDP "
            "ships behind its contrib flag."
        )
    rests += (
        " The guarantee covers data that meets the contract (required columns hold a value, "
        "values lie in their declared levels and ranges, and each followed parent key names one "
        "row); data that does not is refused, releases nothing, and the refusal is for the data "
        "owner (it names a table, a column and a row): a refusal is not covered by the "
        "guarantee."
    )
    say(rests, soft_wrap=True)
    for table in drawn.tables:
        place = f" ({table.table})" if table.table else ""
        if table.kind == "rank":
            first, second = table.columns
            say(
                f"  {_KIND[table.kind]} {table.definition} of {first} and {second}{place}: noise "
                f"scale {table.scale}, share rho {table.share}, row floor {table.row_floor}, "
                f"sensitivity {table.delta} for one unit",
                soft_wrap=True,
            )
            continue
        if table.kind in ("relationship", "link"):
            what = table.label
        else:
            what = ", ".join(table.columns)
        say(
            f"  {_KIND[table.kind]} {what}{place}: noise scale {table.scale}, share rho "
            f"{table.share}, squared L2 sensitivity {table.sensitivity} for one unit",
            soft_wrap=True,
        )
    if plan.selects:
        chosen: Dict[str, Any] = dict(drawn.cliques)
        listed = "; ".join(
            f"{name}: " + ", ".join(", ".join(clique) for clique in found)
            for name, found in chosen.items()
            if found
        )
        origin = (
            "from the release record" if rederived else f"chosen with {plan.clique_method.upper()}"
        )
        say(f"Cliques {origin}: {listed or 'none'}.", soft_wrap=True)
    by_reason: Dict[str, List[str]] = {}
    for what, reason in plan.not_released:
        by_reason.setdefault(reason, []).append(what)
    for reason, items in by_reason.items():
        say(f"Not released ({reason}): {'; '.join(items)}.", soft_wrap=True)
    if rederived:
        say("Drawn again from the release record: the profile has the recorded digest.")
    say(f"Wrote {out} (readable by you alone; read it before you share it).", soft_wrap=True)
    if record_path is not None:
        say(f"Wrote the release record {record_path} (readable by you alone).", soft_wrap=True)
        say(
            "Keep the release record inside the data owner's boundary: the noise is drawn from "
            "a seeded randomness source recorded there, and with the record and the profile "
            "the noise can be taken off. Never reuse it for another release.",
            soft_wrap=True,
        )
    say("Pin it in the FLUX Simulation:")
    say(f"  profile: {{digest: {drawn.digest}}}", soft_wrap=True)
