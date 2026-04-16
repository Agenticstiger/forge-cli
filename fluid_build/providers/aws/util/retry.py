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

# fluid_build/providers/aws/util/retry.py
"""
Retry logic for AWS operations with exponential backoff.
"""

import logging
import time
from typing import Any, Callable, Optional


def with_retry(
    func: Callable[[], Any],
    logger: Optional[logging.Logger] = None,
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
) -> Any:
    """
    Execute function with exponential backoff retry logic.

    Args:
        func: Function to execute
        logger: Optional logger for retry messages
        max_attempts: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        backoff_factor: Exponential backoff multiplier

    Returns:
        Function result

    Raises:
        Exception from last attempt if all retries fail
    """
    log = logger or logging.getLogger(__name__)

    last_exception = None
    delay = initial_delay

    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as e:
            last_exception = e

            # Don't retry on certain errors
            if _is_permanent_error(e):
                log.warning(f"Permanent error encountered, not retrying: {e}")
                raise

            if attempt < max_attempts:
                log.info(f"Attempt {attempt}/{max_attempts} failed: {e}. Retrying in {delay}s...")
                time.sleep(delay)
                delay = min(delay * backoff_factor, max_delay)
            else:
                log.error(f"All {max_attempts} attempts failed. Last error: {e}")

    # All retries exhausted
    raise last_exception


def _is_permanent_error(exception: Exception) -> bool:
    """
    Determine if an exception represents a permanent error that shouldn't be retried.

    Args:
        exception: Exception to check

    Returns:
        True if error is permanent (don't retry)
    """
    # Check for boto3 client errors
    error_code = getattr(exception, "response", {}).get("Error", {}).get("Code")

    permanent_codes = {
        "ValidationException",
        "InvalidParameterException",
        "InvalidParameterValue",
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedException",
        "InvalidClientTokenId",
        "SignatureDoesNotMatch",
        "ResourceNotFoundException",
        "NoSuchBucket",
        "NoSuchKey",
    }

    return error_code in permanent_codes
