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
Provider Action Engine for FLUID 0.7.0+

Parses provider actions from contracts and routes them to provider implementations.
Supports declarative orchestration with provider-agnostic primitives.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ActionType(Enum):
    """Supported provider action types (from FLUID 0.7.1 schema)."""

    PROVISION_DATASET = "provisionDataset"
    GRANT_ACCESS = "grantAccess"
    REVOKE_ACCESS = "revokeAccess"
    SCHEDULE_TASK = "scheduleTask"
    REGISTER_SCHEMA = "registerSchema"
    CREATE_VIEW = "createView"
    UPDATE_POLICY = "updatePolicy"
    PUBLISH_EVENT = "publishEvent"
    CUSTOM = "custom"


@dataclass
class ProviderAction:
    """Normalized provider action."""

    action_id: str
    action_type: ActionType
    provider: str  # aws, gcp, snowflake, local
    params: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)
    description: Optional[str] = None

    def __repr__(self) -> str:
        return f"ProviderAction({self.action_id}, {self.action_type.value}, {self.provider})"


class ProviderActionParser:
    """Parses provider actions from FLUID 0.7.0+ contracts."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)

    def parse(self, contract: Dict[str, Any]) -> List[ProviderAction]:
        """
        Extract provider actions from contract.

        Supports both:
        - 0.7.0+: contract.providerActions array (explicit)
        - 0.5.7: inferred from exposes/builds (implicit)

        Args:
            contract: FLUID contract dict

        Returns:
            List of normalized ProviderAction objects
        """
        fluid_version = contract.get("fluidVersion", "0.7.3")

        # Check for explicit provider actions (0.7.0+)
        if "providerActions" in contract:
            return self._parse_explicit_actions(contract)

        # Fallback: infer actions from 0.5.7 patterns
        self.logger.debug(
            f"No providerActions found, inferring from 0.5.7 patterns (version: {fluid_version})"
        )
        return self._infer_from_legacy(contract)

    def _parse_explicit_actions(self, contract: Dict[str, Any]) -> List[ProviderAction]:
        """Parse explicit providerActions array from 0.7.0+ contracts."""
        actions = []
        provider_actions = contract.get("providerActions", [])

        for i, pa in enumerate(provider_actions):
            action_id = pa.get("actionId", f"action_{i}")
            action_type_str = pa.get("action")
            provider = pa.get("provider", "local")
            params = pa.get("params", {})
            depends_on = pa.get("dependsOn", [])
            description = pa.get("description")

            # Parse action type
            try:
                action_type = ActionType(action_type_str)
            except ValueError:
                self.logger.warning(f"Unknown action type '{action_type_str}', treating as CUSTOM")
                action_type = ActionType.CUSTOM
                params["customAction"] = action_type_str

            actions.append(
                ProviderAction(
                    action_id=action_id,
                    action_type=action_type,
                    provider=provider,
                    params=params,
                    depends_on=depends_on,
                    description=description,
                )
            )

        self.logger.debug(f"Parsed {len(actions)} explicit provider actions")
        return actions

    def _infer_from_legacy(self, contract: Dict[str, Any]) -> List[ProviderAction]:
        """Infer provider actions from 0.5.7-style contracts."""
        actions = []

        # Infer from exposes (provision datasets)
        for i, expose in enumerate(contract.get("exposes", [])):
            binding = expose.get("binding", {})
            # Provider is called "platform" in FLUID 0.7.1 binding schema
            provider = binding.get("platform") or binding.get("provider", "local")
            expose_id = expose.get("exposeId", f"expose_{i}")

            # Extract labels using same logic as planner. Pass the resolved
            # platform so label-value sanitization is GCP-conditional —
            # non-GCP targets must keep label values verbatim.
            labels = self._extract_labels(contract, expose, provider)

            actions.append(
                ProviderAction(
                    action_id=f"provision_{expose_id}",
                    action_type=ActionType.PROVISION_DATASET,
                    provider=provider,
                    params={
                        "exposeId": expose_id,
                        "kind": expose.get("kind"),
                        "binding": binding,
                        "schema": expose.get("contract", {}).get("schema"),
                        "contract": contract,  # Pass full contract for policy extraction
                        "labels": labels,  # Pass extracted labels
                    },
                    description=f"Provision dataset for {expose_id}",
                )
            )

            # Infer access grants from policy
            policy = expose.get("policy", {})
            authz = policy.get("authz", {})
            for grant in authz.get("grants", []):
                actions.append(
                    ProviderAction(
                        action_id=f"grant_{expose_id}_{grant.get('principal', 'unknown')}",
                        action_type=ActionType.GRANT_ACCESS,
                        provider=provider,
                        params={
                            "exposeId": expose_id,
                            "principal": grant.get("principal"),
                            "role": grant.get("role"),
                            "binding": binding,
                        },
                        depends_on=[f"provision_{expose_id}"],
                        description=f"Grant access to {expose_id}",
                    )
                )

        # Infer from builds (schedule tasks). Pass through the build's
        # SQL + outputs + properties so the provider's ``scheduleTask``
        # handler can run the SQL inline for ``engine: sql`` builds
        # (Snowflake / BigQuery / etc.).
        for i, build in enumerate(contract.get("builds", [])):
            build_id = build.get("buildId") or build.get("id") or f"build_{i}"
            engine = build.get("engine", "dbt")
            build_props = build.get("properties") or {}

            actions.append(
                ProviderAction(
                    action_id=f"schedule_{build_id}",
                    action_type=ActionType.SCHEDULE_TASK,
                    provider="local",
                    params={
                        "buildId": build_id,
                        "engine": engine,
                        "script": build.get("script"),
                        "schedule": build.get("schedule"),
                        # Inline-execution data: SQL builds need the
                        # SQL string, outputs reference the target
                        # expose, properties carry per-engine knobs.
                        "sql": build.get("sql") or build_props.get("sql"),
                        "outputs": build.get("outputs") or [],
                        "pattern": build.get("pattern"),
                    },
                    description=f"Schedule build task {build_id}",
                )
            )

        self.logger.debug(f"Inferred {len(actions)} actions from legacy contract")
        return actions

    def _extract_labels(
        self,
        contract: Dict[str, Any],
        exposure: Dict[str, Any],
        provider: Optional[str] = None,
    ) -> Dict[str, str]:
        """Extract labels from contract and exposure.

        Label *value* sanitization is platform-aware. The GCP/BigQuery label
        constraint is "lowercase ``[a-z0-9_-]`` only, <= 63 chars" — applying
        that unconditionally silently mangles label values for every other
        target (CJK ``用户三百六十度`` collapses to ``_______``, ``NO`` becomes
        ``no``, ``1.2.3`` becomes ``1_2_3``). Snowflake / AWS / local tags and
        labels are free-form strings and must round-trip faithfully into
        ``plan.json``; only GCP/BigQuery exposures get the lossy rewrite.

        Label *keys* are still normalized on every platform: keys feed into
        action-param dict keys and downstream tooling expects a stable,
        identifier-shaped key regardless of target.

        Args:
            contract: full FLUID contract dict.
            exposure: the single ``exposes[]`` entry being provisioned.
            provider: resolved target platform (``gcp``/``aws``/``snowflake``/
                ``local``/...). When ``None`` it is re-derived from the
                exposure's ``binding`` so the function is safe to call
                without the caller pre-resolving it.
        """
        import re

        # Resolve the target platform. The caller in ``_infer_from_legacy``
        # already computes this from ``binding.platform``; fall back to
        # re-deriving it here so the helper is self-sufficient.
        binding = exposure.get("binding", {}) or {}
        resolved_platform = (
            provider or binding.get("platform") or binding.get("provider") or "local"
        )
        platform_lc = str(resolved_platform).strip().lower()
        binding_format = str(binding.get("format", "")).strip().lower()
        # GCP labels: platform ``gcp`` (canonical), the ``bigquery`` alias
        # used in some provider code paths, or a ``bigquery_table`` binding
        # format. These are the only targets whose label values are subject
        # to GCP's ``[a-z0-9_-]`` lowercase constraint.
        is_gcp_target = platform_lc in ("gcp", "bigquery") or binding_format in (
            "bigquery_table",
            "bigquery",
        )

        def sanitize_label_key(key: str) -> str:
            sanitized = re.sub(r"[^a-z0-9_-]", "_", key.lower())
            if sanitized and not sanitized[0].isalpha():
                sanitized = f"label_{sanitized}"
            return sanitized[:63] if sanitized else ""

        def sanitize_label_value(value: str) -> str:
            # Non-GCP targets (Snowflake / AWS / local / ...) accept
            # free-form label/tag values — preserve them verbatim so the
            # value round-trips faithfully into ``plan.json``. ``str()`` is
            # an identity for the str inputs this helper receives and only
            # guards the declared ``Dict[str, str]`` return contract.
            if not is_gcp_target:
                return str(value)
            # GCP/BigQuery: enforce the platform's actual label constraint.
            sanitized = re.sub(r"[^a-z0-9_-]", "_", str(value).lower())
            return sanitized[:63] if sanitized else ""

        labels = {}

        # Contract-level labels
        if contract.get("id"):
            labels["fluid_contract_id"] = sanitize_label_value(contract["id"])
        if contract.get("name"):
            labels["fluid_contract_name"] = sanitize_label_value(contract["name"])

        metadata = contract.get("metadata", {})
        if metadata.get("layer"):
            labels["fluid_layer"] = sanitize_label_value(metadata["layer"])
        if metadata.get("productType"):
            labels["fluid_product_type"] = sanitize_label_value(metadata["productType"])
        if metadata.get("domain"):
            labels["fluid_domain"] = sanitize_label_value(metadata.get("domain", ""))
        if metadata.get("owner", {}).get("team"):
            labels["fluid_team"] = sanitize_label_value(metadata["owner"]["team"])

        # Contract custom labels
        for key, value in contract.get("labels", {}).items():
            sanitized_key = sanitize_label_key(key)
            if sanitized_key:
                labels[sanitized_key] = sanitize_label_value(str(value))

        # Contract tags → labels
        for tag in contract.get("tags", []):
            safe_tag = sanitize_label_key(tag)
            if safe_tag:
                labels[f"tag_{safe_tag}"] = "true"

        # Exposure-level labels
        for key, value in exposure.get("labels", {}).items():
            sanitized_key = sanitize_label_key(key)
            if sanitized_key:
                labels[sanitized_key] = sanitize_label_value(str(value))

        # Exposure tags → labels
        for tag in exposure.get("tags", []):
            safe_tag = sanitize_label_key(tag)
            if safe_tag:
                labels[f"tag_{safe_tag}"] = "true"

        # Policy governance labels
        policy = exposure.get("policy", {})
        if policy.get("classification"):
            labels["data_classification"] = sanitize_label_value(policy["classification"])
        if policy.get("authn"):
            labels["authn_method"] = sanitize_label_value(policy["authn"])

        # Policy labels
        for key, value in policy.get("labels", {}).items():
            sanitized_key = sanitize_label_key(f"policy_{key}")
            if sanitized_key:
                labels[sanitized_key] = sanitize_label_value(str(value))

        # Policy tags
        for tag in policy.get("tags", []):
            safe_tag = sanitize_label_key(tag)
            if safe_tag:
                labels[f"policy_{safe_tag}"] = "true"

        return labels

    def build_dependency_graph(self, actions: List[ProviderAction]) -> Dict[str, Any]:
        """
        Build dependency graph from provider actions with cycle detection.

        Returns:
            {
                "graph": {action_id: [dependency_action_ids]},
                "has_cycles": bool,
                "cycles": [[action_ids_in_cycle]]
            }
        """
        graph = {}
        for action in actions:
            graph[action.action_id] = action.depends_on

        # Detect cycles using DFS
        has_cycles, cycles = self._detect_cycles(actions)

        return {"graph": graph, "has_cycles": has_cycles, "cycles": cycles}

    def _detect_cycles(self, actions: List[ProviderAction]) -> Tuple[bool, List[List[str]]]:
        """Detect circular dependencies using DFS."""
        visited = set()
        rec_stack = set()
        cycles = []

        def dfs(action_id: str, path: List[str]) -> bool:
            visited.add(action_id)
            rec_stack.add(action_id)
            path.append(action_id)

            # Find action
            action = next((a for a in actions if a.action_id == action_id), None)
            if not action:
                rec_stack.remove(action_id)
                return False

            for dep in action.depends_on:
                if dep not in visited:
                    if dfs(dep, path.copy()):
                        return True
                elif dep in rec_stack:
                    # Cycle detected
                    cycle_start = path.index(dep)
                    cycle = path[cycle_start:] + [dep]
                    cycles.append(cycle)
                    rec_stack.remove(action_id)
                    return True

            rec_stack.remove(action_id)
            return False

        for action in actions:
            if action.action_id not in visited:
                dfs(action.action_id, [])

        return len(cycles) > 0, cycles

    def get_execution_order(self, actions: List[ProviderAction]) -> List[List[str]]:
        """
        Get execution order respecting dependencies (topological sort).

        Returns:
            List of action ID batches (each batch can execute in parallel)
        """
        self.build_dependency_graph(actions)

        # Calculate in-degrees
        in_degree = {action.action_id: 0 for action in actions}
        for action in actions:
            for dep in action.depends_on:
                if dep in in_degree:
                    in_degree[action.action_id] += 1

        # Topological sort by levels.
        #
        # Iterate in the stable order actions were parsed (``exposes[]`` then
        # ``builds[]`` in document order) — NOT in ``set`` iteration order. A
        # ``set`` of action-id strings iterates in an order that depends on
        # per-process hash randomisation (PYTHONHASHSEED), so two otherwise-
        # identical ``fluid plan`` runs emitted the same actions in different
        # order. That ordering flows into ``planDigest`` (action order is part
        # of the plan-binding hash by design — see test_actions_reorder_*), so
        # the digest was non-deterministic across runs. Preserving parse order
        # keeps the plan body — and its digest — byte-stable.
        order = [action.action_id for action in actions]
        levels = []
        remaining = set(order)

        while remaining:
            # Nodes with no remaining dependencies, in stable parse order.
            current_level = [aid for aid in order if aid in remaining and in_degree[aid] == 0]

            if not current_level:
                # Circular dependency detected
                self.logger.warning(f"Circular dependency detected in actions: {remaining}")
                break

            levels.append(current_level)

            # Remove current level and update in-degrees
            for action_id in current_level:
                remaining.remove(action_id)
                for action in actions:
                    if action_id in action.depends_on:
                        in_degree[action.action_id] -= 1

        return levels


def get_action_by_id(actions: List[ProviderAction], action_id: str) -> Optional[ProviderAction]:
    """Find action by ID."""
    for action in actions:
        if action.action_id == action_id:
            return action
    return None


def filter_actions_by_provider(
    actions: List[ProviderAction], provider: str
) -> List[ProviderAction]:
    """Filter actions by provider."""
    return [a for a in actions if a.provider == provider]


def filter_actions_by_type(
    actions: List[ProviderAction], action_type: ActionType
) -> List[ProviderAction]:
    """Filter actions by type."""
    return [a for a in actions if a.action_type == action_type]
