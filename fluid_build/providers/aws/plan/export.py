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

# fluid_build/providers/aws/plan/export.py
"""Export FLUID contracts to external formats (OPDS, DOT)."""

from collections.abc import Mapping
from typing import Any, Dict, List


def export_opds(src: Mapping[str, Any] | List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Export to Open Data Product Standard format."""
    return {
        "status": "ok",
        "message": "OPDS export not yet implemented for AWS provider",
    }


def export_dot_graph(src: Mapping[str, Any] | List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Export to GraphViz DOT format."""
    return {
        "status": "ok",
        "message": "DOT export not yet implemented for AWS provider",
    }
