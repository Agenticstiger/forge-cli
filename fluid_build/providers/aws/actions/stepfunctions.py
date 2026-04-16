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

# fluid_build/providers/aws/actions/stepfunctions.py
"""AWS Step Functions actions."""

from typing import Any, Dict


def ensure_state_machine(action: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure Step Functions state machine exists."""
    return {"status": "ok", "message": "Step Functions stub", "changed": False}


def start_execution(action: Dict[str, Any]) -> Dict[str, Any]:
    """Start Step Functions execution."""
    return {"status": "ok", "message": "Step Functions execution stub", "changed": False}
