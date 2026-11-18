# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The dbt engine and the IaC emitters type a column alike, on every adapter both cover.

A dbt model builds into the table ``fluid apply`` creates, and dbt recreates it with
its model contract's types. When the two disagree, each run undoes the other: dbt
wrote ``string`` for an ``int16`` the BigQuery emitter writes as INT64, and the
Snowflake emitter wrote VARCHAR for a ``smallint`` dbt writes as NUMBER. Now each
adapter has one table: BigQuery's is the IaC emitter's (``gcp._bq_sql_type``),
Snowflake's and Databricks' are the dbt engine's (``engines/dbt/_types.py``). This
test types every FLUID column type the 0.7.7 schema admits, and the parameterised
forms, both ways and fails on the first disagreement.

Out of scope here: a column that declares nested types (``element``, ``fields``),
which the dbt engine does not read yet; and a type the IaC emitter refuses, which no
table holds.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pytest

import fluid_build
from fluid_build.engines.dbt import _types as dbt
from fluid_build.iac import column_types
from fluid_build.iac.providers import databricks, gcp, snowflake

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_SCHEMA = json.loads(
    (Path(fluid_build.__file__).parent / "schemas" / "fluid-schema-0.7.7.json").read_text(
        encoding="utf-8"
    )
)
#: Every type name the 0.7.7 column schema lists, and the parameterised forms.
TYPES: Tuple[str, ...] = tuple(
    _SCHEMA["$defs"]["column"]["properties"]["type"]["anyOf"][0]["enum"]
) + (
    "decimal(18,4)",
    "decimal(38,10)",
    "decimal(10)",
    "decimal(10,0)",
    "numeric(10,2)",
    "number(12,2)",
    "decimal(60,2)",
    "bignumeric(40,20)",
    "varchar(255)",
    "char(10)",
    "timestamp(3)",
    "Decimal (12, 2)",
)

_ARGS = re.compile(r"^([A-Z_][A-Z0-9_ ]*?)\s*(?:\((.*)\))?$")


def _split(sql: str) -> Tuple[str, Optional[str]]:
    match = _ARGS.match(sql.strip().upper())
    assert match, sql
    args = match.group(2)
    return match.group(1).strip(), None if args is None else args.replace(" ", "")


def _bigquery_canonical(sql: str) -> str:
    """``NUMERIC(10)`` for ``numeric(10,0)``: BigQuery leaves a scale of 0 out."""
    base, args = _split(sql)
    if args is None:
        return base
    precision, _, scale = args.partition(",")
    return f"{base}({precision})" if scale in ("", "0") else f"{base}({precision},{scale})"


def _bigquery_iac(fluid_type: str) -> Optional[str]:
    if gcp._bq_outcome(fluid_type).status == column_types.REFUSED:
        return None
    field = gcp._bq_field({"name": "c", "type": fluid_type})
    if "precision" not in field:
        return field["type"]
    scale = f",{field['scale']}" if "scale" in field else ""
    return f"{field['type']}({field['precision']}{scale})"


def _snowflake_canonical(sql: str) -> str:
    """NUMBER for DECIMAL and NUMERIC (one type in Snowflake), NUMBER(38,0) when bare."""
    base, args = _split(sql)
    if base in ("NUMBER", "DECIMAL", "NUMERIC"):
        return f"NUMBER({args})" if args else "NUMBER(38,0)"
    return f"{base}({args})" if args else base


def _databricks_iac(fluid_type: str) -> Optional[str]:
    refusals: List[object] = []
    resolved = databricks._column_type(fluid_type, "c", refusals)
    return None if refusals else resolved


def _databricks_canonical(sql: str) -> str:
    return databricks._canonical_type(sql.strip().lower())


#: adapter -> (what the IaC emitter writes or None when it refuses, canonical form)
_ADAPTERS: Dict[str, Tuple[Callable[[str], Optional[str]], Callable[[str], str]]] = {
    "bigquery": (_bigquery_iac, _bigquery_canonical),
    "snowflake": (lambda fluid_type: snowflake._sf_type(fluid_type), _snowflake_canonical),
    "databricks": (_databricks_iac, _databricks_canonical),
}


def disagreements(adapter: str) -> List[Tuple[str, str, str]]:
    """``(type, dbt, iac)`` for every type the two write differently on ``adapter``."""
    iac_type, canonical = _ADAPTERS[adapter]
    found = []
    for fluid_type in TYPES:
        iac = iac_type(fluid_type)
        if iac is None:
            continue
        model = dbt.sql_type(fluid_type, adapter)
        if canonical(model) != canonical(iac):
            found.append((fluid_type, model, iac))
    return found


@pytest.mark.parametrize("adapter", sorted(_ADAPTERS))
def test_the_dbt_engine_and_the_iac_emitter_type_every_column_alike(adapter):
    assert disagreements(adapter) == []


def test_the_test_finds_a_dbt_type_that_drifts_on_bigquery(monkeypatch):
    monkeypatch.setattr(dbt, "_bigquery_type", lambda fluid_type: "string")
    drift = disagreements("bigquery")
    assert ("integer", "string", "INT64") in drift


def test_the_test_finds_an_emitter_type_that_drifts_on_snowflake(monkeypatch):
    """The Snowflake emitter types through the dbt table, so a change to the table moves
    both; an emitter that types on its own again is what drifts, as it did before."""
    monkeypatch.setattr(snowflake, "_sf_type", lambda fluid_type: "VARCHAR")
    drift = disagreements("snowflake")
    assert ("smallint", "number", "VARCHAR") in drift


def test_every_type_is_typed_on_every_adapter():
    """Nothing in the list was skipped by mistake: each adapter types most of it."""
    for adapter, (iac_type, _canonical) in _ADAPTERS.items():
        typed = [t for t in TYPES if iac_type(t) is not None]
        assert len(typed) > len(TYPES) // 2, adapter


@pytest.mark.parametrize(
    "fluid_type, bigquery, snowflake_type",
    [
        ("int16", "int64", "NUMBER(38,0)"),
        ("smallint", "int64", "NUMBER(38,0)"),
        ("timestamp_ntz", "datetime", "TIMESTAMP_NTZ"),
        ("timestamp_ltz", "timestamp", "TIMESTAMP_LTZ"),
        ("timestamptz", "timestamp", "TIMESTAMP_TZ"),
        ("json", "json", "VARIANT"),
        ("money", "numeric", "NUMBER(19,4)"),
        ("varchar(max)", "string", "VARCHAR"),
        ("tinyint(1)", "int64", "NUMBER(38,0)"),
        ("bytes", "bytes", "BINARY"),
        ("decimal(18,4)", "numeric(18,4)", "NUMBER(18,4)"),
        ("decimal(38,10)", "bignumeric(38,10)", "NUMBER(38,10)"),
        ("varchar(255)", "string", "VARCHAR(255)"),
        ("bignumeric(40,20)", "bignumeric(40,20)", "VARCHAR"),
    ],
)
def test_the_agreed_types(fluid_type, bigquery, snowflake_type):
    """What both write now, for the types that disagreed before."""
    assert dbt.sql_type(fluid_type, "bigquery") == bigquery
    assert snowflake._sf_type(fluid_type) == snowflake_type
