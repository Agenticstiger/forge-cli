# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Stage 3 — Snowflake CLI-surface matrix on real Snowflake.

Mirrors ``test_iac_aws_real_cli_matrix_e2e.py`` and
``test_iac_gcp_real_cli_matrix_e2e.py`` for the security-critical
pair:

  * ``test_real_cli_apply_plan_binding_tamper_rejected_snowflake`` —
    tampered plan.json is rejected with no DDL executed.
  * ``test_real_cli_apply_no_verify_plan_binding_bypass_snowflake`` —
    documented DR escape hatch lets a tampered plan apply.

Plan-binding is provider-agnostic (the same
``_verify_plan_binding_for_opentofu`` gate in the apply engine fires
regardless of plugin), but AWS+GCP have their own audit-trail E2E
pins — Snowflake was missing one. This file closes that gap.

Uses the same throwaway-database isolation as
``test_iac_snowflake_live.py`` (a unique ``FLUID_IACTEST_*`` database
the test creates + drops). Triple-gated:
``tofu`` + Snowflake creds + ``FLUID_IAC_LIVE_SNOWFLAKE=1``.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pytest
import yaml

from .conftest import (
    LIVE_SKIP_REASON,
    LIVE_SNOWFLAKE_ENABLED,
    sf_exists,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.snowflake,
    pytest.mark.slow,
    pytest.mark.skipif(not LIVE_SNOWFLAKE_ENABLED, reason=LIVE_SKIP_REASON),
]


def _fluid(
    *args: str,
    cwd: Path,
    env_overrides: Optional[Mapping[str, str]] = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess:
    """Invoke `fluid` as a subprocess (subprocess-level dispatch — not a
    Python-level call into runner.tofu_apply) so we exercise the full
    CLI dispatch chain that real users hit."""
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _live_env() -> Dict[str, str]:
    """Snowflake live env — the SNOWFLAKE_* vars + the opt-in flag."""
    overrides: Dict[str, str] = {}
    for var in (
        "FLUID_IAC_LIVE_SNOWFLAKE",
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_PRIVATE_KEY_PATH",
        "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
        "SNOWFLAKE_AUTHENTICATOR",
        "SNOWFLAKE_OAUTH_TOKEN",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_ROLE",
    ):
        v = os.environ.get(var)
        if v:
            overrides[var] = v
    return overrides


def _write_contract(workdir: Path, contract: Dict[str, Any]) -> Path:
    contract = {"fluidVersion": "0.7.3", "kind": "DataProduct", **contract}
    contract.setdefault("metadata", {"layer": "Silver", "owner": {"team": "data-eng"}})
    target = workdir / "contract.fluid.yaml"
    target.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return target


def _build_table_contract(db: str, *, cid: str = "iac.snowflake.cli.tamper") -> Dict[str, Any]:
    """A minimal Snowflake contract — one table — cheap to apply and
    destroy. The CLI-matrix focus is the dispatch chain, not the
    resource shape, so we use the smallest viable contract."""
    return {
        "id": cid,
        "name": "CLI matrix Snowflake",
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": db, "schema": "S1", "table": "EVENTS"},
                },
                "contract": {
                    "schema": [
                        {"name": "ID", "type": "string", "required": True},
                        {"name": "TS", "type": "timestamp"},
                    ]
                },
            }
        ],
    }


def _bundle_then_plan(
    workdir: Path, *, bundle_name: str = "bundle.tgz", plan_name: str = "plan.json"
) -> tuple[Path, Path]:
    """``fluid bundle`` + ``fluid plan <bundle>`` — the canonical chain
    that populates both ``bundleDigest`` and ``planDigest`` on the plan."""
    rc = _fluid(
        "bundle",
        "contract.fluid.yaml",
        "--out",
        bundle_name,
        "--format",
        "tgz",
        cwd=workdir,
        env_overrides=_live_env(),
        timeout=180,
    )
    assert rc.returncode == 0, f"fluid bundle failed:\n{rc.stderr or rc.stdout}"
    bundle_path = workdir / bundle_name
    assert bundle_path.exists()

    rc = _fluid(
        "plan",
        bundle_name,
        "--out",
        plan_name,
        cwd=workdir,
        env_overrides=_live_env(),
        timeout=180,
    )
    assert rc.returncode == 0, f"fluid plan failed:\n{rc.stderr or rc.stdout}"
    plan_path = workdir / plan_name
    assert plan_path.exists()
    return bundle_path, plan_path


