# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A ``validationRules`` enum list reads a quoted value as SQL reads one, in the data-rules
lint, the dbt bridge, ``fluid test`` and the ODCS exporter alike, and every list in which no
value opens with a quote reads as it did before quoted values (RFC-data-rules.md 2.3)."""

from __future__ import annotations

import itertools
import random
import re
import unicodedata
from typing import List, Sequence, Tuple

import pytest

from fluid_build.data_rules._model import enum_values
from fluid_build.engines.dbt._test_mapping import column_enum_values
from fluid_build.providers.odcs.mappers.quality import _validation_rule_to_quality
from fluid_build.providers.quality_engine import value_domain
from fluid_build.util.column_type import read_enum_list

pytestmark = pytest.mark.unit

E_ACUTE = chr(0xE9)
E_COMBINING = "e" + chr(0x301)
#: Every character class the reader treats differently: letters, a space, both separators,
#: both quotes, a hyphen, and one letter written composed and decomposed (NFC).
ALPHABET = ("a", "B", " ", ",", "|", "'", '"', "-", E_ACUTE, E_COMBINING)


def _previous(constraint: str) -> Tuple[str, ...]:
    """What ``enum_values`` read before quoted values (forge-cli 0.31.0)."""
    values: List[str] = []
    for part in constraint.split(",") + re.split(r"[|,]", constraint):
        part = unicodedata.normalize("NFC", part.strip())
        if part and part not in values:
            values.append(part)
    return tuple(values)


def _read(constraint: str) -> Tuple[str, ...]:
    return enum_values({"validationRules": [{"type": "enum", "constraint": constraint}]})


def _a_value_opens_with_a_quote(constraint: str) -> bool:
    return any(part.lstrip()[:1] in ("'", '"') for part in re.split(r"[|,]", constraint))


def _lists() -> List[str]:
    every_short = [
        "".join(chars) for n in range(5) for chars in itertools.product(ALPHABET, repeat=n)
    ]
    rng = random.Random(20261118)
    longer = ["".join(rng.choices(ALPHABET, k=rng.randint(5, 14))) for _ in range(20000)]
    return every_short + longer


def test_a_list_in_which_no_value_opens_with_a_quote_reads_as_before():
    unchanged = [text for text in _lists() if not _a_value_opens_with_a_quote(text)]
    assert len(unchanged) > 10000
    for text in unchanged:
        assert _read(text) == _previous(text), text


def _quoted(value: str, quote: str) -> str:
    return quote + value.replace(quote, quote + quote) + quote


@pytest.mark.parametrize("quote", ["'", '"'])
def test_a_list_of_quoted_values_reads_back_each_value_as_written(quote):
    rng = random.Random(7)
    for _ in range(3000):
        values: Sequence[str] = [
            "".join(rng.choices(ALPHABET, k=rng.randint(0, 6))) for _ in range(rng.randint(1, 4))
        ]
        text = (rng.choice([",", ", ", " ,  "])).join(_quoted(v, quote) for v in values)
        expected = tuple(dict.fromkeys(unicodedata.normalize("NFC", v) for v in values))
        assert _read(text) == expected, text


@pytest.mark.parametrize(
    "constraint, expected",
    [
        ("'Dr.', '>10000', 'O''Brien', 'a, b'", ("Dr.", ">10000", "O'Brien", "a, b")),
        ("IE, United Kingdom", ("IE", "United Kingdom")),
        ("A | B", ("A | B", "A", "B")),
        ("'a|b', c|d", ("a|b", "c|d", "c", "d")),
        ("O'Brien, D'Arcy", ("O'Brien", "D'Arcy")),
    ],
)
def test_enum_values(constraint, expected):
    assert _read(constraint) == expected


def _plain(constraint: str, separators: str) -> List[str]:
    """The split the dbt bridge (",") and ``fluid test`` ("|,") made before quoted values."""
    return [v.strip() for v in re.split(f"[{re.escape(separators)}]", constraint) if v.strip()]


def _previous_odcs(constraint: str) -> List[str]:
    """The ODCS exporter's reading before quoted values: commas, then quotes stripped."""
    return [v for v in (p.strip().strip("'\"") for p in constraint.split(",")) if v]


@pytest.mark.parametrize("separators", [",", "|,"])
def test_the_dbt_bridge_and_fluid_test_read_a_list_without_a_quoted_value_as_before(separators):
    for text in _lists():
        if not _a_value_opens_with_a_quote(text):
            assert read_enum_list(text, separators) == _plain(text, separators), text


def test_the_odcs_exporter_reads_a_list_without_a_quote_at_a_value_end_as_before():
    checked = 0
    for text in _lists():
        parts = [part.strip() for part in text.split(",")]
        if any(part[:1] in ("'", '"') or part[-1:] in ("'", '"') for part in parts):
            continue
        checked += 1
        assert read_enum_list(text) == _previous_odcs(text), text
    assert checked > 10000


QUOTED = "'base_fee', 'fee, late', 'O''Brien'"
LEVELS = ["base_fee", "fee, late", "O'Brien"]


def test_the_dbt_bridge_reads_a_quoted_list():
    column = {"validationRules": [{"type": "enum", "constraint": QUOTED}]}
    assert column_enum_values(column) == LEVELS


def test_fluid_test_reads_a_quoted_list():
    columns = [{"name": "kind", "validationRules": [{"type": "enum", "constraint": QUOTED}]}]
    assert value_domain({"selector": "kind"}, columns).values == tuple(LEVELS)


def test_the_odcs_exporter_reads_a_quoted_list():
    rule = _validation_rule_to_quality({"type": "enum", "constraint": QUOTED}, "kind")
    assert rule is not None and rule["arguments"]["validValues"] == LEVELS
    assert rule["description"] == f"Field 'kind' must be one of: {QUOTED}"
