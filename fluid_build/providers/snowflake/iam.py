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

# fluid_build/provider/snowflake/iam.py
from __future__ import annotations

import logging
from typing import Dict, List

from .._sql_safety import validate_ident
from .connection import SnowflakeConnection
from .types import SnowflakeIdentifier
from .util.names import quote_identifier

log = logging.getLogger("fluid.provider.snowflake")

# Minimalist mapping: principals -> roles -> grants
# principals may be 'user:alice@example.com', 'group:analysts@example.com', 'role:EXISTING_ROLE'
# We compile to new roles (ROLE_<slug>) unless an explicit 'role:' is provided.


def _slugify(s: str) -> str:
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in s).upper()


def compile_table_grants(
    principal: str, db: str, schema: str, table: str, perms: List[str]
) -> List[str]:
    # Create or reuse a role, then grant usage + table privileges.
    # ``role`` flows into GRANT/CREATE ROLE DDL f-strings below; DDL
    # identifiers cannot be parameterized, so ``role`` is run through
    # ``validate_ident`` at this trust boundary. For an explicit
    # ``role:`` principal the value is caller-supplied and MUST be
    # validated; for a derived ``ROLE_<slug>`` it is already
    # alphanumeric+underscore but re-validating is cheap defense.
    if principal.startswith("role:"):
        role = validate_ident(principal.split(":", 1)[1].upper())
        create_role = None
    else:
        role = validate_ident(f"ROLE_{_slugify(principal)}")
        create_role = f"CREATE ROLE IF NOT EXISTS {quote_identifier(role)};"

    grants = []
    if create_role:
        grants.append(create_role)

    # Minimal privilege set based on perms
    # readData -> SELECT, readMetadata -> USAGE on database/schema; manage -> OWNERSHIP (not recommended)
    wants_select = "readData" in perms
    _wants_metadata = "readMetadata" in perms  # noqa: F841
    wants_manage = "manage" in perms

    grants += [
        f"GRANT USAGE ON DATABASE {quote_identifier(db)} TO ROLE {quote_identifier(role)};",
        f"GRANT USAGE ON SCHEMA {quote_identifier(db)}.{quote_identifier(schema)} "
        f"TO ROLE {quote_identifier(role)};",
    ]

    if wants_select:
        grants.append(
            f"GRANT SELECT ON TABLE {quote_identifier(db)}.{quote_identifier(schema)}."
            f"{quote_identifier(table)} TO ROLE {quote_identifier(role)};"
        )

    if wants_manage:
        # DO NOT grant OWNERSHIP automatically, log a warning and skip.
        log.warning(
            "Refusing to grant OWNERSHIP to %s on %s.%s.%s (manage is too broad).",
            role,
            db,
            schema,
            table,
        )

    return grants


def apply_access_policy(
    conn: SnowflakeConnection, table_ident: SnowflakeIdentifier, access_policy: Dict
):
    if not access_policy:
        return
    grants = access_policy.get("grants", [])
    for g in grants:
        principal = g.get("principal")
        perms = g.get("permissions", [])
        if not principal or not perms:
            continue
        stmts = compile_table_grants(
            principal, table_ident.database, table_ident.schema, table_ident.name, perms
        )
        for s in stmts:
            conn.execute(s)
