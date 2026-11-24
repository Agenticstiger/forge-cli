# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A small contract, request and records for the tests of a unit that follows its child rows.

``account`` is the unit's expose. An ``invoice`` follows an account through ``placed_by`` and a
``line`` follows an invoice through ``in_invoice``; an invoice also names a ``product`` through
``of``, and a ``ticket`` names an account through ``raised_by``. The product and ticket tables are
ones the unit never reaches. A product's ``category`` carries the sensitivity label ``pii``.
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from fluid_build.profiler import _stats, sources
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp.follows import FollowSpec, parse_follow
from tests.profiler._fake_backend import FakeBackend

SEED = bytes(range(7, 39))
REQUEST_URL = (
    "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-2-0/FluxProfileRequest.json"
)

CONTRACT: Dict[str, Any] = yaml.safe_load(
    """
fluidVersion: "0.7.7"
kind: DataProduct
id: billing.follows
name: Billing
metadata:
  owner:
    team: billing-team
exposes:
  - exposeId: account
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/account.parquet }
    contract:
      schema:
        - { name: id, type: INT, required: true }
        - { name: segment, type: "enum('business','personal')", required: true }
        - name: age
          type: INT
          required: true
          validationRules:
            - { type: range, constraint: ">= 0 and <= 100" }
    semantics:
      entities:
        - { name: account, type: primary, expr: id }
  - exposeId: invoice
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/invoice.parquet }
    contract:
      schema:
        - { name: invoice_id, type: INT, required: true }
        - { name: account_id, type: INT, required: true }
        - { name: product_id, type: INT, required: true }
        - { name: status, type: "enum('open','paid')", required: true }
        - { name: channel, type: "enum('web','shop')", required: true }
        - name: qty
          type: INT
          required: true
          validationRules:
            - { type: range, constraint: ">= 0 and <= 10" }
        - name: units
          type: INT
          required: true
          validationRules:
            - { type: range, constraint: ">= 0 and <= 5" }
    semantics:
      entities:
        - { name: invoice, type: primary, expr: invoice_id }
      relationships:
        - name: placed_by
          to: account
          fromColumns: [account_id]
          toColumns: [id]
          cardinality: many_to_one
        - name: of
          to: product
          fromColumns: [product_id]
          toColumns: [product_id]
          cardinality: many_to_one
  - exposeId: line
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/line.parquet }
    contract:
      schema:
        - { name: line_id, type: INT, required: true }
        - { name: invoice_id, type: INT, required: true }
        - { name: kind, type: "enum('a','b')", required: true }
    semantics:
      entities:
        - { name: line, type: primary, expr: line_id }
      relationships:
        - name: in_invoice
          to: invoice
          fromColumns: [invoice_id]
          toColumns: [invoice_id]
          cardinality: many_to_one
  - exposeId: ticket
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/ticket.parquet }
    contract:
      schema:
        - { name: ticket_id, type: INT, required: true }
        - { name: account_id, type: INT, required: true }
        - { name: severity, type: "enum('lo','hi')", required: true }
    semantics:
      entities:
        - { name: ticket, type: primary, expr: ticket_id }
      relationships:
        - name: raised_by
          to: account
          fromColumns: [account_id]
          toColumns: [id]
          cardinality: many_to_one
  - exposeId: product
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/product.parquet }
    contract:
      schema:
        - { name: product_id, type: INT, required: true }
        - { name: category, type: "enum('x','y','z')", required: true, sensitivity: pii }
        - { name: tier, type: "enum('lo','hi')", required: true }
    semantics:
      entities:
        - { name: product, type: primary, expr: product_id }
"""
)

#: The two followed relationships of the standard tree: account to invoice (cap 3) to line (cap 2).
FOLLOW_INVOICES = "account.id=invoice.account_id:3"
FOLLOW_LINES = "invoice.invoice_id=line.invoice_id:2"
FOLLOWS = (FOLLOW_INVOICES, FOLLOW_LINES)

Row = Dict[str, Any]
Tables = Dict[str, List[Row]]


def specs(*texts: str) -> Tuple[FollowSpec, ...]:
    return tuple(parse_follow(text) for text in (texts or FOLLOWS))


