# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Shared in-memory contract / expose fixtures for the output-port tests.

Kept as a lightweight Python module rather than YAML so the tests
can build variants programmatically (e.g. add a column restriction
in one test without touching the others).
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any, Dict, List


def make_expose(
    *,
    expose_id: str = "customer_profiles",
    kind: str = "table",
    columns: List[Dict[str, Any]] = None,
    semantics: Dict[str, Any] = None,
    binding: Dict[str, Any] = None,
    column_restrictions: List[Dict[str, Any]] = None,
    privacy_masking: List[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": expose_id,
        "kind": kind,
        "contract": {
            "schema": columns
            or [
                {"name": "customer_id", "type": "STRING", "required": True},
                {"name": "email", "type": "STRING", "sensitivity": "pii"},
                {"name": "lifetime_value_usd", "type": "FLOAT64"},
                {"name": "signup_date", "type": "DATE"},
            ],
        },
        "binding": binding
        or {
            "platform": "local",
            "format": "csv",
            "location": {"path": "<set-by-test>", "table": "customer_profiles"},
        },
    }
    if semantics is not None:
        expose["semantics"] = semantics
    if column_restrictions is not None:
        expose.setdefault("policy", {}).setdefault("authz", {})[
            "columnRestrictions"
        ] = column_restrictions
    if privacy_masking is not None:
        expose.setdefault("policy", {}).setdefault("privacy", {})["masking"] = privacy_masking
    return expose


def make_contract(*, exposes: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "gold.test.contract",
        "name": "Test contract",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "qa", "email": "qa@example.com"},
            "businessContext": {"domain": "Test"},
        },
        "exposes": exposes
        or [
            make_expose(
                semantics={
                    "name": "customer_profiles",
                    "measures": [
                        {"name": "customer_count", "agg": "count_distinct", "expr": "customer_id"},
                        {"name": "total_ltv_usd", "agg": "sum", "expr": "lifetime_value_usd"},
                    ],
                    "dimensions": [
                        {"name": "signup_date", "type": "time"},
                    ],
                    "metrics": [
                        {"name": "active_customers", "type": "simple", "measure": "customer_count"},
                    ],
                },
            ),
        ],
    }


def write_customer_csv(path: Path) -> Path:
    """Write a tiny customer CSV for DuckDB integration tests."""
    rows = [
        {
            "customer_id": "C0001",
            "email": "alice@example.com",
            "lifetime_value_usd": "1200.50",
            "signup_date": "2024-01-15",
        },
        {
            "customer_id": "C0002",
            "email": "bob@example.com",
            "lifetime_value_usd": "850.00",
            "signup_date": "2024-02-10",
        },
        {
            "customer_id": "C0003",
            "email": "carol@example.com",
            "lifetime_value_usd": "300.75",
            "signup_date": "2024-03-05",
        },
    ]
    fieldnames = list(rows[0].keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
