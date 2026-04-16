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

"""Tests for adaptive forge copilot interview orchestration."""

from types import SimpleNamespace
from unittest.mock import patch

from fluid_build.cli.forge_copilot_interview import (
    CopilotInterviewState,
    InterviewDecision,
    InterviewQuestion,
    ask_interview_question,
    bootstrap_interview_state,
    resolve_choice_input,
    run_adaptive_copilot_interview,
    run_post_generation_clarification,
)
from fluid_build.cli.forge_copilot_runtime import DiscoveryReport, LlmConfig


def _llm_config() -> LlmConfig:
    return LlmConfig(
        provider="openai",
        model="gpt-4o-mini",
        endpoint="https://api.openai.com/v1/chat/completions",
        api_key="test-key",
    )


class FakeConsole:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.index = 0

    def input(self, _prompt=""):
        if self.index >= len(self.answers):
            return ""
        answer = self.answers[self.index]
        self.index += 1
        return answer

    def print(self, *_args, **_kwargs):
        return None


class TestAdaptiveCopilotInterview:
    @patch(
        "fluid_build.cli.forge_copilot_interview.request_interview_decision",
        return_value=InterviewDecision(status="ready", reason="Enough context gathered."),
    )
    def test_bootstrap_collects_missing_project_goal_and_sources(self, _mock_decision):
        console = FakeConsole(["Orders dashboard", "Postgres tables"])
        state = run_adaptive_copilot_interview(
            initial_context={},
            console=console,
            llm_config=_llm_config(),
            discovery_report=DiscoveryReport(workspace_roots=["/tmp/workspace"]),
            capability_matrix={"providers": ["local"], "templates": {"starter": {}}},
        )

        assert state.normalized_context["project_goal"] == "Orders dashboard"
        assert state.normalized_context["data_sources"] == "Postgres tables"
        assert state.normalized_context["interview_summary"]["project_goal"] == "Orders dashboard"

    @patch(
        "fluid_build.cli.forge_copilot_interview.request_interview_decision",
        return_value=InterviewDecision(
            status="ready", reason="Discovery already provided enough context."
        ),
    )
    def test_no_question_fast_path_when_context_and_discovery_are_sufficient(self, _mock_decision):
        console = FakeConsole([])
        state = run_adaptive_copilot_interview(
            initial_context={
                "project_goal": "Streaming events",
                "data_sources": "Kafka topics",
                "use_case": "streaming",
            },
            console=console,
            llm_config=_llm_config(),
            discovery_report=DiscoveryReport(
                workspace_roots=["/tmp/workspace"],
                provider_hints=["aws"],
            ),
            capability_matrix={"providers": ["aws"], "templates": {"streaming": {}}},
        )

        assert state.normalized_context["project_goal"] == "Streaming events"
        assert state.normalized_context["provider"] == "aws"
        assert console.index == 0

    @patch(
        "fluid_build.cli.forge_copilot_interview.request_interview_decision",
        side_effect=[
            InterviewDecision(
                status="ask",
                reason="A couple of semantic details will help.",
                questions=[
                    InterviewQuestion(
                        id="use_case",
                        field="use_case",
                        prompt="Pick the closest use case",
                        type="choice",
                        choices=[
                            {"label": "Analytics & BI", "value": "analytics"},
                            {"label": "Streaming / Real-time", "value": "streaming"},
                        ],
                    ),
                    InterviewQuestion(
                        id="output_kind",
                        field="output_kind",
                        prompt="What should the expose look like?",
                        type="choice",
                        choices=[{"label": "table", "value": "table"}],
                    ),
                    InterviewQuestion(
                        id="primary_entity",
                        field="primary_entity",
                        prompt="What is the primary entity?",
                    ),
                ],
            ),
            InterviewDecision(status="ready", reason="That is enough."),
        ],
    )
    def test_interview_caps_round_questions_at_two(self, _mock_decision):
        # Bootstrap asks up to 3 optional questions (byot, engine, schedule)
        # before the LLM-driven dynamic round.  Provide empty strings to
        # skip those, then the real answers for the dynamic questions.
        console = FakeConsole(["", "", "", "dashboards", "table"])
        state = run_adaptive_copilot_interview(
            initial_context={
                "project_goal": "Orders analytics",
                "data_sources": "warehouse tables",
            },
            console=console,
            llm_config=_llm_config(),
            discovery_report=DiscoveryReport(workspace_roots=["/tmp/workspace"]),
            capability_matrix={"providers": ["local"], "templates": {"analytics": {}}},
        )

        assert state.normalized_context["use_case"] == "analytics"
        assert state.normalized_context["output_kind"] == "table"
        assert "primary_entity" not in state.normalized_context
        # Filter for dynamic-question user turns (skip bootstrap turns)
        user_turns = [
            turn
            for turn in state.transcript
            if turn["role"] == "user"
            and turn.get("question_id", "").startswith(
                ("use_case", "output_kind", "primary_entity")
            )
        ]
        assert user_turns[0]["raw_input"] == "dashboards"
        assert user_turns[0]["resolved_value"] == "analytics"

    def test_bootstrap_precedence_keeps_explicit_values_over_discovery_and_memory(self):
        state = bootstrap_interview_state(
            {
                "project_goal": "Risk model",
                "provider": "snowflake",
                "canonical_model": "hl7 fhir",
            },
            discovery_report=DiscoveryReport(
                workspace_roots=["/tmp/workspace"],
                provider_hints=["gcp"],
            ),
            project_memory=SimpleNamespace(
                preferred_provider="local",
                preferred_domain="analytics",
                preferred_owner="data-team",
                build_engines=["sql"],
            ),
        )

        assert state.normalized_context["provider"] == "snowflake"
        assert state.normalized_context["domain"] == "analytics"
        assert state.normalized_context["canonical_model"] == "hl7_fhir"

    @patch(
        "fluid_build.cli.forge_copilot_interview.request_interview_decision",
        return_value=InterviewDecision(
            status="ask",
            reason="One metric detail is still missing.",
            questions=[
                InterviewQuestion(
                    id="primary_measures",
                    field="primary_measures",
                    prompt="Which measure matters most?",
                )
            ],
        ),
    )
    def test_post_generation_clarification_updates_semantic_intent(self, _mock_decision):
        state = CopilotInterviewState(
            normalized_context={
                "project_goal": "Orders dashboard",
                "data_sources": "warehouse tables",
            }
        )
        console = FakeConsole(["revenue"])

        updated = run_post_generation_clarification(
            state,
            console=console,
            llm_config=_llm_config(),
            discovery_report=DiscoveryReport(workspace_roots=["/tmp/workspace"]),
            capability_matrix={"providers": ["local"], "templates": {"analytics": {}}},
            failure_summary=["Expose 'orders_output' semantics must include at least one measure."],
        )

        assert updated.normalized_context["primary_measures"] == ["revenue"]
        assert updated.normalized_context["interview_summary"]["semantic_intent"][
            "primary_measures"
        ] == ["revenue"]

    def test_interview_summary_infers_healthcare_modeling_defaults(self):
        state = CopilotInterviewState(
            normalized_context={
                "project_goal": "Population health dashboard",
                "data_sources": "claims and clinical warehouse",
                "domain": "healthcare",
                "use_case": "analytics",
            }
        )

        final_context = state.finalize()

        assert final_context["canonical_model"] == "omop_cdm"
        assert final_context["interview_summary"]["canonical_model"] == "omop_cdm"

    def test_interview_summary_infers_retail_digital_modeling_defaults(self):
        state = CopilotInterviewState(
            normalized_context={
                "project_goal": "Retail digital journey analytics",
                "data_sources": "web sdk profile events",
                "domain": "retail",
            }
        )

        final_context = state.finalize()

        assert final_context["canonical_model"] == "adobe_xdm"
        assert final_context["interview_summary"]["canonical_model"] == "adobe_xdm"

    @patch(
        "fluid_build.cli.forge_copilot_interview.request_interview_decision",
        side_effect=[
            InterviewDecision(
                status="ask",
                reason="A build hint would help.",
                questions=[
                    InterviewQuestion(
                        id="build_engine",
                        field="build_engine",
                        prompt="Which build engine fits best?",
                        type="choice",
                        choices=[
                            {"label": "SQL", "value": "sql"},
                            {"label": "Python", "value": "python"},
                        ],
                    )
                ],
            ),
            InterviewDecision(status="ready", reason="Proceeding with the raw context we have."),
        ],
    )
    def test_unresolved_structured_input_is_preserved_in_transcript(self, _mock_decision):
        # Bootstrap asks up to 3 optional questions (byot, engine, schedule)
        # before the LLM-driven dynamic round.  Provide empty strings to
        # skip those, then the real answer for the dynamic question.
        console = FakeConsole(["", "", "", "spark jobs"])
        state = run_adaptive_copilot_interview(
            initial_context={
                "project_goal": "Orders analytics",
                "data_sources": "warehouse tables",
            },
            console=console,
            llm_config=_llm_config(),
            discovery_report=DiscoveryReport(workspace_roots=["/tmp/workspace"]),
            capability_matrix={"providers": ["local"], "templates": {"analytics": {}}},
        )

        # The dynamic build_engine question offers SQL/Python but user typed
        # "spark jobs" which doesn't match — it should be unresolved.
        # Note: bootstrap may set a default engine; we check the dynamic
        # transcript turn specifically.
        dynamic_turns = [
            turn
            for turn in state.transcript
            if turn["role"] == "user" and turn.get("question_id") == "build_engine"
        ]
        assert len(dynamic_turns) == 1
        assert dynamic_turns[0]["raw_input"] == "spark jobs"
        assert dynamic_turns[0]["resolution_status"] == "unresolved"
        assert dynamic_turns[0]["resolved_value"] is None


