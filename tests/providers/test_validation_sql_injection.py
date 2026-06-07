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

"""Regression tests for the live-quality-check SQL-injection hardening.

A table/dataset/project value sourced from an (unconstrained) contract
``binding`` must never break out of its identifier quoting and reach
``client.query()`` / ``conn.execute()``. Both the BigQuery and Snowflake
validation providers now validate every FQN part and quote each part
individually, failing closed (a ``ValidationIssue`` with NO query issued)
on anything malicious.
"""

from __future__ import annotations

import pytest

from fluid_build.providers import snowflake_validation as sf

# bigquery_validation eagerly imports google.cloud.bigquery at module load, which
# is absent in the minimal unit-test env (the GCP SDK is a provider extra). Guard
# it so collection doesn't error there; the BigQuery cases skip when the SDK is
# missing (they run locally + in GCP-enabled jobs), while the Snowflake + helper
# cases — which exercise the same per-part validate+quote fix — run everywhere.
try:
    from fluid_build.providers import bigquery_validation as bq

    _HAVE_BQ = True
except ImportError:  # pragma: no cover - depends on whether the GCP extra is installed
    bq = None  # type: ignore[assignment]
    _HAVE_BQ = False

_needs_bq = pytest.mark.skipif(not _HAVE_BQ, reason="google.cloud.bigquery not installed")

# A backtick-bearing table that, under the old whole-string single-backtick
# quoting, broke out of `...` and appended a UNION-based exfil subquery.
MALICIOUS_BQ_TABLE = "t` UNION SELECT secret FROM `proj.ds.other"
# A double-quote-bearing table that broke out of "..." on Snowflake.
MALICIOUS_SF_TABLE = 't" UNION SELECT secret FROM other --'

# A single completeness rule is enough to drive one SQL execution.
RULES = [{"id": "r1", "type": "completeness", "selector": "email", "threshold": 1.0}]


# ---------------------------------------------------------------------------
# BigQuery
# ---------------------------------------------------------------------------


class _ExplodingBQClient:
    """A BigQuery client whose .query() fails the test if ever reached."""

    def query(self, sql):  # pragma: no cover - must never be called
        raise AssertionError(f"client.query() was called with unsafe SQL: {sql!r}")


class _FakeBQRow:
    """Mimics a google.cloud.bigquery Row: the provider calls row.values()."""

    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values


class _CapturingBQResult:
    def result(self):
        return [_FakeBQRow((1.0,))]


class _CapturingBQClient:
    def __init__(self):
        self.queries: list[str] = []

    def query(self, sql):
        self.queries.append(sql)
        return _CapturingBQResult()


def _bq_provider(client):
    provider = bq.BigQueryValidationProvider({"project_id": "fallback-proj"})
    provider._client = client  # bypass lazy real-client creation
    return provider


def _resource_with_bq_table(table: str, *, project=None, dataset="ds") -> dict:
    props = {"dataset": dataset, "table": table}
    if project is not None:
        props["project"] = project
    return {"binding": {"location": {"properties": props}}}


@_needs_bq
class TestBigQueryQualityInjection:
    def test_malicious_table_runs_no_query_and_reports_issue(self):
        client = _ExplodingBQClient()
        provider = _bq_provider(client)
        spec = _resource_with_bq_table(MALICIOUS_BQ_TABLE)

        issues = provider.run_quality_checks(spec, RULES)

        # Fail-closed: a ValidationIssue is returned and NO query executed.
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].category == "quality"
        assert "refusing to execute query" in issues[0].message

    def test_malicious_project_runs_no_query_and_reports_issue(self):
        client = _ExplodingBQClient()
        provider = _bq_provider(client)
        spec = _resource_with_bq_table("tbl", project="pr'oj; DROP")

        issues = provider.run_quality_checks(spec, RULES)

        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "refusing to execute query" in issues[0].message

    def test_malicious_resource_string_runs_no_query(self):
        # The binding.resource string-split path must be hardened too.
        client = _ExplodingBQClient()
        provider = _bq_provider(client)
        spec = {"binding": {"resource": f"proj.ds.{MALICIOUS_BQ_TABLE}"}}

        issues = provider.run_quality_checks(spec, RULES)

        assert len(issues) == 1
        assert issues[0].severity == "error"

    def test_legitimate_fqn_builds_per_part_quoted_from(self):
        client = _CapturingBQClient()
        provider = _bq_provider(client)
        spec = _resource_with_bq_table("tbl", project="my-proj-123", dataset="ds")

        issues = provider.run_quality_checks(spec, RULES)

        # Exactly one query ran, with each part individually backtick-quoted.
        assert len(client.queries) == 1
        sql = client.queries[0]
        assert "FROM `my-proj-123`.`ds`.`tbl`" in sql
        # The completeness rule passed (ratio 1.0), so no issue is raised.
        assert issues == []

    def test_legitimate_resource_string_builds_quoted_from(self):
        client = _CapturingBQClient()
        provider = _bq_provider(client)
        spec = {"binding": {"resource": "my-proj-123.ds.tbl"}}

        provider.run_quality_checks(spec, RULES)

        assert len(client.queries) == 1
        assert "FROM `my-proj-123`.`ds`.`tbl`" in client.queries[0]


