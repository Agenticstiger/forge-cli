# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the ``fluid generate iac`` subcommand."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from fluid_build.cli import generate_iac
from fluid_build.cli._common import CLIError

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


class TestResolveProvider:
    def test_explicit_provider_wins(self):
        assert generate_iac._resolve_provider({}, "snowflake") == "snowflake"

    def test_auto_detects_single_platform(self):
        contract = {"exposes": [{"binding": {"platform": "gcp"}}]}
        assert generate_iac._resolve_provider(contract, "auto") == "gcp"

    def test_auto_errors_when_no_supported_cloud(self):
        with pytest.raises(CLIError):
            generate_iac._resolve_provider({"exposes": []}, "auto")

    def test_auto_errors_on_multiple_clouds(self):
        contract = {
            "exposes": [
                {"binding": {"platform": "gcp"}},
                {"binding": {"platform": "aws"}},
            ]
        }
        with pytest.raises(CLIError):
            generate_iac._resolve_provider(contract, "auto")


class TestGenerateIacRun:
    def test_writes_tofu_json_for_gcp_contract(self, tmp_path):
        contract = (
            _EXAMPLES / "bitcoin-price-api-imperative-part-a" / "contract-bigquery.fluid.yaml"
        )
        args = argparse.Namespace(
            contract=str(contract), provider="auto", out=str(tmp_path), env=None
        )
        rc = generate_iac.run(args, logging.getLogger("test"))
        assert rc == 0
        out = tmp_path / "main.tf.json"
        assert out.exists()
        doc = json.loads(out.read_text())
        assert "google_bigquery_table" in doc["resource"]
        assert doc["terraform"]["required_providers"]["google"]["source"] == "hashicorp/google"

    def test_missing_contract_returns_error(self):
        args = argparse.Namespace(contract=None, provider="auto", out="x", env=None)
        assert generate_iac.run(args, logging.getLogger("test")) == 1
