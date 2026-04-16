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

"""Tests for fluid_build.forge.core.provider_actions — action parsing & dependency graphs."""

from fluid_build.forge.core.provider_actions import (
    ActionType,
    ProviderAction,
    ProviderActionParser,
    filter_actions_by_provider,
    filter_actions_by_type,
    get_action_by_id,
)


class TestActionType:
    def test_values(self):
        assert ActionType.PROVISION_DATASET.value == "provisionDataset"
        assert ActionType.GRANT_ACCESS.value == "grantAccess"
        assert ActionType.CUSTOM.value == "custom"


class TestProviderAction:
    def test_defaults(self):
        a = ProviderAction(
            action_id="a1",
            action_type=ActionType.PROVISION_DATASET,
            provider="gcp",
            params={"k": "v"},
        )
        assert a.depends_on == []
        assert a.description is None

    def test_repr(self):
        a = ProviderAction(
            action_id="a1",
            action_type=ActionType.GRANT_ACCESS,
            provider="aws",
            params={},
        )
        r = repr(a)
        assert "a1" in r
        assert "grantAccess" in r
        assert "aws" in r


class TestProviderActionParser:
    def test_parse_explicit_actions(self):
        contract = {
            "fluidVersion": "0.7.1",
            "providerActions": [
                {
                    "actionId": "prov1",
                    "action": "provisionDataset",
                    "provider": "gcp",
                    "params": {"dataset": "x"},
                },
                {
                    "actionId": "grant1",
                    "action": "grantAccess",
                    "provider": "gcp",
                    "params": {},
                    "dependsOn": ["prov1"],
                },
            ],
        }
        parser = ProviderActionParser()
        actions = parser.parse(contract)
        assert len(actions) == 2
        assert actions[0].action_type == ActionType.PROVISION_DATASET
        assert actions[1].depends_on == ["prov1"]

    def test_unknown_action_type_becomes_custom(self):
        contract = {
            "providerActions": [
                {"actionId": "x", "action": "someFutureAction", "provider": "local"},
            ],
        }
        actions = ProviderActionParser().parse(contract)
        assert actions[0].action_type == ActionType.CUSTOM
        assert actions[0].params.get("customAction") == "someFutureAction"

    def test_infer_from_legacy_exposes(self):
        contract = {
            "fluidVersion": "0.5.7",
            "exposes": [
                {
                    "exposeId": "users_table",
                    "kind": "table",
                    "binding": {"platform": "gcp", "location": {"region": "us-east1"}},
                    "policy": {},
                },
            ],
        }
        actions = ProviderActionParser().parse(contract)
        assert any(a.action_type == ActionType.PROVISION_DATASET for a in actions)
        assert actions[0].provider == "gcp"

    def test_infer_access_grants(self):
        contract = {
            "exposes": [
                {
                    "exposeId": "e1",
                    "binding": {"provider": "aws"},
                    "policy": {
                        "authz": {
                            "grants": [
                                {"principal": "team-a", "role": "reader"},
                            ],
                        },
                    },
                },
            ],
        }
        actions = ProviderActionParser().parse(contract)
        grant_actions = [a for a in actions if a.action_type == ActionType.GRANT_ACCESS]
        assert len(grant_actions) == 1
        assert grant_actions[0].depends_on == ["provision_e1"]

    def test_infer_from_builds(self):
        contract = {
            "builds": [
                {"buildId": "dbt_run", "engine": "dbt", "script": "run.sh"},
            ],
        }
        actions = ProviderActionParser().parse(contract)
        assert any(a.action_type == ActionType.SCHEDULE_TASK for a in actions)

    def test_extract_labels(self):
        parser = ProviderActionParser()
        contract = {
            "id": "My-Product",
            "name": "My Product",
            "metadata": {"layer": "Gold", "domain": "Finance", "owner": {"team": "data-eng"}},
            "tags": ["important"],
            "labels": {"costCenter": "42"},
        }
        expose = {
            "tags": ["expose-tag"],
            "labels": {"custom": "val"},
            "policy": {"classification": "confidential"},
        }
        labels = parser._extract_labels(contract, expose)
        assert labels["fluid_contract_id"] == "my-product"
        assert labels["fluid_layer"] == "gold"
        assert labels["fluid_domain"] == "finance"
        assert labels["tag_important"] == "true"
        assert labels["data_classification"] == "confidential"

    # Dependency graph & cycle detection
    def test_no_cycles(self):
        actions = [
            ProviderAction("a", ActionType.PROVISION_DATASET, "gcp", {}),
            ProviderAction("b", ActionType.GRANT_ACCESS, "gcp", {}, depends_on=["a"]),
        ]
        graph = ProviderActionParser().build_dependency_graph(actions)
        assert graph["has_cycles"] is False

    def test_cycle_detected(self):
        actions = [
            ProviderAction("a", ActionType.PROVISION_DATASET, "gcp", {}, depends_on=["b"]),
            ProviderAction("b", ActionType.GRANT_ACCESS, "gcp", {}, depends_on=["a"]),
        ]
        graph = ProviderActionParser().build_dependency_graph(actions)
        assert graph["has_cycles"] is True

    def test_execution_order_simple(self):
        actions = [
            ProviderAction("a", ActionType.PROVISION_DATASET, "gcp", {}),
            ProviderAction("b", ActionType.GRANT_ACCESS, "gcp", {}, depends_on=["a"]),
            ProviderAction("c", ActionType.REGISTER_SCHEMA, "gcp", {}, depends_on=["a"]),
        ]
        levels = ProviderActionParser().get_execution_order(actions)
        assert levels[0] == ["a"]
        assert set(levels[1]) == {"b", "c"}

    def test_execution_order_no_deps(self):
        actions = [
            ProviderAction("x", ActionType.PROVISION_DATASET, "gcp", {}),
            ProviderAction("y", ActionType.PROVISION_DATASET, "aws", {}),
        ]
        levels = ProviderActionParser().get_execution_order(actions)
        assert len(levels) == 1
        assert set(levels[0]) == {"x", "y"}


class TestHelperFunctions:
    def _actions(self):
        return [
            ProviderAction("a1", ActionType.PROVISION_DATASET, "gcp", {}),
            ProviderAction("a2", ActionType.GRANT_ACCESS, "aws", {}),
            ProviderAction("a3", ActionType.PROVISION_DATASET, "aws", {}),
        ]

    def test_get_action_by_id_found(self):
        actions = self._actions()
        assert get_action_by_id(actions, "a2").provider == "aws"

    def test_get_action_by_id_missing(self):
        assert get_action_by_id(self._actions(), "nope") is None

    def test_filter_by_provider(self):
        result = filter_actions_by_provider(self._actions(), "aws")
        assert len(result) == 2
        assert all(a.provider == "aws" for a in result)

    def test_filter_by_type(self):
        result = filter_actions_by_type(self._actions(), ActionType.PROVISION_DATASET)
        assert len(result) == 2
