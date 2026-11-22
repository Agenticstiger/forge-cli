# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Helpers for the tests of a release: the fixtures read, planned and drawn in memory."""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from fluid_build.profiler import _stats, sources
from fluid_build.profiler.dp import release, selection
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _release_data as data

SEED = bytes(range(1, 33))
CLIQUES = (("status", "channel"),)
COLUMNS = ("order_id", "customer_id", "status", "channel", "amount")


def contract(name: str) -> Dict[str, Any]:
    loaded: Dict[str, Any] = yaml.safe_load(
        (data.FIXTURES / f"{name}.fluid.yaml").read_text(encoding="utf-8")
    )
    return loaded


def request(name: str, backend: Optional[FakeBackend] = None) -> _stats.Request:
    text = (data.FIXTURES / f"{name}.profile-request.yml").read_text(encoding="utf-8")
    return (backend or FakeBackend()).read_request(text, name=f"{name}.profile-request.yml")


def columns(rows: Sequence[data.Order]) -> sources.SourceColumns:
    """*rows* as one read of the table returns them: every value as text, as a CSV holds it."""
    values: Dict[str, List[Any]] = {
        name: [str(row[place]) for row in rows] for place, name in enumerate(COLUMNS)
    }
    return sources.SourceColumns(description="rows", row_count=len(rows), values=values)


def keyed_plan(
    backend: Optional[FakeBackend] = None, *, rho: str = "1", c: int = 3, k: Optional[int] = None
) -> release.ReleasePlan:
    """The orders of ``orders.fluid.yaml``, an entity being the rows of one ``customer_id``."""
    backend = backend or FakeBackend()
    return release.plan_release(
        contract("orders"),
        request("orders", backend),
        backend,
        unit="orders",
        rho=release.parse_rho(rho),
        unit_key=("customer_id",),
        max_rows_per_unit=c,
        max_rows_per_cell=k,
    )


def related_plan(
    backend: Optional[FakeBackend] = None, *, rho: str = "1", c: int = 2, k: Optional[int] = None
) -> release.ReleasePlan:
    """The orders of ``shop.fluid.yaml``, an entity being a customer."""
    backend = backend or FakeBackend()
    return release.plan_release(
        contract("shop"),
        request("shop", backend),
        backend,
        unit="customers",
        rho=release.parse_rho(rho),
        release_table="orders",
        max_rows_per_unit=c,
        max_rows_per_cell=k,
    )


def rows_plan(backend: Optional[FakeBackend] = None, *, rho: str = "1") -> release.ReleasePlan:
    """The orders of ``orders.fluid.yaml``, each row an entity (its primary key is unique)."""
    backend = backend or FakeBackend()
    return release.plan_release(
        contract("orders"),
        request("orders", backend),
        backend,
        unit="orders",
        rho=release.parse_rho(rho),
    )


def draw(
    plan: release.ReleasePlan,
    rows: Sequence[data.Order],
    *,
    seed: bytes = SEED,
    cliques: Optional[Sequence[Sequence[str]]] = CLIQUES,
    backend: Optional[FakeBackend] = None,
    **more: Any,
) -> release.Release:
    backend = backend or FakeBackend()
    encoded = release.encode_rows(plan, columns(rows), backend)
    return release.draw(plan, encoded, seed, backend, cliques=cliques, **more)


def choose(
    rows: Any,
    names: Any,
    levels: Any,
    weights: Any,
    rho: Fraction,
    contributions: int,
    **_: Any,
) -> Tuple[Tuple[str, ...], ...]:
    """A selection that always chooses the pair ``status`` and ``channel``."""
    return CLIQUES if len(names) >= 2 else ()


def keep(
    names: Any, sizes: Any, cliques: Sequence[Sequence[str]], **_: Any
) -> Tuple[Tuple[str, ...], ...]:
    """A structure that keeps the cliques as chosen, right for cliques that have a junction tree."""
    return tuple(tuple(each) for each in cliques)


def stub_selection(
    monkeypatch: Any, chosen: Sequence[Sequence[str]] = CLIQUES
) -> List[Tuple[Any, ...]]:
    """Put the selection's two functions out of OpenDP's and mbi's reach; return the calls made."""
    calls: List[Tuple[Any, ...]] = []

    def select(
        rows: Any,
        names: Any,
        levels: Any,
        weights: Any,
        rho: Fraction,
        contributions: int,
        **_: Any,
    ) -> Tuple[Tuple[str, ...], ...]:
        calls.append((rows, tuple(names), weights, rho, contributions))
        return tuple(tuple(each) for each in chosen) if len(names) >= 2 else ()

    monkeypatch.setattr(selection, "select_cliques", select)
    monkeypatch.setattr(selection, "release_structure", keep)
    return calls
