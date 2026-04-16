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

from .connection import SnowflakeConnection
from .types import SnowflakeIdentifier
from .util import backtick

log = logging.getLogger("fluid.provider.snowflake")

# Minimalist mapping: principals -> roles -> grants
# principals may be 'user:alice@example.com', 'group:analysts@example.com', 'role:EXISTING_ROLE'
# We compile to new roles (ROLE_<slug>) unless an explicit 'role:' is provided.


def _slugify(s: str) -> str:
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in s).upper()


def compile_table_grants(
    principal: str, db: str, schema: str, table: str, perms: List[str]
) -> List[str]:
    # Create or reuse a role, then grant usage + table privileges
    if principal.startswith("role:"):
        role = principal.split(":", 1)[1].upper()
        create_role = None
    else:
        role = f"ROLE_{_slugify(principal)}"
        create_role = f"CREATE ROLE IF NOT EXISTS {backtick(role)};"

    grants = []
    if create_role:
        grants.append(create_role)

    # Minimal privilege set based on perms
    # readData -> SELECT, readMetadata -> USAGE on database/schema; manage -> OWNERSHIP (not recommended)
    wants_select = "readData" in perms
    _wants_metadata = "readMetadata" in perms  # noqa: F841
    wants_manage = "manage" in perms

    grants += [
        f"GRANT USAGE ON DATABASE {backtick(db)} TO ROLE {backtick(role)};",
        f"GRANT USAGE ON SCHEMA {backtick(db)}.{backtick(schema)} TO ROLE {backtick(role)};",
    ]

    if wants_select:
        grants.append(
            f"GRANT SELECT ON TABLE {backtick(db)}.{backtick(schema)}.{backtick(table)} TO ROLE {backtick(role)};"
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
