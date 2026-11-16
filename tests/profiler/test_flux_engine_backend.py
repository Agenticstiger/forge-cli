# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid profile`` against flux-engine's own registry and profile reader.

Runs only where flux-engine is installed: forge-cli depends on it nowhere, and
CI does not install it. On such a machine it checks three things:

* the engine's profile reader (``flux_engine.stats.profile.load_profile_text``)
  accepts every document ``fluid profile`` writes, and reads back the values
  it wrote;
* the stand-in registry the other tests use (``_fake_backend.FakeBackend``)
  writes byte-identical documents, so those tests exercise the real numbers;
* the backend below, the shape ``fluid_build/profiler/_stats.py`` will take once
  flux-stats is published, satisfies the seam's protocol.

The reader half it uses (``_exact``, ``parse_rfc3339_ms``, ``nfc``) is engine code
today. The founder decided on 2026-11-16 that it moves into flux-stats.
"""

from __future__ import annotations

import json
from typing import Any, Tuple

import pytest

from fluid_build.profiler._stats import DocumentRefused, ReadRefused, StatisticsBackend
from fluid_build.profiler.runner import profile_expose
from tests.profiler._fake_backend import FakeBackend

flux_stats = pytest.importorskip("flux_engine.stats")
from flux_engine.concordance.boundary.statistic import _exact  # noqa: E402
from flux_engine.core.canonical import nfc  # noqa: E402
from flux_engine.core.clock import parse_rfc3339_ms  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.stats.profile import load_profile_text  # noqa: E402

pytestmark = pytest.mark.unit


class FluxEngineBackend:
    """The seam over flux-engine's registry, as ``_stats.load_backend`` will build it."""

    def metrics(self) -> Tuple[Any, ...]:
        return tuple(flux_stats.metrics())

    def read_value(self, domain: str, value: Any, *, column: str, row_index: int) -> Any:
        if value is None:
            return None
        subject = f"column {column!r} row {row_index}"
        try:
            if domain == "number":
                return _exact(value, subject)
            if domain == "timestamp":
                return parse_rfc3339_ms(value)
            if not isinstance(value, str):
                raise ReadRefused(f"{subject}: value class {type(value).__name__!r}")
            return nfc(value)
        except FluxError as exc:
            # The engine's own text names no column (parse_rfc3339_ms does not know it).
            raise ReadRefused(f"{subject}: {exc}") from None

    def check_document(self, text: str) -> None:
        try:
            load_profile_text(text)
        except FluxError as exc:
            raise DocumentRefused(str(exc)) from None


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


def test_the_backend_satisfies_the_seam():
    assert isinstance(FluxEngineBackend(), StatisticsBackend)


@pytest.mark.parametrize("count", [3, 40, 200])
def test_the_engine_reads_every_document_back(tmp_path, count):
    result = profile_expose(
        CONTRACT, "payment", source=_slice(tmp_path, _rows(count)), backend=FluxEngineBackend()
    )
    profile, raw = load_profile_text(result.canonical.decode("utf-8"))
    assert raw == result.document
    assert set(profile.tables) == {"payment"}


@pytest.mark.parametrize("count", [3, 40, 200])
def test_the_stand_in_registry_writes_the_same_bytes(tmp_path, count):
    source = _slice(tmp_path, _rows(count))
    real = profile_expose(CONTRACT, "payment", source=source, backend=FluxEngineBackend())
    fake = profile_expose(CONTRACT, "payment", source=source, backend=FakeBackend())
    assert fake.canonical == real.canonical


def test_the_engine_refuses_what_the_seam_refuses(tmp_path):
    rows = _rows(40)
    rows[5]["paid_at"] = "2026-01-15T08:30:00.0001Z"  # below the millisecond clock
    with pytest.raises(ReadRefused, match="column 'paid_at' row 5"):
        profile_expose(
            CONTRACT, "payment", source=_slice(tmp_path, rows), backend=FluxEngineBackend()
        )


def test_a_boolean_column_is_left_out_as_the_engine_reads_none(tmp_path):
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
    result = profile_expose(
        contract, "payment", source=_slice(tmp_path, rows), backend=FluxEngineBackend()
    )
    assert [name for name, _ in result.plan.skipped] == ["paid"]
    load_profile_text(result.canonical.decode("utf-8"))
