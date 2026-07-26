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

"""Tests for the contract <-> published-lineage reconcile (``--reconcile-lineage``).

The reconcile cross-checks declared lineage (``consumes[]``/``exposes[]``)
against (a) the run evidence the build runners persist locally (run
records written via the real ``FileStateStore`` + cursor state) and (b)
the lineage payload the catalog registrars would publish
(``CatalogPublicationPayload.from_contract``, rebuilt locally). Drift
classes:

- ``declared_but_never_read``   (soft)     — consume with no run evidence
- ``read_but_undeclared``       (critical) — observed stream never declared
- ``publish_payload_mismatch``  (critical) — payload lineage != contract
"""

from __future__ import annotations

import argparse
import logging
from unittest.mock import patch

import pytest

from fluid_build.build_runners._state import FileStateStore
from fluid_build.cli._verify_reconcile_lineage import (
    REASON_DECLARED_NEVER_READ,
    REASON_PUBLISH_MISMATCH,
    REASON_READ_UNDECLARED,
    LineageReconcileReport,
    reconcile_contract_lineage,
)

PRODUCT_ID = "silver.sales.orders"
BUILD_ID = "main_build"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _consume(product_id: str, expose_id: str = "default"):
    return {"productId": product_id, "exposeId": expose_id}


def _expose(expose_id: str, path: str | None = None):
    location = {"path": path} if path else {}
    return {
        "exposeId": expose_id,
        "binding": {"platform": "local", "format": "csv", "location": location},
        "contract": {"schema": [{"name": "id", "type": "integer"}]},
    }


def _contract(consumes=None, exposes=None, builds=None, product_id: str = PRODUCT_ID):
    return {
        "id": product_id,
        "name": "Orders",
        "version": "1.0.0",
        "consumes": consumes if consumes is not None else [],
        "exposes": exposes if exposes is not None else [_expose("orders")],
        "builds": (
            builds
            if builds is not None
            else [{"id": BUILD_ID, "engine": "duckdb", "pattern": "acquisition"}]
        ),
    }


def _seed_run_record(workspace, streams, *, build_id: str = BUILD_ID, run_id: str = "r-001"):
    """Write a run record through the REAL FileStateStore (canonical shape)."""
    store = FileStateStore(workspace / ".fluid")
    store.write_run_record(
        PRODUCT_ID,
        build_id,
        {
            "run_id": run_id,
            "state": "succeeded",
            "started_at": "2026-07-18T00:00:00Z",
            "finished_at": "2026-07-18T00:01:00Z",
            "records_total": 42,
            "streams": [{"name": s, "state": "succeeded", "records": 21} for s in streams],
            "error": None,
            "facets": {"engine": "duckdb"},
        },
    )
    return store


def _reconcile(workspace, contract) -> LineageReconcileReport:
    contract_path = workspace / "contract.fluid.yaml"
    if not contract_path.exists():
        contract_path.write_text(f"id: {contract.get('id', 'x')}\n", encoding="utf-8")
    return reconcile_contract_lineage(contract, contract_path)


def _drifts(report, reason):
    return [d for d in report.drifts if d.reason == reason]


# ---------------------------------------------------------------------------
# Graceful degradation: no run evidence at all
# ---------------------------------------------------------------------------


