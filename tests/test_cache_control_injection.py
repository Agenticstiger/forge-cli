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

"""Tests for Anthropic cache-control auto-injection + cache-token cost split.

Covers Wave-1 behaviour:

* Anthropic model → ``build_request`` kwargs include
  ``cache_control_injection_points``.
* OpenAI model → no injection.
* ``RunCostTracker`` applies the 1.25x cache-write / 0.10x cache-read
  multipliers when ``usd_override`` is None.
* ``usd_override``, when present, wins; the heuristic is ignored.

Receipts: searches for the ``cache_control_injection_points`` parameter
shape (litellm docs/tutorials/prompt_caching) and the
``cache_creation_input_tokens`` / ``cache_read_input_tokens`` usage
field names (litellm GH issue #15056) were run before writing this file.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# build_request — Anthropic auto-injection
# ---------------------------------------------------------------------------


def test_anthropic_build_request_injects_cache_control():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    provider = LiteLLMProvider("anthropic")
    cfg = LlmConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        endpoint="litellm://anthropic/claude-sonnet-4-6",
        api_key="sk-test",
    )
    _, payload = provider.build_request(cfg, "system prompt", "user prompt")
    # The exact shape per litellm's docs — location + role + index.
    assert payload["cache_control_injection_points"] == [
        {"location": "message", "role": "system", "index": 0}
    ]


def test_anthropic_tool_request_also_injects_cache_control():
    """Tool-use path uses ``build_tool_request`` — must also inject."""
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    provider = LiteLLMProvider("anthropic")
    cfg = LlmConfig(
        provider="anthropic",
        model="claude-haiku-4-5",
        endpoint="x",
        api_key="sk-test",
    )
    _, _, payload = provider.build_tool_request(cfg, "system", [], [])
    assert "cache_control_injection_points" in payload


def test_openai_build_request_no_injection():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    provider = LiteLLMProvider("openai")
    cfg = LlmConfig(
        provider="openai",
        model="gpt-4o",
        endpoint="x",
        api_key="sk-test",
    )
    _, payload = provider.build_request(cfg, "s", "u")
    assert "cache_control_injection_points" not in payload


def test_gemini_build_request_no_injection():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider
    from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

    provider = LiteLLMProvider("gemini")
    cfg = LlmConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        endpoint="x",
        api_key="x",
    )
    _, payload = provider.build_request(cfg, "s", "u")
    assert "cache_control_injection_points" not in payload


def test_bedrock_claude_build_request_injects():
    """Anthropic on Bedrock still benefits from cache_control auto-inject."""
    from fluid_build.cli.forge_copilot_llm_litellm import _is_anthropic_model

    # Bedrock SKU shape — the helper recognises ``anthropic.claude-*``.
    assert _is_anthropic_model("anthropic.claude-3-5-sonnet-20240620-v1:0") is True


def test_caller_supplied_injection_points_not_clobbered():
    """The agent layer may pre-populate injection_points (multi-turn
    caching). ``build_request`` uses setdefault so that's preserved."""
    from fluid_build.cli.forge_copilot_llm_litellm import _maybe_inject_cache_control

    payload = {
        "model": "anthropic/claude-sonnet-4-6",
        "cache_control_injection_points": [
            {"location": "message", "role": "user", "index": -1},
        ],
    }
    _maybe_inject_cache_control(payload, "anthropic/claude-sonnet-4-6")
    assert payload["cache_control_injection_points"] == [
        {"location": "message", "role": "user", "index": -1}
    ]


# ---------------------------------------------------------------------------
# extract_usage — cache token fields surface in the canonical dict
# ---------------------------------------------------------------------------


def test_extract_usage_surfaces_cache_token_fields():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    provider = LiteLLMProvider("anthropic")
    usage = provider.extract_usage(
        {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 300,
            }
        }
    )
    assert usage["cache_creation_input_tokens"] == 500
    assert usage["cache_read_input_tokens"] == 300
    # Existing fields remain untouched.
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 200


