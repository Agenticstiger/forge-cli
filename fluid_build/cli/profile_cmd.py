# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile``: an output-checked profile of a data product, read where its records live.

The command reads each expose's records once, inside the environment that holds
them, and writes one FLUX profile document (PROFILE-SPEC, in the least format
that holds it, 1-0-0 to 1-4-0) of aggregates that passed the disclosure
rules. A FLUX Simulation pins the document by the digest this prints. See
:mod:`fluid_build.profiler` for the parts.

Registered lazily (``cli/bootstrap.py`` ``CORE_COMMANDS``), and only the standard library is
imported at module scope, so neither ``fluid --help`` nor ``fluid profile
--help`` loads DuckDB or the statistic registry.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

COMMAND = "profile"

_DESCRIPTION = """\
Write an output-checked profile of an expose's records, read where they live.
Several exposes of the data product go into one document: give each one its
records as --source <exposeId>=<location> (and a database's table as --table
<exposeId>=<table>); the document is then named after the contract's id.

The document follows FLUX PROFILE-SPEC and names the least format that holds it:
1-0-0, or 1-1-0 when it holds a quantile with bins, 1-2-0 with facts, 1-3-0 with a
statistic grouped through a link, 1-4-0 with a rank correlation or a statistic grouped
by bins. It holds only aggregates that passed the output checks: no
record, no sample, no per-record value. The checks are ACRO 1.1.0's defaults, named
"acro-default" in the document:
  - a count below 10 records is withheld, and so is a zero;
  - every published count is rounded to a multiple of 5;
  - a sum is withheld when 2 records make up 90% of it, or when what is left
    after the 2 largest is below 10% of the largest;
  - a min, max or quantile is published only when 10 records hold that value;
  - a mean is published only when the column's count and sum are.
A withheld value keeps an SDMX CL_CONF_STATUS code and no value: A (a small
count), O or T (dominated by one or two records), M (concentration), D
(secondary suppression) or C (withheld). C marks a sum over at most two values,
and every withheld group and cell of a histogram or a quantile, whatever
withheld it, so their codes say nothing of a count's size. The summary counts
each withheld value by the check that withheld it.

For each number column the document also holds mean/1, the statistic the FLUX
fidelity check compares a mean property with: the number of values and their
sum on a grid of 2^-N steps. N is --fractional-bits, 16 unless given, and the
fidelity block's encoding.fractionalBits must be the same.

Without --bundle, the document also carries the statistics the contract's own
declarations give, declared from the contract and never from the records, as the
fidelity block FLUX's grammar reads (so `fluxsim init` can start from them):
  - for each enum(...) column, a levelHistogram over every declared level; a
    level no record holds is a withheld cell, and the column's distinctCount
    is withheld (D);
  - for each boolean column (boolean, bool, bit), the same over the two levels
    false and true, which a boolean is read as; a level no record holds is a
    withheld cell, and a value that is neither false nor true refuses the profile;
  - for a number whose validationRules declare both ends of a range inside
    its type, a marginalHistogram over about ten bins of it; its sum, min,
    max, quantiles and distinctCount are then withheld (D), and it has no
    mean/1;
  - for any other number of a bounded type, and each date and timestamp,
    quantile/2 at 0.25, 0.5 and 0.75 on a ladder fixed by the type (0, 1, 2,
    5, 10, 20, 50 ... for a number, one bin per year from 1900 to 2100 for a
    date or a timestamp);
  - for each categorical semantics.dimensions entry on an enum or boolean
    column, the other enum and boolean columns' histograms per level of it, every declared level a
    group, up to 10,000 cells for one column's groupings;
  - for each semantics.relationships entry between two exposes of the call,
    children per parent: how many child rows each parent row has, counted on
    the bins 0, 1, 2, 3-4, 5-9, 10-19 and 20 or more, with the parent as the
    unit; the child's foreign key then withholds its distinctCount, sum,
    min, max and quantiles, its key columns take no statistic of their own,
    and a cell a reader who knows the numbers of parents and children
    exactly could work out is withheld too.
A record outside what the contract declares (a level its type does not list, a
value outside its type or its declared range) refuses the profile.

With --bundle DIR the profile answers the Simulation's fidelity block instead:
it reads the block of DIR's one Simulation (*.flux.yml) for the expose and
writes the statistic each declared property is compared with (a mean, a
histogram over its bins or levels, a support, a quantile; per group where the
property has a groupBy), each group checked like the rest. The grid is the
block's. A group of a mean or a support that is withheld also withholds its
siblings, under D (secondary suppression), so it cannot be recovered from the
column's published total; the records a null groupBy value leaves out of every
group count as one more group there. A correlation is not answered: its moments
need the exact record count, which the output checks round.

What the checks leave readable: under --bundle, a grouped statistic lists every
group the data forms, published or withheld. A key is always a level the bundle
declares (a value outside the declared support is refused), so its presence says
only that the level holds at least one record. Without --bundle, every declared
level is a group, whatever the records hold.

The counts a histogram, a level histogram or a quantile publishes at the edges
of one column are checked together, as FLUX ADR 0012 sets, by flux-stats'
check_edge_counts. They sit on one partition (the column's bins, or its
levels). A quantile that declares bins is written as quantile/2, with the bin
that holds its order statistic and the records below that bin and through it,
and the profile then names format 1-1-0; its three cells (below, in and above
the bin) each hold at least 10 records, or none at the ends of the range. A
quantile without bins is published only as the one such statistic of its column.
A group of fewer than 10 records withholds every group of its groupBy, and a
group whose every cell is withheld is withheld whole. Across those statistics
of one column, no small cell, a zero included where zeros are disclosive, can be
formed by adding or subtracting what they publish, so more is withheld (D in
the summary). Only then are counts rounded. The check does not see a column's
support, which lists the levels that hold records, or the group sizes of a
statistic grouped by the column, which repeat its level counts; and under
--bundle a declared level that holds no record is absent from a grouped
statistic, which shows its zero. A published min or
max shows that no record lies below or above it. A quantile whose value is the
column's smallest or largest shows the same, and is withheld for it, as a zero
is.

With --request FILE the profile answers a FLUX profile request, the list of
statistics, column metrics and relationships `fluxsim init --contract` writes
for a contract, and measures what it lists and nothing else, under the same
checks. Give each listed table its records as --source <exposeId>=<location>
(one table may take a bare --source); there is no --expose, --env or --bundle,
and the request's grid is the profile's. The request's contract digest, when it
has one, must be the contract file's (run `fluxsim init` again otherwise). Per
table it holds only the columns, metrics and statistics listed, and the row
count only when asked; of the statistics, those the contract's declarations give
and the request lists, ungrouped. Children per
parent are counted for a listed relationship the contract declares, as a
histogram.

A request of format 1-2-0 may also ask for more, each exact and under the same checks:
  - rankCorrelations: for a table, Kendall's tau (kendallTau/1) or Spearman's rho
    (spearmanRho/1) of each pair of the columns listed, the one listed first first,
    as columnPairs entries (format 1-4-0). Each holds the number of records whose two
    values are known, with the sum S of the signs of the pairs' differences or A4, four
    times the sum of the products of the mid-ranks, over values floored onto the
    grid; neither is the Pearson correlation of the ranks. A group of fewer than 10
    records is withheld. With --row-floor N the statistic also states rowFloor N and
    its value is formed over max(records, N);
  - cliqueColumns: this profile has no cliques, so for each pair of the first 32
    columns listed it holds the first column's histogram grouped by the second, by
    groupBy where the second is an enum or boolean column and by groupByBins (format
    1-4-0) where it is a number with a declared range. Every level or bin is a group,
    so the groups never depend on the records, and the output check for counts at
    edges decides what each may publish. A column with no public levels or bins, and
    a pair past the 32 columns, is named in the report with its reason;
  - a relationship's groupBy: the children per parent of the parents in each level or
    bin of a parent column (format 1-4-0), a parent whose value is null in no group;
  - a relationship's linked: a child column's histogram grouped by an enum or boolean
    column of the parent row its key reaches, as groupByLink (format 1-3-0); a child
    that reaches no parent is in no group. Each listed item the profile does not answer is named in the report
with its reason (for example a histogram over a number with no declared range,
quantiles beside a histogram on one set of bins, a mean of a timestamp, a key
column of a counted relationship). A workload and the consumers' queries are not
formed: each is named under Not measured, and with --strict such a request is refused
before any record is read. A table's calendar is formed, but not by a private release,
which names it. A table's date pairs (DATE columns) and
a relationship's dates and exposure are formed (format 1-3-0); a pair's days are
counted on a fixed ladder of whole days, trimmed to the bins from the first holding a
record to the last, and are published only when each of those two bins holds at
least minimumCell records. One that cannot be formed (a pair with a timestamp
column, an exposure with no window, days with a small end bin) is named under
Not measured with its reason. A listed fact, a rule of the contract's
invariants, is measured on the table's records as the share of the rows it covers
that satisfy it: two counts (the rows that do not, the rows that do) that pass the
checks as cells do, and either withheld withholds both (format 1-2-0, the
invariants section). The facts of one table are also checked together, on exact
counts: a fact whose counts, with the other facts' and the row count, would give
back fewer than minimumCell rows, or where zeros are disclosive an empty
combination the rules' texts do not rule out, is withheld too (D in the report).
Rules that share too many parts to be read are taken to rule nothing out, which
withholds more. A rule over an aggregate, a dependency or a related row is
not measured, and the report says why. A boolean
column is asked only its counts by `fluxsim init`, so it gets no level
histogram this way.

These are output-checking conventions only. No privacy or anonymity guarantee
is made or implied: read the document before you share it.
"""

