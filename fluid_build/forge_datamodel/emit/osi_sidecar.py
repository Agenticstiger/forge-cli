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

"""Standalone OSI sidecar emission."""

from __future__ import annotations

import yaml

from fluid_build.copilot.schemas.stage_outputs import LogicalDraft


def emit_osi_yaml(logical: LogicalDraft) -> str:
    return yaml.safe_dump(logical.osi.model_dump(mode="json", by_alias=True), sort_keys=False)
