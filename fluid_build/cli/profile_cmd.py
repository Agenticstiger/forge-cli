# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile``: an output-checked profile of one expose, read where its records live.

The command reads the expose's records once, inside the environment that holds
them, and writes a FLUX profile document (PROFILE-SPEC format 1-0-0, or 1-1-0
when it holds a quantile with bins) of aggregates that passed the disclosure
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
from pathlib import Path
from typing import Any, Dict, List, Optional

COMMAND = "profile"

_DESCRIPTION = """\
Write an output-checked profile of one expose's records, read where they live.

The document follows FLUX PROFILE-SPEC (format 1-0-0, or 1-1-0 when it holds a
quantile with bins) and holds only aggregates that passed the output checks: no
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

What the checks leave readable: a grouped statistic lists every group the data
forms, published or withheld. A key is always a level the bundle declares (a
value outside the declared support is refused), so its presence says only that
the level holds at least one record.

The counts a histogram, a level histogram or a quantile publishes at the edges
of one column are checked together, as FLUX ADR 0012 sets, by flux-stats'
check_edge_counts. They sit on one partition (the column's bins, or its
levels). A quantile that declares bins is written as quantile/2, with the bin
that holds its order statistic and the records below that bin and through it,
and the profile then names format 1-1-0; its three cells (below, in and above
the bin) each hold at least 10 records, or none at the ends of the range. A
quantile without bins is published only as the one such statistic of its column.
A group of fewer than 10 records withholds every group of its groupBy. No small
cell, a zero included where zeros are disclosive, can be formed by adding or
subtracting what is published, so more is withheld (D in the summary). Only
then are counts rounded. A published min or
max shows that no record lies below or above it. A quantile whose value is the
column's smallest or largest shows the same, and is withheld for it, as a zero
is.

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

# Only aggregates leave: a count under 10 or a zero is withheld, counts are rounded
# to 5, a sum 2 records dominate is withheld, and a min, max or quantile is shown only
# when 10 records hold it (ACRO 1.1.0 defaults). With --bundle, a column's histograms and
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

# answer a Simulation's fidelity block: the bundle directory `fluxsim init` wrote holds
# the Simulation and the contract; the grid is the block's, so leave --fractional-bits out
fluid profile payment-bundle/payment.fluid.yml --expose payment --source ./slice.ndjson \\
    --bundle payment-bundle --out payment.profile.json
"""


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Wire ``fluid profile`` into the CLI."""
    p = subparsers.add_parser(
        COMMAND,
        help="Profile an expose's records where they live (aggregates only, output-checked)",
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
        help="exposeId to profile (default: the contract's only expose)",
    )
    p.add_argument(
        "--source",
        required=True,
        help=(
            "where the records live: a .ndjson, .jsonl, .csv or .parquet file (a path "
            "or a file:// URL), or a postgres://, mysql:// or sqlite:/// database URI"
        ),
    )
    p.add_argument(
        "--table",
        help="the table of a database --source, as name or schema.name",
    )
    p.add_argument(
        "--out",
        help="where to write the profile (default: <exposeId>.profile.json here)",
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
    p.add_argument("--env", help="environment overlay to apply to the contract first")
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


def _refusal(event: str, detail: str, context: Dict[str, str]) -> Any:
    """A :class:`CLIError` whose first context line says what happened."""
    from fluid_build._contract_loader import CLIError

    return CLIError(
        1, event, {"detail": detail, **{k: v for k, v in context.items() if k != "detail"}}
    )


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Profile the expose and write the document; return the exit code."""
    from fluid_build._console import cprint_literal
    from fluid_build._contract_loader import load_contract_with_overlay
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.profiler import _stats, bundle, runner, sources
    from fluid_build.profiler.document import DocumentError

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
    contract = load_contract_with_overlay(str(contract_path), args.env, logger)
    try:
        expose_id = runner.resolve_expose_id(contract, args.expose_id)
        out = _out_path(args.out, expose_id, args.source)
        block = bundle.read_bundle_block(bundle_dir, expose_id, backend) if bundle_dir else None
        result = runner.profile_expose(
            contract,
            expose_id,
            source=args.source,
            table=args.table,
            cwd=Path.cwd(),
            backend=backend,
            fractional_bits=args.fractional_bits,
            bundle=block,
        )
        _write(out, result.canonical)
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
    _report(cprint_literal, result, out, answered_block=block is not None)
    return 0


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


