# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the OpenTofu apply engine (``fluid apply --engine opentofu``)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_GCP_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "bitcoin-price-api-imperative-part-a"
    / "contract-bigquery.fluid.yaml"
)


class TestDataLossGate:
    def test_destructive_plan_blocked_by_default(self):
        assert engine._data_loss_blocked({"add": 0, "change": 0, "remove": 2}, False) is True

    def test_destructive_plan_allowed_with_flag(self):
        assert engine._data_loss_blocked({"add": 0, "change": 0, "remove": 2}, True) is False

    def test_additive_plan_never_blocked(self):
        assert engine._data_loss_blocked({"add": 5, "change": 1, "remove": 0}, False) is False


class TestLoadContract:
    def test_loads_yaml_contract(self):
        args = argparse.Namespace(contract=str(_GCP_CONTRACT), env=None)
        contract = engine._load_contract(args, logging.getLogger("test"))
        assert contract.get("exposes")

    def test_loads_json_plan_contract(self, tmp_path):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"contract": {"id": "x", "exposes": []}}))
        args = argparse.Namespace(contract=str(plan), env=None)
        assert engine._load_contract(args, logging.getLogger("test")) == {"id": "x", "exposes": []}

    def test_json_plan_without_contract_errors(self, tmp_path):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps({"actions": []}))
        args = argparse.Namespace(contract=str(plan), env=None)
        with pytest.raises(CLIError):
            engine._load_contract(args, logging.getLogger("test"))


class TestApplyViaOpentofuGuards:
    def test_missing_tofu_binary_errors(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine.runner, "tofu_path", lambda: None)
        args = argparse.Namespace(
            contract=str(_GCP_CONTRACT),
            env=None,
            provider=None,
            workspace_dir=tmp_path,
            state_backend=None,
            dry_run=True,
            allow_data_loss=False,
        )
        with pytest.raises(CLIError) as exc:
            engine.apply_via_opentofu(args, logging.getLogger("test"))
        assert "tofu" in str(exc.value).lower()