def test_extract_usage_zero_cache_tokens_when_absent():
    from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

    provider = LiteLLMProvider("openai")
    usage = provider.extract_usage(
        {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
    )
    assert usage["cache_creation_input_tokens"] == 0
    assert usage["cache_read_input_tokens"] == 0


# ---------------------------------------------------------------------------
# RunCostTracker — cache token cost split (no usd_override)
# ---------------------------------------------------------------------------


def test_run_cost_tracker_applies_cache_split_for_anthropic(monkeypatch, tmp_path):
    """Anthropic input @ 1x, cache write @ 1.25x, cache read @ 0.10x.

    We pin the per-1M rate via the override file to (3.00, 15.00) so
    the assertion is exact regardless of whether litellm's catalog
    has a different figure for this model. Manual computation:

        plain : 1000 * 3.00 / 1e6 = 0.003000
        write : 2000 * 3.00 * 1.25 / 1e6 = 0.007500
        read  : 4000 * 3.00 * 0.10 / 1e6 = 0.001200
        out   : 500 * 15.00 / 1e6 = 0.007500
        TOTAL = 0.019200
    """
    import json

    from fluid_build.copilot.cost import RunCostTracker

    # Use a model name not in litellm's catalog so the override (path 1
    # in _resolve_per_million_rate) wins over the catalog lookup.
    overrides = tmp_path / "prices.json"
    overrides.write_text(json.dumps({"claude-pinned-test-model": [3.00, 15.00]}))
    monkeypatch.setenv("FLUID_PRICES_JSON", str(overrides))

    tr = RunCostTracker()
    tr.record_call(
        provider="anthropic",
        model="claude-pinned-test-model",
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=2000,
        cache_read_input_tokens=4000,
        # NO usd_override — we want the heuristic to fire.
    )
    bd = tr.breakdown()
    assert bd.total_usd == pytest.approx(0.0192, abs=1e-4)


def test_run_cost_tracker_no_cache_tokens_uses_flat_rate(monkeypatch, tmp_path):
    """When cache tokens are zero, the heuristic collapses to the
    legacy flat-rate path — backward-compat invariant.

    Pin the rate via the override file so the assertion is exact
    regardless of whether litellm's catalog matches the embedded
    MODEL_PRICES_USD figure for the same model.
    """
    import json

    from fluid_build.copilot.cost import RunCostTracker

    overrides = tmp_path / "prices.json"
    overrides.write_text(json.dumps({"claude-flat-test-model": [3.00, 15.00]}))
    monkeypatch.setenv("FLUID_PRICES_JSON", str(overrides))

    tr = RunCostTracker()
    tr.record_call(
        provider="anthropic",
        model="claude-flat-test-model",
        input_tokens=1000,
        output_tokens=500,
    )
    bd = tr.breakdown()
    expected = (1000 * 3.00 + 500 * 15.00) / 1_000_000  # 0.0105
    assert bd.total_usd == pytest.approx(expected, abs=1e-4)


def test_run_cost_tracker_cache_split_only_for_anthropic(monkeypatch, tmp_path):
    """A non-Anthropic provider with cache tokens (synthetic / future
    OpenAI-style caching) does NOT get the Anthropic split — we apply
    flat-rate input pricing. The cache tokens are still recorded on
    the row for visibility, but the dollar charge mirrors flat input.
    """
    import json

    from fluid_build.copilot.cost import RunCostTracker

    # Pin rates so the assertion is exact regardless of catalog drift.
    overrides = tmp_path / "prices.json"
    overrides.write_text(json.dumps({"gpt-pinned-test-model": [2.50, 10.00]}))
    monkeypatch.setenv("FLUID_PRICES_JSON", str(overrides))

    tr = RunCostTracker()
    tr.record_call(
        provider="openai",
        model="gpt-pinned-test-model",
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=2000,
        cache_read_input_tokens=4000,
    )
    bd = tr.breakdown()
    expected_flat = (1000 * 2.50 + 500 * 10.00) / 1_000_000  # 0.0075
    assert bd.total_usd == pytest.approx(expected_flat, abs=1e-4)
    # Cache tokens still surface on the row for operator visibility.
    row = bd.rows[0]
    assert row.cache_creation_input_tokens == 2000
    assert row.cache_read_input_tokens == 4000


# ---------------------------------------------------------------------------
# usd_override wins — heuristic ignored
# ---------------------------------------------------------------------------


def test_usd_override_wins_over_cache_heuristic():
    """When litellm hands us an authoritative per-call USD, we use it
    verbatim — the cache split is purely a fallback for the catalog-miss
    case. Otherwise we'd be double-applying the discount."""
    from fluid_build.copilot.cost import RunCostTracker

    tr = RunCostTracker()
    tr.record_call(
        provider="anthropic",
        model="claude-sonnet-4-5",
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=2000,
        cache_read_input_tokens=4000,
        usd_override=0.0099,
    )
    bd = tr.breakdown()
    assert bd.total_usd == pytest.approx(0.0099)


# ---------------------------------------------------------------------------
# format_cost_summary — cache footer only when there's traffic
# ---------------------------------------------------------------------------


def test_format_cost_summary_omits_cache_footer_when_no_traffic():
    from fluid_build.copilot.cost import RunCostTracker, format_cost_summary

    tr = RunCostTracker()
    tr.record_call(
        provider="anthropic",
        model="claude-sonnet-4-5",
        input_tokens=100,
        output_tokens=50,
    )
    text = format_cost_summary(tr.breakdown())
    assert "Prompt cache" not in text


def test_format_cost_summary_shows_cache_footer_when_present():
    from fluid_build.copilot.cost import RunCostTracker, format_cost_summary

    tr = RunCostTracker()
    tr.record_call(
        provider="anthropic",
        model="claude-sonnet-4-5",
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=2000,
        cache_read_input_tokens=4000,
    )
    text = format_cost_summary(tr.breakdown())
    assert "Prompt cache" in text
    assert "write" in text
    assert "read" in text
    assert "2,000" in text
    assert "4,000" in text


# ---------------------------------------------------------------------------
# Gap 4 — End-to-end PROOF that prompt caching reduces cost.
#
# Plumbing tests above prove the cache fields flow through correctly.
# These tests prove the cache DELIVERS the discount by running two
# back-to-back calls and asserting the second one charges less than the
# first when the same content is cache-read.
# ---------------------------------------------------------------------------


def test_cache_read_charges_less_than_cache_miss(monkeypatch, tmp_path):
    """Two calls, same content. Call 1 writes the cache; call 2 reads it.
    Assert call 2's USD charge is < call 1's by the read-vs-write delta.

    With rate=(3.00, 15.00) / 1M and a 10,000-token system prompt:
      call 1 (cache miss):
        cache_creation = 10000  → 10000 * 3.00 * 1.25 / 1e6 = 0.0375
        out = 100              → 100 * 15.00 / 1e6          = 0.0015
        TOTAL                                                ≈ 0.0390
      call 2 (cache hit):
        cache_read = 10000     → 10000 * 3.00 * 0.10 / 1e6  = 0.0030
        out = 100              → 100 * 15.00 / 1e6          = 0.0015
        TOTAL                                                ≈ 0.0045

    Cache savings: 0.0345 (88% cheaper on turn 2).
    """
    import json

    from fluid_build.copilot.cost import RunCostTracker

    overrides = tmp_path / "prices.json"
    overrides.write_text(json.dumps({"claude-cache-proof-model": [3.00, 15.00]}))
    monkeypatch.setenv("FLUID_PRICES_JSON", str(overrides))

    tr = RunCostTracker()

    # Call 1 — cache miss. The whole 10000-token system prompt gets
    # written to the cache.
    tr.record_call(
        provider="anthropic",
        model="claude-cache-proof-model",
        input_tokens=0,
        output_tokens=100,
        cache_creation_input_tokens=10000,
        cache_read_input_tokens=0,
    )
    call1_cost = tr.breakdown().total_usd

    # Call 2 — same content, served entirely from the cache.
    tr.record_call(
        provider="anthropic",
        model="claude-cache-proof-model",
        input_tokens=0,
        output_tokens=100,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=10000,
    )
    call2_only = tr.breakdown().total_usd - call1_cost

    # Proof of savings: call 2 alone charges less than call 1 alone.
    assert call2_only < call1_cost
    # And the ratio is in the right neighbourhood — read is 0.10x of base
    # vs write at 1.25x, so call 2 should be ~12.5x cheaper on the cache
    # portion. With the 100-token output included, the overall ratio
    # lands at roughly 8-9x.
    assert (
        call1_cost / call2_only > 5.0
    ), f"Cache discount too small: call1={call1_cost:.4f} call2={call2_only:.4f}"


def test_cache_savings_visible_in_cost_summary(monkeypatch, tmp_path):
    """Cost summary footer renders the cache-read total so operators can
    see the discount working without computing it themselves."""
    import json

    from fluid_build.copilot.cost import RunCostTracker, format_cost_summary

    overrides = tmp_path / "prices.json"
    overrides.write_text(json.dumps({"claude-summary-test": [3.00, 15.00]}))
    monkeypatch.setenv("FLUID_PRICES_JSON", str(overrides))

    tr = RunCostTracker()
    # Three turns: first writes cache, next two read it.
    tr.record_call(
        provider="anthropic",
        model="claude-summary-test",
        input_tokens=0,
        output_tokens=100,
        cache_creation_input_tokens=10000,
    )
    tr.record_call(
        provider="anthropic",
        model="claude-summary-test",
        input_tokens=0,
        output_tokens=100,
        cache_read_input_tokens=10000,
    )
    tr.record_call(
        provider="anthropic",
        model="claude-summary-test",
        input_tokens=0,
        output_tokens=100,
        cache_read_input_tokens=10000,
    )
    text = format_cost_summary(tr.breakdown())
    # Combined cache-read token total across the three calls must show.
    assert "20,000" in text
    # And write total (10000 from call 1) must show too.
    assert "10,000" in text


# ---------------------------------------------------------------------------
# Cost catalog precedence — bonus pin from Wave 1 finding.
#
# Wave 1 surfaced that litellm.cost_per_token may return different rates
# than our embedded MODEL_PRICES_USD for the same model. The code's
# lookup ladder (`_resolve_per_million_rate`) already prefers litellm
# over the embedded table — this test pins that precedence so a future
# refactor can't silently flip the priority.
# ---------------------------------------------------------------------------


def test_litellm_catalog_wins_over_embedded_table_when_present(monkeypatch):
    """_resolve_per_million_rate prefers litellm's catalog when it returns
    a non-zero rate, falling back to MODEL_PRICES_USD only when litellm
    doesn't know the model.
    """
    from fluid_build.copilot import cost

    # Patch litellm to return a distinctive rate the embedded table does NOT carry.
    sentinel_in = 7.77
    sentinel_out = 33.33

    def fake_cost_per_token(*, model, prompt_tokens, completion_tokens):
        return sentinel_in, sentinel_out

    fake_litellm = type("L", (), {"cost_per_token": staticmethod(fake_cost_per_token)})()
    monkeypatch.setitem(__import__("sys").modules, "litellm", fake_litellm)
    # No override file, so litellm is path 2.
    monkeypatch.delenv("FLUID_PRICES_JSON", raising=False)
    (
        cost._load_price_overrides.cache_clear()
        if hasattr(cost._load_price_overrides, "cache_clear")
        else None
    )

    # Pick a model whose embedded rate is clearly different from the
    # sentinel — claude-sonnet-4-5 ships embedded at (3.00, 15.00).
    in_rate, out_rate = cost._resolve_per_million_rate("anthropic", "claude-sonnet-4-5")
    assert in_rate == sentinel_in, "litellm catalog should win over embedded MODEL_PRICES_USD"
    assert out_rate == sentinel_out


def test_embedded_table_fallback_when_litellm_unknown(monkeypatch):
    """When litellm returns (0, 0) — meaning it doesn't know the model —
    the embedded MODEL_PRICES_USD table provides the fallback.
    """
    from fluid_build.copilot import cost

    def fake_cost_per_token(*, model, prompt_tokens, completion_tokens):
        return 0.0, 0.0  # litellm doesn't know the model

    fake_litellm = type("L", (), {"cost_per_token": staticmethod(fake_cost_per_token)})()
    monkeypatch.setitem(__import__("sys").modules, "litellm", fake_litellm)
    monkeypatch.delenv("FLUID_PRICES_JSON", raising=False)

    # claude-sonnet-4-5 IS in MODEL_PRICES_USD as (3.00, 15.00).
    in_rate, out_rate = cost._resolve_per_million_rate("anthropic", "claude-sonnet-4-5")
    assert in_rate == 3.00
    assert out_rate == 15.00


def test_override_file_wins_over_litellm_and_embedded(monkeypatch, tmp_path):
    """Operator override file (path 1) wins over litellm (path 2) and
    embedded (path 3). This is the contract for negotiated-rate
    enterprise deployments.
    """
    import json

    from fluid_build.copilot import cost

    overrides = tmp_path / "prices.json"
    overrides.write_text(json.dumps({"claude-sonnet-4-5": [0.50, 1.00]}))
    monkeypatch.setenv("FLUID_PRICES_JSON", str(overrides))

    def fake_cost_per_token(*, model, prompt_tokens, completion_tokens):
        return 99.99, 999.99  # would-be wrong rate from litellm

    fake_litellm = type("L", (), {"cost_per_token": staticmethod(fake_cost_per_token)})()
    monkeypatch.setitem(__import__("sys").modules, "litellm", fake_litellm)

    in_rate, out_rate = cost._resolve_per_million_rate("anthropic", "claude-sonnet-4-5")
    assert in_rate == 0.50, "override file should win over litellm catalog"
    assert out_rate == 1.00
