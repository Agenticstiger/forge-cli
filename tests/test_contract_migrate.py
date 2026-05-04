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

"""Tests for ``fluid contract migrate-product-type`` (Phase 1.2).

The verb walks ``**/*.fluid.yaml`` under ``--root`` and fills the
missing twin of ``metadata.layer`` / ``metadata.productType`` per the
equivalence axiom (Bronze↔SDP, Silver↔ADP, Gold↔CDP). Pin:

1. **Dry-run leaves files alone** + reports what would change.
2. **`--write` actually rewrites** + the rewritten file passes
   schema validation.
3. **`--check` exits non-zero** when contracts still need migration
   and ``--write`` wasn't passed (CI gate behaviour).
4. **Already-complete contracts are no-ops** — exit 0, nothing
   changed.
5. **Bidirectional fill** — works whichever twin is missing.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli.contract import _run_migrate_product_type
from fluid_build.schema_manager import FluidSchemaManager

_FLUID_VERSION = FluidSchemaManager.latest_bundled_version()


def _layer_only_contract() -> Dict[str, Any]:
    """A contract authored with only ``metadata.layer`` set."""
    return {
        "fluidVersion": _FLUID_VERSION,
        "kind": "DataProduct",
        "id": "test.migrate.target",
        "name": "Migrate Target",
        "description": "Has layer but missing productType.",
        "domain": "test",
        "metadata": {
            "owner": {"team": "test", "email": "test@example.com"},
            "layer": "Bronze",
            # productType missing — the migration verb's job to fill.
        },
        "consumes": [],
        "builds": [
            {
                "id": "main",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": "SELECT 1 AS id"},
                "execution": {
                    "trigger": {"type": "manual", "iterations": 1},
                    "runtime": {
                        "platform": "local",
                        "resources": {"cpu": "1", "memory": "1Gi"},
                    },
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "out",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "runtime/out/x.parquet"},
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
        ],
    }


def _product_type_only_contract() -> Dict[str, Any]:
    """Same shape as above but only ``metadata.productType`` set."""
    c = _layer_only_contract()
    c["metadata"].pop("layer", None)
    c["metadata"]["productType"] = "ADP"
    c["id"] = "test.migrate.adp_target"
    return c


def _complete_contract() -> Dict[str, Any]:
    c = _layer_only_contract()
    c["metadata"]["productType"] = "SDP"
    c["id"] = "test.migrate.complete"
    return c


def _write_yaml(path: Path, contract: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")


def _args(
    root: Path, *, write: bool = False, check: bool = False, yes: bool = True
) -> argparse.Namespace:
    """Build a Namespace mirroring the argparse output for migrate-product-type.

    ``yes`` defaults to True so non-interactive test runs pass the
    security gate that refuses ``--write`` without ``--yes`` when
    stdin / stdout aren't both TTYs (S-015 in the security review —
    the gate exists to prevent piped CI inputs from auto-confirming
    a destructive rewrite).
    """
    return argparse.Namespace(root=str(root), write=write, check=check, yes=yes)


# ---------------------------------------------------------------------------
# Behaviour 1 — dry-run leaves files alone
# ---------------------------------------------------------------------------


def test_migrate_dry_run_does_not_mutate_files(tmp_path):
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_yaml(contract_path, _layer_only_contract())
    snapshot = contract_path.read_bytes()

    rc = _run_migrate_product_type(_args(tmp_path), logging.getLogger("test"))
    assert rc == 0
    # File is byte-identical to the snapshot.
    assert contract_path.read_bytes() == snapshot


# ---------------------------------------------------------------------------
# Behaviour 2 — --write actually rewrites
# ---------------------------------------------------------------------------


def test_migrate_write_fills_missing_product_type(tmp_path):
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_yaml(contract_path, _layer_only_contract())

    rc = _run_migrate_product_type(_args(tmp_path, write=True), logging.getLogger("test"))
    assert rc == 0

    after = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert after["metadata"]["layer"] == "Bronze"
    assert after["metadata"]["productType"] == "SDP"

    # Schema validates clean after migration.
    res = FluidSchemaManager().validate_contract(after)
    assert res.is_valid, res.errors


# ---------------------------------------------------------------------------
# Behaviour 3 — --check exits non-zero when migration would change something
# ---------------------------------------------------------------------------


def test_migrate_check_exits_nonzero_when_migration_pending(tmp_path):
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_yaml(contract_path, _layer_only_contract())

    rc = _run_migrate_product_type(_args(tmp_path, check=True), logging.getLogger("test"))
    assert rc == 1  # CI gate fires


def test_migrate_check_exits_zero_when_nothing_pending(tmp_path):
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_yaml(contract_path, _complete_contract())

    rc = _run_migrate_product_type(_args(tmp_path, check=True), logging.getLogger("test"))
    assert rc == 0


# ---------------------------------------------------------------------------
# Behaviour 4 — already-complete contracts are no-ops
# ---------------------------------------------------------------------------


def test_migrate_skips_already_complete_contracts(tmp_path):
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_yaml(contract_path, _complete_contract())
    snapshot = contract_path.read_bytes()

    rc = _run_migrate_product_type(_args(tmp_path, write=True), logging.getLogger("test"))
    assert rc == 0
    # Even with --write, byte-identical file because nothing changed.
    assert contract_path.read_bytes() == snapshot


# ---------------------------------------------------------------------------
# Behaviour 5 — bidirectional fill
# ---------------------------------------------------------------------------


def test_migrate_fills_layer_when_only_product_type_set(tmp_path):
    contract_path = tmp_path / "contract.fluid.yaml"
    _write_yaml(contract_path, _product_type_only_contract())

    rc = _run_migrate_product_type(_args(tmp_path, write=True), logging.getLogger("test"))
    assert rc == 0

    after = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert after["metadata"]["layer"] == "Silver"  # filled from ADP
    assert after["metadata"]["productType"] == "ADP"


# ---------------------------------------------------------------------------
# Behaviour 6 — walks subdirectories + skips .git/__pycache__
# ---------------------------------------------------------------------------


def test_migrate_walks_nested_directories(tmp_path):
    nested = tmp_path / "products" / "bronze" / "stripe"
    _write_yaml(nested / "contract.fluid.yaml", _layer_only_contract())

    # Throw a noisy directory in the way; the walker should skip it.
    skip_dir = tmp_path / ".git" / "objects"
    skip_dir.mkdir(parents=True)
    _write_yaml(skip_dir / "should_be_skipped.fluid.yaml", _layer_only_contract())

    rc = _run_migrate_product_type(_args(tmp_path, write=True), logging.getLogger("test"))
    assert rc == 0

    after_nested = yaml.safe_load((nested / "contract.fluid.yaml").read_text(encoding="utf-8"))
    assert after_nested["metadata"]["productType"] == "SDP"
    # The .git path was untouched.
    skipped = yaml.safe_load(
        (skip_dir / "should_be_skipped.fluid.yaml").read_text(encoding="utf-8")
    )
    assert "productType" not in skipped["metadata"]


# ---------------------------------------------------------------------------
# Behaviour 7 (S-015) — refuse non-interactive --write without --yes
# ---------------------------------------------------------------------------


def test_migrate_write_without_yes_refuses_non_interactive_run(tmp_path):
    """Security gate: when stdin or stdout is not a TTY (CI runner,
    piped input, redirected output), ``--write`` MUST NOT proceed
    without an explicit ``--yes`` flag. Otherwise a malicious upstream
    could pipe ``echo y`` into the prompt and auto-confirm a
    destructive rewrite."""
    from fluid_build.cli._common import CLIError

    contract_path = tmp_path / "contract.fluid.yaml"
    _write_yaml(contract_path, _layer_only_contract())
    snapshot = contract_path.read_bytes()

    # ``yes=False`` simulates the operator forgetting --yes in a CI run.
    with pytest.raises(CLIError) as exc_info:
        _run_migrate_product_type(
            _args(tmp_path, write=True, yes=False),
            logging.getLogger("test"),
        )

    assert exc_info.value.event == "interactive_write_requires_yes"
    # File is byte-identical — the gate fired before any write.
    assert contract_path.read_bytes() == snapshot
