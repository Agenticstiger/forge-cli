# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Helpers for the tests of rank correlations and mixed cliques in a release.

The orders of ``orders.fluid.yaml`` get one more required column, ``quantity``, a whole number
from 1 to 10, so that the release table holds two number columns, ``amount`` and ``quantity``.
"""

from __future__ import annotations

import copy
import dataclasses
import random
from typing import Any, Dict, Optional, Sequence, Tuple

from fluid_build.profiler import _stats, sources
from fluid_build.profiler.dp import release
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _release_data as data
from tests.profiler.dp import _release_run as run

KENDALL, SPEARMAN = "kendallTau/1", "spearmanRho/1"
COLUMNS = (*run.COLUMNS, "quantity")
JOINING = ("status", "channel", "amount", "quantity")


def contract() -> Dict[str, Any]:
    found = copy.deepcopy(run.contract("orders"))
    found["exposes"][0]["contract"]["schema"].append(
        {
            "name": "quantity",
            "type": "INT",
            "required": True,
            "validationRules": [{"type": "range", "constraint": "between 1 and 10"}],
        }
    )
    return found


def request(
    backend: FakeBackend,
    *,
    ranks: Sequence[str] = (KENDALL, SPEARMAN),
    rank_columns: Sequence[str] = ("amount", "quantity"),
    joining: Sequence[str] = JOINING,
) -> _stats.Request:
    base = run.request("orders", backend)
    table = base.tables[0]
    added = _stats.RequestColumn("quantity", "number", (), ("marginalHistogram/1",))
    asks = tuple(_stats.RequestRankCorrelation(each, tuple(rank_columns)) for each in ranks)
    changed = dataclasses.replace(
        table,
        columns=(*table.columns, added),
        rank_correlations=asks,
        clique_columns=tuple(joining),
    )
    return dataclasses.replace(base, tables=(changed,))


def plan(
    backend: Optional[FakeBackend] = None,
    *,
    rho: str = "1",
    c: Optional[int] = 3,
    ranks: Sequence[str] = (KENDALL, SPEARMAN),
    rank_columns: Sequence[str] = ("amount", "quantity"),
    joining: Sequence[str] = JOINING,
    row_floor: Optional[int] = None,
    method: str = "aim",
) -> release.ReleasePlan:
    backend = backend or FakeBackend()
    return release.plan_release(
        contract(),
        request(backend, ranks=ranks, rank_columns=rank_columns, joining=joining),
        backend,
        unit="orders",
        rho=release.parse_rho(rho),
        **({"unit_key": ("customer_id",), "max_rows_per_unit": c} if c is not None else {}),
        row_floor=row_floor,
        clique_method=method,
    )


def columns(rows: Sequence[data.Order], seed: int = 3) -> sources.SourceColumns:
    rng = random.Random(seed)
    values: Dict[str, Any] = {
        name: [str(row[place]) for row in rows] for place, name in enumerate(run.COLUMNS)
    }
    values["quantity"] = [str(rng.randint(1, 10)) for _ in rows]
    return sources.SourceColumns(description="rows", row_count=len(rows), values=values)


def draw(
    release_plan: release.ReleasePlan,
    rows: Sequence[data.Order],
    *,
    seed: bytes = run.SEED,
    cliques: Optional[Sequence[Sequence[str]]] = (("status", "amount"), ("amount", "quantity")),
    backend: Optional[FakeBackend] = None,
    **more: Any,
) -> release.Release:
    backend = backend or FakeBackend()
    encoded = release.encode_rows(release_plan, columns(rows), backend)
    return release.draw(release_plan, encoded, seed, backend, cliques=cliques, **more)


MIXED: Tuple[Tuple[str, ...], ...] = (("status", "amount"), ("amount", "quantity"))
