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

from fluid_build.profiler import dates, query_rates, runner
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
    "NO_QUERIES_PRIVATE",
    "contract_unformed",
    "decided_by_records",
    "filter_block",
    "not_measured",
    "read_request_file",
    "refuse_asks",
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
_NO_RECORD_FORMS = "no record forms it"
_WITHHELD_ROW_COUNT = "the output check withholds it"
_TIMESTAMP_LADDER = "its date span is not published, so it gets quantile/2 on its type's ladder"
_TIMESTAMP_NEVER_BOTH = (
    "its histogram is on its published span or declared range, and a column gets a histogram "
    "or quantiles, never both on one set of bins"
)

#: Why each kind of ask that ``fluid profile`` does not form is left out. The request's
#: reader holds every ask of its format, so none is dropped without a word: each is named
#: with one of these reasons (:func:`unformed`), and ``--strict`` refuses a request that
#: holds one. A public profile forms a table's date pairs and calendar and a relationship's dates and
#: exposure (:mod:`fluid_build.profiler.dates`) and names the ones it cannot with a reason of
#: their own; a private release forms none of them, and names each with the reasons below.
NO_DATE_PAIRS = "a private release forms no date pair"
NO_CALENDAR = "a private release forms no calendar"
NO_WORKLOAD = (
    "a workload weighs the column sets a private release may join, and this profile joins none"
)
NO_DATES = "a private release forms no relationship dates"
NO_EXPOSURE = "a private release forms no exposure"
NO_QUERIES_PRIVATE = (
    "a private release publishes no query rate: PROFILE-SPEC refuses noise on a query statistic"
)


#: The reasons only the records can give, which ``--strict`` therefore never refuses on.
_BY_RECORDS = frozenset(
    {
        _NO_RECORD,  # a column or a calendar with no non-null record
        _NO_RECORD_FORMS,  # the statistic is declared and no record forms a group of it
        _WITHHELD_ROW_COUNT,  # the row count's own output check
        *query_rates.RECORD_REASONS,  # a query's values, statements and rows are the records'
        _TIMESTAMP_LADDER,  # whether a timestamp's date span is published is read from its records
        _TIMESTAMP_NEVER_BOTH,  # the same: the histogram sits on a span the records gave
        # a day bin of a date pair holds too few records to publish an outer edge
        dates._NO_DAY_HISTOGRAM,
        # an exposure's window is the parent start's published span
        dates._NO_WINDOW,
    }
)


def decided_by_records(reason: str) -> bool:
    """Whether *reason* for an ask left out is one only the records can give.

    ``--strict`` refuses an ask before any record is read when the contract and the request
    show it cannot be formed; an ask whose reason this says the records decide is never
    refused, and is named under Not measured once they are read. A reason is one of the
    exact texts above, a value that is not a whole day or that the reader refuses, or a key
    that names more than one parent.
    """
    return (
        reason in _BY_RECORDS
        or (reason.startswith("a value of ") and reason.endswith(("is not a whole day", "reader")))
        or "names more than one parent" in reason
    )


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


def _distinct_asks(
    links: Sequence[RequestRelationship], *picks: Callable[[RequestRelationship], Sequence[Any]]
) -> List[List[Any]]:
    """What each of *picks* takes from the links, each value once, in the order listed."""
    found: List[List[Any]] = [[] for _ in picks]
    for link in links:
        for held, pick in zip(found, picks, strict=True):
            held.extend(value for value in pick(link) if value not in held)
    return found


