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

"""FLUID-canonical → Snowflake type translation.

Single source of truth for FLUID's generic type names → Snowflake DDL types.
Imported by both the planner (``plan/planner.py``) and the abstract-action
recovery path in ``provider.py``; without this consolidation, the
two paths drifted and `NUMBER` columns silently degraded to `VARCHAR`.

Per /borrow-before-build receipts (search 2026-05):

- ``sqlglot.dialects.snowflake.SnowflakeGenerator.TYPE_MAPPING`` exists but
  pulling sqlglot for one translation table is overkill (~3 MB+ deps).
- ``snowflake-sqlalchemy`` exposes the dialect type classes (``VARIANT``,
  ``OBJECT``, ``TIMESTAMP_TZ``, …) but no FLUID-style canonical translator.
- ``dbt-snowflake`` owns the canonical Snowflake type table used across
  thousands of dbt projects; treated here as the *spiritual* prior art —
  https://github.com/dbt-labs/dbt-snowflake. The mapping below mirrors
  dbt's defaults for the 12 most common types (string→VARCHAR,
  integer→NUMBER(38,0), decimal→NUMBER(38,10), boolean→BOOLEAN, …).

When dbt-snowflake's defaults change in a future release, update
``_TYPE_MAP`` here and re-run the parity test in
``tests/providers/test_snowflake_type_mapping.py``.
"""

from __future__ import annotations

from typing import Dict, FrozenSet

from ..._sql_safety import SqlTypeError, validate_sql_type_param_payload

# Bare type names that take optional ``(precision, scale)`` parameters; for
# these, an explicit ``(...)`` suffix is preserved verbatim and uppercased
# (e.g. ``decimal(18,4)`` → ``DECIMAL(18,4)``).
_PARAMETERIZED_PREFIXES: FrozenSet[str] = frozenset(
    {
        "decimal",
        "numeric",
        "number",
        "varchar",
        "char",
        "character",
        "binary",
        "varbinary",
    }
)

# Lowercased FLUID-canonical type → Snowflake DDL type. Mirrors
# dbt-snowflake's adapter defaults for cross-tooling consistency.
_TYPE_MAP: Dict[str, str] = {
    "string": "VARCHAR",
    "integer": "NUMBER(38,0)",
    "int": "NUMBER(38,0)",
    "long": "NUMBER(38,0)",
    "bigint": "NUMBER(38,0)",
    # Bare ``number`` (no precision/scale) is the contract author's way of
    # saying "default numeric"; Snowflake's NUMBER default is NUMBER(38,0).
    # Without this entry the type fell through to VARCHAR, silently turning
    # numeric columns into strings.
    "number": "NUMBER(38,0)",
    "float": "FLOAT",
    "double": "DOUBLE",
    "decimal": "NUMBER(38,10)",
    "numeric": "NUMBER(38,10)",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP_NTZ",
    "datetime": "TIMESTAMP_NTZ",
    "timestamp_ntz": "TIMESTAMP_NTZ",
    "timestamp_tz": "TIMESTAMP_TZ",
    "timestamp_ltz": "TIMESTAMP_LTZ",
    "time": "TIME",
    "binary": "BINARY",
    "array": "ARRAY",
    "object": "OBJECT",
    "variant": "VARIANT",
    "geography": "GEOGRAPHY",
    "geometry": "GEOMETRY",
}


def map_fluid_type_to_snowflake(fluid_type: str) -> str:
    """Translate a FLUID-canonical type string into a Snowflake DDL type.

    Examples:

    - ``"string"`` → ``"VARCHAR"``
    - ``"integer"`` → ``"NUMBER(38,0)"``
    - ``"number"`` → ``"NUMBER(38,0)"`` (bare numeric default)
    - ``"decimal(18,4)"`` → ``"DECIMAL(18,4)"`` (parameter passthrough)
    - ``"timestamp_tz"`` → ``"TIMESTAMP_TZ"``
    - unknown type → ``"VARCHAR"`` (safe fallback)

    The parameterised passthrough branch is a SQL-injection boundary: the
    returned string is interpolated into ``CREATE TABLE`` DDL downstream.
    A malformed ``(...)`` payload — anything beyond ``(N)`` / ``(N,N)`` — is
    rejected with :class:`SqlTypeError` here rather than passed through
    verbatim (BUG-SQL-TYPE defense-in-depth).
    """
    raw_type = (fluid_type or "string").strip()
    lower_type = raw_type.lower()
    base_type = lower_type.split("(", 1)[0].strip()
    if "(" in lower_type and base_type in _PARAMETERIZED_PREFIXES:
        # Passthrough is only safe once the parameter payload is proven to be
        # ``N`` / ``N,N``. Split the inner text out of the *first* ``(`` … last
        # ``)`` and allowlist it; a trailing ``; DROP TABLE t`` (or any other
        # injection) fails the digits-and-comma check and is rejected.
        open_paren = raw_type.find("(")
        close_paren = raw_type.rfind(")")
        if close_paren <= open_paren:
            raise SqlTypeError(f"Invalid parameterised SQL type: {fluid_type!r}")
        payload = raw_type[open_paren + 1 : close_paren]
        suffix = raw_type[close_paren + 1 :]
        if suffix.strip():
            # Anything after the closing paren (e.g. ``decimal(18) ; DROP …``)
            # is not part of a type and must not survive.
            raise SqlTypeError(f"Invalid parameterised SQL type: {fluid_type!r}")
        validate_sql_type_param_payload(payload)
        return raw_type.upper()
    return _TYPE_MAP.get(lower_type, "VARCHAR")
