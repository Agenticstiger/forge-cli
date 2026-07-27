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

"""Regression: ``fluid apply --mode amend-and-build`` id path-traversal.

The amend-and-build runtime path loads a contract WITHOUT jsonschema
validation (``build_runners.base.run_builds_from_args`` reads a plan.json
and pulls ``plan["contract"]`` verbatim). ``contract['id']`` and each
``build['id']`` then flow into ``RunContext.product_id`` / ``build_id``
and ``FileStateStore._build_dir``, which joins them as
``<root>/runs/<product_id>/<build_id>`` with ``parents=True``. A prior
version applied NO sanitisation, so a contract with
``id='../../../../tmp/escape'`` wrote JSON OUTSIDE the workspace
(fail-OPEN); the parallel pipeline (``cli/_acquisition_stage_ext``)
already validated the same fields, so this was a one-sided-guard
regression.

This file pins the fail-CLOSED behaviour:

* a traversal/absolute ``contract.id`` or ``build.id`` is rejected at the
  ``run_builds_from_args`` chokepoint BEFORE any out-of-workspace path is
  created (a sentinel directory that must not exist afterwards), and
* a normal id still flows through the guard.

These tests deliberately do NOT require any optional acquisition extra —
the guard fires before any runner import — so the regression is covered
in the base CI install too.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from fluid_build.build_runners import base as base_mod
from fluid_build.build_runners._ids import IdentifierViolation, validate_identifier
from fluid_build.build_runners._state import FileStateStore
from fluid_build.build_runners.base import run_builds_from_args


def _args(contract_path: Path) -> argparse.Namespace:
    """A minimal ``argparse.Namespace`` matching what apply passes in."""
    return argparse.Namespace(
        contract=str(contract_path),
        build_id=None,
        dry_run=False,
        fail_fast=False,
        delay=0,
        no_output=True,
        mode="amend-and-build",
        env=None,
        sample_rows=None,
    )


def _write_plan(workspace: Path, *, product_id: str, build_id: str) -> Path:
    """Write a plan.json (the amend-and-build input shape) embedding a
    contract whose ``id`` / ``builds[].id`` are attacker-controlled.

    The embedded build is a plain python-engine build so that, IF the
    guard ever failed to fire, dispatch would reach the state store and
    attempt to create ``<workspace>/.fluid/runs/<id>/...`` — surfacing the
    escape via the sentinel assertion.
    """
    plan_path = workspace / "runtime" / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan = {
        "contract": {
            "id": product_id,
            "builds": [
                {
                    "id": build_id,
                    "engine": "python",
                    "pattern": "acquisition",
                    "properties": {"source": {"kind": "rest"}},
                }
            ],
        }
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path


# ── chokepoint: traversal is rejected before any path is created ──────────


def test_traversal_contract_id_rejected_before_path_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "escape"  # ../../../../<sentinel> target, must not appear
    assert not sentinel.exists()

    traversal_id = "../../../../" + str(sentinel.name)
    plan_path = _write_plan(workspace, product_id=traversal_id, build_id="ok_build")

    # If the guard fails to fire and dispatch is reached, this trips.
    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("dispatch reached: id guard ran AFTER path creation")

    monkeypatch.setattr(base_mod, "_execute_acquisition_build", _boom)

    with pytest.raises(IdentifierViolation, match="contract.id"):
        run_builds_from_args(_args(plan_path), logging.getLogger("test"))

    assert not sentinel.exists(), "contract.id traversal escaped the workspace"
    # The state store directory tree must not have been created either.
    assert not (workspace / ".fluid" / "runs").exists()


def test_traversal_build_id_rejected_before_path_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "escape_build"
    assert not sentinel.exists()

    traversal_build = "../../../../" + str(sentinel.name)
    plan_path = _write_plan(workspace, product_id="good_product", build_id=traversal_build)

    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("dispatch reached: id guard ran AFTER path creation")

    monkeypatch.setattr(base_mod, "_execute_acquisition_build", _boom)

    with pytest.raises(IdentifierViolation, match="build.id"):
        run_builds_from_args(_args(plan_path), logging.getLogger("test"))

    assert not sentinel.exists(), "build.id traversal escaped the workspace"
    assert not (workspace / ".fluid" / "runs").exists()


def test_absolute_build_id_rejected_before_path_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = tmp_path / "abs_escape"
    abs_id = str(sentinel)  # an absolute path is also not a valid identifier
    assert Path(abs_id).is_absolute()

    plan_path = _write_plan(workspace, product_id="good_product", build_id=abs_id)

    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("dispatch reached: id guard ran AFTER path creation")

    monkeypatch.setattr(base_mod, "_execute_acquisition_build", _boom)

    with pytest.raises(IdentifierViolation, match="build.id"):
        run_builds_from_args(_args(plan_path), logging.getLogger("test"))

    assert not sentinel.exists()
    assert not (workspace / ".fluid" / "runs").exists()


# ── positive: a normal id flows through the guard ────────────────────────


def test_normal_ids_pass_the_guard(tmp_path: Path) -> None:
    """A valid contract.id + build.id pass the chokepoint and proceed.

    The build points at a non-existent python script so the python runner
    branch reports it as skipped — proving the guard let a normal id through
    (it did not raise). ``--allow-skipped-builds`` is set because an
    all-skipped build run is itself an error now (green-on-nothing guard);
    this test is about the identifier chokepoint, not that exit code.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract_path = workspace / "contract.fluid.yaml"
    # Plain YAML contract (the standard path), build with a valid id and a
    # missing script so dispatch resolves to "skipped" (rc 0).
    contract_path.write_text(
        "id: orders.bronze\n"
        "builds:\n"
        "  - id: ingest_orders\n"
        "    engine: python\n"
        "    repository: ./\n",
        encoding="utf-8",
    )

    args = _args(contract_path)
    args.allow_skipped_builds = True
    rc = run_builds_from_args(args, logging.getLogger("test"))
    assert rc == 0


