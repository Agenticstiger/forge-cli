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

"""Tests for forge/core/provider_actions.py — action parser, dependency graph, execution order."""

from fluid_build.forge.core.provider_actions import (
    ActionType,
    ProviderAction,
    ProviderActionParser,
    filter_actions_by_provider,
    filter_actions_by_type,
    get_action_by_id,
)


# ── ActionType enum ──────────────────────────────────────────────────
class TestActionType:
    def test_all_values(self):
        assert ActionType.PROVISION_DATASET.value == "provisionDataset"
        assert ActionType.GRANT_ACCESS.value == "grantAccess"
        assert ActionType.REVOKE_ACCESS.value == "revokeAccess"
        assert ActionType.SCHEDULE_TASK.value == "scheduleTask"
        assert ActionType.REGISTER_SCHEMA.value == "registerSchema"
        assert ActionType.CREATE_VIEW.value == "createView"
        assert ActionType.UPDATE_POLICY.value == "updatePolicy"
        assert ActionType.PUBLISH_EVENT.value == "publishEvent"
        assert ActionType.CUSTOM.value == "custom"

    def test_from_value(self):
        assert ActionType("provisionDataset") is ActionType.PROVISION_DATASET


# ── ProviderAction dataclass ─────────────────────────────────────────
class TestProviderAction:
    def test_defaults(self):
        pa = ProviderAction("a1", ActionType.CUSTOM, "local", {})
        assert pa.depends_on == []
        assert pa.description is None

    def test_repr(self):
        pa = ProviderAction("a1", ActionType.GRANT_ACCESS, "aws", {})
        assert "a1" in repr(pa) and "grantAccess" in repr(pa) and "aws" in repr(pa)


# ── ProviderActionParser._parse_explicit_actions ─────────────────────
class TestParseExplicitActions:
    def setup_method(self):
        self.parser = ProviderActionParser()

    def test_basic_explicit(self):
        contract = {
            "providerActions": [
                {
                    "actionId": "p1",
                    "action": "provisionDataset",
                    "provider": "gcp",
                    "params": {"x": 1},
                },
                {"actionId": "g1", "action": "grantAccess", "provider": "aws", "dependsOn": ["p1"]},
            ]
        }
        actions = self.parser.parse(contract)
        assert len(actions) == 2
        assert actions[0].action_type is ActionType.PROVISION_DATASET
        assert actions[0].provider == "gcp"
        assert actions[0].params == {"x": 1}
        assert actions[1].depends_on == ["p1"]

    def test_unknown_action_becomes_custom(self):
        contract = {"providerActions": [{"action": "myAction"}]}
        actions = self.parser.parse(contract)
        assert actions[0].action_type is ActionType.CUSTOM
        assert actions[0].params["customAction"] == "myAction"

    def test_defaults_when_fields_missing(self):
        contract = {"providerActions": [{"action": "grantAccess"}]}
        actions = self.parser.parse(contract)
        assert actions[0].action_id == "action_0"
        assert actions[0].provider == "local"
        assert actions[0].depends_on == []

    def test_description_preserved(self):
        contract = {"providerActions": [{"action": "custom", "description": "hello"}]}
        actions = self.parser.parse(contract)
        assert actions[0].description == "hello"