_EPILOG = """\
# a Parquet or CSV file, or a FLUX committed slice (.ndjson)
fluid profile contract.fluid.yaml --expose orders --source ./orders.parquet
fluid profile contract.fluid.yaml --expose orders --source ./slice.ndjson \\
    --out orders.profile.json
# a database table (the URI's password is never printed)
fluid profile contract.fluid.yaml --expose orders \\
    --source postgresql://db.internal:5432/sales --table public.orders
# the whole data product in one document: one --source <exposeId>=<location> each
fluid profile contract.fluid.yaml --source customers=./customers.csv \\
    --source orders=./orders.parquet --out product.profile.json

# Only aggregates leave: a count under 10 or a zero is withheld, counts are rounded
# to 5, a sum 2 records dominate is withheld, and a min, max or quantile is shown only
# when 10 records hold it (ACRO 1.1.0 defaults). A column's histograms and
# quantiles are checked together (FLUX ADR 0012): on one partition, a quantile with bins
# only with at least 10 records below its bin, in it and above it, and no small cell,
# a zero included, left to be formed by adding or subtracting what is published. A group
# of fewer than 10 records withholds every group of its groupBy. A histogram's or a
# quantile's withheld values all read C, whatever withheld them. A min or max is published
# though it shows that no record lies below or above it, and a quantile at the column's
# smallest or largest value is withheld for showing the same. Output-checking
# conventions only: no privacy or anonymity guarantee. Pin the printed digest in the
# FLUX Simulation.
# Each number column also gets mean/1, which FLUX compares a mean property with, on the
# grid --fractional-bits names (16 unless given): the fidelity block must declare it.

# answer a profile request: `fluxsim init --contract` writes it, the profile measures
# what it lists and nothing else; one --source <exposeId>=<location> per listed table
fluxsim init payment-bundle --contract contract.fluid.yaml
fluid profile contract.fluid.yaml --request payment-bundle.profile-request.yml \\
    --source customers=./customers.csv --source orders=./orders.parquet \\
    --out product.profile.json

# answer a Simulation's fidelity block: the bundle directory `fluxsim init` wrote holds
# the Simulation and the contract; the grid is the block's, so leave --fractional-bits out
fluid profile payment-bundle/payment.fluid.yml --expose payment --source ./slice.ndjson \\
    --bundle payment-bundle --out payment.profile.json
"""


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Wire ``fluid profile`` into the CLI."""
    p = subparsers.add_parser(
        COMMAND,
        help="Profile a data product's records where they live (aggregates only, output-checked)",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "contract",
        help="FLUID contract (or a stage-1 bundle .tgz) that declares the expose",
    )
    p.add_argument(
        "--expose",
        "--expose-id",
        dest="expose_id",
        # Collected, not overwritten: argparse would keep the last of two and read the
        # first expose's source as the second one.
        action="append",
        help=(
            "exposeId to profile; repeat it to profile several exposes into one document, "
            "each with its own --source <exposeId>=<location> (default: the contract's only "
            "expose, or every expose a --source names)"
        ),
    )
    p.add_argument(
        "--source",
        required=True,
        action="append",
        help=(
            "where the records live: a .ndjson, .jsonl, .csv or .parquet file (a path "
            "or a file:// URL), or a postgres://, mysql:// or sqlite:/// database URI; "
            "for several exposes, one --source <exposeId>=<location> each"
        ),
    )
    p.add_argument(
        "--table",
        action="append",
        help=(
            "the table of a database --source, as name or schema.name; for several "
            "exposes, --table <exposeId>=<table>"
        ),
    )
    p.add_argument(
        "--out",
        help=(
            "where to write the profile (default: <exposeId>.profile.json here, or "
            "<contract id>.profile.json for several exposes)"
        ),
    )
    p.add_argument(
        "--fractional-bits",
        type=_fractional_bits,
        default=None,
        metavar="N",
        help=(
            "the grid mean/1 is formed on, 2^-N steps: the fidelity block's "
            f"encoding.fractionalBits (default: {_DEFAULT_FRACTIONAL_BITS}, FLUX's default, "
            f"or the block's with --bundle; 0 to {_MAX_FRACTIONAL_BITS})"
        ),
    )
    p.add_argument(
        "--bundle",
        metavar="DIR",
        help=(
            "a FLUX bundle directory: answer the fidelity block its one Simulation "
            "(*.flux.yml) declares for the expose, with the statistic each property "
            "is compared with"
        ),
    )
    p.add_argument(
        "--request",
        metavar="FILE",
        help=(
            "a FLUX profile request `fluxsim init --contract` wrote: measure what it lists "
            "and nothing else, under the same output checks (not with --bundle, --env or "
            "--expose: the request names the tables, and its grid is the profile's); the "
            "facts of one table are also checked together, and rules that share too many "
            "parts to be read rule nothing out, which withholds more"
        ),
    )
    p.add_argument(
        "--row-floor",
        type=_row_floor,
        default=None,
        metavar="N",
        help=(
            "the row floor of the rank correlations a --request asks for: a public count of "
            "2 or more, written as the statistic's rowFloor and used to form its value "
            "(N = max(records, floor)); without it a rank correlation is formed over its "
            "records alone and states no rowFloor"
        ),
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "refuse a --request that asks for a kind of statistic fluid profile does not form "
            "(a workload, a consumer query; a table's calendar too, for a private release), "
            "before any record is read; without it each such ask is named under Not measured"
        ),
    )
    p.add_argument(
        "--timings",
        action="store_true",
        help=(
            "after the report, print how long each table took to read, measure and check, and "
            "how long the document took to write, in seconds; the document is the same bytes "
            "without it. check: the output checks across a column's statistics "
            "(counts at edges); a check made metric by metric is counted as measure. Refused "
            "with a private release, whose output must not follow the row count"
        ),
    )
    p.add_argument("--env", help="environment overlay to apply to the contract first")
    from fluid_build.cli import _profile_release

    _profile_release.add_flags(p)
    p.set_defaults(func=run)


#: The profile document's grid bounds, repeated from ``profiler/document.py`` so
#: that registering the command imports nothing but the standard library;
#: ``tests/profiler/test_cli_profile.py`` keeps the two equal.
_DEFAULT_FRACTIONAL_BITS = 16
_MAX_FRACTIONAL_BITS = 128


def _fractional_bits(text: str) -> int:
    """``--fractional-bits``: a whole number from 0 to 128, as PROFILE-SPEC §2 allows."""
    try:
        bits = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if not 0 <= bits <= _MAX_FRACTIONAL_BITS:
        raise argparse.ArgumentTypeError(f"{bits} is outside 0 to {_MAX_FRACTIONAL_BITS}")
    return bits


def _row_floor(text: str) -> int:
    """``--row-floor``: a whole number of 2 or more, as PROFILE-SPEC section 5 allows."""
    try:
        floor = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if not 2 <= floor <= 2**53 - 1:
        raise argparse.ArgumentTypeError(f"{floor} is outside 2 to {2**53 - 1}")
    return floor


def _refusal(event: str, detail: str, context: Dict[str, str]) -> Any:
    """A :class:`CLIError` whose first context line says what happened."""
    from fluid_build._contract_loader import CLIError

    return CLIError(
        1, event, {"detail": detail, **{k: v for k, v in context.items() if k != "detail"}}
    )


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Profile the expose and write the document; return the exit code."""
    from fluid_build.cli import _profile_release

    if getattr(args, "strict", False) and getattr(args, "request", None) is None:
        raise _refusal(
            "profile_strict_needs_request",
            "--strict refuses a request that asks for what fluid profile does not form, and "
            "--request names none",
            {
                "read": "nothing: no record was read",
                "next": "give --request, or leave --strict out",
            },
        )
    _refuse_timings_with_release(args)
    if _profile_release.wanted(args):
        return _profile_release.run(args, logger)
    from fluid_build._console import cprint_literal
    from fluid_build._contract_loader import load_contract_with_overlay
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.profiler import _stats, bundle, runner, sources
    from fluid_build.profiler import request as asked_for
    from fluid_build.profiler.document import DocumentError

    if getattr(args, "request", None) is not None:
        try:
            asked_for.check_flags(bundle=args.bundle, env=args.env, exposes=args.expose_id)
        except runner.ProfileError as exc:
            raise _as_refusal(exc, args.source) from None
    elif getattr(args, "row_floor", None) is not None:
        raise _refusal(
            "profile_row_floor_needs_request",
            "--row-floor is the floor of the rank correlations a request asks for",
            {
                "read": "nothing: no record was read",
                "next": "give --request, or leave --row-floor out",
            },
        )
    try:
        backend = _stats.load_backend()
    except _stats.StatisticsUnavailable as exc:
        raise _refusal(
            "profile_statistics_unavailable",
            str(exc),
            {
                "read": "nothing: no record was read",
                "next": _stats.FLUX_STATS_INSTALL_COMMAND,
            },
        ) from None

    contract_path = validate_cli_path(args.contract, mode="read", file_type="contract")
    bundle_dir = _bundle_dir(args.bundle)
    request_file = getattr(args, "request", None)
    asked = None
    digest_matched: Optional[bool] = None
    if request_file is not None:
        try:
            asked_for.check_flags(bundle=args.bundle, env=args.env, exposes=args.expose_id)
            asked = asked_for.read_request_file(
                validate_cli_path(request_file, mode="read", file_type="request"), backend
            )
            digest_matched = asked_for.check_contract_file(contract_path, asked, backend)
            if getattr(args, "strict", False):
                asked_for.refuse_unformed(asked)
        except (runner.ProfileError, _stats.StatisticsUnavailable) as exc:
            raise _as_refusal(exc, args.source) from None
    contract = load_contract_with_overlay(str(contract_path), args.env, logger)
    try:
        if asked is not None:
            exposes = _request_sources(contract, asked, args.source, args.table)
        else:
            exposes = _expose_sources(contract, args.expose_id, args.source, args.table)
        out = _out_path(
            args.out, _document_name(contract, exposes), [each.source for each in exposes]
        )
        blocks = (
            {
                each.expose_id: bundle.read_bundle_block(bundle_dir, each.expose_id, backend)
                for each in exposes
            }
            if bundle_dir
            else None
        )
        timings = runner.StageTimings() if getattr(args, "timings", False) else None
        started = time.perf_counter()
        result = runner.profile_exposes(
            contract,
            exposes,
            cwd=Path.cwd(),
            backend=backend,
            fractional_bits=args.fractional_bits,
            bundles=blocks,
            request=asked,
            row_floor=getattr(args, "row_floor", None),
            timings=timings,
        )
        write_started = time.perf_counter()
        _write(out, result.canonical)
        _stamp_timings(timings, started, write_started)
    except (
        runner.ProfileError,
        bundle.BundleError,
        sources.SourceError,
        _stats.ReadRefused,
        _stats.MeasureRefused,
        _stats.StatisticsUnavailable,
        DocumentError,
        _stats.DocumentRefused,
    ) as exc:
        raise _as_refusal(exc, args.source) from None
    _report(
        cprint_literal,
        result,
        out,
        answered_block=blocks is not None,
        request=None if asked is None else (Path(str(request_file)).name, digest_matched),
    )
    _report_timings(cprint_literal, result, timings)
    return 0


