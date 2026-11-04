# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# fluid_build/providers/aws/util/auth.py
"""
AWS authentication and environment reporting utilities.
"""

from typing import Any, Dict


def get_auth_report(account_id: str, region: str) -> Dict[str, Any]:
    """
    Generate AWS authentication and environment report.

    Args:
        account_id: AWS account ID
        region: AWS region

    Returns:
        Authentication report dictionary
    """
    report = {
        "provider": "aws",
        "account_id": account_id,
        "region": region,
        "boto3_available": False,
        "caller_identity": None,
        "available_services": [],
    }

    try:
        import boto3

        report["boto3_available"] = True
        report["boto3_version"] = boto3.__version__

        # Get caller identity
        try:
            sts = boto3.client("sts", region_name=region)
            identity = sts.get_caller_identity()
            report["caller_identity"] = {
                "account": identity.get("Account"),
                "arn": identity.get("Arn"),
                "user_id": identity.get("UserId"),
            }
        except Exception as e:
            report["caller_identity_error"] = str(e)

        # List available services
        try:
            session = boto3.Session(region_name=region)
            report["available_services"] = session.get_available_services()
        except Exception as e:
            report["services_error"] = str(e)

    except ImportError:
        report["status"] = "error"
        report["error"] = "boto3 not installed"
    except Exception as e:
        report["status"] = "error"
        report["error"] = str(e)

    return report
