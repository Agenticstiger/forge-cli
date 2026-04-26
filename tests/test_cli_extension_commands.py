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

from __future__ import annotations

from fluid_build.cli import build_parser


def test_parser_registers_roadmap_memory_and_mcp_commands():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert "roadmap" in choices
    assert "memory" in choices
    assert "mcp" in choices


def test_parser_exposes_ai_models_and_forge_tier_controls():
    parser = build_parser()

    ai_args = parser.parse_args(["ai", "models", "--provider", "openai", "--json"])
    assert ai_args.cmd == "ai"
    assert ai_args.ai_action == "models"
    assert ai_args.provider == "openai"
    assert ai_args.json is True

    forge_args = parser.parse_args(
        [
            "forge",
            "--tiered",
            "--require-llm",
            "--llm-routing-model",
            "gpt-4.1-nano",
        ]
    )
    assert forge_args.cmd == "forge"
    assert forge_args.tiered is True
    assert forge_args.require_llm is True
    assert forge_args.llm_routing_model == "gpt-4.1-nano"


def test_generate_transformation_and_dbt_aliases_parse_to_same_runner():
    parser = build_parser()

    transformation_args = parser.parse_args(["generate", "transformation", "--list"])
    dbt_args = parser.parse_args(["generate", "dbt", "--list"])

    assert transformation_args.cmd == "generate"
    assert transformation_args.generate_sub == "speed-transformation"
    assert transformation_args.list_engines is True
    assert dbt_args.generate_sub == "speed-transformation"
    assert dbt_args.list_engines is True