# ── ProviderActionParser._infer_from_legacy ──────────────────────────
class TestInferFromLegacy:
    def setup_method(self):
        self.parser = ProviderActionParser()

    def test_no_provider_actions_key_triggers_fallback(self):
        """Without 'providerActions', legacy path runs."""
        contract = {"fluidVersion": "0.5.7", "exposes": [], "builds": []}
        actions = self.parser.parse(contract)
        assert actions == []

    def test_expose_provisions_dataset(self):
        contract = {
            "exposes": [
                {
                    "exposeId": "e1",
                    "kind": "table",
                    "binding": {"platform": "snowflake"},
                    "contract": {"schema": [{"name": "id", "type": "INTEGER"}]},
                }
            ]
        }
        actions = self.parser.parse(contract)
        assert len(actions) == 1
        assert actions[0].action_type is ActionType.PROVISION_DATASET
        assert actions[0].provider == "snowflake"
        assert actions[0].params["kind"] == "table"

    def test_expose_with_grants(self):
        contract = {
            "exposes": [
                {
                    "exposeId": "e2",
                    "binding": {"provider": "aws"},
                    "policy": {
                        "authz": {
                            "grants": [
                                {"principal": "team_a", "role": "reader"},
                                {"principal": "team_b", "role": "writer"},
                            ]
                        }
                    },
                }
            ]
        }
        actions = self.parser.parse(contract)
        # 1 provision + 2 grants
        assert len(actions) == 3
        grants = [a for a in actions if a.action_type is ActionType.GRANT_ACCESS]
        assert len(grants) == 2
        assert all(g.depends_on == ["provision_e2"] for g in grants)

    def test_builds_schedule_task(self):
        contract = {
            "builds": [{"buildId": "b1", "engine": "dbt", "script": "run.sh", "schedule": "@daily"}]
        }
        actions = self.parser.parse(contract)
        assert len(actions) == 1
        assert actions[0].action_type is ActionType.SCHEDULE_TASK
        assert actions[0].params["engine"] == "dbt"
        assert actions[0].params["schedule"] == "@daily"
        assert actions[0].provider == "local"

    def test_expose_provider_fallback_to_local(self):
        contract = {"exposes": [{"binding": {}}]}
        actions = self.parser.parse(contract)
        assert actions[0].provider == "local"


# ── _extract_labels (GCP target — label values get GCP sanitization) ──
class TestExtractLabels:
    """GCP/BigQuery exposures: label *values* are lowercased + ``[a-z0-9_-]``.

    Every case here passes ``provider="gcp"`` so the GCP label constraint
    applies — these assertions pin the GCP-target behavior. Non-GCP target
    behavior (verbatim preservation) is pinned in
    :class:`TestExtractLabelsPlatformAware`.
    """

    def setup_method(self):
        self.parser = ProviderActionParser()

    def test_contract_id_and_name(self):
        labels = self.parser._extract_labels(
            {"id": "My-Contract", "name": "Hello World"}, {}, provider="gcp"
        )
        assert labels["fluid_contract_id"] == "my-contract"
        assert labels["fluid_contract_name"] == "hello_world"

    def test_metadata_layer_domain_team(self):
        contract = {
            "metadata": {
                "layer": "gold",
                "domain": "finance",
                "owner": {"team": "Data Eng"},
            }
        }
        labels = self.parser._extract_labels(contract, {}, provider="gcp")
        assert labels["fluid_layer"] == "gold"
        assert labels["fluid_domain"] == "finance"
        assert labels["fluid_team"] == "data_eng"

    def test_contract_custom_labels(self):
        labels = self.parser._extract_labels(
            {"labels": {"Cost-Center": "CC99"}}, {}, provider="gcp"
        )
        assert labels["cost-center"] == "cc99"

    def test_contract_tags(self):
        labels = self.parser._extract_labels({"tags": ["PII", "real-time"]}, {}, provider="gcp")
        assert labels["tag_pii"] == "true"
        assert labels["tag_real-time"] == "true"

    def test_exposure_labels_and_tags(self):
        labels = self.parser._extract_labels(
            {}, {"labels": {"env": "prod"}, "tags": ["critical"]}, provider="gcp"
        )
        assert labels["env"] == "prod"
        assert labels["tag_critical"] == "true"

    def test_policy_classification_authn(self):
        labels = self.parser._extract_labels(
            {},
            {"policy": {"classification": "PII", "authn": "oauth2"}},
            provider="gcp",
        )
        assert labels["data_classification"] == "pii"
        assert labels["authn_method"] == "oauth2"

    def test_policy_labels_and_tags(self):
        labels = self.parser._extract_labels(
            {},
            {"policy": {"labels": {"review": "done"}, "tags": ["compliance"]}},
            provider="gcp",
        )
        assert labels["policy_review"] == "done"
        assert labels["policy_compliance"] == "true"

    def test_label_key_starts_with_digit(self):
        # Key normalization is platform-independent — keys feed into action
        # param dict keys, so they are always identifier-shaped.
        labels = self.parser._extract_labels({"labels": {"1abc": "val"}}, {}, provider="gcp")
        assert "label_1abc" in labels

    def test_empty_sanitized_key_skipped(self):
        # A label key that sanitizes to empty should be skipped
        labels = self.parser._extract_labels({"labels": {"": "x"}}, {}, provider="gcp")
        assert "" not in labels


