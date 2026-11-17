# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile``: an output-checked profile of one expose, read where its records live.

The command reads the expose's records once, inside the environment that holds
them, and writes a FLUX profile document (PROFILE-SPEC format 1-0-0) of
aggregates that passed the disclosure rules. A FLUX Simulation pins the document
by the digest this prints. See :mod:`fluid_build.profiler` for the parts.

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

The document follows FLUX PROFILE-SPEC (format 1-0-0) and holds only aggregates
that passed the output checks: no record, no sample, no per-record value. The
checks are ACRO 1.1.0's defaults, named "acro-default" in the document:
  - a count below 10 records is withheld, and so is a zero;
  - every published count is rounded to a multiple of 5;
  - a sum is withheld when 2 records make up 90% of it, or when what is left
    after the 2 largest is below 10% of the largest;
  - a min, max or quantile is published only when 10 records hold that value;
  - a mean is published only when the column's count and sum are.
A withheld value keeps its SDMX CL_CONF_STATUS code and no value: A (a small
count), C (a sum over at most two values), O or T (dominated by one or two
records) or M (concentration).

For each number column the document also holds mean/1, the statistic the FLUX
fidelity check compares a mean property with: the number of values and their
sum on a grid of 2^-N steps. N is --fractional-bits, 16 unless given, and the
fidelity block's encoding.fractionalBits must be the same.

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
# when 10 records hold it (ACRO 1.1.0 defaults). Output-checking conventions only: no
# privacy or anonymity guarantee. Pin the printed digest in the FLUX Simulation.
# Each number column also gets mean/1, which FLUX compares a mean property with, on the
# grid --fractional-bits names (16 unless given): the fidelity block must declare it.
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
        default=_DEFAULT_FRACTIONAL_BITS,
        metavar="N",
        help=(
            "the grid mean/1 is formed on, 2^-N steps: the fidelity block's "
            f"encoding.fractionalBits (default: {_DEFAULT_FRACTIONAL_BITS}, FLUX's default; "
            f"0 to {_MAX_FRACTIONAL_BITS})"
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
    from fluid_build.profiler import _stats, runner, sources
    from fluid_build.profiler.document import DocumentError

    try:
        backend = _stats.load_backend()
    except _stats.StatisticsUnavailable as exc:
        raise _refusal(
            "profile_statistics_unavailable",
            str(exc),
            {
                "read": "nothing: no record was read",
                "next": f"pip install 'data-product-forge[profile]' && {_stats.FLUX_STATS_INSTALL_COMMAND}",
            },
        ) from None

    contract_path = validate_cli_path(args.contract, mode="read", file_type="contract")
    contract = load_contract_with_overlay(str(contract_path), args.env, logger)
    try:
        expose_id = runner.resolve_expose_id(contract, args.expose_id)
        out = _out_path(args.out, expose_id, args.source)
        result = runner.profile_expose(
            contract,
            expose_id,
            source=args.source,
            table=args.table,
            cwd=Path.cwd(),
            backend=backend,
            fractional_bits=args.fractional_bits,
        )
        _write(out, result.canonical)
    except (
        runner.ProfileError,
        sources.SourceError,
        _stats.ReadRefused,
        DocumentError,
        _stats.DocumentRefused,
    ) as exc:
        raise _as_refusal(exc, args.source) from None
    _report(cprint_literal, result, out)
    return 0


def _as_refusal(exc: Exception, source: str) -> Any:
    """The :class:`CLIError` for a refusal from the profiler; nothing has been written."""
    from fluid_build.profiler import _stats, runner, sources

    if isinstance(exc, runner.ProfileError):
        return _refusal(exc.event, str(exc), exc.context)
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
    "M": "concentration",
    "O": "dominated by one record",
    "T": "dominated by two records",
}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _report(say: Any, result: Any, out: Path) -> None:
    """Tell the owner what the document holds, then print the pin."""
    plan = result.plan
    withheld: Dict[str, int] = dict(result.withheld)
    say(
        f"Profiled exposes[{plan.expose_id}]: {_plural(len(plan.columns), 'column')}, "
        f"{_plural(result.published, 'value')} published, {sum(withheld.values())} withheld."
    )
    if withheld:
        codes = ", ".join(
            f"{code} ({_CODE_MEANING.get(code, 'withheld')}) {count}"
            for code, count in withheld.items()
        )
        say(f"Withheld, by CL_CONF_STATUS code: {codes}.")
    by_definition: Dict[str, List[str]] = {}
    for column, identifier in result.statistics:
        by_definition.setdefault(identifier, []).append(repr(column))
    if by_definition:
        published = "; ".join(
            f"{identifier} of {', '.join(columns)}" for identifier, columns in by_definition.items()
        )
        say(
            f"Fidelity statistics: {published}, on fractionalBits {result.fractional_bits}"
            " (the fidelity block must declare the same).",
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
