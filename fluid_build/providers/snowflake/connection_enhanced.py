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

# fluid_build/provider/snowflake/connection.py
"""
Enhanced Snowflake connection management with pooling and retry capabilities.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any, Dict, List, Optional

try:
    import snowflake.connector

    SNOWFLAKE_AVAILABLE = True
except ImportError:
    SNOWFLAKE_AVAILABLE = False
    snowflake = None

from .types import ProviderOptions

logger = logging.getLogger("fluid_build.providers.snowflake.connection")


@dataclass
class ConnectionMetrics:
    """Connection usage metrics."""

    created: int = 0
    reused: int = 0
    errors: int = 0
    total_queries: int = 0
    avg_response_time: float = 0.0


class SnowflakeConnection:
    """Enhanced Snowflake connection with retry capabilities and monitoring."""

    def __init__(self, opts: ProviderOptions):
        self.opts = opts
        self._conn = None
        self._created_at = time.time()
        self._last_used = time.time()
        self._query_count = 0
        self._lock = threading.Lock()

    def __enter__(self):
        if self._conn is None:
            self._conn = self._connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        # Don't close connection in context manager for pooling
        # Connection will be returned to pool or closed by pool manager
        pass

    def close(self):
        """Explicitly close the connection."""
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception as e:
            logger.warning(f"Error closing connection: {e}")
        finally:
            self._conn = None

    def create_connection(self):
        """Create a new database connection."""
        return self._connect()

    def retry_logic(self, func, *args, **kwargs):
        """Execute function with retry logic."""
        max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise e
                time.sleep(retry_delay * (2**attempt))

    def health_check(self) -> bool:
        """Check if connection is healthy and usable."""
        return self.is_healthy

    @property
    def is_healthy(self) -> bool:
        """Check if connection is healthy and usable."""
        if self._conn is None:
            return False

        try:
            # Simple health check query
            cursor = self._conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except Exception:
            return False

    @property
    def age_seconds(self) -> float:
        """Get connection age in seconds."""
        return time.time() - self._created_at

    @property
    def idle_seconds(self) -> float:
        """Get idle time in seconds."""
        return time.time() - self._last_used

    def _connect(self):
        """Create authenticated Snowflake connection with retry logic."""
        if not SNOWFLAKE_AVAILABLE:
            raise RuntimeError(
                "snowflake-connector-python not installed. "
                "Install with: pip install snowflake-connector-python"
            )

        max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                kwargs: Dict[str, Any] = dict(
                    account=self.opts.account,
                    user=self.opts.user,
                    role=self.opts.role,
                    warehouse=self.opts.warehouse,
                    database=self.opts.database,
                    schema=self.opts.schema,
                    session_parameters=self.opts.session_params or {"QUERY_TAG": "fluid-forge"},
                    # Enhanced connection settings
                    login_timeout=getattr(self.opts, "login_timeout", 30),
                    network_timeout=getattr(self.opts, "connection_timeout", 60),
                    client_session_keep_alive=getattr(self.opts, "client_session_keep_alive", True),
                    # Connection optimization
                    paramstyle="qmark",
                    autocommit=True,
                )

                # Authentication method selection
                if self.opts.oauth_token:
                    kwargs["authenticator"] = "oauth"
                    kwargs["token"] = self.opts.oauth_token
                elif getattr(self.opts, "authenticator", None):
                    kwargs["authenticator"] = self.opts.authenticator
                elif self.opts.private_key_path:
                    # Key pair authentication
                    from cryptography.hazmat.primitives import serialization

                    with open(self.opts.private_key_path, "rb") as f:
                        pkey = serialization.load_pem_private_key(
                            f.read(),
                            password=(
                                self.opts.private_key_passphrase.encode("utf-8")
                                if self.opts.private_key_passphrase
                                else None
                            ),
                        )

                    pkb = pkey.private_bytes(
                        serialization.Encoding.DER,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                    kwargs["private_key"] = pkb
                else:
                    # Password authentication
                    kwargs["password"] = self.opts.password

                logger.debug(f"Connecting to Snowflake (attempt {attempt + 1})")
                conn = snowflake.connector.connect(**kwargs)

                # Verify connection with simple query
                cursor = conn.cursor()
                cursor.execute("SELECT CURRENT_VERSION()")
                cursor.close()

                logger.info("Successfully connected to Snowflake")
                return conn

            except Exception as e:
                logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (2**attempt))  # Exponential backoff
                else:
                    raise Exception(f"Failed to connect after {max_retries} attempts: {e}")

    def execute(
        self, sql: str, params: Optional[Iterable] = None, many: bool = False, fetch: bool = True
    ) -> Optional[List[Any]]:
        """Execute SQL with enhanced error handling and monitoring."""
        start_time = time.time()

        with self._lock:
            self._last_used = time.time()
            self._query_count += 1

        try:
            if self._conn is None:
                self._conn = self._connect()

            cursor = self._conn.cursor()

            try:
                if params and many:
                    cursor.executemany(sql, params)
                elif params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)

                # Fetch results if requested
                results = None
                if fetch and cursor.description:
                    results = cursor.fetchall()

                duration = time.time() - start_time
                logger.debug(f"Query executed in {duration:.3f}s: {sql[:100]}...")

                return results

            finally:
                cursor.close()

        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Query failed after {duration:.3f}s: {sql[:100]}... Error: {e}")
            raise

    def executescript(self, script: str):
        """Execute multi-statement SQL script."""
        statements = self._split_sql_statements(script)
        results = []

        for stmt in statements:
            if stmt.strip():
                result = self.execute(stmt, fetch=False)
                results.append(result)

        return results

    def _split_sql_statements(self, script: str) -> List[str]:
        """Split SQL script into individual statements."""
        # Simple statement splitting - can be enhanced for complex cases
        statements = []
        current_statement = []

        for line in script.split("\n"):
            line = line.strip()
            if not line or line.startswith("--"):
                continue

            current_statement.append(line)

            if line.endswith(";"):
                statements.append(" ".join(current_statement))
                current_statement = []

        # Add any remaining statement
        if current_statement:
            statements.append(" ".join(current_statement))

        return statements


class ConnectionPool:
    """Connection pool for managing Snowflake connections."""

    def __init__(self, opts: ProviderOptions):
        self.opts = opts
        self.pool_size = getattr(opts, "pool_size", 5)
        self.max_overflow = getattr(opts, "max_overflow", 10)

        self._pool: Queue[SnowflakeConnection] = Queue(maxsize=self.pool_size)
        self._overflow_connections: List[SnowflakeConnection] = []
        self._lock = threading.Lock()
        self._metrics = ConnectionMetrics()

        # Connection lifecycle settings
        self.max_connection_age = 3600  # 1 hour
        self.max_idle_time = 600  # 10 minutes

        # Pre-create initial connections
        self._initialize_pool()

        logger.info(f"Connection pool initialized with {self.pool_size} connections")

    def _initialize_pool(self):
        """Initialize the connection pool with initial connections."""
        for _ in range(min(2, self.pool_size)):  # Start with 2 connections
            try:
                conn = SnowflakeConnection(self.opts)
                self._pool.put(conn, block=False)
                self._metrics.created += 1
            except Exception as e:
                logger.warning(f"Failed to create initial connection: {e}")

    @contextmanager
    def get_connection(self):
        """Get a connection from the pool."""
        conn = None
        is_overflow = False

        try:
            # Try to get connection from pool
            try:
                conn = self._pool.get(block=False)
                self._metrics.reused += 1
            except Empty:
                # Pool is empty, create overflow connection if allowed
                with self._lock:
                    if len(self._overflow_connections) < self.max_overflow:
                        conn = SnowflakeConnection(self.opts)
                        self._overflow_connections.append(conn)
                        is_overflow = True
                        self._metrics.created += 1
                    else:
                        # Wait for connection to become available
                        conn = self._pool.get(timeout=30)
                        self._metrics.reused += 1

            # Validate connection health
            if not conn.is_healthy:
                logger.warning("Unhealthy connection detected, creating new one")
                conn.close()
                conn = SnowflakeConnection(self.opts)
                self._metrics.created += 1

            # Check connection age
            if conn.age_seconds > self.max_connection_age:
                logger.debug("Connection too old, creating new one")
                conn.close()
                conn = SnowflakeConnection(self.opts)
                self._metrics.created += 1

            yield conn

        except Exception as e:
            self._metrics.errors += 1
            logger.error(f"Connection pool error: {e}")
            raise
        finally:
            if conn:
                # Return connection to pool or clean up overflow
                if is_overflow:
                    with self._lock:
                        if conn in self._overflow_connections:
                            self._overflow_connections.remove(conn)
                        conn.close()
                else:
                    try:
                        # Return healthy connections to pool
                        if conn.is_healthy and conn.idle_seconds < self.max_idle_time:
                            self._pool.put(conn, block=False)
                        else:
                            conn.close()
                    except Exception:
                        conn.close()

    def return_connection(self, conn):
        """Return a connection to the pool."""
        try:
            # Return healthy connections to pool
            if conn.is_healthy and conn.idle_seconds < self.max_idle_time:
                self._pool.put(conn, block=False)
            else:
                conn.close()
        except Exception:
            conn.close()

    def get_metrics(self) -> Dict[str, Any]:
        """Get connection pool metrics."""
        with self._lock:
            return {
                "pool_size": self.pool_size,
                "active_connections": self.pool_size - self._pool.qsize(),
                "overflow_connections": len(self._overflow_connections),
                "total_created": self._metrics.created,
                "total_reused": self._metrics.reused,
                "total_errors": self._metrics.errors,
                "pool_utilization": (self.pool_size - self._pool.qsize()) / self.pool_size,
            }

    def close_all(self):
        """Close all connections in the pool."""
        # Close pool connections
        while not self._pool.empty():
            try:
                conn = self._pool.get(block=False)
                conn.close()
            except Empty:
                break

        # Close overflow connections
        with self._lock:
            for conn in self._overflow_connections:
                conn.close()
            self._overflow_connections.clear()

        logger.info("All connections closed")

    def health_check(self) -> Dict[str, Any]:
        """Perform health check on the connection pool."""
        healthy_connections = 0
        total_connections = 0

        # Check pool connections
        temp_connections = []
        while not self._pool.empty():
            try:
                conn = self._pool.get(block=False)
                total_connections += 1
                if conn.is_healthy:
                    healthy_connections += 1
                temp_connections.append(conn)
            except Empty:
                break

        # Return connections to pool
        for conn in temp_connections:
            try:
                self._pool.put(conn, block=False)
            except Exception:
                pass

        # Check overflow connections
        with self._lock:
            for conn in self._overflow_connections:
                total_connections += 1
                if conn.is_healthy:
                    healthy_connections += 1

        health_ratio = healthy_connections / total_connections if total_connections > 0 else 0

        return {
            "healthy_connections": healthy_connections,
            "total_connections": total_connections,
            "health_ratio": health_ratio,
            "status": (
                "healthy"
                if health_ratio >= 0.8
                else "degraded" if health_ratio >= 0.5 else "unhealthy"
            ),
        }