def relationship_asks(
    request: Request, relationship: "runner.Relationship"
) -> Tuple[bool, Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
    """What the request asks of one declared relationship: ``(counted, group_by, linked)``.

    *counted* says whether it lists ``marginalHistogram/1`` for the relationship, which is
    what children per parent is answered with; *group_by* are the parent columns its
    statistics are asked grouped by and *linked* the ``(child column, parent column)`` pairs
    asked, each once, in the order listed.
    """
    links = _links_of(request, relationship)
    counted = any("marginalHistogram/1" in each.statistics for each in links)
    group_by, linked = _distinct_asks(
        links,
        lambda each: each.group_by,
        lambda each: [(item.column, item.parent_column) for item in each.linked],
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
    dates, exposure = _distinct_asks(
        _links_of(request, relationship),
        lambda each: [(item.parent, item.child) for item in each.dates],
        lambda each: [(item.start, item.end) for item in each.exposure],
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

    Decided from the request alone, before any record is read: a workload entry, and, for a
    *private* release only, a consumer query, a table's date pairs and calendar and a
    relationship's dates and exposure (a public profile forms those, and names one it cannot
    with the reason it has, once it has read the records; which queries it answers needs the
    contract, :func:`fluid_build.profiler.query_rates.plan_queries`). *workload* is the reason a workload
    entry is not used, which a private release words its own way. Never a value: only tables,
    columns, names and reasons.
    """
    found: List[Tuple[str, str]] = []
    for table in request.tables:
        for pair in table.date_pairs if private else ():
            found.append(
                (f"date pair {pair.start} to {pair.end} of {table.expose_id}", NO_DATE_PAIRS)
            )
        for column in table.calendar if private else ():
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
    for query in request.queries if private else ():
        found.append((f"query {query.name}", NO_QUERIES_PRIVATE))
    return found


def refuse_unformed(request: Request, *, private: bool = False) -> None:
    """``--strict``: refuse *request* when it asks for anything :func:`unformed` names.

    A public profile forms date pairs, dates and exposure, so this leaves them: the contract's
    reasons for one it cannot form are told, with every other, by the profile before any record
    is read (:func:`refuse_asks`, from :func:`fluid_build.profiler.runner.profile_exposes`),
    and the records' reasons are named under Not measured.

    Raised before any record is read, so the refusal says nothing about the records. The
    message counts the asks and its context names each, grouped by its reason.
    """
    found = unformed(request, private=private)
    if found:
        raise refuse_asks(found)


def refuse_asks(found: Sequence[Tuple[str, str]]) -> ProfileError:
    """The ``--strict`` refusal naming each ask of *found*, ``(what, reason)``, by reason."""
    by_reason: Dict[str, List[str]] = {}
    for what, reason in found:
        by_reason.setdefault(reason, []).append(what)
    return ProfileError(
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
    metric_domains: Mapping[str, FrozenSet[str]],
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
        found.extend(_table_not_measured(table, tables, defaults, uncovered, keys, metric_domains))
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
    metric_domains: Mapping[str, FrozenSet[str]],
) -> List[Tuple[str, str]]:
    """Each item a request lists of one table that the profile does not hold, with why."""
    found: List[Tuple[str, str]] = []
    document = tables.get(table.expose_id, {})
    if table.row_count and "rowCount" not in document:
        found.append((f"rowCount of {table.expose_id}", _WITHHELD_ROW_COUNT))
    for column in table.columns:
        if column.name not in document.get("columns", {}):
            continue  # not read at all: the report names it, with why, as not profiled
        entry = document["columns"][column.name]
        for name in column.metrics:
            if name in entry.get("metrics", {}):
                continue
            reason = _metric_reason(name, column.domain, metric_domains)
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


def _metric_reason(name: str, domain: str, metric_domains: Mapping[str, FrozenSet[str]]) -> str:
    """Why a listed metric of a column is not in the profile."""
    if name not in metric_domains:
        return "fluid profile has no output check for it"
    if domain not in metric_domains[name]:
        return f"{name} is not formed for a {domain} column"
    return _NO_RECORD


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


def _binned_why(identifier: str, domain: str, default_ids: Set[str]) -> str:
    """Why a histogram or quantiles of a column are not in the profile: it gets the other."""
    other = "quantile/2" if identifier == "marginalHistogram/1" else "marginalHistogram/1"
    if other not in default_ids:
        return "it has neither a declared range nor a type that bounds it"
    if identifier == "marginalHistogram/1":
        if domain == "timestamp":
            return _TIMESTAMP_LADDER
        return "it has no declared range, so it gets quantile/2 on its type's ladder"
    if domain == "timestamp":
        return _TIMESTAMP_NEVER_BOTH
    return "a column gets a histogram or quantiles, never both on one set of bins"


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
        return _NO_RECORD_FORMS
    if column in uncovered:
        return uncovered[column]
    if identifier in ("marginalHistogram/1", "quantile/2"):
        return _binned_why(identifier, domain, default_ids)
    if identifier == "support/1":
        if "levelHistogram/1" in default_ids:
            return "beside a level histogram it would show which withheld cells hold no record"
        return "its levels are not declared by an enum(...) type"
    if identifier == "levelHistogram/1":
        return "its type declares no levels, as an enum(...) or a boolean does"
    if identifier == "quantile/1":
        return "a quantile without bins is not formed, quantile/2 is"
    return "fluid profile forms no such statistic"


def contract_unformed(
    request: Request,
    *,
    plans: Mapping[str, "runner.ExposePlan"],
    defaults: Mapping[str, Optional[DeclaredBlock]],
    uncovered: Mapping[str, Mapping[str, str]],
    keys: Mapping[str, FrozenSet[str]],
    no_mean: Mapping[str, FrozenSet[str]],
    matched: Set[int],
    unread: Mapping[int, str],
    metric_domains: Mapping[str, FrozenSet[str]],
    queries: Optional["query_rates.QueryPlans"] = None,
) -> List[Tuple[str, str]]:
    """Each ask of a table, relationship or query the contract rules out, before any record is read.

    The asks :func:`not_measured` names once the records are read, less those whose reason the
    records decide (:func:`decided_by_records`): *plans* are the tables as the request restricts
    them, *defaults* each table's default block before the request's filter, *uncovered* the
    columns it leaves out with why, *keys* the key columns of the relationships the request
    asks of, and *no_mean* each table's number columns that take no ``mean/1`` of their own
    (:func:`fluid_build.profiler.runner._no_fixed_mean`). *matched* are the listed relationships
    that name a declared one and *unread* those of them the profile does not read, with why. A
    listed column the profile does not read at all has every ask named, with the reason it is
    not profiled. A consumer query is named with the reason :func:`query_rates.plan_queries`
    gives it (*queries*): the check, the shape rules and the public bounds decide it from the
    request and the contract alone.
    """
    found: List[Tuple[str, str]] = []
    if queries is not None:
        found.extend(queries.unanswered())
    for table in request.tables:
        found.extend(
            _table_unformed(
                table,
                plans[table.expose_id],
                defaults.get(table.expose_id),
                uncovered.get(table.expose_id, {}),
                keys.get(table.expose_id, frozenset()),
                no_mean.get(table.expose_id, frozenset()),
                metric_domains,
            )
        )
    for position, link in enumerate(request.relationships):
        if position not in matched:
            found.extend(_undeclared_not_measured(link))
            continue
        found.extend(_link_not_measured(link, declared=True))
        if position in unread and ("marginalHistogram/1" in link.statistics or link.linked):
            found.append((f"children per parent of {_spelled(link)}", unread[position]))
    return found


def _table_unformed(
    table: RequestTable,
    plan: "runner.ExposePlan",
    default_block: Optional[DeclaredBlock],
    uncovered: Mapping[str, str],
    keys: FrozenSet[str],
    no_mean: FrozenSet[str],
    metric_domains: Mapping[str, FrozenSet[str]],
) -> List[Tuple[str, str]]:
    """Each ask of one table's columns the contract rules out, with why."""
    found: List[Tuple[str, str]] = []
    domains = {column.name: column.domain for column in plan.columns}
    skipped = dict(plan.skipped)
    default_ids_of: Dict[str, Set[str]] = {}
    for each in default_block.statistics if default_block else ():
        if each.group_by is None:
            default_ids_of.setdefault(each.column, set()).add(each.identifier)
    for column in table.columns:
        if column.name not in domains:
            # The profile does not read the column at all: nothing asked of it is formed.
            why = skipped.get(column.name, "it is a column fluid profile does not read")
            found.extend(
                (f"{name} of {table.expose_id}.{column.name}", why)
                for name in (*column.metrics, *column.statistics)
            )
            continue
        domain = domains[column.name]
        for name in column.metrics:
            reason = _metric_reason(name, domain, metric_domains)
            if not decided_by_records(reason):
                found.append((f"{name} of {table.expose_id}.{column.name}", reason))
        default_ids = default_ids_of.get(column.name, set())
        for identifier in column.statistics:
            if identifier in default_ids:
                continue  # the profile forms it: the records say whether a group is left
            if identifier == "mean/1" and domain == "number" and column.name not in no_mean:
                continue  # the fixed mean is formed, unless the column has no record
            reason = _why(identifier, domain, column.name, default_ids, uncovered, keys)
            if not decided_by_records(reason):
                found.append((f"{identifier} of {table.expose_id}.{column.name}", reason))
    return found
