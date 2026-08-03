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

"""``fluid retention sweep <product-id>`` — periodic cleanup with summary."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from fluid_build.build_runners._retention import RetentionConfig, sweep_all


@dataclass
class RetentionSummary:
    deleted_paths: List[str] = field(default_factory=list)
    bytes_freed: int = 0
    by_category: Dict[str, int] = field(default_factory=dict)


def sweep_with_summary(
    state_root: Path,
    *,
    config: Optional[RetentionConfig] = None,
) -> RetentionSummary:
    """Run the retention sweeper and return a structured summary."""
    config = config or RetentionConfig.from_dict(None)
    results = sweep_all(state_root, config)
    summary = RetentionSummary()
    for category, result in results.items():
        for path in result.deleted_paths:
            summary.deleted_paths.append(str(path))
        summary.bytes_freed += result.total_bytes
        summary.by_category[category] = len(result.deleted_paths)
    return summary
