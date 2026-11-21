# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The freshness dimension of ``fluid verify``: how old is the newest write to an Iceberg table.

An Iceberg table keeps one snapshot per commit, and Athena lists them in the
metadata table ``"<database>"."<table>$snapshots"``, with the commit time
(``committed_at``), the snapshot id and the operation. The newest snapshot that
wrote data, set against the expose's ``qos.freshnessSLO`` (an ISO-8601
duration), says whether the table is as fresh as the contract promises.

Two choices decide what "the newest write" means:

* **A compaction is not a write.** Iceberg defines the ``replace`` operation as
  files "removed and replaced, without changing the data in the table"
  (``org.apache.iceberg.DataOperations``), which is what a rewrite or an Athena
  ``OPTIMIZE`` commits. Counting it would show a table nobody has written to for
  a week as fresh as its last compaction. Every other operation (``append``,
  ``overwrite``, ``delete``) is a write.
* **The age is computed by Athena.** ``current_timestamp`` is the query's own
  start, so this machine's clock, and its time zone, are not in the answer. The
  commit time is the one the writer recorded, by its own clock; one dated after
  Athena's is not fresher than now, and counts as age zero.

Everything read back from Athena is data a writer of the table chose, so it is
bounded before it is printed: a commit time or an age outside what a time can
hold is an unreadable result, and an operation is kept only when it looks like
one (a short word).

This module holds no AWS call: ``_verify_athena`` runs the query and hands the
rows here. The prior art is dbt's source freshness (the newest value of a
timestamp against now, in the warehouse, with a warn and an error threshold) and
Soda's freshness check; neither reads Iceberg snapshot metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

#: The one snapshot operation that changes no data (see the module docstring).
COMPACTION_OPERATION = "replace"

#: What an operator does about a table that missed its objective.
FIX = "Run the job that writes the table, or find out why it stopped, then verify again"

#: Athena does not evaluate Lake Formation for an Iceberg table's metadata tables, and
#: refuses them to a principal that is filtered on the base table (AWS Athena User
#: Guide, "Query Apache Iceberg tables", Considerations and limitations).
LAKE_FORMATION_HINT = (
    "Athena refuses an Iceberg table's metadata tables (the snapshots) when Lake Formation "
    "has row or cell filters on the table, or the principal cannot see every column: run "
    "verify as a principal that can read the whole table"
)

# An operation is a short word (``append``, ``replace``, ``overwrite``, ``delete``).
_OPERATION = re.compile(r"[A-Za-z0-9_.-]{1,32}")
# A week has a fixed length, so ``P1W`` and ``P1W2D`` are thresholds; the repo's duration
# parser leaves weeks out, with years and months, on the ground that they need a calendar.
_WEEKS = re.compile(r"P(\d+)W(.*)", re.DOTALL)
# What can follow the weeks and add nothing (``P1W0D``, ``P1WT0H0M0S``): the schema takes it, and
# the shared parser refuses an all-zero duration, so it is told apart from garbage here.
_ZERO_TAIL = re.compile(r"(?:0+D)?(?:T(?=\d)(?:0+H)?(?:0+M)?(?:0+(?:\.0+)?S)?)?")
# What fits exactly in a float, and so in a JSON number: the bound of an age in milliseconds.
_MAX_MS = 2**53
_ACCESS_DENIED = re.compile(r"access\s?denied|lake\s?formation", re.IGNORECASE)


@dataclass(frozen=True)
class SnapshotReading:
    """What the snapshot query answered.

    ``last_commit_ms`` and the fields that depend on it are ``None`` when no
    snapshot recorded a write; ``counted`` is how many did. ``operation`` is
    ``None`` when the snapshot has none, or one that does not look like an operation.
    """

    last_commit_ms: Optional[int]
    age_ms: Optional[int]
    snapshot_id: Optional[int]
    operation: Optional[str]
    counted: int


def declared_objective(expose: Mapping[str, Any]) -> Optional[str]:
    """The expose's ``qos.freshnessSLO`` as written, or ``None`` when it declares none."""
    qos = expose.get("qos")
    value = qos.get("freshnessSLO") if isinstance(qos, Mapping) else None
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def objective_seconds(objective: str) -> Optional[float]:
    """The objective in seconds, or ``None`` when it is not a duration verify can compare.

    Weeks, days, hours, minutes and seconds: a month or a year has no length
    without a calendar, so ``P1M`` is not a threshold, and nor is a length no
    ``timedelta`` can hold. The one parser the repo keeps for ISO-8601 durations
    reads the rest; it is imported here so that nothing in ``fluid verify --help``
    pulls the build runners in.
    """
    from fluid_build.build_runners._late_arrival import parse_iso_duration

    text = objective.strip()
    weeks = timedelta(0)
    match = _WEEKS.fullmatch(text)
    try:
        if match is not None:
            weeks = timedelta(weeks=int(match.group(1)))
            rest = match.group(2)
            # Days and a time part may follow the weeks; nothing, or only zeros, adds nothing.
            text = "P" if _ZERO_TAIL.fullmatch(rest) else "P" + rest
            if text == "P":
                return weeks.total_seconds() or None
        duration = parse_iso_duration(text)
        if duration is None:
            return None
        return (weeks + duration).total_seconds() or None
    except (OverflowError, ValueError):
        return None


