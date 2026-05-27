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

"""``detect_stale_contract`` — three baseline cases:

* Same contract on disk + matching cursor hash → not stale.
* Edited contract → stale, summary names the cursor stage.
* No prior checkpoints → not stale (clean state).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.copilot.checkpoint import FileCheckpointStore
from fluid_build.copilot.checkpoint_stale import detect_stale_contract


def _hash(contract: Dict[str, Any]) -> str:
    blob = yaml.safe_dump(contract, sort_keys=True, default_flow_style=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_contract(path: Path, contract: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(contract, sort_keys=True, default_flow_style=False))


# ---------------------------------------------------------------------
# Three baseline cases
# ---------------------------------------------------------------------


def test_matching_hash_is_not_stale(tmp_path):
    """Cursor hash matches on-disk hash → not stale."""
    contract = {
        "fluidVersion": "0.7.3",
        "id": "orders",
        "metadata": {"layer": "Bronze", "productType": "SDP"},
    }
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_contract(contract_path, contract)
    saver = FileCheckpointStore(workspace_root=tmp_path)
    run_id = "run-match"
    # Lay down a checkpoint with the matching hash.
    saver.put(run_id, "contract_forge", contract, cost_usd=0.01, contract_hash=_hash(contract))

    is_stale, summary = detect_stale_contract(saver, run_id, contract_path)
    assert is_stale is False
    assert summary is None


def test_edited_contract_is_stale(tmp_path):
    """Cursor was taken against contract A; B is on disk → stale."""
    contract_v1 = {
        "fluidVersion": "0.7.3",
        "id": "orders",
        "metadata": {"layer": "Bronze", "productType": "SDP"},
    }
    contract_v2 = {
        "fluidVersion": "0.7.3",
        "id": "orders",
        "metadata": {"layer": "Silver", "productType": "ADP"},
    }
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_contract(contract_path, contract_v2)  # operator edited
    saver = FileCheckpointStore(workspace_root=tmp_path)
    run_id = "run-edit"
    # Cursor was taken against v1 — the run has progressed past
    # contract_forge so later stages also carry v1's hash.
    saver.put(
        run_id, "contract_forge", contract_v1, cost_usd=0.01, contract_hash=_hash(contract_v1)
    )
    saver.put(run_id, "builder", {"sql": "..."}, cost_usd=0.02, contract_hash=_hash(contract_v1))

    is_stale, summary = detect_stale_contract(saver, run_id, contract_path)
    assert is_stale is True
    assert summary is not None
    # Summary names the latest cursor stage that carried the hash —
    # later stages always re-stamp, so builder wins over contract_forge.
    assert "builder" in summary
    assert "sha256=" in summary


def test_no_prior_checkpoints_is_not_stale(tmp_path):
    """Fresh workspace + no prior checkpoints → not stale.

    The detector treats "no reference hash to compare against" the
    same as "clean run". The caller's resume picker is the right
    place to handle the "no run to resume" branch.
    """
    saver = FileCheckpointStore(workspace_root=tmp_path)
    # No saver.put() calls.
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_contract(contract_path, {"id": "orders"})

    is_stale, summary = detect_stale_contract(saver, "run-empty", contract_path)
    assert is_stale is False
    assert summary is None


# ---------------------------------------------------------------------
# Edge cases — defensive contract
# ---------------------------------------------------------------------


def test_logical_only_run_does_not_trigger_stale(tmp_path):
    """The ``logical`` stage legitimately has ``contract_hash=None``
    (no contract yet). A run that's only completed ``logical`` must
    not be flagged stale on resume."""
    saver = FileCheckpointStore(workspace_root=tmp_path)
    run_id = "run-logical-only"
    saver.put(run_id, "logical", {"name": "orders"}, cost_usd=0.01, contract_hash=None)
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_contract(contract_path, {"id": "orders"})

    is_stale, summary = detect_stale_contract(saver, run_id, contract_path)
    assert is_stale is False
    assert summary is None


def test_missing_contract_path_is_not_stale(tmp_path):
    """No file on disk to compare against → clean state, not stale."""
    contract = {"id": "orders"}
    saver = FileCheckpointStore(workspace_root=tmp_path)
    saver.put("run-x", "contract_forge", contract, cost_usd=0.01, contract_hash=_hash(contract))

    # current_contract_path=None
    is_stale, summary = detect_stale_contract(saver, "run-x", None)
    assert is_stale is False
    assert summary is None

    # Path that doesn't exist.
    is_stale, summary = detect_stale_contract(saver, "run-x", tmp_path / "missing.fluid.yaml")
    assert is_stale is False
    assert summary is None
