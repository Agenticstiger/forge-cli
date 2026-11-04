# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
