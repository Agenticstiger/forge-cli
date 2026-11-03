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

"""``policy.privacy.masking`` on a Databricks expose: accepted when forge-cli applies it.

The acquisition runner masks at landing, before the rows leave the machine
(``build_runners/_masking.py``), and lands them in the Databricks table
(``build_runners/_databricks_load.py``). So an expose every build of which is a DuckDB
acquisition build keeps its ``policy.privacy.masking``, as on AWS. Anything else that writes the
table (dbt, another engine, no build at all) applies no mask, and the emitter keeps refusing the
field, with the reason: applied anyway, the table would hold the values the contract promises to
mask.

``fluid validate`` and ``fluid apply`` read the verdict from the one ``_compile`` pass, so each
case below is checked through both.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac import IAC_PLUGINS
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

MASKING = [{"column": "msisdn", "strategy": "hash"}]


@pytest.fixture(autouse=True)
def _warehouse(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-0123456789abcdef")


def _build(
    build_id: str = "ingest",
    *,
    engine: str = "duckdb",
    pattern: Optional[str] = "acquisition",
    outputs: Optional[List[str]] = ("orders",),
) -> Dict[str, Any]:
    build: Dict[str, Any] = {
        "id": build_id,
        "engine": engine,
        "properties": {
            "source": {"kind": "filesystem", "mode": "full_refresh"},
            "sink": {"format": "parquet"},
        },
    }
    if pattern:
        build["pattern"] = pattern
    if outputs is not None:
        build["outputs"] = list(outputs)
    return build


def _contract(
    *builds: Dict[str, Any], masking: Any = MASKING, **expose_extra: Any
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "orders",
        "kind": "table",
        "binding": {
            "platform": "databricks",
            "format": "delta_table",
            "location": {"database": "main", "schema": "bronze", "table": "orders"},
        },
        "policy": {"authz": {"readers": ["group:analysts"]}, "privacy": {"masking": masking}},
        "contract": {
            "schema": [{"name": "id", "type": "bigint"}, {"name": "msisdn", "type": "string"}]
        },
    }
    expose.update(expose_extra)
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "bronze.orders",
        "name": "Orders",
        "exposes": [expose],
    }
    if builds:
        contract["builds"] = list(builds)
    return contract


def _emit(contract: Dict[str, Any]) -> Optional[UnsupportedBindingError]:
    try:
        IAC_PLUGINS["databricks"].emit(copy.deepcopy(contract))
    except UnsupportedBindingError as exc:
        return exc
    return None


def _validate(contract: Dict[str, Any]) -> List[str]:
    return dbx.validate_databricks_binding(copy.deepcopy(contract))[0]


def _both(contract: Dict[str, Any]) -> Optional[str]:
    """The refusal text, asserting that validate and emit agree on whether there is one."""
    refusal = _emit(contract)
    errors = _validate(contract)
    assert bool(errors) == (refusal is not None), (errors, refusal)
    if refusal is None:
        return None
    assert refusal.kind == "databricks-governance-unmapped" or refusal.kind == "governance-unmapped"
    assert "policy.privacy.masking" in str(refusal)
    assert any("policy.privacy.masking" in e for e in errors)
    return str(refusal)


# ── accepted: every build that writes the expose applies the mask ─────────


def test_a_duckdb_acquisition_build_that_writes_the_expose_makes_masking_acceptable():
    assert _both(_contract(_build())) is None


def test_a_duckdb_build_that_names_no_outputs_is_taken_to_write_it():
    assert _both(_contract(_build(outputs=None))) is None


def test_two_duckdb_acquisition_builds_both_apply_it():
    assert _both(_contract(_build("a"), _build("b"))) is None


def test_a_build_that_writes_another_expose_does_not_matter():
    other = _build("other", engine="dbt", pattern="embedded-logic", outputs=["something_else"])
    assert _both(_contract(_build(), other)) is None


def test_the_engine_is_read_case_insensitively():
    assert _both(_contract(_build(engine=" DuckDB "))) is None


def test_masking_and_column_restrictions_are_both_enforced():
    contract = _contract(_build())
    contract["exposes"][0]["policy"]["authz"]["columnRestrictions"] = [
        {"principal": "group:analysts", "columns": ["msisdn"], "access": "deny"}
    ]
    assert _both(contract) is None


def test_the_table_is_emitted_exactly_as_without_masking():
    """Masking is the runner's, not the table's: the module has no trace of it."""
    with_mask = _contract(_build())
    without = _contract(_build())
    without["exposes"][0]["policy"]["privacy"] = {}
    a = IAC_PLUGINS["databricks"].emit(copy.deepcopy(with_mask))
    b = IAC_PLUGINS["databricks"].emit(copy.deepcopy(without))
    assert a == b


# ── refused, with the reason ──────────────────────────────────────────────


def test_a_contract_with_no_build_that_writes_the_expose_is_refused():
    text = _both(_contract())
    assert "machine. No build writes it" in text and "DuckDB acquisition" in text


def test_a_dbt_build_writing_the_expose_is_refused_naming_it():
    text = _both(_contract(_build("models", engine="dbt", pattern="embedded-logic")))
    assert "'models'" in text and "dbt" in text
    assert "DuckDB acquisition" in text


def test_a_dbt_build_beside_a_duckdb_one_is_still_refused():
    text = _both(_contract(_build(), _build("models", engine="dbt-databricks", pattern=None)))
    assert "'models'" in text and "'ingest'" not in text


@pytest.mark.parametrize(
    "engine", ["dlt", "airbyte", "meltano", "kafka-connect", "debezium", "spark"]
)
def test_another_engine_applies_no_mask_here(engine):
    text = _both(_contract(_build("other", engine=engine)))
    assert "'other'" in text and engine in text


def test_a_duckdb_build_that_is_not_an_acquisition_build_applies_no_mask():
    """Embedded SQL on DuckDB does not run ``_masking`` and cannot land a Databricks table."""
    text = _both(_contract(_build("sql", engine="duckdb", pattern="embedded-logic")))
    assert "'sql'" in text


def test_a_build_with_no_engine_applies_no_mask():
    build = _build("plain")
    del build["engine"]
    assert "'plain'" in _both(_contract(build))


# ── the rest of the governance block is unchanged ─────────────────────────


@pytest.mark.parametrize(
    "extra, field",
    [
        ({"policy": {"privacy": {"rowLevelPolicy": {"a": 1}}}}, "policy.privacy.rowLevelPolicy"),
        (
            {
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "bronze", "table": "orders"},
                    "governance": {"lakeFormation": {"grants": []}},
                }
            },
            "binding.governance",
        ),
    ],
)
def test_row_level_policy_and_binding_governance_stay_refused(extra, field):
    contract = _contract(_build(), masking=[])
    contract["exposes"][0].update(extra)
    refusal = _emit(contract)
    assert refusal is not None and field in str(refusal)
    assert any(field in e for e in _validate(contract))


def test_row_level_policy_beside_accepted_masking_is_still_refused_alone():
    contract = _contract(_build())
    contract["exposes"][0]["policy"]["privacy"]["rowLevelPolicy"] = {"a": 1}
    refusal = str(_emit(contract))
    assert "policy.privacy.rowLevelPolicy" in refusal
    assert "policy.privacy.masking" not in refusal, "the accepted field is not named as refused"


@pytest.mark.parametrize("masking", [None, [], {}])
def test_an_empty_masking_block_is_not_masking(masking):
    assert _both(_contract(masking=masking)) is None


@pytest.mark.parametrize(
    "source, sink, fragment",
    [
        ({"mode": "incremental_append"}, {"format": "parquet"}, "full_refresh"),
        ({"mode": "full_refresh"}, {"format": "csv"}, "sink format"),
        ({}, {"format": "parquet"}, "modes"),
    ],
)
def test_a_landing_the_runner_would_refuse_is_refused_before_the_table_exists(
    source, sink, fragment
):
    build = _build()
    build["properties"] = {"source": {"kind": "filesystem", **source}, "sink": sink}
    refusal = _emit(_contract(build))
    assert refusal is not None and refusal.kind == "databricks-landing-unsupported"
    assert fragment in str(refusal) and "'ingest'" in str(refusal)


def test_a_placeholder_mode_is_left_to_the_emitter():
    build = _build()
    build["properties"]["source"]["mode"] = "{{ env.MODE }}"
    errors, _ = dbx.validate_databricks_binding(_contract(build))
    assert not [e for e in errors if "landing" in e]
    # at apply the contract is substituted; a variable still unset is refused there
    refusal = _emit(_contract(build))
    assert refusal is not None and refusal.kind == "databricks-landing-unsupported"


@pytest.mark.parametrize(
    "masking",
    [
        [{"column": "msisdn", "strategy": "k_anonymity"}],
        [{"column": "msisdn", "strategy": "no-such-strategy"}],
        {"column": "msisdn", "strategy": "hash"},
    ],
)
def test_a_masking_rule_the_runner_cannot_apply_is_refused(masking):
    refusal = _emit(_contract(_build(), masking=masking))
    assert refusal is not None and refusal.kind == "databricks-masking-invalid"
