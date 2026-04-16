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

# fluid_build/providers/aws/util/dependencies.py
"""
Resource dependency tracking for AWS provider.

Ensures actions are executed in the correct order based on dependencies:
- Databases must exist before tables
- Buckets must exist before objects
- Roles must exist before policies
"""

from collections import defaultdict, deque
from typing import Any, Dict, List, Set, Tuple


class DependencyGraph:
    """Tracks and resolves resource dependencies."""

    def __init__(self):
        """Initialize empty dependency graph."""
        self.nodes: Set[str] = set()
        self.edges: Dict[str, Set[str]] = defaultdict(set)  # node -> dependencies
        self.action_map: Dict[str, Dict[str, Any]] = {}

    def add_action(self, action: Dict[str, Any]) -> str:
        """
        Add action to graph and return its ID.

        Args:
            action: Action to add

        Returns:
            Action ID
        """
        action_id = action.get("id", self._generate_id(action))
        self.nodes.add(action_id)
        self.action_map[action_id] = action
        return action_id

    def add_dependency(self, action_id: str, depends_on: str) -> None:
        """
        Declare that action_id depends on another action.

        Args:
            action_id: Dependent action ID
            depends_on: Action that must execute first
        """
        self.edges[action_id].add(depends_on)

    def _generate_id(self, action: Dict[str, Any]) -> str:
        """Generate unique ID for action."""
        op = action.get("op", "unknown")

        if op == "s3.ensure_bucket":
            return f"s3_bucket_{action.get('bucket')}"
        elif op == "glue.ensure_database":
            return f"glue_db_{action.get('database')}"
        elif op in ["glue.ensure_table", "glue.ensure_iceberg_table"]:
            return f"glue_table_{action.get('database')}_{action.get('table')}"
        elif op == "athena.ensure_table":
            return f"athena_table_{action.get('database')}_{action.get('table')}"
        elif op == "lambda.ensure_function":
            return f"lambda_{action.get('function_name')}"
        elif op == "iam.ensure_role":
            return f"iam_role_{action.get('role_name')}"
        else:
            return f"{op}_{hash(str(action))}"

    def topological_sort(self) -> List[str]:
        """
        Sort actions by dependencies using Kahn's algorithm.

        Returns:
            List of action IDs in execution order

        Raises:
            ValueError: If circular dependency detected
        """
        # Calculate in-degree (number of dependencies) for each node
        in_degree = {node: 0 for node in self.nodes}
        for node in self.nodes:
            for dep in self.edges[node]:
                in_degree[node] += 1

        # Start with nodes that have no dependencies
        queue = deque([node for node in self.nodes if in_degree[node] == 0])
        result = []

        while queue:
            node = queue.popleft()
            result.append(node)

            # For each node that depends on this one
            for other_node in self.nodes:
                if node in self.edges[other_node]:
                    in_degree[other_node] -= 1
                    if in_degree[other_node] == 0:
                        queue.append(other_node)

        # Check for cycles
        if len(result) != len(self.nodes):
            remaining = set(self.nodes) - set(result)
            raise ValueError(
                f"Circular dependency detected among actions: {remaining}. "
                "Cannot determine execution order."
            )

        return result

    def get_ordered_actions(self) -> List[Dict[str, Any]]:
        """
        Get actions in dependency order.

        Returns:
            List of actions ready for execution
        """
        ordered_ids = self.topological_sort()
        return [self.action_map[action_id] for action_id in ordered_ids]


def analyze_dependencies(actions: List[Dict[str, Any]]) -> DependencyGraph:
    """
    Analyze actions and build dependency graph.

    Automatically detects dependencies based on resource references:
    - Tables depend on databases
    - Objects depend on buckets
    - Policies depend on roles

    Args:
        actions: List of actions to analyze

    Returns:
        Dependency graph
    """
    graph = DependencyGraph()

    # First pass: Add all actions
    action_ids = {}
    for action in actions:
        action_id = graph.add_action(action)
        action_ids[id(action)] = action_id

    # Track created resources
    databases = {}  # database_name -> action_id
    buckets = {}  # bucket_name -> action_id
    roles = {}  # role_name -> action_id

    # Second pass: Detect dependencies
    for action in actions:
        action_id = action_ids[id(action)]
        op = action.get("op", "")

        # Database creation
        if op == "glue.ensure_database":
            database = action.get("database")
            databases[database] = action_id

        # Bucket creation
        elif op == "s3.ensure_bucket":
            bucket = action.get("bucket")
            buckets[bucket] = action_id

        # Role creation
        elif op == "iam.ensure_role":
            role = action.get("role_name")
            roles[role] = action_id

        # Table creation - depends on database
        elif op in ["glue.ensure_table", "glue.ensure_iceberg_table", "athena.ensure_table"]:
            database = action.get("database")
            if database in databases:
                graph.add_dependency(action_id, databases[database])

            # Also depends on bucket (for location)
            location = action.get("location", "")
            if location.startswith("s3://"):
                bucket_name = location.replace("s3://", "").split("/")[0]
                if bucket_name in buckets:
                    graph.add_dependency(action_id, buckets[bucket_name])

        # S3 object operations depend on bucket
        elif op in ["s3.ensure_prefix", "s3.ensure_lifecycle"]:
            bucket = action.get("bucket")
            if bucket in buckets:
                graph.add_dependency(action_id, buckets[bucket])

        # Policy attachment depends on role
        elif op == "iam.attach_policy":
            role = action.get("role_name")
            if role in roles:
                graph.add_dependency(action_id, roles[role])

        # Lambda function may depend on role
        elif op == "lambda.ensure_function":
            role_arn = action.get("role")
            if role_arn:
                # Extract role name from ARN
                role_name = role_arn.split("/")[-1]
                if role_name in roles:
                    graph.add_dependency(action_id, roles[role_name])

    return graph


def order_actions_by_dependencies(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Order actions by dependencies.

    Ensures actions execute in correct order (databases before tables, etc.).

    Args:
        actions: Unordered actions

    Returns:
        Actions in dependency order
    """
    if not actions:
        return []

    graph = analyze_dependencies(actions)
    return graph.get_ordered_actions()


def validate_no_cycles(actions: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    Check for circular dependencies.

    Args:
        actions: Actions to check

    Returns:
        Tuple of (is_valid, error_messages)
    """
    try:
        graph = analyze_dependencies(actions)
        graph.topological_sort()
        return True, []
    except ValueError as e:
        return False, [str(e)]
