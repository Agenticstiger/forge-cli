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

"""Cost tracking + budget enforcement types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol


@dataclass(frozen=True)
class BudgetCap:
    """Monthly budget caps. Any field None = uncapped."""

    rows: Optional[int] = None
    bytes: Optional[int] = None  # already-parsed bytes integer
    compute_minutes: Optional[int] = None
    on_exceed: str = "warn"  # "warn" | "abort"


@dataclass(frozen=True)
class ChargebackTag:
    """Chargeback labels propagated to OTel metrics + OL events."""

    team: Optional[str] = None
    project: Optional[str] = None
    cost_center: Optional[str] = None

    def as_labels(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if self.team:
            out["team"] = self.team
        if self.project:
            out["project"] = self.project
        if self.cost_center:
            out["cost_center"] = self.cost_center
        return out


class CostTracker(Protocol):
    """Tracks cost during a run; checks budgets before bytes are read."""

    def check_budget(self, cap: BudgetCap, current_usage: Dict[str, int]) -> bool:
        """Return True if within budget; False if over.

        Caller decides what to do based on cap.on_exceed.
        """
        ...

    def record_records(self, n: int) -> None: ...

    def record_bytes(self, n: int, direction: str = "read") -> None: ...

    def record_compute_seconds(self, seconds: float) -> None: ...

    def usage(self) -> Dict[str, int]: ...
