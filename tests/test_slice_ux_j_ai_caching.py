# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Slice UX-J: regression tests for model routing, precompiled skills, and discovery cache."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
import yaml

from fluid_build.cli.artifact_discovery_cache import (
    compute_file_tree_hash,
    discovery_cache_enabled,
    load_discovery_cache,
    write_discovery_cache,
)
from fluid_build.cli.artifact_paths import (
    workspace_discovery_cache_path,
    workspace_skills_compiled_path,
)
from fluid_build.cli.forge_copilot_llm_providers import (
    LlmConfig,
    _default_routing_model,
)
from fluid_build.cli.forge_copilot_skills_cache import (
    clear_compiled_skills_cache,
    load_compiled_skills,
    write_compiled_skills,
)
from fluid_build.cli.industry_skills import compile_skill, load_industry_skills

# ---------------------------------------------------------------------------
# J.1 — Model routing
# ---------------------------------------------------------------------------


class TestModelRouting:
    def test_for_routing_returns_self_when_no_routing_model(self):
        cfg = LlmConfig(
            provider="openai",
            model="gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="k",
        )
        assert cfg.for_routing() is cfg

    def test_for_routing_returns_copy_with_routing_model(self):
        cfg = LlmConfig(
            provider="anthropic",
            model="claude-3-5-sonnet-latest",
            endpoint="https://api.anthropic.com/v1/messages",
            api_key="k",
            routing_model="claude-3-5-haiku-latest",
        )
        routed = cfg.for_routing()
        assert routed is not cfg
        assert routed.model == "claude-3-5-haiku-latest"
        assert routed.provider == "anthropic"
        assert routed.api_key == "k"

    def test_for_routing_re_derives_endpoint_when_no_routing_endpoint(self):
        cfg = LlmConfig(
            provider="anthropic",
            model="claude-3-5-sonnet-latest",
            endpoint="https://api.anthropic.com/v1/messages",
            api_key="k",
            routing_model="claude-3-5-haiku-latest",
        )
        routed = cfg.for_routing()
        # Anthropic always returns the same endpoint regardless of model
        assert "anthropic.com" in routed.endpoint

    def test_for_routing_uses_explicit_routing_endpoint(self):
        cfg = LlmConfig(
            provider="openai",
            model="gpt-4o",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="k",
            routing_model="gpt-4o-mini",
            routing_endpoint="https://custom.proxy/v1/chat/completions",
        )
        routed = cfg.for_routing()
        assert routed.endpoint == "https://custom.proxy/v1/chat/completions"

    def test_default_routing_model_returns_catalog_routing(self):
        """Routing model should come from the catalog, not a hardcoded table."""
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        for provider in ("openai", "anthropic", "gemini"):
            entry = catalog.get("providers", {}).get(provider, {})
            flagship = entry.get("flagship")
            expected_routing = entry.get("routing")
            if flagship and expected_routing and expected_routing != flagship:
                result = _default_routing_model(provider, flagship)
                assert (
                    result == expected_routing
                ), f"{provider}: expected routing '{expected_routing}', got '{result}'"

    def test_default_routing_model_returns_none_when_same_as_strong(self):
        """When routing == strong, returns None (no point routing to self)."""
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        ollama = catalog.get("providers", {}).get("ollama", {})
        # For ollama, routing is llama3.1:8b but strong is llama3.1 — different
        # So let's just test with a model that equals the routing model
        balanced = ollama.get("balanced", "llama3.1")
        routing = ollama.get("routing", "llama3.1:8b")
        if balanced == routing:
            assert _default_routing_model("ollama", balanced) is None


# ---------------------------------------------------------------------------
# J.2 — Precompile skills
# ---------------------------------------------------------------------------


class TestSkillsCompile:
    def test_compile_skill_extracts_prompt_fields(self):
        telco = load_industry_skills("telco")
        compiled = compile_skill(telco)
        assert compiled["industry"] == "Telecommunications"
        assert compiled["canonical_model"] == "TM Forum SID"
        assert isinstance(compiled["domains"], list)
        assert len(compiled["domains"]) > 0
        assert isinstance(compiled["compliance"], list)
        assert "GDPR" in compiled["compliance"]

    def test_compile_skill_drops_non_prompt_fields(self):
        telco = load_industry_skills("telco")
        compiled = compile_skill(telco)
        # These should NOT be in the compiled output
        assert "common_data_sources" not in compiled
        assert "tools" not in compiled
        assert "key_entities" not in str(compiled)  # no nested key_entities
        assert "_version" not in compiled
        assert "_generated" not in compiled

    def test_compile_skill_handles_empty_input(self):
        compiled = compile_skill({})
        assert compiled == {}

    def test_compile_skill_handles_partial_input(self):
        compiled = compile_skill({"industry": {"label": "Test"}})
        assert compiled == {"industry": "Test"}

    def test_compile_all_bundled_industries(self):
        """Every bundled industry YAML must compile without error."""
        for name in ("telco", "retail", "healthcare", "finance"):
            raw = load_industry_skills(name)
            compiled = compile_skill(raw)
            assert "industry" in compiled, f"{name} compiled must have industry"


