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

"""What works, what degrades and what fails when litellm is not installed.

litellm left the base install in 0.28.0. The litellm-backed providers fail up
front with the install line and the keyless alternatives; the keyless coding
agents, ``mcp-sampling`` and template/blank forge keep working; the places
that only ever used litellm opportunistically (price table, token counting,
deprecation hints) degrade to their built-in fallbacks.
"""

from __future__ import annotations

import json

import pytest

from fluid_build._optional import MissingOptionalDependency
from tests.test_optional_deps import _hint, block, cli  # noqa: F401  (fixtures)

pytestmark = pytest.mark.unit

_ALTERNATIVES = (
    "Or author without it: --llm-provider claude-code (uses your Claude Code login) "
    "or --template starter."
)


class TestProviderGate:
    @pytest.mark.parametrize("name", ["openai", "anthropic", "gemini", "ollama", "bedrock"])
    def test_litellm_backed_provider_fails_with_hint_and_alternatives(
        self, block, name
    ):  # noqa: F811
        block("litellm")
        from fluid_build.llm.providers import get_llm_provider

        with pytest.raises(MissingOptionalDependency) as info:
            get_llm_provider(name)
        assert str(info.value) == (
            _hint(f"AI-assisted authoring (--llm-provider {name})", "litellm", "litellm")
            + "\n\n"
            + _ALTERNATIVES
        )
        assert info.value.event == "copilot_litellm_unavailable"
        assert info.value.exit_code == 1

    @pytest.mark.parametrize("name", ["claude-code", "codex", "cursor", "kiro", "mcp-sampling"])
    def test_keyless_providers_resolve_without_litellm(self, block, name):  # noqa: F811
        block("litellm")
        from fluid_build.llm.providers import get_llm_provider

        assert get_llm_provider(name) is not None

    def test_resolve_llm_config_fails_before_any_key_check(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        from types import SimpleNamespace

        from fluid_build.llm.providers import resolve_llm_config

        with pytest.raises(MissingOptionalDependency):
            resolve_llm_config(SimpleNamespace(llm_provider="openai"))

    def test_keyless_config_resolves_without_litellm(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        from types import SimpleNamespace

        from fluid_build.llm.providers import resolve_llm_config

        cfg = resolve_llm_config(SimpleNamespace(llm_provider="claude-code"))
        assert cfg.provider == "claude-code"


class TestCliSurface:
    def test_ai_forge_exits_1_with_the_hint_and_no_traceback(
        self, block, cli, monkeypatch
    ):  # noqa: F811
        block("litellm")
        # The failure must precede the interview: nothing may load context or memory.
        import fluid_build.cli.forge_copilot_discovery as discovery
        import fluid_build.cli.forge_copilot_personal_memory as personal

        loaded: list[str] = []
        real_discover, real_memory = discovery.discover_local_context, personal.load_personal_memory
        monkeypatch.setattr(
            discovery,
            "discover_local_context",
            lambda *a, **k: loaded.append("context") or real_discover(*a, **k),
        )
        monkeypatch.setattr(
            personal,
            "load_personal_memory",
            lambda *a, **k: loaded.append("memory") or real_memory(*a, **k),
        )
        rc, out = cli("forge", "--llm-provider", "openai", "--non-interactive")
        flat = " ".join(out.split())
        assert rc == 1, out
        assert "pip install 'data-product-forge[litellm]'" in flat
        assert _ALTERNATIVES in flat
        assert "Traceback" not in out
        assert loaded == []

    def test_template_forge_works_without_litellm(self, block, cli, tmp_path):  # noqa: F811
        block("litellm")
        rc, out = cli(
            "forge",
            "--template",
            "starter",
            "--non-interactive",
            "--yes",
            "--target-dir",
            str(tmp_path / "proj"),
        )
        assert "Traceback" not in out, out
        assert "litellm" not in out.lower() or "pip install" not in out, out


class TestDegradations:
    def test_price_table_is_used_when_litellm_is_absent(self, block):  # noqa: F811
        block("litellm")
        from fluid_build.copilot import cost

        model = "claude-sonnet-4-6"
        in_price, out_price = cost.MODEL_PRICES_USD[model]
        got = cost._price_for("anthropic", model, 1_000_000, 2_000_000)
        assert got == pytest.approx(in_price + 2 * out_price)
        assert cost._resolve_per_million_rate("anthropic", model) == (in_price, out_price)

    def test_unknown_model_price_is_none_not_an_error(self, block):  # noqa: F811
        block("litellm")
        from fluid_build.copilot import cost

        assert cost._price_for("openai", "model-nobody-has-heard-of", 10, 10) is None

    def test_token_count_falls_back_to_the_char_heuristic(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        monkeypatch.delenv("FLUID_TOKEN_COUNTER", raising=False)
        from fluid_build.copilot.agents import token_budget as tb

        text = "select * from orders where id = 1" * 20
        assert tb.count_tokens(text, provider="openai", model="gpt-4o") == tb.estimate_tokens(text)
        assert tb._lookup_litellm_max_input_tokens("gpt-4o") is None

    def test_deprecation_hint_is_none_without_litellm(self, block):  # noqa: F811
        block("litellm")
        from fluid_build.copilot import models

        assert models._is_deprecated_in_litellm("gemini-1.5-flash") is False
        assert models.deprecation_hint("gemini-1.5-flash") is None
        assert models._suggest_replacement_via_litellm("gemini-1.5-flash") is None


class TestCodingAgentWithoutLitellm:
    def test_invoke_blocking_runs_with_litellm_blocked(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        from fluid_build.cli import forge_copilot_coding_agent as cap
        from fluid_build.llm.providers import LlmConfig, get_llm_provider

        structured = {"recommended_template": "starter", "contract": {"id": "x"}}
        stdout = json.dumps({"structured_output": structured, "total_cost_usd": 0.01})
        calls = []

        def fake_run(argv, *, stdin, timeout, cwd, env):
            calls.append(argv)
            return (0, stdout, "")

        monkeypatch.setattr(cap.shutil, "which", lambda b: f"/usr/local/bin/{b}")
        monkeypatch.setattr(cap, "_run_agent", fake_run)

        provider = get_llm_provider("claude-code")
        cfg = LlmConfig(
            provider="claude-code",
            model="claude-code",
            endpoint="coding-agent://claude-code",
            api_key=None,
        )
        result = provider.invoke_blocking(cfg, "SYS", "USER")
        assert json.loads(result) == structured
        assert len(calls) == 1


def test_readiness_is_not_ready_when_a_key_is_set_but_litellm_is_absent(block):  # noqa: F811
    block("litellm")
    from fluid_build.llm.providers import check_llm_readiness

    result = check_llm_readiness({"OPENAI_API_KEY": "sk-test1234567890abcdef"})
    assert result.ready is False
    assert result.auth_available is True
    assert "pip install 'data-product-forge[litellm]'" in (result.error or "")
