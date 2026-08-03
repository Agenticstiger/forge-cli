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

"""Integration tests for the ``--apply-enrichment`` flag.

These exercise the helper that wires the apply pass into the forge
runtime — ``_maybe_apply_enrichment`` — under each of the four
relevant input shapes:

* flag set + ``--yes`` ⇒ contract patched on disk
* flag set + interactive 'n' ⇒ contract unchanged
* flag set + interactive 'y' ⇒ contract patched
* flag NOT set ⇒ baseline regression guard (no apply pass runs)

The runtime helper signature is stable; we drive it directly rather
than spinning up the whole forge CLI end-to-end, which keeps the test
fast and isolates the apply-pass behavior from the LLM / argparse
plumbing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List
from unittest.mock import patch

import yaml

# Import forge_modes FIRST so the circular-import path that lives at
# the bottom of forge_modes.py finishes wiring before we reach into
# _template_mode. Mirrors the order existing tests use.
from fluid_build.cli import forge_modes as _forge_modes  # noqa: F401
from fluid_build.cli._template_mode import _maybe_apply_enrichment

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample_contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "ecom.sales.orders_v1",
        "name": "orders",
        "domain": "sales",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "owner": {"team": "sales-eng"},
            "refreshCadence": "hourly",
        },
        "builds": [{"id": "ingest", "engine": "dbt"}],
        "exposes": [
            {
                "exposeId": "orders_curated",
                "kind": "table",
                "binding": {"platform": "snowflake", "format": "table"},
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "BIGINT"},
                        {"name": "updated_at", "type": "TIMESTAMP"},
                    ],
                },
            }
        ],
    }


def _sample_artifacts() -> Dict[str, Any]:
    return {
        "provider": "snowflake",
        "refresh_cadence": "hourly",
        "dbt_tests": [
            {
                "version": 2,
                "models": [
                    {
                        "name": "orders_curated",
                        "columns": [
                            {"name": "order_id", "tests": ["not_null", "unique"]},
                        ],
                    }
                ],
            }
        ],
        "freshness": {
            "warn_after": {"count": 2, "period": "hour"},
            "error_after": {"count": 6, "period": "hour"},
            "filter": None,
        },
        "physical_layout": [
            {
                "model_name": "orders_curated",
                "clustering_keys": ["order_id"],
                "partition_by": None,
                "partition_grain": None,
                "materialization_hint": "incremental",
                "provider_specific": {},
            }
        ],
    }


class _StubResult:
    """Mimic the CopilotGenerationResult shape ``_maybe_apply_enrichment`` reads."""

    def __init__(self, artifacts: Dict[str, Any] | None) -> None:
        self.provenance: Dict[str, Any] = {
            "enrichment_applied": artifacts is not None,
            "enrichment_artifacts": artifacts,
        }


# ---------------------------------------------------------------------------
# --apply-enrichment --yes  ⇒ contract patched on disk
# ---------------------------------------------------------------------------


def test_apply_enrichment_with_yes_patches_contract(tmp_path):
    contract = _sample_contract()
    result = _StubResult(_sample_artifacts())
    logger = logging.getLogger("test.apply_enrichment.yes")

    patched = _maybe_apply_enrichment(
        contract=contract,
        generation_result=result,
        auto_yes=True,
        logger=logger,
        console=None,
    )

    # Patch landed — every fill in its schema-valid slot.
    assert patched is not contract
    assert patched["exposes"][0]["qos"]["freshnessSLO"] == "PT2H"
    dq_rules = patched["exposes"][0]["contract"]["dq"]["rules"]
    assert any(r["type"] == "freshness" for r in dq_rules)
    assert "physical" in patched["exposes"][0]["binding"]["properties"]
    enrichment_ns = patched["extensions"]["enrichment"]
    assert "dbtTestSuggestions" in enrichment_ns
    assert enrichment_ns["applied"]["source"] == "enrichment-v2"

    # Round-trip the patched contract through YAML to prove it serialises —
    # the apply pass should produce something we can actually write to disk.
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text(yaml.safe_dump(patched, sort_keys=False), encoding="utf-8")
    reloaded = yaml.safe_load(contract_path.read_text())
    assert reloaded["exposes"][0]["qos"]["freshnessSLO"] == "PT2H"


# ---------------------------------------------------------------------------
# --apply-enrichment (no --yes)  ⇒ user declines  ⇒ contract unchanged
# ---------------------------------------------------------------------------


def test_apply_enrichment_user_declines_keeps_original(tmp_path, monkeypatch):
    contract = _sample_contract()
    result = _StubResult(_sample_artifacts())
    logger = logging.getLogger("test.apply_enrichment.decline")

    # Pretend stdin is a TTY so the prompt code path actually runs.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with patch("builtins.input", return_value="n"):
        out = _maybe_apply_enrichment(
            contract=contract,
            generation_result=result,
            auto_yes=False,
            logger=logger,
            console=None,
        )

    # Declined ⇒ caller gets back the un-patched contract.
    assert out is contract
    assert "qos" not in contract["exposes"][0]
    assert "dq" not in contract["exposes"][0]["contract"]
    assert "properties" not in contract["exposes"][0]["binding"]
    assert "extensions" not in contract


# ---------------------------------------------------------------------------
# --apply-enrichment (no --yes)  ⇒ user accepts  ⇒ contract patched
# ---------------------------------------------------------------------------


def test_apply_enrichment_user_accepts_returns_patched(monkeypatch):
    contract = _sample_contract()
    result = _StubResult(_sample_artifacts())
    logger = logging.getLogger("test.apply_enrichment.accept")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with patch("builtins.input", return_value="y"):
        patched = _maybe_apply_enrichment(
            contract=contract,
            generation_result=result,
            auto_yes=False,
            logger=logger,
            console=None,
        )

    assert patched is not contract
    assert patched["exposes"][0]["qos"]["freshnessSLO"] == "PT2H"
    assert "physical" in patched["exposes"][0]["binding"]["properties"]
    # Default-accept on empty input is also exercised once for safety.
    with patch("builtins.input", return_value=""):
        patched2 = _maybe_apply_enrichment(
            contract=_sample_contract(),
            generation_result=_StubResult(_sample_artifacts()),
            auto_yes=False,
            logger=logger,
            console=None,
        )
    # Empty input — empty string is NOT in ("y", "yes") so the helper
    # treats it as a decline. Strict default-no, which is the safer
    # interpretation for a destructive-ish pass.
    assert "qos" not in patched2["exposes"][0]


# ---------------------------------------------------------------------------
# No flag set ⇒ baseline regression guard
# ---------------------------------------------------------------------------


def test_without_flag_contract_is_identical_to_today():
    """The forge runtime never CALLS ``_maybe_apply_enrichment`` when the
    flag is unset. We assert the dispatch logic here by reading the
    template-mode source — the only call site lives inside an
    ``if options.get("apply_enrichment")`` block."""
    import inspect

    from fluid_build.cli import _template_mode

    src = inspect.getsource(_template_mode._create_project_minimal)
    assert 'options.get("apply_enrichment")' in src
    assert "_maybe_apply_enrichment" in src
    # The apply call must sit inside the conditional, not above it.
    cond_pos = src.find('if options.get("apply_enrichment")')
    call_pos = src.find("_maybe_apply_enrichment(")
    assert 0 < cond_pos < call_pos


# ---------------------------------------------------------------------------
# Edge cases: no artifacts / stdin not a tty
# ---------------------------------------------------------------------------


def test_apply_enrichment_no_artifacts_short_circuits():
    contract = _sample_contract()
    result = _StubResult(None)
    logger = logging.getLogger("test.apply_enrichment.empty")

    out = _maybe_apply_enrichment(
        contract=contract,
        generation_result=result,
        auto_yes=True,
        logger=logger,
        console=None,
    )

    assert out is contract  # no-op, original returned


def test_apply_enrichment_stdin_not_tty_accepts_default(monkeypatch):
    """Headless / piped invocations should auto-accept (avoid hanging on input)."""
    contract = _sample_contract()
    result = _StubResult(_sample_artifacts())
    logger = logging.getLogger("test.apply_enrichment.headless")

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    patched = _maybe_apply_enrichment(
        contract=contract,
        generation_result=result,
        auto_yes=False,
        logger=logger,
        console=None,
    )

    assert patched is not contract
    assert patched["exposes"][0]["qos"]["freshnessSLO"] == "PT2H"


# ---------------------------------------------------------------------------
# CLI flag registration (argparse)
# ---------------------------------------------------------------------------


def test_forge_argparse_registers_apply_enrichment_flag():
    """The flag has to appear in the forge subparser so users can set it."""
    import argparse

    from fluid_build.cli import forge

    root = argparse.ArgumentParser()
    sub = root.add_subparsers()
    forge.register(sub)

    # When the flag is unset the dest defaults to False.
    args = root.parse_args(["forge"])
    assert getattr(args, "apply_enrichment", "MISSING") is False

    # When set, it flips to True.
    args = root.parse_args(["forge", "--apply-enrichment"])
    assert args.apply_enrichment is True


def test_copilot_options_propagates_apply_enrichment():
    """``run_ai_copilot_mode`` must thread the flag into ``copilot_options``."""
    import inspect

    from fluid_build.cli import forge_modes

    src = inspect.getsource(forge_modes)
    # The flag is stashed under the canonical key the template-mode
    # helper reads.
    assert '"apply_enrichment"' in src
    assert 'get_cli_arg_fn(args, "apply_enrichment", False)' in src


# ---------------------------------------------------------------------------
# Diff visibility — the panel always renders, even on --yes
# ---------------------------------------------------------------------------


def test_apply_enrichment_with_yes_still_renders_diff(capsys):
    """Invariant I4 mirror: cost is always visible. Diff is too."""
    contract = _sample_contract()
    result = _StubResult(_sample_artifacts())
    logger = logging.getLogger("test.apply_enrichment.diff_visible")

    _maybe_apply_enrichment(
        contract=contract,
        generation_result=result,
        auto_yes=True,
        logger=logger,
        console=None,  # ⇒ plain stdout fallback
    )

    captured = capsys.readouterr()
    # The change-list and the unified-diff header are both on stdout.
    assert "--apply-enrichment" in captured.out
    assert "+++ contract.after.yaml" in captured.out
