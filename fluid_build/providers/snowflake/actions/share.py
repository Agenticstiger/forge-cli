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

# fluid_build/providers/snowflake/actions/share.py
"""Snowflake data sharing operations."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import quote_identifier


def ensure_share(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """
    Create or update Snowflake data share.

    Data shares enable secure data sharing with external accounts.
    """
    start_time = time.time()

    share_name = action["name"]
    account = action["account"]
    comment = action.get("comment")
    accounts = action.get("accounts", [])  # External accounts to share with

    provider.debug_kv(event="ensure_share_started", share=share_name)

    try:
        params = get_connection_params(
            account=account, warehouse=provider.warehouse, **provider._kwargs
        )

        with SnowflakeConnection(**params) as conn:
            # Create share (idempotent)
            create_sql = f"CREATE SHARE IF NOT EXISTS {quote_identifier(share_name)}"
            if comment:
                escaped_comment = comment.replace("'", "''")
                create_sql += f" COMMENT = '{escaped_comment}'"

            conn.execute(create_sql)

            # Grant share to external accounts
            for external_account in accounts:
                grant_sql = (
                    f"ALTER SHARE {quote_identifier(share_name)} ADD ACCOUNTS = {external_account}"
                )
                conn.execute(grant_sql)

            provider.info_kv(event="share_created", share=share_name, accounts=len(accounts))

            return {
                "status": "changed",
                "op": action["op"],
                "share": share_name,
                "accounts": accounts,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(event="ensure_share_failed", share=share_name, error=str(e))
        raise
