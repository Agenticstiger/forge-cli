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

import re
import time
from typing import Any, Dict

from ..._sql_safety import quote_string_literal, validate_ident
from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import quote_identifier


def _validate_account_locator(account: str) -> str:
    """Validate a Snowflake account locator for the ``ADD ACCOUNTS =`` clause.

    The external account in ``ALTER SHARE ... ADD ACCOUNTS = <account>`` is
    interpolated raw — an unvalidated value is an injection point (BUG-SQL-TYPE
    sibling). A locator is ``orgname.accountname`` or a legacy
    hyphen-separated form, so it cannot route through :func:`validate_ident`
    verbatim. Instead each ``.``/``-`` separated segment is allowlist-validated
    via :func:`validate_ident`; the original string is returned unchanged when
    every segment passes. Raises :class:`ValueError` otherwise.
    """
    if not isinstance(account, str) or not account.strip():
        raise ValueError(f"Invalid Snowflake account locator: {account!r}")
    for segment in re.split(r"[.\-]", account.strip()):
        validate_ident(segment)
    return account


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
                create_sql += f" COMMENT = {quote_string_literal(str(comment))}"

            conn.execute(create_sql)

            # Grant share to external accounts. ``external_account`` is
            # interpolated raw into the ALTER SHARE DDL — route it through the
            # account-locator allowlist so a contract cannot smuggle DDL
            # through the ADD ACCOUNTS clause (BUG-SQL-TYPE sibling).
            for external_account in accounts:
                safe_account = _validate_account_locator(external_account)
                grant_sql = (
                    f"ALTER SHARE {quote_identifier(share_name)} ADD ACCOUNTS = {safe_account}"
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
