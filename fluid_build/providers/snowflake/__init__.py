# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# fluid_build/providers/snowflake/__init__.py
"""
Snowflake provider package.

Production-grade Snowflake provider with comprehensive features:
  - Database, schema, and table management
  - Views and materialized views
  - Stored procedures and UDFs
  - Streams and tasks
  - RBAC and data governance
  - Data sharing and secure views

Discovery compatibility:
  - auto_register(register)
  - PROVIDERS mapping
"""

from typing import Callable, Dict

# Import enhanced provider as production implementation
# The enhanced provider is now the canonical implementation in provider.py.
from .provider import SnowflakeProviderEnhanced as SnowflakeProvider  # noqa: F401


def auto_register(register: Callable[[str, object], None]) -> None:
    """Register Snowflake provider with discovery system."""
    try:
        register("snowflake", SnowflakeProvider)
    except Exception:
        pass


PROVIDERS: Dict[str, type] = {"snowflake": SnowflakeProvider}

__all__ = ["SnowflakeProvider", "auto_register", "PROVIDERS"]
