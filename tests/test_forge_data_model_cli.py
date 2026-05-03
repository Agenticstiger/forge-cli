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

import json
import logging
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from fluid_build.cli.forge_data_model import run_from_intent_command, run_validate_command


def _intent_discovery_args(**overrides):
    base = {
        "example": None,
        "schema": False,
        "validate_intent": None,
        "intent_file": None,
        "output": None,
    }
    base.update(overrides)
    return Namespace(**base)


def test_from_intent_example_prints_parseable_yaml(capsys):
    code = run_from_intent_command(
        _intent_discovery_args(example="minimal"), logging.getLogger("test")
    )

    assert code == 0
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["data_product"]["name"] == "customer_orders"
    assert payload["grain"]["entity"] == "order_line"


def test_from_intent_schema_prints_json_schema(capsys):
    code = run_from_intent_command(_intent_discovery_args(schema=True), logging.getLogger("test"))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["title"] == "BusinessIntent"
    assert "data_product" in payload["properties"]


def test_from_intent_validate_checks_input_without_writing(tmp_path, capsys):
    intent_path = tmp_path / "customer_orders.intent.yaml"
    intent_path.write_text(
        """
data_product:
  name: customer_orders
  domain: retail
dimensions:
  entities: [customer]
""",
        encoding="utf-8",
    )

    code = run_from_intent_command(
        _intent_discovery_args(validate_intent=str(intent_path)), logging.getLogger("test")
    )

    assert code == 0
    assert "Intent file is valid" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.fluid.yaml"))


def test_from_intent_missing_name_has_friendly_error(tmp_path, capsys):
    intent_path = tmp_path / "bad.intent.yaml"
    intent_path.write_text(
        """
data_product:
  domain: retail
dimensions:
  entities: [customer]
""",
        encoding="utf-8",
    )

    code = run_from_intent_command(
        _intent_discovery_args(validate_intent=str(intent_path)), logging.getLogger("test")
    )

    assert code == 1
    assert "intent file is missing data_product.name" in capsys.readouterr().out


def test_from_intent_weak_input_has_friendly_error(tmp_path, capsys):
    intent_path = tmp_path / "weak.intent.yaml"
    intent_path.write_text(
        """
data_product:
  name: weak_model
  domain: retail
""",
        encoding="utf-8",
    )

    code = run_from_intent_command(
        _intent_discovery_args(validate_intent=str(intent_path)), logging.getLogger("test")
    )

    assert code == 1
    output = " ".join(capsys.readouterr().out.split())
    assert "intent file needs at least one grain, dimension, metric, or data source" in output


def test_from_intent_rejects_wrong_file_type(tmp_path, capsys):
    intent_path = tmp_path / "intent.txt"
    intent_path.write_text("data_product: {name: x, domain: retail}", encoding="utf-8")

    code = run_from_intent_command(
        _intent_discovery_args(validate_intent=str(intent_path)), logging.getLogger("test")
    )

    assert code == 1
    assert "intent files must be YAML or JSON" in capsys.readouterr().out


