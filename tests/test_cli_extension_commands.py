# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
