# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# fluid_build/providers/aws/util/config.py
"""
AWS configuration resolution utilities.
"""

import importlib.util
import os
from typing import Optional, Tuple

from fluid_build.providers._plan_validation import PlanValidationError


class AwsAccountUnresolvedError(PlanValidationError):
    """No AWS account id could be resolved, so no resource name can be planned.

    A :class:`PlanValidationError` so ``native_actions`` re-raises it (matched by
    type) rather than treating the planner as one that could not run: the
    account feeds names (``{account}-fluid-data``) and every role ARN, and a
    plan built on ``None`` is refused by the validator with a message about a
    bucket name, not the cause.
    """


def _boto3_missing() -> bool:
    """True when boto3 (the ``[aws]`` extra) is not installed."""
    return importlib.util.find_spec("boto3") is None


def account_unresolved_message() -> str:
    """Why no account resolved, and what fixes it.

    ``resolve_account_and_region`` swallows the ``ImportError`` for a missing
    boto3 along with every STS failure, so the cause is re-derived here:
    blaming credentials for a package that is not installed sends the user to
    the wrong place.
    """
    if _boto3_missing():
        return (
            "The AWS account id could not be resolved: boto3 is not installed, so "
            "sts:GetCallerIdentity cannot be called. Install it with "
            "pip install 'data-product-forge[aws]', or set AWS_ACCOUNT_ID."
        )
    return (
        "The AWS account id could not be resolved. Set AWS_ACCOUNT_ID, or "
        "configure AWS credentials (environment, profile or role) so that "
        "sts:GetCallerIdentity succeeds."
    )


def resolve_account_and_region(
    account_id: Optional[str] = None, region: Optional[str] = None
) -> Tuple[str, str]:
    """
    Resolve AWS account ID and region from multiple sources.

    Priority:
    1. Explicit parameters
    2. Environment variables (AWS_ACCOUNT_ID, AWS_DEFAULT_REGION, AWS_REGION)
    3. AWS CLI configuration
    4. Defaults (None for account, us-east-1 for region)

    Args:
        account_id: Explicit account ID
        region: Explicit region

    Returns:
        Tuple of (account_id, region)
    """
    # Resolve account ID
    resolved_account = account_id or os.getenv("AWS_ACCOUNT_ID")

    if not resolved_account:
        # Try to get from STS
        try:
            import boto3

            sts = boto3.client("sts")
            response = sts.get_caller_identity()
            resolved_account = response.get("Account")
        except Exception:
            # If STS fails, leave as None (will be resolved at runtime)
            resolved_account = None

    # Resolve region - prioritize environment variables
    resolved_region = region
    if not resolved_region:
        resolved_region = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION")
    if not resolved_region:
        resolved_region = "us-east-1"

    return resolved_account, resolved_region


def get_boto_session(region: Optional[str] = None):
    """
    Get a boto3 session with proper configuration.

    Args:
        region: AWS region

    Returns:
        Configured boto3 Session
    """
    try:
        import boto3
    except ImportError:
        raise RuntimeError("boto3 not installed. Install with: pip install boto3")

    if region:
        return boto3.Session(region_name=region)
    else:
        return boto3.Session()
