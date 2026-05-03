# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Meltano (Singer protocol) acquisition runner.

Engine name: ``meltano``. Lane: 600+ Singer taps. Capabilities:
``full_refresh``, ``incremental_append``, ``incremental_dedup``,
``schema_discovery``.
"""

from __future__ import annotations

from .runner import MeltanoRunner, execute_meltano_build

__all__ = ["MeltanoRunner", "execute_meltano_build"]
