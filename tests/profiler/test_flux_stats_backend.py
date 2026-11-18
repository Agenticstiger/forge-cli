# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile`` over the real statistic registry, ``flux-stats``.

Runs where flux-stats is installed: ``[profile]`` brings it and ``[dev]``
includes ``[profile]``, so CI runs it. It checks that:

* :func:`fluid_build.profiler._stats.load_backend` gives a backend that satisfies
  the seam, over flux-stats' registry, record reader and profile reader;
* flux-stats' profile reader reads back every document ``fluid profile`` writes,
  value for value;
* the stand-in registry the other tests use (``_fake_backend.FakeBackend``)
  writes byte-identical documents, so those tests exercise the real numbers;
* a value is read as the fidelity check reads it, and a refused one is named by
  its column, its row and flux-stats' code, never by itself;
* a value is placed on the grid as the check places it, and the ``mean/1`` a
  profile carries is one the check compares a ``mean`` property with.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction

import pytest

from fluid_build.profiler._stats import (
    DocumentRefused,
    ReadRefused,
    StatisticsBackend,
    load_backend,
)
from fluid_build.profiler.disclosure import ACRO_DEFAULT, round_count
from fluid_build.profiler.runner import profile_expose
from tests.profiler._fake_backend import FakeBackend

pytest.importorskip("flux_stats")
from flux_stats.profile import load_profile_text  # noqa: E402

pytestmark = pytest.mark.unit

CONTRACT = {
    "exposes": [
        {
            "exposeId": "payment",
            "contract": {
                "schema": [
                    {"name": "amount", "type": "decimal(12,2)"},
                    {"name": "market", "type": "string"},
                    {"name": "paid_at", "type": "timestamp"},
                    {"name": "attempts", "type": "integer"},
                ]
            },
        }
    ]
}

#: 2026-01-15T08:30:00Z in milliseconds since the epoch.
PAID_AT_MS = int(datetime(2026, 1, 15, 8, 30, tzinfo=timezone.utc).timestamp()) * 1000


def _slice(tmp_path, rows):
    path = tmp_path / "payment.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return str(path)


def _rows(count):
    return [
        {
            "amount": f"{(n % 5) * 12}.25" if n % 7 else None,
            "market": ["IE", "GB", "FR"][n % 3],
            "paid_at": f"2026-0{1 + n % 3}-15T08:30:00.000Z",
            "attempts": n % 4 if n % 11 else n * 1000,
        }
        for n in range(count)
    ]


@pytest.fixture(scope="module")
def backend():
    return load_backend()


def test_the_backend_satisfies_the_seam(backend):
    assert isinstance(backend, StatisticsBackend)
    names = {metric.name for metric in backend.metrics()}
    assert {"count", "nullCount", "distinctCount", "sum", "min", "max", "quantiles"} <= names
    assert "mean/1" in {statistic.identifier for statistic in backend.statistics()}


@pytest.mark.parametrize("count", [3, 40, 200])
def test_flux_stats_reads_every_document_back(tmp_path, backend, count):
    result = profile_expose(
        CONTRACT, "payment", source=_slice(tmp_path, _rows(count)), backend=backend
    )
    profile, raw = load_profile_text(result.canonical.decode("utf-8"))
    assert raw == result.document
    assert set(profile.tables) == {"payment"}


@pytest.mark.parametrize("count", [3, 40, 200])
def test_the_stand_in_registry_writes_the_same_bytes(tmp_path, backend, count):
    source = _slice(tmp_path, _rows(count))
    real = profile_expose(CONTRACT, "payment", source=source, backend=backend)
    fake = profile_expose(CONTRACT, "payment", source=source, backend=FakeBackend())
    assert fake.canonical == real.canonical


