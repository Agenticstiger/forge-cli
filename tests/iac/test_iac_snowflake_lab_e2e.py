# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end: the in-repo example Snowflake contracts, applied for real.

Layers 3 and 4 exercise synthetic contracts per resource type. This layer
takes the real ``examples/snowflake/*`` contracts — the ones shipped as
reference — and runs them through the ``apply_via_opentofu`` engine against
a live account, redirected into a throwaway database so the example's own
database name is never touched.

Gated + isolated — see ``conftest.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli import _apply_opentofu_engine as engine

from .conftest import sf_exists

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.snowflake,
    pytest.mark.slow,
]

_LOG = logging.getLogger("test.iac.snowflake.e2e")
_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "snowflake"
_SCHEMA = "S1"


def _redirected_plan(contract_name: str, live_db: str, plan_path: Path) -> Dict[str, Any]:
    """Load an example contract, point every Snowflake exposure at the
    throwaway database, and write it as a ``plan.json`` for the engine."""
    src = _EXAMPLES_DIR / contract_name / "contract.fluid.yaml"
    contract = yaml.safe_load(src.read_text(encoding="utf-8"))
    for exposure in contract.get("exposes") or []:
        location = (exposure.get("binding") or {}).get("location")
        if isinstance(location, dict):
            location["database"] = live_db
            location["schema"] = _SCHEMA
    plan_path.write_text(json.dumps({"contract": contract}), encoding="utf-8")
    return contract


def _apply_args(contract_path: Path, workspace_dir: Path, **overrides) -> argparse.Namespace:
    base: Dict[str, Any] = {
        "contract": str(contract_path),
        "env": None,
        "provider": None,
        "workspace_dir": str(workspace_dir),
        "state_backend": None,
        "dry_run": False,
        "allow_data_loss": False,
        # The test writes a hand-crafted synthetic plan.json carrying
        # only ``{"contract": ...}`` — no bundle, no actions array, no
        # bindingMode field. The OpenTofu engine's plan-binding gate
        # (added by commit 4c9163f for the security fix) would
        # otherwise reject this with
        # ``apply_plan_digest_binding_mode_missing``. This test
        # exercises the apply path, not plan-binding integrity (that
        # lives in ``test_iac_snowflake_real_cli_matrix_e2e.py`` with
        # a real bundle → plan → apply chain), so the bypass is the
        # right call here.
        "no_verify_plan_binding": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _exposed_table(contract: Dict[str, Any]) -> str | None:
    for exposure in contract.get("exposes") or []:
        location = (exposure.get("binding") or {}).get("location") or {}
        table = location.get("table") or location.get("view")
        if table:
            return table
    return None


@pytest.mark.parametrize("contract_name", ["smoke", "billing_history"])
def test_example_contract_applies_live(contract_name, live_db, sf_connection, tmp_path):
    """Each shipped ``examples/snowflake/*`` contract compiles and provisions
    cleanly through the OpenTofu apply engine."""
    plan = tmp_path / "plan.json"
    contract = _redirected_plan(contract_name, live_db, plan)

    rc = engine.apply_via_opentofu(_apply_args(plan, tmp_path), _LOG)

    assert rc == 0
    assert sf_exists(sf_connection, "DATABASES", live_db)
    table = _exposed_table(contract)
    assert table is not None, f"{contract_name}: no table/view exposure found"
    assert sf_exists(
        sf_connection, "TABLES", table, in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    ) or sf_exists(sf_connection, "VIEWS", table, in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"')


def test_example_contract_reapply_is_idempotent(live_db, sf_connection, tmp_path):
    """Re-running the engine on an already-applied example contract succeeds
    — a real contract has no perpetual diff."""
    plan = tmp_path / "plan.json"
    _redirected_plan("smoke", live_db, plan)
    args = _apply_args(plan, tmp_path)

    assert engine.apply_via_opentofu(args, _LOG) == 0
    # Second apply: same workdir + state — must re-converge without error.
    assert engine.apply_via_opentofu(args, _LOG) == 0
    assert sf_exists(sf_connection, "DATABASES", live_db)


def test_example_contract_dry_run_provisions_nothing(live_db, sf_connection, tmp_path):
    """``--dry-run`` on a real example contract plans but provisions nothing."""
    plan = tmp_path / "plan.json"
    _redirected_plan("billing_history", live_db, plan)

    rc = engine.apply_via_opentofu(_apply_args(plan, tmp_path, dry_run=True), _LOG)

    assert rc == 0
    assert not sf_exists(sf_connection, "DATABASES", live_db)
