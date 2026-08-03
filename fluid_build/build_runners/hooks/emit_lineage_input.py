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

"""Emit-lineage-input hook.

A no-op pass-through that signals the runner to attach an OL ``inputs``
facet for the current batch — used when the dataset only becomes
identifiable after the first batch is read (e.g., dlt verified sources
that lazy-resolve URIs).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from fluid_build.api.hooks import HookResult


@dataclass
class EmitLineageInputHook:
    name: str = "emit_lineage_input"

    def apply(self, records: List[Dict[str, Any]], ctx: Dict[str, Any]) -> HookResult:
        return HookResult(records=records, metadata={"lineage_input_marker": True})