def _request_sources(
    contract: Any, request: Any, given_sources: List[str], given_tables: Optional[List[str]]
) -> List[Any]:
    """Pair each table the request lists with its ``--source``, before any read.

    One listed table may take a bare ``--source``; otherwise each is written
    ``--source <exposeId>=<location>``, one for every listed table and none for another.
    """
    from fluid_build.profiler import request as asked_for
    from fluid_build.profiler import runner

    listed = [table.expose_id for table in request.tables]
    pairs = [_paired(runner.expose_ids(contract), value) for value in given_sources]
    if not (len(listed) == 1 and len(pairs) == 1 and pairs[0][0] is None):
        asked_for.check_sources(request, [expose_id for expose_id, _ in pairs if expose_id])
    return _expose_sources(contract, listed, given_sources, given_tables)


def _paired(contract_ids: Sequence[str], value: str) -> Tuple[Optional[str], str]:
    """``(exposeId, rest)`` for ``<exposeId>=<rest>``, else ``(None, value)``.

    A value is paired only when the text before its first ``=`` is an exposeId of the
    contract, so a database URI's query (``?sslmode=require``) or a path holding ``=``
    stays whole; ``./orders=2024.csv`` reads a file named ``orders=2024.csv``.
    """
    head, sep, rest = value.partition("=")
    if sep and head in contract_ids:
        return head, rest
    return None, value


