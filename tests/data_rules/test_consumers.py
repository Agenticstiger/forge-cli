# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What the consumers do with the new fields in v1 of RFC-data-rules.md (Consumer impact matrix).

The ODCS exporter's quality rules are pinned in ``tests/providers/odcs/test_odcs_data_rules.py``;
the dbt bridge's ``fluid_rule`` test is a later step. Here the contract is pinned for what each
consumer does today:

* ODCS: the declarations round-trip through ``fluidExtras`` (FLUID -> ODCS -> FLUID loses no
  invariant, format, placeholder or shared attribute).
* dbt: the generated project is byte-for-byte what it is without the fields.
* MCP: ``describe`` returns them with the rest of the contract; nothing compiles them.
* The contract digest covers them, so a FLUX plan sees a changed rule.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.unit


def _contract() -> Dict[str, Any]:
    def expose(expose_id: str, schema: list, **extra: Any) -> Dict[str, Any]:
        return {
            "exposeId": expose_id,
            "kind": "table",
            "binding": {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {"database": "BILLING", "schema": "CDP", "table": expose_id.upper()},
            },
            "contract": {"schema": schema, **extra.pop("contract", {})},
            **extra,
        }

    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "gold.billing.invoices",
        "name": "Invoices",
        "metadata": {"layer": "Gold", "owner": {"team": "billing-platform"}},
        "builds": [
            {
                "id": "main",
                "engine": "dbt",
                "pattern": "hybrid-reference",
                "execution": {"runtime": {"platform": "local"}},
            }
        ],
        "exposes": [
            expose(
                "invoices",
                [
                    {"name": "invoice_id", "type": "string", "required": True},
                    {"name": "contract_id", "type": "string"},
                    {"name": "net", "type": "decimal(12,2)"},
                    {"name": "gross", "type": "decimal(12,2)"},
                    {"name": "status", "type": "string"},
                    {"name": "segment", "type": "string", "sharedAttribute": "segment"},
                    {
                        "name": "tower_id",
                        "type": "string",
                        "identifierFormat": [{"prefixes": ["MSC", "TWR"], "mask": "99999"}],
                        "placeholders": {"values": ["XXXXX"], "prefixes": ["TBD_"]},
                    },
                ],
                contract={
                    "invariants": [
                        {"id": "gross_from_net", "derive": "gross", "expr": "ROUND(net * 1.19, 2)"},
                        {"id": "net_positive", "check": "net > 0", "mustBe": 0},
                    ]
                },
                semantics={
                    "relationships": [
                        {
                            "name": "contract",
                            "to": "contracts",
                            "fromColumns": ["contract_id"],
                            "toColumns": ["contract_id"],
                            "cardinality": "many_to_one",
                            "sharedColumns": ["status"],
                        }
                    ]
                },
            ),
            expose(
                "contracts",
                [
                    {"name": "contract_id", "type": "string", "required": True},
                    {"name": "status", "type": "string"},
                    {"name": "segment", "type": "string", "sharedAttribute": "segment"},
                ],
            ),
        ],
    }


def _without_new_fields(contract: Dict[str, Any]) -> Dict[str, Any]:
    contract = copy.deepcopy(contract)
    for expose in contract["exposes"]:
        expose["contract"].pop("invariants", None)
        for column in expose["contract"]["schema"]:
            for key in ("identifierFormat", "placeholders", "sharedAttribute"):
                column.pop(key, None)
        for rel in (expose.get("semantics") or {}).get("relationships", []):
            rel.pop("sharedColumns", None)
    return contract


def _by_id(contract: Dict[str, Any], expose_id: str) -> Dict[str, Any]:
    return next(e for e in contract["exposes"] if e.get("exposeId") == expose_id)


def test_the_contract_is_valid_and_lint_clean() -> None:
    from fluid_build.data_rules import lint_contract

    assert [f for f in lint_contract(_contract()) if f.severity == "error"] == []


def test_odcs_round_trip_keeps_every_declaration() -> None:
    from fluid_build.providers.odcs.provider import OdcsProvider

    contract = _contract()
    provider = OdcsProvider()
    odcs = provider.render(contract, fmt=None, expose_id="invoices")
    back = _by_id(provider.import_contract(odcs), "invoices")
    original = _by_id(contract, "invoices")
    assert back["contract"]["invariants"] == original["contract"]["invariants"]
    columns = {c["name"]: c for c in back["contract"]["schema"]}
    tower = columns["tower_id"]
    assert tower["identifierFormat"] == original["contract"]["schema"][6]["identifierFormat"]
    assert tower["placeholders"] == original["contract"]["schema"][6]["placeholders"]
    assert columns["segment"]["sharedAttribute"] == "segment"
    assert back["semantics"]["relationships"][0]["sharedColumns"] == ["status"]


def test_the_dbt_project_does_not_change() -> None:
    from fluid_build.engines.dbt import DbtEngine

    contract = _contract()
    plain = _without_new_fields(contract)
    assert DbtEngine().generate(contract, contract["builds"][0]) == DbtEngine().generate(
        plain, plain["builds"][0]
    )


def test_mcp_describe_returns_the_fields_as_information() -> None:
    from fluid_build.output_ports.mcp._handlers import tool_describe

    expose = _by_id(_contract(), "invoices")
    descriptor = SimpleNamespace(
        platform="snowflake",
        format="snowflake_table",
        table_reference='"BILLING"."CDP"."INVOICES"',
        dialect="snowflake",
        capabilities={},
    )
    state = SimpleNamespace(
        expose=expose, get_driver=lambda: SimpleNamespace(descriptor=lambda: descriptor)
    )
    described = tool_describe(state)
    assert described["contract"]["invariants"] == expose["contract"]["invariants"]
    assert described["semantics"]["relationships"][0]["sharedColumns"] == ["status"]


def test_the_contract_digest_covers_the_rules() -> None:
    from fluid_build.forge.core.plan_digest import compute_contract_digest

    contract = _contract()
    changed = copy.deepcopy(contract)
    changed["exposes"][0]["contract"]["invariants"][1]["check"] = "net >= 0"
    assert compute_contract_digest(contract) != compute_contract_digest(changed)
    masked = copy.deepcopy(contract)
    masked["exposes"][0]["contract"]["schema"][6]["identifierFormat"][0]["mask"] = "9999"
    assert compute_contract_digest(contract) != compute_contract_digest(masked)