class TestFriendlyChoiceResolution:
    def test_resolve_choice_supports_numeric_selection(self):
        result = resolve_choice_input(
            field_name="use_case",
            raw_input="2",
            choices=[
                {"label": "Analytics & BI", "value": "analytics"},
                {"label": "Streaming / Real-time", "value": "streaming"},
            ],
            allow_skip=True,
        )

        assert result.status == "matched"
        assert result.value == "streaming"

    def test_resolve_choice_supports_partial_and_alias_matching(self):
        result = resolve_choice_input(
            field_name="use_case",
            raw_input="cdc sync",
            choices=[
                {"label": "Analytics & BI", "value": "analytics"},
                {"label": "ETL / Data Pipelines", "value": "etl_pipeline"},
                {"label": "Streaming / Real-time", "value": "streaming"},
            ],
            allow_skip=True,
        )

        assert result.status == "matched"
        assert result.value == "etl_pipeline"

    def test_resolve_choice_supports_skip_phrases(self):
        result = resolve_choice_input(
            field_name="use_case",
            raw_input="not sure",
            choices=list(InterviewQuestion.from_payload({"field": "use_case"}).choices),
            allow_skip=True,
        )

        assert result.status == "skipped"

    def test_use_case_custom_phrase_becomes_other_with_follow_up_text(self):
        console = FakeConsole(["customer 360"])
        result = ask_interview_question(
            console,
            InterviewQuestion(
                id="use_case",
                field="use_case",
                prompt="What's your primary use case?",
                type="choice",
                choices=[
                    {"label": "Analytics & BI", "value": "analytics"},
                    {"label": "Other / Not sure", "value": "other"},
                ],
                required=True,
            ),
        )

        assert result.context_patch["use_case"] == "other"
        assert result.context_patch["use_case_other"] == "customer 360"

    def test_ambiguous_choice_gets_one_friendly_confirmation(self):
        console = FakeConsole(["analytic", "Analytics Engineering"])
        result = ask_interview_question(
            console,
            InterviewQuestion(
                id="shape",
                field="shape",
                prompt="Which analytics flavor is closer?",
                type="choice",
                choices=[
                    {"label": "Analytics & BI", "value": "analytics"},
                    {"label": "Analytics Engineering", "value": "analytics_engineering"},
                ],
                required=True,
            ),
        )

        assert result.value == "analytics_engineering"
        assert result.resolution_status == "matched"