def _expose_sources(
    contract: Any,
    exposes: Optional[List[str]],
    given_sources: List[str],
    given_tables: Optional[List[str]],
) -> List[Any]:
    """Pair each expose to profile with its ``--source`` (and ``--table``), before any read.

    One expose takes a bare ``--source``: the one ``--expose``, or the contract's only
    expose. Several take one ``--source <exposeId>=<location>`` each, and are the
    ``--expose`` flags given, in that order, or else every expose a source names, in the
    contract's order. Every refusal names a database source with its password masked.
    """
    from fluid_build.profiler import runner
    from fluid_build.profiler.sources import describe_source

    ids = runner.expose_ids(contract)
    named = list(exposes or [])
    for position, requested in enumerate(named):
        if requested in named[:position]:
            raise runner.ProfileError(
                "profile_expose_repeated",
                f"--expose names {requested!r} twice",
                {"read": "nothing: no record was read", "next": "name each expose once"},
            )
        if requested not in ids:
            runner.resolve_expose_id(contract, requested)  # profile_expose_not_found
    pairs = [_paired(ids, value) for value in given_sources]
    tables = [_paired(ids, value) for value in given_tables or []]
    if all(expose_id is None for expose_id, _ in pairs):
        if len(pairs) > 1 or len(named) > 1:
            raise runner.ProfileError(
                "profile_source_unpaired",
                (
                    f"--source is given {len(pairs)} times"
                    if len(pairs) > 1
                    else f"--expose is given {len(named)} times ({', '.join(named)})"
                )
                + ", and no --source names its expose",
                {
                    "read": "nothing: no record was read",
                    "next": "give each expose its records as --source <exposeId>=<location>",
                },
            )
        only = runner.resolve_expose_id(contract, named[0] if named else None)
        return [runner.ExposeSource(only, pairs[0][1], _one_table(tables, [only]))]
    for paired, location in pairs:
        if paired is None:
            raise runner.ProfileError(
                "profile_source_unpaired",
                f"--source {describe_source(location)} names no expose of the contract",
                {
                    "read": "nothing: no record was read",
                    "exposes": ", ".join(ids),
                    "next": "with several sources, write each as --source <exposeId>=<location>",
                },
            )
    by_expose: Dict[str, str] = {}
    for paired, location in pairs:
        if str(paired) in by_expose:
            raise runner.ProfileError(
                "profile_source_repeated",
                f"exposes[{paired}] is given more than one --source",
                {"read": "nothing: no record was read", "next": "give each expose one source"},
            )
        by_expose[str(paired)] = location
    if named:
        missing = [expose_id for expose_id in named if expose_id not in by_expose]
        if missing:
            raise runner.ProfileError(
                "profile_source_missing",
                f"exposes[{missing[0]}] has no --source {missing[0]}=<location>",
                {"read": "nothing: no record was read", "next": "give every --expose its source"},
            )
        unused = [expose_id for expose_id in by_expose if expose_id not in named]
        if unused:
            raise runner.ProfileError(
                "profile_source_unused",
                f"--source names exposes[{unused[0]}], which no --expose names",
                {
                    "read": "nothing: no record was read",
                    "next": f"add --expose {unused[0]}, or leave its --source out",
                },
            )
        order = named
    else:
        order = [expose_id for expose_id in ids if expose_id in by_expose]
    chosen = _tables_by_expose(tables, order)
    return [
        runner.ExposeSource(expose_id, by_expose[expose_id], chosen.get(expose_id))
        for expose_id in order
    ]


