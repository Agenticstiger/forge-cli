# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Customer-Profile-360-shaped Databricks contract with column restrictions and a row filter,
shared by the SQL, enforcement and stand-in tests."""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional, Tuple

ANALYSTS = "group:analysts@northwind.example"
STEWARDS = "group:stewards@northwind.example"
ANALYSTS_GROUP = "fluid-demo-lab-analysts"
STEWARDS_GROUP = "fluid-demo-lab-stewards"

COLUMNS: List[Tuple[str, str]] = [
    ("customer_id", "string"),
    ("msisdn", "string"),
    ("email", "string"),
    ("score", "double"),
    ("region", "string"),
    ("consent_data_analytics", "boolean"),
    ("is_suppressed", "boolean"),
]
DENIED = ["customer_id", "email", "score"]
CONSENT = "consent_data_analytics = true AND (is_suppressed IS NULL OR is_suppressed = false)"

DENY = {"principal": ANALYSTS, "columns": DENIED, "access": "deny"}
MASK = {"principal": ANALYSTS, "columns": ["msisdn"], "access": "mask", "mask": "last_four"}
FILTER = {"principal": ANALYSTS, "where": CONSENT}


def contract(
    restrictions: Optional[Iterable[Dict[str, Any]]] = (DENY, MASK),
    filters: Optional[Iterable[Dict[str, Any]]] = (FILTER,),
    *,
    principals: Optional[Dict[str, Any]] = None,
    columns: Iterable[Tuple[str, str]] = COLUMNS,
) -> Dict[str, Any]:
    authz: Dict[str, Any] = {"readers": [ANALYSTS, STEWARDS]}
    if restrictions:
        authz["columnRestrictions"] = [copy.deepcopy(r) for r in restrictions]
    if filters:
        authz["rowFilters"] = [copy.deepcopy(f) for f in filters]
    binding: Dict[str, Any] = {
        "platform": "databricks",
        "format": "delta_table",
        "location": {"database": "main", "schema": "cp360", "table": "profile"},
        "principals": (
            {ANALYSTS: ANALYSTS_GROUP, STEWARDS: STEWARDS_GROUP}
            if principals is None
            else principals
        ),
    }
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "cp360.profile",
        "name": "Customer Profile 360",
        "exposes": [
            {
                "exposeId": "profile",
                "kind": "table",
                "binding": binding,
                "policy": {"authz": authz},
                "contract": {"schema": [{"name": n, "type": t} for n, t in columns]},
            }
        ],
    }