@pytest.mark.skip(
    reason="emitter defaults to fluidVersion 0.7.3 \u2014 needs PR-3+ for build_runners + matching emitter update"
)
def test_forge_data_model_from_intent_writes_contract_and_sidecar(tmp_path):
    intent_path = tmp_path / "intent.yaml"
    intent_path.write_text(
        """
data_product:
  name: customer_orders
  domain: retail
grain:
  entity: order_line
  time_dimension: order_date
dimensions:
  entities: [customer, product]
  attributes: [name, category]
""",
        encoding="utf-8",
    )
    output_path = tmp_path / "customer_orders.fluid.yaml"
    args = Namespace(
        intent_file=str(intent_path),
        technique="dimensional",
        output=str(output_path),
        engine="dbt",
        review=False,
        dry_run=False,
        no_cache=True,
        tiered=False,
        llm_provider=None,
        llm_model=None,
        llm_endpoint=None,
    )

    code = run_from_intent_command(args, logging.getLogger("test"))

    assert code == 0
    assert output_path.exists()
    sidecar = output_path.with_name(f"{output_path.name}.model.json")
    model_doc = output_path.with_name(f"{output_path.name}.model.md")
    assert sidecar.exists()
    assert model_doc.exists()
    model_doc_text = model_doc.read_text(encoding="utf-8")
    assert "```mermaid" in model_doc_text
    assert "### Facts" in model_doc_text
    assert "### Dimensions" in model_doc_text
    contract = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert contract["labels"]["dataModelingTechnique"] == "dimensional"
    assert contract["labels"]["modelSidecar"] == sidecar.name
    assert contract["labels"]["modelDoc"] == model_doc.name
    assert contract["labels"]["contractForgedBy"] == "ContractForgeAgent"
    assert contract["labels"]["agenticMode"] == "heuristic"
    assert contract["labels"]["agenticFallbackUsed"] == "false"
    semantics = contract["exposes"][0]["semantics"]
    for key in ("entities", "dimensions", "measures", "metrics"):
        assert semantics.get(key), f"generated contract must emit semantics.{key}"

    validate_args = Namespace(path=str(output_path))
    assert run_validate_command(validate_args, logging.getLogger("test")) == 0

    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    plan_path = tmp_path / "plan.json"
    plan_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build",
            "plan",
            str(output_path),
            "--out",
            str(plan_path),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert plan_result.returncode == 0, plan_result.stdout + plan_result.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan.get("planDigest", "").startswith("sha256:")
    assert plan.get("contract", {}).get("id") == contract["id"]
    assert plan.get("actions"), "forged contracts must produce executable plan actions"

    apply_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build",
            "apply",
            str(plan_path),
            "--dry-run",
            "--yes",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert apply_result.returncode == 0, apply_result.stdout + apply_result.stderr


def test_require_llm_without_provider_fails_before_writing_contract(tmp_path):
    intent_path = tmp_path / "intent.yaml"
    intent_path.write_text(
        """
data_product:
  name: strict_orders
  domain: retail
grain:
  entity: order_line
dimensions:
  entities: [customer]
modeling:
  technique: dimensional
""",
        encoding="utf-8",
    )
    output_path = tmp_path / "strict_orders.fluid.yaml"
    args = Namespace(
        intent_file=str(intent_path),
        technique="dimensional",
        output=str(output_path),
        engine="dbt",
        review=False,
        dry_run=False,
        no_cache=True,
        deterministic=False,
        tiered=False,
        require_llm=True,
        llm_provider=None,
        llm_model=None,
        llm_endpoint=None,
        industry=None,
        allow_semantic_warnings=True,
        emit_model_doc=True,
    )

    code = run_from_intent_command(args, logging.getLogger("test"))

    assert code == 1
    assert not output_path.exists()


@pytest.mark.skip(
    reason="emitter defaults to fluidVersion 0.7.3 \u2014 needs PR-3+ for build_runners + matching emitter update"
)
def test_forge_data_model_no_emit_model_doc_keeps_sidecar(tmp_path):
    intent_path = tmp_path / "intent.yaml"
    intent_path.write_text(
        """
data_product:
  name: customer_orders
  domain: retail
grain:
  entity: order_line
  time_dimension: order_date
dimensions:
  entities: [customer]
  attributes: [name]
""",
        encoding="utf-8",
    )
    output_path = tmp_path / "customer_orders.fluid.yaml"
    args = Namespace(
        intent_file=str(intent_path),
        technique="dimensional",
        output=str(output_path),
        engine="dbt",
        review=False,
        dry_run=False,
        no_cache=True,
        tiered=False,
        llm_provider=None,
        llm_model=None,
        llm_endpoint=None,
        emit_model_doc=False,
    )

    code = run_from_intent_command(args, logging.getLogger("test"))

    assert code == 0
    sidecar = output_path.with_name(f"{output_path.name}.model.json")
    model_doc = output_path.with_name(f"{output_path.name}.model.md")
    assert sidecar.exists()
    assert not model_doc.exists()
    contract = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert contract["labels"]["modelSidecar"] == sidecar.name
    assert "modelDoc" not in contract["labels"]