def _one_table(tables: List[Tuple[Optional[str], str]], exposes: List[str]) -> Optional[str]:
    """The ``--table`` of a profile of one expose: bare, or paired with that expose."""
    if not tables:
        return None
    return _tables_by_expose(tables, exposes, bare=True).get(exposes[0])


def _tables_by_expose(
    tables: List[Tuple[Optional[str], str]], exposes: List[str], *, bare: bool = False
) -> Dict[str, str]:
    """Each ``--table`` by the expose it names; a bare one only where *bare* allows one."""
    from fluid_build.profiler.runner import ProfileError

    found: Dict[str, str] = {}
    for expose_id, table in tables:
        if expose_id is None:
            if not bare or len(tables) > 1:
                raise ProfileError(
                    "profile_table_unpaired",
                    "--table names no expose, and several tables or exposes are given",
                    {"next": "write each as --table <exposeId>=<table>"},
                )
            expose_id = exposes[0]
        if expose_id not in exposes:
            raise ProfileError(
                "profile_table_unused",
                f"--table names exposes[{expose_id}], which this profile does not read",
                {"next": "name a table for an expose that is profiled"},
            )
        if expose_id in found:
            raise ProfileError(
                "profile_table_repeated",
                f"exposes[{expose_id}] is given more than one --table",
                {"next": "give each expose one table"},
            )
        found[expose_id] = table
    return found


def _document_name(contract: Any, exposes: List[Any]) -> str:
    """The default document name: the expose's id, or for several the contract's ``id``."""
    if len(exposes) == 1:
        return str(exposes[0].expose_id)
    product = contract.get("id") if hasattr(contract, "get") else None
    return str(product) if isinstance(product, str) else ""


def _bundle_dir(given: Optional[str]) -> Optional[Path]:
    """``--bundle``, checked as the contract is, and a directory that exists."""
    from fluid_build.cli.security import validate_cli_path

    if given is None:
        return None
    if not given.strip():
        # An unset variable (`--bundle "$BUNDLE"`) must not drop the block unnoticed,
        # nor name the working directory.
        raise _refusal(
            "profile_bundle_not_a_directory",
            "--bundle names no directory",
            {"next": "name the bundle directory `fluxsim init` wrote"},
        ) from None
    path = validate_cli_path(given, mode="read", file_type="bundle")
    if not path.is_dir():
        raise _refusal(
            "profile_bundle_not_a_directory",
            f"{path.name} is not a directory",
            {"next": "name the bundle directory `fluxsim init` wrote"},
        ) from None
    return path


