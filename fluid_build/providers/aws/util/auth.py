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
