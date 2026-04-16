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

"""Transformation engine registry.

Engines self-register via the :func:`register_engine` decorator or by
calling it explicitly.  Built-in engines (dbt, sql) are registered at
import time when :mod:`fluid_build.engines` is first imported.

Adding a new engine is one class + one decorator::

    from fluid_build.engines.registry import register_engine
    from fluid_build.engines.base import TransformationEngine

    @register_engine
    class MyEngine(TransformationEngine):
        name = "my_engine"
        ...
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Type

from .base import TransformationEngine

_ENGINES: Dict[str, Type[TransformationEngine]] = {}
_log = logging.getLogger(__name__)


def register_engine(cls: Type[TransformationEngine]) -> Type[TransformationEngine]:
    """Register a :class:`TransformationEngine` subclass.

    Can be used as a decorator::

        @register_engine
        class DbtEngine(TransformationEngine):
            name = "dbt"
            ...

    Or called explicitly::

        register_engine(DbtEngine)
    """
    if not cls.name:
        raise ValueError(f"Engine class {cls.__name__} must define a non-empty 'name' attribute")
    if cls.name in _ENGINES:
        _log.debug("Overriding engine registration for %r with %s", cls.name, cls.__name__)
    _ENGINES[cls.name] = cls
    return cls


def get_engine(name: str) -> Optional[TransformationEngine]:
    """Look up a registered engine by name and return an instance.

    Returns ``None`` if no engine is registered for *name*.
    """
    cls = _ENGINES.get(name)
    if cls is None:
        return None
    return cls()


def list_engines() -> List[str]:
    """Return sorted list of registered engine names."""
    return sorted(_ENGINES.keys())


def has_engine(name: str) -> bool:
    """Return ``True`` if a generator is registered for *name*."""
    return name in _ENGINES


def list_engines_for_platform(platform: str) -> List[str]:
    """Return engines available for a given platform.

    Includes platform-agnostic engines (``supported_platforms`` is ``None``)
    and engines that explicitly list *platform*.
    """
    result = []
    for name, cls in sorted(_ENGINES.items()):
        instance = cls()
        platforms = instance.supported_platforms
        if platforms is None or not platforms:
            # Platform-agnostic
            result.append(name)
        elif platform.lower() in [p.lower() for p in platforms]:
            result.append(name)
    return result


def _reset_registry() -> None:
    """Clear all registrations.  **Testing only.**"""
    _ENGINES.clear()
