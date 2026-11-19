# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A stand-in for the flux-stats registry, for the tests of ``fluid profile``.

It does what flux-stats does at flux-engine main 9ea8edf9 (``packages/flux-stats``):
the seven column metrics of ``column_metrics.py``, the order-statistic index of
``fixedpoint.order_statistic_index`` (``ceil(level * n)`` clamped to ``[1, n]``),
the exact spelling of ``spelling.render_exact``, the value reading of
``reading.py`` (``exact``, ``parse_rfc3339_ms``, ``nfc``), the grid of
``fixedpoint.Grid.quantise`` (``floor(value * 2**bits)``) and the ``mean/1`` group
of ``definitions.py`` (``k`` and ``Sx``). ``check_document`` validates against the
vendored ``profile-1.schema.json``.

``tests/profiler/test_flux_stats_backend.py`` runs the same profiles through the
real registry wherever flux-stats is installed, and ``[dev]`` installs it in every
CI lane, so this double cannot drift from it unnoticed.

The fidelity grammar has no stand-in: it is flux-stats' own (ADR 0009), and the
profile every run answers without ``--bundle`` is a fidelity block the profiler
declares from the contract (``fluid_build/profiler/defaults.py``). So ``read_block``,
``measure``, ``check_edge_counts`` and ``enum_levels`` hand the work to flux-stats.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Sequence, Tuple

from fluid_build.profiler._stats import BundleTextRefused, DocumentRefused, ReadRefused

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA_PATH = FIXTURES / "profile-1.schema.json"
#: flux-engine docs/spec/profile-1.schema.json, the same bytes at 2205253 and 9ea8edf9.
# pragma: allowlist nextline secret
SCHEMA_SHA256 = "fde1dfedd35a20556643ca9102c6886de3982b8761e8730d70d46fdc7c360674"

_EVERY = frozenset({"category", "number", "timestamp"})
_ORDERED = frozenset({"number", "timestamp"})
_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?P<fraction>\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})\Z"
)


def render_exact(value: Fraction) -> str:
    reduced = Fraction(value)
    residue, twos, fives = reduced.denominator, 0, 0
    while residue % 2 == 0:
        residue //= 2
        twos += 1
    while residue % 5 == 0:
        residue //= 5
        fives += 1
    if residue != 1:
        raise ValueError("not a terminating decimal")
    scale = max(twos, fives)
    scaled = reduced.numerator * 2 ** (scale - twos) * 5 ** (scale - fives)
    sign = "-" if scaled < 0 else ""
    digits = str(abs(scaled)).rjust(scale + 1, "0")
    return f"{sign}{digits}" if scale == 0 else f"{sign}{digits[:-scale]}.{digits[-scale:]}"


def order_statistic_index(count: int, level: Fraction) -> int:
    index = -((-level.numerator * count) // level.denominator)
    return min(max(index, 1), count)


@dataclass(frozen=True)
class FakeMetric:
    identifier: str
    name: str
    domains: FrozenSet[str]
    _measure: Callable[[List[Any], Tuple[Fraction, ...]], Any]
    _spell: Callable[[Any], Dict[str, Any]]

    def measure(self, values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
        return self._measure(list(values), levels)

    def spell(self, value: Any) -> Dict[str, Any]:
        return self._spell(value)


def _present(values: List[Any]) -> List[Any]:
    return [value for value in values if value is not None]


def _quantiles(values: List[Any], levels: Tuple[Fraction, ...]) -> Any:
    ordered = sorted(_present(values))
    if not ordered:
        return {}
    return {level: ordered[order_statistic_index(len(ordered), level) - 1] for level in levels}


def _min(values: List[Any], _levels: Tuple[Fraction, ...]) -> Any:
    present = _present(values)
    return min(present) if present else None


def _max(values: List[Any], _levels: Tuple[Fraction, ...]) -> Any:
    present = _present(values)
    return max(present) if present else None


def _count(value: Any) -> Dict[str, Any]:
    return {"value": value}


def _decimal(value: Any) -> Dict[str, Any]:
    return {"value": render_exact(Fraction(value))}


def _spell_quantiles(value: Any) -> Dict[str, Any]:
    return {"values": {render_exact(k): render_exact(Fraction(x)) for k, x in value.items()}}


METRICS = (
    FakeMetric("count/1", "count", _EVERY, lambda v, _l: len(_present(v)), _count),
    FakeMetric("nullCount/1", "nullCount", _EVERY, lambda v, _l: sum(x is None for x in v), _count),
    FakeMetric(
        "distinctCount/1", "distinctCount", _EVERY, lambda v, _l: len(set(_present(v))), _count
    ),
    FakeMetric(
        "sum/1", "sum", frozenset({"number"}), lambda v, _l: sum(_present(v), Fraction(0)), _decimal
    ),
    FakeMetric("min/1", "min", _ORDERED, _min, _decimal),
    FakeMetric("max/1", "max", _ORDERED, _max, _decimal),
    FakeMetric("quantiles/1", "quantiles", _ORDERED, _quantiles, _spell_quantiles),
)


@dataclass(frozen=True)
class FakeStatistic:
    identifier: str
    _spell: Callable[[List[Any]], Dict[str, Any]]

    def measure(self, group: str, values: Sequence[Any]) -> Dict[str, Any]:
        return {"group": group, "k": len(values), **self._spell(list(values))}


STATISTICS = (FakeStatistic("mean/1", lambda units: {"Sx": str(sum(units))}),)


def _exact(value: Any, where: str) -> Fraction:
    if isinstance(value, (bool, float)):
        raise ReadRefused(f"{where}: value class {type(value).__name__!r}")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, (Decimal, str)):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise ReadRefused(f"{where}: value class 'non-numeric string'") from None
        if not parsed.is_finite():
            raise ReadRefused(f"{where}: value class 'non-finite Decimal'")
        return Fraction(parsed)
    raise ReadRefused(f"{where}: value class {type(value).__name__!r}")


def _milliseconds(value: Any, where: str) -> int:
    if not isinstance(value, str) or not _RFC3339.match(value):
        raise ReadRefused(f"{where}: not an RFC 3339 timestamp with an offset")
    fraction = _RFC3339.match(value).group("fraction")
    if fraction and len(fraction) > 4 and set(fraction[4:]) != {"0"}:
        raise ReadRefused(f"{where}: sub-millisecond precision")
    moment = datetime.fromisoformat(value.replace("z", "Z").replace("Z", "+00:00"))
    delta = moment.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400_000) + (delta.seconds * 1000) + delta.microseconds // 1000