def snapshot_sql(database: str, table: str) -> str:
    """The query for one table. ``database`` and ``table`` are checked by the caller.

    ``IS DISTINCT FROM`` rather than ``<>``: a snapshot a writer left without an
    operation has a NULL one, and ``NULL <> 'replace'`` is not true, so ``<>``
    would drop a write from the count.

    One row, whatever the table holds: the newest write's commit time as epoch
    milliseconds, its age in milliseconds, its snapshot id and operation, and
    how many snapshots wrote data. Both times are integers, so nothing depends
    on how Athena formats a timestamp or a double.
    """
    return (
        "SELECT CAST(to_unixtime(max(committed_at)) * 1000 AS bigint), "
        "date_diff('millisecond', max(committed_at), current_timestamp), "
        "max_by(snapshot_id, committed_at), max_by(operation, committed_at), COUNT(*) "
        f'FROM "{database}"."{table}$snapshots" '
        f"WHERE operation IS DISTINCT FROM '{COMPACTION_OPERATION}'"
    )


def _iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()


def read_snapshot_row(rows: Sequence[Mapping[str, Any]]) -> SnapshotReading:
    """The reading in ``rows`` (Athena's result rows); ``ValueError`` when they hold none.

    ``rows[0]`` is the header Athena always returns for a SELECT. A cell that is
    NULL has no ``VarCharValue``: an aggregate over no row is one row of NULLs
    and a count of zero.
    """
    try:
        cells = [cell.get("VarCharValue") for cell in rows[1]["Data"]]
        if len(cells) != 5 or cells[4] is None:
            raise ValueError("the row does not have the five columns the query selects")
        counted = int(cells[4])
        if cells[0] is None:
            return SnapshotReading(None, None, None, None, counted)
        if cells[1] is None:
            raise ValueError("a commit time without an age")
        last_commit_ms, age_ms = int(cells[0]), int(cells[1])
        if abs(age_ms) > _MAX_MS:
            raise ValueError("an age too large to be a length of time")
        try:
            _iso(last_commit_ms)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("a commit time outside the years 1 to 9999") from exc
        operation = cells[3]
        return SnapshotReading(
            last_commit_ms=last_commit_ms,
            age_ms=age_ms,
            snapshot_id=int(cells[2]) if cells[2] is not None else None,
            operation=operation if operation and _OPERATION.fullmatch(operation) else None,
            counted=counted,
        )
    except (IndexError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"unreadable result row: {exc}") from exc


def _span(seconds: float) -> str:
    """``7380`` as ``2h 3m``: the largest unit that is not zero, and the one below it if it is not."""
    remaining = int(max(seconds, 0))
    units = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, remaining = divmod(remaining, size)
        units.append((count, unit))
    for index, (count, unit) in enumerate(units):
        if count:
            below_count, below_unit = units[index + 1] if index + 1 < len(units) else (0, "")
            return f"{count}{unit}" + (f" {below_count}{below_unit}" if below_count else "")
    return "0s"


def freshness_dimension(
    objective: str, seconds: float, reading: SnapshotReading, *, reference_only: bool = False
) -> Dict[str, Any]:
    """``pass`` (age within the objective, the limit included), ``fail`` or ``skip``.

    No snapshot that wrote data is ``fail``: the objective cannot be shown met
    by a table nothing is known to have written to, or whose history was
    expired down to compactions. ``reference_only`` (a pipeline outside forge
    owns the rows and may not have run yet) makes that ``skip``, as an empty
    table is ``info`` for the row count there.
    """
    dimension: Dict[str, Any] = {
        "objective": objective,
        "objective_seconds": seconds,
        "snapshots_counted": reading.counted,
    }
    if reading.last_commit_ms is None or reading.age_ms is None:
        if reference_only:
            dimension.update(
                status="skip",
                message=(
                    "no snapshot records a write yet; not gated, because the contract is "
                    "reference-only and the pipeline that owns the table may not have run"
                ),
            )
        else:
            dimension.update(
                status="fail",
                message=(
                    "no snapshot records a write to the table, so its age cannot be shown to "
                    f"meet {objective} (the history may have been expired down to compactions)"
                ),
                fix=FIX,
            )
        return dimension

    age_seconds = max(reading.age_ms, 0) / 1000
    details = ", ".join(
        part
        for part in (
            f"snapshot {reading.snapshot_id}" if reading.snapshot_id is not None else None,
            reading.operation,
        )
        if part
    )
    written = f"last write {_span(age_seconds)} ago" + (f" ({details})" if details else "")
    dimension.update(
        age_seconds=age_seconds,
        last_commit=_iso(reading.last_commit_ms),
        snapshot_id=reading.snapshot_id,
        operation=reading.operation,
    )
    if age_seconds <= seconds:
        dimension.update(status="pass", message=f"{written}, within {objective}")
    else:
        dimension.update(status="fail", message=f"{written}; the objective is {objective}", fix=FIX)
    return dimension


def skipped_dimension(objective: str, reason: str) -> Dict[str, Any]:
    """A declared objective that is not checked, with why. Never gates."""
    return {"status": "skip", "objective": objective, "message": f"not checked: {reason}"}


def error_dimension(objective: str, message: str) -> Dict[str, Any]:
    """A declared objective whose check could not run: unproven, not passed."""
    return {"status": "error", "objective": objective, "message": message}


def explain_failure(message: str) -> str:
    """``message``, with the reason Athena most often refuses the query added when it fits."""
    if _ACCESS_DENIED.search(message):
        return f"{message.rstrip('. ')}. {LAKE_FORMATION_HINT}"
    return message


def severity_problem(dimension: Optional[Mapping[str, Any]]) -> Optional[Tuple[str, str]]:
    """``(reason, action)`` when the objective was missed, for the severity grading."""
    if not dimension or dimension.get("status") != "fail":
        return None
    return (f"freshness objective missed: {dimension.get('message')}", FIX)
