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

"""Parse dbt ``run_results.json`` → FLUID run records + verify checks.

Covers the card "dbt: parse run_results.json into fluid run records + verify
checks":

* :mod:`fluid_build.build_runners.dbt.artifacts` — defensive, version-agnostic
  parse of ``target/run_results.json``.
* ``build_runners/dbt/runner.py`` — the ``execute_dbt_build`` post-build hook
  that writes a canonical run record via the ``FileStateStore`` chokepoint,
  and the product-id resolver.
* ``cli/_transformation_stage_ext.py`` — the ``verify_transformation`` sibling
  that gates ``fluid verify --strict`` on failing contract tests.

Acceptance proven end-to-end here: a failing dbt test lands in the record with
per-test granularity (readable by ``fluid runs status``) AND surfaces as a
critical verify mismatch.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from fluid_build.build_runners.dbt import artifacts, runner

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "run_results_v6.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _write_run_results(project_dir: Path, payload: dict) -> Path:
    target = project_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "run_results.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── artifacts.parse_run_results ────────────────────────────────────────────


class TestParseRunResults:
    def test_parses_fixture_fields(self, tmp_path):
        _write_run_results(tmp_path, _load_fixture())
        rr = artifacts.parse_run_results(tmp_path)
        assert rr is not None
        assert rr.schema_version == "v6"
        assert rr.dbt_version == "1.8.7"
        assert rr.invocation_id
        assert rr.elapsed_time == pytest.approx(6.71)
        assert len(rr.results) == 6

    def test_counts_split_models_and_tests(self, tmp_path):
        _write_run_results(tmp_path, _load_fixture())
        rr = artifacts.parse_run_results(tmp_path)
        counts = rr.counts()
        assert counts["nodes_total"] == 6
        assert counts["models_total"] == 2
        assert counts["models_errored"] == 0
        assert counts["tests_total"] == 4
        assert counts["tests_passed"] == 1
        assert counts["tests_failed"] == 3
        # 3 failing tests, no failing models → 3 error-severity failures.
        assert counts["error_severity_failures"] == 3

    def test_test_node_classification_and_failures(self, tmp_path):
        _write_run_results(tmp_path, _load_fixture())
        rr = artifacts.parse_run_results(tmp_path)
        failed = {n.unique_id: n.failures for n in rr.failed_tests}
        assert failed == {
            "test.acme.unique_orders_order_id.def456": 5,
            "test.acme.not_null_orders_customer_id.ghi789": 3,
            "test.acme.relationships_orders_customer_id.jkl012": 1,
        }
        # A passing model has failures=None (dbt emits null for models).
        model = next(n for n in rr.models if n.unique_id == "model.acme.orders")
        assert model.failures is None
        assert model.is_ok

    def test_missing_artifact_returns_none(self, tmp_path):
        assert artifacts.parse_run_results(tmp_path) is None

    def test_malformed_json_returns_none(self, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        (target / "run_results.json").write_text("{not json", encoding="utf-8")
        assert artifacts.parse_run_results(tmp_path) is None

    def test_non_dict_payload_returns_none(self):
        assert artifacts.parse_run_results_dict([1, 2, 3]) is None
        assert artifacts.parse_run_results_dict({"no": "results"}) is None

    def test_version_agnostic_older_schema(self):
        # v4-style artifact (no invocation_id, different URL) — the stable
        # fields still parse; this is the whole reason we avoid a versioned dep.
        payload = {
            "metadata": {
                "dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v4.json"
            },
            "results": [
                {"unique_id": "test.p.t1", "status": "fail", "failures": 2, "execution_time": 0.1}
            ],
            "elapsed_time": 0.5,
        }
        rr = artifacts.parse_run_results_dict(payload)
        assert rr is not None
        assert rr.schema_version == "v4"
        assert rr.counts()["tests_failed"] == 1

    def test_stringified_and_bad_failures_coerce(self):
        payload = {
            "metadata": {},
            "results": [
                {"unique_id": "test.p.a", "status": "fail", "failures": "7", "execution_time": 0},
                {
                    "unique_id": "test.p.b",
                    "status": "fail",
                    "failures": "oops",
                    "execution_time": 0,
                },
                {"status": "success"},  # no unique_id → dropped
                "not-a-dict",  # not a mapping → dropped
            ],
            "elapsed_time": 0,
        }
        rr = artifacts.parse_run_results_dict(payload)
        # The two malformed entries (no unique_id / not a dict) are dropped.
        assert len(rr.results) == 2
        by_id = {n.unique_id: n.failures for n in rr.results}
        assert by_id["test.p.a"] == 7
        assert by_id["test.p.b"] is None  # uncoercible → None, no crash


# ── runner.build_dbt_run_record ────────────────────────────────────────────


class TestBuildDbtRunRecord:
    def _record(self, payload=None, returncode=1):
        rr = artifacts.parse_run_results_dict(payload or _load_fixture())
        return runner.build_dbt_run_record(
            rr,
            run_id="RID1",
            started_at="2026-07-18T10:00:00Z",
            finished_at="2026-07-18T10:00:06Z",
            duration_seconds=6.71,
            returncode=returncode,
        )

    def test_canonical_shape(self):
        rec = self._record()
        assert set(rec) >= {
            "run_id",
            "state",
            "started_at",
            "finished_at",
            "records_total",
            "streams",
            "error",
            "facets",
        }
        assert rec["run_id"] == "RID1"
        assert rec["records_total"] == 6

    def test_state_partial_when_some_pass_some_fail(self):
        rec = self._record(returncode=1)
        assert rec["state"] == "partial"  # models+1 test passed, 3 tests failed

    def test_state_succeeded_on_clean_run(self):
        payload = {
            "metadata": {},
            "results": [
                {"unique_id": "model.p.m", "status": "success", "execution_time": 1},
                {"unique_id": "test.p.t", "status": "pass", "failures": 0, "execution_time": 0.1},
            ],
            "elapsed_time": 1.1,
        }
        rec = self._record(payload=payload, returncode=0)
        assert rec["state"] == "succeeded"
        assert rec["error"] is None

    def test_state_failed_when_nothing_ok(self):
        payload = {
            "metadata": {},
            "results": [
                {"unique_id": "model.p.m", "status": "error", "execution_time": 1},
            ],
            "elapsed_time": 1,
        }
        rec = self._record(payload=payload, returncode=2)
        assert rec["state"] == "failed"

    def test_streams_carry_per_node_failures(self):
        rec = self._record()
        streams = {s["name"]: s for s in rec["streams"]}
        # Per-test granularity: the failing unique test is its own stream with
        # its failure count carried on both ``records`` and ``failures``.
        s = streams["test.acme.unique_orders_order_id.def456"]
        assert s["state"] == "fail"
        assert s["records"] == 5
        assert s["failures"] == 5

    def test_error_message_names_failed_nodes(self):
        rec = self._record()
        assert rec["error"] is not None
        assert "3 dbt node(s) at error severity" in rec["error"]
        assert "test.acme.unique_orders_order_id.def456" in rec["error"]

    def test_facets_carry_counts_and_engine(self):
        rec = self._record()
        f = rec["facets"]
        assert f["engine"] == "dbt"
        assert f["returncode"] == 1
        assert f["dbt_schema_version"] == "v6"
        assert f["tests_failed"] == 3
        assert f["error_severity_failures"] == 3
        assert f["duration_seconds"] == pytest.approx(6.71)


# ── runner._resolve_product_id_from_dir ─────────────────────────────────────


class TestResolveProductId:
    def _write_contract(self, path: Path, cid: str, build_ids) -> None:
        builds = "\n".join(f"  - id: {b}\n    engine: dbt" for b in build_ids)
        path.write_text(f"id: {cid}\nbuilds:\n{builds}\n", encoding="utf-8")

    def test_single_contract(self, tmp_path):
        self._write_contract(tmp_path / "c.fluid.yaml", "acme.orders", ["b1"])
        pid = runner._resolve_product_id_from_dir(tmp_path, {"id": "b1"})
        assert pid == "acme.orders"

    def test_disambiguates_by_build_id(self, tmp_path):
        self._write_contract(tmp_path / "a.fluid.yaml", "acme.a", ["ba"])
        self._write_contract(tmp_path / "b.fluid.yaml", "acme.b", ["bb"])
        assert runner._resolve_product_id_from_dir(tmp_path, {"id": "bb"}) == "acme.b"

    def test_falls_back_to_first_with_id(self, tmp_path):
        self._write_contract(tmp_path / "a.fluid.yaml", "acme.a", ["ba"])
        # build id not present anywhere → first contract with an id wins.
        assert runner._resolve_product_id_from_dir(tmp_path, {"id": "unknown"}) == "acme.a"

    def test_no_contract_returns_none(self, tmp_path):
        assert runner._resolve_product_id_from_dir(tmp_path, {"id": "b1"}) is None


# ── runner._persist_dbt_run_record → fluid runs status ─────────────────────


class TestPersistLightsUpRunsStatus:
    def _contract_dir(self, tmp_path):
        contract_dir = tmp_path / "ws"
        contract_dir.mkdir()
        (contract_dir / "orders.fluid.yaml").write_text(
            "id: acme.orders\nbuilds:\n  - id: orders_build\n    engine: dbt\n",
            encoding="utf-8",
        )
        project_dir = contract_dir / "dbt"
        project_dir.mkdir()
        _write_run_results(project_dir, _load_fixture())
        return contract_dir, project_dir

    def test_record_written_and_keyed_for_verify(self, tmp_path):
        contract_dir, project_dir = self._contract_dir(tmp_path)
        run_id = runner._persist_dbt_run_record(
            {"id": "orders_build"},
            project_dir,
            contract_dir,
            returncode=1,
            started_at="2026-07-18T10:00:00Z",
            finished_at="2026-07-18T10:00:06Z",
            duration_seconds=6.71,
        )
        assert run_id is not None
        # Keyed at <contract_dir>/.fluid/runs/<contract.id>/<build.id>/runs/ —
        # exactly where verify_transformation / latest_run_record read.
        runs_dir = contract_dir / ".fluid" / "runs" / "acme.orders" / "orders_build" / "runs"
        written = list(runs_dir.glob("*.json"))
        assert len(written) == 1

    def test_fluid_runs_status_shows_per_test_granularity(self, tmp_path):
        contract_dir, project_dir = self._contract_dir(tmp_path)
        runner._persist_dbt_run_record(
            {"id": "orders_build"},
            project_dir,
            contract_dir,
            returncode=1,
            started_at="2026-07-18T10:00:00Z",
            finished_at="2026-07-18T10:00:06Z",
            duration_seconds=6.71,
        )
        # Read back through the SAME surface `fluid runs status` uses.
        from fluid_build.build_runners._state import FileStateStore
        from fluid_build.cli.ops.status import build_status_report

        store = FileStateStore(contract_dir / ".fluid")
        report = build_status_report(store, "acme.orders", "orders_build", limit=5)
        assert len(report.runs) == 1
        run = report.runs[0]
        assert run.state == "partial"
        assert run.records_total == 6
        names = {s["name"] for s in run.streams}
        assert "test.acme.unique_orders_order_id.def456" in names
        assert "test.acme.not_null_orders_customer_id.ghi789" in names

    def test_persist_is_best_effort_no_contract(self, tmp_path):
        # No contract file → product id unresolved → no write, no raise.
        project_dir = tmp_path / "dbt"
        project_dir.mkdir()
        _write_run_results(project_dir, _load_fixture())
        assert (
            runner._persist_dbt_run_record(
                {"id": "b"},
                project_dir,
                tmp_path,
                returncode=0,
                started_at="s",
                finished_at="f",
                duration_seconds=0.0,
            )
            is None
        )

    def test_persist_routes_through_redaction_chokepoint(self, tmp_path, monkeypatch):
        # The record MUST be written via FileStateStore.write_run_record (the
        # PR #272 redaction funnel), never json.dump directly.
        contract_dir, project_dir = self._contract_dir(tmp_path)
        calls = {}
        import fluid_build.build_runners._state as state_mod

        orig = state_mod.FileStateStore.write_run_record

        def _spy(self, product_id, build_id, record):
            calls["args"] = (product_id, build_id)
            return orig(self, product_id, build_id, record)

        monkeypatch.setattr(state_mod.FileStateStore, "write_run_record", _spy)
        runner._persist_dbt_run_record(
            {"id": "orders_build"},
            project_dir,
            contract_dir,
            returncode=1,
            started_at="s",
            finished_at="f",
            duration_seconds=0.0,
        )
        assert calls["args"] == ("acme.orders", "orders_build")


# ── cli/_transformation_stage_ext.verify_transformation ────────────────────


class TestVerifyTransformation:
    def _seed(self, tmp_path, returncode=1, payload=None):
        from fluid_build.build_runners._state import FileStateStore

        contract = {
            "id": "acme.orders",
            "builds": [{"id": "orders_build", "engine": "dbt"}],
        }
        rr = artifacts.parse_run_results_dict(payload or _load_fixture())
        rec = runner.build_dbt_run_record(
            rr,
            run_id="RID",
            started_at="s",
            finished_at="f",
            duration_seconds=1.0,
            returncode=returncode,
        )
        FileStateStore(tmp_path / ".fluid").write_run_record("acme.orders", "orders_build", rec)
        return contract

    def test_is_transformation_contract(self):
        from fluid_build.cli._transformation_stage_ext import is_transformation_contract

        assert is_transformation_contract({"builds": [{"id": "b", "engine": "dbt"}]})
        assert is_transformation_contract({"builds": [{"id": "b", "engine": "dbt-snowflake"}]})
        assert not is_transformation_contract({"builds": [{"id": "b", "engine": "duckdb"}]})
        # Inline-SQL builds run via DuckDB, not dbt (engine: sql, or
        # pattern: embedded-logic + properties.sql) → not a txf build.
        assert not is_transformation_contract({"builds": [{"id": "b", "engine": "sql"}]})
        assert not is_transformation_contract(
            {
                "builds": [
                    {
                        "id": "b",
                        "engine": "dbt",
                        "pattern": "embedded-logic",
                        "properties": {"sql": "SELECT 1"},
                    }
                ]
            }
        )

    def test_failing_tests_produce_critical_mismatch(self, tmp_path):
        from fluid_build.cli._transformation_stage_ext import (
            CRITICAL_TRANSFORMATION_CHECK_NAMES,
            verify_transformation,
        )

        contract = self._seed(tmp_path, returncode=1)
        results = verify_transformation(contract, tmp_path)
        assert len(results) == 1
        checks = {c.name: c for c in results[0].checks}
        assert checks["dbt_tests_passed"].passed is False
        assert checks["no_error_severity_failures"].passed is False
        # Both failing checks are in the critical set → --strict will gate.
        failed_critical = [
            c
            for c in results[0].checks
            if not c.passed and c.name in CRITICAL_TRANSFORMATION_CHECK_NAMES
        ]
        assert len(failed_critical) == 2

    def test_clean_run_all_checks_pass(self, tmp_path):
        payload = {
            "metadata": {},
            "results": [
                {"unique_id": "model.p.m", "status": "success", "execution_time": 1},
                {"unique_id": "test.p.t", "status": "pass", "failures": 0, "execution_time": 0.1},
            ],
            "elapsed_time": 1.1,
        }
        from fluid_build.cli._transformation_stage_ext import verify_transformation

        contract = self._seed(tmp_path, returncode=0, payload=payload)
        results = verify_transformation(contract, tmp_path)
        assert results[0].all_passed

    def test_missing_run_record_is_non_critical(self, tmp_path):
        from fluid_build.cli._transformation_stage_ext import (
            CRITICAL_TRANSFORMATION_CHECK_NAMES,
            verify_transformation,
        )

        contract = {"id": "acme.orders", "builds": [{"id": "orders_build", "engine": "dbt"}]}
        results = verify_transformation(contract, tmp_path)
        checks = results[0].checks
        assert len(checks) == 1
        assert checks[0].name == "run_record_present"
        assert checks[0].passed is False
        # Absent record is NOT critical — verify may run before amend-and-build.
        assert "run_record_present" not in CRITICAL_TRANSFORMATION_CHECK_NAMES


# ── execute_dbt_build end-to-end hook ──────────────────────────────────────


class TestExecuteDbtBuildHook:
    def test_failing_build_still_writes_per_test_record(self, tmp_path, monkeypatch):
        contract_dir = tmp_path / "ws"
        contract_dir.mkdir()
        (contract_dir / "orders.fluid.yaml").write_text(
            "id: acme.orders\nbuilds:\n  - id: orders_build\n    engine: dbt\n",
            encoding="utf-8",
        )
        project_dir = contract_dir / "dbt"
        project_dir.mkdir()

        # Stub out project/profile resolution + command build so no real dbt
        # install is needed.
        monkeypatch.setattr(runner, "_load_dbt_project_config", lambda *_a, **_k: {})
        monkeypatch.setattr(
            runner, "resolve_dbt_profiles_dir", lambda *a, **k: (contract_dir, None)
        )
        monkeypatch.setattr(runner, "build_dbt_command", lambda *a, **k: ["dbt", "build"])

        def _fake_run(command, **kwargs):
            # Simulate dbt writing target/run_results.json then failing (tests failed).
            _write_run_results(project_dir, _load_fixture())
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")

        monkeypatch.setattr(runner.subprocess, "run", _fake_run)

        build = {
            "id": "orders_build",
            "engine": "dbt",
            "execution": {"trigger": {"type": "manual", "iterations": 1}},
        }
        rc = runner.execute_dbt_build(build, project_dir, contract_dir)

        # Build exit code is preserved (dbt tests failed → non-zero).
        assert rc == 1

        # …and a per-test run record landed where fluid runs status reads it.
        from fluid_build.build_runners._state import FileStateStore

        store = FileStateStore(contract_dir / ".fluid")
        records = store.list_runs("acme.orders", "orders_build")
        assert len(records) == 1
        assert records[0]["state"] == "partial"
        assert records[0]["facets"]["tests_failed"] == 3
        names = {s["name"] for s in records[0]["streams"]}
        assert "test.acme.unique_orders_order_id.def456" in names
