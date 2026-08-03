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

"""Airbyte acquisition runner.

Engine name: ``airbyte``. Lane: 350+ Airbyte connectors. Capabilities:
``full_refresh``, ``incremental_append``, ``incremental_dedup``, ``cdc``,
``schema_discovery``.

Two execution modes:
- **REST mode**: drives an Airbyte OSS / Cloud server via REST. Set
  ``properties.airbyte.deployment.mode = bring-your-own`` and
  ``deployment.server_url``.
- **Embedded mode (PyAirbyte)**: runs connectors in-process. Set
  ``deployment.mode = embedded``. Optional dependency ``airbyte`` package.
"""

from __future__ import annotations

# Side-effect imports — both register their factories with the runner's
# dispatch tables at package-load time. Don't remove.
from . import (
    destinations,  # noqa: F401  per-platform PyAirbyte cache factories
    sources,  # noqa: F401  per-source-kind config adapters
)
from .runner import AirbyteRunner, execute_airbyte_build

__all__ = ["AirbyteRunner", "execute_airbyte_build"]