class TestNoEvidence:
    def test_no_run_records_skips_evidence_checks_with_note(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        report = _reconcile(tmp_path, contract)
        assert _drifts(report, REASON_DECLARED_NEVER_READ) == []
        assert _drifts(report, REASON_READ_UNDECLARED) == []
        assert any("no run-record evidence" in n for n in report.notes)
        assert report.checked_run_records == 0

    def test_no_evidence_never_fails(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        report = _reconcile(tmp_path, contract)
        # Publish payload agrees (edge built from the consume), so a
        # never-run product reports zero drift — never fails.
        assert report.has_critical_drift is False

    def test_contract_without_id_is_note_not_crash(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        contract["id"] = ""
        report = _reconcile(tmp_path, contract)
        assert any("no id" in n for n in report.notes)

    def test_traversal_product_id_is_confined_not_read(self, tmp_path):
        # A hostile contract id must not glob run records outside the
        # workspace (verify does not run the runner-side id grammar).
        outside = tmp_path / "outside" / ".fluid" / "runs" / "x" / "b" / "runs"
        outside.mkdir(parents=True)
        (outside / "r1.json").write_text(
            '{"run_id": "r1", "streams": [{"name": "leaked"}]}', encoding="utf-8"
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        contract = _contract(
            consumes=[_consume("bronze.crm.customers", "customers")],
            product_id="../outside/.fluid/runs/x",
            builds=[{"id": "../../../../x/b", "engine": "duckdb"}],
        )
        contract["id"] = "../outside"
        report = _reconcile(workspace, contract)
        assert "leaked" not in report.observed_streams
        assert _drifts(report, REASON_READ_UNDECLARED) == []


# ---------------------------------------------------------------------------
# Consume evidence: declared_but_never_read (soft) / read_but_undeclared (critical)
# ---------------------------------------------------------------------------


class TestConsumeEvidence:
    def test_evidenced_consume_has_no_drift(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        _seed_run_record(tmp_path, ["customers"])
        report = _reconcile(tmp_path, contract)
        assert _drifts(report, REASON_DECLARED_NEVER_READ) == []

    def test_evidence_matches_full_product_id(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        _seed_run_record(tmp_path, ["bronze.crm.customers"])
        report = _reconcile(tmp_path, contract)
        assert _drifts(report, REASON_DECLARED_NEVER_READ) == []

    def test_evidence_matches_compound_product_expose(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "raw")])
        _seed_run_record(tmp_path, ["bronze.crm.customers.raw"])
        report = _reconcile(tmp_path, contract)
        assert _drifts(report, REASON_DECLARED_NEVER_READ) == []

    def test_declared_but_never_read_is_soft(self, tmp_path):
        contract = _contract(
            consumes=[
                _consume("bronze.crm.customers", "customers"),
                _consume("bronze.erp.suppliers", "suppliers"),
            ]
        )
        _seed_run_record(tmp_path, ["customers"])
        report = _reconcile(tmp_path, contract)
        soft = _drifts(report, REASON_DECLARED_NEVER_READ)
        assert len(soft) == 1
        assert soft[0].subject == "bronze.erp.suppliers.suppliers"
        assert soft[0].severity == "soft"
        assert report.has_critical_drift is False

    def test_read_but_undeclared_is_critical(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        _seed_run_record(tmp_path, ["customers", "mystery_feed"])
        report = _reconcile(tmp_path, contract)
        critical = _drifts(report, REASON_READ_UNDECLARED)
        assert len(critical) == 1
        assert critical[0].subject == "mystery_feed"
        assert critical[0].severity == "critical"
        assert critical[0].build_id == BUILD_ID
        assert report.has_critical_drift is True

    def test_declared_source_stream_not_flagged(self, tmp_path):
        builds = [
            {
                "id": BUILD_ID,
                "engine": "duckdb",
                "pattern": "acquisition",
                "properties": {"source": {"streams": ["raw_events"]}},
            }
        ]
        contract = _contract(consumes=[], builds=builds)
        _seed_run_record(tmp_path, ["raw_events"])
        report = _reconcile(tmp_path, contract)
        assert _drifts(report, REASON_READ_UNDECLARED) == []

    def test_stream_match_is_case_insensitive(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "Customers")])
        _seed_run_record(tmp_path, ["CUSTOMERS"])
        report = _reconcile(tmp_path, contract)
        assert report.has_drift is False

    def test_cursor_state_counts_as_evidence(self, tmp_path):
        from fluid_build.api.state import Cursor

        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        store = FileStateStore(tmp_path / ".fluid")
        store.set_cursor(
            PRODUCT_ID,
            BUILD_ID,
            Cursor(stream="customers", value="2026-07-18", updated_at="2026-07-18T00:00:00Z"),
        )
        report = _reconcile(tmp_path, contract)
        assert _drifts(report, REASON_DECLARED_NEVER_READ) == []
        assert "customers" in report.observed_streams

    def test_dbt_node_streams_excluded_from_undeclared_check(self, tmp_path):
        builds = [{"id": BUILD_ID, "engine": "dbt", "repository": "."}]
        contract = _contract(
            consumes=[_consume("bronze.crm.customers", "customers")], builds=builds
        )
        _seed_run_record(tmp_path, ["model.demo.orders", "test.demo.not_null_orders_id"])
        report = _reconcile(tmp_path, contract)
        # dbt node names are execution nodes, not upstream reads — no
        # critical noise, but the consume still lacks read evidence (soft).
        assert _drifts(report, REASON_READ_UNDECLARED) == []
        assert len(_drifts(report, REASON_DECLARED_NEVER_READ)) == 1
        assert any("dbt node stream" in n for n in report.notes)

    def test_observed_streams_and_counts_in_report(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        _seed_run_record(tmp_path, ["customers"])
        report = _reconcile(tmp_path, contract)
        assert report.checked_builds == 1
        assert report.checked_run_records == 1
        assert report.declared_consumes == 1
        assert report.observed_streams == ["customers"]


# ---------------------------------------------------------------------------
# Publish payload: publish_payload_mismatch (critical)
# ---------------------------------------------------------------------------


class TestPublishPayload:
    def test_well_formed_consume_publishes_edge_no_drift(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        report = _reconcile(tmp_path, contract)
        assert _drifts(report, REASON_PUBLISH_MISMATCH) == []

    def test_consume_without_expose_id_is_dropped_by_payload_builder(self, tmp_path):
        # The canonical payload builder (api/catalog_publication._build_asset)
        # silently drops a consumes[] ref lacking exposeId — no lineage edge
        # would ever reach the catalog. That is a critical publish mismatch.
        contract = _contract(consumes=[{"productId": "bronze.crm.customers"}])
        report = _reconcile(tmp_path, contract)
        mismatches = _drifts(report, REASON_PUBLISH_MISMATCH)
        assert len(mismatches) == 1
        assert mismatches[0].subject == "bronze.crm.customers"
        assert "no exposeId" in mismatches[0].detail
        assert mismatches[0].severity == "critical"

    def test_consumes_with_no_exposes_flags_unpublishable_edges(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")], exposes=[])
        report = _reconcile(tmp_path, contract)
        mismatches = _drifts(report, REASON_PUBLISH_MISMATCH)
        assert len(mismatches) == 1
        assert "no exposes[]" in mismatches[0].detail
        assert any("no assets" in n for n in report.notes)

    def test_no_consumes_no_mismatch(self, tmp_path):
        contract = _contract(consumes=[])
        report = _reconcile(tmp_path, contract)
        assert _drifts(report, REASON_PUBLISH_MISMATCH) == []


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


class TestReportShape:
    def test_to_dict_carries_taxonomy(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        _seed_run_record(tmp_path, ["mystery_feed"])
        report = _reconcile(tmp_path, contract)
        data = report.to_dict()
        assert data["has_drift"] is True
        assert data["has_critical_drift"] is True
        reasons = {d["reason"] for d in data["drifts"]}
        assert REASON_READ_UNDECLARED in reasons
        assert REASON_DECLARED_NEVER_READ in reasons
        for d in data["drifts"]:
            assert set(d) == {"reason", "severity", "subject", "detail", "build_id"}

    def test_critical_and_soft_partitions(self, tmp_path):
        contract = _contract(consumes=[_consume("bronze.crm.customers", "customers")])
        _seed_run_record(tmp_path, ["mystery_feed"])
        report = _reconcile(tmp_path, contract)
        assert {d.reason for d in report.critical_drifts} == {REASON_READ_UNDECLARED}
        assert {d.reason for d in report.soft_drifts} == {REASON_DECLARED_NEVER_READ}


# ---------------------------------------------------------------------------
# run() integration via fluid verify --reconcile-lineage
# ---------------------------------------------------------------------------


class TestVerifyRunLineageIntegration:
    def _args(self, contract, **kw):
        base = dict(
            contract=contract,
            expose_id=None,
            strict=False,
            out=None,
            show_diffs=False,
            env=None,
            reconcile_dbt=False,
            reconcile_lineage=True,
            warn_only=False,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _setup(self, tmp_path, consumes, observed_streams):
        # The fixture below needs the local-file expose to verify *cleanly*
        # (see the comment on the CSV): ``_verify_local_file`` introspects it
        # with DuckDB, which ships in the ``local`` extra, not ``dev``. Without
        # it the expose reports ``status: error`` — "we could not check" — and
        # every exit-code assertion in this class measures that instead of the
        # lineage reconcile it names. Make the prerequisite explicit and loud
        # (a reported skip) rather than latent, per the repo's own idiom in
        # tests/test_forge_db_tools.py and tests/test_fix_a4_bugs.py.
        pytest.importorskip(
            "duckdb",
            reason="local-file expose verification needs DuckDB (the `local` extra); "
            "without it error_count is 1 and these exit codes stop being about lineage",
        )
        contract_path = tmp_path / "contract.fluid.yaml"
        contract_path.write_text(f"id: {PRODUCT_ID}\n", encoding="utf-8")
        # A real CSV output so the local-file expose verify passes cleanly
        # (error_count must stay 0 for the --strict exit-code assertions).
        csv_path = tmp_path / "orders.csv"
        csv_path.write_text("id\n1\n2\n", encoding="utf-8")
        contract = _contract(consumes=consumes, exposes=[_expose("orders", str(csv_path))])
        if observed_streams is not None:
            _seed_run_record(tmp_path, observed_streams)
        return contract_path, contract

    def _run(self, contract, args):
        from fluid_build.cli.verify import run

        with (
            patch(
                "fluid_build.cli.verify.load_contract_with_overlay",
                return_value=contract,
            ),
            # Hermetic: keep the unconditional Snowflake settings resolve
            # away from the developer's real env/config (mirrors the
            # --reconcile-dbt integration tests).
            patch(
                "fluid_build.providers.snowflake.util.config.resolve_snowflake_settings",
                return_value={"account": "a", "warehouse": "w", "user": "u"},
            ),
        ):
            return run(args, logging.getLogger("test"))

    def test_critical_drift_with_strict_returns_nonzero(self, tmp_path):
        contract_path, contract = self._setup(
            tmp_path,
            [_consume("bronze.crm.customers", "customers")],
            ["customers", "mystery_feed"],
        )
        rc = self._run(contract, self._args(str(contract_path), strict=True))
        assert rc == 1

    def test_critical_drift_without_strict_returns_zero(self, tmp_path):
        contract_path, contract = self._setup(
            tmp_path,
            [_consume("bronze.crm.customers", "customers")],
            ["customers", "mystery_feed"],
        )
        rc = self._run(contract, self._args(str(contract_path)))
        assert rc == 0

    def test_warn_only_downgrades_critical_under_strict(self, tmp_path):
        contract_path, contract = self._setup(
            tmp_path,
            [_consume("bronze.crm.customers", "customers")],
            ["customers", "mystery_feed"],
        )
        rc = self._run(contract, self._args(str(contract_path), strict=True, warn_only=True))
        assert rc == 0

    def test_soft_drift_alone_never_fails_even_strict(self, tmp_path):
        contract_path, contract = self._setup(
            tmp_path,
            [
                _consume("bronze.crm.customers", "customers"),
                _consume("bronze.erp.suppliers", "suppliers"),
            ],
            ["customers"],
        )
        rc = self._run(contract, self._args(str(contract_path), strict=True))
        assert rc == 0

    def test_no_run_records_never_fails(self, tmp_path):
        contract_path, contract = self._setup(
            tmp_path, [_consume("bronze.crm.customers", "customers")], None
        )
        rc = self._run(contract, self._args(str(contract_path), strict=True))
        assert rc == 0

    def test_absent_flag_is_backward_compatible(self, tmp_path):
        contract_path, contract = self._setup(
            tmp_path,
            [_consume("bronze.crm.customers", "customers")],
            ["customers", "mystery_feed"],
        )
        args = argparse.Namespace(
            contract=str(contract_path),
            expose_id=None,
            strict=True,
            out=None,
            show_diffs=False,
            env=None,
        )
        rc = self._run(contract, args)
        assert rc == 0

    def test_out_report_includes_reconcile_lineage(self, tmp_path):
        import json

        contract_path, contract = self._setup(
            tmp_path,
            [_consume("bronze.crm.customers", "customers")],
            ["customers", "mystery_feed"],
        )
        out = tmp_path / "report.json"
        self._run(contract, self._args(str(contract_path), out=str(out)))
        data = json.loads(out.read_text())
        assert "reconcile_lineage" in data
        assert data["reconcile_lineage"]["has_critical_drift"] is True
        reasons = {d["reason"] for d in data["reconcile_lineage"]["drifts"]}
        assert REASON_READ_UNDECLARED in reasons
