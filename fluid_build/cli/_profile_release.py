# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The private release of ``fluid profile``: its flags, their checks, the dispatch and the report.

``fluid profile CONTRACT --request REQ --source ... --privacy-unit U --rho R --release-record
PATH`` releases one table's requested statistics under differential privacy
(:mod:`fluid_build.profiler.dp.release`). This module is the command-line side of it:
:func:`add_flags` adds the flags to the parser, :func:`wanted` says whether a call asks for a
release, :func:`run` checks the flags, plans the release before any record is read, reads the
table once, draws the release, writes the record and then the profile, and reports.

Only the standard library is imported at module scope, so ``fluid --help`` loads nothing
heavy. Nothing printed, logged or written outside the noise depends on the records: the
report names the plan (unit, bounds, privacy loss and its split), the tables and their
scales, the cliques chosen, what is not released and why, and the files; never a count of
rows, entities, dropped rows, distinct values or nulls, and never the seed.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["HELP", "add_flags", "run", "wanted"]

#: The release's flags, by their ``args`` name: a call that sets any of them asks for one.
_FLAGS = (
    "privacy_unit",
    "release_table",
    "unit_key",
    "rho",
    "max_rows_per_unit",
    "max_rows_per_cell",
    "release_record",
    "rederive",
    "clique_method",
    "follow",
)

