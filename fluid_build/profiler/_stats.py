# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The one seam between ``fluid profile`` and FLUX's statistic registry.

MEAS-3 asks for one implementation of every statistic, shared by the profiler
and the fidelity check, so that real and synthetic numbers are formed the same
way. FLUX keeps that implementation in its registry (``flux_engine.stats`` today,
the ``flux-stats`` distribution once the split lands): one definition per
statistic, with the pure function that forms it and its canonical spelling. The
founder decided on 2026-11-16 that the record reader's pure half moves there too
(decimal and RFC 3339 parsing, NFC, the refusal of binary floats), so the profiler
and the check also read a value one way.

So this package forms nothing and reads no value itself. It fetches rows
(:mod:`~fluid_build.profiler.sources`), hands every value to
:meth:`StatisticsBackend.read_value`, forms every metric with the registry's
definition, and has the registry read the finished document back before it is
written (:meth:`StatisticsBackend.check_document`).

This module is the only one in forge-cli that may import the registry. The
pattern is Deequ's (one analyzer per metric, run by both its profiler and its
verification suite), as flux-engine's registry itself records.

Not wired yet: the split's distribution and module names are not final, so
:func:`load_backend` raises :class:`StatisticsUnavailable`. Wiring it is one
change here (and ``flux-stats`` joining the ``profile`` extra); everything above
this seam takes a :class:`StatisticsBackend` and is tested with one.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, FrozenSet, Protocol, Sequence, Tuple, runtime_checkable

__all__ = [
    "DocumentRefused",
    "MetricDefinition",
    "ReadRefused",
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

    The shape of ``flux_engine.stats.registry.MetricDefinition``: ``measure``
    takes the column's values as read (``None`` for a null) and the declared
    quantile levels; ``spell`` returns the value's canonical spelling,
    ``{"value": ...}`` or ``{"values": {...}}``.
    """

    identifier: str
    name: str
    domains: FrozenSet[str]

    def measure(self, values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
        """The metric over *values* (``None`` for a null), at the quantile *levels*."""

    def spell(self, value: Any) -> Dict[str, Any]:
        """The canonical spelling of a value :meth:`measure` returned."""


@runtime_checkable
class StatisticsBackend(Protocol):
    """What ``fluid profile`` needs from the statistic registry, and nothing more."""

    def metrics(self) -> Tuple[MetricDefinition, ...]:
        """Every registered column metric."""

    def read_value(self, domain: str, value: Any, *, column: str, row_index: int) -> Any:
        """Read one raw value of a *domain* column as the fidelity check reads it.

        A number becomes an exact rational, a timestamp integer milliseconds
        since the epoch, a category an NFC string, and a null ``None``. Raises
        :class:`ReadRefused` for a value the reader refuses (a binary float,
        a timestamp with no offset, ...).
        """

    def check_document(self, text: str) -> None:
        """Read *text* back as a profile document; raise :class:`DocumentRefused` if refused."""


def load_backend() -> StatisticsBackend:
    """Load the statistic registry.

    Raises :class:`StatisticsUnavailable` until the ``flux-stats`` distribution
    is published and wired here.
    """
    raise StatisticsUnavailable(
        "fluid profile forms every statistic with flux-stats, the statistic registry "
        "FLUX shares with its fidelity check, and flux-stats is not published yet"
    )
