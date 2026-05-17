# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Snowflake IaC plugin — FLUID contract → database / schema / table ``.tf.json``.

Translates Snowflake-bound exposures into ``snowflakedb/snowflake``
resources. A pure function of the contract; no credentials, no network.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ..naming import safe_ident
from ..versions import required_providers

# FLUID column type → Snowflake SQL type.
_SF_TYPES = {
    "string": "VARCHAR",
    "str": "VARCHAR",
    "text": "VARCHAR",
    "varchar": "VARCHAR",
    "char": "VARCHAR",
    "integer": "NUMBER(38,0)",
    "int": "NUMBER(38,0)",
    "bigint": "NUMBER(38,0)",
    "int64": "NUMBER(38,0)",
    "long": "NUMBER(38,0)",
    "float": "FLOAT",
    "double": "FLOAT",
    "float64": "FLOAT",
    "real": "FLOAT",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "date": "DATE",
    "time": "TIME",
    "timestamp": "TIMESTAMP_NTZ",
    "datetime": "TIMESTAMP_NTZ",
    "variant": "VARIANT",
    "object": "OBJECT",
    "array": "ARRAY",
    "binary": "BINARY",
    "bytes": "BINARY",
}


def _sf_type(raw: Any) -> str:
    t = str(raw or "VARCHAR").strip().lower()
    if t.startswith(("decimal", "numeric", "number")):
        # decimal(10,2) → NUMBER(10,2); a bare type widens to a safe default.
        return f"NUMBER{t[t.index('('):]}" if "(" in t else "NUMBER(38,0)"
    return _SF_TYPES.get(t, "VARCHAR")


class SnowflakeIacPlugin:
    """``IacProviderPlugin`` for Snowflake."""

    name = "snowflake"
    required_providers = required_providers("snowflake")
    # The Snowflake provider authenticates via several enterprise methods;
    # `tofu` reads whichever SNOWFLAKE_* vars are set in the environment, so
    # the emitted `.tf.json` stays credential-free regardless of method.
    credential_env_vars = (
        # Account + identity (v2 splits the account into org + account name).
        "SNOWFLAKE_ORGANIZATION_NAME",
        "SNOWFLAKE_ACCOUNT_NAME",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_ROLE",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_AUTHENTICATOR",
        # Password / MFA auth.
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_PASSCODE",
        # Key-pair (JWT) auth.
        "SNOWFLAKE_PRIVATE_KEY",
        "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
        # Programmatic access token (PAT).
        "SNOWFLAKE_TOKEN",
        # OAuth (client-credentials / authorization-code).
        "SNOWFLAKE_OAUTH_CLIENT_ID",
        "SNOWFLAKE_OAUTH_CLIENT_SECRET",
        "SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL",
    )

    def emit(self, contract: Mapping[str, Any]) -> Dict[str, Any]:
        resources: Dict[str, Dict[str, Any]] = {}
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            if binding.get("platform") != "snowflake":
                continue
            loc = binding.get("location") or {}
            fmt = binding.get("format")
            schema_cols = (exposure.get("contract") or {}).get("schema") or []
            _emit_snowflake(resources, loc, fmt, schema_cols, cid)
        return resources


def _emit_snowflake(
    resources: Dict[str, Any],
    loc: Mapping[str, Any],
    fmt: Any,
    schema_cols: List[Mapping[str, Any]],
    cid: str,
) -> None:
    database = loc.get("database")
    schema_name = loc.get("schema")
    if not (database and schema_name):
        return

    db_res = safe_ident(f"{cid}_{database}")
    sc_res = safe_ident(f"{cid}_{database}_{schema_name}")
    resources.setdefault("snowflake_database", {}).setdefault(db_res, {"name": database})
    resources.setdefault("snowflake_schema", {}).setdefault(
        sc_res,
        {"name": schema_name, "database": f"${{snowflake_database.{db_res}.name}}"},
    )

    table = loc.get("table") or loc.get("view")
    if not table:
        return
    tbl_res = safe_ident(f"{cid}_{database}_{schema_name}_{table}")
    db_ref = f"${{snowflake_database.{db_res}.name}}"
    sc_ref = f"${{snowflake_schema.{sc_res}.name}}"

    if fmt == "snowflake_view":
        resources.setdefault("snowflake_view", {})[tbl_res] = {
            "name": table,
            "database": db_ref,
            "schema": sc_ref,
            "statement": loc.get("query") or f"SELECT * FROM {table}",
        }
        return

    resources.setdefault("snowflake_table", {})[tbl_res] = {
        "name": table,
        "database": db_ref,
        "schema": sc_ref,
        "column": [
            {
                "name": col.get("name"),
                "type": _sf_type(col.get("type")),
                "nullable": not col.get("required", False),
            }
            for col in schema_cols
        ],
    }
