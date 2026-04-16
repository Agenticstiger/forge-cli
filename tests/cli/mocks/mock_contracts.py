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

"""Sample test contracts for CLI testing"""

# FLUID 0.7.1 Contract
CONTRACT_071_MINIMAL = {
    "id": "test.minimal.v1",
    "fluidVersion": "0.7.1",
    "kind": "DataContract",
    "metadata": {"name": "Minimal Test Contract", "version": "1.0.0"},
    "schema": [{"name": "id", "type": "integer"}, {"name": "value", "type": "string"}],
    "exposes": [
        {
            "exposeId": "test_table",
            "binding": {"platform": "local", "database": "test_db", "table": "test_table"},
            "contract": {
                "schema": [{"name": "id", "type": "integer"}, {"name": "value", "type": "string"}]
            },
        }
    ],
}

# FLUID 0.5.7 Contract (backward compatibility)
CONTRACT_057_MINIMAL = {
    "id": "test.minimal.v1",
    "fluidVersion": "0.5.7",
    "kind": "DataContract",
    "name": "Minimal Test Contract",
    "version": "1.0.0",
    "schema": {"fields": [{"name": "id", "type": "integer"}, {"name": "value", "type": "string"}]},
    "exposes": [
        {
            "id": "test_table",
            "provider": "local",
            "location": {"database": "test_db", "table": "test_table"},
        }
    ],
}

# Invalid Contract (missing required fields)
CONTRACT_INVALID = {
    "id": "test.invalid",
    # Missing fluidVersion, kind, schema, etc.
}

# Contract with multiple exposes
CONTRACT_MULTI_EXPOSE = {
    "id": "test.multi.v1",
    "fluidVersion": "0.7.1",
    "kind": "DataContract",
    "metadata": {"name": "Multi-Expose Test Contract"},
    "schema": [
        {"name": "id", "type": "integer"},
        {"name": "name", "type": "string"},
        {"name": "status", "type": "string"},
    ],
    "exposes": [
        {"exposeId": "table_1", "binding": {"platform": "local", "table": "table_1"}},
        {"exposeId": "table_2", "binding": {"platform": "local", "table": "table_2"}},
        {"exposeId": "view_1", "binding": {"platform": "local", "table": "view_1"}},
    ],
}
