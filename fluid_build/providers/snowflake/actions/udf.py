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

# fluid_build/providers/snowflake/actions/udf.py
"""Snowflake user-defined function (UDF) operations."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..._sql_safety import validate_sql_language, validate_sql_type
from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import build_qualified_name, normalize_table_name


def ensure_udf(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """
    Create or replace Snowflake user-defined function (UDF).

    UDFs enable custom transformations and calculations.
    """
    start_time = time.time()

    database = action["database"]
    schema = action["schema"]
    name = normalize_table_name(action["name"])
    language = action.get("language", "SQL")  # SQL, JAVASCRIPT, PYTHON, JAVA, SCALA
    return_type = action["return_type"]
    body = action["body"]
    parameters = action.get("parameters", [])
    account = action["account"]

    provider.debug_kv(
        event="ensure_udf_started", database=database, schema=schema, name=name, language=language
    )

    try:
        params = get_connection_params(
            account=account,
            warehouse=provider.warehouse,
            database=database,
            schema=schema,
            **provider._kwargs,
        )

        with SnowflakeConnection(**params) as conn:
            qualified_name = build_qualified_name(database, schema, name)

            # Build parameter list. ``param_type`` is interpolated raw into the
            # CREATE FUNCTION DDL — route every type through the allowlist so a
            # contract param ``type`` cannot smuggle DDL (BUG-SQL-TYPE).
            param_list = []
            for param in parameters:
                param_name = param.get("name")
                param_type = param.get("type")
                if param_name and param_type:
                    param_list.append(f"{param_name} {validate_sql_type(param_type)}")

            params_str = ", ".join(param_list) if param_list else ""

            # Create or replace UDF. ``return_type`` and ``language`` are
            # raw-interpolated — allowlist both. The ``body`` is legitimately
            # arbitrary function code and is intentionally left unvalidated.
            create_sql = f"CREATE OR REPLACE FUNCTION {qualified_name}({params_str})\n"
            create_sql += f"RETURNS {validate_sql_type(return_type)}\n"
            create_sql += f"LANGUAGE {validate_sql_language(language)}\n"
            create_sql += f"AS\n{body}"

            conn.execute(create_sql)

            provider.info_kv(
                event="udf_created",
                database=database,
                schema=schema,
                name=name,
                language=language,
                return_type=return_type,
            )

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "schema": schema,
                "name": name,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(
            event="ensure_udf_failed", database=database, schema=schema, name=name, error=str(e)
        )
        raise
