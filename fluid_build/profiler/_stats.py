# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The one seam between ``fluid profile`` and FLUX's statistic registry.

The profiler and the fidelity check share one implementation of every statistic, so that real and synthetic numbers are formed the same
way. FLUX keeps that implementation in ``flux-stats``, the registry flux-engine
releases as a distribution of its own (ADR 0009): one definition per statistic,
with the pure function that forms it and its canonical spelling. The record
reader's pure half lives there too (``flux_stats.reading``: decimal and RFC 3339
parsing, NFC, the refusal of binary floats), so the profiler and the check also
read a value one way.

So this package forms nothing and reads no value itself. It fetches rows
(:mod:`~fluid_build.profiler.sources`), hands every value to
:meth:`StatisticsBackend.read_value`, places it on the grid with
:meth:`StatisticsBackend.encoder` where a statistic needs one, forms every metric
and statistic with the registry's definition, and has the registry read the
finished document back before it is written
(:meth:`StatisticsBackend.check_document`).

This module is the only one in forge-cli that may import the registry, and it
imports it only when a profile runs (:func:`load_backend`), so ``fluid --help``
never loads it. The pattern is Deequ's (one analyzer per metric, run by both its
profiler and its verification suite), as flux-stats' registry itself records.
Everything above this seam takes a :class:`StatisticsBackend`. The tests run it
over flux-stats and over a stand-in (``tests/profiler/_fake_backend.py``) that
writes the same bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import partial
from importlib import import_module
from types import SimpleNamespace
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

__all__ = [
    "BlockRefused",
    "DeclaredBlock",
    "DeclaredStatistic",
    "DocumentRefused",
    "MeasureRefused",
    "MeasuredGroup",
    "MeasuredStatistic",
    "MetricDefinition",
    "ReadRefused",
    "StatisticDefinition",
    "StatisticsBackend",
    "StatisticsUnavailable",
    "load_backend",
]


class StatisticsUnavailable(Exception):
    """The statistic registry cannot be loaded, so nothing is read."""


class ReadRefused(ValueError):
    """The registry's reader refused a value.

    The message names the column, the row and the value's class, never the
    value: a refusal is printed in the owner's terminal and can travel from there.
    """


class DocumentRefused(ValueError):
    """The registry's profile reader refused a document the profiler built.

    It means the two disagree about the format, so nothing is written. The
    message names where in the document, never a value.
    """


class BlockRefused(ValueError):
    """The registry's parser refused the fidelity block a Simulation declares.

    ``code`` is the ``FLX-nnnn`` code and the message the registry's own words
    for it, which never quote a record value (the block holds none).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class MeasureRefused(ValueError):
    """The registry refused a record while forming a declared statistic.

    A null under ``null: fail``, a value outside the declared range or support,
    more distinct members than ``maxCardinality``, a column the table lacks. The
    message names the column and the row, never the value.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class DeclaredStatistic:
    """One statistic the fidelity block asks the profile to carry.

    ``shape`` holds the document keys that say which statistic it is beyond its
    definition and column: ``{"bins": [...]}`` (the declared edges, as decimal
    strings), ``{"levels": [...]}`` (the declared levels in cell order),
    ``{"level": "0.5"}`` or nothing. ``columns`` is every column it reads, the
    ``groupBy`` column included, and ``read_as`` the domain each is read in.
    """

    identifier: str
    kind: str
    column: str
    group_by: Optional[str]
    shape: Mapping[str, Any]
    columns: Tuple[str, ...]
    read_as: Mapping[str, str]
    #: The registry's parsed declaration; only the registry's own seam reads it.
    _declaration: Any = field(default=None, repr=False, compare=False)

    @property
    def description(self) -> str:
        """The statistic as a person names it: ``mean/1 of 'amount' by 'market'``."""
        by = f" by {self.group_by!r}" if self.group_by else ""
        return f"{self.identifier} of {self.column!r}{by}"


