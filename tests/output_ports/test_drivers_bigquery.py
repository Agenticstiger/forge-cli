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

"""Tests for the BigQuery output-port driver.

Closes a coverage gap surfaced by the branch-wide test campaign: the
``BigQueryDriver`` was registered (``("gcp","bigquery_table")``) but
never instantiated by any test. The mocked unit tests below mirror
``test_drivers_snowflake.py::TestSnowflakeDriverMocked`` — descriptor
shape, binding validation, identifier safety, the ``:p_`` → ``@p_``
placeholder rewrite, the driver-level injection guard, health checks,
and column-restriction masking — all keyless.

The live integration test is opt-in: it only runs when
``FLUID_BQ_TEST_PROJECT`` names a billing project, and it is
self-seeding + self-cleaning (creates a temp dataset, queries it via
the driver, then DROPs the dataset) so it never leaves resources
behind.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.output_ports.mcp.drivers.base import UnsupportedBindingError
from fluid_build.output_ports.mcp.drivers.bigquery import BigQueryDriver, _bq_param_type


def _make_expose(
    *,
    project: str = "analytics-prod-123",
    dataset: str = "CUSTOMER",
    table: str = "PROFILES",
    column_restrictions: Optional[List[Dict[str, Any]]] = None,
    semantics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "customer_profiles",
        "kind": "table",
        "contract": {
            "schema": [
                {"name": "CUSTOMER_ID", "type": "STRING", "required": True},
                {"name": "EMAIL", "type": "STRING", "sensitivity": "pii"},
                {"name": "SIGNUP_DATE", "type": "DATE"},
            ],
        },
        "binding": {
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {
                "project": project,
                "dataset": dataset,
                "table": table,
            },
        },
    }
    if semantics is not None:
        expose["semantics"] = semantics
    if column_restrictions is not None:
        expose.setdefault("policy", {}).setdefault("authz", {})[
            "columnRestrictions"
        ] = column_restrictions
    return expose


class _FakeField:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeResult:
    """Stand-in for a BigQuery RowIterator: has ``.schema`` (objects
    with ``.name``) and yields dict-like rows supporting ``row[col]``."""

    def __init__(self, columns: List[str], rows: List[Dict[str, Any]]) -> None:
        self.schema = [_FakeField(c) for c in columns]
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


def _fake_client(*, columns: List[str], rows: List[Dict[str, Any]]):
    """A MagicMock BigQuery client whose ``query().result()`` returns a
    :class:`_FakeResult`. Uses the REAL ``google.cloud.bigquery`` module
    for ScalarQueryParameter/QueryJobConfig (plain data classes) but a
    mock client so no network/auth happens."""
    from unittest.mock import MagicMock

    job = MagicMock(name="job")
    job.result.return_value = _FakeResult(columns, rows)
    client = MagicMock(name="bq_client")
    client.query.return_value = job
    return client


# ---------------------------------------------------------------------
# Mocked unit tests — always run, no creds needed
# ---------------------------------------------------------------------


class TestBigQueryDriverMocked:
    def test_descriptor_returns_backtick_qualified_reference(self):
        driver = BigQueryDriver(expose=_make_expose(), contract={})
        descriptor = driver.descriptor()
        assert descriptor.platform == "gcp"
        assert descriptor.format == "bigquery_table"
        assert descriptor.dialect == "bigquery"
        assert descriptor.table_reference == "`analytics-prod-123.CUSTOMER.PROFILES`"

    def test_descriptor_capabilities_advertised(self):
        caps = BigQueryDriver(expose=_make_expose(), contract={}).descriptor().capabilities
        assert caps["describe"] is True
        assert caps["sample"] is True
        assert caps["query"] is True
        assert caps["query_sql"] is True

    def test_unsupported_platform_raises(self):
        expose = _make_expose()
        expose["binding"]["platform"] = "snowflake"
        with pytest.raises(UnsupportedBindingError, match="gcp"):
            BigQueryDriver(expose=expose, contract={})

    def test_unsupported_format_raises(self):
        expose = _make_expose()
        expose["binding"]["format"] = "bigquery_view"
        with pytest.raises(UnsupportedBindingError, match="bigquery_table"):
            BigQueryDriver(expose=expose, contract={})

    def test_missing_project_raises(self):
        expose = _make_expose()
        del expose["binding"]["location"]["project"]
        with pytest.raises(UnsupportedBindingError, match="project"):
            BigQueryDriver(expose=expose, contract={})

    def test_missing_dataset_raises(self):
        expose = _make_expose()
        del expose["binding"]["location"]["dataset"]
        with pytest.raises(UnsupportedBindingError, match="dataset"):
            BigQueryDriver(expose=expose, contract={})

    def test_missing_table_raises(self):
        expose = _make_expose()
        del expose["binding"]["location"]["table"]
        with pytest.raises(UnsupportedBindingError, match="table"):
            BigQueryDriver(expose=expose, contract={})

    def test_hyphenated_project_id_accepted(self):
        """BigQuery project IDs legitimately contain hyphens; the driver
        validates the project via the expression allowlist (not the
        stricter ident rule) precisely so hyphens pass."""
        driver = BigQueryDriver(expose=_make_expose(project="my-gcp-proj-42"), contract={})
        assert driver.descriptor().table_reference.startswith("`my-gcp-proj-42.")

    def test_unsafe_dataset_identifier_rejected(self):
        with pytest.raises(ValueError):
            BigQueryDriver(expose=_make_expose(dataset="CUSTOMER; DROP TABLE x"), contract={})

    def test_execute_rewrites_named_placeholders(self):
        """Compiler emits ``:p_0``; BigQuery expects ``@p_0``. The driver
        MUST rewrite before binding."""
        # execute() imports google.cloud.bigquery for real (QueryJobConfig /
        # ScalarQueryParameter); skip when the optional [gcp] extra isn't
        # installed (base CI) rather than fail. DuckDB-module convention.
        pytest.importorskip("google.cloud.bigquery")
        driver = BigQueryDriver(expose=_make_expose(), contract={})
        driver._client = _fake_client(columns=["CUSTOMER_ID"], rows=[{"CUSTOMER_ID": "C0001"}])
        sql_named = (
            "SELECT CUSTOMER_ID FROM `analytics-prod-123.CUSTOMER.PROFILES` WHERE EMAIL = :p_0"
        )
        result = driver.execute(sql=sql_named, params=("alice@example.com",))
        executed_sql = driver._client.query.call_args.args[0]
        assert "@p_0" in executed_sql
        assert ":p_0" not in executed_sql
        # Bound as a named ScalarQueryParameter, passed via job_config.
        job_config = driver._client.query.call_args.kwargs["job_config"]
        assert [p.name for p in job_config.query_parameters] == ["p_0"]
        assert result.columns == ("CUSTOMER_ID",)
        assert result.rows == ({"CUSTOMER_ID": "C0001"},)

    def test_execute_blocks_injection_marker_at_driver(self):
        pytest.importorskip("google.cloud.bigquery")
        driver = BigQueryDriver(expose=_make_expose(), contract={})
        driver._client = _fake_client(columns=[], rows=[])
        with pytest.raises(ValueError):
            driver.execute(sql="SELECT 1; DROP TABLE x", params=())

    def test_health_check_reports_ok(self):
        pytest.importorskip("google.cloud.bigquery")
        driver = BigQueryDriver(expose=_make_expose(), contract={})
        driver._client = _fake_client(columns=["f0_"], rows=[{"f0_": 1}])
        result = driver.health_check()
        assert result["status"] == "ok"
        assert result["engine"] == "bigquery"
        assert "latency_ms" in result

    def test_health_check_reports_unavailable_on_error(self):
        pytest.importorskip("google.cloud.bigquery")
        from unittest.mock import MagicMock

        driver = BigQueryDriver(expose=_make_expose(), contract={})
        client = MagicMock(name="bq_client")
        client.query.side_effect = RuntimeError("permission denied")
        driver._client = client
        result = driver.health_check()
        assert result["status"] == "unavailable"
        assert "permission denied" in result["detail"]

    def test_restricted_columns_drop_via_project(self):
        driver = BigQueryDriver(
            expose=_make_expose(
                column_restrictions=[{"principal": "*", "columns": ["EMAIL"], "access": "deny"}]
            ),
            contract={},
        )
        rows = [{"CUSTOMER_ID": "C1", "EMAIL": "x@y", "SIGNUP_DATE": "2024-01-01"}]
        visible_columns, masked_rows = driver.project(rows)
        assert "EMAIL" not in visible_columns
        assert all("EMAIL" not in row for row in masked_rows)

    def test_bq_param_type_scalar_mapping(self):
        assert _bq_param_type(True) == "BOOL"
        assert _bq_param_type(7) == "INT64"
        assert _bq_param_type(1.5) == "FLOAT64"
        assert _bq_param_type("s") == "STRING"
        with pytest.raises(ValueError):
            _bq_param_type([1, 2, 3])  # non-scalar

    def test_tool_query_handler_keyless_end_to_end(self):
        """Keyless regression pinning TWO bugs at once on BigQuery:

        1. The ``query`` MCP tool's handler→compiler→driver wiring (the
           handler called ``compile_semantic_query`` / ``driver.query``
           with signatures they never exposed → TypeError on every call).
        2. The SQL-safety allowlist rejecting BigQuery's REQUIRED
           backtick-quoted ``table_reference`` (defence-in-depth check
           in ``compile_semantic_query``), which broke the ``query``
           tool on BigQuery even after the wiring was fixed.

        Runs the REAL compiler + handler; only the BigQuery client is a
        fake. Still needs the google.cloud.bigquery lib importable (the
        driver builds QueryJobConfig); skip when the optional [gcp] extra
        is absent (base CI)."""
        pytest.importorskip("google.cloud.bigquery")
        import logging

        from fluid_build.output_ports.mcp import _handlers
        from fluid_build.output_ports.mcp.policy import OutputPortPolicy
        from fluid_build.output_ports.mcp.server import SessionState

        expose = _make_expose(
            semantics={
                "name": "profiles",
                "measures": [{"name": "row_count", "agg": "count", "expr": "CUSTOMER_ID"}],
            }
        )
        state = SessionState(
            contract={"exposes": [expose]},
            expose=expose,
            policy=OutputPortPolicy.from_contract_and_flags(expose=expose),
            logger=logging.getLogger("test.bq.handler.keyless"),
        )
        # Inject a fake client into the lazily-built driver — the real
        # compiler (incl. the backtick allowlist check) and handler run.
        driver = state.get_driver()
        driver._client = _fake_client(columns=["row_count"], rows=[{"row_count": 3}])

        payload = _handlers.tool_query(state, {"measure": "row_count", "limit": 5})
        assert payload["rowCount"] == 1
        assert payload["rows"][0]["row_count"] == 3
        # The backtick-quoted reference survived the allowlist into the SQL.
        assert "`analytics-prod-123.CUSTOMER.PROFILES`" in payload["compiled"]["sql"]


# ---------------------------------------------------------------------
# Live integration — opt-in + self-cleaning. Set FLUID_BQ_TEST_PROJECT
# to a billing project to enable; creates a temp dataset, queries it
# through the driver, then DROPs the dataset (no resources left behind).
# ---------------------------------------------------------------------

_BQ_LIVE_PROJECT = os.environ.get("FLUID_BQ_TEST_PROJECT")


@pytest.mark.gcp
@pytest.mark.integration
@pytest.mark.skipif(
    not _BQ_LIVE_PROJECT,
    reason="set FLUID_BQ_TEST_PROJECT=<billing-project> to run the live, self-cleaning BQ test",
)
class TestBigQueryDriverIntegration:
    def test_execute_sample_against_temp_table_then_cleanup(self):
        bigquery = pytest.importorskip("google.cloud.bigquery")
        project = _BQ_LIVE_PROJECT
        dataset_id = f"fluid_bqtest_{uuid.uuid4().hex[:10]}"
        client = bigquery.Client(project=project)
        client.create_dataset(bigquery.Dataset(f"{project}.{dataset_id}"))
        try:
            table_id = f"{project}.{dataset_id}.profiles"
            client.query(f"CREATE TABLE `{table_id}` (CUSTOMER_ID STRING, EMAIL STRING)").result()
            client.query(
                f"INSERT INTO `{table_id}` (CUSTOMER_ID, EMAIL) "
                "VALUES ('C1','a@x'), ('C2','b@y'), ('C3','c@z')"
            ).result()

            driver = BigQueryDriver(
                expose=_make_expose(project=project, dataset=dataset_id, table="profiles"),
                contract={},
            )
            assert driver.health_check()["status"] == "ok"
            sample = driver.sample(limit=2, caller_attributes={})
            assert sample.row_count >= 1 if hasattr(sample, "row_count") else len(sample.rows) >= 1
            res = driver.execute(sql=f"SELECT COUNT(*) AS n FROM `{table_id}`", params=())
            assert res.rows[0]["n"] == 3

            # Handler-path query (the wiring that was broken across ALL
            # drivers): drive _handlers.tool_query through a SessionState
            # bound to the temp table, proving handler →
            # compile_semantic_query → BigQueryDriver.query works
            # end-to-end on a live engine — not just driver.execute in
            # isolation (which is all the asserts above cover).
            import logging

            from fluid_build.output_ports.mcp import _handlers
            from fluid_build.output_ports.mcp.policy import OutputPortPolicy
            from fluid_build.output_ports.mcp.server import SessionState

            expose = _make_expose(
                project=project,
                dataset=dataset_id,
                table="profiles",
                semantics={
                    "name": "profiles",
                    "measures": [{"name": "row_count", "agg": "count", "expr": "CUSTOMER_ID"}],
                },
            )
            state = SessionState(
                contract={"exposes": [expose]},
                expose=expose,
                policy=OutputPortPolicy.from_contract_and_flags(expose=expose),
                logger=logging.getLogger("test.bq.handler"),
            )
            payload = _handlers.tool_query(state, {"measure": "row_count", "limit": 5})
            assert payload["rowCount"] == 1
            assert next(iter(payload["rows"][0].values())) == 3
        finally:
            client.delete_dataset(
                f"{project}.{dataset_id}", delete_contents=True, not_found_ok=True
            )
