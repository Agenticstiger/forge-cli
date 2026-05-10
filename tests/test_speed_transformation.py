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

"""Tests for ``fluid generate speed-transformation`` feature set.

Covers the five change points described in the plan file at
``/Users/A200004702/.claude/plans/here-is-the-setup-breezy-bunny.md``:

1. Bootstrap interview — ``data_modeling_technique`` default + alias
   normalisation.
2. CLI rename — ``speed-transformation`` routes + legacy alias emits a
   deprecation line.
3. Prompt injection — ``build_user_prompt`` surfaces technique +
   guidance; ``build_system_prompt`` carries the mandate clause.
4. Validation guardrail — ``validate_generated_result`` flags a DV2
   context whose ``additional_files`` contain only dimensional models.
5. Engine threading — ``TransformationIntent.data_modeling_technique``
   drives hub/link/satellite vs stg/fct/dim skeleton layout.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
import yaml

# ---------------------------------------------------------------------------
# 1. Interview bootstrap / normalisation
# ---------------------------------------------------------------------------


class TestInterviewTechnique:
    def test_default_applied_when_console_is_none(self):
        from fluid_build.cli.forge_copilot_discovery import DiscoveryReport
        from fluid_build.cli.forge_copilot_interview import bootstrap_interview_state

        state = bootstrap_interview_state(
            {}, discovery_report=DiscoveryReport(workspace_roots=["/tmp"])
        )
        assert state.normalized_context["data_modeling_technique"] == "data_vault_2"
        assert state.field_sources["data_modeling_technique"] == "default"

    def test_explicit_answer_beats_default(self):
        from fluid_build.cli.forge_copilot_discovery import DiscoveryReport
        from fluid_build.cli.forge_copilot_interview import bootstrap_interview_state

        state = bootstrap_interview_state(
            {"data_modeling_technique": "dimensional"},
            discovery_report=DiscoveryReport(workspace_roots=["/tmp"]),
        )
        assert state.normalized_context["data_modeling_technique"] == "dimensional"
        # Source should be whatever the initial context supplied, not
        # ``default`` (default only wins when no answer exists).
        assert state.field_sources["data_modeling_technique"] != "default"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("dv2", "data_vault_2"),
            ("DV 2.0", "data_vault_2"),
            ("data vault 2.0", "data_vault_2"),
            ("datavault", "data_vault_2"),
            ("data_vault_2", "data_vault_2"),
            ("Dimensional", "dimensional"),
            ("kimball", "dimensional"),
            ("star schema", "dimensional"),
            ("", None),
            ("anchor modeling", None),
        ],
    )
    def test_alias_normalization(self, raw, expected):
        from fluid_build.cli.forge_copilot_interview import normalize_interview_value

        assert normalize_interview_value("data_modeling_technique", raw) == expected


# ---------------------------------------------------------------------------
# 2. CLI rename — transformation/dbt aliases route to the same generator.
# ---------------------------------------------------------------------------


class TestCliRename:
    def test_transformation_aliases_registered(self):
        import argparse

        from fluid_build.cli import generate_speed_transformation

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="generate_sub")
        generate_speed_transformation.register_subcommand(sub)
        choices = sub.choices or {}
        assert "speed-transformation" in choices
        assert "transformation" in choices
        assert "dbt" in choices

    def test_speed_transformation_runs(self, capsys):
        from fluid_build.cli import generate_speed_transformation

        args = SimpleNamespace(
            generate_sub="speed-transformation",
            list_engines=True,
            verbose=False,
            contract=None,
            output=None,
            overwrite=False,
            env=None,
            build_index=0,
        )
        rc = generate_speed_transformation.run(args, logger=None)
        assert rc == 0
        captured = capsys.readouterr()
        # No deprecation line — the legacy name does not exist.
        assert "deprecated" not in captured.out


# ---------------------------------------------------------------------------
# 3. Prompt injection
# ---------------------------------------------------------------------------


class TestPromptInjection:
    def test_user_prompt_carries_technique_and_guidance(self, tmp_path):
        import json

        from fluid_build.cli.forge_copilot_discovery import DiscoveryReport
        from fluid_build.cli.forge_copilot_runtime import build_user_prompt

        prompt = build_user_prompt(
            context={
                "project_goal": "silver aggregate",
                "data_modeling_technique": "data_vault_2",
            },
            discovery_report=DiscoveryReport(workspace_roots=[str(tmp_path)]),
            capability_matrix={"providers": ["snowflake"], "templates": {"starter": {}}},
            seed_contract={"fluidVersion": "0.7.2", "id": "x.v1", "builds": [], "exposes": []},
            seed_template="starter",
            seed_provider="snowflake",
            attempt_index=1,
            previous_errors=[],
            previous_payload=None,
        )
        payload = json.loads(prompt)
        assert payload["data_modeling_technique"] == "data_vault_2"
        guidance = payload["data_modeling_guidance"]
        assert guidance["naming_conventions"]["hub"].startswith("hub_")
        assert guidance["naming_conventions"]["satellite"].startswith("sat_")
        assert guidance.get("insert_only") is True

    def test_user_prompt_omits_technique_when_unknown(self, tmp_path):
        import json

        from fluid_build.cli.forge_copilot_discovery import DiscoveryReport
        from fluid_build.cli.forge_copilot_runtime import build_user_prompt

        prompt = build_user_prompt(
            context={
                "project_goal": "x",
                "data_modeling_technique": "anchor_modeling",  # not in map
            },
            discovery_report=DiscoveryReport(workspace_roots=[str(tmp_path)]),
            capability_matrix={"providers": ["local"], "templates": {"starter": {}}},
            seed_contract={"fluidVersion": "0.7.2", "id": "x.v1", "builds": [], "exposes": []},
            seed_template="starter",
            seed_provider="local",
            attempt_index=1,
            previous_errors=[],
            previous_payload=None,
        )
        payload = json.loads(prompt)
        assert "data_modeling_technique" not in payload
        assert "data_modeling_guidance" not in payload

    def test_system_prompt_has_mandate_clause(self):
        from fluid_build.cli.forge_copilot_runtime import build_system_prompt

        prompt = build_system_prompt(
            {"providers": ["snowflake"], "build_engines": ["dbt"]},
        )
        assert "MODELING TECHNIQUE MANDATE" in prompt
        assert "data_modeling_technique" in prompt
        assert "hub_/lnk_/sat_" in prompt or "hub_" in prompt


# ---------------------------------------------------------------------------
# 4. Validation guardrail + repair guidance
# ---------------------------------------------------------------------------


def _fake_schema_manager():
    class _Fake:
        def __init__(self, logger=None):
            pass

        def validate_contract(self, contract, **_kw):
            return SimpleNamespace(errors=[], warnings=[], is_valid=True)

    return _Fake


def _minimal_normalized(additional_files: Dict[str, str]) -> Dict[str, Any]:
    return {
        "contract": {
            "fluidVersion": "0.7.2",
            "id": "silver.x_v1",
            "builds": [
                {
                    "id": "b1",
                    "engine": "dbt",
                    "pattern": "hybrid-reference",
                    "properties": {"model": "mart"},
                    "execution": {"runtime": {"platform": "snowflake"}},
                }
            ],
            "exposes": [
                {
                    "exposeId": "mart",
                    "kind": "table",
                    "binding": {"platform": "snowflake"},
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                    "semantics": {
                        "entities": [{"name": "id", "type": "primary"}],
                        "measures": [{"name": "m", "agg": "count"}],
                        "dimensions": [{"name": "d", "type": "categorical"}],
                        "metrics": [{"name": "kpi", "type": "simple"}],
                    },
                }
            ],
        },
        "suggestions": {
            "recommended_template": "starter",
            "recommended_provider": "snowflake",
        },
        "additional_files": additional_files,
    }


class TestValidationGuardrail:
    def test_dv2_missing_hub_raises_error(self):
        from fluid_build.cli.forge_copilot_contract_helpers import validate_generated_result

        errors, _ = validate_generated_result(
            _minimal_normalized(
                {"dbt_project/models/marts/fct_mart.sql": "-- not DV2"},
            ),
            capabilities={"providers": ["snowflake"], "templates": {"starter": {}}},
            logger=None,
            schema_manager_cls=_fake_schema_manager(),
            resolve_provider_from_contract_fn=lambda c: ("snowflake", None),
            get_builds_fn=lambda c: c["builds"],
            context={"data_modeling_technique": "data_vault_2"},
        )
        technique_errors = [e for e in errors if "data_vault_2" in e]
        assert technique_errors, f"no technique error surfaced, got {errors}"
        assert "hub_" in technique_errors[0]

    def test_dimensional_missing_dim_raises_error(self):
        from fluid_build.cli.forge_copilot_contract_helpers import validate_generated_result

        errors, _ = validate_generated_result(
            _minimal_normalized(
                {"dbt_project/models/staging/hub_mart.sql": "-- DV2 when dim was asked"},
            ),
            capabilities={"providers": ["snowflake"], "templates": {"starter": {}}},
            logger=None,
            schema_manager_cls=_fake_schema_manager(),
            resolve_provider_from_contract_fn=lambda c: ("snowflake", None),
            get_builds_fn=lambda c: c["builds"],
            context={"data_modeling_technique": "dimensional"},
        )
        technique_errors = [e for e in errors if "dimensional" in e]
        assert technique_errors
        assert "fct_" in technique_errors[0] or "dim_" in technique_errors[0]

    def test_dv2_with_hub_passes(self):
        from fluid_build.cli.forge_copilot_contract_helpers import validate_generated_result

        errors, _ = validate_generated_result(
            _minimal_normalized(
                {
                    "dbt_project/models/staging/hub_mart.sql": "-- hub",
                    "dbt_project/models/staging/sat_mart_raw.sql": "-- sat",
                },
            ),
            capabilities={"providers": ["snowflake"], "templates": {"starter": {}}},
            logger=None,
            schema_manager_cls=_fake_schema_manager(),
            resolve_provider_from_contract_fn=lambda c: ("snowflake", None),
            get_builds_fn=lambda c: c["builds"],
            context={"data_modeling_technique": "data_vault_2"},
        )
        assert not any("data_modeling_technique" in e for e in errors)

    def test_no_additional_files_skips_check(self):
        """Empty additional_files means engine fallback handles it — no error."""
        from fluid_build.cli.forge_copilot_contract_helpers import validate_generated_result

        errors, _ = validate_generated_result(
            _minimal_normalized({}),
            capabilities={"providers": ["snowflake"], "templates": {"starter": {}}},
            logger=None,
            schema_manager_cls=_fake_schema_manager(),
            resolve_provider_from_contract_fn=lambda c: ("snowflake", None),
            get_builds_fn=lambda c: c["builds"],
            context={"data_modeling_technique": "data_vault_2"},
        )
        assert not any("data_modeling_technique" in e for e in errors)

    def test_repair_feedback_carries_technique_hint(self):
        from fluid_build.cli.forge_copilot_contract_helpers import (
            build_structured_repair_feedback,
        )

        fb = build_structured_repair_feedback(
            [
                "data_modeling_technique=data_vault_2 requires at least one "
                "hub_/sat_/lnk_ model in additional_files — none found."
            ]
        )
        assert fb[0]["category"] == "modeling_technique_mismatch"
        assert "hub_" in fb[0]["fix_hint"]
        assert "sat_" in fb[0]["fix_hint"]

    def test_llm_sources_in_schema_yml_is_flagged(self):
        """LLM `sources:` in additional_files/schema.yml collides with engine."""
        from fluid_build.cli.forge_copilot_contract_helpers import validate_generated_result

        bad = _minimal_normalized(
            {
                "dbt_project/models/staging/hub_mart.sql": "-- real hub",
                "dbt_project/models/schema.yml": (
                    "version: 2\n"
                    "sources:\n"
                    "  - name: raw\n"
                    "    tables:\n"
                    "      - name: party_source\n"
                    "models:\n"
                    "  - name: hub_mart\n"
                ),
            }
        )
        errors, _ = validate_generated_result(
            bad,
            capabilities={"providers": ["snowflake"], "templates": {"starter": {}}},
            logger=None,
            schema_manager_cls=_fake_schema_manager(),
            resolve_provider_from_contract_fn=lambda c: ("snowflake", None),
            get_builds_fn=lambda c: c["builds"],
            context={"data_modeling_technique": "data_vault_2"},
        )
        sources_errs = [e for e in errors if "declares `sources:`" in e]
        assert sources_errs, f"expected sources collision error, got {errors}"
        assert "schema.yml" in sources_errs[0]

    def test_sources_collision_routes_to_repair_hint(self):
        from fluid_build.cli.forge_copilot_contract_helpers import (
            build_structured_repair_feedback,
        )

        fb = build_structured_repair_feedback(
            [
                "LLM-shipped 'dbt_project/models/schema.yml' declares `sources:` — "
                "that key is reserved for the engine-generated models/sources.yml. "
                "Remove the `sources:` block; keep only `models:`."
            ]
        )
        assert fb[0]["category"] == "engine_owned_file_collision"
        assert "sources:" in fb[0]["fix_hint"] or "sources.yml" in fb[0]["fix_hint"]


# ---------------------------------------------------------------------------
# 5. Engine threading (TransformationIntent + dbt skeleton shape)
# ---------------------------------------------------------------------------


class TestEngineThreading:
    def test_transformation_intent_accepts_technique(self):
        from fluid_build.engines.base import TransformationIntent

        intent = TransformationIntent(data_modeling_technique="data_vault_2")
        assert intent.data_modeling_technique == "data_vault_2"

    def test_dbt_dv2_without_stages_falls_back_to_compile_safe_models(self):
        """DV2 technique without a sidecar plan still emits non-empty dbt SQL."""
        from fluid_build.engines.base import TransformationIntent
        from fluid_build.engines.dbt.models import generate_models

        contract = {
            "fluidVersion": "0.7.2",
            "id": "silver.x_v1",
            "consumes": [
                {"exposeId": "party_source", "productId": "bronze.party_v1"},
                {"exposeId": "account_source", "productId": "bronze.party_v1"},
            ],
            "builds": [
                {
                    "id": "b1",
                    "engine": "dbt",
                    "pattern": "hybrid-reference",
                    "properties": {"model": "mart"},
                    "execution": {"runtime": {"platform": "snowflake"}},
                }
            ],
            "exposes": [
                {
                    "exposeId": "mart",
                    "kind": "table",
                    "binding": {"platform": "snowflake"},
                    "contract": {
                        "schema": [
                            {"name": "subscriber_id", "type": "string", "required": True},
                            {"name": "total", "type": "number"},
                        ]
                    },
                }
            ],
        }
        intent = TransformationIntent(data_modeling_technique="data_vault_2")
        files = generate_models(contract, contract["builds"][0], transformation_intent=intent)
        names = sorted(files.keys())
        assert "models/staging/stg_party_source.sql" in names
        assert "models/staging/stg_account_source.sql" in names
        assert "models/marts/mart.sql" in names

    def test_dbt_dimensional_without_stages_falls_back_to_compile_safe_models(self):
        """Dimensional technique without a sidecar plan still emits non-empty dbt SQL."""
        from fluid_build.engines.base import TransformationIntent
        from fluid_build.engines.dbt.models import generate_models

        contract = {
            "id": "silver.x_v1",
            "consumes": [{"exposeId": "party_source", "productId": "bronze.party_v1"}],
            "builds": [
                {
                    "id": "b1",
                    "engine": "dbt",
                    "pattern": "hybrid-reference",
                    "properties": {"model": "mart"},
                    "execution": {"runtime": {"platform": "snowflake"}},
                }
            ],
            "exposes": [
                {
                    "exposeId": "mart",
                    "kind": "table",
                    "binding": {"platform": "snowflake"},
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
        }
        intent = TransformationIntent(data_modeling_technique="dimensional")
        files = generate_models(contract, contract["builds"][0], transformation_intent=intent)
        names = sorted(files.keys())
        assert "models/staging/stg_party_source.sql" in names
        assert "models/marts/mart.sql" in names

    def test_dbt_engine_skips_schema_yml_when_technique_set(self):
        """Engine must skip marts/schema.yml emission — LLM ships its own."""
        from fluid_build.engines import get_engine
        from fluid_build.engines.base import TransformationIntent

        contract = {
            "fluidVersion": "0.7.2",
            "id": "silver.x_v1",
            "consumes": [{"exposeId": "party_source", "productId": "bronze.party_v1"}],
            "builds": [
                {
                    "id": "b1",
                    "engine": "dbt",
                    "pattern": "hybrid-reference",
                    "properties": {"model": "mart"},
                    "execution": {"runtime": {"platform": "snowflake"}},
                }
            ],
            "exposes": [
                {
                    "exposeId": "mart",
                    "kind": "table",
                    "binding": {"platform": "snowflake"},
                    "contract": {
                        "schema": [{"name": "id", "type": "string", "required": True}],
                        "dq": {"rules": [{"id": "r1", "type": "uniqueness", "selector": "id"}]},
                    },
                }
            ],
        }
        engine = get_engine("dbt")
        intent = TransformationIntent(data_modeling_technique="data_vault_2")
        files = engine.generate(contract, contract["builds"][0], transformation_intent=intent)
        # Engine still emits infrastructure; it must NOT emit model-level
        # schema.yml files that would collide with the LLM's own.
        schema_yml_files = [p for p in files if "schema.yml" in p]
        assert schema_yml_files == [], (
            f"engine must skip schema.yml when technique is set; got {schema_yml_files}"
        )
        # Infrastructure must still be there.
        assert "dbt_project.yml" in files
        assert "profiles.yml" in files

    def test_dbt_unset_technique_falls_back_to_default_dimensional(self):
        from fluid_build.engines.dbt.models import generate_models

        contract = {
            "id": "x.v1",
            "consumes": [{"exposeId": "src", "productId": "u.v1"}],
            "builds": [
                {
                    "id": "b",
                    "engine": "dbt",
                    "pattern": "hybrid-reference",
                    "properties": {"model": "m"},
                    "execution": {"runtime": {"platform": "local"}},
                }
            ],
            "exposes": [
                {
                    "exposeId": "m",
                    "kind": "table",
                    "binding": {"platform": "local"},
                    "contract": {"schema": [{"name": "id", "type": "string"}]},
                }
            ],
        }
        files = generate_models(contract, contract["builds"][0], transformation_intent=None)
        names = sorted(files.keys())
        # No intent at all → stg / mart classic path.
        assert "models/staging/stg_src.sql" in names
        assert "models/marts/m.sql" in names


# ---------------------------------------------------------------------------
# 6. Agent-spec file shape + enrichment wiring
# ---------------------------------------------------------------------------


class TestAgentSpec:
    def test_modeling_techniques_yaml_ships_both_techniques(self):
        from fluid_build.cli.forge_agent_specs import AGENT_SPECS_DIR

        path = Path(AGENT_SPECS_DIR) / "modeling_techniques.yaml"
        assert path.exists(), "modeling_techniques.yaml must ship with the package"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "data_vault_2" in raw
        assert "dimensional" in raw
        dv2 = raw["data_vault_2"]
        assert dv2.get("insert_only") is True
        assert "hub" in dv2["naming_conventions"]
        dim = raw["dimensional"]
        assert "dimension" in dim["naming_conventions"]
        assert "fact" in dim["naming_conventions"]
