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

"""Schedule engine base class and supporting types.

Every schedule engine (airflow, dagster, prefect, ...) subclasses
:class:`ScheduleEngine` and registers itself with the scheduler
registry.  The framework separates **what** schedule to generate
(engine) from **where** to run it (provider).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from fluid_build.engines.base import Severity, ValidationIssue

# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------


@dataclass
class ScheduleIntent:
    """AI-generated scheduling plan passed to :meth:`ScheduleEngine.generate`.

    Produced by the copilot LLM during ``fluid forge``.  Engines use this
    to generate full scheduling logic rather than skeleton stubs.
    When ``None`` is passed, engines fall back to skeleton generation.
    """

    schedule: str = "0 2 * * *"
    """Cron expression or Airflow keyword (@daily, @hourly, etc.)."""

    timezone: str = "UTC"
    """IANA timezone for schedule evaluation."""

    tasks: List[Dict[str, Any]] = field(default_factory=list)
    """Orchestration tasks from the contract."""

    provider: Optional[str] = None
    """Cloud provider (gcp, aws, snowflake) — selects provider-specific operators."""

    provider_config: Dict[str, Any] = field(default_factory=dict)
    """Provider-specific config (project, region, account_id, etc.)."""


# ---------------------------------------------------------------------------
# ScheduleGenerationResult type alias
# ---------------------------------------------------------------------------

#: Return type of :meth:`ScheduleEngine.generate`.
#: Maps relative file paths to their string content.
#: Example: ``{"dags/my_dag.py": "..."}``
ScheduleGenerationResult = Dict[str, str]


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class ScheduleEngine(ABC):
    """Abstract base for all schedule engines.

    Subclasses **must** implement :meth:`generate` and :meth:`validate`.

    The engine always receives the *bundled* (``$ref``-resolved) contract
    dict — fragment resolution is the caller's responsibility.
    """

    #: Engine identifier (e.g. ``"airflow"``, ``"dagster"``).
    name: str = ""

    #: Platforms this engine works with.
    #: ``None`` or empty means platform-agnostic (works everywhere).
    supported_platforms: Optional[Sequence[str]] = None

    @abstractmethod
    def generate(
        self,
        contract: Dict[str, Any],
        *,
        provider: Optional[str] = None,
        provider_config: Optional[Dict[str, Any]] = None,
        schedule_intent: Optional[ScheduleIntent] = None,
    ) -> ScheduleGenerationResult:
        """Generate schedule artifacts from contract + optional AI context.

        Parameters
        ----------
        contract:
            The full FLUID contract (bundled, ``$ref``-resolved).
        provider:
            Cloud provider name (gcp, aws, snowflake).  Used to select
            provider-specific operators in the generated code.
        provider_config:
            Provider-specific configuration (project ID, region, etc.).
        schedule_intent:
            AI-generated scheduling plan.  When ``None``, the engine
            should produce skeleton files with TODO placeholders.

        Returns
        -------
        Dict mapping relative file paths to their string content.
        """

    @abstractmethod
    def validate(
        self,
        contract: Dict[str, Any],
    ) -> List[ValidationIssue]:
        """Validate that *contract* is valid for this schedule engine.

        Returns an empty list when valid.
        """