def _described(given_sources: Sequence[str]) -> str:
    """Every ``--source`` as it may be shown: each database URI with its password masked.

    :func:`~fluid_build.profiler.sources.describe_source` masks a URI after its
    ``<exposeId>=`` too, a mistyped exposeId included.
    """
    from fluid_build.profiler.sources import describe_source

    return ", ".join(describe_source(value) for value in given_sources)


def _as_refusal(exc: Exception, given_sources: Sequence[str]) -> Any:
    """The :class:`CLIError` for a refusal from the profiler; nothing has been written."""
    from fluid_build.profiler import _stats, bundle, runner, sources

    if isinstance(exc, (runner.ProfileError, bundle.BundleError)):
        return _refusal(exc.event, str(exc), exc.context)
    if isinstance(exc, _stats.StatisticsUnavailable):
        return _refusal(
            "profile_statistics_unavailable",
            str(exc),
            {"read": "nothing: no record was read", "next": _stats.FLUX_STATS_INSTALL_COMMAND},
        )
    if isinstance(exc, _stats.MeasureRefused):
        return _refusal(
            "profile_bundle_record_refused",
            str(exc),
            {
                "written": "nothing",
                "next": "the data and the fidelity block disagree: fix either, then profile",
            },
        )
    if isinstance(exc, sources.SourceError):
        return _refusal(
            "profile_source_unreadable",
            str(exc),
            {"source": _described(given_sources), "written": "nothing"},
        )
    if isinstance(exc, _stats.ReadRefused):
        return _refusal(
            "profile_value_refused",
            str(exc),
            {
                "written": "nothing",
                "next": (
                    "write it as the fidelity check reads one: a number as an exact "
                    "decimal, a timestamp in RFC 3339 with an offset"
                ),
            },
        )
    return _refusal(
        "profile_document_refused",
        str(exc),
        {"written": "nothing", "next": "report it with your forge-cli and flux-stats versions"},
    )


#: What a default document name may hold: the contract's exposeId comes from wherever
#: the contract did, and a name with a separator or shell syntax in it would put the
#: document, or something worse, somewhere else.
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _out_path(given: Optional[str], name: str, given_sources: Sequence[str]) -> Path:
    """Where the document goes: ``--out``, or ``<name>.profile.json`` here.

    *name* is the one expose's id, or for several exposes the contract's ``id``.

    Checked before any record is read, and without touching the file system: no
    directory is made and nothing is written until the profile is formed
    (:func:`_write`). The document is written as named, never through a link: a
    symbolic link at the target is refused, and the write replaces whatever name is
    there without following it (``atomic_write_bytes`` uses ``os.replace``). A link
    planted in a checked-out repository cannot turn the write into one on another
    file, and a target that is the source itself is refused, so the records are not
    replaced by their own profile.
    """
    from fluid_build.profiler.runner import ProfileError

    if given:
        target = Path(given)
    elif _PLAIN_NAME.match(name) and name not in (".", ".."):
        target = Path(f"{name}.profile.json")
    else:
        raise ProfileError(
            "profile_out_required",
            f"{name!r} cannot name a file" if name else "the contract has no id to name it by",
            {"next": "name the document with --out"},
        )
    unresolved = target if target.is_absolute() else Path.cwd() / target
    try:
        if unresolved.is_symlink():
            raise ProfileError(
                "profile_out_is_a_link",
                f"{unresolved} is a symbolic link",
                {"next": "remove the link, or name another file with --out"},
            )
        _refuse_a_source(unresolved, given_sources)
    except OSError as exc:
        raise ProfileError(
            "profile_out_unusable",
            f"{unresolved} cannot be used: {exc.strerror or type(exc).__name__}",
        ) from None
    return unresolved


def _refuse_a_source(target: Path, given_sources: Sequence[str]) -> None:
    """Refuse a document target that is a file the records are read from."""
    from fluid_build.profiler.runner import ProfileError
    from fluid_build.profiler.sources import local_file

    if not target.exists():
        return
    for source in given_sources:
        read_from = local_file(source)
        if read_from is not None and read_from.exists() and target.samefile(read_from):
            raise ProfileError(
                "profile_out_is_the_source",
                f"{target} is the file the records are read from",
                {"next": "name another file with --out"},
            )


def _write(out: Path, data: bytes) -> None:
    """Check the target as every ``--out`` is, then write the document owner-only and atomically.

    ``validate_cli_path`` refuses ``..`` segments and the operating system's own
    directories, and makes the parent directory; it runs here, once the profile is
    formed, so a refused profile leaves nothing behind.
    """
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.profiler.runner import ProfileError
    from fluid_build.util.atomic_write import atomic_write_bytes

    try:
        validate_cli_path(out, mode="write", must_exist=False, file_type="profile")
        if out.is_symlink():
            raise ProfileError(
                "profile_out_is_a_link",
                f"{out} is a symbolic link",
                {"next": "remove the link, or name another file with --out"},
            )
        atomic_write_bytes(out, data)
    except OSError as exc:
        raise ProfileError(
            "profile_write_failed",
            f"{out} could not be written: {exc.strerror or type(exc).__name__}",
        ) from None


#: What each withheld code means in the summary (PROFILE-SPEC §3's paraphrase).
_CODE_MEANING = {
    "A": "small count",
    "C": "a sum over at most two values",
    "D": "secondary suppression",
    "M": "concentration",
    "O": "dominated by one record",
    "T": "dominated by two records",
}


def _codes(counts: Dict[str, int]) -> str:
    """``A (small count) 3, D (secondary suppression) 1``."""
    return ", ".join(
        f"{code} ({_CODE_MEANING.get(code, 'withheld')}) {count}" for code, count in counts.items()
    )