@dataclass(frozen=True)
class DeclaredBlock:
    """What the fidelity block asks of a profile, read by the registry's own parser.

    ``left_out`` names each declared property that is not answered, with the
    reason. The parsed block stays private to this seam, which measures with it.
    """

    fractional_bits: int
    statistics: Tuple[DeclaredStatistic, ...]
    left_out: Tuple[Tuple[str, str], ...] = ()
    _block: Any = field(default=None, repr=False, compare=False)

    @property
    def columns(self) -> Tuple[str, ...]:
        """Every column any declared statistic reads, sorted."""
        return tuple(sorted({name for each in self.statistics for name in each.columns}))


@dataclass(frozen=True)
class MeasuredGroup:
    """One group of a declared statistic, formed from its exact records.

    ``document`` is the group as the registry spells it (``group``, ``k`` and the
    statistic's quantities), with every count exact and no ``status``. The other
    fields hold what the disclosure rules read: ``cells`` (a histogram's bin or
    level counts), ``member_holders`` (the records each member of a support is
    held by), ``quantile`` (witness, records strictly below it, records at or
    below it) and, for a mean, the group's exact values and their grid points.
    """

    key: str
    document: Dict[str, Any]
    k: int
    cells: Tuple[int, ...] = ()
    member_holders: Tuple[int, ...] = ()
    quantile: Optional[Tuple[Optional[int], int, int]] = None
    values: Tuple[Fraction, ...] = ()
    encoded: Tuple[int, ...] = ()


@dataclass(frozen=True)
class MeasuredStatistic:
    """A declared statistic over one table: its groups, and the rows no group holds.

    A ``groupBy`` column read under ``null: exclude`` leaves a row whose key is null
    out of every group, while the column's ungrouped figures (its ``count``, its
    ``sum``, an ungrouped mean) still hold the row's value. ``left_out`` is those rows
    measured as one more group, never written, and ``left_out_unread`` says they
    could not be read as the block reads them. A grouped mean or support checks
    them as a group, so its published groups cannot give them back.
    """

    groups: Tuple[MeasuredGroup, ...]
    left_out: Optional[MeasuredGroup] = None
    left_out_unread: bool = False


