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

"""Tests for the contract <-> dbt reconciliation used by ``fluid verify``.

The reconcile helper cross-checks a data product's FLUID contract
(``exposes[].contract.schema``) against the columns declared in the build's
dbt project (``models/**/schema.yml``) and surfaces DRIFT:

- a contract column missing from the dbt model     (``missing_in_dbt``)
- a dbt model column not declared in the contract  (``missing_in_contract``)
- a declared type that disagrees                    (``type_mismatch``)
- an expose with no matching dbt model              (``model_missing_in_dbt``)
- a ``access: public`` dbt model with no expose     (``model_missing_in_contract``)
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from fluid_build.cli._verify_reconcile import (
    ReconcileReport,
    load_dbt_schema_models,
    normalize_type,
    reconcile_contract_dbt,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_dbt_project(root: Path, schema_models: list, project_name: str = "demo") -> None:
    """Write a minimal dbt project (``dbt_project.yml`` + ``models/marts/schema.yml``)."""
    (root / "dbt_project.yml").write_text(
        f"name: {project_name}\nversion: '1.0.0'\nprofile: {project_name}\n",
        encoding="utf-8",
    )
    marts = root / "models" / "marts"
    marts.mkdir(parents=True, exist_ok=True)
    (marts / "schema.yml").write_text(
        yaml.safe_dump({"version": 2, "models": schema_models}, sort_keys=False),
        encoding="utf-8",
    )


def _contract(exposes, builds=None):
    return {
        "id": "recon-test",
        "builds": (
            builds
            if builds is not None
            else [{"id": "b1", "engine": "dbt", "repository": ".", "outputs": None}]
        ),
        "exposes": exposes,
    }


def _expose(expose_id, columns, fmt="snowflake_table"):
    return {
        "exposeId": expose_id,
        "format": fmt,
        # A resolvable location: without database/schema/table the run short
        # -circuits to an ``error`` BEFORE ``verify_snowflake_table`` is
        # reached, so the stub in ``_run`` never takes effect and the error —
        # now fatal on its own — masks the reconcile gate these tests assert.
        "binding": {
            "platform": "snowflake",
            "format": fmt,
            "location": {"database": "DB", "schema": "SCH", "table": expose_id.upper()},
        },
        "contract": {"schema": columns},
    }


# ---------------------------------------------------------------------------
# normalize_type
# ---------------------------------------------------------------------------


class TestNormalizeType:
    def test_string_family(self):
        assert normalize_type("varchar(255)") == normalize_type("string") == "TEXT"

    def test_numeric_family_merges_int_and_decimal(self):
        # Conservative: int/decimal/number all collapse to one family so we
        # don't false-flag NUMBER(38,0) vs integer as a mismatch.
        assert normalize_type("integer") == normalize_type("int") == "NUMERIC"
        assert normalize_type("number") == normalize_type("decimal(18,4)") == "NUMERIC"

    def test_boolean_family(self):
        assert normalize_type("boolean") == normalize_type("bool") == "BOOLEAN"

    def test_timestamp_family(self):
        assert normalize_type("timestamp_ntz") == normalize_type("datetime") == "TIMESTAMP"

    def test_unknown_type_returns_unknown(self):
        assert normalize_type("mystery_type") == "UNKNOWN"
        assert normalize_type(None) == "UNKNOWN"


# ---------------------------------------------------------------------------
# load_dbt_schema_models
# ---------------------------------------------------------------------------


class TestLoadDbtSchemaModels:
    def test_reads_models_and_columns(self, tmp_path):
        _write_dbt_project(
            tmp_path,
            [
                {
                    "name": "orders",
                    "access": "public",
                    "columns": [
                        {"name": "order_id", "data_type": "integer"},
                        {"name": "amount", "data_type": "number"},
                    ],
                }
            ],
        )
        models = load_dbt_schema_models(tmp_path)
        assert "orders" in models
        assert models["orders"]["access"] == "public"
        assert set(models["orders"]["columns"]) == {"order_id", "amount"}
        assert models["orders"]["columns"]["order_id"]["data_type"] == "integer"

    def test_missing_models_dir_returns_empty(self, tmp_path):
        (tmp_path / "dbt_project.yml").write_text("name: x\n", encoding="utf-8")
        assert load_dbt_schema_models(tmp_path) == {}

    def test_malformed_yaml_is_skipped_not_fatal(self, tmp_path):
        _write_dbt_project(tmp_path, [{"name": "orders", "columns": []}])
        bad = tmp_path / "models" / "marts" / "broken.yml"
        bad.write_text("version: 2\nmodels: [ {{{ not yaml", encoding="utf-8")
        # Should not raise; good file still parsed.
        models = load_dbt_schema_models(tmp_path)
        assert "orders" in models


# ---------------------------------------------------------------------------
# reconcile_contract_dbt — aligned pair passes
# ---------------------------------------------------------------------------


class TestReconcileAligned:
    def test_aligned_pair_has_no_drift(self, tmp_path):
        _write_dbt_project(
            tmp_path,
            [
                {
                    "name": "orders",
                    "access": "public",
                    "columns": [
                        {"name": "order_id", "data_type": "integer"},
                        {"name": "amount", "data_type": "number"},
                    ],
                }
            ],
        )
        contract = _contract(
            [
                _expose(
                    "orders",
                    [
                        {"name": "order_id", "type": "integer"},
                        {"name": "amount", "type": "decimal"},
                    ],
                )
            ]
        )
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        assert isinstance(report, ReconcileReport)
        assert report.has_drift is False
        assert report.checked_builds == 1
        assert report.column_drifts == []
        assert report.model_drifts == []


# ---------------------------------------------------------------------------
# reconcile_contract_dbt — each drift class flagged
# ---------------------------------------------------------------------------


class TestReconcileDrift:
    def _reasons(self, report):
        return {d.reason for d in report.column_drifts} | {d.reason for d in report.model_drifts}

    def test_contract_column_missing_from_dbt(self, tmp_path):
        _write_dbt_project(
            tmp_path,
            [{"name": "orders", "access": "public", "columns": [{"name": "order_id"}]}],
        )
        contract = _contract(
            [
                _expose(
                    "orders",
                    [
                        {"name": "order_id", "type": "integer"},
                        {"name": "amount", "type": "decimal"},
                    ],
                )
            ]
        )
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        assert report.has_drift is True
        missing = [d for d in report.column_drifts if d.reason == "missing_in_dbt"]
        assert [d.column for d in missing] == ["amount"]

    def test_dbt_column_not_in_contract(self, tmp_path):
        _write_dbt_project(
            tmp_path,
            [
                {
                    "name": "orders",
                    "access": "public",
                    "columns": [{"name": "order_id"}, {"name": "secret_col"}],
                }
            ],
        )
        contract = _contract([_expose("orders", [{"name": "order_id", "type": "integer"}])])
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        missing = [d for d in report.column_drifts if d.reason == "missing_in_contract"]
        assert [d.column for d in missing] == ["secret_col"]

    def test_type_mismatch_flagged(self, tmp_path):
        _write_dbt_project(
            tmp_path,
            [
                {
                    "name": "orders",
                    "access": "public",
                    "columns": [{"name": "order_id", "data_type": "varchar"}],
                }
            ],
        )
        contract = _contract([_expose("orders", [{"name": "order_id", "type": "integer"}])])
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        mism = [d for d in report.column_drifts if d.reason == "type_mismatch"]
        assert len(mism) == 1
        assert mism[0].contract_type == "integer"
        assert mism[0].dbt_type == "varchar"

    def test_type_check_skipped_when_dbt_type_absent(self, tmp_path):
        # dbt column has no data_type -> presence-only, no type drift.
        _write_dbt_project(
            tmp_path,
            [{"name": "orders", "access": "public", "columns": [{"name": "order_id"}]}],
        )
        contract = _contract([_expose("orders", [{"name": "order_id", "type": "integer"}])])
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        assert report.has_drift is False

    def test_expose_with_no_dbt_model(self, tmp_path):
        _write_dbt_project(
            tmp_path,
            [{"name": "orders", "access": "public", "columns": [{"name": "order_id"}]}],
        )
        contract = _contract(
            [
                _expose("orders", [{"name": "order_id", "type": "integer"}]),
                _expose("customers", [{"name": "customer_id", "type": "integer"}]),
            ],
            builds=[
                {"id": "b1", "engine": "dbt", "repository": ".", "outputs": ["orders", "customers"]}
            ],
        )
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        md = [d for d in report.model_drifts if d.reason == "model_missing_in_dbt"]
        assert [d.model for d in md] == ["customers"]

    def test_public_dbt_model_not_in_contract(self, tmp_path):
        _write_dbt_project(
            tmp_path,
            [
                {"name": "orders", "access": "public", "columns": [{"name": "order_id"}]},
                {"name": "orphan_mart", "access": "public", "columns": [{"name": "x"}]},
            ],
        )
        contract = _contract([_expose("orders", [{"name": "order_id", "type": "integer"}])])
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        md = [d for d in report.model_drifts if d.reason == "model_missing_in_contract"]
        assert [d.model for d in md] == ["orphan_mart"]

    def test_protected_staging_model_not_flagged(self, tmp_path):
        # A non-public (staging) dbt model with no expose must NOT be drift —
        # only public output ports are expected to be reflected in the contract.
        _write_dbt_project(
            tmp_path,
            [
                {"name": "orders", "access": "public", "columns": [{"name": "order_id"}]},
                {"name": "stg_orders", "columns": [{"name": "raw_id"}]},  # no access -> protected
            ],
        )
        contract = _contract([_expose("orders", [{"name": "order_id", "type": "integer"}])])
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        assert report.model_drifts == []


# ---------------------------------------------------------------------------
# reconcile_contract_dbt — no dbt build / no local project
# ---------------------------------------------------------------------------


class TestReconcileNoop:
    def test_no_dbt_build_yields_note_no_drift(self, tmp_path):
        contract = {
            "id": "no-dbt",
            "builds": [{"id": "b1", "engine": "python", "repository": "."}],
            "exposes": [_expose("orders", [{"name": "id", "type": "integer"}])],
        }
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        assert report.has_drift is False
        assert report.checked_builds == 0

    def test_dbt_build_without_local_project_is_note(self, tmp_path):
        # engine dbt but no dbt_project.yml on disk -> skipped with a note.
        contract = _contract([_expose("orders", [{"name": "id", "type": "integer"}])])
        report = reconcile_contract_dbt(contract, tmp_path / "contract.fluid.yaml")
        assert report.has_drift is False
        assert report.checked_builds == 0
        assert any("dbt project" in n.lower() for n in report.notes)


# ---------------------------------------------------------------------------
# run() integration via fluid verify --reconcile-dbt
# ---------------------------------------------------------------------------


# A minimal passing warehouse-verify result: enough shape for the summary
# renderer, and explicitly NOT an error (see the patch note below).
_MATCH_RESULT = {
    "status": "match",
    "exists": True,
    "severity": {"symbol": "✅", "level": "SUCCESS", "impact": "NONE"},
    "metadata": {"num_rows": 0, "created": None, "modified": None},
    "dimensions": {
        "structure": {"status": "pass", "matching_fields": [], "total_expected": 0},
        "types": {"status": "pass", "mismatches": []},
        "constraints": {"status": "pass", "mismatches": []},
        "location": {"status": "pass", "expected": None, "actual": None, "message": None},
    },
}


class TestVerifyRunReconcileIntegration:
    def _args(self, contract, **kw):
        base = dict(
            contract=contract,
            expose_id=None,
            strict=False,
            out=None,
            show_diffs=False,
            env=None,
            reconcile_dbt=True,
            warn_only=False,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _setup(self, tmp_path, dbt_models, exposes, builds=None):
        _write_dbt_project(tmp_path, dbt_models)
        contract_path = tmp_path / "contract.fluid.yaml"
        contract_path.write_text("id: recon-test\n", encoding="utf-8")
        contract = _contract(exposes, builds=builds)
        return contract_path, contract

    def _run(self, contract_path, contract, args):
        from fluid_build.cli.verify import run

        with (
            patch(
                "fluid_build.cli.verify.load_contract_with_overlay",
                return_value=contract,
            ),
            patch(
                "fluid_build.providers.snowflake.util.config.resolve_snowflake_settings",
                return_value={"account": "a", "warehouse": "w", "user": "u"},
            ),
            # Neutralize the live warehouse verify so the run reaches reconcile.
            # It must return a PASSING result, not an ``error``: an error means
            # "we could not check at all" and now fails the run on its own
            # (cli/verify.py), which would mask what these tests assert — the
            # contract-vs-dbt reconcile gate.
            patch(
                "fluid_build.cli.verify.verify_snowflake_table",
                return_value=_MATCH_RESULT,
            ),
        ):
            return run(args, logging.getLogger("test"))

    def test_drift_returns_nonzero(self, tmp_path):
        contract_path, contract = self._setup(
            tmp_path,
            [{"name": "orders", "access": "public", "columns": [{"name": "order_id"}]}],
            [
                _expose(
                    "orders",
                    [
                        {"name": "order_id", "type": "integer"},
                        {"name": "amount", "type": "decimal"},
                    ],
                )
            ],
        )
        rc = self._run(contract_path, contract, self._args(str(contract_path)))
        assert rc == 1

    def test_aligned_returns_zero(self, tmp_path):
        contract_path, contract = self._setup(
            tmp_path,
            [{"name": "orders", "access": "public", "columns": [{"name": "order_id"}]}],
            [_expose("orders", [{"name": "order_id", "type": "integer"}])],
        )
        rc = self._run(contract_path, contract, self._args(str(contract_path)))
        assert rc == 0

    def test_warn_only_downgrades_drift_to_zero(self, tmp_path):
        contract_path, contract = self._setup(
            tmp_path,
            [{"name": "orders", "access": "public", "columns": [{"name": "order_id"}]}],
            [
                _expose(
                    "orders",
                    [
                        {"name": "order_id", "type": "integer"},
                        {"name": "amount", "type": "decimal"},
                    ],
                )
            ],
        )
        rc = self._run(contract_path, contract, self._args(str(contract_path), warn_only=True))
        assert rc == 0

    def test_reconcile_absent_flag_is_backward_compatible(self, tmp_path):
        # Without reconcile_dbt attr at all, run() must behave exactly as before.
        contract_path, contract = self._setup(
            tmp_path,
            [{"name": "orders", "access": "public", "columns": [{"name": "order_id"}]}],
            [
                _expose(
                    "orders",
                    [
                        {"name": "order_id", "type": "integer"},
                        {"name": "amount", "type": "decimal"},
                    ],
                )
            ],
        )
        args = argparse.Namespace(
            contract=str(contract_path),
            expose_id=None,
            strict=False,
            out=None,
            show_diffs=False,
            env=None,
        )
        rc = self._run(contract_path, contract, args)
        assert rc == 0

    def test_out_report_includes_reconcile(self, tmp_path):
        import json

        contract_path, contract = self._setup(
            tmp_path,
            [{"name": "orders", "access": "public", "columns": [{"name": "order_id"}]}],
            [
                _expose(
                    "orders",
                    [
                        {"name": "order_id", "type": "integer"},
                        {"name": "amount", "type": "decimal"},
                    ],
                )
            ],
        )
        out = tmp_path / "report.json"
        self._run(contract_path, contract, self._args(str(contract_path), out=str(out)))
        data = json.loads(out.read_text())
        assert "reconcile" in data
        assert data["reconcile"]["has_drift"] is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