# ── belt-and-suspenders: _state confines an unvalidated component ─────────


def test_state_store_confines_unvalidated_component(tmp_path: Path) -> None:
    """Second wall: ``FileStateStore`` rejects a traversal even if a future
    caller bypasses the chokepoint and feeds an unvalidated component.
    """
    store = FileStateStore(tmp_path / "ws" / ".fluid")
    with pytest.raises(IdentifierViolation, match="escapes the state-store root"):
        store._build_dir("../../../../tmp/escape", "build")


def test_lock_path_confines_traversal_resource_id(tmp_path: Path) -> None:
    """``_lock_path`` was the only path helper not wrapped in ``_confine``;
    a ``resource_id`` with enough ``../`` to climb out of the state-store
    root must now be rejected, exactly like the sibling helpers.
    """
    store = FileStateStore(tmp_path / "ws" / ".fluid")
    with pytest.raises(IdentifierViolation, match="escapes the state-store root"):
        store._lock_path("acq", "../../../../tmp/escape")
    # A traversal smuggled through the ``scope`` component is rejected too.
    with pytest.raises(IdentifierViolation, match="escapes the state-store root"):
        store._lock_path("../../../../tmp/escape", "orders")


def test_lock_path_allows_normal_resource_id(tmp_path: Path) -> None:
    """Positive control: a normal scope/resource_id still resolves to a lock
    file strictly inside the state-store root (no false-positive rejection)."""
    root = tmp_path / "ws" / ".fluid"
    store = FileStateStore(root)
    p = store._lock_path("acquisition", "orders.bronze")
    assert p.name == "acquisition__orders.bronze.lock"
    # Resolved path stays under the (resolved) state-store root.
    assert p.is_relative_to(root.resolve())


def test_acquire_lock_rejects_traversal_resource_id(tmp_path: Path) -> None:
    """End-to-end: ``acquire_lock`` (which calls ``_lock_path``) refuses a
    traversal ``resource_id`` before creating any lock file outside root."""
    store = FileStateStore(tmp_path / "ws" / ".fluid")
    sentinel = tmp_path / "lock_escape"
    assert not sentinel.exists()
    with pytest.raises(IdentifierViolation, match="escapes the state-store root"):
        with store.acquire_lock("acq", "../../../../" + sentinel.name):
            pass  # pragma: no cover — must never enter the body
    assert not (sentinel.with_suffix(".lock")).exists()


def test_shared_validator_accepts_normal_and_rejects_bad() -> None:
    """Unit-pin the hoisted validator's grammar at its new home."""
    assert validate_identifier("orders.bronze", kind="contract.id") == "orders.bronze"
    assert validate_identifier("ingest_orders-v2", kind="build.id") == "ingest_orders-v2"
    for bad in ("../escape", "/abs/path", "a/b", "a\\b", ".hidden", "a" * 200, ""):
        with pytest.raises(IdentifierViolation):
            validate_identifier(bad, kind="contract.id")