def _as_refusal(exc: Exception, source: str) -> Any:
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
            {"source": sources.describe_source(source), "written": "nothing"},
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


def _out_path(given: Optional[str], expose_id: str, source: str) -> Path:
    """Where the document goes: ``--out``, or ``<exposeId>.profile.json`` here.

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
    from fluid_build.profiler.sources import local_file

    if given:
        target = Path(given)
    elif _PLAIN_NAME.match(expose_id) and expose_id not in (".", ".."):
        target = Path(f"{expose_id}.profile.json")
    else:
        raise ProfileError(
            "profile_out_required",
            f"exposeId {expose_id!r} cannot name a file",
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
        read_from = local_file(source)
        if read_from is not None and read_from.exists() and unresolved.exists():
            if unresolved.samefile(read_from):
                raise ProfileError(
                    "profile_out_is_the_source",
                    f"{unresolved} is the file the records are read from",
                    {"next": "name another file with --out"},
                )
    except OSError as exc:
        raise ProfileError(
            "profile_out_unusable",
            f"{unresolved} cannot be used: {exc.strerror or type(exc).__name__}",
        ) from None
    return unresolved


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


def _one_code_withheld(result: Any) -> bool:
    """Whether a histogram or a quantile, whose withheld values all read C, has one."""
    from fluid_build.profiler.disclosure import ONE_CODE

    return any(
        each[1].partition("/")[0] in ONE_CODE
        for each in (*result.withheld_statistics, *result.partly_withheld_statistics)
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


def _report(say: Any, result: Any, out: Path, *, answered_block: bool = False) -> None:
    """Tell the owner what the document holds, then print the pin."""
    plan = result.plan
    withheld: Dict[str, int] = dict(result.withheld)
    say(
        f"Profiled exposes[{plan.expose_id}]: {_plural(len(plan.columns), 'column')}, "
        f"{_plural(result.published, 'value')} published, {sum(withheld.values())} withheld."
    )
    if withheld:
        say(f"Withheld, by the check (CL_CONF_STATUS code): {_codes(withheld)}.")
    cells: Dict[str, int] = dict(result.withheld_cells)
    if cells:
        say(f"Withheld histogram cells, by the check (CL_CONF_STATUS code): {_codes(cells)}.")
    if cells or _one_code_withheld(result):
        say(
            "The document writes C for every withheld group and cell of a histogram or "
            "a quantile, whatever withheld it, so its code says nothing of its size.",
            soft_wrap=True,
        )
    named = _named(result.statistics)
    whole = _named(result.withheld_statistics)
    if named:
        grid = (
            f"on the fidelity block's fractionalBits {result.fractional_bits}"
            if answered_block
            else f"on fractionalBits {result.fractional_bits} "
            "(the fidelity block must declare the same)"
        )
        say(f"Fidelity statistics: {'; '.join(named)}, {grid}.", soft_wrap=True)
    if whole:
        say(
            "Withheld whole, so a run that checks them scores them censored: "
            f"{'; '.join(whole)}.",
            soft_wrap=True,
        )
    partly = _named_partly(result.partly_withheld_statistics)
    if partly:
        say(
            "Partly withheld, so a run that checks them scores only the published groups: "
            f"{'; '.join(partly)}.",
            soft_wrap=True,
        )
    for description, reason in result.left_out:
        say(
            f"Not answered: {description}: {reason}; a run that checks it is refused "
            "(FLX-5037).",
            soft_wrap=True,
        )
    for name, reason in plan.skipped:
        say(f"Not profiled: column {name!r}: {reason}.")
    if not result.published:
        say("Every value was withheld: the document says nothing about the records.")
    say(f"Wrote {out} (readable by you alone; read it before you share it).", soft_wrap=True)
    say("Pin it in the FLUX Simulation:")
    # One line, never wrapped: it is copied as it stands.
    say(f"  profile: {{digest: {result.digest}}}", soft_wrap=True)