@_needs_bq
class TestBuildBqTableRef:
    def test_hyphenated_project(self):
        assert bq._build_bq_table_ref("my-proj-123.ds.tbl", None) == "`my-proj-123`.`ds`.`tbl`"

    def test_two_part_uses_default_project(self):
        assert bq._build_bq_table_ref("ds.tbl", "fallback-proj") == "`fallback-proj`.`ds`.`tbl`"

    @pytest.mark.parametrize(
        "bad",
        [
            MALICIOUS_BQ_TABLE,
            "proj.ds.t`x",
            "pr`oj.ds.tbl",
            "a.b.c.d",
            "onlyonepart",
        ],
    )
    def test_malicious_raises(self, bad):
        with pytest.raises(ValueError):
            bq._build_bq_table_ref(bad, "fallback-proj")

    @pytest.mark.parametrize("bad", ["pr'oj", "proj`x", "x", "-leading", "trailing-"])
    def test_quote_project_rejects(self, bad):
        with pytest.raises(ValueError):
            bq._quote_bq_project(bad)


# ---------------------------------------------------------------------------
# Snowflake
# ---------------------------------------------------------------------------


class _ExplodingSFConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError(f"conn.execute() was called with unsafe SQL: {args!r}")


class _CapturingSFConn:
    def __init__(self):
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *args, **kwargs):
        self.queries.append(sql)
        return [(1.0,)]


def _sf_provider(conn, monkeypatch):
    provider = sf.SnowflakeValidationProvider({})
    monkeypatch.setattr(provider, "_connect", lambda: conn)
    return provider


def _resource_with_sf_table(table: str, *, database="DB", schema="SCH") -> dict:
    return {
        "binding": {
            "location": {"properties": {"database": database, "schema": schema, "table": table}}
        }
    }


class TestSnowflakeQualityInjection:
    def test_malicious_table_runs_no_query_and_reports_issue(self, monkeypatch):
        conn = _ExplodingSFConn()
        provider = _sf_provider(conn, monkeypatch)
        spec = _resource_with_sf_table(MALICIOUS_SF_TABLE)

        issues = provider.run_quality_checks(spec, RULES)

        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert issues[0].category == "quality"
        assert "refusing to execute query" in issues[0].message

    def test_malicious_schema_runs_no_query(self, monkeypatch):
        conn = _ExplodingSFConn()
        provider = _sf_provider(conn, monkeypatch)
        spec = _resource_with_sf_table("TBL", schema='S"CH')

        issues = provider.run_quality_checks(spec, RULES)

        assert len(issues) == 1
        assert issues[0].severity == "error"

    def test_legitimate_fqn_builds_per_part_quoted_from(self, monkeypatch):
        conn = _CapturingSFConn()
        provider = _sf_provider(conn, monkeypatch)
        spec = _resource_with_sf_table("TBL", database="DB", schema="SCH")

        issues = provider.run_quality_checks(spec, RULES)

        assert len(conn.queries) == 1
        assert 'FROM "DB"."SCH"."TBL"' in conn.queries[0]
        assert issues == []


class TestBuildSfTableRef:
    def test_normal(self):
        assert sf._build_sf_table_ref(("DB", "SCH", "TBL")) == '"DB"."SCH"."TBL"'

    @pytest.mark.parametrize(
        "bad",
        [
            ("DB", "SCH", 't"; DROP'),
            ('D"B', "SCH", "TBL"),
            ("DB", 'S"CH', "TBL"),
            ("DB", "SCH", "tbl; DROP"),
        ],
    )
    def test_malicious_raises(self, bad):
        with pytest.raises(ValueError):
            sf._build_sf_table_ref(bad)


class TestSnowflakeGetResourceSchemaInjection:
    """``get_resource_schema`` interpolates ``database`` unquoted into
    ``FROM {db}.INFORMATION_SCHEMA.*`` and quotes all three FQN parts into the
    row-count query, so each must be validated before any SQL runs."""

    def test_malicious_database_runs_no_query(self, monkeypatch):
        conn = _ExplodingSFConn()
        provider = _sf_provider(conn, monkeypatch)
        spec = _resource_with_sf_table("TBL", database="DB; DROP SCHEMA x", schema="SCH")
        # Returns None (fail closed) and the exploding conn is never executed.
        assert provider.get_resource_schema(spec) is None

    def test_malicious_table_quote_runs_no_query(self, monkeypatch):
        conn = _ExplodingSFConn()
        provider = _sf_provider(conn, monkeypatch)
        spec = _resource_with_sf_table(MALICIOUS_SF_TABLE)
        assert provider.get_resource_schema(spec) is None

    def test_malicious_schema_quote_runs_no_query(self, monkeypatch):
        conn = _ExplodingSFConn()
        provider = _sf_provider(conn, monkeypatch)
        spec = _resource_with_sf_table("TBL", schema='S"CH')
        assert provider.get_resource_schema(spec) is None
