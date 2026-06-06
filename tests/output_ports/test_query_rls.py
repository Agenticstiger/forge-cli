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

"""Regression tests: ``policy.rowFilters[]`` (row-level security) MUST be
enforced on the ``query`` and ``query_sql`` tools — not just ``sample``.

Before this fix, ``EngineDriver.query()`` executed the compiled statement with
NO row filter (only ``sample()`` applied one), so a contract declaring a
multi-tenant ``rowFilter`` (``tenant_id = ${caller.tenant_id}``) had it silently
BYPASSED on ``tool_query`` / ``tool_query_sql`` — a caller received every
tenant's rows. These tests pin:

1. the row filter merged into the semantic ``query`` WHERE (offset placeholders);
2. the row filter wrapping free-form ``query_sql``;
3. fail-closed (:class:`RowFilterIdentityMissing`) on the query path when a
   referenced caller attribute is absent;
4. the handler wiring actually passes ``caller_attributes`` / ``expose`` through
   (so the compiled SQL the driver executes carries the filter + bound params).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fluid_build.output_ports.mcp._handlers import tool_query, tool_query_sql
from fluid_build.output_ports.mcp.query_compiler import (
    RowFilterIdentityMissing,
    compile_free_form_sql,
    compile_semantic_query,
)

_MEASURES = [{"name": "row_count", "agg": "count", "expr": "id"}]

TENANT_EXPOSE = {
    "exposeId": "demo",
    "semantics": {"measures": _MEASURES},
    "policy": {"rowFilters": [{"column": "tenant_id", "equals": "${caller.tenant_id}"}]},
}


# ---------------------------------------------------------------------------
# Compiler level — query() executes compiled.sql verbatim, so a row filter in
# the compiled SQL IS the executed SQL.
# ---------------------------------------------------------------------------


class TestSemanticQueryRowFilter:
    def test_row_filter_merged_into_where(self):
        compiled = compile_semantic_query(
            expose=TENANT_EXPOSE,
            measure="row_count",
            limit=100,
            caller_attributes={"tenant_id": "acme"},
            table_reference="db.t",
        )
        assert "WHERE" in compiled.sql
        assert '"tenant_id" = :p_0' in compiled.sql
        assert compiled.params == ["acme"]

    def test_row_filter_placeholder_offset_after_user_filter(self):
        # A caller-supplied filter owns :p_0; the row filter MUST take :p_1 so
        # the two never collide into one bound value.
        expose = {**TENANT_EXPOSE, "contract": {"schema": [{"name": "region"}]}}
        compiled = compile_semantic_query(
            expose=expose,
            measure="row_count",
            filters={"region": "us"},
            limit=100,
            caller_attributes={"tenant_id": "acme"},
            table_reference="db.t",
        )
        assert "region = :p_0" in compiled.sql
        assert '"tenant_id" = :p_1' in compiled.sql
        assert compiled.params == ["us", "acme"]

    def test_in_filter_resolves_list(self):
        expose = {
            "exposeId": "demo",
            "semantics": {"measures": _MEASURES},
            "policy": {"rowFilters": [{"column": "region", "in": "${caller.regions}"}]},
        }
        compiled = compile_semantic_query(
            expose=expose,
            measure="row_count",
            limit=10,
            caller_attributes={"regions": ["us", "eu"]},
            table_reference="db.t",
        )
        assert '"region" IN (:p_0, :p_1)' in compiled.sql
        assert compiled.params == ["us", "eu"]

    def test_fail_closed_when_caller_attribute_missing(self):
        with pytest.raises(RowFilterIdentityMissing, match="tenant_id"):
            compile_semantic_query(
                expose=TENANT_EXPOSE,
                measure="row_count",
                limit=100,
                caller_attributes={"model_id": "x"},
                table_reference="db.t",
            )

    def test_no_rowfilter_no_where(self):
        compiled = compile_semantic_query(
            expose={"exposeId": "d", "semantics": {"measures": _MEASURES}},
            measure="row_count",
            limit=10,
            caller_attributes={"tenant_id": "acme"},
            table_reference="db.t",
        )
        assert '"tenant_id"' not in compiled.sql


class TestFreeFormQueryRowFilter:
    def test_row_filter_wraps_caller_sql(self):
        compiled = compile_free_form_sql(
            sql="SELECT id, tenant_id FROM db.t",
            table_reference="db.t",
            limit=50,
            expose=TENANT_EXPOSE,
            caller_attributes={"tenant_id": "acme"},
        )
        assert "SELECT * FROM (" in compiled.sql
        assert '"tenant_id" = :p_0' in compiled.sql
        assert compiled.sql.rstrip().endswith("LIMIT 50")
        assert compiled.params == ["acme"]

    def test_no_wrap_when_no_rowfilters(self):
        compiled = compile_free_form_sql(
            sql="SELECT id FROM db.t",
            table_reference="db.t",
            limit=50,
        )
        assert "SELECT * FROM (" not in compiled.sql
        assert compiled.params == []

    def test_fail_closed_when_caller_attribute_missing(self):
        with pytest.raises(RowFilterIdentityMissing, match="tenant_id"):
            compile_free_form_sql(
                sql="SELECT id FROM db.t",
                table_reference="db.t",
                limit=50,
                expose=TENANT_EXPOSE,
                caller_attributes={"model_id": "x"},
            )


# ---------------------------------------------------------------------------
# Handler level — proves tool_query / tool_query_sql actually thread
# caller_attributes + expose into the compiler (a regression that drops them
# would re-open the bypass even with the compiler fixed).
# ---------------------------------------------------------------------------


class _CapturingDriver:
    def __init__(self):
        self.captured = None
        self._restricted_columns: set = set()
        self._pii_columns: set = set()

    def descriptor(self):
        return SimpleNamespace(
            table_reference="db.t",
            dialect="duckdb",
            platform="local",
            format="csv",
            capabilities={},
        )

    def query(self, *, compiled, timeout_seconds=None):
        self.captured = compiled
        return SimpleNamespace(columns=[], rows=[])


def _fake_state(expose, caller_attributes, driver):
    return SimpleNamespace(
        expose=expose,
        caller_attributes=caller_attributes,
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )


class TestHandlerWiring:
    def test_tool_query_executes_rowfiltered_sql(self):
        driver = _CapturingDriver()
        state = _fake_state(TENANT_EXPOSE, {"tenant_id": "acme"}, driver)
        tool_query(state, {"measure": "row_count", "limit": 10})
        assert driver.captured is not None
        assert '"tenant_id" = :p_0' in driver.captured.sql
        assert "acme" in driver.captured.params

    def test_tool_query_sql_executes_wrapped_rowfiltered_sql(self):
        driver = _CapturingDriver()
        state = _fake_state(TENANT_EXPOSE, {"tenant_id": "acme"}, driver)
        tool_query_sql(state, {"sql": "SELECT id, tenant_id FROM db.t", "limit": 10})
        assert driver.captured is not None
        assert "SELECT * FROM (" in driver.captured.sql
        assert '"tenant_id" = :p_0' in driver.captured.sql
        assert "acme" in driver.captured.params

    def test_tool_query_fail_closed_never_executes(self):
        driver = _CapturingDriver()
        state = _fake_state(TENANT_EXPOSE, {"model_id": "x"}, driver)
        with pytest.raises(RowFilterIdentityMissing):
            tool_query(state, {"measure": "row_count", "limit": 10})
        assert driver.captured is None  # fail-closed: no SQL executed

    def test_tool_query_sql_fail_closed_never_executes(self):
        driver = _CapturingDriver()
        state = _fake_state(TENANT_EXPOSE, {"model_id": "x"}, driver)
        with pytest.raises(RowFilterIdentityMissing):
            tool_query_sql(state, {"sql": "SELECT id FROM db.t", "limit": 10})
        assert driver.captured is None
