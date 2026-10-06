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

"""A replace of a governed BigQuery table must keep the table's governance.

BigQuery documents that "when a table is replaced using the ``CREATE OR REPLACE
TABLE`` DDL statement, all existing row access policies on the original table
are dropped" (row-level security, Limitations), and the replacing table's
schema comes from the SELECT, so the column policy tags (column-level security
and data masking) go too. Measured on BigQuery, 6 October 2026: a table with a
row access policy had none after ``CREATE OR REPLACE TABLE ... AS SELECT`` and
showed every row.

The governance (policy tags in the table schema, ``google_bigquery_row_access_policy``,
data policies) is applied by ``tofu apply`` from ``iac/providers/gcp_governance.py``,
before any build runs. A replace that recreates the table therefore leaves every
row-filtered or column-restricted principal reading everything until the next apply.
The data-only replacement that keeps them is the one the load path already uses
(``build_runners/_bigquery_load.py``): ``WRITE_TRUNCATE_DATA`` into the existing
table, which "overwrites the data, but keeps the constraints and schema of the
existing table" (BigQuery REST reference, ``JobConfigurationQuery.writeDisposition``).
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from fluid_build.providers.gcp.plan.planner import plan_actions

pytestmark = pytest.mark.unit

PROJECT = "fluid-probe-123"
DATASET = "silver"
TABLE = "customer_profile"


def _governed_contract() -> Dict[str, Any]:
    """One BigQuery table with a row filter and a column restriction, and an
    inline-SQL build that writes it."""
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "silver.customer_profile",
        "name": "customer profile",
        "metadata": {"owner": {"team": "data"}},
        "exposes": [
            {
                "exposeId": TABLE,
                "kind": "table",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "project": PROJECT,
                        "dataset": DATASET,
                        "table": TABLE,
                        "region": "europe-west1",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "customer_id", "type": "STRING"},
                        {"name": "email", "type": "STRING"},
                        {"name": "consent", "type": "BOOLEAN"},
                    ]
                },
                "policy": {
                    "authz": {
                        "readers": ["group:analysts@example.com"],
                        "rowFilters": [
                            {
                                "principal": "group:analysts@example.com",
                                "name": "analysts_consented",
                                "where": "consent = true",
                            }
                        ],
                        "columnRestrictions": [
                            {
                                "principal": "group:analysts@example.com",
                                "columns": ["email"],
                                "access": "deny",
                            },
                        ],
                    }
                },
            }
        ],
        "builds": [
            {
                "id": "build_profile",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {
                    "sql": f"SELECT customer_id, email, consent FROM `{PROJECT}.bronze.customers`"
                },
                "outputs": [TABLE],
            }
        ],
    }


def _actions(mode: str = "replace", contract: Dict[str, Any] | None = None) -> List[Dict]:
    return plan_actions(
        contract or _governed_contract(),
        PROJECT,
        "europe-west1",
        logging.getLogger("test"),
        mode=mode,
    )


def _replace_actions():
    return _actions("replace")


def _build(actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    (build,) = [a for a in actions if a.get("phase") == "build"]
    return build


def _snapshot_action(actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    (snap,) = [a for a in actions if a.get("phase") == "snapshot"]
    return snap


class TestReplaceModeSqlBuild:
    def test_replace_does_not_recreate_the_governed_table(self):
        """No planned statement may ``CREATE OR REPLACE`` the governed table:
        BigQuery drops its row access policies, and the SELECT's schema has no
        policy tags."""
        offending = [
            a
            for a in _replace_actions()
            if "CREATE OR REPLACE TABLE" in str(a.get("sql") or "").upper()
            and f"{DATASET}.{TABLE}`" in str(a.get("sql"))
        ]
        assert offending == [], (
            "replace mode re-creates the governed table, which drops its row access "
            "policies and column policy tags:\n" + "\n".join(str(a["sql"]) for a in offending)
        )

    def test_replace_keeps_the_table_definition_planned(self):
        """The table (with its policy tags) stays owned by the declared table
        definition in replace mode; the build replaces only its rows."""
        ensured = [
            a
            for a in _replace_actions()
            if a.get("op") == "bq.ensure_table" and a.get("table") == TABLE
        ]
        assert ensured, (
            "replace mode drops the governed table's definition from the plan and "
            "leaves its materialisation to a CREATE OR REPLACE TABLE ... AS SELECT"
        )

    @pytest.mark.parametrize("mode", ["replace", "replace-and-build"])
    def test_replace_writes_the_select_into_the_existing_table(self, mode):
        """The build is the SELECT, a structured destination and the query job's
        dispositions: BigQuery replaces the rows and keeps the table."""
        build = _build(_actions(mode))
        assert build["op"] == "bq.sql.execute"
        assert (
            build["sql"] == f"SELECT customer_id, email, consent FROM `{PROJECT}.bronze.customers`"
        )
        assert build["destination"] == {"project": PROJECT, "dataset": DATASET, "table": TABLE}
        assert build["write_disposition"] == "WRITE_TRUNCATE_DATA"
        assert build["create_disposition"] == "CREATE_NEVER"
        assert build["mode"] == "replace"

    def test_amend_still_inserts(self):
        build = _build(_actions("amend"))
        assert build["sql"] == (
            f"INSERT INTO `{PROJECT}.{DATASET}.{TABLE}`\n"
            f"SELECT customer_id, email, consent FROM `{PROJECT}.bronze.customers`"
        )
        assert "destination" not in build and "write_disposition" not in build

    @pytest.mark.parametrize(
        "sql",
        [
            f"INSERT INTO `{PROJECT}.{DATASET}.{TABLE}` SELECT * FROM `{PROJECT}.bronze.c`",
            f"MERGE `{PROJECT}.{DATASET}.{TABLE}` t USING `{PROJECT}.bronze.c` s ON FALSE "
            "WHEN NOT MATCHED THEN INSERT ROW",
            "  create temp table x as select 1",
        ],
        ids=["insert", "merge", "create"],
    )
    def test_sql_that_names_its_own_sink_passes_through_untouched(self, sql):
        contract = _governed_contract()
        contract["builds"][0]["properties"]["sql"] = sql
        build = _build(_actions("replace", contract))
        assert build["sql"] == sql
        assert "destination" not in build and "write_disposition" not in build

    def test_a_crafted_destination_is_refused_not_planned(self):
        """The destination is validated like every FQN the planner builds."""
        contract = _governed_contract()
        contract["exposes"][0]["binding"]["location"]["dataset"] = "silver`; DROP TABLE x; --"
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            _actions("replace", contract)

    def test_the_backup_is_a_zero_copy_snapshot_not_an_ungoverned_copy(self):
        """``CREATE TABLE … AS SELECT * FROM <t>`` was a full copy with no row access
        policy and no policy tag; a table snapshot is BigQuery's zero-copy backup."""
        snap = _snapshot_action(_replace_actions())
        backup = snap["rollback_snapshot"]["backup_name"]
        assert re.fullmatch(rf"BACKUP_{TABLE}_\d+", backup)
        assert snap["sql"] == (
            f"CREATE SNAPSHOT TABLE IF NOT EXISTS `{PROJECT}.{DATASET}.{backup}` "
            f"CLONE `{PROJECT}.{DATASET}.{TABLE}` "
            "OPTIONS(expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 30 DAY))"
        )
        assert " AS SELECT" not in snap["sql"].upper()
        assert snap["allow_failure"] is True
        assert snap["rollback_snapshot"]["location"] == {
            "database": PROJECT,
            "schema": DATASET,
            "table": TABLE,
            "backup_table": backup,
        }

    def test_an_additive_plan_takes_no_snapshot(self):
        assert not [a for a in _actions("amend") if a.get("phase") == "snapshot"]

    def test_the_plan_never_recreates_any_table(self):
        for mode in ("amend", "replace", "replace-and-build"):
            for action in _actions(mode):
                assert "CREATE OR REPLACE" not in str(action.get("sql") or "").upper(), action