HELP = """\
With --request FILE, --privacy-unit EXPOSE, --rho R and --release-record PATH the profile
is a release under differential privacy: the noisy counts of one table, and the noisy rank
correlations and date pairs the request asks for, in profile format 1-2-0 (1-3-0 when it holds
a date pair, 1-4-0 when it holds a rank correlation, a clique with a binned column, a clique
selection or a unit that follows its child rows), which flux-engine grades
P3. Nothing else of the request is released.

What it releases, from the release table (the privacy unit's expose, or with
--release-table a child of it through one declared relationship, or with --unit-key the rows
of the unit's expose that name one entity by COL[,COL]):
  - a noisy levelHistogram/1 of each enum column the request lists;
  - a noisy marginalHistogram/1 of each number, date or timestamp column the request
    lists, on the bins the contract's declared range gives (else the type's ladder);
  - for a column the contract does not declare required, the same histogram over the column's
    known values: a record with no value lands in no cell, the column has a share of its own and
    joins no clique, and no null count or share of nulls is released;
  - for each date pair of two DATE columns the request lists (a table the release holds), a
    noisy histogram of the days from start to end on the whole public ladder of days, never
    trimmed, and a noisy order (after, before, same), both over the records where both dates
    are known, each with a share of its own;
  - noisy cliqueMarginal/1 tables over categorical columns, the cliques chosen with AIM or,
    with --clique-method mst, MST (OpenDP, from the [privacy] extra) when two or more columns
    may join them. A request that lists cliqueColumns lets those released columns join, a
    number or timestamp column by its public bins: such a clique is a cliqueMarginal/2, and
    the table states its cliqueSelection (method, producer and the share spent choosing);
  - a noisy kendallTau/1 or spearmanRho/1 of each pair of released number or timestamp
    columns a rankCorrelations entry of the request lists (columns the contract declares
    required), each value formed with the public
    row floor --row-floor N (default 1000, a count of 2 or more, written as its rowFloor).
The levels and bins are public: they come from the contract, never from the records. A key
column and a column with no public levels or bins are not released, a column that may hold a
null takes part in no clique and no rank correlation, and a date pair with a timestamp or a key
column is not formed; neither is a row count, a column metric, a quantile, a mean, a
relationship or a fact, nor a calendar or a relationship's dates or exposure. The report names
each requested item that is not released, with the reason.

What it guarantees: zero-concentrated differential privacy with total privacy loss rho = R,
for one release of one table. A tenth of R chooses the cliques when two or more columns
may join them; the rest is split equally over the parts, each count table and each rank
correlation one part. A count table gets exact discrete Gaussian noise whose scale is the
least one that spends no more than its share on the change of one entity: its sensitivity
to one row is 1. A rank correlation's value moves by at most 4/(f+1) (Kendall) or 9/(f+1)
(Spearman) when one record is added or removed, f being the row floor, so by at most that
times the rows an entity contributes when an entity does. The value is floored onto a public
lattice of 2^20 steps per sensitivity, an exact discrete Gaussian is added on the lattice
(Canonne, Kamath and Steinke 2020, so no floating-point noise leaks, Mironov 2012), and the
sum is written to 12 decimals; the profile states it as a gaussian of the scale, whose variance
bounds the discrete one's. The report names each part's sensitivity, scale and share, and adds
an informational conversion to (epsilon, delta) at delta = 1e-6.

A unit that follows its child rows: with --follow PARENT.COL[,COL]=CHILD.COL[,COL]:MAX (repeat
it for each relationship; each names a relationship the contract declares, MAX is a public cap
of at least 1) the privacy unit is one row of the --privacy-unit expose together with the rows
that follow it: at most MAX rows of the child table for each parent row, and so on down the
tree the entries form, which must be rooted at the expose (a table has one path into it, no
cycle, the expose is never a child). The release then holds the unit's expose and every table
it reaches, in format 1-4-0: each table's histograms, cliques and rank correlations with the
sensitivity of the rows one unit contributes to it (m_T, the product of the caps down to the
table); each followed relationship's noisy children per parent in the bins 0 to its cap, and a
relationship asked by the request in a fixed public ladder when its child is reached and its
parent is not (or the reverse), grouped by the parent columns the request names (a category by
its levels, a number or timestamp by public bins); and the child columns the request asks to be
grouped through a relationship (groupByLink) by the parent column's levels, when the unit
follows the relationship or does not reach the parent. Before anything is measured, at most MAX
children of each parent row are kept by a keyed priority that reads only that parent's own
children, so what a unit keeps never depends on another unit or on the order of the records;
children beyond the cap and rows that follow no kept row are dropped. A table the unit does not
reach is treated as fixed: its rows are the same in every neighbouring data set, so a statistic
grouped by its column protects the unit's rows and not that table's, and a labelled column of
it (pii, restricted, ...) that groups a noisy statistic draws a warning, since the engine
counts such a column as P1. --follow cannot be used with --release-table, --unit-key or
--max-rows-per-unit; each followed parent key must name one row, except that a followed
relationship out of a table with a validity window by its natural entity repeats its key by
design, and its data precondition is then that an entity's versions follow one parent row (a
release whose records break it is refused as profile_release_entity_units); and every table the
release reads needs a --source <exposeId>=<location>.

For which unit: one entity is one row of the unit's expose; with a release table related to
it, or with a unit key, an entity owns every row that belongs to it, and the stated loss holds
for each entity. Each entity contributes at most --max-rows-per-unit N rows of the release
table (default 1) and counts at most --max-rows-per-cell K rows in one cell of a table
(default N), so the stated loss holds for each entity and for each row of it. Which rows an
entity keeps is decided by a keyed priority, never by the other entities. Rows with no key
value (a null in a key column) are taken together as one entity, bounded the same way: the
stated loss holds for that entity as a whole and for each of its rows. An entity that holds
only some of those rows, or rows both with and without a key value, is covered at a larger
loss: by group privacy at most 4 times rho, or 9 times rho when it holds rows of both kinds.
With the release table equal to the privacy unit and no --unit-key, each row is an entity,
nothing is dropped and N and K are not accepted; a primary key the contract declares must
then be unique, and a null in it refuses the release.

What the guarantee rests on: the noise is pseudorandom, drawn from HMAC-SHA256 keyed by the
recorded seed, so the guarantee holds as long as HMAC-SHA256 behaves as a pseudorandom
function. Choosing the cliques uses OpenDP's AIM, which OpenDP ships behind its contrib
flag (with --clique-method mst, its MST, shipped the same way). The guarantee covers data that meets the contract (required columns hold a value,
values lie in their declared levels and ranges, and in rows mode the primary key is
unique); data that does not is refused, releases nothing, and the refusal is for the data
owner (it names a column and a row): a refusal is not covered by the guarantee.

What it does not do: it states nothing about a second release of the same records, which
spends its own loss; it does not make the contract, the request or the levels and bins
private, since they are public; and it is not a statement that the data is anonymous. A
record with a null in a column the contract declares required, or a value outside its levels
or bins, or a date that is no whole day in a date pair, refuses the release, naming the column
and the row and never the value. The report says no count of
rows, entities or dropped rows. A table that has relationships cannot ship on flux-engine
0.9 until it skips a table the release holds no statistic for.

Re-derivation: the noise is drawn from a seeded randomness source recorded inside the data
owner's boundary, so the release can be re-derived there. --release-record PATH is created
exclusively, readable by you alone, and must not exist; --rederive with the existing
record draws the same release again, takes the privacy loss, the clique method, the row floor
and the rest from the record,
and writes the profile only when its digest equals the recorded one. Keep the record inside the data
owner's boundary: with it and the profile the noise can be taken off. Never reuse a record
for another release.
"""

