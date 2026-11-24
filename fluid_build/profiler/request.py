# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile --request FILE``: measure what a FLUX profile request lists, and nothing else.

``fluxsim init``, given a contract and no profile, writes a request: the names of the
statistics, column metrics and fact shares a profiler must measure. The request is the
list of what may be released. A profile that answers it holds, for each listed table,
only the columns, metrics, statistics and relationships it lists, under the same output
checks as any other profile, and says for every listed item it leaves out why.

This module reads and checks the request before any record is read:

* the file is a regular file, held to a size before it is read, and UTF-8 text that
  flux-stats' reader (through :mod:`~fluid_build.profiler._stats`) accepts whole;
* its contract digest, when it has one, is the digest of the contract file as
  ``fluxsim init`` takes it;
* every table is an expose of the contract, every column one of its columns, in the
  domain ``fluid profile`` reads it in;
* every table has its records, and no records are given for a table it does not list.

It then selects what the profile measures from the default statistics of the contract
(:mod:`~fluid_build.profiler.defaults`) and gives each listed item the profile cannot
answer a reason (:func:`not_measured`). A request asks for no grouping, so a grouped
default statistic is not measured. Never a value is named: only tables, columns,
definitions and reasons.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from fluid_build.profiler import runner
from fluid_build.profiler._stats import (
    DeclaredBlock,
    Request,
    RequestRefused,
    RequestRelationship,
    RequestTable,
    StatisticsBackend,
)
from fluid_build.profiler.runner import ProfileError

__all__ = [
    "MOST_CONTRACT_BYTES",
    "MOST_REQUEST_BYTES",
    "check_contract_file",
    "check_flags",
    "check_sources",
    "check_tables",
    "filter_block",
    "not_measured",
    "read_request_file",
    "refuse_unformed",
    "relationship_asks",
    "relationship_dates_asks",
    "relationship_filter",
    "restrict_plan",
    "unformed",
]

#: The most a request may hold: the reader's own bound of 8 MiB of characters, and a margin
#: for the bytes that spell them. The file's size is checked before a byte is read.
MOST_REQUEST_BYTES = 8 * 1024 * 1024 + 64 * 1024
#: The most a contract file may hold for its digest, as a request's.
MOST_CONTRACT_BYTES = 8 * 1024 * 1024 + 64 * 1024

_NOT_MEASURED_RELATIONSHIP = (
    "children per parent is formed as a histogram on fixed bins (marginalHistogram/1) only"
)
_KEY_COLUMN = "a key column of a counted relationship, which its children per parent describe"
_NO_RECORD = "no non-null record to form it over"

#: Why each kind of ask that ``fluid profile`` does not form is left out. The request's
#: reader holds every ask of its format, so none is dropped without a word: each is named
#: with one of these reasons (:func:`unformed`), and ``--strict`` refuses a request that
#: holds one. A public profile forms a table's date pairs and a relationship's dates and
#: exposure (:mod:`fluid_build.profiler.dates`) and names the ones it cannot with a reason of
#: their own; a private release forms none of them, and names each with the reasons below.
NO_DATE_PAIRS = "a private release forms no date pair"
NO_CALENDAR = "fluid profile does not form a calendar"
NO_WORKLOAD = (
    "a workload weighs the column sets a private release may join, and this profile joins none"
)
NO_DATES = "a private release forms no relationship dates"
NO_EXPOSURE = "a private release forms no exposure"
NO_QUERIES = "fluid profile runs no consumer query"


def _refuse(event: str, message: str, next_step: str) -> ProfileError:
    return ProfileError(event, message, {"read": "nothing: no record was read", "next": next_step})


