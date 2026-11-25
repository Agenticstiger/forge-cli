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
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "columns_text",
    "left_out_words",
    "part_words",
    "report",
    "say_not_released",
    "unknown_values_notes",
]

_KIND = {
    "clique": "clique of",
    "levels": "level histogram of",
    "bins": "histogram of",
    "rank": "rank correlation",
    "relationship": "children per parent,",
    "link": "histogram grouped through a link,",
    "pairDays": "days of the date pair",
    "pairOrder": "order of the date pair",
}


def unknown_values_notes(plan: Any, table: Optional[str] = None) -> List[str]:
    """What the histograms of columns that may hold a null and of date pairs count, in words.

    Names columns and pairs only: the format holds no noisy null count, so none is released, and a
    share of nulls is never stated.
    """
    place = "" if table is None else f" of exposes[{table}]"
    notes: List[str] = []
    nullable = [each.name for each in plan.columns if each.nullable]
    if nullable:
        notes.append(
            f"The histogram of {', '.join(nullable)}{place}, which the contract does not declare "
            "required, counts the column's known values: a record with no value lands in no cell. "
            "It joins no clique, and no null count or share of nulls is released."
        )
    if plan.date_pairs:
        notes.append(
            f"The days and the order of each date pair{place} count the records where both dates "
            "are known, on the whole public ladder of days: "
            + "; ".join(f"{start} to {end}" for start, end in plan.date_pairs)
            + "."
        )
    return notes


def columns_text(table: Any) -> str:
    """The columns of a released table as a report line names them: a date pair reads start to end."""
    joiner = " to " if table.kind in ("pairDays", "pairOrder") else ", "
    return joiner.join(table.columns)


def part_words(label: str, table: Optional[str] = None) -> str:
    """A release part's label in the words the other items it leaves out are named with.

    ``[<table>/]pair/<days|order>/<start>/<end>`` is a date pair, ``[<table>/]noise/<column>`` a
    column's histogram, and ``link/<child>.<column>/by/<parent>.<column>/via/...`` a column grouped
    through a link; any other label is said as it is.
    """
    steps = label.split("/")
    if steps[0] == "link" and len(steps) >= 4 and "." in steps[1]:
        child, column = steps[1].split(".", 1)
        return f"{column} of {child} grouped by {steps[3]}"
    if steps[0] not in ("noise", "pair") and len(steps) > 1:
        table, steps = steps[0], steps[1:]
    where = f" of {table}" if table else ""
    if steps[0] == "pair" and len(steps) == 4:
        return f"date pair {steps[2]} to {steps[3]}{where}"
    if steps[0] == "noise" and len(steps) == 2:
        return f"{steps[1]}{where}"
    return label


def left_out_words(
    left_out: Sequence[Tuple[str, str]], table: Optional[str] = None
) -> List[Tuple[str, str]]:
    """The parts the cap leaves out, each named once in words (a pair's two parts are one)."""
    found: Dict[Tuple[str, str], None] = {}
    for label, reason in left_out:
        found.setdefault((part_words(label, table), reason), None)
    return list(found)


def say_not_released(say: Any, plan: Any, drawn: Any) -> None:
    """Say what the release does not hold, one line for each reason: the plan's and the cap's."""
    by_reason: Dict[str, List[str]] = {}
    left = left_out_words(drawn.left_out, getattr(plan, "table", None))
    for what, reason in (*plan.not_released, *left):
        by_reason.setdefault(reason, []).append(what)
    for reason, items in by_reason.items():
        say(f"Not released ({reason}): {'; '.join(items)}.", soft_wrap=True)


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
        "row, except that a followed relationship out of a versioned table by its natural entity "
        "repeats its key by design and then needs an entity's versions to follow one parent row, "
        "refused as profile_release_entity_units otherwise); data that does not is refused, releases nothing, and the refusal is for the data "
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
        what = table.label if table.kind in ("relationship", "link") else columns_text(table)
        say(
            f"  {_KIND[table.kind]} {what}{place}: noise scale {table.scale}, share rho "
            f"{table.share}, squared L2 sensitivity {table.sensitivity} for one unit",
            soft_wrap=True,
        )
    for each in (drawn.plan or plan).tables:
        for line in unknown_values_notes(each.plan, each.table):
            say(line, soft_wrap=True)
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
    say_not_released(say, plan, drawn)
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
