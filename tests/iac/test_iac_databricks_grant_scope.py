# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``accessPolicy.grants[].resources`` scopes a grant to exposes on Databricks.

The emitter evaluates the expose filters a data product writes,
``$.exposes[?(@.exposeId=='<id>')]``, and ``$.exposes[*]`` / ``$.exposes`` for every expose:
the grant lands on exactly the tables they name. Anything it cannot evaluate, or an expose id
the contract does not have, is refused (``databricks-grant-scope-unmapped``), never widened.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List

import pytest

from fluid_build.iac import IAC_PLUGINS, build_module
from fluid_build.iac.access import normalize_access_grants
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx

from .test_iac_databricks import _contract

PRINCIPAL = "group:auditors"


@pytest.fixture(autouse=True)
def _warehouse(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-0123456789abcdef")


def _two_exposes() -> Dict[str, Any]:
    contract = _contract()
    refunds = copy.deepcopy(contract["exposes"][0])
    refunds["exposeId"] = "refunds"
    refunds["binding"]["location"]["table"] = "refunds"
    contract["exposes"].append(refunds)
    return contract


def _scoped(contract: Dict[str, Any], *resources: str) -> Dict[str, Any]:
    contract["accessPolicy"]["grants"].append(
        {"principal": PRINCIPAL, "permissions": ["read"], "resources": list(resources)}
    )
    return contract


def _tables_granted(contract: Dict[str, Any], principal: str = "auditors") -> List[str]:
    """The tables (``main.sales.<name>``) a principal is granted on."""
    module = json.loads(build_module(IAC_PLUGINS["databricks"], contract))
    tables = module["resource"]["databricks_sql_table"]
    by_ref = {f"${{databricks_sql_table.{key}.id}}": t for key, t in tables.items()}
    return sorted(
        "main.sales." + by_ref[g["table"]]["name"]
        for g in module["resource"]["databricks_grant"].values()
        if "table" in g and g["principal"] == principal
    )


def _refusal(contract: Dict[str, Any]) -> UnsupportedBindingError:
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(contract)
    errors, _ = dbx.validate_databricks_binding(contract)
    assert any(exc.value.args[0][:60] in e for e in errors), "validate refuses it too"
    return exc.value


@pytest.mark.parametrize(
    "resource",
    [
        "$.exposes[?(@.exposeId=='orders')]",
        '$.exposes[?(@.exposeId=="orders")]',
        "$.exposes[?( @.exposeId == 'orders' )]",
        " $.exposes[ ?(@.exposeId=='orders') ] ",
    ],
)
def test_a_grant_scoped_to_the_one_expose_lands_on_its_table(resource):
    contract = _scoped(_contract(), resource)
    assert _tables_granted(contract) == ["main.sales.orders"]
    assert dbx.validate_databricks_binding(contract)[0] == []


def test_a_grant_scoped_to_one_of_two_exposes_lands_on_that_table_only():
    contract = _scoped(_two_exposes(), "$.exposes[?(@.exposeId=='refunds')]")
    assert _tables_granted(contract) == ["main.sales.refunds"]
    tables, _ = dbx.managed_objects(contract)
    readers = {t.expose_id: t.readers for t in tables}
    assert "auditors" in readers["refunds"] and "auditors" not in readers["orders"]


def test_a_grant_scoped_to_both_exposes_lands_on_both():
    contract = _scoped(
        _two_exposes(),
        "$.exposes[?(@.exposeId=='orders')]",
        "$.exposes[?(@.exposeId=='refunds')]",
    )
    assert _tables_granted(contract) == ["main.sales.orders", "main.sales.refunds"]


@pytest.mark.parametrize("resource", ["$.exposes[*]", "$.exposes", " $.exposes[ * ] "])
def test_every_expose_selectors_land_on_every_table(resource):
    contract = _scoped(_two_exposes(), resource)
    assert _tables_granted(contract) == ["main.sales.orders", "main.sales.refunds"]


def test_an_unscoped_grant_still_lands_on_every_table():
    contract = _two_exposes()
    contract["accessPolicy"]["grants"].append({"principal": PRINCIPAL, "permissions": ["read"]})
    assert _tables_granted(contract) == ["main.sales.orders", "main.sales.refunds"]


def test_an_expose_id_the_contract_does_not_have_is_refused():
    contract = _scoped(_contract(), "$.exposes[?(@.exposeId=='ordres')]")
    refusal = _refusal(contract)
    assert refusal.kind == "databricks-grant-scope-unmapped"
    text = str(refusal)
    assert "'ordres'" in text and "no expose" in text
    assert "reach no" not in text


@pytest.mark.parametrize(
    "resource",
    [
        "$.exposes[0]",
        "$.exposes[?(@.kind=='table')]",
        "$..orders",
        "$.exposes[?(@.exposeId=='orders' || @.exposeId=='refunds')]",
        "$.exposes[?(@.exposeId=='orders')].contract",
        "orders",
        "",
        "   ",
    ],
)
def test_a_jsonpath_the_emitter_cannot_evaluate_is_refused(resource):
    refusal = _refusal(_scoped(_two_exposes(), resource))
    assert refusal.kind == "databricks-grant-scope-unmapped"
    assert repr(resource) in str(refusal)
    assert "reach no" not in str(refusal)


def test_one_unevaluable_resource_beside_a_good_one_refuses_the_grant():
    contract = _scoped(_two_exposes(), "$.exposes[?(@.exposeId=='orders')]", "$.exposes[0]")
    assert _refusal(contract).kind == "databricks-grant-scope-unmapped"


def test_a_scope_refusal_never_emits_a_wider_grant():
    # the grant is refused, and the table privileges for it are computed for no table
    contract = _scoped(_two_exposes(), "$.exposes[0]")
    refusals: List[UnsupportedBindingError] = []
    compiled = dbx._compile(contract, {dbx.WAREHOUSE_ENV: "wh"}, validating=True)
    refusals = compiled.refusals
    assert refusals
    assert not [g for g in compiled.grants if g[2] == "auditors"]


def test_the_grant_model_parses_the_scope_once():
    contract = _scoped(_two_exposes(), "$.exposes[?(@.exposeId=='refunds')]")
    grant = next(g for g in normalize_access_grants(contract) if g.principal == "auditors")
    assert grant.scope.expose_ids == frozenset({"refunds"})
    assert not grant.scope.all_exposes and grant.scope.unevaluable == ()
    assert grant.scope.covers("refunds") and not grant.scope.covers("orders")
