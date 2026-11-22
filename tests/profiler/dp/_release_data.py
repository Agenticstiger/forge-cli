# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Deterministic records for the release fixtures in ``tests/profiler/fixtures/release``.

``orders.fluid.yaml`` is one table whose rows carry an entity key, ``customer_id``, and
``shop.fluid.yaml`` the same orders as a child of ``customers`` through ``placed_by``. Each
contract's ``*.profile-request.yml`` beside it is the request ``fluxsim init <dir> --contract
<contract>`` (flux-engine 0.9) wrote for it.

One entity, the ``customer_id`` :data:`HEAVY_CUSTOMER`, holds :data:`HEAVY_ORDERS` orders, far
more than any other, so a test can remove the entity with the largest contribution and see what
the bounding does with it.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import List, Tuple

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "release"

#: The customer with by far the most orders, and how many it places.
HEAVY_CUSTOMER = 7
HEAVY_ORDERS = 80
CUSTOMERS = 400

Order = Tuple[int, int, str, str, str]


def orders(seed: int = 20261121) -> List[Order]:
    """``(order_id, customer_id, status, channel, amount)`` rows; the same for one seed."""
    rng = random.Random(seed)
    rows: List[Order] = []
    order_id = 0
    for customer in range(1, CUSTOMERS + 1):
        count = HEAVY_ORDERS if customer == HEAVY_CUSTOMER else rng.choice([1, 1, 2, 2, 3, 4, 5, 8])
        online_share = rng.random()
        for _ in range(count):
            order_id += 1
            channel = "online" if rng.random() < online_share else "store"
            weights = [5, 2, 1, 4] if channel == "online" else [3, 3, 2, 2]
            status = rng.choices(["paid", "placed", "returned", "shipped"], weights=weights)[0]
            low, high = (5, 300) if channel == "online" else (20, 900)
            amount = f"{rng.uniform(low, high):.2f}"
            rows.append((order_id, customer, status, channel, amount))
    return rows


def write_orders(path: Path, rows: List[Order], *, note: bool = False) -> Path:
    """Write *rows* as a CSV with a header; *note* adds the free-text ``note`` column."""
    header = ["order_id", "customer_id", "status", "channel", "amount"] + (["note"] if note else [])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow([*row, f"order {row[0]}"] if note else list(row))
    return path


def write_customers(path: Path, seed: int = 20261121) -> Path:
    """``customer_id, region`` for every customer of :func:`orders`."""
    rng = random.Random(seed + 1)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["customer_id", "region"])
        for customer in range(1, CUSTOMERS + 1):
            writer.writerow([customer, rng.choice(["east", "north", "west"])])
    return path


def without_customer(rows: List[Order], customer: int) -> List[Order]:
    """*rows* less every order of *customer*: the neighbouring data set of one entity."""
    return [row for row in rows if row[1] != customer]