def _refuse_float(_text: str) -> Any:
    raise DocumentRefused("a binary float")


class FakeBackend:
    """A :class:`fluid_build.profiler._stats.StatisticsBackend` for the tests."""

    def __init__(self) -> None:
        self.checked: List[str] = []
        self._registry: Any = None

    def _real(self) -> Any:
        if self._registry is None:
            # Built directly: a test that puts this double in place of load_backend would
            # otherwise get this double back.
            from fluid_build.profiler._stats import _FluxStatsBackend

            self._registry = _FluxStatsBackend()
        return self._registry

    def enum_levels(self, declared_type: str) -> Tuple[str, ...]:
        levels: Tuple[str, ...] = self._real().enum_levels(declared_type)
        return levels

    def read_block(self, raw_block: Any, contract_document: Any, expose_id: str) -> Any:
        return self._real().read_block(raw_block, contract_document, expose_id)

    def measure(self, block: Any, statistic: Any, rows: Any) -> Any:
        return self._real().measure(block, statistic, rows)

    def check_edge_counts(self, statistics: Any, **checks: Any) -> Any:
        return self._real().check_edge_counts(statistics, **checks)

    def metrics(self) -> Tuple[FakeMetric, ...]:
        return METRICS

    def statistics(self) -> Tuple[FakeStatistic, ...]:
        return STATISTICS

    def encoder(self, fractional_bits: int) -> Callable[[Fraction], int]:
        return lambda value: (value.numerator << fractional_bits) // value.denominator

    def read_value(self, domain: str, value: Any, *, column: str, row_index: int) -> Any:
        where = f"column {column!r} row {row_index}"
        if value is None:
            return None
        if domain == "number":
            return _exact(value, where)
        if domain == "timestamp":
            return _milliseconds(value, where)
        if not isinstance(value, str):  # a boolean included, as the engine's reader refuses one
            raise ReadRefused(f"{where}: value class {type(value).__name__!r}")
        return unicodedata.normalize("NFC", value)

    def check_document(self, text: str) -> None:
        import jsonschema

        self.checked.append(text)
        document = json.loads(text, parse_float=_refuse_float)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        if document.get("_schemaURL") == schema["$id"]:  # format 1-0-0: the vendored schema
            errors = sorted(
                jsonschema.Draft202012Validator(schema).iter_errors(document),
                key=lambda error: list(error.absolute_path),
            )
            if errors:
                raise DocumentRefused("; ".join(error.message for error in errors[:3]))
        # Every document is also read back by the registry's own reader: a schema does not
        # hold its rules (levels sorted, a null cell only where withheld, groups in order),
        # and 1-1-0 (a quantile/2) has no vendored schema here.
        self._real().check_document(text)

    def read_budget(self) -> object:
        return object()

    def read_documents(self, text: str, *, name: str, budget: object) -> Tuple[Dict[str, Any], ...]:
        """Plain YAML: enough for a bundle's file listing, which the real reader tests cover."""
        import yaml

        try:
            documents = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            raise BundleTextRefused("FLX-3105", f"{name}: unreadable YAML") from None
        return tuple(document for document in documents if isinstance(document, dict))

    def canonicalise(self, value: Any, *, name: str) -> Any:
        return value