@runtime_checkable
class MetricDefinition(Protocol):
    """One registered column metric (``count/1`` ... ``quantiles/1``).

    The shape of ``flux_stats.registry.MetricDefinition``, a frozen dataclass, so
    its attributes are read-only here too (a settable protocol attribute would not
    match it). ``measure`` takes the column's values as read (``None`` for a null)
    and the declared quantile levels; ``spell`` returns the value's canonical
    spelling, ``{"value": ...}`` or ``{"values": {...}}``.
    """

    @property
    def identifier(self) -> str:
        """``<name>/<major>``, for example ``count/1``."""

    @property
    def name(self) -> str:
        """The OpenLineage name, for example ``nullCount``."""

    @property
    def domains(self) -> FrozenSet[str]:
        """The column domains it applies to."""

    def measure(self, values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
        """The metric over *values* (``None`` for a null), at the quantile *levels*."""

    def spell(self, value: Any) -> Dict[str, Any]:
        """The canonical spelling of a value :meth:`measure` returned."""


@runtime_checkable
class StatisticDefinition(Protocol):
    """One registered statistic the fidelity check compares (``mean/1`` ...).

    A ``flux_stats.registry.StatisticDefinition`` measured on no declared shape:
    no bin edges, no levels, no quantile level, which is all an ungrouped
    ``mean/1`` needs. ``measure`` takes one group's values as the registry's own
    measure takes them (for an on-grid statistic, the values
    :meth:`StatisticsBackend.encoder` placed on the grid) and returns the group
    as the fidelity check's ``S`` spells it: ``group``, ``k``, then the
    definition's quantities.
    """

    @property
    def identifier(self) -> str:
        """``<name>/<major>``, for example ``mean/1``."""

    def measure(self, group: str, values: Sequence[Any]) -> Dict[str, Any]:
        """One group's canonical spelling, ``{"group": ..., "k": ..., ...}``."""


@runtime_checkable
class StatisticsBackend(Protocol):
    """What ``fluid profile`` needs from the statistic registry, and nothing more."""

    def metrics(self) -> Tuple[MetricDefinition, ...]:
        """Every registered column metric."""

    def statistics(self) -> Tuple[StatisticDefinition, ...]:
        """Every registered statistic, measured on no declared shape."""

    def encoder(self, fractional_bits: int) -> Callable[[Fraction], int]:
        """The grid of ``2**-fractional_bits`` steps: an exact value to its grid point.

        It rounds toward -infinity, as the fidelity check encodes the values it
        forms ``mean/1`` from (KERNEL-SPEC §12): ``floor(value * 2**fractional_bits)``.
        """

    def read_value(self, domain: str, value: Any, *, column: str, row_index: int) -> Any:
        """Read one raw value of a *domain* column as the fidelity check reads it.

        A number becomes an exact rational, a timestamp integer milliseconds
        since the epoch, a category an NFC string, and a null ``None``. Raises
        :class:`ReadRefused` for a value the reader refuses (a binary float,
        a timestamp with no offset, ...).
        """

    def check_document(self, text: str) -> None:
        """Read *text* back as a profile document; raise :class:`DocumentRefused` if refused."""

    def read_block(
        self, raw_block: Any, contract_document: Mapping[str, Any], expose_id: str
    ) -> DeclaredBlock:
        """Parse a Simulation's fidelity block against the contract's expose.

        Raises :class:`BlockRefused` for a block the grammar refuses, and
        :class:`StatisticsUnavailable` when the registry has no grammar.
        """

    def measure(
        self,
        block: DeclaredBlock,
        statistic: DeclaredStatistic,
        rows: Sequence[Mapping[str, Any]],
    ) -> MeasuredStatistic:
        """Form *statistic* over *rows* (column to raw value), one group per key.

        The rows a ``groupBy`` null leaves out of every group are measured too
        (:attr:`MeasuredStatistic.left_out`). Raises :class:`MeasureRefused` for a
        record the block's declaration refuses.
        """

    def quantile_ranks_ok(self, k: int, level: Fraction, below: int, at_or_below: int) -> bool:
        """Whether a witness with these ranks, over *k* records, is the *level* quantile."""


#: The most digits a number may need written out without an exponent: the
#: engine's declared-number rule (flux-engine ``concordance/predicate.py``
#: ``require_number_length``), which flux-stats' ``exact()`` takes too since
#: 0.6.0, so this pre-check can be dropped.
#: The fidelity check reads values inside a declared range; a profile declares
#: none, so the seam bounds a number before flux-stats builds its exact value:
#: building ``1E+99999999`` takes minutes and gigabytes. A sum longer than the
#: 1,000 digits flux-stats spells, or a value below 1 with 1,000 fraction digits
#: (spelled with its leading zero), is refused by the document check instead,
#: before anything is written.
_MAX_DIGITS = 1000
_INT_LIMIT: int = 10**_MAX_DIGITS  # annotated: int ** int is Any to mypy


def _too_many_digits(value: Any) -> bool:
    """Whether *value* is a finite number needing more than :data:`_MAX_DIGITS` digits.

    Counted as flux-stats counts, without building the number: an int by its
    size, a decimal by its coefficient and exponent, a zero as one digit.
    Anything that is not a finite number is left to flux-stats' reader, which
    refuses it by its class.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) >= _INT_LIMIT
    if isinstance(value, str):
        try:
            value = Decimal(value)
        except InvalidOperation:
            return False
    if not isinstance(value, Decimal):
        return False
    _, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int) or not any(digits):  # NaN, infinity or zero
        return False
    span = len(digits) + exponent if exponent >= 0 else max(len(digits), -exponent)
    return span > _MAX_DIGITS


@dataclass(frozen=True)
class _Column:
    """A column as flux-stats' value readers take one.

    ``read_exact`` reads its name and domain (``flux_stats.reading.ValueColumn``)
    and ``category`` its name alone (``NamedColumn``). Neither reads a declared
    range or support, which a profile does not have: checking a value against a
    declared range is the fidelity check's step (``encode_value``).
    """

    name: str
    domain: Any = None


class _Statistic:
    """A :class:`StatisticDefinition` over one of flux-stats' definitions.

    It measures with no declared shape (``flux_stats.Parameters()``) and spells
    the group as the registry's ``GroupStatistic.to_document`` does, so the
    group is the one the fidelity check's ``S`` would write for the same values.
    """

    def __init__(self, definition: Any, parameters: Any) -> None:
        self._definition = definition
        self._parameters = parameters

    @property
    def identifier(self) -> str:
        identifier: str = self._definition.identifier
        return identifier

    def measure(self, group: str, values: Sequence[Any]) -> Dict[str, Any]:
        state = self._definition.measure(group, values, self._parameters)
        spelled: Dict[str, Any] = state.to_document(self._definition.kind)
        return spelled


class _FluxStatsBackend:
    """The :class:`StatisticsBackend` over ``flux-stats``."""

    def __init__(self) -> None:
        # The package first: when it is missing, this is the import that fails,
        # even with one of its submodules still in sys.modules.
        from flux_stats import Parameters, definitions, metrics
        from flux_stats.errors import FluxError
        from flux_stats.fixedpoint import Grid
        from flux_stats.profile import load_profile_text
        from flux_stats.reading import ValueDomain, category, read_exact

        self._metrics = metrics
        self._statistics = tuple(_Statistic(each, Parameters()) for each in definitions())
        self._grid = Grid
        self._refused = FluxError
        self._load_profile_text = load_profile_text
        self._category = category
        self._read_exact = read_exact
        self._value_domain = ValueDomain
        self._columns: Dict[Tuple[str, str], _Column] = {}
        self._grammar: Any = None

    def metrics(self) -> Tuple[MetricDefinition, ...]:
        registered: Tuple[MetricDefinition, ...] = tuple(self._metrics())
        return registered

    def statistics(self) -> Tuple[StatisticDefinition, ...]:
        return self._statistics

    def encoder(self, fractional_bits: int) -> Callable[[Fraction], int]:
        quantise: Callable[[Fraction], int] = self._grid(fractional_bits).quantise
        return quantise

    def read_value(self, domain: str, value: Any, *, column: str, row_index: int) -> Any:
        if value is None:
            return None
        key = (domain, column)
        shape = self._columns.get(key)
        if shape is None:
            # ValueDomain(domain) raises ValueError for a domain with no reader.
            kind = None if domain == "category" else self._value_domain(domain)
            shape = self._columns[key] = _Column(column, kind)
        if domain == "number" and _too_many_digits(value):
            # Worded as flux-stats will word it, and with no digit count: the count
            # would tell the number's magnitude.
            raise ReadRefused(
                f"column {column!r} row {row_index}: value class 'over-long number' (FLX-5005)"
            )
        try:
            if domain == "category":
                return self._category(value, shape, row_index=row_index)
            exact = self._read_exact(value, shape, row_index=row_index)
        except self._refused as error:
            raise _read_refused(error, column, row_index) from None
        # A timestamp reads as a whole number of milliseconds since the epoch.
        return int(exact) if domain == "timestamp" else exact

    def check_document(self, text: str) -> None:
        try:
            self._load_profile_text(text)
        except self._refused as error:
            raise DocumentRefused(str(error)) from None

    def _fidelity(self) -> Any:
        """The registry's fidelity grammar and the readers it measures with, loaded once.

        ``flux_stats.fidelity`` is newer than the rest of the registry (flux-stats
        0.6.1 and older lack it), so it is imported when a ``--bundle`` asks for it.
        """
        if self._grammar is None:
            try:
                # By name, so a flux-stats that lacks the module raises here.
                fidelity = import_module("flux_stats.fidelity")
                reading = import_module("flux_stats.reading")
                from flux_stats.fixedpoint import quantile_rank_ok
                from flux_stats.registry import definition_for_kind
                from flux_stats.spelling import render_exact
            except ImportError as error:
                missing = error.name or "flux_stats.fidelity"
                raise StatisticsUnavailable(
                    "--bundle reads the fidelity block with flux-stats, and the installed "
                    f"flux-stats lacks {missing}. It needs {FLUX_STATS_REQUIREMENT}"
                ) from None
            self._grammar = SimpleNamespace(
                fidelity=fidelity,
                reading=reading,
                quantile_rank_ok=quantile_rank_ok,
                definition_for_kind=definition_for_kind,
                render_exact=render_exact,
            )
        return self._grammar

    def read_block(
        self, raw_block: Any, contract_document: Mapping[str, Any], expose_id: str
    ) -> DeclaredBlock:
        grammar = self._fidelity()
        try:
            expose = grammar.fidelity.expose_from_contract(contract_document, expose_id)
            parsed = grammar.fidelity.parse_fidelity_block(raw_block, expose=expose)
        except self._refused as error:
            raise BlockRefused(error.code.code, error.detail or error.code.title) from None
        except (TypeError, ValueError, AttributeError) as error:
            raise BlockRefused(
                "FLX-5030",
                f"the fidelity block is not in a form the grammar reads ({type(error).__name__})",
            ) from None
        statistics: List[DeclaredStatistic] = []
        left_out: List[Tuple[str, str]] = []
        seen = set()
        for declaration in parsed.properties:
            kind = declaration.kind.value
            if kind == "correlation":
                first, second = declaration.columns
                left_out.append((f"correlation of {first!r} and {second!r}", _CORRELATION_LEFT_OUT))
                continue
            statistic = self._declared(grammar, parsed, declaration)
            key = (
                statistic.identifier,
                statistic.column,
                statistic.group_by,
                repr(sorted(statistic.shape.items())),
            )
            if key not in seen:
                seen.add(key)
                statistics.append(statistic)
        return DeclaredBlock(
            fractional_bits=parsed.encoding.fractional_bits,
            statistics=tuple(statistics),
            left_out=tuple(left_out),
            _block=parsed,
        )

    @staticmethod
    def _declared(grammar: Any, parsed: Any, declaration: Any) -> DeclaredStatistic:
        """One property of the parsed block as the statistic a profile carries for it."""
        kind: str = declaration.kind.value
        column: str = declaration.columns[0]
        group_by: Optional[str] = declaration.group_by
        shape: Dict[str, Any] = {}
        categorical = kind in ("support", "levelHistogram")
        if kind == "marginalHistogram":
            shape["bins"] = [grammar.render_exact(edge) for edge in declaration.bins]
        elif kind == "levelHistogram":
            shape["levels"] = list(grammar.reading.level_cells(parsed.columns[column]))
        elif kind == "quantile":
            shape["level"] = grammar.render_exact(declaration.level)
        names = (column,) if group_by is None else (column, group_by)
        read_as = {column: "category" if categorical else parsed.columns[column].domain.value}
        if group_by is not None:
            read_as[group_by] = "category"
        return DeclaredStatistic(
            identifier=grammar.definition_for_kind(declaration.kind).identifier,
            kind=kind,
            column=column,
            group_by=group_by,
            shape=shape,
            columns=names,
            read_as=read_as,
            _declaration=declaration,
        )

    def measure(
        self,
        block: DeclaredBlock,
        statistic: DeclaredStatistic,
        rows: Sequence[Mapping[str, Any]],
    ) -> MeasuredStatistic:
        grammar = self._fidelity()
        parsed = block._block
        declaration = statistic._declaration
        try:
            by = parsed.columns[statistic.group_by] if statistic.group_by else None
            # A group's key is published even when the group is withheld, so every key
            # must be a level the block declares: a key read from the records would
            # publish a record's value. flux-stats refuses an undeclared key for a level
            # histogram only and groups the other properties by whatever the rows hold;
            # the engine's synthetic rows hold declared levels alone, so a check loses
            # no group it could compare.
            keys = grammar.reading.group_keys(rows, by, declared_only=True)
            groups = tuple(
                self._measured(grammar, parsed, declaration, statistic, rows, key, indices)
                for key, indices in keys
            )
        except self._refused as error:
            raise MeasureRefused(error.code.code, error.detail or error.code.title) from None
        grouped = {index for _, indices in keys for index in indices}
        # A row that holds a value of the statistic's column and is in no group: the
        # column's ungrouped figures hold it, and the groups do not.
        outside = [
            index
            for index, row in enumerate(rows)
            if index not in grouped and row.get(statistic.column) is not None
        ]
        if not outside:
            return MeasuredStatistic(groups)
        try:
            left_out = self._measured(grammar, parsed, declaration, statistic, rows, "", outside)
        except self._refused:
            # Never written, so not refused either: the profile cannot vouch for rows
            # the block's reading refuses, and the caller withholds the groups.
            return MeasuredStatistic(groups, left_out_unread=True)
        return MeasuredStatistic(groups, left_out)

    @staticmethod
    def _measured(
        grammar: Any,
        parsed: Any,
        declaration: Any,
        statistic: DeclaredStatistic,
        rows: Sequence[Mapping[str, Any]],
        key: str,
        indices: Sequence[int],
    ) -> MeasuredGroup:
        state = grammar.fidelity.measure_group(
            rows, indices, key, declaration, parsed.columns, parsed.grid
        )
        kind = statistic.kind
        spelled: Dict[str, Any] = state.to_document(kind)
        group = MeasuredGroup(key=key, document=spelled, k=state.k)
        if kind in ("marginalHistogram", "levelHistogram"):
            return replace(group, cells=tuple(state.cell_counts(kind)))
        if kind == "support":
            return replace(group, member_holders=tuple(state.support_counts))
        if kind == "quantile":
            ranks = (state.quantile_witness, state.quantile_below, state.quantile_at_or_below)
            return replace(group, quantile=ranks)
        # A mean: the same indices, read as the block reads them, so the null policy
        # and the range check are the block's.
        column = parsed.columns[statistic.column]
        reading = grammar.reading
        values = reading.read_column(
            rows, indices, column, partial(reading.read_exact, column=column)
        )
        encoded = reading.read_column(
            rows, indices, column, partial(reading.encode_value, column=column, grid=parsed.grid)
        )
        return replace(group, values=tuple(values), encoded=tuple(encoded))

    def quantile_ranks_ok(self, k: int, level: Fraction, below: int, at_or_below: int) -> bool:
        grammar = self._fidelity()
        try:
            proved: bool = grammar.quantile_rank_ok(
                count=k, level=level, count_strictly_below=below, count_at_or_below=at_or_below
            )
        except self._refused:
            return False
        return proved


#: Why a correlation is not answered (founder decision, 2026-11-18).
_CORRELATION_LEFT_OUT = (
    "a correlation's moments need the exact record count, which the output checks round"
)


def load_backend() -> StatisticsBackend:
    """Load FLUX's statistic registry, the ``flux-stats`` distribution.

    Raises :class:`StatisticsUnavailable`, before any record is read, when it
    is not installed or cannot be imported.
    """
    try:
        return _FluxStatsBackend()
    except ImportError as error:
        raise _unavailable(error) from None


#: The flux-stats range ``[profile]`` names in ``pyproject.toml``. The CLI cannot read
#: that file once installed, so the install message repeats it;
#: ``tests/profiler/test_flux_stats_pin.py`` keeps the two equal.
FLUX_STATS_REQUIREMENT = "flux-stats>=0.6.2,<0.7"
#: The extra carries the range, and pip replaces a copy outside it (the 0.0.1
#: placeholder, or a release newer than this module knows).
FLUX_STATS_INSTALL_COMMAND = "pip install 'data-product-forge[profile]'"


def _unavailable(error: ImportError) -> StatisticsUnavailable:
    """Say whether ``flux-stats`` is missing, or installed and not importable.

    PyPI's ``flux-stats`` 0.0.1 is a placeholder that installs no ``flux_stats``
    module, so a copy of it leaves a distribution that cannot be imported. The
    message names the installed version rather than calling it missing.
    """
    from importlib import metadata

    try:
        version = metadata.version("flux-stats")
    except metadata.PackageNotFoundError:
        state = "is not installed"
    else:
        state = f"{version} is installed but cannot be imported ({type(error).__name__})"
    return StatisticsUnavailable(
        "fluid profile forms every statistic with flux-stats, the statistic registry "
        f"FLUX shares with its fidelity check, and flux-stats {state}. It needs "
        f"{FLUX_STATS_REQUIREMENT}; install it with: {FLUX_STATS_INSTALL_COMMAND}"
    )


def _read_refused(error: Any, column: str, row_index: int) -> ReadRefused:
    """A :class:`ReadRefused` that names the cell once, then flux-stats' reason and code.

    flux-stats names the cell in a ``FLX-5005`` detail but not in an RFC 3339
    one (``FLX-5022``), and never the value (KERNEL-SPEC §9), so neither does this.
    """
    subject = f"column {column!r} row {row_index}"
    reason = error.detail.removeprefix(f"{subject}: ") or error.code.title
    return ReadRefused(f"{subject}: {reason} ({error.code.code})")
