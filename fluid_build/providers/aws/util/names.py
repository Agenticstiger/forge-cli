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

# fluid_build/providers/aws/util/names.py
"""
AWS resource name normalization utilities.
"""

import hashlib
import re
from typing import Optional


def normalize_database_name(name: str) -> str:
    """
    Normalize Glue database name to AWS requirements.

    Glue database names must:
    - Be lowercase
    - Contain only alphanumeric characters and underscores
    - Start with a letter
    - Be 1-255 characters

    Args:
        name: Input database name

    Returns:
        Normalized database name
    """
    # Convert to lowercase
    normalized = name.lower()

    # Replace invalid characters with underscores
    normalized = re.sub(r"[^a-z0-9_]", "_", normalized)

    # Ensure starts with letter
    if normalized and not normalized[0].isalpha():
        normalized = f"db_{normalized}"

    # Truncate to 255 characters
    return normalized[:255]


def normalize_table_name(name: str) -> str:
    """
    Normalize Glue table name to AWS requirements.

    Glue table names must:
    - Be lowercase
    - Contain only alphanumeric characters and underscores
    - Be 1-255 characters

    Args:
        name: Input table name

    Returns:
        Normalized table name
    """
    # Convert to lowercase
    normalized = name.lower()

    # Replace invalid characters with underscores
    normalized = re.sub(r"[^a-z0-9_]", "_", normalized)

    # Remove consecutive underscores
    normalized = re.sub(r"_+", "_", normalized)

    # Remove leading/trailing underscores
    normalized = normalized.strip("_")

    # Truncate to 255 characters
    return normalized[:255]


def normalize_bucket_name(name: str, account_id: Optional[str] = None) -> str:
    """
    Normalize S3 bucket name to AWS requirements.

    S3 bucket names must:
    - Be 3-63 characters
    - Be lowercase
    - Contain only lowercase letters, numbers, hyphens, and periods
    - Start and end with letter or number
    - Not be formatted as IP address

    Args:
        name: Input bucket name
        account_id: Optional account ID to ensure uniqueness

    Returns:
        Normalized bucket name
    """
    # Convert to lowercase
    normalized = name.lower()

    # Replace invalid characters with hyphens
    normalized = re.sub(r"[^a-z0-9.-]", "-", normalized)

    # Remove consecutive hyphens
    normalized = re.sub(r"-+", "-", normalized)

    # Remove leading/trailing hyphens and periods
    normalized = normalized.strip(".-")

    # Ensure starts and ends with alphanumeric
    if normalized and not normalized[0].isalnum():
        normalized = f"bucket-{normalized}"
    if normalized and not normalized[-1].isalnum():
        normalized = f"{normalized}-data"

    # Add account ID prefix if provided for uniqueness
    if account_id and not normalized.startswith(account_id):
        normalized = f"{account_id}-{normalized}"

    # Truncate to 63 characters
    if len(normalized) > 63:
        normalized = normalized[:63].rstrip(".-")

    # Ensure minimum length
    if len(normalized) < 3:
        normalized = f"{normalized}-bucket"

    return normalized


def normalize_lambda_name(name: str) -> str:
    """
    Normalize Lambda function name to AWS requirements.

    Lambda function names must:
    - Be 1-64 characters
    - Contain only letters, numbers, hyphens, and underscores

    Args:
        name: Input function name

    Returns:
        Normalized function name
    """
    # Replace invalid characters with underscores
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)

    # Remove consecutive underscores
    normalized = re.sub(r"_+", "_", normalized)

    # Truncate to 64 characters
    return normalized[:64]


#: Per-service naming rules for names the planner derives from a contract id.
#: Each is ``(characters NOT allowed, maximum length)``; pinned from the AWS API
#: references so the planner's output is valid before ``validate_actions`` sees
#: it:
#:
#: * Lambda function name, ``CreateFunction`` ``FunctionName``: letters, digits,
#:   ``-`` and ``_``; 64 characters when only the name is given (the API's longer
#:   limit applies to a full ARN).
#:   https://docs.aws.amazon.com/lambda/latest/api/API_CreateFunction.html
#: * Lambda permission statement id, ``AddPermission`` ``StatementId``: letters,
#:   digits, ``-`` and ``_``; 1-100 characters.
#:   https://docs.aws.amazon.com/lambda/latest/api/API_AddPermission.html
#: * EventBridge Scheduler schedule name, ``CreateSchedule`` ``Name``: letters,
#:   digits, ``-``, ``_`` and ``.``; 1-64 characters.
#:   https://docs.aws.amazon.com/scheduler/latest/APIReference/API_CreateSchedule.html
#: * EventBridge rule name, ``PutRule`` ``Name``: letters, digits, ``-``, ``_``
#:   and ``.``; 1-64 characters.
#:   https://docs.aws.amazon.com/eventbridge/latest/APIReference/API_PutRule.html
#: * MWAA environment name, ``CreateEnvironment`` ``Name``: starts with a letter,
#:   then letters, digits, ``-`` and ``_``; 1-80 characters (every name built
#:   here starts with the literal ``mwaa-``).
#:   https://docs.aws.amazon.com/mwaa/latest/API/API_CreateEnvironment.html
#: * Step Functions state machine name, ``CreateStateMachine`` ``name``: 1-80
#:   characters, no whitespace, brackets, wildcards or ``" # % \ ^ | ~ ` $ & , ; : /``.
#:   https://docs.aws.amazon.com/step-functions/latest/apireference/API_CreateStateMachine.html
LAMBDA_FUNCTION_NAME = (re.compile(r"[^a-zA-Z0-9\-_]"), 64)
LAMBDA_STATEMENT_ID = (re.compile(r"[^a-zA-Z0-9\-_]"), 100)
SCHEDULER_SCHEDULE_NAME = (re.compile(r"[^a-zA-Z0-9\-_.]"), 64)
EVENTBRIDGE_RULE_NAME = (re.compile(r"[^a-zA-Z0-9\-_.]"), 64)
MWAA_ENVIRONMENT_NAME = (re.compile(r"[^a-zA-Z0-9\-_]"), 80)
STATE_MACHINE_NAME = (re.compile(r"[\s<>{}\[\]?*\"#%\\^|~`$&,;:/\x00-\x1f\x7f-\x9f]"), 80)

#: Hex digits of the digest appended to a name that had to be shortened.
_NAME_HASH_LEN = 8


def bounded_aws_name(raw: str, rule: "tuple[re.Pattern[str], int]") -> str:
    """``raw`` as a name ``rule`` accepts, deterministic and collision-safe.

    Characters outside the rule's allowed set become ``-``. A name that had to
    change at all (a replacement, or a cut to the service limit) ends in
    ``-<hash>``, the hash taken from the *whole raw* name: ``a.b`` and ``a-b``
    would otherwise both be ``fluid-workflow-a-b``, two products in one account
    fighting over one Lambda, and two long ids sharing a prefix would collide
    after the cut. A name the rule already accepts is returned as written, so
    nothing already deployed under it is renamed.
    """
    pattern, limit = rule
    name = pattern.sub("-", raw)
    if name == raw and len(name) <= limit:
        return name
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_NAME_HASH_LEN]
    keep = limit - _NAME_HASH_LEN - 1
    return f"{name[:keep]}-{digest}"
