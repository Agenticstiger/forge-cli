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

"""Pluggable Schedule Engine framework.

Public API::

    from fluid_build.schedulers import get_scheduler, list_schedulers, has_scheduler

    scheduler = get_scheduler("airflow")
    if scheduler:
        files = scheduler.generate(contract, provider="gcp")
"""

# Auto-discover and register all scheduler subpackages.
# Each scheduler subpackage uses @register_scheduler on its class.
# New schedulers are picked up automatically — no need to edit this file.
import importlib
import pkgutil

from .base import (
    ScheduleEngine,
    ScheduleGenerationResult,
    ScheduleIntent,
)
from .registry import (
    get_scheduler,
    has_scheduler,
    list_schedulers,
    list_schedulers_for_platform,
    register_scheduler,
)

for _finder, _name, _ispkg in pkgutil.iter_modules(__path__):
    if _ispkg:
        try:
            importlib.import_module(f"{__name__}.{_name}")
        except ImportError:
            pass  # Scheduler has unmet optional dependencies — skip silently

__all__ = [
    "ScheduleEngine",
    "ScheduleGenerationResult",
    "ScheduleIntent",
    "get_scheduler",
    "has_scheduler",
    "list_schedulers",
    "list_schedulers_for_platform",
    "register_scheduler",
]
