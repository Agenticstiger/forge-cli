# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A small shop for the tests of what a profile request of format 1-2-0 asks.

The contract is ``fixtures/request/shop.fluid.yaml`` with an ``age`` on the customers, a
whole number declared between 0 and 100, so that a parent column has public bins. The records
are made so that every group the tests read holds enough records to be published:

* 300 customers, ``region`` by ``customer_id % 3``, ``age`` by ``customer_id % 100``;
* a customer has 0, 1, 2, 3, 6, 12 or 25 orders, by ``(customer_id // 3) % 7``, so every
  region holds 14 or 15 customers of each count and every bin of children per parent holds at
  least ten of them; ``customer_id`` of an order is its customer's, and the orders of ``extra``
  come last, each for a customer no row holds (999) or none;
* an order has a ``status`` (by ``order_id % 3``), a ``quantity`` (by ``order_id % 100``), a
  ``units``, an ``amount``, a ``paid`` flag (by ``order_id % 2``) and a ``placed_at``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestEnd,
    RequestLinked,
    RequestRankCorrelation,
    RequestRelationship,
    RequestTable,
)
from fluid_build.profiler.runner import ExposeSource, ProfileResult, profile_exposes

FIXTURES = Path(__file__).parent / "fixtures" / "request"
_AGE = """        - name: age
          type: int
          validationRules:
            - type: range
              constraint: ">= 0 and <= 100"
"""
CONTRACT_TEXT = (
    (FIXTURES / "shop.fluid.yaml")
    .read_text(encoding="utf-8")
    .replace("        - name: region\n", _AGE + "        - name: region\n", 1)
)
CONTRACT: Dict[str, Any] = yaml.safe_load(CONTRACT_TEXT)

CUSTOMERS = 1400
REGIONS = ("north", "south", "west")
STATUSES = ("open", "paid", "void")


def customers() -> List[Dict[str, Any]]:
    return [{"customer_id": n, "age": n % 100, "region": REGIONS[n % 3]} for n in range(CUSTOMERS)]


def orders(extra: Sequence[Optional[int]] = ()) -> List[Dict[str, Any]]:
    """Each customer's orders, then one order for each customer id in *extra* (``None``: no customer)."""
    owners: List[Optional[int]] = [
        customer
        for customer in range(CUSTOMERS)
        for _ in range((0, 1, 2, 3, 6, 12, 25)[(customer // 3) % 7])
    ]
    owners.extend(extra)
    return [
        {
            "order_id": n,
            "customer_id": owner,
            "status": STATUSES[n % 3],
            "quantity": n % 100,
            "units": n % 7,
            "amount": f"{n % 20}.50",
            "placed_at": f"2026-07-{1 + n % 20:02d}T10:00:00.000Z",
            "paid": n % 2 == 0,
            "note": "n",
        }
        for n, owner in enumerate(owners)
    ]


def write(directory: Path, tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> Dict[str, str]:
    """Write each table as NDJSON in *directory*; its path, by table."""
    found = {}
    for name, rows in tables.items():
        path = directory / f"{name}.ndjson"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        found[name] = str(path)
    return found


def columns(*specs: Tuple[str, str], metrics: Sequence[str] = ()) -> Tuple[RequestColumn, ...]:
    return tuple(RequestColumn(name, domain, tuple(metrics)) for name, domain in specs)


def request(
    *,
    rank: Sequence[RequestRankCorrelation] = (),
    clique: Sequence[str] = (),
    group_by: Sequence[str] = (),
    linked: Sequence[Tuple[str, str]] = (),
    statistics: Sequence[str] = ("marginalHistogram/1",),
    relationship: bool = True,
    metrics: Sequence[str] = (),
) -> Request:
    """A request over both tables, asking only what is given (and the column *metrics* named)."""
    return Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "customers",
                False,
                columns(
                    ("customer_id", "number"),
                    ("age", "number"),
                    ("region", "category"),
                    metrics=metrics,
                ),
            ),
            RequestTable(
                "orders",
                False,
                columns(
                    ("customer_id", "number"),
                    ("quantity", "number"),
                    ("units", "number"),
                    ("amount", "number"),
                    ("placed_at", "timestamp"),
                    ("status", "category"),
                    ("paid", "category"),
                    metrics=metrics,
                ),
                rank_correlations=tuple(rank),
                clique_columns=tuple(clique),
            ),
        ),
        relationships=(
            (
                RequestRelationship(
                    RequestEnd("customers", ("customer_id",)),
                    RequestEnd("orders", ("customer_id",)),
                    tuple(statistics),
                    group_by=tuple(group_by),
                    linked=tuple(RequestLinked(a, b) for a, b in linked),
                ),
            )
            if relationship
            else ()
        ),
    )


def profile(
    directory: Path,
    asked: Request,
    backend: Any,
    *,
    tables: Optional[Mapping[str, Sequence[Mapping[str, Any]]]] = None,
    **options: Any,
) -> ProfileResult:
    paths = write(directory, tables or {"customers": customers(), "orders": orders()})
    return profile_exposes(
        CONTRACT,
        [ExposeSource(table.expose_id, paths[table.expose_id]) for table in asked.tables],
        backend=backend,
        request=asked,
        **options,
    )


REQUEST_1_2_0: Dict[str, Any] = {
    "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-2-0/FluxProfileRequest.json",
    "encoding": {"fractionalBits": 16, "rounding": "floor"},
    "tables": {
        "customers": {
            "columns": {
                "customer_id": {"domain": "number"},
                "age": {"domain": "number"},
                "region": {"domain": "category"},
            },
        },
        "orders": {
            "columns": {
                "customer_id": {"domain": "number"},
                "quantity": {"domain": "number"},
                "units": {"domain": "number"},
                "amount": {"domain": "number"},
                "placed_at": {"domain": "timestamp"},
                "status": {"domain": "category"},
                "paid": {"domain": "category"},
            },
            "rankCorrelations": [
                {"definition": "kendallTau/1", "columns": ["quantity", "amount", "placed_at"]},
                {"definition": "spearmanRho/1", "columns": ["quantity", "units"]},
            ],
            "cliqueColumns": ["status", "quantity", "paid"],
        },
    },
    "relationships": [
        {
            "parent": {"table": "customers", "columns": ["customer_id"]},
            "child": {"table": "orders", "columns": ["customer_id"]},
            "statistics": ["marginalHistogram/1"],
            "groupBy": ["region", "age"],
            "linked": [
                {"column": "status", "parentColumn": "region"},
                {"column": "paid", "parentColumn": "region"},
            ],
        }
    ],
}


def request_text(document: Optional[Mapping[str, Any]] = None) -> str:
    """A request as text (JSON is YAML), by default :data:`REQUEST_1_2_0`."""
    return json.dumps(REQUEST_1_2_0 if document is None else document, indent=1)
