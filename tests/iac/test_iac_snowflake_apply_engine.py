# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The ``fluid apply`` OpenTofu engine, end-to-end for Snowflake.

These tests drive ``apply_via_opentofu`` — the real CLI entrypoint — not
just the plugin: contract → ``.tf.json`` → ``tofu init/plan/apply``,
including the data-loss gate, ``--dry-run``, and brownfield adoption.

The contract is handed over as a ``plan.json`` (``{"contract": {...}}``),
the engine's own no-schema-validation load path, so the tests stay focused
on the apply engine. Live tests are gated + isolated — see ``conftest.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.providers.base import ProviderError

from .conftest import create_container, sf_exists, table_contract

pytestmark = [pytest.mark.provider]

_LOG = logging.getLogger("test.iac.snowflake.engine")


def _write_plan(path: Path, contract: Dict[str, Any]) -> Path:
    """Write a contract as a ``plan.json`` the engine loads without schema
    validation — keeps these tests scoped to the apply engine itself."""
    path.write_text(json.dumps({"contract": contract}), encoding="utf-8")
    return path


def _apply_args(contract_path: Path, workspace_dir: Path, **overrides) -> argparse.Namespace:
    base: Dict[str, Any] = {
        "contract": str(contract_path),
        "env": None,
        "provider": None,
        "workspace_dir": str(workspace_dir),
        "state_backend": None,
        "dry_run": False,
        "allow_data_loss": False,
        # Synthetic plan.json written by these tests carries
        # ``{"contract": ...}`` only — no bundle, no actions, no
        # bindingMode. The OpenTofu engine's plan-binding gate (added
        # by commit 4c9163f) would otherwise reject with
        # ``apply_plan_digest_binding_mode_missing``. The plan-binding
        # security boundary is pinned by
        # ``test_iac_snowflake_real_cli_matrix_e2e.py`` with a real
        # bundle → plan → apply chain — this file exercises resource
        # provisioning, so the bypass is correct.
        "no_verify_plan_binding": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# Engine resolution + the retired native path (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_apply_engine_is_opentofu_for_snowflake(tmp_path):
    """A Snowflake contract resolves to the OpenTofu engine — Snowflake is
    a cut-over provider, so ``fluid apply`` routes it through ``tofu``."""
    plan = _write_plan(tmp_path / "plan.json", table_contract("DB", "SC", "T"))
    args = argparse.Namespace(contract=str(plan), env=None, provider=None)
    assert engine.resolve_apply_engine(args, _LOG) == "opentofu"


@pytest.mark.unit
def test_native_snowflake_apply_is_retired():
    """The native Snowflake apply path raises instead of silently doing
    nothing — a contract is never reported 'applied' without provisioning."""
    from fluid_build.providers.snowflake.provider import SnowflakeProviderEnhanced

    provider = SnowflakeProviderEnhanced(account="TESTACCT", database="TESTDB", schema="PUBLIC")
    with pytest.raises(ProviderError, match="retired"):
        provider.apply([])


# ---------------------------------------------------------------------------
# Live apply-engine round-trips
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.snowflake
@pytest.mark.slow
def test_apply_engine_provisions_snowflake(live_db, sf_connection, tmp_path):
    """``apply_via_opentofu`` compiles a Snowflake contract and provisions
    real objects through ``tofu``."""
    contract = table_contract(live_db, "S1", "EVENTS", cid="iac.engine.live")
    plan = _write_plan(tmp_path / "plan.json", contract)

    rc = engine.apply_via_opentofu(_apply_args(plan, tmp_path), _LOG)

    assert rc == 0
    assert sf_exists(sf_connection, "DATABASES", live_db)
    assert sf_exists(sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."S1"')


@pytest.mark.integration
@pytest.mark.snowflake
@pytest.mark.slow
def test_apply_engine_dry_run_provisions_nothing(live_db, sf_connection, tmp_path):
    """``--dry-run`` stops at ``tofu plan`` — the review point — and creates
    nothing in the account."""
    contract = table_contract(live_db, "S1", "EVENTS", cid="iac.engine.dryrun")
    plan = _write_plan(tmp_path / "plan.json", contract)

    rc = engine.apply_via_opentofu(_apply_args(plan, tmp_path, dry_run=True), _LOG)

    assert rc == 0
    assert not sf_exists(sf_connection, "DATABASES", live_db)


@pytest.mark.integration
@pytest.mark.snowflake
@pytest.mark.slow
def test_apply_engine_data_loss_gate_blocks_then_allows(live_db, sf_connection, tmp_path):
    """A plan that destroys a resource is blocked closed — ``tofu`` has no
    data snapshot — and proceeds only with ``--allow-data-loss``."""
    cid = "iac.engine.dlgate"
    # Apply A: database + schema + table.
    plan_a = _write_plan(tmp_path / "a.json", table_contract(live_db, "S1", "EVENTS", cid=cid))
    assert engine.apply_via_opentofu(_apply_args(plan_a, tmp_path), _LOG) == 0
    assert sf_exists(sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."S1"')

    # Apply B: same contract id (so it shares A's workdir + state) with the
    # table dropped — the exposure keeps only database + schema.
    contract_b = {
        "id": cid,
        "name": "drop the table",
        "exposes": [
            {
                "exposeId": "t",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": live_db, "schema": "S1"},
                },
            }
        ],
    }
    plan_b = _write_plan(tmp_path / "b.json", contract_b)

    # The table removal trips the data-loss gate.
    with pytest.raises(CLIError) as excinfo:
        engine.apply_via_opentofu(_apply_args(plan_b, tmp_path), _LOG)
    assert "data" in str(excinfo.value).lower() or "destroy" in str(excinfo.value).lower()
    assert sf_exists(
        sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."S1"'
    ), "the table must survive a blocked apply"

    # --allow-data-loss lets the destructive plan through.
    rc = engine.apply_via_opentofu(_apply_args(plan_b, tmp_path, allow_data_loss=True), _LOG)
    assert rc == 0
    assert not sf_exists(sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."S1"')


@pytest.mark.integration
@pytest.mark.snowflake
@pytest.mark.slow
def test_apply_engine_adopts_brownfield_resources(live_db, sf_connection, tmp_path):
    """A database that already exists is adopted via ``tofu import`` — the
    apply reconciles brownfield infrastructure instead of failing
    'object already exists'."""
    # Pre-create the database + schema out of band.
    create_container(sf_connection, live_db, "S1")

    contract = table_contract(live_db, "S1", "EVENTS", cid="iac.engine.brownfield")
    plan = _write_plan(tmp_path / "plan.json", contract)

    rc = engine.apply_via_opentofu(_apply_args(plan, tmp_path), _LOG)

    assert rc == 0  # pre-existing database adopted, not a fatal "already exists"
    assert sf_exists(sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."S1"')
