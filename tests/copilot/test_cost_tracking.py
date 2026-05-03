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

"""Coverage for V2.4.4 — per-run cost tracking (CLI-only).

After each ``fluid forge data-model`` invocation the user sees a
one-block panel naming every (provider, model) pair the run touched,
with token counts and USD cost. The plan promised this as a CLI-only
feature — no UI, no dashboard, just a summary in the terminal.

The tests below pin the four contracts:

1. **Per-(provider, model) accumulation.** Two calls against the
   same model land in one row, summed; two calls against different
   models produce two rows.
2. **Pricing.** Known models from :data:`MODEL_PRICES_USD` produce a
   numeric cost; unknown models surface as ``None`` (rendered as
   ``$?`` in the formatter) so an operator can see "I should update
   the price table" rather than getting a misleading $0.
3. **Ollama special case.** Local Ollama models always cost $0 — the
   provider-name match wins over the per-model lookup so any local
   Ollama model is handled even if it's not in the price table.
4. **Reset boundary.** ``reset_run_tracker`` clears the singleton
   between runs so the summary reflects only the current invocation.
   Tests use this fixture-style to stay hermetic.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.copilot.cost import (
    MODEL_PRICES_USD,
    CostBreakdown,
    CostRow,
    RunCostTracker,
    format_cost_summary,
    get_run_tracker,
    reset_run_tracker,
)

# ---------------------------------------------------------------------
# Tracker-level pins — accumulation, reset, breakdown shape
# ---------------------------------------------------------------------


class TestRunCostTracker:
    def setup_method(self) -> None:
        # Singleton state across tests in this class — explicit reset.
        reset_run_tracker()

    def test_single_call_produces_one_row(self):
        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000,
            output_tokens=500,
        )
        breakdown = tracker.breakdown()
        assert len(breakdown.rows) == 1
        row = breakdown.rows[0]
        assert row.provider == "openai"
        assert row.model == "gpt-4.1-mini"
        assert row.input_tokens == 1_000
        assert row.output_tokens == 500
        assert row.calls == 1

    def test_two_calls_same_model_collapse_to_one_row(self):
        """Per-(provider, model) accumulation — same model → same row,
        counts add."""
        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai", model="gpt-4.1-mini", input_tokens=100, output_tokens=50
        )
        tracker.record_call(
            provider="openai", model="gpt-4.1-mini", input_tokens=200, output_tokens=100
        )
        breakdown = tracker.breakdown()
        assert len(breakdown.rows) == 1
        assert breakdown.rows[0].input_tokens == 300
        assert breakdown.rows[0].output_tokens == 150
        assert breakdown.rows[0].calls == 2

    def test_two_models_produce_two_rows(self):
        tracker = get_run_tracker()
        tracker.record_call(provider="openai", model="gpt-4.1", input_tokens=100, output_tokens=50)
        tracker.record_call(
            provider="openai", model="gpt-4.1-mini", input_tokens=200, output_tokens=100
        )
        breakdown = tracker.breakdown()
        assert len(breakdown.rows) == 2
        models = {row.model for row in breakdown.rows}
        assert models == {"gpt-4.1", "gpt-4.1-mini"}

    def test_reset_clears_counters(self):
        tracker = get_run_tracker()
        tracker.record_call(provider="openai", model="gpt-4.1", input_tokens=100, output_tokens=50)
        reset_run_tracker()
        assert get_run_tracker().breakdown().rows == []

    def test_breakdown_totals_sum_correctly(self):
        tracker = get_run_tracker()
        tracker.record_call(provider="openai", model="gpt-4.1", input_tokens=100, output_tokens=50)
        tracker.record_call(
            provider="openai", model="gpt-4.1-mini", input_tokens=200, output_tokens=100
        )
        tracker.record_call(
            provider="anthropic", model="claude-haiku-4-5", input_tokens=300, output_tokens=150
        )
        breakdown = tracker.breakdown()
        assert breakdown.total_input_tokens == 600
        assert breakdown.total_output_tokens == 300
        assert breakdown.total_calls == 3


# ---------------------------------------------------------------------
# Pricing — known models, unknown models, Ollama-as-zero
# ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_embedded_price_table(monkeypatch, request):
    """Pin the embedded ``MODEL_PRICES_USD`` table for cost tests.

    `_price_for` now consults litellm's catalog before the embedded
    table (matching the "rely on litellm" architecture). The pricing
    tests in this file pin specific dollar amounts that came from the
    embedded table — mocking litellm out forces the fallthrough so
    the assertions stay deterministic regardless of litellm's
    upstream pricing changes.

    Only fires for the ``TestPricing`` and ``TestPriceOverride``
    classes; the rest of the file doesn't read prices.
    """
    target_classes = {"TestPricing", "TestPriceOverride"}
    cls = request.cls.__name__ if request.cls else ""
    if cls not in target_classes:
        return
    import sys

    fake_litellm = type(sys)("_test_fake_litellm")
    fake_litellm.cost_per_token = lambda **_kw: (_ for _ in ()).throw(
        RuntimeError("litellm disabled for embedded-fallback test")
    )
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)


class TestPricing:
    def setup_method(self):
        reset_run_tracker()

    def test_known_openai_model_priced_correctly(self):
        """``gpt-4.1-mini`` is $0.15 in / $0.60 out per 1M tokens.
        1M in + 1M out → $0.75."""
        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        breakdown = tracker.breakdown()
        assert breakdown.rows[0].usd == pytest.approx(0.75)
        assert breakdown.total_usd == pytest.approx(0.75)

    def test_known_anthropic_model_priced_correctly(self):
        """``claude-sonnet-4-6`` is $3.00 in / $15.00 out per 1M.
        100k in + 10k out = $0.30 + $0.15 = $0.45."""
        tracker = get_run_tracker()
        tracker.record_call(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=100_000,
            output_tokens=10_000,
        )
        breakdown = tracker.breakdown()
        assert breakdown.rows[0].usd == pytest.approx(0.45)

    def test_unknown_model_surfaces_as_none_with_listing(self):
        """An unknown model id must NOT silently report $0 — operators
        would think they got their pipeline for free. Surface the
        unknown model name so the table can be updated."""
        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="future-gpt-9000",
            input_tokens=1000,
            output_tokens=500,
        )
        breakdown = tracker.breakdown()
        assert breakdown.rows[0].usd is None
        assert "future-gpt-9000" in breakdown.unknown_models
        # Total is None when ANY row has unknown price — defends
        # against partial sums that look authoritative.
        assert breakdown.total_usd is None

    def test_ollama_provider_always_zero(self):
        """Local Ollama models all cost $0 (compute is the user's own
        electricity). The provider-name match wins regardless of the
        specific model id, so any local model works without a
        per-model price-table entry."""
        tracker = get_run_tracker()
        tracker.record_call(
            provider="ollama",
            model="llama3.1:70b",
            input_tokens=10_000_000,
            output_tokens=5_000_000,
        )
        breakdown = tracker.breakdown()
        assert breakdown.rows[0].usd == 0.0
        assert breakdown.total_usd == 0.0
        assert "llama3.1:70b" not in breakdown.unknown_models

    def test_mixed_known_and_unknown_total_is_none(self):
        """Even one unknown model must mark the total as ``None`` so
        the operator knows the headline figure is incomplete."""
        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000,
            output_tokens=500,
        )
        tracker.record_call(
            provider="openai",
            model="unknown-future-model",
            input_tokens=1_000,
            output_tokens=500,
        )
        breakdown = tracker.breakdown()
        # First row has a price; second doesn't.
        prices = [row.usd for row in breakdown.rows]
        assert None in prices
        assert any(p is not None for p in prices)
        # Total is None — must not silently treat the missing one as zero.
        assert breakdown.total_usd is None


# ---------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------


class TestFormatCostSummary:
    def test_empty_breakdown_message(self):
        breakdown = CostBreakdown(rows=[])
        text = format_cost_summary(breakdown)
        assert "no LLM calls" in text

    def test_known_price_renders_dollars(self):
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="gpt-4.1-mini",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=0.0005,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=0.0005,
        )
        text = format_cost_summary(breakdown)
        assert "$0.0005" in text
        assert "openai" in text
        assert "gpt-4.1-mini" in text
        assert "1,000 in" in text  # comma separator for readability
        assert "500 out" in text

    def test_unknown_price_renders_question_mark(self):
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="future-model",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=None,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=None,
            unknown_models=["future-model"],
        )
        text = format_cost_summary(breakdown)
        # Per-row + total both show $?.
        assert text.count("$?") >= 2
        # The unknown-model note guides the operator to the price-table file.
        assert "future-model" in text
        assert "MODEL_PRICES_USD" in text


# ---------------------------------------------------------------------
# Pricing table sanity — every entry is non-negative + has both prices
# ---------------------------------------------------------------------


def test_price_table_entries_well_formed():
    """Every entry in :data:`MODEL_PRICES_USD` must be a (in, out)
    tuple with non-negative numeric prices. Defends against a typo
    introducing a negative price (which would produce negative cost
    summaries) or a single-element entry."""
    for model, prices in MODEL_PRICES_USD.items():
        assert isinstance(prices, tuple), f"{model}: prices must be a tuple"
        assert len(prices) == 2, f"{model}: expected (input, output) prices"
        for p in prices:
            assert isinstance(p, (int, float)), f"{model}: prices must be numeric"
            assert p >= 0, f"{model}: prices must be non-negative"


def test_ollama_sentinel_present():
    """The ``*ollama*`` sentinel must remain in the table because the
    provider-name match relies on it semantically (even though the
    actual lookup fast-paths Ollama via provider==ollama). Pin the
    sentinel so a future cleanup doesn't drop it without noticing the
    coverage path."""
    assert "*ollama*" in MODEL_PRICES_USD


# ---------------------------------------------------------------------
# Missing-usage surfacing — V2.4.4 follow-up (Gap 7.1)
# ---------------------------------------------------------------------


class TestMissingUsageSurfacing:
    """Some providers ship empty / partial ``usage`` blocks (under
    load, on streaming-cancellation paths, on certain Azure
    deployments). Without a counter, the user sees a misleading
    "$0.0042" total with no hint that the figure is under-reported.

    Pin the contract:

    * ``record_call`` with both token counts zero on a non-Ollama
      provider increments ``missing_usage_calls``.
    * Same call on Ollama does NOT — Ollama is legitimately $0 and
      0/0 is the local-compute baseline, not a missing block.
    * ``record_missing_usage`` (the exception-path counter) bumps
      the same surfaced flag.
    * ``format_cost_summary`` prints the warning footer when the
      counter is non-zero, suppresses it when zero.
    """

    def setup_method(self):
        reset_run_tracker()

    def test_zero_usage_non_ollama_flagged_as_missing(self):
        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=0,
            output_tokens=0,
        )
        breakdown = tracker.breakdown()
        assert breakdown.missing_usage_calls == 1

    def test_zero_usage_ollama_NOT_flagged_as_missing(self):
        tracker = get_run_tracker()
        tracker.record_call(
            provider="ollama",
            model="llama3.1:70b",
            input_tokens=0,
            output_tokens=0,
        )
        breakdown = tracker.breakdown()
        assert breakdown.missing_usage_calls == 0

    def test_record_missing_usage_increments_counter(self):
        """The exception-path entry point bumps the counter without
        recording any token data — the row table is unaffected."""
        tracker = get_run_tracker()
        tracker.record_missing_usage()
        tracker.record_missing_usage()
        breakdown = tracker.breakdown()
        assert breakdown.missing_usage_calls == 2
        assert breakdown.rows == []

    def test_full_usage_call_does_not_flag(self):
        tracker = get_run_tracker()
        tracker.record_call(
            provider="anthropic",
            model="claude-haiku-4-5",
            input_tokens=100,
            output_tokens=50,
        )
        assert tracker.breakdown().missing_usage_calls == 0

    def test_reset_clears_missing_usage_counter(self):
        tracker = get_run_tracker()
        tracker.record_missing_usage()
        reset_run_tracker()
        assert get_run_tracker().breakdown().missing_usage_calls == 0

    def test_format_summary_shows_footer_when_missing(self):
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="gpt-4.1-mini",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=2,
                    usd=0.0005,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=2,
            total_usd=0.0005,
            missing_usage_calls=1,
        )
        text = format_cost_summary(breakdown)
        assert "no usage data" in text
        assert "under-reported" in text
        # Singular form for one call.
        assert "1 call had no usage data" in text

    def test_format_summary_pluralises_correctly(self):
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="gpt-4.1-mini",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=3,
                    usd=0.0005,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=3,
            total_usd=0.0005,
            missing_usage_calls=2,
        )
        text = format_cost_summary(breakdown)
        assert "2 calls had no usage data" in text

    def test_format_summary_no_footer_when_clean(self):
        """The whole point: silence is the success state. When every
        call had usage data, the footer must NOT appear so it doesn't
        cry wolf."""
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="gpt-4.1-mini",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=0.0005,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=0.0005,
            missing_usage_calls=0,
        )
        text = format_cost_summary(breakdown)
        assert "no usage data" not in text
        assert "under-reported" not in text


# ---------------------------------------------------------------------
# Per-org price override (Gap 7.2) — ~/.fluid/prices.json
# ---------------------------------------------------------------------


class TestPriceOverride:
    """Enterprise customers negotiate rates that don't match the
    embedded list price. The override file lets them patch in those
    rates without forking forge-cli.

    Tests pin:

    * Override file present + well-formed → row pricing uses the
      override, not the embedded table.
    * Override missing or malformed → falls back to the embedded
      table silently (never raises during a forge run).
    * Both wrapped (``{"prices": {...}}``) and flat (``{...}``)
      layouts are accepted so an operator scribbling an override
      doesn't have to consult docs to get it right.
    * Negative / non-numeric prices in the override are rejected
      per-entry (the rest of the file still applies).
    * Override only applies to models it lists — a partial override
      doesn't blow away the embedded table for unmentioned models.
    """

    def setup_method(self):
        reset_run_tracker()

    def _write_override(self, tmp_path, payload):
        path = tmp_path / "prices.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_override_wrapped_layout_used(self, tmp_path, monkeypatch):
        path = self._write_override(
            tmp_path,
            {
                "schema_version": 1,
                "prices": {"gpt-4.1-mini": [0.10, 0.40]},  # half the list rate
            },
        )
        monkeypatch.setenv("FLUID_PRICES_JSON", str(path))

        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # 0.10 + 0.40 = $0.50 (vs. $0.75 at list rate).
        assert tracker.breakdown().rows[0].usd == pytest.approx(0.50)

    def test_override_flat_layout_used(self, tmp_path, monkeypatch):
        path = self._write_override(
            tmp_path,
            {"gpt-4.1-mini": [0.05, 0.20]},  # quarter the list rate
        )
        monkeypatch.setenv("FLUID_PRICES_JSON", str(path))

        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert tracker.breakdown().rows[0].usd == pytest.approx(0.25)

    def test_override_missing_falls_back_to_embedded(self, tmp_path, monkeypatch):
        # Point at a path that doesn't exist.
        monkeypatch.setenv("FLUID_PRICES_JSON", str(tmp_path / "absent.json"))
        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # Embedded $0.75 stands.
        assert tracker.breakdown().rows[0].usd == pytest.approx(0.75)

    def test_override_malformed_json_falls_back(self, tmp_path, monkeypatch):
        path = tmp_path / "prices.json"
        path.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setenv("FLUID_PRICES_JSON", str(path))
        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # Embedded fallback — never raises mid-forge.
        assert tracker.breakdown().rows[0].usd == pytest.approx(0.75)

    def test_override_partial_does_not_clobber_embedded(self, tmp_path, monkeypatch):
        """A partial override only patches the listed models. Other
        models still use the embedded table."""
        path = self._write_override(
            tmp_path,
            {"gpt-4.1-mini": [0.10, 0.40]},
        )
        monkeypatch.setenv("FLUID_PRICES_JSON", str(path))

        tracker = get_run_tracker()
        # Override applies.
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # NOT overridden — embedded $3 in / $15 out applies.
        tracker.record_call(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        breakdown = tracker.breakdown()
        prices = {row.model: row.usd for row in breakdown.rows}
        assert prices["gpt-4.1-mini"] == pytest.approx(0.50)  # override
        assert prices["claude-sonnet-4-6"] == pytest.approx(18.00)  # embedded

    def test_override_rejects_negative_prices(self, tmp_path, monkeypatch):
        """Operator typo: negative price → silently skipped, embedded
        table wins. Defends against misleading negative cost."""
        path = self._write_override(
            tmp_path,
            {"gpt-4.1-mini": [-0.10, 0.40]},
        )
        monkeypatch.setenv("FLUID_PRICES_JSON", str(path))

        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        # Override rejected → embedded $0.75 stands.
        assert tracker.breakdown().rows[0].usd == pytest.approx(0.75)

    def test_override_rejects_short_tuple(self, tmp_path, monkeypatch):
        """Bad entry: only one price → skipped per-entry, rest applies."""
        path = self._write_override(
            tmp_path,
            {
                "gpt-4.1-mini": [0.10],  # too short
                "gpt-4o-mini": [0.05, 0.20],
            },
        )
        monkeypatch.setenv("FLUID_PRICES_JSON", str(path))

        tracker = get_run_tracker()
        tracker.record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        tracker.record_call(
            provider="openai",
            model="gpt-4o-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        prices = {row.model: row.usd for row in tracker.breakdown().rows}
        assert prices["gpt-4.1-mini"] == pytest.approx(0.75)  # embedded fallback
        assert prices["gpt-4o-mini"] == pytest.approx(0.25)  # override applied


# ---------------------------------------------------------------------
# Variant-lint surfacing (Gap 7.5) — warning count + variant in footer
# ---------------------------------------------------------------------


class TestVariantLintSurfacing:
    """The dimensional variant validator (``lint_dimensional_variant``)
    flags structural mismatches like ``variant='galaxy'`` claimed with
    only one fact table. Warnings flow into the validation report;
    Gap 7.5 puts them right next to the cost in the summary footer
    so operators piping stdout to a log don't have to dig through
    the longer report to find the lint score.

    Tests pin:

    * ``record_variant_lint`` REPLACES the per-variant count on each
      call (repair loops run the validator multiple times; the
      footer should show the final pass, not an accumulated total).
    * Setting count to 0 removes the entry — clean lint is silent.
    * The formatter renders one footer line per variant, sorted for
      stability.
    * Singular vs plural ("warning" vs "warnings") is correct.
    * Reset clears variant-lint state.
    """

    def setup_method(self):
        reset_run_tracker()

    def test_record_then_breakdown_carries_count(self):
        tracker = get_run_tracker()
        tracker.record_variant_lint("snowflake", 2)
        breakdown = tracker.breakdown()
        assert breakdown.variant_lint_findings == {"snowflake": 2}

    def test_record_replaces_not_accumulates(self):
        """Repair loop runs the validator twice; the final pass had
        only 1 warning. The footer must show 1, not 1+previous."""
        tracker = get_run_tracker()
        tracker.record_variant_lint("snowflake", 3)  # first pass
        tracker.record_variant_lint("snowflake", 1)  # final pass after repair
        assert tracker.breakdown().variant_lint_findings == {"snowflake": 1}

    def test_zero_count_removes_entry(self):
        """A clean lint pass is silent — no footer line for variants
        with zero warnings."""
        tracker = get_run_tracker()
        tracker.record_variant_lint("star", 2)
        tracker.record_variant_lint("star", 0)  # repair fixed all warnings
        assert tracker.breakdown().variant_lint_findings == {}

    def test_empty_variant_string_ignored(self):
        """Defensive: a missing/empty variant must not pollute the
        store with empty-string keys."""
        tracker = get_run_tracker()
        tracker.record_variant_lint("", 5)
        assert tracker.breakdown().variant_lint_findings == {}

    def test_reset_clears_variant_state(self):
        tracker = get_run_tracker()
        tracker.record_variant_lint("galaxy", 4)
        reset_run_tracker()
        assert get_run_tracker().breakdown().variant_lint_findings == {}

    def test_format_summary_renders_singular(self):
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="gpt-4.1-mini",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=0.0005,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=0.0005,
            variant_lint_findings={"snowflake": 1},
        )
        text = format_cost_summary(breakdown)
        assert "1 variant-lint warning" in text
        assert "variant='snowflake'" in text

    def test_format_summary_renders_plural(self):
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="gpt-4.1-mini",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=0.0005,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=0.0005,
            variant_lint_findings={"galaxy": 3},
        )
        text = format_cost_summary(breakdown)
        assert "3 variant-lint warnings" in text
        assert "variant='galaxy'" in text

    def test_format_summary_no_lint_section_when_clean(self):
        """No findings → no footer line. Silence is the success
        state — same UX rule as missing-usage and unknown-models."""
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="gpt-4.1-mini",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=0.0005,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=0.0005,
            variant_lint_findings={},
        )
        text = format_cost_summary(breakdown)
        assert "variant-lint" not in text

    def test_format_summary_multiple_variants_sorted(self):
        """If multiple variants have findings (e.g. galaxy run that
        produced both a star and snowflake fact), each variant gets
        its own line, sorted alphabetically for stable diffing."""
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="gpt-4.1-mini",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=0.0005,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=0.0005,
            variant_lint_findings={"snowflake": 2, "galaxy": 1},
        )
        text = format_cost_summary(breakdown)
        # Both lines present.
        assert "variant='galaxy'" in text
        assert "variant='snowflake'" in text
        # Galaxy comes first (alphabetical sort).
        galaxy_pos = text.index("variant='galaxy'")
        snowflake_pos = text.index("variant='snowflake'")
        assert galaxy_pos < snowflake_pos


# ---------------------------------------------------------------------
# Catalog-fetch latency surfacing (Gap 9) — operators see whether
# their forge runtime is dominated by the LLM stage or by catalog
# round-trip latency.
# ---------------------------------------------------------------------


class TestCatalogFetchSurfacing:
    """Five behaviour pins for the catalog-fetch latency tracker:

    1. ``record_catalog_fetch`` accumulates across multiple calls
       per catalog (a forge that hits Snowflake three times shows
       the SUM, not the last).
    2. Negative / zero durations are ignored (defensive).
    3. The footer line renders milliseconds for sub-1s and
       seconds-with-1-decimal for ≥1s — readability matters for
       wall-clock at-a-glance.
    4. Multiple catalogs each get their own footer line, sorted.
    5. Reset clears the catalog-fetch dict.
    """

    def setup_method(self):
        reset_run_tracker()

    def test_record_accumulates_across_calls(self):
        tracker = get_run_tracker()
        tracker.record_catalog_fetch("snowflake", 1500)
        tracker.record_catalog_fetch("snowflake", 800)
        breakdown = tracker.breakdown()
        assert breakdown.catalog_fetch_ms == {"snowflake": 2300}

    def test_negative_or_zero_ignored(self):
        tracker = get_run_tracker()
        tracker.record_catalog_fetch("snowflake", 0)
        tracker.record_catalog_fetch("snowflake", -100)
        tracker.record_catalog_fetch("", 500)
        assert tracker.breakdown().catalog_fetch_ms == {}

    def test_format_summary_renders_seconds_for_long_fetch(self):
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=0.0125,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=0.0125,
            catalog_fetch_ms={"snowflake": 4231},
        )
        text = format_cost_summary(breakdown)
        assert "Catalog fetch: snowflake" in text
        assert "4.2s" in text
        assert "read-only metadata" in text

    def test_format_summary_renders_ms_for_short_fetch(self):
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="anthropic",
                    model="claude-haiku-4-5",
                    input_tokens=100,
                    output_tokens=50,
                    calls=1,
                    usd=0.00035,
                )
            ],
            total_input_tokens=100,
            total_output_tokens=50,
            total_calls=1,
            total_usd=0.00035,
            catalog_fetch_ms={"datamesh_manager": 250},
        )
        text = format_cost_summary(breakdown)
        # Sub-1s renders as "250ms" with a comma if >1000.
        assert "250ms" in text

    def test_format_summary_multiple_catalogs_sorted(self):
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=0.0125,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=0.0125,
            catalog_fetch_ms={"snowflake": 1500, "datahub": 800},
        )
        text = format_cost_summary(breakdown)
        # Both shown.
        assert "snowflake" in text
        assert "datahub" in text
        # Sorted alphabetically — datahub before snowflake.
        assert text.index("datahub") < text.index("snowflake")

    def test_format_summary_no_section_when_clean(self):
        """No catalog calls → no footer line."""
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="openai",
                    model="gpt-4.1-mini",
                    input_tokens=1_000,
                    output_tokens=500,
                    calls=1,
                    usd=0.0005,
                )
            ],
            total_input_tokens=1_000,
            total_output_tokens=500,
            total_calls=1,
            total_usd=0.0005,
            catalog_fetch_ms={},
        )
        text = format_cost_summary(breakdown)
        assert "Catalog fetch" not in text

    def test_reset_clears_catalog_fetch(self):
        tracker = get_run_tracker()
        tracker.record_catalog_fetch("snowflake", 1500)
        reset_run_tracker()
        assert get_run_tracker().breakdown().catalog_fetch_ms == {}