def check_flags(
    *, bundle: Optional[str], env: Optional[str], exposes: Optional[Sequence[str]]
) -> None:
    """Refuse ``--request`` beside a flag that also says what the profile answers or names."""
    if bundle is not None:
        raise _refuse(
            "profile_request_with_bundle",
            "--request and --bundle both say what the profile answers",
            "give one of them: --request for the list a request makes, --bundle for a "
            "Simulation's fidelity block",
        )
    if env is not None:
        raise _refuse(
            "profile_request_with_env",
            "--request names the columns of the contract `fluxsim init` read, and --env "
            "would change them",
            "leave --env out, and profile the contract the request was made for",
        )
    if exposes:
        raise _refuse(
            "profile_request_with_expose",
            "--request names the tables to profile, and --expose would name them again",
            "leave --expose out; give each table its records as --source <exposeId>=<location>",
        )


def _read_regular(path: Path, *, limit: int, event: str, what: str, next_step: str) -> bytes:
    """The bytes of a regular file of at most *limit* bytes, never through a link.

    The size is taken from the open file before any byte is read, and at most *limit* bytes
    are read.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    except OSError as exc:
        reason = "is a link" if exc.errno == errno.ELOOP else exc.strerror or type(exc).__name__
        raise _refuse(event, f"{what} {path.name} cannot be read: {reason}", next_step) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _refuse(event, f"{what} {path.name} is not a regular file", next_step)
        if info.st_size > limit:
            raise _refuse(
                event,
                f"{what} {path.name} is larger than {limit // (1024 * 1024)} MiB and more",
                next_step,
            )
        chunks: List[bytes] = []
        left = limit + 1
        while left > 0:
            chunk = os.read(descriptor, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    except OSError as exc:
        raise _refuse(
            event,
            f"{what} {path.name} cannot be read: {exc.strerror or type(exc).__name__}",
            next_step,
        ) from None
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > limit:
        raise _refuse(
            event, f"{what} {path.name} is larger than {limit // (1024 * 1024)} MiB", next_step
        )
    return data


def read_request_file(path: Path, backend: StatisticsBackend) -> Request:
    """The request in *path*, read whole, or a refusal naming where the problem is."""
    next_step = "write the request again with `fluxsim init --contract <contract>`"
    data = _read_regular(
        path,
        limit=MOST_REQUEST_BYTES,
        event="profile_request_unreadable",
        what="the request",
        next_step=next_step,
    )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _refuse(
            "profile_request_unreadable", f"the request {path.name} is not UTF-8 text", next_step
        ) from None
    try:
        return backend.read_request(text, name=path.name)
    except RequestRefused as exc:
        raise _refuse("profile_request_refused", f"{path.name}: {exc}", next_step) from None


def check_contract_file(path: Path, request: Request, backend: StatisticsBackend) -> Optional[bool]:
    """Refuse a contract the request was not made from; say whether its digest matched.

    A stage-1 bundle (a gzip archive) is refused: ``fluxsim init`` reads a YAML contract.
    Returns ``True`` when the request names a contract digest and it is this contract's,
    and ``None`` when the request names none, which is accepted.
    """
    if path.name.endswith((".tgz", ".tar.gz", ".tar")) or _is_gzip(path):
        raise _refuse(
            "profile_request_contract_is_a_bundle",
            f"{path.name} is an archive, and a request is made from a YAML contract",
            "give the contract file `fluxsim init` read",
        )
    if request.contract_digest is None:
        return None
    again = "run `fluxsim init` again on this contract, then profile with the request it writes"
    data = _read_regular(
        path,
        limit=MOST_CONTRACT_BYTES,
        event="profile_request_contract_unreadable",
        what="the contract",
        next_step=again,
    )
    try:
        actual = backend.contract_digest(data.decode("utf-8"), name=path.name)
    except UnicodeDecodeError:
        raise _refuse(
            "profile_request_contract_unreadable",
            f"the contract {path.name} is not UTF-8 text",
            again,
        ) from None
    except RequestRefused as exc:
        raise _refuse(
            "profile_request_contract_unreadable",
            f"the contract {path.name} cannot be digested: {exc}",
            again,
        ) from None
    if actual != request.contract_digest:
        raise _refuse(
            "profile_request_digest_mismatch",
            f"the request names a contract digest that is not the digest of {path.name}",
            again,
        )
    return True


def _is_gzip(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def check_tables(request: Request, contract: Mapping[str, Any]) -> None:
    """Refuse a table, column or domain of the request that the contract and profile do not share."""
    exposes = runner.expose_ids(contract)
    for table in request.tables:
        if table.expose_id not in exposes:
            raise _refuse(
                "profile_request_table_not_an_expose",
                f"the request lists table {table.expose_id!r}, which is not an expose of the contract",
                "write the request again with `fluxsim init` for this contract; exposes: "
                + (", ".join(exposes) or "(none)"),
            )
        plan = runner.plan_expose(contract, table.expose_id)
        read = {column.name: column.domain for column in plan.columns}
        left_out = dict(plan.skipped)
        for column in table.columns:
            if column.name not in read and column.name not in left_out:
                raise _refuse(
                    "profile_request_column_not_in_contract",
                    f"the request lists column {column.name!r} of {table.expose_id!r}, "
                    "which the contract does not declare",
                    "write the request again with `fluxsim init` for this contract",
                )
            # A column fluid profile does not read at all (a nested value, bytes, a binary
            # float) is reported as not profiled, with its reason; only one read in another
            # domain than the request's means the two disagree about the contract.
            if column.name in read and read[column.name] != column.domain:
                raise _refuse(
                    "profile_request_column_domain",
                    f"the request lists column {column.name!r} of {table.expose_id!r} as "
                    f"{column.domain}, and fluid profile reads it as {read[column.name]}",
                    "write the request again with `fluxsim init` for this contract",
                )


def check_sources(request: Request, sourced: Sequence[str]) -> None:
    """Refuse a listed table with no records, and records for a table the request does not list."""
    listed = [table.expose_id for table in request.tables]
    for expose_id in listed:
        if expose_id not in sourced:
            raise _refuse(
                "profile_request_source_missing",
                f"the request lists table {expose_id!r}, and no --source {expose_id}=<location> "
                "gives its records",
                "give each listed table its records as --source <exposeId>=<location>",
            )
    for expose_id in sourced:
        if expose_id not in listed:
            raise _refuse(
                "profile_request_source_unused",
                f"--source names exposes[{expose_id}], which the request does not list",
                f"leave its --source out, or list it in the request; listed: {', '.join(listed)}",
            )


def restrict_plan(plan: runner.ExposePlan, table: RequestTable) -> runner.ExposePlan:
    """*plan* with only the columns the request lists: no other column is read."""
    listed = {column.name for column in table.columns}
    return replace(
        plan,
        columns=tuple(c for c in plan.columns if c.name in listed),
        skipped=tuple((name, why) for name, why in plan.skipped if name in listed),
    )


def filter_block(declared: Optional[DeclaredBlock], table: RequestTable) -> Optional[DeclaredBlock]:
    """The statistics of *declared* the request lists: ungrouped, and listed for their column."""
    if declared is None:
        return None
    kept = []
    for statistic in declared.statistics:
        ask = table.column(statistic.column)
        if (
            statistic.group_by is None
            and ask is not None
            and statistic.identifier in ask.statistics
        ):
            kept.append(statistic)
    return replace(declared, statistics=tuple(kept))


def relationship_filter(
    request: Request,
) -> Tuple[Callable[[str, str, Tuple[str, ...], Tuple[str, ...]], bool], Set[int]]:
    """A test for a declared relationship, and the listed relationships it matched, by position.

    The test says whether the request lists the declared relationship (child table, parent
    table, key columns of each, pair by pair) with ``marginalHistogram/1``, the one statistic
    answered over children per parent, or with a ``linked`` column (request format 1-2-0).
    """
    matched: Set[int] = set()

    def wanted(
        child: str, parent: str, child_columns: Tuple[str, ...], parent_columns: Tuple[str, ...]
    ) -> bool:
        found = False
        for position, each in enumerate(request.relationships):
            if (
                each.child.table == child
                and each.parent.table == parent
                and each.child.columns == child_columns
                and each.parent.columns == parent_columns
            ):
                matched.add(position)
                found = (
                    found
                    or "marginalHistogram/1" in each.statistics
                    or bool(each.linked)
                    or bool(each.dates)
                    or bool(each.exposure)
                )
        return found

    return wanted, matched


def _links_of(request: Request, relationship: "runner.Relationship") -> List[RequestRelationship]:
    """The links the request lists for one declared relationship: the same tables and keys."""
    return [
        each
        for each in request.relationships
        if each.child.table == relationship.child
        and each.parent.table == relationship.parent
        and each.child.columns == relationship.child_columns
        and each.parent.columns == relationship.parent_columns
    ]


def relationship_asks(
    request: Request, relationship: "runner.Relationship"
) -> Tuple[bool, Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
    """What the request asks of one declared relationship: ``(counted, group_by, linked)``.

    *counted* says whether it lists ``marginalHistogram/1`` for the relationship, which is
    what children per parent is answered with; *group_by* are the parent columns its
    statistics are asked grouped by and *linked* the ``(child column, parent column)`` pairs
    asked, each once, in the order listed.
    """
    counted = False
    group_by: List[str] = []
    linked: List[Tuple[str, str]] = []
    for each in _links_of(request, relationship):
        counted = counted or "marginalHistogram/1" in each.statistics
        group_by.extend(name for name in each.group_by if name not in group_by)
        linked.extend(
            pair
            for pair in ((item.column, item.parent_column) for item in each.linked)
            if pair not in linked
        )
    return counted, tuple(group_by), tuple(linked)


def relationship_dates_asks(
    request: Request, relationship: "runner.Relationship"
) -> Tuple[Tuple[Tuple[str, str], ...], Tuple[Tuple[str, Optional[str]], ...]]:
    """The dates and exposure the request asks of one declared relationship.

    ``(dates, exposure)``: the ``(parent column, child column)`` pairs asked days apart and the
    ``(start, end)`` parent lifetimes asked as children per active day (``end`` is ``None`` for a
    lifetime with no end column), each once, in the order listed.
    """
    dates: List[Tuple[str, str]] = []
    exposure: List[Tuple[str, Optional[str]]] = []
    for each in _links_of(request, relationship):
        dates.extend(
            pair for pair in ((item.parent, item.child) for item in each.dates) if pair not in dates
        )
        exposure.extend(
            pair
            for pair in ((item.start, item.end) for item in each.exposure)
            if pair not in exposure
        )
    return tuple(dates), tuple(exposure)


def _spelled(link: RequestRelationship) -> str:
    return (
        f"{link.child.table}({', '.join(link.child.columns)}) to "
        f"{link.parent.table}({', '.join(link.parent.columns)})"
    )


def _exposure_name(link: RequestRelationship, exposure: Any) -> str:
    span = exposure.start if exposure.end is None else f"{exposure.start} to {exposure.end}"
    return f"exposure {link.parent.table}.{span} of {_spelled(link)}"


def unformed(
    request: Request, *, workload: str = NO_WORKLOAD, private: bool = False
) -> List[Tuple[str, str]]:
    """Each ask of *request* of a kind ``fluid profile`` does not form, with why: ``(what, reason)``.

    Decided from the request alone, before any record is read: a calendar column, a workload
    entry and a consumer query, and, for a *private* release only, a table's date pairs and a
    relationship's dates and exposure (a public profile forms those, and names one it cannot
    with the reason it has, once it has read the records). *workload* is the reason a workload
    entry is not used, which a private release words its own way. Never a value: only tables,
    columns, names and reasons.
    """
    found: List[Tuple[str, str]] = []
    for table in request.tables:
        for pair in table.date_pairs if private else ():
            found.append(
                (f"date pair {pair.start} to {pair.end} of {table.expose_id}", NO_DATE_PAIRS)
            )
        for column in table.calendar:
            found.append((f"calendar of {table.expose_id}.{column}", NO_CALENDAR))
        for entry in table.workload:
            found.append((f"workload {', '.join(entry.columns)} of {table.expose_id}", workload))
    for link in request.relationships if private else ():
        for dates in link.dates:
            found.append(
                (
                    f"dates {link.parent.table}.{dates.parent} to {link.child.table}.{dates.child} "
                    f"of {_spelled(link)}",
                    NO_DATES,
                )
            )
        for exposure in link.exposure:
            found.append((_exposure_name(link, exposure), NO_EXPOSURE))
    for name in request.queries:
        found.append((f"query {name}", NO_QUERIES))
    return found


def refuse_unformed(request: Request, *, private: bool = False) -> None:
    """``--strict``: refuse *request* when it asks for anything :func:`unformed` names.

    A public profile forms date pairs, dates and exposure, so ``--strict`` leaves them: whether
    one can be formed depends on its columns' types and, for an exposure, on a window the
    records give, and the profile names each it cannot under Not measured.

    Raised before any record is read, so the refusal says nothing about the records. The
    message counts the asks and its context names each, grouped by its reason.
    """
    found = unformed(request, private=private)
    if not found:
        return
    by_reason: Dict[str, List[str]] = {}
    for what, reason in found:
        by_reason.setdefault(reason, []).append(what)
    raise ProfileError(
        "profile_request_not_formed",
        f"--strict: the request asks for {len(found)} item{'' if len(found) == 1 else 's'} "
        "fluid profile does not form",
        {
            **{reason: "; ".join(items) for reason, items in by_reason.items()},
            "read": "nothing: no record was read",
            "next": (
                "leave --strict out to profile what fluid profile forms, with each of these "
                "named under Not measured, or write the request without them"
            ),
        },
    )


def not_measured(
    request: Request,
    *,
    tables: Mapping[str, Mapping[str, Any]],
    defaults: Mapping[str, Optional[DeclaredBlock]],
    uncovered: Mapping[str, Mapping[str, str]],
    keys: Mapping[str, FrozenSet[str]],
    matched: Set[int],
    metric_names: FrozenSet[str],
) -> List[Tuple[str, str]]:
    """Each listed item the profile does not hold, with the reason: ``(what, reason)``.

    *tables* are the profile's table documents, *defaults* each table's default
    block before the request's filter, *uncovered* the columns the defaults leave
    out with why, *keys* the key columns of the counted relationships, and *matched* the
    listed relationships that name a declared one. A listed fact is the runner's
    (:func:`fluid_build.profiler.runner._fact_entries`). Never a value.
    """
    found: List[Tuple[str, str]] = []
    for table in request.tables:
        found.extend(_table_not_measured(table, tables, defaults, uncovered, keys, metric_names))
    for position, link in enumerate(request.relationships):
        found.extend(_link_not_measured(link, declared=position in matched))
    found.extend(unformed(request))
    return found


def _table_not_measured(
    table: RequestTable,
    tables: Mapping[str, Mapping[str, Any]],
    defaults: Mapping[str, Optional[DeclaredBlock]],
    uncovered: Mapping[str, Mapping[str, str]],
    keys: Mapping[str, FrozenSet[str]],
    metric_names: FrozenSet[str],
) -> List[Tuple[str, str]]:
    """Each item a request lists of one table that the profile does not hold, with why."""
    found: List[Tuple[str, str]] = []
    document = tables.get(table.expose_id, {})
    if table.row_count and "rowCount" not in document:
        found.append((f"rowCount of {table.expose_id}", "the output check withholds it"))
    for column in table.columns:
        if column.name not in document.get("columns", {}):
            continue  # not read at all: the report names it, with why, as not profiled
        entry = document["columns"][column.name]
        for name in column.metrics:
            if name in entry.get("metrics", {}):
                continue
            reason = (
                _NO_RECORD if name in metric_names else "fluid profile has no output check for it"
            )
            found.append((f"{name} of {table.expose_id}.{column.name}", reason))
        published = {each["definition"] for each in entry.get("statistics", ())}
        default_block = defaults.get(table.expose_id)
        default_ids = {
            each.identifier
            for each in (default_block.statistics if default_block else ())
            if each.column == column.name and each.group_by is None
        }
        for identifier in column.statistics:
            if identifier in published:
                continue
            reason = _why(
                identifier,
                column.domain,
                column.name,
                default_ids,
                uncovered.get(table.expose_id, {}),
                keys.get(table.expose_id, frozenset()),
            )
            found.append((f"{identifier} of {table.expose_id}.{column.name}", reason))
    return found


def _undeclared_not_measured(link: RequestRelationship) -> List[Tuple[str, str]]:
    """Each ask of a listed relationship the contract does not declare, with why."""
    undeclared = "the contract declares no such relationship"
    found: List[Tuple[str, str]] = []
    if link.statistics or link.group_by or link.linked or not (link.dates or link.exposure):
        found.append((f"children per parent of {_spelled(link)}", undeclared))
    for dates in link.dates:
        found.append(
            (
                f"dates {link.parent.table}.{dates.parent} to "
                f"{link.child.table}.{dates.child} of {_spelled(link)}",
                undeclared,
            )
        )
    for exposure in link.exposure:
        found.append((_exposure_name(link, exposure), undeclared))
    return found


def _link_not_measured(link: RequestRelationship, *, declared: bool) -> List[Tuple[str, str]]:
    """Each ask of one listed relationship that the profile does not hold, with why."""
    if not declared:
        return _undeclared_not_measured(link)
    found: List[Tuple[str, str]] = []
    if link.group_by and "marginalHistogram/1" not in link.statistics:
        found.append(
            (
                f"groupBy of children per parent of {_spelled(link)}",
                "children per parent is counted only where marginalHistogram/1 is listed",
            )
        )
    for identifier in link.statistics:
        if identifier != "marginalHistogram/1":
            found.append(
                (
                    f"{identifier} of children per parent of {_spelled(link)}",
                    _NOT_MEASURED_RELATIONSHIP,
                )
            )
    return found


def _why(
    identifier: str,
    domain: str,
    column: str,
    default_ids: Set[str],
    uncovered: Mapping[str, str],
    keys: FrozenSet[str],
) -> str:
    """Why a listed statistic of a column is not in the profile."""
    if column in keys:
        return _KEY_COLUMN
    if identifier == "mean/1":
        if domain != "number":
            return "a mean is formed for a number column only"
        if "marginalHistogram/1" in default_ids:
            return (
                "beside a histogram over its declared range its sum would show what the "
                "histogram withholds"
            )
        return _NO_RECORD
    if identifier in default_ids:
        return "no record forms it"
    if column in uncovered:
        return uncovered[column]
    if identifier == "marginalHistogram/1":
        if "quantile/2" in default_ids:
            return "it has no declared range, so it gets quantile/2 on its type's ladder"
        return "it has neither a declared range nor a type that bounds it"
    if identifier == "quantile/2":
        if "marginalHistogram/1" in default_ids:
            return "a column gets a histogram or quantiles, never both on one set of bins"
        return "it has neither a declared range nor a type that bounds it"
    if identifier == "support/1":
        if "levelHistogram/1" in default_ids:
            return "beside a level histogram it would show which withheld cells hold no record"
        return "its levels are not declared by an enum(...) type"
    if identifier == "levelHistogram/1":
        return "its type declares no levels, as an enum(...) or a boolean does"
    if identifier == "quantile/1":
        return "a quantile without bins is not formed, quantile/2 is"
    return "fluid profile forms no such statistic"
