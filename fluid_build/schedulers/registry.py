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

"""Schedule engine registry.

Engines self-register via the :func:`register_scheduler` decorator or by
calling it explicitly.  Built-in engines (airflow, dagster, prefect) are
registered at import time when :mod:`fluid_build.schedulers` is first
imported.

Adding a new scheduler is one class + one decorator::

    from fluid_build.schedulers.registry import register_scheduler
    from fluid_build.schedulers.base import ScheduleEngine

    @register_scheduler
    class MyScheduler(ScheduleEngine):
        name = "my_scheduler"
        ...
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Type

from .base import ScheduleEngine

_SCHEDULERS: Dict[str, Type[ScheduleEngine]] = {}
_log = logging.getLogger(__name__)


def register_scheduler(cls: Type[ScheduleEngine]) -> Type[ScheduleEngine]:
    """Register a :class:`ScheduleEngine` subclass.

    Can be used as a decorator::

        @register_scheduler
        class AirflowScheduler(ScheduleEngine):
            name = "airflow"
            ...

    Or called explicitly::

        register_scheduler(AirflowScheduler)
    """
    if not cls.name:
        raise ValueError(f"Scheduler class {cls.__name__} must define a non-empty 'name' attribute")
    if cls.name in _SCHEDULERS:
        _log.debug("Overriding scheduler registration for %r with %s", cls.name, cls.__name__)
    _SCHEDULERS[cls.name] = cls
    return cls


def get_scheduler(name: str) -> Optional[ScheduleEngine]:
    """Look up a registered scheduler by name and return an instance.

    Returns ``None`` if no scheduler is registered for *name*.
    """
    cls = _SCHEDULERS.get(name)
    if cls is None:
        return None
    return cls()


def list_schedulers() -> List[str]:
    """Return sorted list of registered scheduler names."""
    return sorted(_SCHEDULERS.keys())


def has_scheduler(name: str) -> bool:
    """Return ``True`` if a scheduler is registered for *name*."""
    return name in _SCHEDULERS


def list_schedulers_for_platform(platform: str) -> List[str]:
    """Return schedulers available for a given platform.

    Includes platform-agnostic schedulers (``supported_platforms`` is ``None``)
    and schedulers that explicitly list *platform*.
    """
    result = []
    for name, cls in sorted(_SCHEDULERS.items()):
        instance = cls()
        platforms = instance.supported_platforms
        if platforms is None or not platforms:
            result.append(name)
        elif platform.lower() in [p.lower() for p in platforms]:
            result.append(name)
    return result


def _reset_registry() -> None:
    """Clear all registrations.  **Testing only.**"""
    _SCHEDULERS.clear()