# ── _extract_labels (platform-aware value sanitization — BUG-1 fix) ───
class TestExtractLabelsPlatformAware:
    """Non-GCP targets must keep label *values* verbatim.

    Regression guard for the lossy-mangling bug: ``sanitize_label_value``
    used to run GCP's ``[a-z0-9_-]`` lowercase rewrite on every target,
    silently corrupting Snowflake / AWS / local label values in
    ``plan.json`` (CJK collapsed to underscores, ``NO`` → ``no``,
    ``1.2.3`` → ``1_2_3``).
    """

    def setup_method(self):
        self.parser = ProviderActionParser()

    def test_snowflake_preserves_cjk_value(self):
        labels = self.parser._extract_labels(
            {"labels": {"segment": "用户三百六十度"}}, {}, provider="snowflake"
        )
        assert labels["segment"] == "用户三百六十度"

    def test_snowflake_preserves_case_and_dots(self):
        contract = {
            "id": "My-Product",
            "name": "My Product",
            "metadata": {"layer": "Gold", "owner": {"team": "Data Eng"}},
            "labels": {"version": "1.2.3", "approved": "NO"},
        }
        labels = self.parser._extract_labels(contract, {}, provider="snowflake")
        # Verbatim — no lowercasing, no char substitution.
        assert labels["fluid_contract_id"] == "My-Product"
        assert labels["fluid_contract_name"] == "My Product"
        assert labels["fluid_layer"] == "Gold"
        assert labels["fluid_team"] == "Data Eng"
        assert labels["version"] == "1.2.3"
        assert labels["approved"] == "NO"

    def test_aws_preserves_value_verbatim(self):
        labels = self.parser._extract_labels(
            {}, {"policy": {"classification": "PII"}}, provider="aws"
        )
        assert labels["data_classification"] == "PII"

    def test_local_preserves_value_verbatim(self):
        labels = self.parser._extract_labels({}, {"labels": {"env": "PROD-East"}}, provider="local")
        assert labels["env"] == "PROD-East"

    def test_gcp_still_sanitizes_value(self):
        # The GCP path remains lossy by design — GCP labels require it.
        labels = self.parser._extract_labels(
            {"labels": {"version": "1.2.3", "approved": "NO"}}, {}, provider="gcp"
        )
        assert labels["version"] == "1_2_3"
        assert labels["approved"] == "no"

    def test_platform_derived_from_binding_when_provider_omitted(self):
        # When the caller omits ``provider``, the platform is re-derived
        # from the exposure's binding — Snowflake binding ⇒ verbatim.
        labels = self.parser._extract_labels(
            {"labels": {"team": "Data-Eng"}},
            {"binding": {"platform": "snowflake"}},
        )
        assert labels["team"] == "Data-Eng"

    def test_binding_format_bigquery_table_triggers_gcp_sanitization(self):
        # A ``bigquery_table`` binding format implies a GCP target even if
        # ``provider`` is not explicitly "gcp".
        labels = self.parser._extract_labels(
            {"labels": {"team": "Data Eng"}},
            {"binding": {"format": "bigquery_table"}},
        )
        assert labels["team"] == "data_eng"

    def test_unknown_provider_defaults_to_verbatim(self):
        # Unknown / absent platform defaults to non-GCP (local) ⇒ verbatim.
        labels = self.parser._extract_labels(
            {"labels": {"team": "Data Eng"}}, {}, provider="databricks"
        )
        assert labels["team"] == "Data Eng"

    def test_legacy_inference_threads_platform_into_labels(self):
        # End-to-end through the public ``parse()`` path: a Snowflake
        # exposure must round-trip the label value into the action params.
        contract = {
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {"platform": "snowflake"},
                    "labels": {"cost_center": "FIN-2024"},
                }
            ]
        }
        actions = self.parser.parse(contract)
        provision = next(a for a in actions if a.action_id == "provision_orders")
        assert provision.params["labels"]["cost_center"] == "FIN-2024"

    def test_legacy_inference_gcp_exposure_sanitizes_labels(self):
        contract = {
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {"platform": "gcp"},
                    "labels": {"cost_center": "FIN-2024"},
                }
            ]
        }
        actions = self.parser.parse(contract)
        provision = next(a for a in actions if a.action_id == "provision_orders")
        assert provision.params["labels"]["cost_center"] == "fin-2024"