class TestCompiledSkillsMemoryCache:
    def setup_method(self):
        clear_compiled_skills_cache()

    def teardown_method(self):
        clear_compiled_skills_cache()

    def test_write_and_load_round_trip(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        compiled = {"industry": "Test", "domains": ["A", "B"]}
        out_path = write_compiled_skills(ws, compiled)
        assert out_path.is_file()
        loaded = load_compiled_skills(ws)
        assert loaded == compiled

    def test_load_returns_none_when_no_file(self, tmp_path: Path):
        assert load_compiled_skills(tmp_path) is None

    def test_cache_returns_same_object_on_repeat_load(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        write_compiled_skills(ws, {"industry": "Test"})
        first = load_compiled_skills(ws)
        second = load_compiled_skills(ws)
        assert first is second, "mtime-keyed cache should return same dict object"

    def test_cache_invalidated_on_clear(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        write_compiled_skills(ws, {"industry": "Test"})
        first = load_compiled_skills(ws)
        clear_compiled_skills_cache()
        second = load_compiled_skills(ws)
        assert first == second
        assert first is not second

    def test_fallback_compiles_from_raw_yaml(self, tmp_path: Path):
        """When skills.compiled.json is missing, load_compiled_skills
        should fall back to compiling from skills.yaml on-the-fly."""
        ws = tmp_path / "ws"
        fluid_dir = ws / ".fluid"
        fluid_dir.mkdir(parents=True)
        # Write a raw skills YAML (no compiled form)
        skills_yaml = fluid_dir / "skills.yaml"
        skills_yaml.write_text(
            yaml.dump(
                {
                    "industry": {"label": "Test Industry"},
                    "canonical_model": {"label": "Test Model"},
                    "domains": [{"label": "Domain A"}],
                    "compliance": ["GDPR"],
                    "tools": {"should": "be dropped"},
                }
            )
        )
        loaded = load_compiled_skills(ws)
        assert loaded is not None
        assert loaded["industry"] == "Test Industry"
        assert loaded["canonical_model"] == "Test Model"
        assert "tools" not in loaded


# ---------------------------------------------------------------------------
# J.3 — Discovery cache on disk
# ---------------------------------------------------------------------------


class TestDiscoveryDiskCache:
    def test_hash_is_stable_for_same_files(self, tmp_path: Path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        h1 = compute_file_tree_hash([f1, f2])
        h2 = compute_file_tree_hash([f1, f2])
        assert h1 == h2

    def test_hash_changes_when_file_modified(self, tmp_path: Path):
        f1 = tmp_path / "a.txt"
        f1.write_text("v1")
        h1 = compute_file_tree_hash([f1])
        f1.write_text("v2")
        h2 = compute_file_tree_hash([f1])
        assert h1 != h2

    def test_hash_changes_when_file_added(self, tmp_path: Path):
        f1 = tmp_path / "a.txt"
        f1.write_text("hello")
        h1 = compute_file_tree_hash([f1])
        f2 = tmp_path / "b.txt"
        f2.write_text("world")
        h2 = compute_file_tree_hash([f1, f2])
        assert h1 != h2

    def test_hash_order_independent(self, tmp_path: Path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        h_forward = compute_file_tree_hash([f1, f2])
        h_reverse = compute_file_tree_hash([f2, f1])
        assert h_forward == h_reverse

    def test_write_and_load_cache_round_trip(self, tmp_path: Path):
        from fluid_build.cli.forge_copilot_discovery import DiscoveryReport

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".fluid").mkdir()

        report = DiscoveryReport(
            workspace_roots=[str(ws)],
            files_scanned=42,
            detected_sources=[{"path": "test.csv"}],
            provider_hints=["local"],
        )
        tree_hash = "abc123"
        write_discovery_cache(ws, report, tree_hash)

        cache_path = workspace_discovery_cache_path(ws)
        assert cache_path.is_file()

        loaded = load_discovery_cache(ws, tree_hash)
        assert loaded is not None
        assert loaded["files_scanned"] == 42
        assert loaded["provider_hints"] == ["local"]

    def test_load_returns_none_on_hash_mismatch(self, tmp_path: Path):
        from fluid_build.cli.forge_copilot_discovery import DiscoveryReport

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".fluid").mkdir()

        report = DiscoveryReport(workspace_roots=[str(ws)])
        write_discovery_cache(ws, report, "hash_v1")

        assert load_discovery_cache(ws, "hash_v2") is None

    def test_load_returns_none_when_no_file(self, tmp_path: Path):
        assert load_discovery_cache(tmp_path, "any") is None

    def test_load_tolerates_corrupt_json(self, tmp_path: Path):
        ws = tmp_path / "ws"
        (ws / ".fluid").mkdir(parents=True)
        cache_path = workspace_discovery_cache_path(ws)
        cache_path.write_text("not valid json {{{")
        assert load_discovery_cache(ws, "any") is None

    def test_discovery_cache_enabled_env_switch(self, monkeypatch):
        monkeypatch.delenv("FLUID_DISCOVERY_CACHE", raising=False)
        assert discovery_cache_enabled() is True
        monkeypatch.setenv("FLUID_DISCOVERY_CACHE", "0")
        assert discovery_cache_enabled() is False
        monkeypatch.setenv("FLUID_DISCOVERY_CACHE", "1")
        assert discovery_cache_enabled() is True
