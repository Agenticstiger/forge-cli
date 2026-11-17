# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The provider settings a DAG is rendered with, resolved the way the provider resolves them.

``fluid export`` takes them from the provider object. ``fluid generate schedule`` has no
provider object, so this reads the same sources: the contract's ``metadata`` first, then the
environment variables the provider reads. A value that cannot be resolved is left out, and the
renderer refuses a task that needs it; no placeholder is ever written into a DAG.
"""

import os
from typing import Any, Dict


def resolve_provider_config(provider: str, contract: Dict[str, Any]) -> Dict[str, Any]:
    """``provider_config`` for ``render_dag``: only values that resolved."""
    metadata = contract.get("metadata") or {}
    provider = (provider or "").lower()
    config: Dict[str, Any] = {}

    if provider == "gcp":
        from fluid_build.providers.gcp.util.config import resolve_project_and_region

        try:
            project, region = resolve_project_and_region(
                metadata.get("gcp_project"), metadata.get("gcp_region")
            )
            config = {"project": project, "region": region}
        except ValueError:
            # No project anywhere: leave it out. A task that needs one is refused.
            config = {"region": metadata.get("gcp_region") or "us-central1"}
    elif provider == "aws":
        config = {
            "region": metadata.get("aws_region")
            or os.environ.get("AWS_DEFAULT_REGION")
            or os.environ.get("AWS_REGION")
            or "us-east-1",
            "account_id": metadata.get("aws_account_id") or os.environ.get("AWS_ACCOUNT_ID") or "",
        }
    elif provider == "snowflake":
        config = {
            "connection_id": metadata.get("snowflake_connection_id") or "snowflake_default",
            "account": metadata.get("snowflake_account") or os.environ.get("SNOWFLAKE_ACCOUNT"),
            "database": metadata.get("snowflake_database") or os.environ.get("SNOWFLAKE_DATABASE"),
            "warehouse": metadata.get("snowflake_warehouse")
            or os.environ.get("SNOWFLAKE_WAREHOUSE"),
        }
    return {key: value for key, value in config.items() if value}
