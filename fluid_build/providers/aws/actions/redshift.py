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

# fluid_build/providers/aws/actions/redshift.py
"""
AWS Redshift data warehouse actions.

Implements idempotent Redshift operations:
- Schema creation and management
- Table creation from FLUID schemas
- SQL execution
- View creation
"""

import time
from typing import Any, Dict

from ..._sql_safety import build_libpq_dsn, validate_ident
from ..util.ddl import generate_redshift_ddl
from ..util.logging import duration_ms


class RedshiftConnectionPool:
    """Simple connection pool for Redshift."""

    _connections = {}

    @classmethod
    def get_connection(cls, connection_string: str):
        """Get or create connection for connection string."""
        if connection_string not in cls._connections:
            try:
                import psycopg2
            except ImportError:
                raise RuntimeError(
                    "psycopg2 not installed. Install with: pip install psycopg2-binary"
                )

            cls._connections[connection_string] = psycopg2.connect(connection_string)

        return cls._connections[connection_string]

    @classmethod
    def close_all(cls):
        """Close all connections."""
        for conn in cls._connections.values():
            try:
                conn.close()
            except Exception:
                pass
        cls._connections.clear()


def _get_connection_string(action: Dict[str, Any]) -> str:
    """Build a Redshift (libpq) connection string from action parameters.

    Routes through :func:`fluid_build.providers._sql_safety.build_libpq_dsn`
    so host/user/password values with whitespace or quotes are escaped per
    libpq syntax instead of being interpolated raw — a raw f-string DSN
    let a crafted password inject extra connection keywords.
    """
    host = action.get("host")
    port = action.get("port", 5439)
    database = action.get("database")
    user = action.get("user")
    password = action.get("password")

    if not all([host, database, user, password]):
        raise ValueError("Redshift connection requires: host, database, user, password")

    return build_libpq_dsn(
        {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
        },
        database_key="dbname",
    )


def ensure_schema(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Redshift schema exists.

    Creates schema if it doesn't exist. Idempotent operation.

    Args:
        action: Schema action with connection details

    Returns:
        Action result
    """
    start_time = time.time()

    try:
        import psycopg2
    except ImportError:
        return {
            "status": "error",
            "error": "psycopg2 not installed. Install with: pip install psycopg2-binary",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    schema = action.get("schema")
    if not schema:
        return {
            "status": "error",
            "error": "'schema' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # SECURITY_REVIEW S-006: schema flows into an f-string DDL
    # (``CREATE SCHEMA {schema}``) a few lines below, so we must validate
    # it at the trust boundary. The existing ``%s`` binding on the
    # information_schema query isn't enough — DDL identifiers can't be
    # parameterized, so validation is the only defense.
    try:
        schema = validate_ident(schema)
    except ValueError as exc:
        return {
            "status": "error",
            "error": f"Invalid schema identifier: {exc}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        conn_string = _get_connection_string(action)
        conn = RedshiftConnectionPool.get_connection(conn_string)

        with conn.cursor() as cursor:
            # Check if schema exists
            cursor.execute(
                """
                SELECT schema_name 
                FROM information_schema.schemata 
                WHERE schema_name = %s
            """,
                (schema,),
            )

            exists = cursor.fetchone() is not None

            if not exists:
                # Create schema
                cursor.execute(f"CREATE SCHEMA {schema}")
                conn.commit()

                return {
                    "status": "changed",
                    "message": f"Created schema: {schema}",
                    "duration_ms": duration_ms(start_time),
                    "changed": True,
                }
            else:
                return {
                    "status": "ok",
                    "message": f"Schema already exists: {schema}",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to create schema: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_table(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Redshift table exists.

    Creates table from FLUID schema if it doesn't exist.
    Idempotent operation.

    Args:
        action: Table action with schema and connection details

    Returns:
        Action result
    """
    start_time = time.time()

    try:
        import psycopg2
    except ImportError:
        return {
            "status": "error",
            "error": "psycopg2 not installed. Install with: pip install psycopg2-binary",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    schema = action.get("schema")
    table = action.get("table")
    columns = action.get("columns", [])

    if not schema or not table:
        return {
            "status": "error",
            "error": "'schema' and 'table' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Generate DDL from columns
        ddl = generate_redshift_ddl(
            schema=schema,
            table=table,
            columns=columns,
            dist_key=action.get("dist_key"),
            sort_keys=action.get("sort_keys", []),
            dist_style=action.get("dist_style", "AUTO"),
        )

        conn_string = _get_connection_string(action)
        conn = RedshiftConnectionPool.get_connection(conn_string)

        with conn.cursor() as cursor:
            # Check if table exists
            cursor.execute(
                """
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = %s AND table_name = %s
            """,
                (schema, table),
            )

            exists = cursor.fetchone() is not None

            if not exists:
                # Create table
                cursor.execute(ddl)
                conn.commit()

                return {
                    "status": "changed",
                    "message": f"Created table: {schema}.{table}",
                    "ddl": ddl,
                    "columns": len(columns),
                    "duration_ms": duration_ms(start_time),
                    "changed": True,
                }
            else:
                return {
                    "status": "ok",
                    "message": f"Table already exists: {schema}.{table}",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to create table: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def execute_sql(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute Redshift SQL statement.

    Args:
        action: SQL action with query and connection details

    Returns:
        Action result
    """
    start_time = time.time()

    try:
        import psycopg2
    except ImportError:
        return {
            "status": "error",
            "error": "psycopg2 not installed. Install with: pip install psycopg2-binary",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    sql = action.get("sql")
    if not sql:
        return {
            "status": "error",
            "error": "'sql' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        conn_string = _get_connection_string(action)
        conn = RedshiftConnectionPool.get_connection(conn_string)

        with conn.cursor() as cursor:
            cursor.execute(sql)

            # Check if query returned results
            if cursor.description:
                rows = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]
                results = [dict(zip(columns, row, strict=True)) for row in rows]
            else:
                results = []

            conn.commit()

            return {
                "status": "changed",
                "message": f"Executed SQL: {sql[:100]}...",
                "rows_affected": cursor.rowcount,
                "results": results[:100],  # Limit results
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to execute SQL: {str(e)}",
            "sql": sql,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_view(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create or replace Redshift view.

    Args:
        action: View action with SQL definition and connection details

    Returns:
        Action result
    """
    start_time = time.time()

    try:
        import psycopg2
    except ImportError:
        return {
            "status": "error",
            "error": "psycopg2 not installed. Install with: pip install psycopg2-binary",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    schema = action.get("schema")
    view = action.get("view")
    sql = action.get("sql")

    if not schema or not view or not sql:
        return {
            "status": "error",
            "error": "'schema', 'view', and 'sql' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # SECURITY: ``schema`` and ``view`` flow into a DDL f-string and
    # cannot be parameterized — validate both as SQL identifiers. The
    # ``{sql}`` view body is handled by a separate sprint.
    try:
        schema = validate_ident(schema)
        view = validate_ident(view)
    except ValueError as exc:
        return {
            "status": "error",
            "error": f"Invalid identifier in view definition: {exc}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        conn_string = _get_connection_string(action)
        conn = RedshiftConnectionPool.get_connection(conn_string)

        with conn.cursor() as cursor:
            # Create or replace view
            create_sql = f"CREATE OR REPLACE VIEW {schema}.{view} AS {sql}"
            cursor.execute(create_sql)
            conn.commit()

            return {
                "status": "changed",
                "message": f"Created view: {schema}.{view}",
                "sql": create_sql,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to create view: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