@pytest.mark.parametrize(
    ("domain", "value", "expected"),
    [
        ("number", "12.50", Fraction(25, 2)),
        ("number", 7, Fraction(7)),
        ("number", Decimal("-0.001"), Fraction(-1, 1000)),
        ("timestamp", "2026-01-15T08:30:00.000Z", PAID_AT_MS),
        ("timestamp", "2026-01-15T09:30:00+01:00", PAID_AT_MS),
        ("category", "Cafe" + chr(0x301), "Caf" + chr(0xE9)),
        ("category", None, None),
        ("number", None, None),
    ],
)
def test_a_value_is_read_as_the_fidelity_check_reads_it(backend, domain, value, expected):
    read = backend.read_value(domain, value, column="c", row_index=0)
    assert read == expected
    assert type(read) is type(expected)


@pytest.mark.parametrize(
    ("domain", "value", "reason", "code"),
    [
        ("number", 12.5, "value class 'float'", "FLX-5005"),
        ("number", True, "value class 'bool'", "FLX-5005"),
        ("number", "12,50", "value class 'non-numeric string'", "FLX-5005"),
        ("timestamp", PAID_AT_MS, "value class 'int'", "FLX-5005"),
        ("timestamp", "2026-01-15T08:30:00.0001Z", "sub-millisecond precision", "FLX-5022"),
        ("timestamp", "2026-01-15 08:30:00", "with an offset", "FLX-5022"),
        ("category", 3, "value class 'int'", "FLX-5005"),
    ],
)
def test_a_refusal_names_the_cell_once_and_never_the_value(backend, domain, value, reason, code):
    with pytest.raises(ReadRefused) as caught:
        backend.read_value(domain, value, column="paid_at", row_index=5)
    message = str(caught.value)
    assert message.startswith("column 'paid_at' row 5: ")
    assert message.count("row 5") == 1
    assert reason in message
    assert message.endswith(f"({code})")
    if isinstance(value, str):
        assert value not in message


def test_a_domain_without_a_reader_is_an_error(backend):
    with pytest.raises(ValueError):
        backend.read_value("boolean", True, column="paid", row_index=0)


@pytest.mark.parametrize(
    "value",
    [
        "1E+99999999",  # building it would take minutes and gigabytes
        "1e5000",  # past Python's 4,300-digit limit, so it could not be spelled
        "1e-7000",
        "9" * 1001,
        Decimal("1E+1000"),
        10**1000,
    ],
)
def test_a_number_of_more_than_1000_digits_is_refused_before_it_is_built(backend, value):
    with pytest.raises(ReadRefused) as caught:
        backend.read_value("number", value, column="amount", row_index=7)
    # As flux-stats words it, with no digit count: the count tells the magnitude.
    assert str(caught.value) == "column 'amount' row 7: value class 'over-long number' (FLX-5005)"


@pytest.mark.parametrize(
    "value",
    [
        "9" * 1000,
        "0." + "9" * 1000,
        Decimal("1E+999"),
        10**1000 - 1,
        "0E+99999999",  # a zero is one digit, whatever its exponent
        "-0E-99999999",
        "NaN",
        "abc",
    ],
)
def test_a_number_within_the_bound_is_left_to_flux_stats(backend, value):
    try:
        read = backend.read_value("number", value, column="amount", row_index=0)
    except ReadRefused as refused:  # NaN and abc: flux-stats refuses them by class
        assert "(FLX-5005)" in str(refused)
    else:
        assert isinstance(read, Fraction)


def test_a_sum_longer_than_flux_stats_spells_is_refused_with_the_document(tmp_path, backend):
    # Four distinct 1000-digit values, ten records each: every value is in bound and
    # published, and their sum (1002 digits) is past what flux-stats spells.
    contract = {
        "exposes": [{"exposeId": "a", "contract": {"schema": [{"name": "n", "type": "integer"}]}}]
    }
    rows = [{"n": 10**999 * (5 + i % 4)} for i in range(40)]
    path = tmp_path / "a.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(DocumentRefused, match=r"columns\.n\.metrics\.sum"):
        profile_expose(contract, "a", source=str(path), backend=backend)


