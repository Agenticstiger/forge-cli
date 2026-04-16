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
# Legacy provider.py has been deprecated in favor of provider_enhanced.py
from .provider_enhanced import SnowflakeProviderEnhanced as SnowflakeProvider  # noqa: F401


def auto_register(register: Callable[[str, object], None]) -> None:
    """Register Snowflake provider with discovery system."""
    try:
        register("snowflake", SnowflakeProvider)
    except Exception:
        pass


PROVIDERS: Dict[str, type] = {"snowflake": SnowflakeProvider}

__all__ = ["SnowflakeProvider", "auto_register", "PROVIDERS"]
