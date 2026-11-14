# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The structured answer a coding agent returns in ``fluid forge``'s one-call path.

``claude -p --json-schema`` and ``codex exec --output-schema`` hold the agent's final
message to this shape: the contract as YAML text, and nothing else. It replaced a
twelve-field response envelope. That envelope required an ``owner``, so every answer had
to name one, invented or not.
"""

from __future__ import annotations

from typing import Any, Dict

CONTRACT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "contract_yaml": {
            "type": "string",
            "description": "The whole contract.fluid.yaml, as YAML text.",
        }
    },
    "required": ["contract_yaml"],
    "additionalProperties": False,
}