def request_document(
    *,
    rich: bool = False,
    relationships: Optional[Sequence[Mapping[str, Any]]] = None,
    extra: Optional[Mapping[str, Any]] = None,
    digest: Optional[str] = None,
) -> Dict[str, Any]:
    """A request of format 1-2-0 over the four tables."""
    document: Dict[str, Any] = {
        "_schemaURL": REQUEST_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "tables": {
            "account": {
                "columns": {
                    "id": {"domain": "number"},
                    "segment": {"domain": "category", "statistics": ["levelHistogram/1"]},
                    "age": {"domain": "number", "statistics": ["marginalHistogram/1"]},
                },
            },
            "invoice": {
                "columns": {
                    "invoice_id": {"domain": "number"},
                    "account_id": {"domain": "number"},
                    "product_id": {"domain": "number"},
                    "status": {"domain": "category", "statistics": ["levelHistogram/1"]},
                    "qty": {"domain": "number", "statistics": ["marginalHistogram/1"]},
                },
            },
            "line": {
                "columns": {
                    "line_id": {"domain": "number"},
                    "invoice_id": {"domain": "number"},
                    "kind": {"domain": "category", "statistics": ["levelHistogram/1"]},
                },
            },
            "ticket": {
                "columns": {
                    "ticket_id": {"domain": "number"},
                    "account_id": {"domain": "number"},
                    "severity": {"domain": "category", "statistics": ["levelHistogram/1"]},
                },
            },
            "product": {
                "columns": {
                    "product_id": {"domain": "number"},
                    "category": {"domain": "category", "statistics": ["levelHistogram/1"]},
                    "tier": {"domain": "category"},
                },
            },
        },
        "relationships": list(
            relationships
            if relationships is not None
            else (
                {
                    "parent": {"table": "account", "columns": ["id"]},
                    "child": {"table": "invoice", "columns": ["account_id"]},
                    "statistics": ["marginalHistogram/1"],
                    "groupBy": ["segment", "age"],
                    "linked": [{"column": "status", "parentColumn": "segment"}],
                },
                {
                    "parent": {"table": "account", "columns": ["id"]},
                    "child": {"table": "ticket", "columns": ["account_id"]},
                    "statistics": ["marginalHistogram/1"],
                    "groupBy": ["segment"],
                    "linked": [{"column": "severity", "parentColumn": "segment"}],
                },
                {
                    "parent": {"table": "product", "columns": ["product_id"]},
                    "child": {"table": "invoice", "columns": ["product_id"]},
                    "statistics": ["marginalHistogram/1"],
                    "groupBy": ["category"],
                    "linked": [{"column": "status", "parentColumn": "category"}],
                },
            )
        ),
    }
    if rich:
        invoice = document["tables"]["invoice"]
        invoice["columns"]["channel"] = {"domain": "category", "statistics": ["levelHistogram/1"]}
        invoice["columns"]["units"] = {"domain": "number", "statistics": ["marginalHistogram/1"]}
        invoice["rankCorrelations"] = [
            {"definition": "kendallTau/1", "columns": ["qty", "units"]},
            {"definition": "spearmanRho/1", "columns": ["qty", "units"]},
        ]
        invoice["cliqueColumns"] = ["status", "channel"]
    if extra:
        document.update(extra)
    if digest is not None:
        document["contract"] = {"digest": digest}
    return document


def request(backend: Optional[FakeBackend] = None, **options: Any) -> _stats.Request:
    backend = backend or FakeBackend()
    return backend.read_request(json.dumps(request_document(**options)), name="follows.request")


def tables(accounts: int = 60, seed: int = 11, *, heavy: Optional[int] = None) -> Tables:
    """Accounts with 0 to 6 invoices each, invoices with 0 to 4 lines, and 6 products."""
    rng = random.Random(seed)
    account: List[Row] = []
    invoice: List[Row] = []
    line: List[Row] = []
    ticket: List[Row] = []
    for n in range(1, accounts + 1):
        account.append(
            {"id": n, "segment": rng.choice(["business", "personal"]), "age": rng.randint(0, 100)}
        )
        for _ in range(rng.randint(0, 2)):
            ticket.append(
                {
                    "ticket_id": len(ticket) + 1,
                    "account_id": n,
                    "severity": rng.choice(["lo", "hi"]),
                }
            )
        count = heavy if heavy is not None and n == 1 else rng.randint(0, 6)
        for _ in range(count):
            number = len(invoice) + 1
            invoice.append(
                {
                    "invoice_id": number,
                    "account_id": n,
                    "product_id": rng.randint(1, 6),
                    "status": rng.choice(["open", "paid"]),
                    "channel": rng.choice(["web", "shop"]),
                    "qty": rng.randint(0, 10),
                    "units": rng.randint(0, 5),
                }
            )
            for _ in range(rng.randint(0, 4)):
                line.append(
                    {
                        "line_id": len(line) + 1,
                        "invoice_id": number,
                        "kind": rng.choice(["a", "b"]),
                    }
                )
    product = [
        {"product_id": n, "category": ("x", "y", "z")[n % 3], "tier": ("lo", "hi")[n % 2]}
        for n in range(1, 7)
    ]
    ticket.append({"ticket_id": len(ticket) + 1, "account_id": accounts + 500, "severity": "lo"})
    return {
        "account": account,
        "invoice": invoice,
        "line": line,
        "ticket": ticket,
        "product": product,
    }


def source_columns(
    plan: fr.FollowReleasePlan, data: Mapping[str, Sequence[Mapping[str, Any]]]
) -> Dict[str, sources.SourceColumns]:
    """Each table of *plan*'s reads as one read of it returns: every value as text, as a CSV holds it."""
    found: Dict[str, sources.SourceColumns] = {}
    for table, columns in plan.reads:
        rows = data[table]
        found[table] = sources.SourceColumns(
            description=table,
            row_count=len(rows),
            values={
                column.name: [
                    "" if row[column.name] is None else str(row[column.name]) for row in rows
                ]
                for column in columns
            },
        )
    return found


def plan(
    backend: Optional[FakeBackend] = None,
    *,
    follows: Sequence[str] = FOLLOWS,
    rho: str = "1",
    asked: Optional[_stats.Request] = None,
    **options: Any,
) -> fr.FollowReleasePlan:
    from fluid_build.profiler.dp import release

    backend = backend or FakeBackend()
    return fr.plan_follow_release(
        CONTRACT,
        asked if asked is not None else request(backend),
        backend,
        unit="account",
        rho=release.parse_rho(rho),
        follows=specs(*follows),
        **options,
    )


def draw(
    release_plan: fr.FollowReleasePlan,
    data: Mapping[str, Sequence[Mapping[str, Any]]],
    backend: Optional[FakeBackend] = None,
    seed: bytes = SEED,
    **options: Any,
) -> fr.FollowRelease:
    backend = backend or FakeBackend()
    rows = fr.encode_follow_rows(release_plan, source_columns(release_plan, data), backend)
    return fr.draw_follow(release_plan, rows, seed, backend, **options)


def write_csvs(directory: Path, data: Mapping[str, Sequence[Mapping[str, Any]]]) -> Dict[str, str]:
    """Write each table as a CSV with a header; its path, by table."""
    found: Dict[str, str] = {}
    for name, rows in data.items():
        path = directory / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        found[name] = str(path)
    return found
