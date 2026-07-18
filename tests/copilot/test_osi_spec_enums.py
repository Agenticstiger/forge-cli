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

"""Pin the Apache Ossie spec vocabularies on the high-risk fields.

The Ossie core-spec constrains ``OSIExpressionDialect.dialect`` to a
finite vocabulary:

* ``dialect`` ∈ {ANSI_SQL, SNOWFLAKE, MDX, TABLEAU, DATABRICKS, MAQL,
  BIGQUERY}

``OSICustomExtension.vendor_name`` is deliberately NOT pinned to an
enum — the spec makes it a free-form string ("any vendor or
organization" may define extensions); the well-known values live in
``OSI_WELL_KNOWN_VENDORS`` as an advisory list only. A regression that
re-tightens ``vendor_name`` to a closed enum would reject valid Ossie
documents (e.g. ``GOODDATA`` / ``HONEYDEW`` extensions) — pinned here.

Without dialect enforcement, the LLM (or a typo in hand-maintained
sidecars) can silently emit off-spec strings like ``"Snowflake"`` or
``"POSTGRES"`` — these pass naive ``str`` validation but break
downstream tools (dbt Core's native OSI reader, the upstream Snowflake
Cortex converter) that key off the exact spec vocabulary.

This file pins:
  1. Every valid dialect value validates successfully.
  2. A representative set of invalid dialect values raises
     ``ValidationError``.
  3. ``vendor_name`` accepts arbitrary vendor strings (spec free-form).
  4. ``osi_dialect_from_source_type`` normalises every forge-cli
     ``--source-type`` hint to a spec-valid ``OSIDialect`` — including
     off-spec inputs like ``postgres`` / ``mysql`` / ``oracle``, which
     must land on ``ANSI_SQL``, while exact-dialect hints (``snowflake``,
     ``databricks``, ``bigquery``) round-trip to their own dialect.
  5. Every output of the mapper is itself accepted by the Pydantic
     model (round-trip safety — the whole point of tightening).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fluid_build.copilot.schemas.osi import (
    OSI_SUPPORTED_DIALECTS,
    OSI_WELL_KNOWN_VENDORS,
    OSICustomExtension,
    OSIExpressionDialect,
    osi_dialect_from_source_type,
)

# ---------------------------------------------------------------------------
# Dialect enum
# ---------------------------------------------------------------------------

VALID_DIALECTS = ["ANSI_SQL", "SNOWFLAKE", "MDX", "TABLEAU", "DATABRICKS", "MAQL", "BIGQUERY"]


def test_supported_dialects_tuple_matches_the_literal_vocabulary() -> None:
    """``OSI_SUPPORTED_DIALECTS`` is hand-synced with the ``Literal`` —
    this pin catches the two drifting apart."""
    assert sorted(OSI_SUPPORTED_DIALECTS) == sorted(VALID_DIALECTS)


@pytest.mark.parametrize("value", VALID_DIALECTS)
def test_dialect_accepts_spec_vocabulary(value: str) -> None:
    """All 7 spec-listed dialect values must validate cleanly."""
    model = OSIExpressionDialect(dialect=value, expression="col_a")
    assert model.dialect == value


@pytest.mark.parametrize(
    "value",
    [
        "POSTGRES",  # from-ddl hint — must go through the mapper first
        "MYSQL",
        "ORACLE",
        "TRINO",
        "DUCKDB",
        "REDSHIFT",
        "snowflake",  # lowercase — OSI is upper-only
        "Snowflake",  # title case
        "bigquery",  # lowercase — spec value is BIGQUERY
        "",  # empty string
        "ANSI SQL",  # space instead of underscore
        "ansi_sql",  # lowercase
    ],
)
def test_dialect_rejects_off_spec_values(value: str) -> None:
    """Off-spec strings — including legitimate engines not in the Ossie
    enum and casing mistakes — must raise ``ValidationError``."""
    with pytest.raises(ValidationError):
        OSIExpressionDialect(dialect=value, expression="col_a")


# ---------------------------------------------------------------------------
# vendor_name — free-form per the spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", list(OSI_WELL_KNOWN_VENDORS))
def test_vendor_name_accepts_well_known_vocabulary(value: str) -> None:
    """Every advisory well-known vendor must validate cleanly."""
    ext = OSICustomExtension(vendor_name=value, data='{"k": "v"}')
    assert ext.vendor_name == value


@pytest.mark.parametrize(
    "value",
    [
        "FLUID",  # fluid's own extension vendor slug
        "SEMANTIDO",  # upstream reference-model example
        "Databricks",  # mixed case — the spec's own example uses this
        "dbt_labs",
        "acme-analytics",
    ],
)
def test_vendor_name_accepts_arbitrary_vendors(value: str) -> None:
    """The spec makes ``vendor_name`` free-form. Re-tightening it to a
    closed enum would reject valid Ossie documents — pinned here."""
    ext = OSICustomExtension(vendor_name=value, data='{"k": "v"}')
    assert ext.vendor_name == value


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
        ("bigquery", "BIGQUERY"),
        ("BIGQUERY", "BIGQUERY"),
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