def test_a_document_flux_stats_cannot_read_is_refused(backend):
    with pytest.raises(DocumentRefused, match="FLX-5036"):
        backend.check_document('{"rowCount": 1.5}')


def test_a_boolean_column_is_left_out_as_flux_stats_reads_none(tmp_path, backend):
    contract = {
        "exposes": [
            {
                "exposeId": "payment",
                "contract": {
                    "schema": [
                        {"name": "attempts", "type": "integer"},
                        {"name": "paid", "type": "boolean"},
                    ]
                },
            }
        ]
    }
    rows = [{"attempts": n % 4, "paid": n % 2 == 0} for n in range(40)]
    result = profile_expose(contract, "payment", source=_slice(tmp_path, rows), backend=backend)
    assert [name for name, _ in result.plan.skipped] == ["paid"]
    load_profile_text(result.canonical.decode("utf-8"))


@pytest.mark.parametrize("fractional_bits", [0, 1, 16, 53, 128])
@pytest.mark.parametrize(
    "value",
    [
        Fraction(0),
        Fraction(1, 2),
        Fraction(-1, 2),
        Fraction(1, 100000),
        Fraction(-1, 100000),
        Fraction(1225, 100),
        Fraction(-(10**30) - 1, 3),
    ],
)
def test_a_value_is_placed_on_the_grid_as_flux_stats_places_it(backend, fractional_bits, value):
    from flux_stats.fixedpoint import Grid

    units = backend.encoder(fractional_bits)(value)
    assert units == Grid(fractional_bits).quantise(value)
    assert units == FakeBackend().encoder(fractional_bits)(value)
    assert type(units) is int


def test_the_mean_a_profile_carries_is_the_one_the_check_compares(tmp_path, backend):
    from flux_stats import Check, GroupStatistic

    result = profile_expose(
        CONTRACT, "payment", source=_slice(tmp_path, _rows(200)), backend=backend
    )
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    assert profile.fractional_bits == 16
    for column in ("amount", "attempts"):
        (entry,) = profile.column_statistics("payment", column)
        assert entry.identifier == "mean/1" and entry.group_by is None
        (group,) = entry.groups
        assert group.published and group.key == ""
        reference = group.statistic
        values = [
            Fraction(Decimal(str(row[column]))) for row in _rows(200) if row[column] is not None
        ]
        # k is the count rounded as every published count is; Sx is the exact sum on the grid.
        assert reference.k == round_count(len(values), ACRO_DEFAULT)
        assert reference.sum_x == sum(map(backend.encoder(16), values))
        exact = sum(values, Fraction(0))
        assert 0 <= exact - Fraction(reference.sum_x, 2**16) < Fraction(len(values), 2**16)
        check = Check(subject="mean", tolerance="relative", margin=Fraction(1, 10))
        same = GroupStatistic(group="", k=reference.k, sum_x=reference.sum_x)
        assert entry.definition.compare(same, reference, check)
        higher = GroupStatistic(group="", k=reference.k, sum_x=reference.sum_x * 12 // 10)
        assert not entry.definition.compare(higher, reference, check)


def test_the_rounding_status_is_the_registrys_word_for_it():
    from flux_stats import edge_counts

    from fluid_build.profiler._stats import WITHHELD_ROUNDING

    assert WITHHELD_ROUNDING == edge_counts.ROUNDING


def test_the_backend_reads_a_bundle_through_the_registrys_exact_reader():
    backend = load_backend()
    budget = backend.read_budget()
    (document,) = backend.read_documents("rate: 0.50\nwhen: 2024-01-01\n", name="s", budget=budget)
    assert document == {"rate": Decimal("0.50"), "when": "2024-01-01"}
    assert backend.canonicalise(document, name="s") == {"rate": "0.50", "when": "2024-01-01"}
