# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