def _one_code_withheld(table: Any) -> bool:
    """Whether a table's histogram or quantile, whose withheld values all read C, has one."""
    from fluid_build.profiler.disclosure import ONE_CODE

    return any(
        each[1].partition("/")[0] in ONE_CODE
        for each in (*table.withheld_statistics, *table.partly_withheld_statistics)
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _named(statistics: Any) -> List[str]:
    """Name each statistic once, those of one definition, shape and groupBy together.

    ``mean/1 of 'amount', 'qty'``, ``quantile/1 of 'amount' at level 0.5 by 'market'``.
    """
    columns: Dict[Any, List[str]] = {}
    for column, identifier, group_by, shape in statistics:
        named = columns.setdefault((identifier, shape, group_by), [])
        if repr(column) not in named:
            named.append(repr(column))
    return [
        f"{identifier} of {', '.join(names)}"
        + (f" {shape}" if shape else "")
        + (f" by {group_by!r}" if group_by else "")
        for (identifier, shape, group_by), names in columns.items()
    ]


def _named_partly(statistics: Any) -> List[str]:
    """``levelHistogram/1 of 'market' by 'tier' (2 of 3 groups)`` for each statistic."""
    return [
        f"{_named([(column, identifier, group_by, shape)])[0]} ({shown} of {total} groups)"
        for column, identifier, group_by, shape, shown, total in statistics
    ]


def _report(
    say: Any,
    result: Any,
    out: Path,
    *,
    answered_block: bool = False,
    request: Optional[Tuple[str, Optional[bool]]] = None,
) -> None:
    """Tell the owner what the document holds, then print the pin.

    A profile of several exposes names each line about a table by its expose. *request* is
    the file name of the request the profile answers and whether its contract digest matched.
    """
    if request is not None:
        say(
            f"The profile answers the request {request[0]}: "
            + (
                "its contract digest is this contract's."
                if request[1]
                else "it names no contract digest."
            ),
            soft_wrap=True,
        )
    tables = result.tables
    several = len(tables) > 1
    withheld: Dict[str, int] = dict(result.withheld)
    counts = f"{_plural(result.published, 'value')} published, {sum(withheld.values())} withheld."
    if several:
        columns = sum(len(table.plan.columns) for table in tables)
        names = ", ".join(table.plan.expose_id for table in tables)
        say(
            f"Profiled {len(tables)} exposes ({names}): {_plural(columns, 'column')}, {counts}",
            soft_wrap=True,
        )
    else:
        plan = tables[0].plan
        say(f"Profiled exposes[{plan.expose_id}]: {_plural(len(plan.columns), 'column')}, {counts}")
    if withheld:
        say(f"Withheld, by the check (CL_CONF_STATUS code): {_codes(withheld)}.")
    cells: Dict[str, int] = dict(result.withheld_cells)
    if cells:
        say(f"Withheld histogram cells, by the check (CL_CONF_STATUS code): {_codes(cells)}.")
    if cells or any(_one_code_withheld(table) for table in tables):
        say(
            "The document writes C for every withheld group and cell of a histogram or "
            "a quantile, whatever withheld it, so its code says nothing of its size.",
            soft_wrap=True,
        )
    grid = (
        f"on the request's fractionalBits {result.fractional_bits}"
        if request is not None
        else (
            f"on the fidelity block's fractionalBits {result.fractional_bits}"
            if answered_block
            else f"on fractionalBits {result.fractional_bits} "
            "(the fidelity block must declare the same)"
        )
    )
    for table in tables:
        _report_table(say, table, f"exposes[{table.plan.expose_id}]: " if several else "", grid)
    for counted in result.relationships:
        link = counted.relationship
        shown = (
            f"{counted.published} of {counted.cells} cells published"
            if counted.published
            else "withheld whole"
        )
        say(
            f"Children per parent of exposes[{link.parent}] in exposes[{link.child}] "
            f"({link.name}): {shown}.",
            soft_wrap=True,
        )
    for child, name, reason in result.relationships_left_out:
        say(f"No children per parent for exposes[{child}] {name!r}: {reason}.", soft_wrap=True)
    _report_rank_correlations(say, result, several)
    _report_dates(say, result)
    _report_structure(say, result)
    for fact in result.facts:
        say(
            (
                f"Fact {fact.name}: its share is published, as rounded counts."
                if fact.published
                else f"Fact {fact.name}: withheld by the check "
                f"({fact.code}, {_CODE_MEANING.get(str(fact.code), 'withheld')})."
            ),
            soft_wrap=True,
        )
    for what, reason in result.not_measured:
        say(f"Not measured: {what}: {reason}.", soft_wrap=True)
    if not result.published:
        say("Every value was withheld: the document says nothing about the records.")
    say(f"Wrote {out} (readable by you alone; read it before you share it).", soft_wrap=True)
    say("Pin it in the FLUX Simulation:")
    # One line, never wrapped: it is copied as it stands.
    say(f"  profile: {{digest: {result.digest}}}", soft_wrap=True)


def _refuse_timings_with_release(args: argparse.Namespace) -> None:
    """Refuse ``--timings`` with a private release, whose output must not follow the row count."""
    from fluid_build.cli import _profile_release

    if getattr(args, "timings", False) and _profile_release.wanted(args):
        raise _refusal(
            "profile_timings_with_release",
            "a private release prints nothing that depends on the records outside the noise, "
            "and timings follow the row count",
            {
                "read": "nothing: no record was read",
                "next": "leave --timings out of a private release",
            },
        )


def _stamp_timings(timings: Any, started: float, write_started: float) -> None:
    """Close the document's timings: the write, and the whole run. Nothing without ``--timings``."""
    if timings is not None:
        now = time.perf_counter()
        timings.write += now - write_started
        timings.total = now - started


def _report_timings(say: Any, result: Any, timings: Any) -> None:
    """One line per table, in profile order, then the document's: seconds, two decimals.

    Nothing without ``--timings`` (*timings* is ``None``).
    """
    if timings is None:
        return
    for table in result.tables:
        name = table.plan.expose_id
        say(
            f"Timings for {name}: read {timings.read.get(name, 0.0):.2f} s, "
            f"measure {timings.measure.get(name, 0.0):.2f} s, "
            f"check {timings.check.get(name, 0.0):.2f} s.",
            soft_wrap=True,
        )
    say(
        f"Timings for the document: write {timings.write:.2f} s; total {timings.total:.2f} s.",
        soft_wrap=True,
    )


def _published_days(entries: Any) -> int:
    """How many pairs publish their day histogram: one is left out when an end bin is small."""
    return sum(
        1
        for each in entries
        if each["statistics"] and each["statistics"][0]["groups"][0]["status"] == "F"
    )


def _published_groups(statistics: Any) -> Tuple[int, int]:
    """How many of the groups of these statistics are published, and how many there are.

    A group counts as published only when it is and none of its cells is withheld.
    """
    groups = [group for each in statistics for group in each["groups"]]
    whole = [
        each
        for each in groups
        if each["status"] == "F" and all(status == "F" for status in each.get("cellStatus", ()))
    ]
    return len(whole), len(groups)


def _report_structure(say: Any, result: Any) -> None:
    """One line for each table's calendar, periods and per-entity statistics: how many published.

    Names tables and how many of the written groups were published, never a count of records.
    """
    for expose_id, table in result.document["tables"].items():
        where = f"exposes[{expose_id}]: "
        calendar = [
            each[key] for each in table.get("calendar", ()) for key in each if key != "column"
        ]
        if calendar:
            shown, total = _published_groups(calendar)
            say(f"{where}Calendar: {shown} of {total} histograms published.", soft_wrap=True)
        periods = [each for entry in table.get("periods", ()) for each in entry["statistics"]]
        if periods:
            shown, total = _published_groups(periods)
            say(f"{where}Periods: {shown} of {total} monthly groups published.", soft_wrap=True)
        per = [
            each
            for column in table["columns"].values()
            for each in column.get("statistics", ())
            if "per" in each
        ]
        if per:
            shown, total = _published_groups(per)
            say(f"{where}Per-entity statistics: {shown} of {total} published.", soft_wrap=True)


def _report_dates(say: Any, result: Any) -> None:
    """The lines about a profile's date pairs, relationship dates, structural classes and exposure.

    Each names tables, columns and how many entries were published, never a count of records.
    """
    for expose_id, table in result.document["tables"].items():
        pairs = table.get("datePairs", ())
        if pairs:
            say(
                f"exposes[{expose_id}]: Date pairs: {_published_days(pairs)} of {len(pairs)} published."
            )
    for entry in result.document.get("relationships", ()):
        where = f"exposes[{entry['parent']['table']}] in exposes[{entry['child']['table']}]"
        dates = entry.get("dates", ())
        if dates:
            say(
                f"Dates of {where}: {_published_days(dates)} of {len(dates)} published.",
                soft_wrap=True,
            )
        if "structural" in entry:
            say(
                f"Structural classes of {where}: {', '.join(entry['structural'])}, "
                "stated by the contract.",
                soft_wrap=True,
            )
        exposure = entry.get("exposure", ())
        if exposure:
            published = sum(
                1
                for each in exposure
                if each["children"]["status"] == "F" and each["activeDays"]["status"] == "F"
            )
            say(f"Exposure of {where}: {published} of {len(exposure)} published.", soft_wrap=True)


def _report_rank_correlations(say: Any, result: Any, several: bool) -> None:
    """One line for each table that holds rank correlations: how many were published."""
    for expose_id, table in result.document["tables"].items():
        statistics = [
            statistic
            for pair in table.get("columnPairs", ())
            for statistic in pair["statistics"]
            if statistic["definition"] in ("kendallTau/1", "spearmanRho/1")
        ]
        if not statistics:
            continue
        published = sum(1 for each in statistics if each["groups"][0]["status"] == "F")
        floors = {each["rowFloor"] for each in statistics if "rowFloor" in each}
        floor = f", each with rowFloor {floors.pop()}" if len(floors) == 1 else ""
        where = f"exposes[{expose_id}]: " if several else ""
        say(
            f"{where}Rank correlations: {published} of {len(statistics)} published{floor}.",
            soft_wrap=True,
        )


def _report_table(say: Any, table: Any, prefix: str, grid: str) -> None:
    """The summary lines about one table, each led by *prefix* (its expose, or nothing)."""
    named = _named(table.statistics)
    whole = _named(table.withheld_statistics)
    if named:
        say(f"{prefix}Fidelity statistics: {'; '.join(named)}, {grid}.", soft_wrap=True)
    if whole:
        say(
            f"{prefix}Withheld whole, so a run that checks them scores them censored: "
            f"{'; '.join(whole)}.",
            soft_wrap=True,
        )
    partly = _named_partly(table.partly_withheld_statistics)
    if partly:
        say(
            f"{prefix}Partly withheld, so a run that checks them scores only the published "
            f"groups: {'; '.join(partly)}.",
            soft_wrap=True,
        )
    for description, reason in table.left_out:
        say(
            f"{prefix}Not answered: {description}: {reason}; a run that checks it is refused "
            "(FLX-5037).",
            soft_wrap=True,
        )
    for name, reason in table.plan.skipped:
        say(f"{prefix}Not profiled: column {name!r}: {reason}.")
    for name, reason in table.uncovered:
        say(
            f"{prefix}Left out of the default statistics: column {name!r}: {reason}.",
            soft_wrap=True,
        )
    for name, reason in table.no_span:
        say(
            f"{prefix}{table.plan.expose_id}.{name}: no date span: {reason}.",
            soft_wrap=True,
        )
