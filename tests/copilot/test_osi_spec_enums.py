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

"""Pin OSI v0.1.1 enum vocabularies on two high-risk fields.

The OSI core-spec v0.1.1 constrains two fields to finite vocabularies:

* ``OSIExpressionDialect.dialect`` ∈ {ANSI_SQL, SNOWFLAKE, MDX, TABLEAU, DATABRICKS}
* ``OSICustomExtension.vendor_name`` ∈ {COMMON, SNOWFLAKE, SALESFORCE, DBT, DATABRICKS}

Without enforcement, the LLM (or a typo in hand-maintained sidecars) can
silently emit off-spec strings like ``"Snowflake"``, ``"BigQuery"``, or
``"dbt_labs"`` — these pass naive ``str`` validation but break downstream
tools (dbt, Snowflake Cortex, Databricks Unity Catalog) that key off the
exact spec vocabularies.

This file pins:
  1. Every valid value validates successfully.
  2. A representative set of invalid values raises ``ValidationError``.
  3. ``osi_dialect_from_source_type`` normalises every forge-cli
     ``--source-type`` hint to a spec-valid ``OSIDialect`` — including
     off-spec inputs like ``postgres`` / ``bigquery`` / ``mysql`` /
     ``oracle``, which must land on ``ANSI_SQL``.
  4. Every output of the mapper is itself accepted by the Pydantic
     model (round-trip safety — the whole point of tightening).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fluid_build.copilot.schemas.osi import (
    OSICustomExtension,
    OSIExpressionDialect,
    osi_dialect_from_source_type,
)

# ---------------------------------------------------------------------------
# Dialect enum
# ---------------------------------------------------------------------------

VALID_DIALECTS = ["ANSI_SQL", "SNOWFLAKE", "MDX", "TABLEAU", "DATABRICKS"]


@pytest.mark.parametrize("value", VALID_DIALECTS)
def test_dialect_accepts_spec_vocabulary(value: str) -> None:
    """All 5 spec-listed dialect values must validate cleanly."""
    model = OSIExpressionDialect(dialect=value, expression="col_a")
    assert model.dialect == value


@pytest.mark.parametrize(
    "value",
    [
        "BIGQUERY",  # real engine, but not in the OSI v0.1.1 enum
        "POSTGRES",  # from-ddl hint — must go through the mapper first
        "MYSQL",
        "ORACLE",
        "TRINO",
        "DUCKDB",
        "REDSHIFT",
        "snowflake",  # lowercase — OSI is upper-only
        "Snowflake",  # title case
        "",  # empty string
        "ANSI SQL",  # space instead of underscore
        "ansi_sql",  # lowercase
    ],
)
def test_dialect_rejects_off_spec_values(value: str) -> None:
    """Off-spec strings — including legitimate engines not in the v0.1.1 enum
    and casing mistakes — must raise ``ValidationError``."""
    with pytest.raises(ValidationError):
        OSIExpressionDialect(dialect=value, expression="col_a")


# ---------------------------------------------------------------------------
# vendor_name enum
# ---------------------------------------------------------------------------

VALID_VENDOR_NAMES = ["COMMON", "SNOWFLAKE", "SALESFORCE", "DBT", "DATABRICKS"]


@pytest.mark.parametrize("value", VALID_VENDOR_NAMES)
def test_vendor_name_accepts_spec_vocabulary(value: str) -> None:
    """All 5 spec-listed custom-extension vendors must validate cleanly."""
    ext = OSICustomExtension(vendor_name=value, data='{"k": "v"}')
    assert ext.vendor_name == value


@pytest.mark.parametrize(
    "value",
    [
        "dbt_labs",  # common typo — the spec says DBT
        "dbt",  # lowercase
        "Snowflake",  # title case
        "Oracle",  # not in the OSI v0.1.1 vendor enum
        "GOOGLE",  # no vendor slot for BigQuery / Google
        "CUSTOM",  # freeform keyword
        "",  # empty
    ],
)
def test_vendor_name_rejects_off_spec_values(value: str) -> None:
    """Off-spec vendor names must raise rather than silently round-trip."""
    with pytest.raises(ValidationError):
        OSICustomExtension(vendor_name=value, data='{"k": "v"}')


# ---------------------------------------------------------------------------
# osi_dialect_from_source_type — the mapper that keeps from-ddl spec-safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        # Spec-exact inputs round-trip unchanged.
        ("snowflake", "SNOWFLAKE"),
        ("SNOWFLAKE", "SNOWFLAKE"),
        ("Snowflake", "SNOWFLAKE"),
        ("databricks", "DATABRICKS"),
        ("DATABRICKS", "DATABRICKS"),
        # Whitespace tolerated.
        ("  snowflake  ", "SNOWFLAKE"),
        ("\tsnowflake\n", "SNOWFLAKE"),
        # None / empty fall back to ANSI_SQL.
        (None, "ANSI_SQL"),
        ("", "ANSI_SQL"),
        ("   ", "ANSI_SQL"),
        # Real engines not in the OSI enum collapse to ANSI_SQL since
        # plain column references are ANSI-compatible.
        ("postgres", "ANSI_SQL"),
        ("POSTGRES", "ANSI_SQL"),
        ("mysql", "ANSI_SQL"),
        ("oracle", "ANSI_SQL"),
        ("bigquery", "ANSI_SQL"),
        ("redshift", "ANSI_SQL"),
        ("duckdb", "ANSI_SQL"),
        ("trino", "ANSI_SQL"),
        # Completely unknown hint still yields a safe default.
        ("some_future_engine", "ANSI_SQL"),
    ],
)
def test_osi_dialect_from_source_type_mapping(source_type, expected) -> None:
    assert osi_dialect_from_source_type(source_type) == expected


@pytest.mark.parametrize(
    "source_type",
    [None, "", "snowflake", "databricks", "postgres", "bigquery", "mysql", "oracle", "trino"],
)
def test_osi_dialect_mapper_output_is_always_accepted_by_pydantic(source_type) -> None:
    """Round-trip safety: every output of the mapper must itself satisfy
    the ``OSIExpressionDialect`` Pydantic constraint. Without this, a
    future edit to the mapper could silently emit a value the schema
    rejects and the test suite would still pass.
    """
    dialect = osi_dialect_from_source_type(source_type)
    # If this raises, the mapper produced an off-spec value — contract bug.
    OSIExpressionDialect(dialect=dialect, expression="col_a")
