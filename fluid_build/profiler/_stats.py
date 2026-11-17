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

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Callable, Dict, FrozenSet, Protocol, Sequence, Tuple, runtime_checkable

__all__ = [
    "DocumentRefused",
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


#: The most digits a number may need written out without an exponent: the
#: engine's declared-number rule (flux-engine ``concordance/predicate.py``
#: ``require_number_length``), which flux-stats' ``exact()`` takes too in a
#: follow-up to flux-engine #54. Drop this pre-check once the pin carries it.
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


def load_backend() -> StatisticsBackend:
    """Load FLUX's statistic registry, the ``flux-stats`` distribution.

    Raises :class:`StatisticsUnavailable`, before any record is read, when it
    is not installed or cannot be imported.
    """
    try:
        return _FluxStatsBackend()
    except ImportError as error:
        raise _unavailable(error) from None


#: The one pin of flux-stats, until it is released. It is not in ``[profile]``: PyPI
#: refuses an upload whose metadata holds a URL. ``requirements/flux-stats.txt`` holds the
#: same line for CI; ``tests/profiler/test_flux_stats_pin.py`` keeps the two equal.
FLUX_STATS_GIT_REQUIREMENT = (
    "flux-stats @ git+https://github.com/Agenticstiger/flux-engine.git"
    "@9ea8edf9bcddd58951f418db13a77253c02bdd7d#subdirectory=packages/flux-stats"
)
FLUX_STATS_INSTALL_COMMAND = f'pip install "{FLUX_STATS_GIT_REQUIREMENT}"'


def _unavailable(error: ImportError) -> StatisticsUnavailable:
    """Say whether ``flux-stats`` is missing, or installed and not importable.

    PyPI's ``flux-stats`` 0.0.1 is a placeholder that installs no ``flux_stats``
    module, so ``pip install flux-stats`` leaves a distribution that cannot be
    imported. The message names its version rather than calling it missing.
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
        f"FLUX shares with its fidelity check, and flux-stats {state}. flux-stats is not on "
        f"PyPI yet; install it from git with: {FLUX_STATS_INSTALL_COMMAND}"
    )


def _read_refused(error: Any, column: str, row_index: int) -> ReadRefused:
    """A :class:`ReadRefused` that names the cell once, then flux-stats' reason and code.

    flux-stats names the cell in a ``FLX-5005`` detail but not in an RFC 3339
    one (``FLX-5022``), and never the value (KERNEL-SPEC §9), so neither does this.
    """
    subject = f"column {column!r} row {row_index}"
    reason = error.detail.removeprefix(f"{subject}: ") or error.code.title
    return ReadRefused(f"{subject}: {reason} ({error.code.code})")