class TestBigQueryRollbackRestore:
    """``fluid rollback`` executes the BigQuery restore; it must not recreate
    the table either."""

    def _snapshot(self) -> Dict[str, Any]:
        return {
            "provider": "gcp",
            "backup_name": f"BACKUP_{TABLE}_1",
            "location": {
                "database": PROJECT,
                "schema": DATASET,
                "table": TABLE,
                "backup_table": f"BACKUP_{TABLE}_1",
            },
        }

    @staticmethod
    def _client(client_cls, *, rows: int = 3, readable: int = 3):
        """The patched client: a backup of ``rows`` rows, ``readable`` of them readable."""
        client = client_cls.return_value
        client.get_table.return_value.num_rows = rows

        def query(sql, **_kwargs):
            job = client.make_job()
            job.result.return_value = [{"f0_": readable}] if "COUNT(*)" in sql else []
            return job

        client.query.side_effect = query
        return client

    def test_restore_replaces_rows_not_the_table(self, monkeypatch):
        bigquery = pytest.importorskip("google.cloud.bigquery")
        from fluid_build.cli import rollback

        monkeypatch.delenv("BIGQUERY_EMULATOR_HOST", raising=False)
        with patch.object(bigquery, "Client") as client_cls:
            self._client(client_cls)
            rollback._restore_bigquery(self._snapshot(), dry_run=False)
        calls = client_cls.return_value.query.call_args_list
        assert calls, "the restore ran no query"
        for call in calls:
            sql = str(call.args[0] if call.args else call.kwargs.get("query"))
            assert "CREATE OR REPLACE TABLE" not in sql.upper(), (
                "the restore re-creates the governed table, dropping its row access "
                f"policies and column policy tags: {sql}"
            )
        # The data-only replacement: the existing table, WRITE_TRUNCATE_DATA.
        assert calls[-1].args[0] == f"SELECT * FROM `{PROJECT}.{DATASET}.BACKUP_{TABLE}_1`"
        job_config = calls[-1].kwargs.get("job_config")
        assert job_config is not None, "the restore sends no job configuration"
        assert job_config.write_disposition == "WRITE_TRUNCATE_DATA"
        assert job_config.create_disposition == "CREATE_NEVER"
        assert job_config.destination is not None
        assert job_config.destination.project == PROJECT
        assert job_config.destination.dataset_id == DATASET
        assert job_config.destination.table_id == TABLE
        # What BigQuery is sent: a destination table, no DDL.
        assert job_config.to_api_repr()["query"]["destinationTable"] == {
            "projectId": PROJECT,
            "datasetId": DATASET,
            "tableId": TABLE,
        }

    def test_a_filtered_backup_is_not_restored(self, monkeypatch):
        """The restore reads the backup as the operator: a row access policy that
        shows it 2 of 3 rows would leave the table with 2. Nothing is written."""
        bigquery = pytest.importorskip("google.cloud.bigquery")
        from fluid_build.cli import rollback
        from fluid_build.cli._common import CLIError

        monkeypatch.delenv("BIGQUERY_EMULATOR_HOST", raising=False)
        with patch.object(bigquery, "Client") as client_cls:
            client = self._client(client_cls, rows=3, readable=2)
            with pytest.raises(CLIError, match="rollback_bigquery_backup_filtered"):
                rollback._restore_bigquery(self._snapshot(), dry_run=False)
        assert all("job_config" not in c.kwargs for c in client.query.call_args_list)

    def test_rollback_restores_from_the_snapshot_the_replace_took(self, tmp_path):
        """The planner's snapshot marker, recorded by the rollback writer, is what the
        restore reads from: the snapshot is the query's source, the live table its
        destination."""
        from fluid_build.cli import _rollback_writer, rollback

        snap_action = _snapshot_action(_replace_actions())
        with patch(
            "fluid_build.providers.gcp.util.config.resolve_project_and_region",
            return_value=(PROJECT, "europe-west1"),
        ):
            (record,) = _rollback_writer.collect_snapshots_from_actions(
                [snap_action], product_id="silver.customer_profile", env="dev", provider="gcp"
            )
        assert record["ddl"] == [], "no restore DDL is recorded for BigQuery"
        result = rollback._restore_bigquery(copy.deepcopy(record), dry_run=True)
        backup = snap_action["rollback_snapshot"]["backup_name"]
        assert result["ddl"] == f"SELECT * FROM `{PROJECT}.{DATASET}.{backup}`"
        assert result["destination"] == f"`{PROJECT}.{DATASET}.{TABLE}`"
        assert result["create_disposition"] == "CREATE_NEVER"

    def test_provider_restore_ddl_does_not_recreate_the_table(self):
        """``GcpProvider.restore_ddl`` is what ``fluid apply`` records as the
        restore recipe in ``.fluid/rollback-state.json``."""
        from fluid_build.providers.gcp.provider import GcpProvider

        with patch(
            "fluid_build.providers.gcp.util.config.resolve_project_and_region",
            return_value=(PROJECT, "europe-west1"),
        ):
            provider = GcpProvider(project=PROJECT, region="europe-west1")
        ddl = provider.restore_ddl(self._snapshot())
        assert not any("CREATE OR REPLACE TABLE" in s.upper() for s in ddl), ddl
        assert ddl == [], "the restore is a query job, not SQL to record (as AWS)"
