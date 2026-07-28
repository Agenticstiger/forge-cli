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

"""dlt acquisition runner — Python-native, code-as-config ingestion.

Engine name: ``dlt``. Lane: long-tail custom APIs and verified ``dlt.sources.*``
packages. Capabilities: ``full_refresh``, ``incremental_append``,
``incremental_merge``, ``schema_evolution``.
"""

from __future__ import annotations

# Side-effect import: registers the dlt engine introspector with the unified
# registry in _credentials.py. The introspector walks dlt's OWN destination
# spec to discover credential field names — no per-destination factories.
# Runner dispatches via make_destination("dlt", binding.platform, ...).
# Don't remove.
from . import destinations  # noqa: F401  (registration side-effect)
from .runner import DltRunner, execute_dlt_build

__all__ = ["DltRunner", "execute_dlt_build"]
