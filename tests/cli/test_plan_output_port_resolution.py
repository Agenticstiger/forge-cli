# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid plan`` resolves a contract's ``{{ env.* }}`` only for a provider that plans
output ports.

#869 made the plan read the contract as the apply does for the one action a plugin lists
after ``tofu apply`` (the Databricks Genie space, through ``planned_output_ports``). It
resolved the whole contract before looking for that hook, so every other contract was
resolved for nothing, and one with a credential-shaped placeholder
(``{{ env.PGPASSWORD }}``) logged a refusal that claimed the value goes to main.tf.json.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluid_build.cli import plan as plan_cli

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "source-aligned-postgres-duckdb"


def _local_contract() -> dict:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "shop.orders",
        "name": "orders",
        "metadata": {"owner": {"team": "t"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out/orders.parquet"},
                },
                "contract": {"schema": [{"name": "id", "type": "VARCHAR"}]},
            }
        ],
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "connection": {
                            "host": "{{ env.PGHOST }}",
                            "password": "{{ env.PGPASSWORD }}",
                        },
                    }
                },
                "outputs": ["orders"],
            }
        ],
    }


def test_a_provider_with_no_planned_output_ports_hook_resolves_nothing(monkeypatch, caplog):
    from fluid_build.cli import _common

    calls = []
    real = _common.resolve_env_templates_in_contract

    def _counting(contract, *a, **k):
        calls.append(contract)
        return real(contract, *a, **k)

    # Recorded, not raised: the plan catches a resolver's exception and carries on.
    monkeypatch.setattr(_common, "resolve_env_templates_in_contract", _counting)
    plan = {"actions": [{"action_id": "a"}]}
    with caplog.at_level(logging.WARNING):
        plan_cli._append_output_port_actions(
            plan,
            _local_contract(),
            SimpleNamespace(provider=None, contract=None),
            logging.getLogger("t"),
        )
    assert plan == {"actions": [{"action_id": "a"}]}
    assert calls == [], "resolved a contract no hook reads"
    assert "Refusing to resolve" not in caplog.text


@pytest.mark.skipif(not EXAMPLE.is_dir(), reason="example not in this checkout")
def test_fluid_plan_of_a_local_contract_logs_no_refusal_and_its_plan_is_the_environments_own(
    tmp_path,
):
    """End to end, as the compatibility gate runs it: no refusal line, and plan.json (with its
    planDigest) is the same whether the placeholders' variables are set or not."""
    work = tmp_path / "ex"
    shutil.copytree(EXAMPLE, work)
    base = {k: v for k, v in os.environ.items() if not k.startswith(("FLUID_", "PG"))}
    base["PYTHONPATH"] = str(REPO)
    plans = []
    for extra in ({}, {"PGHOST": "db.internal", "PG" + "PASSWORD": "x" * 8}):
        out = tmp_path / f"plan{len(plans)}.json"
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "fluid_build.cli",
                "plan",
                "contract.fluid.yaml",
                "--out",
                str(out),
            ],
            cwd=work,
            env={**base, **extra},
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert run.returncode == 0, run.stderr[-2000:]
        assert "Refusing to resolve" not in run.stderr
        plans.append(json.loads(out.read_text()))
    assert plans[0]["planDigest"] == plans[1]["planDigest"]
    assert json.dumps(plans[0], sort_keys=True).count("{{ env.PGHOST }}") >= 1