_EXAMPLE = """\

# a release under differential privacy, rho 1: each entity (a customer_id) gives at most 3 rows
fluid profile orders.fluid.yaml --request orders.profile-request.yml \\
    --source orders=./orders.csv --privacy-unit orders --unit-key customer_id \\
    --max-rows-per-unit 3 --rho 1 --release-record orders.release-record.json
# the same release again, inside the data owner's boundary
fluid profile orders.fluid.yaml --request orders.profile-request.yml \\
    --source orders=./orders.csv --release-record orders.release-record.json --rederive

# a unit that follows its child rows: an account with up to 3 invoices, each with up to 2 lines
fluid profile billing.fluid.yaml --request billing.profile-request.yml \\
    --source account=./account.csv --source invoice=./invoice.csv --source line=./line.csv \\
    --privacy-unit account --follow account.id=invoice.account_id:3 \\
    --follow invoice.invoice_id=line.invoice_id:2 --rho 1 --release-record billing.record.json
"""


def _columns(text: str) -> Tuple[str, ...]:
    names = tuple(part.strip() for part in text.split(","))
    if not names or not all(names) or len(set(names)) != len(names):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a list of distinct column names written COL[,COL]"
        )
    return names


def _follow(text: str) -> Any:
    from fluid_build.profiler.dp.follows import parse_follow

    try:
        return parse_follow(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _whole(text: str) -> int:
    try:
        return int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None


def add_flags(parser: "argparse.ArgumentParser") -> None:
    """Add the private release's flags and its help section to ``fluid profile``."""
    group = parser.add_argument_group(
        "private release (differential privacy)",
        description=HELP,
    )
    group.add_argument(
        "--privacy-unit",
        metavar="EXPOSE",
        help=(
            "the expose whose one row is one entity (with --release-table or --unit-key, an "
            "entity owns every row that belongs to it, and the stated loss holds for each "
            "entity); needs --request, --rho and --release-record"
        ),
    )
    group.add_argument(
        "--release-table",
        metavar="EXPOSE",
        help=(
            "the table whose statistics are released (default: the privacy unit); a child of "
            "the unit through one declared relationship"
        ),
    )
    group.add_argument(
        "--unit-key",
        type=_columns,
        metavar="COL[,COL]",
        help=(
            "the columns of the privacy unit's expose that name an entity, when the release "
            "table is the unit itself and an entity holds several rows"
        ),
    )
    group.add_argument(
        "--follow",
        action="append",
        type=_follow,
        metavar="PARENT.COL[,COL]=CHILD.COL[,COL]:MAX",
        help=(
            "the privacy unit follows its child rows: keep at most MAX children of each parent "
            "row through this declared relationship, and release the unit's expose and every "
            "table it reaches; repeat it for each relationship of the tree rooted at "
            "--privacy-unit. Not with --release-table, --unit-key or --max-rows-per-unit"
        ),
    )
    group.add_argument(
        "--rho",
        metavar="R",
        help=(
            "the total privacy loss, zero-concentrated: decimal text above zero, at most 100 "
            "digits, no exponent (a new release)"
        ),
    )
    group.add_argument(
        "--max-rows-per-unit",
        type=_whole,
        metavar="N",
        help="the most rows of the release table one entity contributes (default 1)",
    )
    group.add_argument(
        "--max-rows-per-cell",
        type=_whole,
        metavar="K",
        help="the most rows one entity counts in one cell of a table (default N)",
    )
    group.add_argument(
        "--clique-method",
        choices=("aim", "mst"),
        metavar="aim|mst",
        help=(
            "how the cliques are chosen when two or more columns may join them: aim (the "
            "default) or mst, both OpenDP's (from the [privacy] extra); a new release"
        ),
    )
    group.add_argument(
        "--release-record",
        metavar="PATH",
        help=(
            "the owner-only record of the release, which must not exist (a new release), or "
            "the existing one to re-derive from (--rederive); keep it inside the data owner's "
            "boundary"
        ),
    )
    group.add_argument(
        "--rederive",
        action="store_true",
        help=(
            "draw the release again from --release-record, which gives the privacy loss and "
            "the rest, and write the profile only when its digest is the recorded one"
        ),
    )
    parser.epilog = (parser.epilog or "") + _EXAMPLE


def wanted(args: argparse.Namespace) -> bool:
    """Whether the call asks for a private release: any of its flags is set."""
    values = (getattr(args, name, None) for name in _FLAGS)
    return any(value is not None and value is not False for value in values)


def _refusal(event: str, detail: str, **context: str) -> Any:
    from fluid_build.cli.profile_cmd import _refusal as refusal

    return refusal(event, detail, {"read": "nothing: no record was read", **context})


def check_flags(args: argparse.Namespace) -> None:
    """Refuse a flag that cannot stand alone, before anything is read."""
    if getattr(args, "request", None) is None:
        raise _refusal(
            "profile_release_needs_request",
            "a private release answers a profile request, and --request names none",
            next="write the request with `fluxsim init --contract <contract>` and name it",
        )
    if args.release_record is None:
        raise _refusal(
            "profile_release_needs_record",
            "a private release writes a release record, and --release-record names none",
            next="name a file for the record: it is created, owner-only, and must not exist",
        )
    if getattr(args, "follow", None):
        clashes = [
            flag
            for flag, value in (
                ("--release-table", args.release_table),
                ("--unit-key", args.unit_key),
                ("--max-rows-per-unit", args.max_rows_per_unit),
                ("--max-rows-per-cell", args.max_rows_per_cell),
            )
            if value is not None
        ]
        if clashes:
            raise _refusal(
                "profile_release_follow_conflict",
                f"--follow makes the unit one row with the child rows that follow it, and "
                f"{clashes[0]} says the rows it contributes in another way",
                next=f"leave out {', '.join(clashes)}, or leave out --follow",
            )
    if args.rederive:
        return
    if args.privacy_unit is None:
        raise _refusal(
            "profile_release_needs_unit",
            "a private release needs --privacy-unit, the expose whose one row is one entity",
            next="name the expose of the entities the loss is stated for",
        )
    if args.rho is None:
        raise _refusal(
            "profile_release_needs_rho",
            "a new private release needs --rho, its total privacy loss",
            next="give the loss as decimal text above zero, for example --rho 1",
        )


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists():
            return a.samefile(b)
    except OSError:
        # A path that cannot be examined (no permission, say) is compared by its resolved name
        # below instead, so a release record named as the profile or a source is still refused.
        pass
    return a.resolve() == b.resolve()


def _absolute(given: str) -> Path:
    target = Path(given)
    return target if target.is_absolute() else Path.cwd() / target


def _check_record_path(given: str, out: Path, locations: Sequence[str], new: bool) -> Path:
    """The record's path, checked as ``--out`` is: no link, not the profile, not a source.

    A new release needs a path that does not exist; a re-derivation one that does.
    """
    from fluid_build.profiler.sources import local_file

    target = _absolute(given)
    if target.is_symlink():
        raise _refusal(
            "profile_release_record_is_a_link",
            f"{target} is a symbolic link",
            next="name a file that is not a link",
        )
    if _same_file(target, out):
        raise _refusal(
            "profile_release_record_is_the_profile",
            f"{target} is the file the profile is written to",
            next="name another file for the release record",
        )
    for location in locations:
        read_from = local_file(location)
        if read_from is not None and read_from.exists() and _same_file(target, read_from):
            raise _refusal(
                "profile_release_record_is_the_source",
                f"{target} is the file the records are read from",
                next="name another file for the release record",
            )
    if new and os.path.lexists(target):
        raise _refusal(
            "profile_record_exists",
            f"{target} already exists: a seed is never used for a second release",
            next="name a new file for the release record",
        )
    return target


def _read_request(path: Path, backend: Any) -> Tuple[bytes, Any]:
    """The request's bytes, read once, and the request as flux-stats reads them."""
    from fluid_build.profiler import _stats
    from fluid_build.profiler import request as asked_for

    again = "write the request again with `fluxsim init --contract <contract>`"
    data = asked_for._read_regular(
        path,
        limit=asked_for.MOST_REQUEST_BYTES,
        event="profile_request_unreadable",
        what="the request",
        next_step=again,
    )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise asked_for._refuse(
            "profile_request_unreadable", f"the request {path.name} is not UTF-8 text", again
        ) from None
    try:
        return data, backend.read_request(text, name=path.name)
    except _stats.RequestRefused as exc:
        raise asked_for._refuse("profile_request_refused", f"{path.name}: {exc}", again) from None


def _options_from_record(options: Any) -> Dict[str, Any]:
    """The clique method and the row floor a record states, read strictly."""
    from fluid_build.profiler.dp import release, selection

    if not isinstance(options, dict) or not set(options) <= {"cliqueMethod", "rowFloor"}:
        raise release.ReleaseRefused(
            "profile_release_record_mismatch",
            "the record's options are not ones this release writes",
        )
    method, floor = options.get("cliqueMethod", "aim"), options.get("rowFloor")
    if method not in selection.METHODS or (
        floor is not None and (isinstance(floor, bool) or not isinstance(floor, int) or floor < 2)
    ):
        raise release.ReleaseRefused(
            "profile_release_record_mismatch",
            "the record's options are not ones this release writes",
        )
    return {"clique_method": method, "row_floor": floor}


def _unit_from_record(unit: Any) -> Dict[str, Any]:
    """The plan inputs a record states, read strictly: a record is not trusted."""
    from fluid_build.profiler.dp.release import ReleaseRefused

    if not isinstance(unit, dict):
        raise ReleaseRefused(
            "profile_release_record_mismatch", "the record's unit is not an object"
        )
    if unit.get("mode") == "follows":
        return _follows_from_record(unit)
    expose, table = unit.get("expose"), unit.get("releaseTable")
    keys, mode = unit.get("keyColumns"), unit.get("mode")
    c, k = unit.get("c"), unit.get("k")
    if (
        not isinstance(expose, str)
        or not isinstance(table, str)
        or mode not in ("rows", "keyed", "related")
        or not isinstance(keys, list)
        or not all(isinstance(each, str) for each in keys)
        or isinstance(c, bool)
        or isinstance(k, bool)
        or not isinstance(c, int)
        or not isinstance(k, int)
    ):
        raise ReleaseRefused(
            "profile_release_record_mismatch", "the record's unit is not one this release writes"
        )
    return {
        "unit": expose,
        "release_table": table,
        "unit_key": tuple(keys) if mode == "keyed" else None,
        "max_rows_per_unit": None if mode == "rows" else c,
        "max_rows_per_cell": None if mode == "rows" else k,
        "follows": (),
    }


def _follows_from_record(unit: Dict[str, Any]) -> Dict[str, Any]:
    """The unit of a record that follows its child rows: the expose and each followed entry."""
    from fluid_build.profiler.dp.follows import FollowSpec
    from fluid_build.profiler.dp.release import ReleaseRefused

    listed = unit.get("follows")
    expose = unit.get("expose")
    bad = ReleaseRefused(
        "profile_release_record_mismatch", "the record's unit is not one this release writes"
    )
    if not isinstance(expose, str) or not isinstance(listed, list) or not listed:
        raise bad
    found = []
    for entry in listed:
        if not isinstance(entry, dict):
            raise bad
        ends = []
        for side in ("parent", "child"):
            end = entry.get(side)
            if (
                not isinstance(end, dict)
                or not isinstance(end.get("table"), str)
                or not isinstance(end.get("columns"), list)
                or not all(isinstance(name, str) for name in end["columns"])
            ):
                raise bad
            ends.append((end["table"], tuple(end["columns"])))
        cap = entry.get("maxChildren")
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise bad
        found.append(FollowSpec(ends[0][0], ends[0][1], ends[1][0], ends[1][1], cap))
    return {
        "unit": expose,
        "release_table": None,
        "unit_key": None,
        "max_rows_per_unit": None,
        "max_rows_per_cell": None,
        "follows": tuple(found),
    }


def _disagreements(args: argparse.Namespace, stated: Dict[str, Any], rho: Any) -> None:
    """Refuse a flag that says something other than the record does."""
    from fluid_build.profiler.dp import release

    given = {
        "--privacy-unit": (args.privacy_unit, stated["unit"]),
        "--release-table": (args.release_table, stated["release_table"]),
        "--unit-key": (args.unit_key, stated["unit_key"]),
        "--max-rows-per-unit": (args.max_rows_per_unit, stated["max_rows_per_unit"]),
        "--max-rows-per-cell": (args.max_rows_per_cell, stated["max_rows_per_cell"]),
        "--row-floor": (getattr(args, "row_floor", None), stated["row_floor"]),
        "--clique-method": (args.clique_method, stated["clique_method"]),
        "--follow": (
            None if not getattr(args, "follow", None) else tuple(args.follow),
            stated["follows"],
        ),
    }
    for flag, (value, recorded) in given.items():
        if value is not None and value != recorded:
            raise _refusal(
                "profile_release_rederive_disagrees",
                f"{flag} differs from the release record, which holds the release's own",
                next=f"leave {flag} out: --rederive takes it from the record",
            )
    if args.rho is not None and release.parse_rho(args.rho) != rho:
        raise _refusal(
            "profile_release_rederive_disagrees",
            "--rho differs from the release record, which holds the release's own",
            next="leave --rho out: --rederive takes it from the record",
        )


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Release the table under differential privacy and write the profile; return the exit code."""
    from fluid_build._console import cprint_literal
    from fluid_build._contract_loader import load_contract_with_overlay
    from fluid_build.cli import _profile_follow, profile_cmd
    from fluid_build.cli.console import warning as console_warning
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.profiler import _stats, runner, sources
    from fluid_build.profiler import request as asked_for
    from fluid_build.profiler.document import DocumentError
    from fluid_build.profiler.dp import bounding, follow_release, randomness, release, selection
    from fluid_build.profiler.dp import record as record_module

    check_flags(args)
    refused = (
        runner.ProfileError,
        sources.SourceError,
        _stats.ReadRefused,
        _stats.StatisticsUnavailable,
        DocumentError,
        _stats.DocumentRefused,
    )
    try:
        asked_for.check_flags(bundle=args.bundle, env=args.env, exposes=args.expose_id)
        backend = _stats.load_backend()
    except refused as exc:
        raise profile_cmd._as_refusal(exc, args.source) from None
    contract_path = validate_cli_path(args.contract, mode="read", file_type="contract")
    rederive = bool(args.rederive)
    try:
        request_bytes, asked = _read_request(
            validate_cli_path(args.request, mode="read", file_type="request"), backend
        )
        digest_matched = asked_for.check_contract_file(contract_path, asked, backend)
        if getattr(args, "strict", False):
            asked_for.refuse_unformed(asked, private=True)
        if args.fractional_bits is not None and args.fractional_bits != asked.fractional_bits:
            raise runner.ProfileError(
                "profile_request_fractional_bits_conflict",
                f"--fractional-bits {args.fractional_bits} differs from the request's "
                f"encoding.fractionalBits {asked.fractional_bits}",
                {
                    "read": "nothing: no record was read",
                    "next": "drop --fractional-bits: the request's grid is the profile's",
                },
            )
        recorded = None
        if rederive:
            if not os.path.lexists(_absolute(args.release_record)):
                raise _refusal(
                    "profile_release_record_missing",
                    f"{_absolute(args.release_record)} does not exist, and --rederive draws the "
                    "release again from it",
                    next="name the release record the release wrote",
                )
            record_given = validate_cli_path(args.release_record, mode="read", file_type="record")
            recorded = record_module.read_record(record_given)
            if (
                recorded.request_digest != _sha256(request_bytes)
                or recorded.contract_digest != asked.contract_digest
            ):
                raise release.ReleaseRefused(
                    "profile_release_record_mismatch",
                    "the release record was made from another request or contract",
                    {"next": "name the request and the contract the release was made from"},
                )
            stated = {
                **_unit_from_record(recorded.unit),
                **_options_from_record(dict(recorded.options)),
            }
            rho = release.parse_rho(str(recorded.privacy_loss.get("rho")))
            _disagreements(args, stated, rho)
        else:
            stated = {
                "unit": args.privacy_unit,
                "release_table": args.release_table,
                "unit_key": args.unit_key,
                "max_rows_per_unit": args.max_rows_per_unit,
                "max_rows_per_cell": args.max_rows_per_cell,
                "row_floor": getattr(args, "row_floor", None),
                "clique_method": args.clique_method or "aim",
                "follows": tuple(args.follow or ()),
            }
            rho = release.parse_rho(args.rho)
        contract = load_contract_with_overlay(str(contract_path), args.env, logger)
        follows = stated["follows"]
        plan: Any
        if follows:
            plan = follow_release.plan_follow_release(
                contract,
                asked,
                backend,
                unit=stated["unit"],
                rho=rho,
                follows=follows,
                row_floor=stated["row_floor"],
                clique_method=stated["clique_method"],
            )
            read_tables = list(plan.read_tables)
            named = plan.unit
        else:
            plan = release.plan_release(
                contract,
                asked,
                backend,
                unit=stated["unit"],
                rho=rho,
                release_table=stated["release_table"],
                unit_key=stated["unit_key"],
                max_rows_per_unit=stated["max_rows_per_unit"],
                max_rows_per_cell=stated["max_rows_per_cell"],
                row_floor=stated["row_floor"],
                clique_method=stated["clique_method"],
            )
            read_tables = [plan.table]
            named = plan.table
        if getattr(args, "strict", False):
            # a date pair the contract shows cannot be formed is a refusal before any record is read
            unformed = [item for item in plan.not_released if item[0].startswith("date pair ")]
            if unformed:
                raise asked_for.refuse_asks(unformed)
        _only_the_read_tables(args.source, contract, read_tables)
        exposes = profile_cmd._expose_sources(contract, read_tables, args.source, args.table)
        out = profile_cmd._out_path(args.out, named, [each.source for each in exposes])
        record_path = _check_record_path(
            args.release_record, out, [each.source for each in exposes], new=not rederive
        )
        notice = release.rho_warning(rho)
        if notice is not None:
            console_warning(notice)
        cliques: Any = None
        checker = follow_release if follows else release
        if recorded is not None:
            cliques = (
                follow_release.check_follow_record(plan, recorded)
                if follows
                else release.check_record(plan, recorded)
            )
            seed = recorded.seed
        else:
            checker.check_selection_available(plan)
            seed = randomness.new_seed()
        version = None if recorded is None else recorded.versions.get("opendp")
        drawn: Any
        if follows:
            wanted_columns = dict(plan.reads)
            reads = {
                each.expose_id: sources.read_columns(
                    each.source,
                    [column.name for column in wanted_columns[each.expose_id]],
                    table=each.table,
                    cwd=Path.cwd(),
                )
                for each in exposes
            }
            drawn = follow_release.draw_follow(
                plan,
                follow_release.encode_follow_rows(plan, reads, backend),
                seed,
                backend,
                cliques=cliques,
                selection_version=version,
            )
        else:
            read = sources.read_columns(
                exposes[0].source,
                [column.name for column in plan.read],
                table=exposes[0].table,
                cwd=Path.cwd(),
            )
            drawn = release.draw(
                plan,
                release.encode_rows(plan, read, backend),
                seed,
                backend,
                cliques=cliques,
                selection_version=version,
            )
        if recorded is not None:
            if drawn.digest != recorded.profile_digest:
                raise release.ReleaseRefused(
                    "profile_release_rederive_mismatch",
                    "the profile drawn again from the release record is not the recorded one: "
                    "the records, the request or the contract changed since the release",
                    {"next": "draw it again from the records the release was made from"},
                )
        else:
            validate_cli_path(out, mode="write", must_exist=False, file_type="profile")
            validate_cli_path(
                record_path, mode="write", must_exist=False, file_type="release record"
            )
            record_module.write_record(
                record_path,
                (follow_release.build_follow_record if follows else release.build_record)(
                    plan,
                    drawn,
                    seed,
                    request_bytes=request_bytes,
                    contract_digest=asked.contract_digest,
                ),
            )
        try:
            profile_cmd._write(out, drawn.canonical)
        except runner.ProfileError as exc:
            if not rederive:
                exc.context["release record"] = (
                    f"{record_path} was written; draw the profile again with --rederive"
                )
            raise
    except refused as exc:
        raise profile_cmd._as_refusal(exc, args.source) from None
    except bounding.UnitRefused as exc:
        raise profile_cmd._refusal(
            exc.event, str(exc), {"read": "nothing: no record was read"}
        ) from None
    except record_module.RecordRefused as exc:
        raise profile_cmd._refusal(exc.event, exc.message, {"written": "nothing"}) from None
    except selection.SelectionUnavailable as exc:
        raise profile_cmd._refusal(
            "profile_release_selection_unavailable",
            str(exc),
            {"read": "nothing: no record was read", "next": selection.PRIVACY_INSTALL_COMMAND},
        ) from None
    report = _profile_follow.report if follows else _report
    report(
        cprint_literal,
        plan,
        drawn,
        out,
        None if rederive else record_path,
        request=(Path(str(args.request)).name, digest_matched),
        rederived=rederive,
    )
    return 0


def _only_the_read_tables(given: Sequence[str], contract: Any, tables: Sequence[str]) -> None:
    """Refuse a ``--source`` for a table the release does not read.

    A release of one table reads that table only; one whose unit follows its child rows reads the
    tables it reaches and the others its statistics group by.
    """
    from fluid_build.profiler import runner
    from fluid_build.profiler.sources import describe_source

    ids = runner.expose_ids(contract)
    for value in given:
        head, sep, _ = value.partition("=")
        if sep and head in ids and head not in tables:
            raise runner.ProfileError(
                "profile_release_source_unused",
                (
                    f"--source names exposes[{head}], and a private release reads the release "
                    f"table exposes[{tables[0]}] only"
                    if len(tables) == 1
                    else f"--source names exposes[{head}], which this release does not read "
                    f"(it reads {', '.join(tables)})"
                ),
                {
                    "read": "nothing: no record was read",
                    "next": f"leave out --source {describe_source(value).partition('=')[0]}=...",
                },
            )


def _words(plan: Any) -> List[str]:
    """The privacy unit in plain words."""
    unit = plan.unit
    table = f"exposes[{unit.release_table}]"
    bound = f"at most {unit.max_rows_per_unit} rows of {table}"
    if unit.max_rows_per_cell != unit.max_rows_per_unit:
        bound += f", and at most {unit.max_rows_per_cell} in one cell of a table"
    if unit.mode == "rows":
        words = f"each row of {table} is one entity, and nothing is dropped"
    elif unit.mode == "keyed":
        words = (
            f"an entity is the rows of {table} that share {', '.join(unit.key_columns)}; "
            f"each entity contributes {bound} to the release"
        )
    else:
        words = (
            f"an entity is a row of exposes[{unit.unit_expose}]; the rows of {table} that belong "
            f"to it through {unit.relationship} ({', '.join(unit.key_columns)}) are kept to {bound}"
        )
    if unit.mode == "rows":
        return [f"Privacy unit: {words}, so the stated loss holds for each row."]
    return [
        f"Privacy unit: {words}, so the stated loss holds for each entity and for each row of it.",
        "Rows with no key value (a null in a key column) are taken together as one entity, "
        "bounded the same way: the stated loss holds for that entity as a whole and for "
        "each of its rows. An entity that holds only some of those rows, or rows both with "
        "and without a key value, is covered at a larger loss: by group privacy at most 4 "
        "times rho, or 9 times rho when it holds rows of both kinds.",
    ]


_KIND = {
    "clique": "clique of",
    "levels": "level histogram of",
    "bins": "histogram of",
    "rank": "rank correlation",
    "pairDays": "days of the date pair",
    "pairOrder": "order of the date pair",
}


def _report(
    say: Any,
    plan: Any,
    drawn: Any,
    out: Path,
    record_path: Optional[Path],
    *,
    request: Tuple[str, Optional[bool]],
    rederived: bool,
) -> None:
    """Tell the owner what was released, under which loss, and what was left out.

    Only what is public or part of the release is said: the plan, the budget, each table's
    scale and share, the cliques, the reasons and the files. Never a count of the records.
    """
    from fluid_build.cli import _profile_follow
    from fluid_build.profiler.dp import release

    say(
        f"Released exposes[{plan.table}] under differential privacy: noisy counts, discrete "
        f"Gaussian noise, answering the request {request[0]}"
        + (" (its contract digest is this contract's)." if request[1] else "."),
        soft_wrap=True,
    )
    for line in _words(plan):
        say(line, soft_wrap=True)
    say(
        f"The profile names exposes[{plan.table}] as its privacy unit, as the format does; the "
        "loss holds for a row of it and, as above, for an entity.",
        soft_wrap=True,
    )
    total = release.decimal_text(plan.rho)
    ranked = len(drawn.rank_tables)
    measured = f"{len(drawn.count_tables)} tables"
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
        "values lie in their declared levels and ranges, and in rows mode the primary key is "
        "unique); data that does not is refused, releases nothing, and the refusal is for the "
        "data owner (it names a column and a row): a refusal is not covered by the guarantee."
    )
    say(rests, soft_wrap=True)
    for table in drawn.tables:
        if table.kind == "rank":
            first, second = table.columns
            say(
                f"  {_KIND[table.kind]} {table.definition} of {first} and {second}: noise scale "
                f"{table.scale}, share rho {table.share}, row floor {table.row_floor}, "
                f"sensitivity {table.delta} for one entity",
                soft_wrap=True,
            )
            continue
        shown = (
            " to ".join(table.columns)
            if table.kind in ("pairDays", "pairOrder")
            else ", ".join(table.columns)
        )
        say(
            f"  {_KIND[table.kind]} {shown}: noise scale {table.scale}, "
            f"share rho {table.share}, squared L2 sensitivity {plan.sensitivity} for one entity",
            soft_wrap=True,
        )
    for line in _profile_follow.unknown_values_notes(drawn.plan or plan):
        say(line, soft_wrap=True)
    if ranked:
        say(
            "A rank correlation's value is formed exactly with its row floor, floored onto a "
            f"public lattice of {2**20:,} steps per sensitivity, and an exact discrete Gaussian "
            "is added on the lattice (Canonne, Kamath and Steinke 2020; no floating-point noise "
            "leaks, Mironov 2012). The profile states it as a gaussian of the scale shown: the "
            "variance of the discrete Gaussian does not exceed its square.",
            soft_wrap=True,
        )
    if plan.selects:
        chosen = (
            "; ".join(", ".join(clique) for clique in drawn.cliques) if drawn.cliques else "none"
        )
        origin = (
            "from the release record" if rederived else f"chosen with {plan.clique_method.upper()}"
        )
        say(f"Cliques {origin}: {chosen}.", soft_wrap=True)
    by_reason: Dict[str, List[str]] = {}
    for what, reason in (*plan.not_released, *drawn.left_out):
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
    if plan.has_other_tables:
        say(
            "The request holds other tables, which the profile lists with their domains only. "
            "A run cannot ship on flux-engine 0.9 until it skips a table the release holds no "
            "statistic for.",
            soft_wrap=True,
        )
    say("Pin it in the FLUX Simulation:")
    say(f"  profile: {{digest: {drawn.digest}}}", soft_wrap=True)