# ── Dependency graph + cycle detection ───────────────────────────────
class TestDependencyGraph:
    def setup_method(self):
        self.parser = ProviderActionParser()

    def _make_actions(self, specs):
        return [
            ProviderAction(s[0], ActionType.CUSTOM, "local", {}, depends_on=s[1]) for s in specs
        ]

    def test_no_cycles(self):
        actions = self._make_actions([("a", []), ("b", ["a"]), ("c", ["b"])])
        result = self.parser.build_dependency_graph(actions)
        assert result["has_cycles"] is False

    def test_cycle_detected(self):
        actions = self._make_actions([("a", ["c"]), ("b", ["a"]), ("c", ["b"])])
        result = self.parser.build_dependency_graph(actions)
        assert result["has_cycles"] is True

    def test_graph_keys(self):
        actions = self._make_actions([("x", []), ("y", ["x"])])
        result = self.parser.build_dependency_graph(actions)
        assert result["graph"] == {"x": [], "y": ["x"]}


class TestExecutionOrder:
    def setup_method(self):
        self.parser = ProviderActionParser()

    def _make_actions(self, specs):
        return [
            ProviderAction(s[0], ActionType.CUSTOM, "local", {}, depends_on=s[1]) for s in specs
        ]

    def test_linear_chain(self):
        actions = self._make_actions([("a", []), ("b", ["a"]), ("c", ["b"])])
        levels = self.parser.get_execution_order(actions)
        assert levels == [["a"], ["b"], ["c"]]

    def test_parallel_independent(self):
        actions = self._make_actions([("a", []), ("b", []), ("c", ["a", "b"])])
        levels = self.parser.get_execution_order(actions)
        assert len(levels) == 2
        assert set(levels[0]) == {"a", "b"}
        assert levels[1] == ["c"]

    def test_empty(self):
        levels = self.parser.get_execution_order([])
        assert levels == []

    def test_cycle_stops_gracefully(self):
        actions = self._make_actions([("a", ["b"]), ("b", ["a"])])
        levels = self.parser.get_execution_order(actions)
        # Should not hang — either empty or partial
        assert isinstance(levels, list)


# ── Module-level helpers ─────────────────────────────────────────────
class TestModuleHelpers:
    def _sample_actions(self):
        return [
            ProviderAction("p1", ActionType.PROVISION_DATASET, "aws", {}),
            ProviderAction("g1", ActionType.GRANT_ACCESS, "aws", {}),
            ProviderAction("p2", ActionType.PROVISION_DATASET, "gcp", {}),
        ]

    def test_get_action_by_id_found(self):
        assert get_action_by_id(self._sample_actions(), "g1").action_id == "g1"

    def test_get_action_by_id_not_found(self):
        assert get_action_by_id(self._sample_actions(), "nope") is None

    def test_filter_by_provider(self):
        result = filter_actions_by_provider(self._sample_actions(), "aws")
        assert len(result) == 2

    def test_filter_by_type(self):
        result = filter_actions_by_type(self._sample_actions(), ActionType.PROVISION_DATASET)
        assert len(result) == 2
