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

import logging
from typing import Optional

from .contract import get_primary_build

LOGGER = logging.getLogger("fluid.util.cron")


def get_cron(contract: dict) -> Optional[str]:
    """Extract cron schedule from contract (supports both 0.4.0 and 0.5.7)."""
    try:
        # Use get_primary_build for version compatibility
        build = get_primary_build(contract)
        if build:
            return build.get("execution", {}).get("trigger", {}).get("cron")
    except Exception as e:
        LOGGER.debug(f"Failed to extract cron schedule from contract: {e}")
    return None
