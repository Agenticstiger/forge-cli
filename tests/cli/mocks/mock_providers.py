# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
Mock Providers for CLI Testing

Mock implementations of FLUID providers for testing CLI commands
without requiring actual cloud credentials or resources.
"""

from typing import Any, Dict, List


class MockLocalProvider:
    """Mock local provider for testing"""

    def __init__(self):
        self.name = "local"
        self.actions_executed = []

    def plan(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate mock plan"""
        actions = []

        exposes = contract.get("exposes", [])
        for expose in exposes:
            # 0.7.1 format
            if "exposeId" in expose:
                expose_id = expose["exposeId"]
                binding = expose.get("binding", {})
                database = binding.get("database", "default_db")
                table = binding.get("table", expose_id)
            # 0.5.7 format
            else:
                expose_id = expose.get("id", "unknown")
                location = expose.get("location", {})
                database = location.get("database", "default_db")
                table = location.get("table", expose_id)

            actions.append(
                {
                    "op": "create_table",
                    "database": database,
                    "table": table,
                    "description": f"Create table {table}",
                }
            )

        return actions

    def apply(self, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Execute mock actions"""
        results = []

        for action in actions:
            self.actions_executed.append(action)
            results.append(
                {
                    "action": action,
                    "status": "success",
                    "message": f"Executed {action.get('op', 'unknown')}",
                }
            )

        return results

    def apply_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute single mock action"""
        self.actions_executed.append(action)
        return {
            "action": action,
            "status": "success",
            "message": f"Executed {action.get('op', 'unknown')}",
        }


class MockGCPProvider:
    """Mock GCP provider for testing"""

    def __init__(self, project_id="test-project", region="us-central1"):
        self.name = "gcp"
        self.project_id = project_id
        self.region = region
        self.actions_executed = []

    def plan(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate mock GCP plan"""
        return [
            {"op": "create_dataset", "dataset": "test_dataset"},
            {"op": "create_table", "dataset": "test_dataset", "table": "test_table"},
        ]

    def apply(self, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Execute mock GCP actions"""
        results = []
        for action in actions:
            self.actions_executed.append(action)
            results.append({"action": action, "status": "success", "project": self.project_id})
        return results


class MockProviderWithErrors:
    """Mock provider that simulates errors for testing"""

    def __init__(self, fail_on_action=None):
        self.name = "mock-errors"
        self.fail_on_action = fail_on_action
        self.actions_executed = []

    def plan(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate plan that may fail"""
        if self.fail_on_action == "plan":
            raise Exception("Planning failed")

        return [{"op": "create_table", "table": "test_table"}]

    def apply(self, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Execute actions with potential failures"""
        if self.fail_on_action == "apply":
            raise Exception("Execution failed")

        results = []
        for i, action in enumerate(actions):
            if self.fail_on_action == i:
                results.append({"action": action, "status": "failed", "error": "Simulated failure"})
            else:
                results.append({"action": action, "status": "success"})

        return results


def create_mock_provider(provider_type="local", **kwargs):
    """Factory function to create mock providers"""
    providers = {
        "local": MockLocalProvider,
        "gcp": MockGCPProvider,
        "errors": MockProviderWithErrors,
    }

    provider_class = providers.get(provider_type, MockLocalProvider)
    return provider_class(**kwargs)
