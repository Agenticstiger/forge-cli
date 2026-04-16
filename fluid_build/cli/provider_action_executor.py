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

"""
FLUID 0.7.1 Provider Action Executor
Integrates provider action handlers into the apply workflow.
"""

import logging
from typing import Any, Dict, List, Optional

from ..forge.core.provider_actions import ProviderAction, ProviderActionParser


class ProviderActionExecutor:
    """
    Executes provider actions using provider-specific handlers.

    Coordinates between parsed actions and provider implementations
    (GCP, AWS, Snowflake, etc.).
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize executor.

        Args:
            logger: Optional logger instance
        """
        self.logger = logger or logging.getLogger(__name__)
        self.handlers = {}

    def execute_actions(
        self, contract: Dict[str, Any], provider_instances: Dict[str, Any], dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Execute all provider actions from a contract.

        Args:
            contract: FLUID contract dict
            provider_instances: Dict of provider name -> provider instance
            dry_run: If True, only simulate execution

        Returns:
            Execution results with status and details
        """
        # Parse actions from contract
        parser = ProviderActionParser(self.logger)
        actions = parser.parse(contract)

        if not actions:
            return {
                "success": True,
                "message": "No provider actions to execute",
                "actions_executed": 0,
            }

        # Build dependency graph
        graph = parser.build_dependency_graph(actions)

        if graph.get("has_cycles"):
            return {
                "success": False,
                "error": "Circular dependency detected in provider actions",
                "cycles": graph.get("cycles", []),
            }

        # Execute actions in topological order
        ordered_actions = graph["ordered_actions"]

        if dry_run:
            return self._dry_run_actions(ordered_actions)

        return self._execute_actions_ordered(ordered_actions, provider_instances)

    def _dry_run_actions(self, actions: List[ProviderAction]) -> Dict[str, Any]:
        """
        Simulate action execution (dry run).

        Args:
            actions: List of actions to simulate

        Returns:
            Simulation results
        """
        self.logger.info(f"[DRY RUN] Would execute {len(actions)} actions")

        results = []
        for i, action in enumerate(actions):
            result = {
                "step": i + 1,
                "action_id": action.action_id,
                "action_type": action.action_type.value,
                "provider": action.provider,
                "status": "simulated",
                "message": f"Would execute {action.action_type.value} on {action.provider}",
            }
            results.append(result)
            self.logger.info(f"  [{i+1}] {action.action_id} ({action.action_type.value})")

        return {
            "success": True,
            "dry_run": True,
            "actions_simulated": len(actions),
            "results": results,
        }

    def _execute_actions_ordered(
        self, actions: List[ProviderAction], provider_instances: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute actions in dependency order.

        Args:
            actions: Ordered list of actions
            provider_instances: Available provider instances

        Returns:
            Execution results
        """
        overall_results = {
            "success": True,
            "actions_executed": 0,
            "actions_failed": 0,
            "results": [],
        }

        for i, action in enumerate(actions):
            self.logger.info(f"Executing action {i+1}/{len(actions)}: {action.action_id}")

            try:
                result = self._execute_single_action(action, provider_instances)
                overall_results["actions_executed"] += 1
                overall_results["results"].append(
                    {
                        "step": i + 1,
                        "action_id": action.action_id,
                        "status": "success",
                        "result": result,
                    }
                )

            except Exception as e:
                self.logger.error(f"Action {action.action_id} failed: {e}")
                overall_results["success"] = False
                overall_results["actions_failed"] += 1
                overall_results["results"].append(
                    {
                        "step": i + 1,
                        "action_id": action.action_id,
                        "status": "failed",
                        "error": str(e),
                    }
                )

                # Stop on first failure by default
                # (could be configurable to continue on errors)
                break

        return overall_results

    def _execute_single_action(
        self, action: ProviderAction, provider_instances: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute a single provider action.

        Args:
            action: Action to execute
            provider_instances: Available provider instances

        Returns:
            Action execution result
        """
        provider_name = action.provider.lower()

        # Get provider instance
        provider = provider_instances.get(provider_name)
        if not provider:
            raise ValueError(
                f"Provider '{provider_name}' not available. "
                f"Available: {list(provider_instances.keys())}"
            )

        # Get or create handler for this provider
        handler = self._get_handler_for_provider(provider_name, provider)

        # Execute action through handler
        result = handler._execute_single_action(action)

        return result

    def _get_handler_for_provider(self, provider_name: str, provider_instance: Any):
        """
        Get or create provider action handler.

        Args:
            provider_name: Provider identifier (gcp, aws, etc.)
            provider_instance: Provider instance

        Returns:
            Provider action handler
        """
        if provider_name in self.handlers:
            return self.handlers[provider_name]

        # Create handler based on provider type
        if provider_name == "gcp":
            from ..providers.gcp.provider_action_handler import GCPProviderActionHandler

            handler = GCPProviderActionHandler(provider_instance, self.logger)
        elif provider_name == "aws":
            # AWS uses the planner + provider.apply() flow directly;
            # high-level action handler was removed in favour of service-level dispatch.
            handler = GenericProviderActionHandler(provider_instance, self.logger)
        elif provider_name == "snowflake":
            # TODO: Implement Snowflake handler
            raise NotImplementedError("Snowflake provider action handler not yet implemented")
        elif provider_name == "airflow":
            # Airflow actions are typically delegated/generated, not directly executed
            handler = AirflowActionHandler(self.logger)
        else:
            # Generic fallback handler
            handler = GenericProviderActionHandler(provider_instance, self.logger)

        self.handlers[provider_name] = handler
        return handler


class AirflowActionHandler:
    """Handler for Airflow orchestration actions."""

    def __init__(self, logger):
        self.logger = logger

    def _execute_single_action(self, action: ProviderAction) -> Dict[str, Any]:
        """Airflow actions are typically delegated to DAG generation."""
        self.logger.info(f"Airflow action {action.action_id} delegated to DAG generation")
        return {
            "status": "delegated",
            "message": "Action will be executed by Airflow DAG",
            "action_id": action.action_id,
        }


class GenericProviderActionHandler:
    """Generic handler for providers without specific implementation."""

    def __init__(self, provider, logger):
        self.provider = provider
        self.logger = logger

    def _execute_single_action(self, action: ProviderAction) -> Dict[str, Any]:
        """Generic action execution - calls provider if it supports the action."""
        method_name = f"execute_{action.action_type.value}"

        if hasattr(self.provider, method_name):
            method = getattr(self.provider, method_name)
            return method(action.params)
        else:
            self.logger.warning(
                f"Provider {type(self.provider).__name__} does not support "
                f"action type {action.action_type.value}"
            )
            return {
                "status": "not_implemented",
                "message": f"Action type {action.action_type.value} not implemented for this provider",
            }
