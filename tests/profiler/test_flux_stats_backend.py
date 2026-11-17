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
  its column, its row and flux-stats' code, never by itself.
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