def _tamper_plan_json(plan_path: Path) -> None:
    """Inject a fake action — mutates the actions array hash so the
    planDigest no longer matches the actual content."""
    doc = json.loads(plan_path.read_text())
    actions = doc.setdefault("actions", [])
    actions.append({"op": "_fluid_test_injection", "tampered": True})
    plan_path.write_text(json.dumps(doc))


@pytest.fixture()
def snowflake_cli_db(sf_connection, tmp_path):
    """A fresh throwaway Snowflake database for this test. Yields
    `(database_name, workdir_path)`. Teardown drops the database
    unconditionally (DROP DATABASE IF EXISTS) so leaked state from a
    crashed apply still gets cleaned up."""
    db_name = f"FLUID_IACTEST_CLI_{uuid.uuid4().hex[:10].upper()}"
    try:
        yield db_name, tmp_path
    finally:
        with contextlib.closing(sf_connection.cursor()) as cur, contextlib.suppress(Exception):
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" CASCADE')


def test_real_cli_apply_plan_binding_tamper_rejected_snowflake(snowflake_cli_db, sf_connection):
    """A plan.json tampered after `fluid bundle + fluid plan` MUST be
    rejected by ``fluid apply`` — the OpenTofu engine's
    ``_verify_plan_binding_for_opentofu`` gate must fire and produce
    a non-zero exit. Zero DDL should execute (no database created).

    Pins the security boundary on Snowflake. The shared apply-engine
    code path is the same as AWS+GCP, but having a Snowflake-specific
    pin closes the audit-trail gap so a future change can't silently
    disable plan-binding for one provider only.
    """
    db, workdir = snowflake_cli_db
    _write_contract(workdir, _build_table_contract(db))
    _, plan_path = _bundle_then_plan(workdir)
    _tamper_plan_json(plan_path)

    apply_rc = _fluid(
        "apply",
        "plan.json",
        "--mode",
        "amend",
        "--yes",
        cwd=workdir,
        env_overrides=_live_env(),
        timeout=180,
    )
    assert apply_rc.returncode != 0, (
        "fluid-apply ACCEPTED a tampered plan.json on Snowflake — "
        f"plan-binding broken!\nstdout:\n{apply_rc.stdout[-2000:]}"
    )
    combined = (apply_rc.stdout + apply_rc.stderr).lower()
    assert (
        "planbinding" in combined
        or "plan_tamper" in combined
        or "plan binding" in combined
        or "digest" in combined
    ), f"expected plan-binding error keyword. last 2000 chars:\n{combined[-2000:]}"

    # The database MUST NOT exist — the verify step blocks BEFORE any DDL.
    assert not sf_exists(
        sf_connection, "DATABASES", db
    ), f"tampered apply created database {db!r} on Snowflake — gate not blocking"


def test_real_cli_apply_no_verify_plan_binding_bypass_snowflake(snowflake_cli_db, sf_connection):
    """``--no-verify-plan-binding`` is the documented DR escape hatch:
    it lets fluid-apply proceed against a corrupt-digest plan, with
    a WARNING log line for audit trails. Verified by applying a
    tampered plan with the bypass flag — the database lands.
    """
    db, workdir = snowflake_cli_db
    _write_contract(workdir, _build_table_contract(db))
    _, plan_path = _bundle_then_plan(workdir)
    _tamper_plan_json(plan_path)

    rc = _fluid(
        "apply",
        "plan.json",
        "--mode",
        "amend",
        "--no-verify-plan-binding",
        "--yes",
        cwd=workdir,
        env_overrides=_live_env(),
        timeout=300,
    )
    assert rc.returncode == 0, (
        "--no-verify-plan-binding should let a tampered plan apply on Snowflake.\n"
        f"stdout:\n{rc.stdout[-2000:]}\nstderr:\n{rc.stderr[-1000:]}"
    )

    # The database WAS created — the bypass let the apply through.
    assert sf_exists(
        sf_connection, "DATABASES", db
    ), f"bypass apply should have created database {db!r} on Snowflake"
