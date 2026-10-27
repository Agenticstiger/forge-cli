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

"""Exported lineage says which version, and a quality expression is executable.

Two defects found by feeding 0.23.0's exports to a resolver and a DQ engine.

An input port claimed ``version: 1.0.0`` whatever the contract said, because
the only version in play was the consume's own or the provider's constant — a
consume stating ``versionConstraint: ^2.1`` has neither, so the export asserted
a pin the contract never made. And an output port named no upstreams at all
unless the contract had been imported from ODPS, so a catalog reading lineage
from ``outputPorts[].inputContracts`` saw a product with no dependencies.

Separately, ``exposes[].contract.quality[].expression`` is a row predicate, and
it was emitted as the ODCS ``sql`` rule's ``query`` verbatim. ODCS defines that
field as "an individual SQL query that returns a value that can be compared",
so an engine executing it received a fragment.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.mappers.quality import _expression_to_quality
from fluid_build.providers.odps_standard.mappers.ports import (
    _input_contracts_from_ports,
    _range_lower_bound,
)
from fluid_build.providers.odps_standard.validation import load_schema, validate
from fluid_build.schema_manager import FluidSchemaManager

# The v1.1 type tests already define provider construction at a chosen
# apiVersion and the render-to-product step; reused rather than spelled a second
# time, the way tests/providers/test_duckdb_sandbox_workspace_root.py reuses a
# sibling module's contract fixtures.
from tests.providers.test_odps_v11_type import _product, _provider

pytestmark = pytest.mark.unit

PRODUCT_ID = "gold.sales.orders"
UPSTREAM_CONTRACT_ID = "silver.crm.core.customers"
EXPRESSION = "preferred_customer_flag IN ('Y', 'N')"


def _contract(consume: Optional[Dict[str, Any]] = None, **kw: Any) -> Dict[str, Any]:
    """A 0.7.6 contract with one consume and one expose carrying a quality rule."""
    base_consume: Dict[str, Any] = {"productId": "silver.crm.core", "exposeId": "customers"}
    if consume:
        base_consume.update(consume)
    contract: Dict[str, Any] = {
        "fluidVersion": kw.pop("fluid_version", "0.7.6"),
        "kind": "DataProduct",
        "id": PRODUCT_ID,
        "name": "orders",
        "metadata": {"owner": {"team": "commerce"}},
        "consumes": [base_consume],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "./orders"},
                },
                "contract": {
                    "schema": [{"name": "preferred_customer_flag", "type": "string"}],
                    "quality": [
                        {"rule": "flag_domain", "expression": EXPRESSION, "severity": "error"}
                    ],
                },
            }
        ],
    }
    contract.update(kw)
    return contract


def _odps(contract: Dict[str, Any], api_version: str = "v1.0.0") -> Dict[str, Any]:
    return _product(_provider(api_version), contract)


def _sql_rules(odcs: Dict[str, Any]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            rules = node.get("quality")
            if isinstance(rules, list):
                found.extend(r for r in rules if isinstance(r, dict) and r.get("type") == "sql")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(odcs)
    return found


# ---------------------------------------------------------------------------
# 19. the version an input port claims
# ---------------------------------------------------------------------------


class TestTheLowerBoundHelper:
    @pytest.mark.parametrize(
        ("constraint", "expected"),
        [
            ("^2.1", "2.1.0"),
            ("^2.1.3", "2.1.3"),
            ("~1.4", "1.4.0"),
            (">=3.0.0", "3.0.0"),
            ("=2.0.0", "2.0.0"),
            ("2.1.0", "2.1.0"),
            ("2.1", "2.1.0"),
            ("  ^2.1  ", "2.1.0"),
        ],
    )
    def test_the_documented_forms_resolve(self, constraint, expected):
        assert _range_lower_bound(constraint) == expected

    @pytest.mark.parametrize(
        "constraint",
        [
            ">1.0.0",  # does NOT accept 1.0.0; its real lower bound is unnameable
            ">=1.0.0,<2.0.0",
            "^1.0 || ^2.0",
            "1.x",
            "1.*",
            "<2.0.0",
            "<=2.0.0",
            "latest",
            "^١.٢",  # Arabic-Indic digits: str.isdigit() is true for these
            "^2.²",  # superscript two likewise
            "",
            "   ",
            None,
            2.1,
        ],
    )
    def test_anything_else_refuses_rather_than_guesses(self, constraint):
        """A wrong exact version in exported lineage reads as an established
        fact, so refusing and falling back beats guessing."""
        assert _range_lower_bound(constraint) is None


class TestTheInputPortVersion:
    @pytest.mark.parametrize(
        ("consume", "expected"),
        [
            pytest.param({"versionConstraint": "^2.1"}, "2.1.0", id="caret"),
            pytest.param({"versionConstraint": "~1.4"}, "1.4.0", id="tilde"),
            pytest.param({"versionConstraint": ">=3.0.0"}, "3.0.0", id="gte"),
            pytest.param({"versionConstraint": "2.1.0"}, "2.1.0", id="exact-range"),
            pytest.param(
                {"versionConstraint": "^2.1", "contractVersion": "2.3.4"},
                "2.3.4",
                id="contract-version-wins",
            ),
            pytest.param(
                {"versionConstraint": ">=1.0.0,<2.0.0"}, "1.0.0", id="compound-falls-back"
            ),
            pytest.param({"versionConstraint": "1.x"}, "1.0.0", id="wildcard-falls-back"),
            pytest.param({"versionConstraint": ">1.0.0"}, "1.0.0", id="exclusive-falls-back"),
            pytest.param({}, "1.0.0", id="nothing-to-go-on"),
        ],
    )
    def test_the_version_an_input_port_claims(self, consume, expected):
        """The acceptance case is ``^2.1``, which used to export ``version:
        1.0.0`` — a pin the contract never made.

        A range the resolver cannot read still falls back to that constant rather
        than guessing, and the document stays valid either way, which is why the
        chain ends at a constant: ODPS v1.0.0 has ``version`` in ``InputPort``'s
        ``required``, so the field cannot simply be omitted.
        """
        odps = _odps(_contract(consume))
        assert odps["inputPorts"][0]["version"] == expected
        validate(odps, load_schema("v1.0.0"))

    def test_the_range_still_rides_along_in_its_custom_property(self):
        """Resolving a version must not lose the range the contract accepts."""
        odps = _odps(_contract({"versionConstraint": "^2.1"}))
        props = odps["inputPorts"][0]["customProperties"]
        assert [p["value"] for p in props if p["property"] == "fluidVersionConstraint"] == ["^2.1"]


# ---------------------------------------------------------------------------
# 19. the lineage an output port carries
# ---------------------------------------------------------------------------


class TestOutputPortLineage:
    def test_an_output_port_names_each_consume(self):
        odps = _odps(_contract({"versionConstraint": "^2.1"}))
        assert odps["outputPorts"][0]["inputContracts"] == [
            {"id": UPSTREAM_CONTRACT_ID, "version": "2.1.0"}
        ]

    def test_the_lineage_version_equals_the_input_port_version(self):
        """One resolution, two places — they cannot disagree."""
        odps = _odps(_contract({"versionConstraint": "~1.4"}))
        port = odps["inputPorts"][0]
        assert odps["outputPorts"][0]["inputContracts"] == [
            {"id": port["contractId"], "version": port["version"]}
        ]

    def test_the_entries_carry_only_what_bitol_allows(self):
        """``$defs.InputContract`` is additionalProperties:false with exactly
        ``id`` and ``version``."""
        odps = _odps(_contract({"versionConstraint": "^2.1"}))
        for entry in odps["outputPorts"][0]["inputContracts"]:
            assert set(entry) == {"id", "version"}

    @pytest.mark.parametrize("api_version", ["v1.0.0", "v1.1.0"])
    def test_the_document_still_validates(self, api_version):
        odps = _odps(_contract({"versionConstraint": "^2.1"}), api_version=api_version)
        validate(odps, load_schema(api_version))

    def test_duplicate_upstreams_collapse_to_one_entry(self):
        ports = [
            {"name": "a", "contractId": "x.y", "version": "1.0.0"},
            {"name": "b", "contractId": "x.y", "version": "1.0.0"},
            {"name": "c", "contractId": "x.z", "version": "2.0.0"},
        ]
        assert _input_contracts_from_ports(ports) == [
            {"id": "x.y", "version": "1.0.0"},
            {"id": "x.z", "version": "2.0.0"},
        ]

    def test_a_port_missing_either_half_is_skipped(self):
        assert _input_contracts_from_ports([{"name": "a"}]) == []
        assert _input_contracts_from_ports([{"name": "a", "contractId": "x.y"}]) == []

    def test_an_imported_documents_own_lineage_wins(self):
        """Import then export must stay a fixed point: the upstream stated these,
        and re-deriving from our consumes could name different contracts."""
        contract = _contract({"versionConstraint": "^2.1"})
        contract["exposes"][0]["odps_passthrough"] = {
            "input_contracts": [{"id": "vendor.supplied", "version": "9.9.9"}]
        }
        odps = _odps(contract)
        assert odps["outputPorts"][0]["inputContracts"] == [
            {"id": "vendor.supplied", "version": "9.9.9"}
        ]

    def test_an_imported_documents_input_ports_are_not_treated_as_lineage(self):
        """``expects[]`` is the carrier an imported ODPS document's input ports
        arrive on, so deriving lineage from it would make import then export add
        a dependency the source document never stated."""
        contract = _contract()
        contract.pop("consumes")
        contract["expects"] = [
            {"id": "customers_raw", "version": "1", "contractId": "vendor.customers_raw"}
        ]
        odps = _odps(contract)
        assert odps["inputPorts"][0]["contractId"] == "vendor.customers_raw"
        assert "inputContracts" not in odps["outputPorts"][0]

    def test_a_contract_with_no_consumes_gets_no_lineage_key(self):
        contract = _contract()
        contract.pop("consumes")
        odps = _odps(contract)
        assert "inputContracts" not in odps["outputPorts"][0]


# ---------------------------------------------------------------------------
# 19. the new field is preview-gated
# ---------------------------------------------------------------------------


class TestTheFieldIsPreviewGated:
    def test_it_validates_at_0_7_6(self):
        contract = _contract({"versionConstraint": "^2.1", "contractVersion": "2.3.4"})
        result = FluidSchemaManager().validate_contract(contract, offline_only=True)
        assert result.is_valid, [str(e) for e in result.errors]

    def test_0_7_5_rejects_it(self):
        contract = _contract(
            {"versionConstraint": "^2.1", "contractVersion": "2.3.4"}, fluid_version="0.7.5"
        )
        result = FluidSchemaManager().validate_contract(contract, offline_only=True)
        assert not result.is_valid
        assert any("contractVersion" in str(e) for e in result.errors)

    def test_a_non_semver_value_is_rejected_with_the_pattern(self):
        contract = _contract({"contractVersion": "2.1"})
        result = FluidSchemaManager().validate_contract(contract, offline_only=True)
        assert not result.is_valid
        assert any("contractVersion" in str(e) for e in result.errors)


# ---------------------------------------------------------------------------
# 20. a quality expression an engine can execute
# ---------------------------------------------------------------------------


class TestTheQualityExpression:
    def test_a_predicate_becomes_a_counting_query_compared_to_zero(self):
        rule = _expression_to_quality({"expression": EXPRESSION})
        assert rule["type"] == "sql"
        assert rule["query"] == (
            f"SELECT COUNT(*) FROM ${{table}} WHERE ({EXPRESSION}) IS NOT TRUE"
        )
        assert rule["mustBe"] == 0

    def test_it_uses_the_placeholder_from_the_odcs_example(self):
        rule = _expression_to_quality({"expression": EXPRESSION})
        assert "${table}" in rule["query"]

    def test_it_negates_with_is_not_true_not_a_bare_not(self):
        """Pins the three-valued-logic choice. ``NOT (p)`` is NULL when ``p`` is
        NULL, so a naive negation counts no failure for a NULL row — and a row
        whose predicate did not evaluate TRUE has not held."""
        rule = _expression_to_quality({"expression": EXPRESSION})
        assert "IS NOT TRUE" in rule["query"]
        assert f"NOT ({EXPRESSION})" not in rule["query"]

    @pytest.mark.parametrize("prefix", ["SELECT", "select", "WITH", "  SELECT"])
    def test_an_expression_that_is_already_a_query_passes_through(self, prefix):
        expression = f"{prefix} COUNT(*) FROM orders"
        rule = _expression_to_quality({"expression": expression})
        assert rule["query"] == expression.strip()
        assert rule["mustBe"] is True

    def test_the_exported_odcs_validates(self):
        from fluid_build.providers.odcs.validation import collect_errors
        from fluid_build.providers.odcs.validation import load_schema as odcs_schema

        odcs = OdcsProvider().render(_contract({"versionConstraint": "^2.1"}), expose_id="orders")
        assert collect_errors(odcs, odcs_schema()) == []

    def test_the_rule_reaches_the_exported_document(self):
        odcs = OdcsProvider().render(_contract(), expose_id="orders")
        queries = [r["query"] for r in _sql_rules(odcs)]
        assert any("SELECT COUNT(*)" in q and "IS NOT TRUE" in q for q in queries)


class TestTheQueryActuallyRuns:
    """The point of the fix is that an engine can execute what we emit, so run it."""

    @staticmethod
    def _count(con: Any, query: str) -> int:
        return con.execute(query.replace("${table}", "t")).fetchone()[0]

    def test_duckdb_executes_it_and_counts_the_failing_rows(self):
        duckdb = pytest.importorskip("duckdb")
        query = _expression_to_quality({"expression": EXPRESSION})["query"]

        con = duckdb.connect()
        con.execute("CREATE TABLE t (preferred_customer_flag VARCHAR)")
        con.execute("INSERT INTO t VALUES ('Y'), ('N')")
        assert self._count(con, query) == 0

        con.execute("INSERT INTO t VALUES ('maybe')")
        assert self._count(con, query) == 1

        con.execute("INSERT INTO t VALUES (NULL)")
        assert self._count(con, query) == 2, "a NULL row did not evaluate TRUE, so it failed"

    def test_the_bare_not_form_would_have_missed_the_null_row(self):
        """Why ``IS NOT TRUE`` rather than ``NOT (...)``, measured rather than
        argued. If this ever stops holding, the choice above is free to change."""
        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect()
        con.execute("CREATE TABLE t (preferred_customer_flag VARCHAR)")
        con.execute("INSERT INTO t VALUES ('Y'), ('maybe'), (NULL)")

        emitted = _expression_to_quality({"expression": EXPRESSION})["query"]
        naive = f"SELECT COUNT(*) FROM ${{table}} WHERE NOT ({EXPRESSION})"

        assert self._count(con, emitted) == 2
        assert self._count(con, naive) == 1


class TestTheNoOpCases:
    def test_a_contract_without_quality_exports_no_sql_rule(self):
        contract = _contract()
        contract["exposes"][0]["contract"].pop("quality")
        odcs = OdcsProvider().render(contract, expose_id="orders")
        assert [r for r in _sql_rules(odcs) if "IS NOT TRUE" in r.get("query", "")] == []
