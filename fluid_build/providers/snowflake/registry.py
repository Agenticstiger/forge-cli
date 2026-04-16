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

# fluid_build/providers/snowflake/registry.py
"""
Snowflake Provider Action Registry.

Central registry of all Snowflake provider actions for FLUID 0.7.1 orchestration support.
Enables programmatic discovery, validation, and documentation of provider actions.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class ParameterType(Enum):
    """Parameter type enumeration."""

    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass
class ActionParameter:
    """Parameter definition for a provider action."""

    name: str
    type: ParameterType
    required: bool
    description: str
    default: Optional[Any] = None
    example: Optional[Any] = None

    def validate(self, value: Any) -> tuple[bool, Optional[str]]:
        """
        Validate parameter value.

        Returns:
            (is_valid, error_message)
        """
        if value is None:
            if self.required:
                return False, f"Required parameter '{self.name}' is missing"
            return True, None

        # Type validation
        if self.type == ParameterType.STRING:
            if not isinstance(value, str):
                return False, f"Parameter '{self.name}' must be a string"
        elif self.type == ParameterType.INTEGER:
            if not isinstance(value, int):
                return False, f"Parameter '{self.name}' must be an integer"
        elif self.type == ParameterType.BOOLEAN:
            if not isinstance(value, bool):
                return False, f"Parameter '{self.name}' must be a boolean"
        elif self.type == ParameterType.ARRAY:
            if not isinstance(value, list):
                return False, f"Parameter '{self.name}' must be an array"
        elif self.type == ParameterType.OBJECT:
            if not isinstance(value, dict):
                return False, f"Parameter '{self.name}' must be an object"

        return True, None


@dataclass
class ActionDefinition:
    """Complete definition of a Snowflake provider action."""

    name: str
    description: str
    handler: Callable
    parameters: List[ActionParameter] = field(default_factory=list)
    produces: List[str] = field(default_factory=list)  # Resources produced
    requires: List[str] = field(default_factory=list)  # Resources required
    examples: List[Dict[str, Any]] = field(default_factory=list)
    phase: str = "expose"  # infrastructure, iam, build, expose, schedule

    @property
    def required_params(self) -> List[str]:
        """Get list of required parameter names."""
        return [p.name for p in self.parameters if p.required]

    def validate_parameters(self, params: Dict[str, Any]) -> tuple[bool, List[str]]:
        """
        Validate all parameters.

        Returns:
            (is_valid, list_of_errors)
        """
        errors = []

        # Validate each defined parameter
        for param in self.parameters:
            value = params.get(param.name)
            is_valid, error = param.validate(value)
            if not is_valid:
                errors.append(error)

        return len(errors) == 0, errors


class SnowflakeActionRegistry:
    """
    Central registry of all Snowflake provider actions.

    Provides:
    - Action discovery and lookup
    - Parameter validation
    - Documentation generation
    - Orchestration integration
    """

    _actions: Dict[str, ActionDefinition] = {}

    @classmethod
    def register(cls, action: ActionDefinition) -> None:
        """Register a provider action."""
        cls._actions[action.name] = action

    @classmethod
    def get(cls, name: str) -> Optional[ActionDefinition]:
        """Get action definition by name."""
        return cls._actions.get(name)

    @classmethod
    def list_all(cls) -> List[str]:
        """List all registered action names."""
        return sorted(cls._actions.keys())

    @classmethod
    def list_by_phase(cls, phase: str) -> List[str]:
        """List actions for a specific phase."""
        return sorted([name for name, action in cls._actions.items() if action.phase == phase])

    @classmethod
    def validate(cls, name: str, params: Dict[str, Any]) -> tuple[bool, List[str]]:
        """
        Validate action parameters.

        Returns:
            (is_valid, list_of_errors)
        """
        action = cls.get(name)
        if not action:
            return False, [f"Unknown action: {name}"]

        return action.validate_parameters(params)

    @classmethod
    def to_markdown_docs(cls) -> str:
        """Generate comprehensive markdown documentation."""
        docs = "# Snowflake Provider Actions Reference\n\n"
        docs += "Complete reference of all Snowflake provider actions for FLUID 0.7.1.\n\n"
        docs += "## Table of Contents\n\n"

        # Group by phase
        phases = ["infrastructure", "iam", "build", "expose", "schedule"]
        for phase in phases:
            actions = cls.list_by_phase(phase)
            if actions:
                docs += f"- [{phase.title()}](#{phase})\n"

        docs += "\n---\n\n"

        # Document each phase
        for phase in phases:
            actions = cls.list_by_phase(phase)
            if not actions:
                continue

            docs += f"## {phase.title()}\n\n"

            for action_name in actions:
                action = cls.get(action_name)
                docs += f"### `{action.name}`\n\n"
                docs += f"{action.description}\n\n"

                if action.parameters:
                    docs += "**Parameters:**\n\n"
                    docs += "| Name | Type | Required | Description |\n"
                    docs += "|------|------|----------|-------------|\n"

                    for param in action.parameters:
                        req = "✅" if param.required else "⚪"
                        default = (
                            f" (default: `{param.default}`)" if param.default is not None else ""
                        )
                        docs += f"| `{param.name}` | {param.type.value} | {req} | {param.description}{default} |\n"

                    docs += "\n"

                if action.produces:
                    docs += f"**Produces:** {', '.join(action.produces)}\n\n"

                if action.requires:
                    docs += f"**Requires:** {', '.join(action.requires)}\n\n"

                if action.examples:
                    docs += "**Example:**\n\n"
                    docs += "```yaml\n"
                    import yaml

                    docs += yaml.dump(action.examples[0], default_flow_style=False)
                    docs += "```\n\n"

                docs += "---\n\n"

        return docs


# Register all Snowflake actions
def _register_all_actions():
    """Register all Snowflake provider actions."""

    # Database actions
    SnowflakeActionRegistry.register(
        ActionDefinition(
            name="sf.database.ensure",
            description="Create or ensure Snowflake database exists",
            handler=None,  # Imported at runtime
            phase="infrastructure",
            parameters=[
                ActionParameter(
                    "account", ParameterType.STRING, True, "Snowflake account identifier"
                ),
                ActionParameter("database", ParameterType.STRING, True, "Database name"),
                ActionParameter(
                    "comment", ParameterType.STRING, False, "Database comment/description"
                ),
                ActionParameter(
                    "transient",
                    ParameterType.BOOLEAN,
                    False,
                    "Create transient database",
                    default=False,
                ),
            ],
            produces=["database"],
            examples=[
                {
                    "type": "provider_action",
                    "action": "sf.database.ensure",
                    "parameters": {
                        "account": "myorg-myaccount",
                        "database": "CRYPTO_DATA",
                        "comment": "Cryptocurrency analytics database",
                    },
                }
            ],
        )
    )

    # Schema actions
    SnowflakeActionRegistry.register(
        ActionDefinition(
            name="sf.schema.ensure",
            description="Create or ensure Snowflake schema exists",
            handler=None,
            phase="infrastructure",
            parameters=[
                ActionParameter(
                    "account", ParameterType.STRING, True, "Snowflake account identifier"
                ),
                ActionParameter("database", ParameterType.STRING, True, "Database name"),
                ActionParameter("schema", ParameterType.STRING, True, "Schema name"),
                ActionParameter(
                    "comment", ParameterType.STRING, False, "Schema comment/description"
                ),
                ActionParameter(
                    "transient",
                    ParameterType.BOOLEAN,
                    False,
                    "Create transient schema",
                    default=False,
                ),
            ],
            produces=["schema"],
            requires=["database"],
            examples=[
                {
                    "type": "provider_action",
                    "action": "sf.schema.ensure",
                    "parameters": {
                        "account": "myorg-myaccount",
                        "database": "CRYPTO_DATA",
                        "schema": "MARKET_DATA",
                    },
                }
            ],
        )
    )

    # Table actions
    SnowflakeActionRegistry.register(
        ActionDefinition(
            name="sf.table.ensure",
            description="Create or update Snowflake table with schema",
            handler=None,
            phase="expose",
            parameters=[
                ActionParameter(
                    "account", ParameterType.STRING, True, "Snowflake account identifier"
                ),
                ActionParameter("database", ParameterType.STRING, True, "Database name"),
                ActionParameter("schema", ParameterType.STRING, True, "Schema name"),
                ActionParameter("table", ParameterType.STRING, True, "Table name"),
                ActionParameter("columns", ParameterType.ARRAY, True, "Column definitions"),
                ActionParameter(
                    "cluster_by", ParameterType.ARRAY, False, "Clustering keys", default=[]
                ),
                ActionParameter(
                    "comment", ParameterType.STRING, False, "Table comment/description"
                ),
                ActionParameter("tags", ParameterType.OBJECT, False, "Snowflake tags", default={}),
            ],
            produces=["table"],
            requires=["database", "schema"],
            examples=[
                {
                    "type": "provider_action",
                    "action": "sf.table.ensure",
                    "parameters": {
                        "account": "myorg-myaccount",
                        "database": "CRYPTO_DATA",
                        "schema": "MARKET_DATA",
                        "table": "BITCOIN_PRICES",
                        "columns": [
                            {"name": "price_timestamp", "type": "TIMESTAMP_NTZ"},
                            {"name": "price_usd", "type": "DECIMAL(18,2)"},
                        ],
                        "cluster_by": ["price_timestamp"],
                    },
                }
            ],
        )
    )

    # View actions
    SnowflakeActionRegistry.register(
        ActionDefinition(
            name="sf.view.ensure",
            description="Create or replace Snowflake view",
            handler=None,
            phase="expose",
            parameters=[
                ActionParameter(
                    "account", ParameterType.STRING, True, "Snowflake account identifier"
                ),
                ActionParameter("database", ParameterType.STRING, True, "Database name"),
                ActionParameter("schema", ParameterType.STRING, True, "Schema name"),
                ActionParameter("name", ParameterType.STRING, True, "View name"),
                ActionParameter("query", ParameterType.STRING, True, "View SQL query"),
                ActionParameter(
                    "secure", ParameterType.BOOLEAN, False, "Create secure view", default=False
                ),
            ],
            produces=["view"],
            requires=["database", "schema"],
        )
    )

    # Stream actions
    SnowflakeActionRegistry.register(
        ActionDefinition(
            name="sf.stream.ensure",
            description="Create Snowflake stream for CDC",
            handler=None,
            phase="expose",
            parameters=[
                ActionParameter(
                    "account", ParameterType.STRING, True, "Snowflake account identifier"
                ),
                ActionParameter("database", ParameterType.STRING, True, "Database name"),
                ActionParameter("schema", ParameterType.STRING, True, "Schema name"),
                ActionParameter("name", ParameterType.STRING, True, "Stream name"),
                ActionParameter("source_table", ParameterType.STRING, True, "Source table for CDC"),
                ActionParameter(
                    "append_only", ParameterType.BOOLEAN, False, "Append-only stream", default=False
                ),
            ],
            produces=["stream"],
            requires=["database", "schema", "table"],
        )
    )

    # Task actions
    SnowflakeActionRegistry.register(
        ActionDefinition(
            name="sf.task.ensure",
            description="Create or update Snowflake task",
            handler=None,
            phase="schedule",
            parameters=[
                ActionParameter(
                    "account", ParameterType.STRING, True, "Snowflake account identifier"
                ),
                ActionParameter("database", ParameterType.STRING, True, "Database name"),
                ActionParameter("schema", ParameterType.STRING, True, "Schema name"),
                ActionParameter("name", ParameterType.STRING, True, "Task name"),
                ActionParameter("schedule", ParameterType.STRING, False, "Cron schedule"),
                ActionParameter("sql", ParameterType.STRING, True, "SQL to execute"),
                ActionParameter("warehouse", ParameterType.STRING, False, "Warehouse for task"),
                ActionParameter(
                    "after", ParameterType.ARRAY, False, "Task dependencies", default=[]
                ),
            ],
            produces=["task"],
            requires=["database", "schema"],
        )
    )

    # SQL execution
    SnowflakeActionRegistry.register(
        ActionDefinition(
            name="sf.sql.execute",
            description="Execute arbitrary SQL statement",
            handler=None,
            phase="build",
            parameters=[
                ActionParameter(
                    "account", ParameterType.STRING, True, "Snowflake account identifier"
                ),
                ActionParameter("database", ParameterType.STRING, True, "Database name"),
                ActionParameter("schema", ParameterType.STRING, True, "Schema name"),
                ActionParameter("sql", ParameterType.STRING, True, "SQL statement to execute"),
            ],
            produces=[],
            requires=["database", "schema"],
        )
    )


# Auto-register on module import
_register_all_actions()
