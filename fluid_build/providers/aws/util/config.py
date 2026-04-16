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

# fluid_build/providers/aws/util/config.py
"""
AWS configuration resolution utilities.
"""

import os
from typing import Optional, Tuple


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
